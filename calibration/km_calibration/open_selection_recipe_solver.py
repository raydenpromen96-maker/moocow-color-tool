"""Offline open-selection inverse recipe candidates with lattice re-prediction.

This boundary consumes only reverified open train/validation artifacts.  It
never opens holdout data and never grants runtime or production authority.
"""

from __future__ import annotations

import copy
import itertools
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path, PurePosixPath, PureWindowsPath
from stat import S_ISDIR, S_ISLNK, S_ISREG
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, minimize

from .acquisition_preflight import (
    COMPONENT_IDS,
    MASS_SOLIDS_NONVOLATILE_DENSITY,
    PERMISSIONS,
    WET_DENSITY_VOLUME_SOLIDS,
    load_verified_open_acquisition_context,
)
from .errors import CalibrationError
from .hashing import (
    canonical_json_bytes,
    read_verified_json,
    sha256_bytes,
    write_json_with_sha256,
)
from .km import finite_film_reflectance
from .open_measurement_admission import load_and_validate_open_selection_dataset
from .open_selection_fit_export import verify_open_selection_fit_export


REQUEST_SCHEMA = "moocow-open-selection-recipe-request-v1"
CANDIDATE_SCHEMA = "moocow-open-selection-recipe-candidate-v1"
RECEIPT_SCHEMA = "moocow-open-selection-recipe-candidate-receipt-v1"
TARGET_EVIDENCE_SCHEMA = "moocow-open-selection-target-spectrum-evidence-v1"
DISPENSE_EVIDENCE_SCHEMA = "moocow-open-selection-dispense-profile-evidence-v1"
_REQUEST_STATUS = "open_selection_recipe_requested"
_CANDIDATE_STATUS = "laboratory_trial_recipe_candidate"
_STATE = "OPEN_SELECTION_RECIPE_CANDIDATE_EXPORTED"
_FIT_MODEL_SCHEMA = "moocow-open-selection-km-fit-model-v1"
_TARGET_EVIDENCE_STATUS = "open_selection_target_spectrum_recorded"
_DISPENSE_EVIDENCE_STATUS = "open_selection_dispense_profile_recorded"
_EVIDENCE_CLASSES = {"measured_current_target", "synthetic_test_only"}
_BACKINGS = ("black", "white")
_REPARSE = 0x400
_HASH_CHARS = frozenset("0123456789abcdef")
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_TEXT_BYTES = 4096
_MAX_DECIMAL_TEXT_BYTES = 128
_MIN_POSITIVE_DECIMAL = Decimal("1e-12")
_MAX_DECIMAL_MAGNITUDE = Decimal("1e12")
_MAX_DISPENSE_TICKS = 1_000_000_000
_MAX_TOTAL_OPTIMIZER_EVALUATIONS = 2_000_000
_MAX_TARGET_CELLS = 12
_MAX_ALLOWED_COLORANTS = 14
_MAX_ALTERNATIVES = 5
_LATTICE_BATCH_SIZE = 4096
_RETAINED_QUANTIZED_CANDIDATES = 1 + _MAX_ALTERNATIVES
_TICK_RADIUS = 2
_BASE_TICK_RADIUS = 1
_FRACTION_TOLERANCE = 1e-10
_OBJECTIVE_TOLERANCE = 1e-18
_QUANTIZATION_MSE_COMPARISON_TOLERANCE = 1e-15
_PACKAGE_FILES = {
    "recipe-candidate.json",
    "recipe-candidate.json.sha256",
    "recipe-candidate-receipt.json",
    "recipe-candidate-receipt.json.sha256",
}


class OpenSelectionRecipeSolverError(CalibrationError):
    """Stable non-secret-bearing recipe-solver failure."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"[{code}] {path}: {message}")


@dataclass(frozen=True)
class _TargetCell:
    cell_id: str
    backing: str
    dft_um: float
    weight: float
    reflectance: np.ndarray


@dataclass(frozen=True)
class _DispenseComponent:
    component_id: str
    physical_lot_id: str
    increment_g: Decimal
    minimum_nonzero_g: Decimal
    maximum_wet_mass_g: Decimal
    minimum_ticks: int
    maximum_ticks: int


@dataclass(frozen=True)
class _Request:
    value: Mapping[str, Any]
    relative_path: str
    sha256: str
    evidence_class: str
    request_id: str
    target_id: str
    cells: tuple[_TargetCell, ...]
    target_evidence: Mapping[str, Any]
    dispense_evidence: Mapping[str, Any]
    allowed_colorants: tuple[str, ...]
    maximum_colorants: int
    maximum_total_colorant_fraction: float
    per_colorant_maximum: Mapping[str, float]
    target_wet_mass_g: Decimal
    maximum_total_mass_error_g: Decimal
    dispense_components: tuple[_DispenseComponent, ...]
    dispense_profile_id: str


@dataclass(frozen=True)
class _Authority:
    wavelengths: np.ndarray
    component_pairs: tuple[tuple[str, str], ...]
    base_index: int
    colorant_indexes: Mapping[str, int]
    absorption: np.ndarray
    scattering: np.ndarray
    backings: Mapping[str, np.ndarray]
    wet_g_per_nonvolatile_ml: tuple[Decimal, ...]
    material_bindings: tuple[Mapping[str, Any], ...]
    train_dft_range_um: tuple[float, float]
    open_maximum_total_colorant_fraction: float
    open_per_colorant_maximum: Mapping[str, float]
    hashes: Mapping[str, str]


@dataclass(frozen=True)
class _QuantizedSearch:
    candidates: tuple[Mapping[str, Any], ...]
    lattice_evaluations: int


def _fail(code: str, path: str, message: str) -> None:
    raise OpenSelectionRecipeSolverError(code, path, message)


def _permissions() -> dict[str, bool]:
    return {name: False for name in PERMISSIONS}


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("TYPE", path, "must be an object")
    return value


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("TYPE", path, "must be an array")
    return value


def _exact(value: Mapping[str, Any], path: str, fields: Iterable[str]) -> None:
    expected = set(fields)
    actual = set(value)
    if actual != expected:
        _fail("SCHEMA", path, f"must contain exactly {sorted(expected)!r}")


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("TEXT", path, "must be a non-empty string")
    result = value.strip()
    if len(result.encode("utf-8")) > _MAX_TEXT_BYTES:
        _fail("INPUT_LIMIT", path, f"must not exceed {_MAX_TEXT_BYTES} UTF-8 bytes")
    return result


def _finite(value: object, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("NUMBER", path, "must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        _fail("NUMBER", path, "must be a finite positive number" if positive else "must be finite")
    return result


def _integer(value: object, path: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail("INTEGER", path, f"must be an integer in [{minimum}, {maximum}]")
    return value


def _decimal(value: object, path: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _fail("NUMBER", path, "must be a finite decimal value")
    raw = str(value)
    if len(raw.encode("utf-8")) > _MAX_DECIMAL_TEXT_BYTES:
        _fail("INPUT_LIMIT", path, f"must not exceed {_MAX_DECIMAL_TEXT_BYTES} UTF-8 bytes")
    try:
        result = Decimal(raw)
    except InvalidOperation:
        _fail("NUMBER", path, "must be a finite decimal value")
    if not result.is_finite() or (positive and result <= 0):
        _fail("NUMBER", path, "must be a finite positive decimal" if positive else "must be finite")
    if abs(result) > _MAX_DECIMAL_MAGNITUDE or (
        positive and result < _MIN_POSITIVE_DECIMAL
    ):
        _fail("INPUT_LIMIT", path, "is outside the supported decimal magnitude")
    return result


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _hash(value: object, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in _HASH_CHARS for character in value):
        _fail("HASH", path, "must be a lowercase SHA-256 digest")
    return value


def _link_or_reparse(stat: os.stat_result) -> bool:
    return S_ISLNK(stat.st_mode) or bool(getattr(stat, "st_file_attributes", 0) & _REPARSE)


def _safe_directory_chain(directory: Path, *, code: str, path: str, create: bool) -> Path:
    try:
        absolute = directory.absolute()
        anchor = Path(absolute.anchor)
        relative = absolute.relative_to(anchor)
    except (OSError, ValueError) as error:
        _fail(code, path, str(error))
    current = anchor
    for part in ("", *relative.parts):
        if part:
            current = current / part
        try:
            stat = current.lstat()
        except FileNotFoundError:
            if not create:
                _fail(code, str(current), "directory does not exist")
            try:
                current.mkdir()
                stat = current.lstat()
            except OSError as error:
                _fail(code, str(current), str(error))
        except OSError as error:
            _fail(code, str(current), str(error))
        if _link_or_reparse(stat) or not S_ISDIR(stat.st_mode):
            _fail(code, str(current), "must be a non-link directory")
    return absolute


def _root(value: Path | str, path: str, *, create: bool = False) -> Path:
    return _safe_directory_chain(Path(value), code="ROOT", path=path, create=create)


def _safe_relative(value: object, path: str) -> str:
    text = _text(value, path)
    pure = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        "\\" in text
        or "\x00" in text
        or any(character in '<>:"|?*' for character in text)
        or pure.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or not pure.parts
        or any(part in ("", ".", "..") or part.endswith((".", " ")) for part in pure.parts)
    ):
        _fail("PATH", path, "must be a portable relative path")
    return pure.as_posix()


def _read_bound_json_evidence(
    value: object,
    *,
    root: Path,
    path: str,
    seen_paths: set[str],
    seen_hashes: set[str],
) -> tuple[dict[str, object], Mapping[str, Any]]:
    record = _mapping(value, path)
    _exact(record, path, ("relative_path", "sha256", "record_locator"))
    relative = _safe_relative(record.get("relative_path"), f"{path}.relative_path")
    if not relative.endswith(".json"):
        _fail("EVIDENCE", f"{path}.relative_path", "must name a structured .json evidence record")
    expected_hash = _hash(record.get("sha256"), f"{path}.sha256")
    locator = _mapping(record.get("record_locator"), f"{path}.record_locator")
    if dict(locator) != {"kind": "whole_file"}:
        _fail("EVIDENCE", f"{path}.record_locator", "must be exactly whole_file")
    evidence_path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        stat = evidence_path.lstat()
        if _link_or_reparse(stat) or not S_ISREG(stat.st_mode):
            _fail("EVIDENCE", relative, "must be a non-link regular JSON file")
        if not 1 <= stat.st_size <= _MAX_JSON_BYTES:
            _fail("INPUT_LIMIT", relative, f"must contain 1..{_MAX_JSON_BYTES} bytes")
        payload_value, actual_hash = read_verified_json(
            evidence_path,
            expected_sha256=expected_hash,
            trusted_root=root,
        )
    except OpenSelectionRecipeSolverError:
        raise
    except CalibrationError as error:
        _fail("EVIDENCE", relative, str(error))
    except OSError as error:
        _fail("EVIDENCE", relative, str(error))
    if actual_hash != expected_hash:
        _fail("EVIDENCE", relative, "does not match the request SHA-256")
    if relative in seen_paths or actual_hash in seen_hashes:
        _fail("EVIDENCE_REUSE", relative, "must have a unique path and SHA-256")
    seen_paths.add(relative)
    seen_hashes.add(actual_hash)
    payload = _mapping(payload_value, relative)
    binding = {
        "relative_path": relative,
        "sha256": actual_hash,
        "record_locator": {"kind": "whole_file"},
        "record_schema_version": _text(payload.get("schema_version"), f"{relative}.schema_version"),
    }
    return binding, payload


def _request_path(root: Path, relative: object) -> tuple[Path, str]:
    normalized = _safe_relative(relative, "request_relative_path")
    if not normalized.endswith(".json"):
        _fail("PATH", "request_relative_path", "must name a .json request")
    return root.joinpath(*PurePosixPath(normalized).parts), normalized


def _read_request_json(root: Path, relative: object) -> tuple[Mapping[str, Any], str, str]:
    path, normalized = _request_path(root, relative)
    try:
        stat = path.lstat()
        if _link_or_reparse(stat) or not S_ISREG(stat.st_mode):
            _fail("REQUEST", normalized, "must be a non-link regular file")
        if stat.st_size > _MAX_JSON_BYTES:
            _fail("INPUT_LIMIT", normalized, f"must not exceed {_MAX_JSON_BYTES} bytes")
        value, digest = read_verified_json(path, require_sidecar=True, trusted_root=root)
    except OpenSelectionRecipeSolverError:
        raise
    except (OSError, CalibrationError) as error:
        _fail("REQUEST", normalized, str(error))
    return _mapping(value, "request"), normalized, digest


def _model_array(value: object, *, length: int, path: str, positive: bool) -> np.ndarray:
    items = _list(value, path)
    if len(items) != length:
        _fail("MODEL", path, "must match the model wavelength grid")
    result = np.asarray([_finite(item, f"{path}[{index}]") for index, item in enumerate(items)], dtype=float)
    if (positive and np.any(result <= 0.0)) or (not positive and np.any(result < 0.0)):
        _fail("MODEL", path, "contains an invalid optical coefficient")
    return result


def _conversion_factor(material: Mapping[str, Any], path: str) -> tuple[str, Decimal]:
    route = _text(material.get("conversion_route"), f"{path}.conversion_route")
    properties = _mapping(material.get("properties"), f"{path}.properties")
    try:
        if route == MASS_SOLIDS_NONVOLATILE_DENSITY:
            mass_fraction = _decimal(
                _mapping(properties.get("nonvolatile_mass_fraction"), f"{path}.properties.nonvolatile_mass_fraction").get("value"),
                f"{path}.properties.nonvolatile_mass_fraction.value",
                positive=True,
            )
            nonvolatile_density = _decimal(
                _mapping(properties.get("nonvolatile_density_g_ml"), f"{path}.properties.nonvolatile_density_g_ml").get("value"),
                f"{path}.properties.nonvolatile_density_g_ml.value",
                positive=True,
            )
            if mass_fraction > 1:
                _fail("PROPERTY_ROUTE", f"{path}.properties.nonvolatile_mass_fraction.value", "must not exceed one")
            factor = nonvolatile_density / mass_fraction
        elif route == WET_DENSITY_VOLUME_SOLIDS:
            wet_density = _decimal(
                _mapping(properties.get("wet_density_g_ml"), f"{path}.properties.wet_density_g_ml").get("value"),
                f"{path}.properties.wet_density_g_ml.value",
                positive=True,
            )
            volume_fraction = _decimal(
                _mapping(
                    properties.get("component_nonvolatile_volume_fraction"),
                    f"{path}.properties.component_nonvolatile_volume_fraction",
                ).get("value"),
                f"{path}.properties.component_nonvolatile_volume_fraction.value",
                positive=True,
            )
            if volume_fraction > 1:
                _fail(
                    "PROPERTY_ROUTE",
                    f"{path}.properties.component_nonvolatile_volume_fraction.value",
                    "must not exceed one",
                )
            factor = wet_density / volume_fraction
        else:
            _fail("PROPERTY_ROUTE", f"{path}.conversion_route", "is not supported")
    except (InvalidOperation, ZeroDivisionError):
        _fail("PROPERTY_ROUTE", path, "does not produce a finite positive wet-mass conversion")
    if not factor.is_finite() or factor <= 0:
        _fail("PROPERTY_ROUTE", path, "does not produce a finite positive wet-mass conversion")
    if factor > _MAX_DECIMAL_MAGNITUDE:
        _fail("PROPERTY_ROUTE", path, "produces an unsupported wet-mass conversion magnitude")
    return route, factor


def _derive_model_domain(
    dataset: Any,
    component_pairs: Sequence[tuple[str, str]],
) -> tuple[tuple[float, float], float, dict[str, float]]:
    measurements = _list(dataset.source.get("measurements"), "sources.open-measurements.measurements")
    train_dft: list[float] = []
    total_maximum = 0.0
    per_colorant = {component_id: 0.0 for component_id, _lot_id in component_pairs[1:]}
    for index, measurement_value in enumerate(measurements):
        path = f"sources.open-measurements.measurements[{index}]"
        measurement = _mapping(measurement_value, path)
        split = _text(measurement.get("split"), f"{path}.split")
        if split not in {"train", "validation"}:
            _fail("MODEL_DOMAIN", f"{path}.split", "must remain train or validation")
        dft_um = _finite(measurement.get("dft_um"), f"{path}.dft_um", positive=True)
        if split == "train":
            train_dft.append(dft_um)
        components = _list(measurement.get("components"), f"{path}.components")
        if len(components) != len(component_pairs):
            _fail("MODEL_DOMAIN", f"{path}.components", "must retain all current-lot components")
        fractions: list[float] = []
        for component_index, (component_value, expected_pair) in enumerate(
            zip(components, component_pairs, strict=True)
        ):
            component_path = f"{path}.components[{component_index}]"
            component = _mapping(component_value, component_path)
            pair = (
                _text(component.get("component_id"), f"{component_path}.component_id"),
                _text(component.get("physical_lot_id"), f"{component_path}.physical_lot_id"),
            )
            if pair != expected_pair:
                _fail("MODEL_DOMAIN", component_path, "does not match the current model component/lot order")
            fraction = _finite(
                component.get("nonvolatile_volume_fraction"),
                f"{component_path}.nonvolatile_volume_fraction",
            )
            if not 0.0 <= fraction <= 1.0:
                _fail("MODEL_DOMAIN", f"{component_path}.nonvolatile_volume_fraction", "must be in [0,1]")
            fractions.append(fraction)
        if not math.isclose(math.fsum(fractions), 1.0, rel_tol=0.0, abs_tol=1e-9):
            _fail("MODEL_DOMAIN", f"{path}.components", "must sum to one")
        colorant_total = math.fsum(fractions[1:])
        total_maximum = max(total_maximum, colorant_total)
        for component_index, (component_id, _lot_id) in enumerate(component_pairs[1:], start=1):
            per_colorant[component_id] = max(per_colorant[component_id], fractions[component_index])

    if not train_dft or min(train_dft) >= max(train_dft):
        _fail("MODEL_DOMAIN", "train.dft_um", "must contain a non-degenerate measured DFT interval")
    if not 0.0 < total_maximum < 1.0 or any(value <= 0.0 for value in per_colorant.values()):
        _fail("MODEL_DOMAIN", "open.components", "must expose every colorant in a positive bounded domain")
    return (min(train_dft), max(train_dft)), total_maximum, per_colorant


def _load_authority(
    *,
    acquisition_receipt_path: Path | str,
    admission_receipt_path: Path | str,
    dataset_root: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
    fit_export_root: Path | str,
) -> _Authority:
    verification = verify_open_selection_fit_export(
        acquisition_receipt_path=acquisition_receipt_path,
        admission_receipt_path=admission_receipt_path,
        dataset_root=dataset_root,
        shared_root=shared_root,
        open_root=open_root,
        measurement_root=measurement_root,
        export_root=fit_export_root,
    )
    if (
        verification.get("status") != "open_selection_fit_export_verified"
        or verification.get("state") != "OPEN_SELECTION_FIT_EXPORTED"
        or verification.get("runtime_compatible") is not False
        or verification.get("production_pass") is not False
        or any(verification.get(permission) is not False for permission in PERMISSIONS)
    ):
        _fail("AUTHORITY", "fit_export_root", "did not pass the open-selection fit-export boundary")

    context = load_verified_open_acquisition_context(
        receipt_path=acquisition_receipt_path,
        shared_root=shared_root,
        open_root=open_root,
    )
    dataset = load_and_validate_open_selection_dataset(dataset_root)
    export = _root(fit_export_root, "fit_export_root")
    try:
        model_value, model_sha = read_verified_json(
            export / "fit-model.json",
            require_sidecar=True,
            trusted_root=export,
        )
        evaluation_value, evaluation_sha = read_verified_json(
            export / "selection-evaluation.json",
            require_sidecar=True,
            trusted_root=export,
        )
        receipt_value, receipt_sha = read_verified_json(
            export / "fit-export-receipt.json",
            require_sidecar=True,
            trusted_root=export,
        )
        _admission_value, admission_sha = read_verified_json(
            Path(admission_receipt_path),
            require_sidecar=True,
        )
    except CalibrationError as error:
        _fail("AUTHORITY", "fit_export_root", str(error))
    model = _mapping(model_value, "fit-model.json")
    _mapping(evaluation_value, "selection-evaluation.json")
    _mapping(receipt_value, "fit-export-receipt.json")
    loaded_hashes = {
        "acquisition_preflight_receipt_sha256": _hash(
            context.get("acquisition_preflight_receipt_sha256"),
            "acquisition_preflight_receipt_sha256",
        ),
        "admission_receipt_sha256": admission_sha,
        "dataset_manifest_sha256": _hash(dataset.manifest_sha256, "dataset.manifest_sha256"),
        "open_measurements_sha256": _hash(
            dataset.open_measurements_sha256,
            "dataset.open_measurements_sha256",
        ),
        "fit_model_sha256": model_sha,
        "selection_evaluation_sha256": evaluation_sha,
        "fit_export_receipt_sha256": receipt_sha,
    }
    verified_hashes = {
        name: _hash(verification.get(name), f"fit_export_verification.{name}")
        for name in loaded_hashes
    }
    if loaded_hashes != verified_hashes:
        _fail("AUTHORITY_DRIFT", "authority", "a predecessor or fit artifact changed after verification")
    if (
        model.get("schema_version") != _FIT_MODEL_SCHEMA
        or model.get("dataset_status") != "open_selection_only"
        or model.get("status") != "open_selection_fit_candidate"
        or model.get("runtime_compatible") is not False
        or model.get("production_pass") is not False
        or any(model.get(permission) is not False for permission in PERMISSIONS)
        or model.get("saunderson") != {"mode": "off"}
    ):
        _fail("AUTHORITY", "fit-model.json", "is not an inactive open-selection model")

    wavelengths = np.asarray(
        [_finite(value, f"fit-model.json.wavelength_nm[{index}]") for index, value in enumerate(_list(model.get("wavelength_nm"), "fit-model.json.wavelength_nm"))],
        dtype=float,
    )
    if len(wavelengths) < 3 or not np.all(np.diff(wavelengths) > 0.0):
        _fail("MODEL", "fit-model.json.wavelength_nm", "must be a strictly increasing grid")

    component_order = _list(model.get("component_order"), "fit-model.json.component_order")
    components = _list(model.get("components"), "fit-model.json.components")
    materials = _list(context.get("materials"), "acquisition.materials")
    if len(component_order) != 15 or len(components) != 15 or len(materials) != 15:
        _fail("COMPONENTS", "authority", "must retain exactly 15 current-lot components")

    pairs: list[tuple[str, str]] = []
    absorption: list[np.ndarray] = []
    scattering: list[np.ndarray] = []
    factors: list[Decimal] = []
    material_bindings: list[Mapping[str, Any]] = []
    routes: set[str] = set()
    for index, (order_value, component_value, material_value) in enumerate(
        zip(component_order, components, materials, strict=True)
    ):
        order = _mapping(order_value, f"fit-model.json.component_order[{index}]")
        component = _mapping(component_value, f"fit-model.json.components[{index}]")
        material = _mapping(material_value, f"acquisition.materials[{index}]")
        pair = (
            _text(order.get("component_id"), f"fit-model.json.component_order[{index}].component_id"),
            _text(order.get("physical_lot_id"), f"fit-model.json.component_order[{index}].physical_lot_id"),
        )
        if pair != (
            component.get("component_id"),
            component.get("physical_lot_id"),
        ) or pair != (
            material.get("component_id"),
            material.get("physical_lot_id"),
        ):
            _fail("COMPONENTS", f"authority.components[{index}]", "model and current-lot material identities differ")
        pairs.append(pair)
        absorption.append(
            _model_array(
                component.get("K_mm_inv"),
                length=len(wavelengths),
                path=f"fit-model.json.components[{index}].K_mm_inv",
                positive=False,
            )
        )
        scattering.append(
            _model_array(
                component.get("S_mm_inv"),
                length=len(wavelengths),
                path=f"fit-model.json.components[{index}].S_mm_inv",
                positive=True,
            )
        )
        route, factor = _conversion_factor(material, f"acquisition.materials[{index}]")
        routes.add(route)
        factors.append(factor)
        property_records = _mapping(material.get("properties"), f"acquisition.materials[{index}].properties")
        property_record_ids = sorted(
            _text(
                _mapping(record, f"acquisition.materials[{index}].properties.{name}").get("property_record_id"),
                f"acquisition.materials[{index}].properties.{name}.property_record_id",
            )
            for name, record in property_records.items()
        )
        property_evidence = _mapping(
            material.get("property_evidence"),
            f"acquisition.materials[{index}].property_evidence",
        )
        material_bindings.append(
            {
                "component_id": pair[0],
                "physical_lot_id": pair[1],
                "conversion_route": route,
                "wet_g_per_nonvolatile_ml_decimal": _decimal_text(factor),
                "property_record_ids": property_record_ids,
                "property_evidence": {
                    "relative_path": _text(
                        property_evidence.get("relative_path"),
                        f"acquisition.materials[{index}].property_evidence.relative_path",
                    ),
                    "sha256": _hash(
                        property_evidence.get("file_sha256"),
                        f"acquisition.materials[{index}].property_evidence.file_sha256",
                    ),
                },
            }
        )
    if tuple(component_id for component_id, _lot_id in pairs) != tuple(COMPONENT_IDS):
        _fail("COMPONENTS", "fit-model.json.component_order", "does not retain the fixed component order")
    if len(set(pairs)) != 15 or len(routes) != 1:
        _fail("PROPERTY_ROUTE", "acquisition.materials", "must retain unique lots and one common conversion route")

    manifest_wavelengths = np.asarray(
        [_finite(value, f"manifest.wavelength_nm[{index}]") for index, value in enumerate(_list(dataset.manifest.get("wavelength_nm"), "manifest.wavelength_nm"))],
        dtype=float,
    )
    if not np.array_equal(wavelengths, manifest_wavelengths):
        _fail("MODEL", "manifest.wavelength_nm", "does not exactly match the verified model")
    backings_value = _mapping(dataset.manifest.get("backings"), "manifest.backings")
    backings: dict[str, np.ndarray] = {}
    for backing in _BACKINGS:
        record = _mapping(backings_value.get(backing), f"manifest.backings.{backing}")
        values = np.asarray(
            [_finite(value, f"manifest.backings.{backing}.mean_reflectance[{index}]") for index, value in enumerate(_list(record.get("mean_reflectance"), f"manifest.backings.{backing}.mean_reflectance"))],
            dtype=float,
        )
        if values.shape != wavelengths.shape or np.any(values < 0.0) or np.any(values > 1.0):
            _fail("BACKING", f"manifest.backings.{backing}.mean_reflectance", "must match the model grid in [0,1]")
        backings[backing] = values
    if np.allclose(backings["black"], backings["white"], rtol=0.0, atol=1e-12):
        _fail("BACKING", "manifest.backings", "black and white means must differ")
    train_dft_range, open_total_maximum, open_per_colorant = _derive_model_domain(dataset, pairs)

    return _Authority(
        wavelengths=wavelengths,
        component_pairs=tuple(pairs),
        base_index=0,
        colorant_indexes={component_id: index for index, (component_id, _lot_id) in enumerate(pairs) if index != 0},
        absorption=np.vstack(absorption),
        scattering=np.vstack(scattering),
        backings=backings,
        wet_g_per_nonvolatile_ml=tuple(factors),
        material_bindings=tuple(copy.deepcopy(material_bindings)),
        train_dft_range_um=train_dft_range,
        open_maximum_total_colorant_fraction=open_total_maximum,
        open_per_colorant_maximum=open_per_colorant,
        hashes=loaded_hashes,
    )


def _tick_count(value: Decimal, increment: Decimal, path: str) -> int:
    quotient = value / increment
    integral = quotient.to_integral_value()
    if quotient != integral:
        _fail("DISPENSE_PROFILE", path, "must be an exact integer number of increments")
    result = int(integral)
    if not 1 <= result <= _MAX_DISPENSE_TICKS:
        _fail("DISPENSE_PROFILE", path, f"must contain 1..{_MAX_DISPENSE_TICKS} increments")
    return result


def _parse_request(
    value: Mapping[str, Any],
    *,
    relative_path: str,
    request_sha256: str,
    request_root: Path,
    authority: _Authority,
) -> _Request:
    _exact(value, "request", ("schema_version", "status", "request_id", "evidence_class", "target", "search_policy", "batch"))
    if value.get("schema_version") != REQUEST_SCHEMA or value.get("status") != _REQUEST_STATUS:
        _fail("REQUEST", "request", "has an unsupported schema or status")
    request_id = _text(value.get("request_id"), "request.request_id")
    evidence_class = _text(value.get("evidence_class"), "request.evidence_class")
    if evidence_class not in _EVIDENCE_CLASSES:
        _fail("EVIDENCE_CLASS", "request.evidence_class", "must be measured_current_target or synthetic_test_only")

    evidence_paths: set[str] = set()
    evidence_hashes: set[str] = set()
    target = _mapping(value.get("target"), "request.target")
    _exact(target, "request.target", ("evidence",))
    target_evidence, target_record = _read_bound_json_evidence(
        target.get("evidence"),
        root=request_root,
        path="request.target.evidence",
        seen_paths=evidence_paths,
        seen_hashes=evidence_hashes,
    )
    _exact(
        target_record,
        "target_evidence_record",
        ("schema_version", "status", "evidence_class", "target_id", "wavelength_nm", "cells"),
    )
    if (
        target_record.get("schema_version") != TARGET_EVIDENCE_SCHEMA
        or target_record.get("status") != _TARGET_EVIDENCE_STATUS
    ):
        _fail("TARGET_EVIDENCE", "target_evidence_record", "has an unsupported schema or status")
    if target_record.get("evidence_class") != evidence_class:
        _fail("TARGET_EVIDENCE", "target_evidence_record.evidence_class", "must match the request evidence class")
    target_id = _text(target_record.get("target_id"), "target_evidence_record.target_id")
    target_wavelengths = np.asarray(
        [
            _finite(item, f"target_evidence_record.wavelength_nm[{index}]")
            for index, item in enumerate(
                _list(target_record.get("wavelength_nm"), "target_evidence_record.wavelength_nm")
            )
        ],
        dtype=float,
    )
    if not np.array_equal(target_wavelengths, authority.wavelengths):
        _fail("TARGET_GRID", "target_evidence_record.wavelength_nm", "must exactly match the verified model grid")
    cell_values = _list(target_record.get("cells"), "target_evidence_record.cells")
    if not 1 <= len(cell_values) <= _MAX_TARGET_CELLS:
        _fail("TARGET_CELLS", "target_evidence_record.cells", f"must contain 1..{_MAX_TARGET_CELLS} cells")
    cells: list[_TargetCell] = []
    seen_cell_ids: set[str] = set()
    seen_conditions: set[tuple[str, float]] = set()
    for index, cell_value in enumerate(cell_values):
        path = f"target_evidence_record.cells[{index}]"
        cell = _mapping(cell_value, path)
        _exact(cell, path, ("cell_id", "backing", "dft_um", "weight", "reflectance"))
        cell_id = _text(cell.get("cell_id"), f"{path}.cell_id")
        backing = _text(cell.get("backing"), f"{path}.backing")
        dft_um = _finite(cell.get("dft_um"), f"{path}.dft_um", positive=True)
        weight = _finite(cell.get("weight"), f"{path}.weight", positive=True)
        if backing not in _BACKINGS:
            _fail("TARGET_CELLS", f"{path}.backing", "must be black or white")
        if dft_um > 5000.0:
            _fail("TARGET_CELLS", f"{path}.dft_um", "must not exceed 5000 um")
        if not authority.train_dft_range_um[0] <= dft_um <= authority.train_dft_range_um[1]:
            _fail(
                "TARGET_DFT_EXTRAPOLATION",
                f"{path}.dft_um",
                "must stay within the measured train DFT interval",
            )
        condition = (backing, dft_um)
        if cell_id in seen_cell_ids or condition in seen_conditions:
            _fail("TARGET_CELLS", path, "duplicates a cell ID or backing/DFT condition")
        reflectance = np.asarray(
            [_finite(item, f"{path}.reflectance[{position}]") for position, item in enumerate(_list(cell.get("reflectance"), f"{path}.reflectance"))],
            dtype=float,
        )
        if reflectance.shape != authority.wavelengths.shape or np.any(reflectance < 0.0) or np.any(reflectance > 1.0):
            _fail("TARGET_REFLECTANCE", f"{path}.reflectance", "must match the model grid in [0,1]")
        seen_cell_ids.add(cell_id)
        seen_conditions.add(condition)
        cells.append(_TargetCell(cell_id=cell_id, backing=backing, dft_um=dft_um, weight=weight, reflectance=reflectance))

    policy = _mapping(value.get("search_policy"), "request.search_policy")
    _exact(
        policy,
        "request.search_policy",
        (
            "allowed_colorant_component_ids",
            "maximum_colorants",
            "maximum_total_colorant_nonvolatile_volume_fraction",
            "per_colorant_maximum_nonvolatile_volume_fraction",
        ),
    )
    allowed_values = _list(policy.get("allowed_colorant_component_ids"), "request.search_policy.allowed_colorant_component_ids")
    if not 1 <= len(allowed_values) <= _MAX_ALLOWED_COLORANTS:
        _fail("SEARCH_POLICY", "request.search_policy.allowed_colorant_component_ids", "must contain 1..14 colorants")
    allowed = tuple(
        _text(item, f"request.search_policy.allowed_colorant_component_ids[{index}]")
        for index, item in enumerate(allowed_values)
    )
    if len(set(allowed)) != len(allowed) or any(item not in authority.colorant_indexes for item in allowed):
        _fail("SEARCH_POLICY", "request.search_policy.allowed_colorant_component_ids", "must contain unique current model colorants")
    expected_order = tuple(sorted(allowed, key=lambda item: authority.colorant_indexes[item]))
    if allowed != expected_order:
        _fail("SEARCH_POLICY", "request.search_policy.allowed_colorant_component_ids", "must retain fixed model order")
    maximum_colorants = _integer(
        policy.get("maximum_colorants"),
        "request.search_policy.maximum_colorants",
        minimum=1,
        maximum=3,
    )
    if maximum_colorants > len(allowed):
        _fail("SEARCH_POLICY", "request.search_policy.maximum_colorants", "must not exceed the allowed colorant count")
    total_maximum = _finite(
        policy.get("maximum_total_colorant_nonvolatile_volume_fraction"),
        "request.search_policy.maximum_total_colorant_nonvolatile_volume_fraction",
        positive=True,
    )
    if total_maximum >= 1.0:
        _fail("SEARCH_POLICY", "request.search_policy.maximum_total_colorant_nonvolatile_volume_fraction", "must be less than one")
    if total_maximum > authority.open_maximum_total_colorant_fraction + _FRACTION_TOLERANCE:
        _fail(
            "SEARCH_EXTRAPOLATION",
            "request.search_policy.maximum_total_colorant_nonvolatile_volume_fraction",
            "must not exceed the observed open-selection total-colorant domain",
        )
    per_max_value = _mapping(
        policy.get("per_colorant_maximum_nonvolatile_volume_fraction"),
        "request.search_policy.per_colorant_maximum_nonvolatile_volume_fraction",
    )
    if set(per_max_value) != set(allowed):
        _fail("SEARCH_POLICY", "request.search_policy.per_colorant_maximum_nonvolatile_volume_fraction", "must cover exactly the allowed colorants")
    per_maximum = {
        component_id: _finite(
            per_max_value[component_id],
            f"request.search_policy.per_colorant_maximum_nonvolatile_volume_fraction.{component_id}",
            positive=True,
        )
        for component_id in allowed
    }
    if any(limit > total_maximum for limit in per_maximum.values()):
        _fail("SEARCH_POLICY", "request.search_policy.per_colorant_maximum_nonvolatile_volume_fraction", "each maximum must not exceed the total maximum")
    for component_id, limit in per_maximum.items():
        if limit > authority.open_per_colorant_maximum[component_id] + _FRACTION_TOLERANCE:
            _fail(
                "SEARCH_EXTRAPOLATION",
                f"request.search_policy.per_colorant_maximum_nonvolatile_volume_fraction.{component_id}",
                "must not exceed the observed open-selection component domain",
            )

    batch = _mapping(value.get("batch"), "request.batch")
    _exact(batch, "request.batch", ("target_wet_mass_g", "dispense_profile_evidence"))
    target_wet_mass = _decimal(batch.get("target_wet_mass_g"), "request.batch.target_wet_mass_g", positive=True)
    dispense_evidence, profile = _read_bound_json_evidence(
        batch.get("dispense_profile_evidence"),
        root=request_root,
        path="request.batch.dispense_profile_evidence",
        seen_paths=evidence_paths,
        seen_hashes=evidence_hashes,
    )
    _exact(
        profile,
        "dispense_profile_evidence_record",
        ("schema_version", "status", "profile_id", "maximum_total_mass_error_g", "components"),
    )
    if (
        profile.get("schema_version") != DISPENSE_EVIDENCE_SCHEMA
        or profile.get("status") != _DISPENSE_EVIDENCE_STATUS
    ):
        _fail("DISPENSE_EVIDENCE", "dispense_profile_evidence_record", "has an unsupported schema or status")
    profile_id = _text(profile.get("profile_id"), "dispense_profile_evidence_record.profile_id")
    maximum_mass_error = _decimal(
        profile.get("maximum_total_mass_error_g"),
        "dispense_profile_evidence_record.maximum_total_mass_error_g",
        positive=True,
    )
    if maximum_mass_error >= target_wet_mass:
        _fail("DISPENSE_PROFILE", "dispense_profile_evidence_record.maximum_total_mass_error_g", "must be smaller than the target wet mass")
    profile_components = _list(profile.get("components"), "dispense_profile_evidence_record.components")
    if len(profile_components) != len(authority.component_pairs):
        _fail("DISPENSE_PROFILE", "dispense_profile_evidence_record.components", "must contain all 15 components")
    dispense_components: list[_DispenseComponent] = []
    for index, (component_value, expected_pair) in enumerate(zip(profile_components, authority.component_pairs, strict=True)):
        path = f"dispense_profile_evidence_record.components[{index}]"
        component = _mapping(component_value, path)
        _exact(component, path, ("component_id", "physical_lot_id", "increment_g", "minimum_nonzero_g", "maximum_wet_mass_g"))
        pair = (
            _text(component.get("component_id"), f"{path}.component_id"),
            _text(component.get("physical_lot_id"), f"{path}.physical_lot_id"),
        )
        if pair != expected_pair:
            _fail("DISPENSE_PROFILE", path, "does not match the current model component/lot order")
        increment = _decimal(component.get("increment_g"), f"{path}.increment_g", positive=True)
        minimum = _decimal(component.get("minimum_nonzero_g"), f"{path}.minimum_nonzero_g", positive=True)
        maximum = _decimal(component.get("maximum_wet_mass_g"), f"{path}.maximum_wet_mass_g", positive=True)
        minimum_ticks = _tick_count(minimum, increment, f"{path}.minimum_nonzero_g")
        maximum_ticks = _tick_count(maximum, increment, f"{path}.maximum_wet_mass_g")
        if maximum_ticks < minimum_ticks:
            _fail("DISPENSE_PROFILE", path, "maximum wet mass must not be below the minimum")
        dispense_components.append(
            _DispenseComponent(
                component_id=pair[0],
                physical_lot_id=pair[1],
                increment_g=increment,
                minimum_nonzero_g=minimum,
                maximum_wet_mass_g=maximum,
                minimum_ticks=minimum_ticks,
                maximum_ticks=maximum_ticks,
            )
        )
    if dispense_components[authority.base_index].minimum_nonzero_g >= target_wet_mass:
        _fail("DISPENSE_PROFILE", "dispense_profile_evidence_record.components[0]", "base minimum must be below target wet mass")

    return _Request(
        value=copy.deepcopy(dict(value)),
        relative_path=relative_path,
        sha256=request_sha256,
        evidence_class=evidence_class,
        request_id=request_id,
        target_id=target_id,
        cells=tuple(cells),
        target_evidence=target_evidence,
        dispense_evidence=dispense_evidence,
        allowed_colorants=allowed,
        maximum_colorants=maximum_colorants,
        maximum_total_colorant_fraction=total_maximum,
        per_colorant_maximum=per_maximum,
        target_wet_mass_g=target_wet_mass,
        maximum_total_mass_error_g=maximum_mass_error,
        dispense_components=tuple(dispense_components),
        dispense_profile_id=profile_id,
    )


def _algorithm_spec() -> dict[str, object]:
    return {
        "schema_version": "moocow-open-selection-recipe-solver-spec-v1",
        "continuous_solver": {
            "method": "SLSQP",
            "objective": "target_weight_normalized_mean_squared_spectral_residual",
            "max_iterations": 300,
            "ftol": 1e-14,
            "start_scales": [0.0, 0.25, 0.60, 0.95],
            "support_enumeration": "all_zero_through_maximum_colorants",
            "heuristic_support_prefilter": False,
            "incomplete_support_policy": "fail_closed_without_output",
            "maximum_total_objective_evaluations": _MAX_TOTAL_OPTIMIZER_EVALUATIONS,
        },
        "quantization": {
            "colorant_tick_radius": _TICK_RADIUS,
            "base_tick_radius": _BASE_TICK_RADIUS,
            "objective_batch_size": _LATTICE_BATCH_SIZE,
            "retained_ranked_candidates": _RETAINED_QUANTIZED_CANDIDATES,
            "streaming_top_k": True,
            "quantization_search_exhaustive_within_neighborhood": True,
            "global_lattice_optimum_proven": False,
            "post_quantization_reprediction": True,
        },
        "ranking": [
            "quantized_spectral_mse",
            "absolute_total_mass_error_g",
            "active_colorant_count",
            "total_colorant_wet_mass_g",
            "canonical_tick_tuple",
        ],
    }


def _fractions_from_support(
    authority: _Authority,
    support: Sequence[str],
    colorant_values: Sequence[float],
) -> np.ndarray:
    fractions = np.zeros(len(authority.component_pairs), dtype=float)
    for component_id, value in zip(support, colorant_values, strict=True):
        fractions[authority.colorant_indexes[component_id]] = float(value)
    fractions[authority.base_index] = 1.0 - float(np.sum(colorant_values))
    return fractions


def _mixed_coefficients(
    authority: _Authority,
    fractions: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    if (
        fractions.shape != (len(authority.component_pairs),)
        or not np.all(np.isfinite(fractions))
        or np.any(fractions < -_FRACTION_TOLERANCE)
        or not math.isclose(float(np.sum(fractions)), 1.0, rel_tol=0.0, abs_tol=1e-9)
        or fractions[authority.base_index] <= 0.0
    ):
        _fail("PREDICTION", "fractions", "must be finite, nonnegative, normalized, and contain positive base")
    normalized = np.maximum(fractions, 0.0)
    normalized /= float(np.sum(normalized))
    absorption = np.sum(normalized[:, np.newaxis] * authority.absorption, axis=0)
    scattering = np.sum(normalized[:, np.newaxis] * authority.scattering, axis=0)
    if np.any(absorption < 0.0) or np.any(scattering <= 0.0):
        _fail("PREDICTION", "mixed_coefficients", "must contain nonnegative K and positive S")
    return absorption, scattering


def _objective_mse_batch(
    authority: _Authority,
    request: _Request,
    fraction_rows: np.ndarray,
) -> np.ndarray:
    rows = np.asarray(fraction_rows, dtype=float)
    component_count = len(authority.component_pairs)
    if (
        rows.ndim != 2
        or rows.shape[1] != component_count
        or len(rows) == 0
        or not np.all(np.isfinite(rows))
        or np.any(rows < -_FRACTION_TOLERANCE)
        or np.any(np.abs(np.sum(rows, axis=1) - 1.0) > 1e-9)
        or np.any(rows[:, authority.base_index] <= 0.0)
    ):
        _fail("PREDICTION", "fraction_rows", "must be finite, nonnegative, normalized, and contain positive base")
    normalized = np.maximum(rows, 0.0)
    normalized /= np.sum(normalized, axis=1, keepdims=True)
    absorption = np.sum(
        normalized[:, :, np.newaxis] * authority.absorption[np.newaxis, :, :],
        axis=1,
    )
    scattering = np.sum(
        normalized[:, :, np.newaxis] * authority.scattering[np.newaxis, :, :],
        axis=1,
    )
    if np.any(absorption < 0.0) or np.any(scattering <= 0.0):
        _fail("PREDICTION", "mixed_coefficients", "must contain nonnegative K and positive S")

    total_weight = math.fsum(cell.weight for cell in request.cells)
    weighted_squared = np.zeros(len(rows), dtype=float)
    for cell in request.cells:
        predicted = finite_film_reflectance(
            absorption,
            scattering,
            cell.dft_um / 1000.0,
            authority.backings[cell.backing][np.newaxis, :],
        )
        if predicted.shape != absorption.shape or not np.all(np.isfinite(predicted)):
            _fail("PREDICTION", cell.cell_id, "produced invalid reflectance")
        error = predicted - cell.reflectance[np.newaxis, :]
        weighted_squared += cell.weight * np.mean(np.square(error), axis=1)
    return weighted_squared / total_weight


def _objective_mse(
    authority: _Authority,
    request: _Request,
    fractions: np.ndarray,
) -> float:
    row = np.asarray(fractions, dtype=float)[np.newaxis, :]
    return float(_objective_mse_batch(authority, request, row)[0])


def _continuous_objective_mse(
    authority: _Authority,
    request: _Request,
    fractions: np.ndarray,
) -> float:
    absorption, scattering = _mixed_coefficients(authority, fractions)
    total_weight = math.fsum(cell.weight for cell in request.cells)
    weighted_squared = 0.0
    for cell in request.cells:
        predicted = finite_film_reflectance(
            absorption,
            scattering,
            cell.dft_um / 1000.0,
            authority.backings[cell.backing],
        )
        if predicted.shape != cell.reflectance.shape or not np.all(np.isfinite(predicted)):
            _fail("PREDICTION", cell.cell_id, "produced invalid reflectance")
        error = predicted - cell.reflectance
        weighted_squared += cell.weight * float(np.mean(np.square(error)))
    return weighted_squared / total_weight


def _evaluate_fractions(
    authority: _Authority,
    request: _Request,
    fractions: np.ndarray,
) -> dict[str, Any]:
    absorption, scattering = _mixed_coefficients(authority, fractions)

    total_weight = math.fsum(cell.weight for cell in request.cells)
    weighted_absolute = 0.0
    maximum_absolute = 0.0
    cells: list[dict[str, Any]] = []
    for cell in request.cells:
        predicted = finite_film_reflectance(
            absorption,
            scattering,
            cell.dft_um / 1000.0,
            authority.backings[cell.backing],
        )
        if predicted.shape != cell.reflectance.shape or not np.all(np.isfinite(predicted)):
            _fail("PREDICTION", cell.cell_id, "produced invalid reflectance")
        error = predicted - cell.reflectance
        mse = float(np.mean(np.square(error)))
        mae = float(np.mean(np.abs(error)))
        max_abs = float(np.max(np.abs(error)))
        weighted_absolute += cell.weight * mae
        maximum_absolute = max(maximum_absolute, max_abs)
        cells.append(
            {
                "cell_id": cell.cell_id,
                "backing": cell.backing,
                "dft_um": cell.dft_um,
                "weight": cell.weight,
                "target_reflectance": cell.reflectance.tolist(),
                "predicted_reflectance": predicted.tolist(),
                "spectral_rmse": math.sqrt(mse),
                "spectral_mae": mae,
                "spectral_max_abs": max_abs,
            }
        )
    objective = _objective_mse(authority, request, fractions)
    return {
        "objective_mse": objective,
        "spectral_rmse": math.sqrt(objective),
        "spectral_mae": weighted_absolute / total_weight,
        "spectral_max_abs": maximum_absolute,
        "cells": cells,
    }


def _support_starts(upper: np.ndarray, total_maximum: float) -> list[np.ndarray]:
    if len(upper) == 0:
        return [np.zeros(0, dtype=float)]
    starts: list[np.ndarray] = []
    for scale in (0.0, 0.25, 0.60, 0.95):
        target = total_maximum * scale
        if target == 0.0:
            start = np.zeros(len(upper), dtype=float)
        else:
            start = np.minimum(upper, target / len(upper))
            remaining = target - float(np.sum(start))
            for index in range(len(start)):
                if remaining <= 0.0:
                    break
                addition = min(float(upper[index] - start[index]), remaining)
                start[index] += addition
                remaining -= addition
        key = tuple(float(value) for value in start)
        if not any(tuple(float(value) for value in existing) == key for existing in starts):
            starts.append(start)
    return starts


def _continuous_candidates(authority: _Authority, request: _Request) -> list[dict[str, Any]]:
    supports: list[tuple[str, ...]] = [()]
    for size in range(1, request.maximum_colorants + 1):
        supports.extend(itertools.combinations(request.allowed_colorants, size))

    candidates_by_fraction: dict[tuple[float, ...], dict[str, Any]] = {}
    optimizer_evaluations = 0
    for support in supports:
        if not support:
            fractions = _fractions_from_support(authority, (), ())
            metrics = _evaluate_fractions(authority, request, fractions)
            candidate = {
                "declared_support": [],
                "active_support": [],
                "fractions": fractions,
                "metrics": metrics,
                "optimizer": {
                    "required": False,
                    "success": True,
                    "status": 0,
                    "message": "base-only direct evaluation",
                    "iterations": 0,
                    "function_evaluations": 1,
                    "start_index": 0,
                },
            }
        else:
            upper = np.asarray([request.per_colorant_maximum[item] for item in support], dtype=float)
            bounds = Bounds(np.zeros(len(support), dtype=float), upper)
            constraint = LinearConstraint(
                np.ones((1, len(support)), dtype=float),
                np.asarray([-np.inf], dtype=float),
                np.asarray([request.maximum_total_colorant_fraction], dtype=float),
            )

            def objective(values: np.ndarray) -> float:
                nonlocal optimizer_evaluations
                optimizer_evaluations += 1
                if optimizer_evaluations > _MAX_TOTAL_OPTIMIZER_EVALUATIONS:
                    _fail(
                        "OPTIMIZATION_BUDGET",
                        "continuous_search",
                        f"exceeded {_MAX_TOTAL_OPTIMIZER_EVALUATIONS} objective evaluations",
                    )
                fractions = _fractions_from_support(authority, support, values)
                if fractions[authority.base_index] <= 0.0:
                    return float("inf")
                return _continuous_objective_mse(authority, request, fractions)

            results: list[tuple[float, int, Any, np.ndarray, dict[str, Any]]] = []
            for start_index, start in enumerate(_support_starts(upper, request.maximum_total_colorant_fraction)):
                result = minimize(
                    objective,
                    start,
                    method="SLSQP",
                    bounds=bounds,
                    constraints=(constraint,),
                    options={"maxiter": 300, "ftol": 1e-14, "disp": False},
                )
                values = np.asarray(result.x, dtype=float)
                if (
                    not bool(result.success)
                    or values.shape != upper.shape
                    or not np.all(np.isfinite(values))
                    or np.any(values < -1e-9)
                    or np.any(values - upper > 1e-9)
                    or float(np.sum(values)) > request.maximum_total_colorant_fraction + 1e-9
                ):
                    continue
                values = np.clip(values, 0.0, upper)
                if float(np.sum(values)) > request.maximum_total_colorant_fraction + _FRACTION_TOLERANCE:
                    continue
                fractions = _fractions_from_support(authority, support, values)
                metrics = _evaluate_fractions(authority, request, fractions)
                results.append((float(metrics["objective_mse"]), start_index, result, fractions, metrics))
            if not results:
                _fail(
                    "OPTIMIZATION_INCOMPLETE",
                    "continuous_search.support[" + ",".join(support) + "]",
                    "all deterministic SLSQP starts failed or returned an invalid constrained result",
                )
            objective_value, start_index, result, fractions, metrics = min(
                results,
                key=lambda item: (item[0], item[1]),
            )
            active_support = [
                component_id
                for component_id in support
                if fractions[authority.colorant_indexes[component_id]] > _FRACTION_TOLERANCE
            ]
            candidate = {
                "declared_support": list(support),
                "active_support": active_support,
                "fractions": fractions,
                "metrics": metrics,
                "optimizer": {
                    "required": True,
                    "success": bool(result.success),
                    "status": int(result.status),
                    "message": str(result.message),
                    "iterations": int(result.nit),
                    "function_evaluations": int(result.nfev),
                    "start_index": start_index,
                    "objective_mse": objective_value,
                },
            }
        key = tuple(round(float(value), 13) for value in candidate["fractions"])
        prior = candidates_by_fraction.get(key)
        candidate_key = (
            float(candidate["metrics"]["objective_mse"]),
            len(candidate["active_support"]),
            tuple(candidate["declared_support"]),
        )
        prior_key = (
            float(prior["metrics"]["objective_mse"]),
            len(prior["active_support"]),
            tuple(prior["declared_support"]),
        ) if prior is not None else None
        if prior_key is None or candidate_key < prior_key:
            candidates_by_fraction[key] = candidate

    candidates = sorted(
        candidates_by_fraction.values(),
        key=lambda item: (
            float(item["metrics"]["objective_mse"]),
            len(item["active_support"]),
            tuple(float(value) for value in item["fractions"]),
        ),
    )
    if not candidates:
        _fail("OPTIMIZATION", "continuous_search", "did not produce a finite feasible candidate")
    return candidates


def _wet_masses_from_fractions(
    authority: _Authority,
    request: _Request,
    fractions: np.ndarray,
) -> tuple[Decimal, tuple[Decimal, ...]]:
    decimal_fractions = tuple(Decimal(repr(float(value))) for value in fractions)
    denominator = sum(
        (
            fraction * factor
            for fraction, factor in zip(decimal_fractions, authority.wet_g_per_nonvolatile_ml, strict=True)
        ),
        Decimal(0),
    )
    if not denominator.is_finite() or denominator <= 0:
        _fail("CONVERSION", "continuous_fractions", "do not produce a positive wet-mass denominator")
    total_nonvolatile_volume = request.target_wet_mass_g / denominator
    masses = tuple(
        fraction * total_nonvolatile_volume * factor
        for fraction, factor in zip(decimal_fractions, authority.wet_g_per_nonvolatile_ml, strict=True)
    )
    if any(not value.is_finite() or value < 0 for value in masses):
        _fail("CONVERSION", "continuous_wet_masses", "contain an invalid mass")
    return total_nonvolatile_volume, masses


def _colorant_tick_options(continuous_mass: Decimal, profile: _DispenseComponent) -> tuple[int, ...]:
    center = int((continuous_mass / profile.increment_g).to_integral_value(rounding=ROUND_HALF_UP))
    options = {0, profile.minimum_ticks}
    for offset in range(-_TICK_RADIUS, _TICK_RADIUS + 1):
        ticks = center + offset
        if ticks == 0 or profile.minimum_ticks <= ticks <= profile.maximum_ticks:
            options.add(ticks)
    return tuple(sorted(ticks for ticks in options if ticks == 0 or profile.minimum_ticks <= ticks <= profile.maximum_ticks))


def _base_tick_options(remaining_mass: Decimal, profile: _DispenseComponent) -> tuple[int, ...]:
    raw = remaining_mass / profile.increment_g
    floor = int(raw.to_integral_value(rounding=ROUND_FLOOR))
    ceiling = int(raw.to_integral_value(rounding=ROUND_CEILING))
    options: set[int] = set()
    for center in (floor, ceiling):
        for offset in range(-_BASE_TICK_RADIUS, _BASE_TICK_RADIUS + 1):
            ticks = center + offset
            if profile.minimum_ticks <= ticks <= profile.maximum_ticks:
                options.add(ticks)
    return tuple(sorted(options))


def _quantized_fraction_vector(
    authority: _Authority,
    masses: Sequence[Decimal],
) -> np.ndarray:
    volumes = [
        mass / factor
        for mass, factor in zip(masses, authority.wet_g_per_nonvolatile_ml, strict=True)
    ]
    total = sum(volumes, Decimal(0))
    if not total.is_finite() or total <= 0:
        _fail("QUANTIZATION", "wet_masses", "do not produce positive nonvolatile volume")
    fractions = np.asarray([float(value / total) for value in volumes], dtype=float)
    if not np.all(np.isfinite(fractions)):
        _fail("QUANTIZATION", "actual_fractions", "contain non-finite values")
    fractions /= float(np.sum(fractions))
    return fractions


def _policy_allows_quantized(
    authority: _Authority,
    request: _Request,
    fractions: np.ndarray,
) -> bool:
    colorant_total = 1.0 - float(fractions[authority.base_index])
    if colorant_total > request.maximum_total_colorant_fraction + 1e-12:
        return False
    for component_id in request.allowed_colorants:
        if fractions[authority.colorant_indexes[component_id]] > request.per_colorant_maximum[component_id] + 1e-12:
            return False
    allowed_indexes = {authority.colorant_indexes[item] for item in request.allowed_colorants}
    return all(
        index == authority.base_index or index in allowed_indexes or fraction <= _FRACTION_TOLERANCE
        for index, fraction in enumerate(fractions)
    )


def _quantized_candidates(
    authority: _Authority,
    request: _Request,
    continuous_candidates: Sequence[Mapping[str, Any]],
) -> _QuantizedSearch:
    base_profile = request.dispense_components[authority.base_index]
    retained: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    lattice_evaluations = 0

    def rank_key(candidate: Mapping[str, Any]) -> tuple[object, ...]:
        return (
            float(_mapping(candidate["metrics"], "candidate.metrics")["objective_mse"]),
            abs(candidate["mass_error"]),
            len(candidate["active_colorants"]),
            candidate["total_colorant_mass"],
            candidate["ticks"],
        )

    def flush_pending() -> None:
        if not pending:
            return
        fraction_rows = np.vstack([np.asarray(item["fractions"], dtype=float) for item in pending])
        objectives = _objective_mse_batch(authority, request, fraction_rows)
        for candidate, objective in zip(pending, objectives, strict=True):
            candidate["metrics"] = {"objective_mse": float(objective)}
            if any(existing["ticks"] == candidate["ticks"] for existing in retained):
                continue
            if len(retained) < _RETAINED_QUANTIZED_CANDIDATES or rank_key(candidate) < rank_key(retained[-1]):
                retained.append(candidate)
                retained.sort(key=rank_key)
                if len(retained) > _RETAINED_QUANTIZED_CANDIDATES:
                    retained.pop()
        pending.clear()

    for continuous_index, continuous in enumerate(continuous_candidates):
        fractions = np.asarray(continuous["fractions"], dtype=float)
        total_nonvolatile_volume, continuous_masses = _wet_masses_from_fractions(authority, request, fractions)
        declared_support = tuple(str(item) for item in continuous["declared_support"])
        support_indexes = tuple(authority.colorant_indexes[item] for item in declared_support)
        option_sets = [
            _colorant_tick_options(continuous_masses[index], request.dispense_components[index])
            for index in support_indexes
        ]
        combinations: Iterable[tuple[int, ...]] = itertools.product(*option_sets) if option_sets else [()]
        for colorant_ticks in combinations:
            ticks = [0 for _item in authority.component_pairs]
            colorant_mass = Decimal(0)
            for index, count in zip(support_indexes, colorant_ticks, strict=True):
                ticks[index] = count
                colorant_mass += request.dispense_components[index].increment_g * count
            remaining = request.target_wet_mass_g - colorant_mass
            for base_ticks in _base_tick_options(remaining, base_profile):
                ticks[authority.base_index] = base_ticks
                tick_key = tuple(ticks)
                masses = tuple(
                    profile.increment_g * count
                    for profile, count in zip(request.dispense_components, tick_key, strict=True)
                )
                total_mass = sum(masses, Decimal(0))
                mass_error = total_mass - request.target_wet_mass_g
                if abs(mass_error) > request.maximum_total_mass_error_g:
                    continue
                if any(
                    count != 0 and not profile.minimum_ticks <= count <= profile.maximum_ticks
                    for profile, count in zip(request.dispense_components, tick_key, strict=True)
                ):
                    continue
                actual_fractions = _quantized_fraction_vector(authority, masses)
                if actual_fractions[authority.base_index] <= 0.0 or not _policy_allows_quantized(authority, request, actual_fractions):
                    continue
                active_colorants = [
                    component_id
                    for component_id in request.allowed_colorants
                    if tick_key[authority.colorant_indexes[component_id]] > 0
                ]
                if len(active_colorants) > request.maximum_colorants:
                    continue
                total_colorant_mass = sum(
                    (
                        masses[index]
                        for index in range(len(masses))
                        if index != authority.base_index
                    ),
                    Decimal(0),
                )
                pending.append({
                    "continuous_index": continuous_index,
                    "continuous_total_nonvolatile_volume_ml_decimal": _decimal_text(total_nonvolatile_volume),
                    "continuous_masses": continuous_masses,
                    "ticks": tick_key,
                    "masses": masses,
                    "total_mass": total_mass,
                    "mass_error": mass_error,
                    "fractions": actual_fractions,
                    "active_colorants": active_colorants,
                    "total_colorant_mass": total_colorant_mass,
                })
                lattice_evaluations += 1
                if len(pending) >= _LATTICE_BATCH_SIZE:
                    flush_pending()
    flush_pending()
    if not retained:
        _fail("INFEASIBLE_LATTICE", "quantized_search", "found no candidate satisfying the declared dispense lattice")
    for candidate in retained:
        ranked_objective = float(candidate["metrics"]["objective_mse"])
        metrics = _evaluate_fractions(authority, request, candidate["fractions"])
        if not math.isclose(
            float(metrics["objective_mse"]),
            ranked_objective,
            rel_tol=0.0,
            abs_tol=_OBJECTIVE_TOLERANCE,
        ):
            _fail("PREDICTION", "quantized_search", "batched and reported objectives differ")
        candidate["metrics"] = metrics
    retained.sort(key=rank_key)
    return _QuantizedSearch(candidates=tuple(retained), lattice_evaluations=lattice_evaluations)


def _fraction_records(authority: _Authority, fractions: Sequence[float]) -> list[dict[str, object]]:
    return [
        {
            "component_id": component_id,
            "physical_lot_id": lot_id,
            "nonvolatile_volume_fraction": float(fraction),
        }
        for (component_id, lot_id), fraction in zip(authority.component_pairs, fractions, strict=True)
    ]


def _continuous_mass_records(
    authority: _Authority,
    masses: Sequence[Decimal],
) -> list[dict[str, object]]:
    return [
        {
            "component_id": component_id,
            "physical_lot_id": lot_id,
            "wet_mass_g_decimal": _decimal_text(mass),
            "wet_mass_g": float(mass),
        }
        for (component_id, lot_id), mass in zip(authority.component_pairs, masses, strict=True)
    ]


def _quantized_mass_records(
    authority: _Authority,
    request: _Request,
    masses: Sequence[Decimal],
    ticks: Sequence[int],
) -> list[dict[str, object]]:
    return [
        {
            "component_id": component_id,
            "physical_lot_id": lot_id,
            "ticks": int(count),
            "increment_g_decimal": _decimal_text(profile.increment_g),
            "wet_mass_g_decimal": _decimal_text(mass),
            "wet_mass_g": float(mass),
        }
        for (component_id, lot_id), profile, mass, count in zip(
            authority.component_pairs,
            request.dispense_components,
            masses,
            ticks,
            strict=True,
        )
    ]


def _selected_payload(
    authority: _Authority,
    request: _Request,
    continuous_candidates: Sequence[Mapping[str, Any]],
    quantized: Mapping[str, Any],
) -> dict[str, Any]:
    continuous = continuous_candidates[int(quantized["continuous_index"])]
    continuous_fractions = np.asarray(continuous["fractions"], dtype=float)
    _continuous_volume, continuous_masses = _wet_masses_from_fractions(authority, request, continuous_fractions)
    continuous_metrics = copy.deepcopy(dict(continuous["metrics"]))
    quantized_metrics = copy.deepcopy(dict(quantized["metrics"]))
    mse_delta = float(quantized_metrics["objective_mse"]) - float(continuous_metrics["objective_mse"])
    if mse_delta > _QUANTIZATION_MSE_COMPARISON_TOLERANCE:
        quantization_effect = "degraded"
    elif mse_delta < -_QUANTIZATION_MSE_COMPARISON_TOLERANCE:
        quantization_effect = "improved"
    else:
        quantization_effect = "equivalent_within_numerical_tolerance"
    return {
        "continuous": {
            "declared_support": list(continuous["declared_support"]),
            "active_support": list(continuous["active_support"]),
            "components": _fraction_records(authority, continuous_fractions),
            "wet_masses": _continuous_mass_records(authority, continuous_masses),
            "total_wet_mass_g_decimal": _decimal_text(sum(continuous_masses, Decimal(0))),
            "metrics": continuous_metrics,
            "optimizer": copy.deepcopy(dict(continuous["optimizer"])),
        },
        "quantized": {
            "active_colorants": list(quantized["active_colorants"]),
            "components": _fraction_records(authority, quantized["fractions"]),
            "wet_masses": _quantized_mass_records(authority, request, quantized["masses"], quantized["ticks"]),
            "total_wet_mass_g_decimal": _decimal_text(quantized["total_mass"]),
            "total_wet_mass_g": float(quantized["total_mass"]),
            "target_wet_mass_g_decimal": _decimal_text(request.target_wet_mass_g),
            "total_mass_error_g_decimal": _decimal_text(quantized["mass_error"]),
            "absolute_total_mass_error_g": float(abs(quantized["mass_error"])),
            "total_nonvolatile_volume_ml_decimal": _decimal_text(
                sum(
                    (
                        mass / factor
                        for mass, factor in zip(
                            quantized["masses"],
                            authority.wet_g_per_nonvolatile_ml,
                            strict=True,
                        )
                    ),
                    Decimal(0),
                )
            ),
            "metrics": quantized_metrics,
        },
        "quantization_degradation": {
            "signed_delta_definition": "quantized_minus_continuous",
            "effect": quantization_effect,
            "objective_mse_comparison_tolerance": _QUANTIZATION_MSE_COMPARISON_TOLERANCE,
            "spectral_mse_delta": mse_delta,
            "spectral_rmse_delta": float(quantized_metrics["spectral_rmse"]) - float(continuous_metrics["spectral_rmse"]),
            "spectral_mae_delta": float(quantized_metrics["spectral_mae"]) - float(continuous_metrics["spectral_mae"]),
            "spectral_max_abs_delta": float(quantized_metrics["spectral_max_abs"]) - float(continuous_metrics["spectral_max_abs"]),
        },
    }


def _alternative_payload(
    authority: _Authority,
    request: _Request,
    quantized: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "active_colorants": list(quantized["active_colorants"]),
        "components": _fraction_records(authority, quantized["fractions"]),
        "wet_masses": _quantized_mass_records(authority, request, quantized["masses"], quantized["ticks"]),
        "total_wet_mass_g_decimal": _decimal_text(quantized["total_mass"]),
        "total_mass_error_g_decimal": _decimal_text(quantized["mass_error"]),
        "metrics": {
            "objective_mse": float(quantized["metrics"]["objective_mse"]),
            "spectral_rmse": float(quantized["metrics"]["spectral_rmse"]),
            "spectral_mae": float(quantized["metrics"]["spectral_mae"]),
            "spectral_max_abs": float(quantized["metrics"]["spectral_max_abs"]),
        },
    }


def _build_objects(authority: _Authority, request: _Request) -> tuple[dict[str, Any], dict[str, Any]]:
    continuous = _continuous_candidates(authority, request)
    quantized_search = _quantized_candidates(authority, request, continuous)
    quantized = quantized_search.candidates
    selected = _selected_payload(authority, request, continuous, quantized[0])
    algorithm = _algorithm_spec()
    algorithm_sha = sha256_bytes(canonical_json_bytes(algorithm))
    candidate = {
        "schema_version": CANDIDATE_SCHEMA,
        "dataset_status": "open_selection_only",
        "status": _CANDIDATE_STATUS,
        "state": _STATE,
        "evidence_class": request.evidence_class,
        "production_pass": False,
        **_permissions(),
        "runtime_compatible": False,
        "physical_accuracy_verified": False,
        "independent_holdout_passed": False,
        "production_executable": False,
        "laboratory_trial_only": True,
        "request": {
            "request_id": request.request_id,
            "target_id": request.target_id,
            "relative_path": request.relative_path,
            "sha256": request.sha256,
            "target_evidence": copy.deepcopy(dict(request.target_evidence)),
            "dispense_profile_id": request.dispense_profile_id,
            "dispense_evidence": copy.deepcopy(dict(request.dispense_evidence)),
        },
        "authority_bindings": copy.deepcopy(dict(authority.hashes)),
        "material_conversion_bindings": copy.deepcopy(list(authority.material_bindings)),
        "observed_componentwise_search_envelope": {
            "train_dft_min_um": authority.train_dft_range_um[0],
            "train_dft_max_um": authority.train_dft_range_um[1],
            "open_maximum_total_colorant_nonvolatile_volume_fraction": authority.open_maximum_total_colorant_fraction,
            "open_per_colorant_maximum_nonvolatile_volume_fraction": copy.deepcopy(
                dict(authority.open_per_colorant_maximum)
            ),
            "dft_extrapolation_permitted": False,
            "componentwise_upper_bound_extrapolation_permitted": False,
            "convex_hull_membership_verified": False,
            "unseen_component_combinations_permitted_for_laboratory_trial": True,
        },
        "algorithm_spec": algorithm,
        "algorithm_spec_payload_sha256": algorithm_sha,
        "search_diagnostics": {
            "supports_enumerated": sum(
                math.comb(len(request.allowed_colorants), size)
                for size in range(0, request.maximum_colorants + 1)
            ),
            "continuous_candidates": len(continuous),
            "quantized_lattice_evaluations": quantized_search.lattice_evaluations,
            "retained_ranked_quantized_candidates": len(quantized),
            "quantization_search_exhaustive_within_neighborhood": True,
            "global_lattice_optimum_proven": False,
        },
        "selected": selected,
        "alternatives": [
            _alternative_payload(authority, request, item)
            for item in quantized[1 : 1 + _MAX_ALTERNATIVES]
        ],
        "uncertainty": {
            "status": "insufficient_data",
            "confidence_interval_available": False,
            "deterministic_quantization_diagnostic_available": True,
            "reasons": [
                "no_verified_repeatability_covariance",
                "no_verified_dft_uncertainty_distribution",
                "no_verified_conversion_property_uncertainty",
                "no_independent_measured_holdout",
                "measured_composition_convex_hull_not_enforced",
            ],
        },
    }
    candidate_sha = sha256_bytes(canonical_json_bytes(candidate) + b"\n")
    receipt_payload = {
        "schema_version": RECEIPT_SCHEMA,
        "dataset_status": "open_selection_only",
        "status": "laboratory_trial_recipe_candidate_exported",
        "state": _STATE,
        "evidence_class": request.evidence_class,
        "production_pass": False,
        **_permissions(),
        "runtime_compatible": False,
        "physical_accuracy_verified": False,
        "independent_holdout_passed": False,
        "production_executable": False,
        "laboratory_trial_only": True,
        "bindings": {
            "recipe_candidate": {
                "path": "recipe-candidate.json",
                "sha256": candidate_sha,
            },
            "request": {
                "relative_path": request.relative_path,
                "sha256": request.sha256,
            },
            "target_evidence": copy.deepcopy(dict(request.target_evidence)),
            "dispense_evidence": copy.deepcopy(dict(request.dispense_evidence)),
            "algorithm_spec_payload_sha256": algorithm_sha,
            **copy.deepcopy(dict(authority.hashes)),
        },
    }
    receipt = {
        **receipt_payload,
        "receipt_payload_sha256": sha256_bytes(canonical_json_bytes(receipt_payload)),
    }
    return candidate, receipt


def _assert_tree(root: Path, *, expected_files: set[str], code: str) -> None:
    try:
        children = list(root.iterdir())
    except OSError as error:
        _fail(code, str(root), str(error))
    actual: set[str] = set()
    for child in children:
        try:
            stat = child.lstat()
        except OSError as error:
            _fail(code, str(child), str(error))
        if _link_or_reparse(stat) or not S_ISREG(stat.st_mode):
            _fail(code, child.name, "must be a non-link regular file")
        actual.add(child.name)
    if actual != expected_files:
        _fail(code, str(root), "does not contain the exact candidate package")


def _prepare_output(output_dir: Path | str) -> tuple[Path, Path, bool]:
    output = Path(output_dir).absolute()
    _safe_directory_chain(output.parent, code="OUTPUT_PATH", path="output_dir", create=True)
    try:
        stat = output.lstat()
    except FileNotFoundError:
        existed_empty = False
    except OSError as error:
        _fail("OUTPUT_PATH", str(output), str(error))
    else:
        existed_empty = True
        if _link_or_reparse(stat) or not S_ISDIR(stat.st_mode):
            _fail("OUTPUT_PATH", str(output), "must be a new or empty non-link directory")
        _safe_directory_chain(output, code="OUTPUT_PATH", path="output_dir", create=False)
        try:
            if any(output.iterdir()):
                _fail("OUTPUT_PATH", str(output), "must be empty")
        except OSError as error:
            _fail("OUTPUT_PATH", str(output), str(error))
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(exist_ok=False)
    except OSError as error:
        _fail("OUTPUT_WRITE", str(staging), str(error))
    _safe_directory_chain(staging, code="OUTPUT_PATH", path="output_dir", create=False)
    return output, staging, existed_empty


def _publish(output: Path, staging: Path, existed_empty: bool) -> None:
    try:
        _safe_directory_chain(output.parent, code="OUTPUT_PATH", path="output_dir", create=False)
        _safe_directory_chain(staging, code="OUTPUT_PATH", path="output_dir", create=False)
        if existed_empty:
            _safe_directory_chain(output, code="OUTPUT_PATH", path="output_dir", create=False)
            if any(output.iterdir()):
                _fail("OUTPUT_PATH", str(output), "must remain empty before publication")
            output.rmdir()
        elif output.exists():
            _fail("OUTPUT_PATH", str(output), "must not appear before publication")
        staging.replace(output)
        _safe_directory_chain(output, code="OUTPUT_PATH", path="output_dir", create=False)
    except OpenSelectionRecipeSolverError:
        raise
    except OSError as error:
        if existed_empty and not output.exists():
            _safe_directory_chain(output, code="OUTPUT_PATH", path="output_dir", create=True)
        _fail("OUTPUT_WRITE", str(output), str(error))


def _load_inputs(
    *,
    acquisition_receipt_path: Path | str,
    admission_receipt_path: Path | str,
    dataset_root: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
    fit_export_root: Path | str,
    request_root: Path | str,
    request_relative_path: str,
) -> tuple[_Authority, _Request]:
    authority = _load_authority(
        acquisition_receipt_path=acquisition_receipt_path,
        admission_receipt_path=admission_receipt_path,
        dataset_root=dataset_root,
        shared_root=shared_root,
        open_root=open_root,
        measurement_root=measurement_root,
        fit_export_root=fit_export_root,
    )
    root = _root(request_root, "request_root")
    value, normalized, request_sha = _read_request_json(root, request_relative_path)
    request = _parse_request(
        value,
        relative_path=normalized,
        request_sha256=request_sha,
        request_root=root,
        authority=authority,
    )
    return authority, request


def solve_open_selection_recipe_candidate(
    *,
    acquisition_receipt_path: Path | str,
    admission_receipt_path: Path | str,
    dataset_root: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
    fit_export_root: Path | str,
    request_root: Path | str,
    request_relative_path: str,
    output_dir: Path | str,
) -> dict[str, object]:
    """Solve and atomically export an inactive laboratory-trial candidate."""

    authority, request = _load_inputs(
        acquisition_receipt_path=acquisition_receipt_path,
        admission_receipt_path=admission_receipt_path,
        dataset_root=dataset_root,
        shared_root=shared_root,
        open_root=open_root,
        measurement_root=measurement_root,
        fit_export_root=fit_export_root,
        request_root=request_root,
        request_relative_path=request_relative_path,
    )
    candidate, receipt = _build_objects(authority, request)
    output, staging, existed_empty = _prepare_output(output_dir)
    try:
        candidate_sha = write_json_with_sha256(staging / "recipe-candidate.json", candidate)
        receipt_sha = write_json_with_sha256(staging / "recipe-candidate-receipt.json", receipt)
        if receipt["bindings"]["recipe_candidate"]["sha256"] != candidate_sha:
            _fail("OUTPUT_WRITE", "recipe-candidate.json", "candidate hash changed during publication")
        _assert_tree(staging, expected_files=_PACKAGE_FILES, code="OUTPUT_TREE")
        _publish(output, staging, existed_empty)
        return {
            "status": "laboratory_trial_recipe_candidate_exported",
            "state": _STATE,
            "dataset_status": "open_selection_only",
            "evidence_class": request.evidence_class,
            "recipe_candidate_sha256": candidate_sha,
            "recipe_candidate_receipt_sha256": receipt_sha,
            "production_pass": False,
            **_permissions(),
            "runtime_compatible": False,
            "physical_accuracy_verified": False,
            "independent_holdout_passed": False,
            "production_executable": False,
            "laboratory_trial_only": True,
        }
    except OpenSelectionRecipeSolverError:
        raise
    except (OSError, CalibrationError) as error:
        _fail("OUTPUT_WRITE", str(staging), str(error))
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _read_candidate_package(candidate_root: Path | str) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    root = _root(candidate_root, "candidate_root")
    _assert_tree(root, expected_files=_PACKAGE_FILES, code="OUTPUT_TREE")
    try:
        candidate_value, candidate_sha = read_verified_json(
            root / "recipe-candidate.json",
            require_sidecar=True,
            trusted_root=root,
        )
        receipt_value, receipt_sha = read_verified_json(
            root / "recipe-candidate-receipt.json",
            require_sidecar=True,
            trusted_root=root,
        )
    except CalibrationError as error:
        _fail("CANDIDATE_BINDING", "candidate_root", str(error))
    candidate = dict(_mapping(candidate_value, "recipe-candidate.json"))
    receipt = dict(_mapping(receipt_value, "recipe-candidate-receipt.json"))
    binding = _mapping(_mapping(receipt.get("bindings"), "recipe-candidate-receipt.json.bindings").get("recipe_candidate"), "recipe-candidate-receipt.json.bindings.recipe_candidate")
    if binding != {"path": "recipe-candidate.json", "sha256": candidate_sha}:
        _fail("CANDIDATE_BINDING", "recipe-candidate-receipt.json.bindings.recipe_candidate", "does not bind the candidate")
    return candidate, candidate_sha, receipt, receipt_sha


def verify_open_selection_recipe_candidate(
    *,
    acquisition_receipt_path: Path | str,
    admission_receipt_path: Path | str,
    dataset_root: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
    fit_export_root: Path | str,
    request_root: Path | str,
    request_relative_path: str,
    candidate_root: Path | str,
) -> dict[str, object]:
    """Reconstruct and verify an existing inactive recipe candidate."""

    actual_candidate, candidate_sha, actual_receipt, receipt_sha = _read_candidate_package(candidate_root)
    authority, request = _load_inputs(
        acquisition_receipt_path=acquisition_receipt_path,
        admission_receipt_path=admission_receipt_path,
        dataset_root=dataset_root,
        shared_root=shared_root,
        open_root=open_root,
        measurement_root=measurement_root,
        fit_export_root=fit_export_root,
        request_root=request_root,
        request_relative_path=request_relative_path,
    )
    expected_candidate, expected_receipt = _build_objects(authority, request)
    if canonical_json_bytes(actual_candidate) != canonical_json_bytes(expected_candidate):
        _fail("RECONSTRUCTION", "recipe-candidate.json", "does not match deterministic reconstruction")
    if canonical_json_bytes(actual_receipt) != canonical_json_bytes(expected_receipt):
        _fail("RECONSTRUCTION", "recipe-candidate-receipt.json", "does not match deterministic reconstruction")
    return {
        "status": "laboratory_trial_recipe_candidate_verified",
        "state": _STATE,
        "dataset_status": "open_selection_only",
        "evidence_class": request.evidence_class,
        "recipe_candidate_sha256": candidate_sha,
        "recipe_candidate_receipt_sha256": receipt_sha,
        "production_pass": False,
        **_permissions(),
        "runtime_compatible": False,
        "physical_accuracy_verified": False,
        "independent_holdout_passed": False,
        "production_executable": False,
        "laboratory_trial_only": True,
    }
