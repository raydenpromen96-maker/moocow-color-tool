"""Receipt-gated admission of open physical measurement records.

This module is intentionally isolated from fitting, evaluation, export, search,
and runtime code.  It accepts only reverified open acquisition provenance and
whole-file evidence bindings, then publishes a non-promotable dataset.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath, PureWindowsPath
from stat import S_ISDIR, S_ISLNK, S_ISREG
from typing import Any, Mapping, Sequence

from .acquisition_preflight import (
    COMPONENT_IDS,
    COMPONENT_ORDER,
    PERMISSIONS,
    load_verified_open_acquisition_context,
)
from .errors import CalibrationError
from .hashing import (
    canonical_json_bytes,
    read_regular_file_snapshot,
    read_verified_json,
    sha256_bytes,
    write_json_with_sha256,
)


INPUT_SCHEMA = "moocow-open-measurement-admission-input-v1"
DATASET_SCHEMA = "moocow-open-selection-dataset-v1"
SOURCE_SCHEMA = "moocow-open-measurements-source-v1"
RECEIPT_SCHEMA = "moocow-open-measurement-admission-receipt-v1"
_BACKINGS = ("black", "white")
_POSITIONS = ("POS01", "POS02", "POS03")
_PLACEHOLDERS = ("required", "template", "placeholder", "synthetic", "inferred", "not_yet")
_SCOPE_MARKERS = ("holdout", "sealed", "custody", "release")
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_OUTPUT_DIRECTORIES = {"sources"}
_OUTPUT_FILES = {
    "manifest.json",
    "manifest.json.sha256",
    "sources/open-measurements.json",
    "sources/open-measurements.json.sha256",
    "admission-receipt.json",
    "admission-receipt.json.sha256",
}


class OpenMeasurementAdmissionError(CalibrationError):
    """Stable, non-secret-bearing v1 admission failure."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"[{code}] {path}: {message}")


@dataclass(frozen=True)
class ValidatedOpenSelectionDataset:
    root: Path
    manifest: Mapping[str, Any]
    source: Mapping[str, Any]
    manifest_sha256: str
    open_measurements_sha256: str


def _fail(code: str, path: str, message: str) -> None:
    raise OpenMeasurementAdmissionError(code, path, message)


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("TYPE", path, "must be an object")
    return value


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("TYPE", path, "must be an array")
    return value


def _exact(value: Mapping[str, Any], path: str, fields: Sequence[str]) -> None:
    expected = set(fields)
    actual = set(value)
    if actual != expected:
        _fail("FIELDS", path, f"must contain exactly {sorted(expected)}; missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}")


def _text(value: object, path: str, *, reject_placeholder: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("TEXT", path, "must be a non-empty string")
    result = value.strip()
    if reject_placeholder and any(marker in result.casefold() for marker in _PLACEHOLDERS):
        _fail("PLACEHOLDER", path, "must not contain a placeholder marker")
    return result


def _sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        _fail("SHA256", path, "must be a lowercase SHA-256 digest")
    return value


def _number(value: object, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("FINITE_NUMBER", path, "must be finite numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0.0):
        _fail("FINITE_NUMBER", path, "must be finite and positive" if positive else "must be finite")
    return number


def _timestamp(value: object, path: str) -> str:
    result = _text(value, path)
    try:
        parsed = dt.datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError:
        _fail("TIMESTAMP", path, "must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("TIMESTAMP", path, "must include a timezone offset")
    return result


def _permissions() -> dict[str, bool]:
    return {name: False for name in PERMISSIONS}


def _assert_permissions(value: Mapping[str, Any], path: str) -> None:
    for permission in PERMISSIONS:
        if value.get(permission) is not False:
            _fail("PERMISSION", f"{path}.{permission}", "must remain false")
    if value.get("production_pass") is not False:
        _fail("PERMISSION", f"{path}.production_pass", "must remain false")


def _reject_scope(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).casefold()
            if any(marker in key_text for marker in _SCOPE_MARKERS):
                _fail("OPEN_SCOPE", f"{path}.{key}", "contains a prohibited scope marker")
            _reject_scope(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_scope(item, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.casefold()
        if any(marker in lowered for marker in _SCOPE_MARKERS) or "fam-ho-" in lowered:
            _fail("OPEN_SCOPE", path, "contains a prohibited scope marker")


def _reject_output_scope(value: object, path: str = "$") -> None:
    """Reject non-open lexical scope while allowing the fixed false bit only."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "holdout_release_permitted" and item is False:
                continue
            key_text = str(key).casefold()
            if any(marker in key_text for marker in _SCOPE_MARKERS):
                _fail("OPEN_SCOPE", f"{path}.{key}", "contains a prohibited scope marker")
            _reject_output_scope(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_output_scope(item, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.casefold()
        if any(marker in lowered for marker in _SCOPE_MARKERS) or "fam-ho-" in lowered:
            _fail("OPEN_SCOPE", path, "contains a prohibited scope marker")


def _portable_relative_path(value: object, path: str) -> str:
    text = _text(value, path, reject_placeholder=False)
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        "\\" in text
        or "\x00" in text
        or any(char in '<>:"|?*' for char in text)
        or windows.is_absolute()
        or windows.drive
        or posix.is_absolute()
        or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in text.split("/"))
    ):
        _fail("EVIDENCE_PATH", path, "must be a portable relative POSIX path without traversal")
    _reject_scope(text, path)
    return text


def _is_link_or_reparse(path_stat: os.stat_result) -> bool:
    return S_ISLNK(path_stat.st_mode) or bool(
        getattr(path_stat, "st_file_attributes", 0)
        & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    )


def _validate_root(value: Path | str, path: str) -> Path:
    _reject_scope(str(value), path)
    candidate = Path(value)
    try:
        stat = candidate.lstat()
    except OSError as error:
        _fail("ROOT", path, str(error))
    if _is_link_or_reparse(stat) or not S_ISDIR(stat.st_mode):
        _fail("ROOT", path, "must be an existing non-link directory")
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        _fail("ROOT", path, str(error))


def _validate_existing_file(value: Path | str, path: str) -> Path:
    _reject_scope(str(value), path)
    candidate = Path(value)
    try:
        raw, _ = read_regular_file_snapshot(candidate)
    except CalibrationError as error:
        _fail("FILE", path, str(error))
    if not raw:
        _fail("FILE", path, "must not be empty")
    return candidate


def _validate_exact_output_tree(root: Path) -> None:
    directories: set[str] = set()
    files: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = list(directory.iterdir())
        except OSError as error:
            _fail("OUTPUT_TREE", str(directory), str(error))
        for child in children:
            try:
                child_stat = child.lstat()
            except OSError as error:
                _fail("OUTPUT_TREE", str(child), str(error))
            relative_path = child.relative_to(root).as_posix()
            if _is_link_or_reparse(child_stat):
                _fail("OUTPUT_TREE", relative_path, "must not be a link or reparse point")
            if S_ISDIR(child_stat.st_mode):
                directories.add(relative_path)
                pending.append(child)
            elif S_ISREG(child_stat.st_mode):
                files.add(relative_path)
            else:
                _fail("OUTPUT_TREE", relative_path, "must be a regular file or directory")
    if directories != _OUTPUT_DIRECTORIES or files != _OUTPUT_FILES:
        _fail(
            "OUTPUT_TREE",
            str(root),
            f"must contain exactly directories={sorted(_OUTPUT_DIRECTORIES)} and files={sorted(_OUTPUT_FILES)}",
        )


def _binding(root: Path, locator: object, path: str) -> dict[str, Any]:
    raw = _mapping(locator, path)
    _exact(raw, path, ("relative_path", "record_locator"))
    relative_path = _portable_relative_path(raw["relative_path"], f"{path}.relative_path")
    record_locator = _mapping(raw["record_locator"], f"{path}.record_locator")
    if dict(record_locator) != {"kind": "whole_file"}:
        _fail("EVIDENCE_PATH", f"{path}.record_locator", "must be exactly a whole-file locator")
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        raw_bytes, digest = read_regular_file_snapshot(candidate, trusted_root=root)
    except CalibrationError as error:
        _fail("EVIDENCE_FILE", relative_path, str(error))
    return {
        "relative_path": relative_path,
        "file_sha256": digest,
        "size_bytes": len(raw_bytes),
        "record_locator": {
            "kind": "whole_file",
            "byte_offset": 0,
            "byte_length": len(raw_bytes),
            "record_sha256": digest,
        },
    }


def _assert_unique_bindings(bindings: Sequence[Mapping[str, Any]], path: str) -> None:
    seen_paths: set[str] = set()
    seen_file_hashes: set[str] = set()
    seen_record_hashes: set[str] = set()
    for index, binding in enumerate(bindings):
        binding_path = _text(
            binding.get("relative_path"),
            f"{path}[{index}].relative_path",
            reject_placeholder=False,
        )
        file_sha = _sha256(
            binding.get("file_sha256"), f"{path}[{index}].file_sha256"
        )
        locator = _mapping(
            binding.get("record_locator"), f"{path}[{index}].record_locator"
        )
        record_sha = _sha256(
            locator.get("record_sha256"),
            f"{path}[{index}].record_locator.record_sha256",
        )
        if binding_path in seen_paths:
            _fail("EVIDENCE_REUSE", f"{path}[{index}].relative_path", "duplicates another evidence path")
        if file_sha in seen_file_hashes:
            _fail("EVIDENCE_REUSE", f"{path}[{index}].file_sha256", "duplicates another evidence file hash")
        if record_sha in seen_record_hashes:
            _fail("EVIDENCE_REUSE", f"{path}[{index}].record_locator.record_sha256", "duplicates another evidence record hash")
        seen_paths.add(binding_path)
        seen_file_hashes.add(file_sha)
        seen_record_hashes.add(record_sha)


def _validate_wavelengths(value: object, path: str) -> list[float]:
    raw = _list(value, path)
    if len(raw) < 3:
        _fail("WAVELENGTH", path, "must contain at least three values")
    result = [_number(item, f"{path}[{index}]") for index, item in enumerate(raw)]
    if result[0] < 360.0 or result[-1] > 830.0 or any(right <= left for left, right in zip(result, result[1:])):
        _fail("WAVELENGTH", path, "must be strictly increasing inside 360-830 nm")
    step = result[1] - result[0]
    if not all(math.isclose(right - left, step, rel_tol=0.0, abs_tol=1e-9) for left, right in zip(result, result[1:])):
        _fail("WAVELENGTH", path, "must be uniformly spaced")
    return result


def _reflectance(value: object, wavelengths: Sequence[float], path: str) -> list[float]:
    raw = _list(value, path)
    if len(raw) != len(wavelengths):
        _fail("REFLECTANCE", path, "must match wavelength_nm")
    result = [_number(item, f"{path}[{index}]") for index, item in enumerate(raw)]
    if any(item < 0.0 or item > 1.0 for item in result):
        _fail("REFLECTANCE", path, "must remain in [0, 1]")
    return result


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _validate_locked_conditions(value: object) -> tuple[dict[str, Any], object, object, str]:
    raw = dict(_mapping(value, "locked_conditions"))
    required = {"instrument_id", "instrument_calibration_evidence", "instrument_run_log_evidence"}
    if not required.issubset(raw) or not raw:
        _fail("LOCKED_CONDITIONS", "locked_conditions", "must include instrument identity and both required evidence locators")
    _text(raw["instrument_id"], "locked_conditions.instrument_id")
    for key, item in raw.items():
        if key.endswith("_evidence") and key not in required:
            _fail("LOCKED_CONDITIONS", f"locked_conditions.{key}", "has an unsupported evidence field")
        if key not in required:
            _reject_scope(item, f"locked_conditions.{key}")
    return raw, raw["instrument_calibration_evidence"], raw["instrument_run_log_evidence"], sha256_bytes(canonical_json_bytes(raw))


def _open_context(
    *, receipt_path: Path | str, shared_root: Path | str, open_root: Path | str
) -> dict[str, Any]:
    receipt = _validate_existing_file(receipt_path, "acquisition_receipt_path")
    shared = _validate_root(shared_root, "shared_root")
    opened = _validate_root(open_root, "open_root")
    try:
        context = load_verified_open_acquisition_context(
            receipt_path=receipt, shared_root=shared, open_root=opened
        )
    except CalibrationError as error:
        _fail("PREDECESSOR", "acquisition_preflight", str(error))
    if len(context["materials"]) != 15 or len(context["batches"]) != 17 or len(context["card_skeleton"]) != 36:
        _fail("PREDECESSOR", "acquisition_preflight", "did not yield the fixed open projection")
    return context


def _component_summary(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    materials = _list(context["materials"], "context.materials")
    result: list[dict[str, Any]] = []
    for index, (formula_key, component_id) in enumerate(COMPONENT_ORDER):
        material = _mapping(materials[index], f"context.materials[{index}]")
        if material.get("formula_key") != formula_key or material.get("component_id") != component_id:
            _fail("PREDECESSOR", "context.materials", "does not retain the fixed component order")
        label = _mapping(material.get("label_verification"), "context.material.label_verification")
        property_evidence = _mapping(material.get("property_evidence"), "context.material.property_evidence")
        result.append(
            {
                "component_id": component_id,
                "physical_lot_id": _text(material.get("physical_lot_id"), "context.material.physical_lot_id", reject_placeholder=False),
                "source_binding": {
                    "label_evidence_sha256": _sha256(label.get("file_sha256"), "context.material.label_verification.file_sha256"),
                    "property_evidence_sha256": _sha256(property_evidence.get("file_sha256"), "context.material.property_evidence.file_sha256"),
                },
            }
        )
    return result


def _formula_components(batch: Mapping[str, Any], components: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    vector = _list(batch.get("actual_nv_vector"), "context.batch.actual_nv_vector")
    if len(vector) != len(components):
        _fail("PREDECESSOR", "context.batch.actual_nv_vector", "must retain all 15 component fractions")
    decimals: list[Decimal] = []
    for index, value in enumerate(vector):
        if isinstance(value, bool):
            _fail("PREDECESSOR", f"context.batch.actual_nv_vector[{index}]", "must be a finite decimal fraction")
        try:
            fraction = Decimal(str(value))
        except (InvalidOperation, ValueError):
            _fail("PREDECESSOR", f"context.batch.actual_nv_vector[{index}]", "must be a finite decimal fraction")
        if not fraction.is_finite() or fraction < 0 or fraction > 1:
            _fail("PREDECESSOR", f"context.batch.actual_nv_vector[{index}]", "must be in [0, 1]")
        decimals.append(fraction)
    decimal_sum = sum(decimals, Decimal(0))
    if abs(decimal_sum - Decimal(1)) > Decimal("1e-12"):
        _fail("PREDECESSOR", "context.batch.actual_nv_vector", "must sum to one within 1e-12")
    return [
        {
            "component_id": component["component_id"],
            "physical_lot_id": component["physical_lot_id"],
            "nonvolatile_volume_fraction": float(decimals[index]),
        }
        for index, component in enumerate(components)
    ]


def _normalize_input(
    value: object,
    *,
    context: Mapping[str, Any],
    measurement_root: Path,
    admission_binding: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    _reject_scope(value)
    raw = _mapping(value, "admission_input")
    _exact(raw, "admission_input", ("schema_version", "measurement_session_id", "wavelength_nm", "locked_conditions", "backings", "cards", "readings"))
    if raw.get("schema_version") != INPUT_SCHEMA:
        _fail("SCHEMA", "admission_input.schema_version", "is not supported")
    session_id = _text(raw["measurement_session_id"], "admission_input.measurement_session_id")
    wavelengths = _validate_wavelengths(raw["wavelength_nm"], "admission_input.wavelength_nm")
    locked_conditions, calibration_locator, run_log_locator, conditions_sha = _validate_locked_conditions(raw["locked_conditions"])
    calibration_binding = _binding(measurement_root, calibration_locator, "locked_conditions.instrument_calibration_evidence")
    run_log_binding = _binding(measurement_root, run_log_locator, "locked_conditions.instrument_run_log_evidence")

    bindings: list[dict[str, Any]] = [dict(admission_binding), calibration_binding, run_log_binding]
    backings_raw = _mapping(raw["backings"], "admission_input.backings")
    _exact(backings_raw, "admission_input.backings", _BACKINGS)
    backings: dict[str, dict[str, Any]] = {}
    seen_measurement_ids: set[str] = set()
    bare_bindings: list[dict[str, Any]] = []
    for backing in _BACKINGS:
        item = _mapping(backings_raw[backing], f"backings.{backing}")
        _exact(item, f"backings.{backing}", ("backing_id", "lot_id", "bare_measurements"))
        bare_raw = _list(item["bare_measurements"], f"backings.{backing}.bare_measurements")
        if len(bare_raw) < 3:
            _fail("BARE_COUNT", f"backings.{backing}.bare_measurements", "must contain at least three records")
        bare_records: list[dict[str, Any]] = []
        for index, measurement in enumerate(bare_raw):
            entry = _mapping(measurement, f"backings.{backing}.bare_measurements[{index}]")
            _exact(entry, f"backings.{backing}.bare_measurements[{index}]", ("instrument_measurement_id", "measured_at_local", "reposition_id", "raw_spectrum_evidence", "reflectance"))
            measurement_id = _text(entry["instrument_measurement_id"], f"backings.{backing}.bare_measurements[{index}].instrument_measurement_id")
            if measurement_id in seen_measurement_ids:
                _fail("MEASUREMENT_ID", measurement_id, "must be globally unique")
            seen_measurement_ids.add(measurement_id)
            binding = _binding(measurement_root, entry["raw_spectrum_evidence"], f"backings.{backing}.bare_measurements[{index}].raw_spectrum_evidence")
            bindings.append(binding)
            bare_bindings.append(binding)
            bare_records.append(
                {
                    "instrument_measurement_id": measurement_id,
                    "measured_at_local": _timestamp(entry["measured_at_local"], f"backings.{backing}.bare_measurements[{index}].measured_at_local"),
                    "reposition_id": _text(entry["reposition_id"], f"backings.{backing}.bare_measurements[{index}].reposition_id"),
                    "raw_spectrum_evidence": binding,
                    "reflectance": _reflectance(entry["reflectance"], wavelengths, f"backings.{backing}.bare_measurements[{index}].reflectance"),
                }
            )
        backings[backing] = {
            "backing_id": _text(item["backing_id"], f"backings.{backing}.backing_id"),
            "lot_id": _text(item["lot_id"], f"backings.{backing}.lot_id"),
            "bare_measurements": bare_records,
            "bare_measurement_count": len(bare_records),
            "mean_reflectance": [_mean([record["reflectance"][position] for record in bare_records]) for position in range(len(wavelengths))],
        }

    skeleton = _list(context["card_skeleton"], "context.card_skeleton")
    expected_cards = {item["card_id"]: item for item in skeleton}
    cards_raw = _list(raw["cards"], "admission_input.cards")
    if len(cards_raw) != 36:
        _fail("CARD_COUNT", "admission_input.cards", "must contain exactly 36 records")
    cards_by_id: dict[str, dict[str, Any]] = {}
    dft_bindings: list[dict[str, Any]] = []
    for index, item in enumerate(cards_raw):
        entry = _mapping(item, f"cards[{index}]")
        _exact(entry, f"cards[{index}]", ("card_id", "dft_by_backing"))
        card_id = _text(entry["card_id"], f"cards[{index}].card_id")
        if card_id not in expected_cards or card_id in cards_by_id:
            _fail("CARD_ROSTER", f"cards[{index}].card_id", "must occur exactly once in the reverified roster")
        dft_by_backing = _mapping(entry["dft_by_backing"], f"cards[{index}].dft_by_backing")
        _exact(dft_by_backing, f"cards[{index}].dft_by_backing", _BACKINGS)
        normalized_dft: dict[str, Any] = {}
        for backing in _BACKINGS:
            dft = _mapping(dft_by_backing[backing], f"cards[{index}].dft_by_backing.{backing}")
            _exact(dft, f"cards[{index}].dft_by_backing.{backing}", ("dft_measurement_id", "measured_at_local", "dft_points_um", "dft_evidence"))
            dft_id = _text(dft["dft_measurement_id"], f"cards[{index}].dft_by_backing.{backing}.dft_measurement_id")
            if dft_id in seen_measurement_ids:
                _fail("DFT_ID", dft_id, "must be unique")
            seen_measurement_ids.add(dft_id)
            points = [_number(point, f"cards[{index}].dft_by_backing.{backing}.dft_points_um[{point_index}]", positive=True) for point_index, point in enumerate(_list(dft["dft_points_um"], f"cards[{index}].dft_by_backing.{backing}.dft_points_um"))]
            if not points:
                _fail("DFT_POINTS", f"cards[{index}].dft_by_backing.{backing}.dft_points_um", "must not be empty")
            binding = _binding(measurement_root, dft["dft_evidence"], f"cards[{index}].dft_by_backing.{backing}.dft_evidence")
            bindings.append(binding)
            dft_bindings.append(binding)
            normalized_dft[backing] = {
                "dft_measurement_id": dft_id,
                "measured_at_local": _timestamp(dft["measured_at_local"], f"cards[{index}].dft_by_backing.{backing}.measured_at_local"),
                "dft_points_um": points,
                "dft_um": _mean(points),
                "dft_evidence": binding,
            }
        receipt_card = expected_cards[card_id]
        cards_by_id[card_id] = {**receipt_card, "dft_by_backing": normalized_dft}
    if set(cards_by_id) != set(expected_cards):
        _fail("CARD_ROSTER", "admission_input.cards", "does not match the complete reverified roster")

    for family in {card["formula_family_id"] for card in cards_by_id.values()}:
        family_cards = [card for card in cards_by_id.values() if card["formula_family_id"] == family]
        bands = ("DFT-L", "DFT-H") if family_cards[0]["split"] == "train" else ("DFT-L", "DFT-M", "DFT-H")
        for backing in _BACKINGS:
            values = [next(card for card in family_cards if card["dft_band"] == band)["dft_by_backing"][backing]["dft_um"] for band in bands]
            if any(right <= left for left, right in zip(values, values[1:])):
                _fail("DFT_ORDER", f"{family}.{backing}", "must preserve its receipt-derived DFT-band order")

    readings_raw = _list(raw["readings"], "admission_input.readings")
    if len(readings_raw) != 216:
        _fail("READING_COUNT", "admission_input.readings", "must contain exactly 216 records")
    expected_slots = {(card_id, backing, position) for card_id in expected_cards for backing in _BACKINGS for position in _POSITIONS}
    readings: dict[tuple[str, str, str], dict[str, Any]] = {}
    coated_bindings: list[dict[str, Any]] = []
    for index, item in enumerate(readings_raw):
        entry = _mapping(item, f"readings[{index}]")
        _exact(entry, f"readings[{index}]", ("card_id", "backing", "reposition_id", "instrument_measurement_id", "position_note", "orientation", "measured_at_local", "raw_spectrum_evidence", "surface_status", "model_applicability_status", "backing_id", "backing_lot_id", "reflectance"))
        card_id = _text(entry["card_id"], f"readings[{index}].card_id")
        backing = entry["backing"]
        position = entry["reposition_id"]
        key = (card_id, backing, position)
        if key not in expected_slots or key in readings:
            _fail("READING_ROSTER", f"readings[{index}]", "must occur once in the fixed card/backing/position roster")
        measurement_id = _text(entry["instrument_measurement_id"], f"readings[{index}].instrument_measurement_id")
        if measurement_id in seen_measurement_ids:
            _fail("MEASUREMENT_ID", measurement_id, "must be globally unique")
        seen_measurement_ids.add(measurement_id)
        if entry["surface_status"] != "accepted_uniform_dry_film" or entry["model_applicability_status"] != "accepted_for_km_diagnostic":
            _fail("READING_STATUS", f"readings[{index}]", "must retain the required accepted physical statuses")
        if _text(entry["backing_id"], f"readings[{index}].backing_id") != backings[backing]["backing_id"] or _text(entry["backing_lot_id"], f"readings[{index}].backing_lot_id") != backings[backing]["lot_id"]:
            _fail("BACKING", f"readings[{index}]", "does not match the declared backing identity and lot")
        binding = _binding(measurement_root, entry["raw_spectrum_evidence"], f"readings[{index}].raw_spectrum_evidence")
        bindings.append(binding)
        coated_bindings.append(binding)
        readings[key] = {
            "card_id": card_id,
            "backing": backing,
            "reposition_id": position,
            "instrument_measurement_id": measurement_id,
            "position_note": _text(entry["position_note"], f"readings[{index}].position_note"),
            "orientation": _text(entry["orientation"], f"readings[{index}].orientation"),
            "measured_at_local": _timestamp(entry["measured_at_local"], f"readings[{index}].measured_at_local"),
            "raw_spectrum_evidence": binding,
            "surface_status": entry["surface_status"],
            "model_applicability_status": entry["model_applicability_status"],
            "backing_id": backings[backing]["backing_id"],
            "backing_lot_id": backings[backing]["lot_id"],
            "reflectance": _reflectance(entry["reflectance"], wavelengths, f"readings[{index}].reflectance"),
        }
    if set(readings) != expected_slots:
        _fail("READING_ROSTER", "admission_input.readings", "does not match the complete fixed roster")

    _assert_unique_bindings(bindings, "admission_input.evidence_bindings")

    components = _component_summary(context)
    batches = {batch["formula_family_id"]: batch for batch in _list(context["batches"], "context.batches")}
    cards = [cards_by_id[item["card_id"]] for item in skeleton]
    measurements: list[dict[str, Any]] = []
    for card in cards:
        batch = _mapping(batches[card["formula_family_id"]], "context.batch")
        formula_components = _formula_components(batch, components)
        for backing in _BACKINGS:
            for position in _POSITIONS:
                reading = readings[(card["card_id"], backing, position)]
                measurements.append(
                    {
                        **reading,
                        "formula_family_id": card["formula_family_id"],
                        "formula_id": card["formula_id"],
                        "formula_batch_id": card["formula_batch_id"],
                        "split": card["split"],
                        "dft_band": card["dft_band"],
                        "components": formula_components,
                        "dft_um": card["dft_by_backing"][backing]["dft_um"],
                        "dft_evidence": card["dft_by_backing"][backing]["dft_evidence"],
                        "locked_conditions_sha256": conditions_sha,
                        "target_kind": "measured_spectrum",
                    }
                )
    source = {
        "schema_version": SOURCE_SCHEMA,
        "dataset_status": "open_selection_only",
        "wavelength_nm": wavelengths,
        "locked_conditions": locked_conditions,
        "measurement_session_id": session_id,
        "cards": cards,
        "measurements": measurements,
        "evidence_bindings": {
            "admission_input": dict(admission_binding),
            "instrument_calibration": calibration_binding,
            "instrument_run_log": run_log_binding,
            "bare_spectra": bare_bindings,
            "dft_records": dft_bindings,
            "coated_spectra": coated_bindings,
        },
    }
    counts = {backing: backings[backing]["bare_measurement_count"] for backing in _BACKINGS}
    manifest = {
        "schema_version": DATASET_SCHEMA,
        "dataset_status": "open_selection_only",
        "production_pass": False,
        **_permissions(),
        "concentration_basis": "nonvolatile_volume_fraction",
        "wavelength_nm": wavelengths,
        "locked_conditions": locked_conditions,
        "saunderson": {"mode": "off"},
        "components": components,
        "backings": backings,
        "splits": {
            split: [{key: card[key] for key in ("card_id", "formula_family_id", "formula_id", "formula_batch_id", "split", "dft_band")} for card in cards if card["split"] == split]
            for split in ("train", "validation")
        },
        "counts": {"cards": 36, "coated_readings": 216, "bare_backing_measurements": counts},
        "predecessor": {
            "acquisition_preflight_receipt_sha256": context["acquisition_preflight_receipt_sha256"],
            "open_source_binding": context["open_source_binding"],
        },
        "source_files": [],
    }
    return source, manifest


def _staging(output_dir: Path | str) -> tuple[Path, Path, bool]:
    output = Path(output_dir)
    _reject_scope(str(output), "output_dir")
    existed_empty = output.exists()
    if existed_empty:
        try:
            stat = output.lstat()
        except OSError as error:
            _fail("OUTPUT_DIR", str(output), str(error))
        if _is_link_or_reparse(stat) or not S_ISDIR(stat.st_mode) or any(output.iterdir()):
            _fail("OUTPUT_DIR", str(output), "must be an empty non-link directory")
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        _fail("OUTPUT_WRITE", str(staging), str(error))
    return output, staging, existed_empty


def _publish(output: Path, staging: Path, existed_empty: bool) -> None:
    try:
        if existed_empty:
            output.rmdir()
        staging.replace(output)
    except OSError as error:
        if existed_empty and not output.exists():
            output.mkdir(parents=True, exist_ok=False)
        _fail("OUTPUT_WRITE", str(output), str(error))


def _receipt_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(payload))
    return payload


def _artifact_sha256(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value) + b"\n")


def _build_admission_receipt(
    *,
    context: Mapping[str, Any],
    admission_binding: Mapping[str, Any],
    counts: Mapping[str, Any],
    manifest_sha256: str,
    source_sha256: str,
) -> dict[str, Any]:
    return _receipt_payload(
        {
            "schema_version": RECEIPT_SCHEMA,
            "status": "open_measurements_admitted",
            "state": "OPEN_SELECTION_DATASET_ADMITTED",
            "production_pass": False,
            **_permissions(),
            "bindings": {
                "acquisition_preflight_receipt_sha256": context[
                    "acquisition_preflight_receipt_sha256"
                ],
                "open_source_binding": context["open_source_binding"],
                "admission_input": dict(admission_binding),
                "dataset_manifest": {
                    "path": "manifest.json",
                    "sha256": manifest_sha256,
                },
                "open_measurements": {
                    "path": "sources/open-measurements.json",
                    "sha256": source_sha256,
                },
            },
            "counts": dict(counts),
        }
    )


def _verify_binding(root: Path, binding: Mapping[str, Any], path: str) -> None:
    _exact(binding, path, ("relative_path", "file_sha256", "size_bytes", "record_locator"))
    relative_path = _portable_relative_path(binding["relative_path"], f"{path}.relative_path")
    digest = _sha256(binding["file_sha256"], f"{path}.file_sha256")
    size = binding["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        _fail("EVIDENCE_BINDING", f"{path}.size_bytes", "must be a non-negative integer")
    locator = _mapping(binding["record_locator"], f"{path}.record_locator")
    _exact(locator, f"{path}.record_locator", ("kind", "byte_offset", "byte_length", "record_sha256"))
    if locator.get("kind") != "whole_file" or locator.get("byte_offset") != 0 or locator.get("byte_length") != size or _sha256(locator.get("record_sha256"), f"{path}.record_locator.record_sha256") != digest:
        _fail("EVIDENCE_BINDING", path, "must be a complete whole-file binding")
    try:
        raw, actual = read_regular_file_snapshot(root.joinpath(*PurePosixPath(relative_path).parts), trusted_root=root)
    except CalibrationError as error:
        _fail("EVIDENCE_FILE", relative_path, str(error))
    if actual != digest or len(raw) != size:
        _fail("EVIDENCE_BINDING", relative_path, "does not match the persisted binding")


def _load_bound_admission_input(
    measurement_root: Path, binding: Mapping[str, Any]
) -> tuple[object, dict[str, Any]]:
    _verify_binding(measurement_root, binding, "admission_receipt.bindings.admission_input")
    relative_path = _portable_relative_path(
        binding.get("relative_path"),
        "admission_receipt.bindings.admission_input.relative_path",
    )
    input_path = measurement_root.joinpath(*PurePosixPath(relative_path).parts)
    expected_sha = _sha256(
        binding.get("file_sha256"),
        "admission_receipt.bindings.admission_input.file_sha256",
    )
    try:
        value, actual_sha = read_verified_json(
            input_path,
            expected_sha256=expected_sha,
            require_sidecar=True,
            trusted_root=measurement_root,
        )
    except CalibrationError as error:
        _fail("ADMISSION_INPUT", relative_path, str(error))
    expected_binding = _binding(
        measurement_root,
        {
            "relative_path": relative_path,
            "record_locator": {"kind": "whole_file"},
        },
        "admission_input",
    )
    if actual_sha != expected_sha or expected_binding != dict(binding):
        _fail("ADMISSION_INPUT", relative_path, "does not match its persisted whole-file binding")
    return value, expected_binding


def _validate_dataset_static(manifest: Mapping[str, Any], source: Mapping[str, Any]) -> None:
    _reject_output_scope(manifest, "manifest")
    _reject_output_scope(source, "sources.open-measurements")
    manifest_fields = (
        "schema_version", "dataset_status", "production_pass", *PERMISSIONS, "concentration_basis", "wavelength_nm", "locked_conditions", "saunderson", "components", "backings", "splits", "counts", "predecessor", "source_files"
    )
    _exact(manifest, "manifest", manifest_fields)
    if manifest.get("schema_version") != DATASET_SCHEMA or manifest.get("dataset_status") != "open_selection_only":
        _fail("SCHEMA", "manifest", "has an unsupported schema or status")
    _assert_permissions(manifest, "manifest")
    if manifest.get("concentration_basis") != "nonvolatile_volume_fraction" or manifest.get("saunderson") != {"mode": "off"}:
        _fail("DATASET", "manifest", "does not retain the v1 concentration or correction policy")
    wavelengths = _validate_wavelengths(manifest.get("wavelength_nm"), "manifest.wavelength_nm")
    locked = _mapping(manifest.get("locked_conditions"), "manifest.locked_conditions")
    if not locked:
        _fail("LOCKED_CONDITIONS", "manifest.locked_conditions", "must not be empty")
    source_fields = ("schema_version", "dataset_status", "wavelength_nm", "locked_conditions", "measurement_session_id", "cards", "measurements", "evidence_bindings")
    _exact(source, "sources.open-measurements", source_fields)
    if source.get("schema_version") != SOURCE_SCHEMA or source.get("dataset_status") != "open_selection_only":
        _fail("SCHEMA", "sources.open-measurements", "has an unsupported schema or status")
    if source.get("wavelength_nm") != wavelengths or source.get("locked_conditions") != locked:
        _fail("DATASET", "sources.open-measurements", "does not match manifest wavelength or locked conditions")
    evidence = _mapping(source.get("evidence_bindings"), "sources.open-measurements.evidence_bindings")
    _exact(evidence, "sources.open-measurements.evidence_bindings", ("admission_input", "instrument_calibration", "instrument_run_log", "bare_spectra", "dft_records", "coated_spectra"))
    for condition_key, binding_key in (
        ("instrument_calibration_evidence", "instrument_calibration"),
        ("instrument_run_log_evidence", "instrument_run_log"),
    ):
        locator = _mapping(locked.get(condition_key), f"manifest.locked_conditions.{condition_key}")
        _exact(locator, f"manifest.locked_conditions.{condition_key}", ("relative_path", "record_locator"))
        if _portable_relative_path(locator["relative_path"], f"manifest.locked_conditions.{condition_key}.relative_path") != _mapping(evidence[binding_key], f"sources.open-measurements.evidence_bindings.{binding_key}").get("relative_path"):
            _fail("LOCKED_CONDITIONS", f"manifest.locked_conditions.{condition_key}", "does not match its persisted evidence binding")
        locator_kind = _mapping(locator["record_locator"], f"manifest.locked_conditions.{condition_key}.record_locator")
        if dict(locator_kind) != {"kind": "whole_file"}:
            _fail("LOCKED_CONDITIONS", f"manifest.locked_conditions.{condition_key}.record_locator", "must remain a whole-file locator")
    if len(_list(source.get("cards"), "sources.open-measurements.cards")) != 36 or len(_list(source.get("measurements"), "sources.open-measurements.measurements")) != 216:
        _fail("DATASET", "sources.open-measurements", "must retain the exact 36-card/216-reading roster")
    splits = _mapping(manifest.get("splits"), "manifest.splits")
    _exact(splits, "manifest.splits", ("train", "validation"))
    if len(_list(splits["train"], "manifest.splits.train")) != 30 or len(_list(splits["validation"], "manifest.splits.validation")) != 6:
        _fail("DATASET", "manifest.splits", "must retain 30 train and 6 validation cards")
    counts = _mapping(manifest.get("counts"), "manifest.counts")
    _exact(counts, "manifest.counts", ("cards", "coated_readings", "bare_backing_measurements"))
    if counts.get("cards") != 36 or counts.get("coated_readings") != 216:
        _fail("DATASET", "manifest.counts", "must retain exact card and reading counts")
    bare_counts = _mapping(counts.get("bare_backing_measurements"), "manifest.counts.bare_backing_measurements")
    _exact(bare_counts, "manifest.counts.bare_backing_measurements", _BACKINGS)
    if any(isinstance(bare_counts[name], bool) or not isinstance(bare_counts[name], int) or bare_counts[name] < 3 for name in _BACKINGS):
        _fail("DATASET", "manifest.counts.bare_backing_measurements", "must retain at least three records per backing")
    components = _list(manifest.get("components"), "manifest.components")
    if len(components) != len(COMPONENT_IDS):
        _fail("DATASET", "manifest.components", "must retain the fixed 15-component order")
    if [item.get("component_id") if isinstance(item, Mapping) else None for item in components] != list(COMPONENT_IDS):
        _fail("DATASET", "manifest.components", "does not retain the fixed component order")
    for index, measurement in enumerate(_list(source["measurements"], "sources.open-measurements.measurements")):
        item = _mapping(measurement, f"sources.open-measurements.measurements[{index}]")
        formula_components = _list(item.get("components"), f"sources.open-measurements.measurements[{index}].components")
        if len(formula_components) != len(COMPONENT_IDS):
            _fail("COMPONENTS", f"sources.open-measurements.measurements[{index}].components", "must retain all 15 receipt-derived components")
        fractions: list[float] = []
        for component_index, component in enumerate(formula_components):
            record = _mapping(component, f"sources.open-measurements.measurements[{index}].components[{component_index}]")
            _exact(record, f"sources.open-measurements.measurements[{index}].components[{component_index}]", ("component_id", "physical_lot_id", "nonvolatile_volume_fraction"))
            if record.get("component_id") != COMPONENT_IDS[component_index]:
                _fail("COMPONENTS", f"sources.open-measurements.measurements[{index}].components[{component_index}].component_id", "does not retain fixed component order")
            fraction = _number(record.get("nonvolatile_volume_fraction"), f"sources.open-measurements.measurements[{index}].components[{component_index}].nonvolatile_volume_fraction")
            if fraction < 0.0 or fraction > 1.0:
                _fail("COMPONENTS", f"sources.open-measurements.measurements[{index}].components[{component_index}].nonvolatile_volume_fraction", "must be in [0, 1]")
            fractions.append(fraction)
        if not math.isclose(math.fsum(fractions), 1.0, rel_tol=0.0, abs_tol=1e-12):
            _fail("COMPONENTS", f"sources.open-measurements.measurements[{index}].components", "nonvolatile_volume_fraction must sum to one")


def load_and_validate_open_selection_dataset(dataset_root: Path | str) -> ValidatedOpenSelectionDataset:
    """Validate only the immutable open-selection v1 schema."""

    root = _validate_root(dataset_root, "dataset_root")
    _validate_exact_output_tree(root)
    try:
        manifest_value, manifest_sha = read_verified_json(root / "manifest.json", require_sidecar=True, trusted_root=root)
        manifest = dict(_mapping(manifest_value, "manifest.json"))
        source_files = _list(manifest.get("source_files"), "manifest.source_files")
        if source_files != [{"path": "sources/open-measurements.json", "sha256": source_files[0].get("sha256") if source_files and isinstance(source_files[0], Mapping) else None, "kind": "open_measurement_records"}]:
            _fail("SOURCE_FILES", "manifest.source_files", "must contain only the v1 open measurement source")
        source_sha = _sha256(source_files[0]["sha256"], "manifest.source_files[0].sha256")
        source_value, actual_source_sha = read_verified_json(root / "sources" / "open-measurements.json", expected_sha256=source_sha, require_sidecar=True, trusted_root=root)
        source = dict(_mapping(source_value, "sources/open-measurements.json"))
    except CalibrationError as error:
        _fail("DATASET_BINDING", "dataset_root", str(error))
    _validate_dataset_static(manifest, source)
    return ValidatedOpenSelectionDataset(root=root, manifest=manifest, source=source, manifest_sha256=manifest_sha, open_measurements_sha256=actual_source_sha)


def _verify_receipt(receipt_path: Path | str) -> tuple[dict[str, Any], str]:
    try:
        value, digest = read_verified_json(Path(receipt_path), require_sidecar=True)
    except CalibrationError as error:
        _fail("RECEIPT_BINDING", "admission_receipt", str(error))
    receipt = dict(_mapping(value, "admission_receipt"))
    _reject_output_scope(receipt, "admission_receipt")
    fields = ("schema_version", "status", "state", "production_pass", *PERMISSIONS, "bindings", "counts", "receipt_payload_sha256")
    _exact(receipt, "admission_receipt", fields)
    if receipt.get("schema_version") != RECEIPT_SCHEMA or receipt.get("status") != "open_measurements_admitted" or receipt.get("state") != "OPEN_SELECTION_DATASET_ADMITTED":
        _fail("RECEIPT_BINDING", "admission_receipt", "has an unsupported schema, status, or state")
    _assert_permissions(receipt, "admission_receipt")
    payload_sha = _sha256(receipt.get("receipt_payload_sha256"), "admission_receipt.receipt_payload_sha256")
    payload = dict(receipt)
    payload.pop("receipt_payload_sha256")
    if sha256_bytes(canonical_json_bytes(payload)) != payload_sha:
        _fail("RECEIPT_BINDING", "admission_receipt.receipt_payload_sha256", "does not bind the receipt payload")
    return receipt, digest


def _validate_against_open_context(
    dataset: ValidatedOpenSelectionDataset, context: Mapping[str, Any]
) -> None:
    manifest = dataset.manifest
    source = dataset.source
    components = _component_summary(context)
    if manifest.get("components") != components:
        _fail("PREDECESSOR", "manifest.components", "does not match the revalidated component lots")
    expected_cards = _list(context["card_skeleton"], "context.card_skeleton")
    cards = _list(source["cards"], "sources.open-measurements.cards")
    required_card_fields = ("card_id", "formula_family_id", "formula_id", "formula_batch_id", "split", "dft_band", "primary_reading_slots", "dft_by_backing")
    if len(cards) != len(expected_cards):
        _fail("CARD_ROSTER", "sources.open-measurements.cards", "does not match the revalidated roster")
    card_by_id: dict[str, Mapping[str, Any]] = {}
    for index, expected in enumerate(expected_cards):
        card = _mapping(cards[index], f"sources.open-measurements.cards[{index}]")
        _exact(card, f"sources.open-measurements.cards[{index}]", required_card_fields)
        if {key: card[key] for key in required_card_fields if key != "dft_by_backing"} != expected:
            _fail("CARD_ROSTER", f"sources.open-measurements.cards[{index}]", "does not match the receipt-derived card identity")
        dft = _mapping(card["dft_by_backing"], f"sources.open-measurements.cards[{index}].dft_by_backing")
        _exact(dft, f"sources.open-measurements.cards[{index}].dft_by_backing", _BACKINGS)
        for backing in _BACKINGS:
            dft_record = _mapping(dft[backing], f"sources.open-measurements.cards[{index}].dft_by_backing.{backing}")
            _exact(dft_record, f"sources.open-measurements.cards[{index}].dft_by_backing.{backing}", ("dft_measurement_id", "measured_at_local", "dft_points_um", "dft_um", "dft_evidence"))
            points = [_number(value, f"sources.open-measurements.cards[{index}].dft_by_backing.{backing}.dft_points_um[{point_index}]", positive=True) for point_index, value in enumerate(_list(dft_record["dft_points_um"], f"sources.open-measurements.cards[{index}].dft_by_backing.{backing}.dft_points_um"))]
            if not points or not math.isclose(_number(dft_record["dft_um"], f"sources.open-measurements.cards[{index}].dft_by_backing.{backing}.dft_um", positive=True), _mean(points), rel_tol=0.0, abs_tol=1e-12):
                _fail("DFT_BINDING", f"sources.open-measurements.cards[{index}].dft_by_backing.{backing}", "does not retain the arithmetic mean of its points")
        card_by_id[card["card_id"]] = card
    splits = _mapping(manifest["splits"], "manifest.splits")
    for split in ("train", "validation"):
        expected_split = [{key: card[key] for key in ("card_id", "formula_family_id", "formula_id", "formula_batch_id", "split", "dft_band")} for card in cards if card["split"] == split]
        if splits[split] != expected_split:
            _fail("CARD_ROSTER", f"manifest.splits.{split}", "does not match source cards")

    backings = _mapping(manifest["backings"], "manifest.backings")
    _exact(backings, "manifest.backings", _BACKINGS)
    for backing in _BACKINGS:
        item = _mapping(backings[backing], f"manifest.backings.{backing}")
        _exact(item, f"manifest.backings.{backing}", ("backing_id", "lot_id", "bare_measurements", "bare_measurement_count", "mean_reflectance"))
        bare = _list(item["bare_measurements"], f"manifest.backings.{backing}.bare_measurements")
        if item["bare_measurement_count"] != len(bare) or len(bare) < 3:
            _fail("BARE_COUNT", f"manifest.backings.{backing}", "does not retain every admitted bare observation")
        bare_reflectances: list[list[float]] = []
        for index, record in enumerate(bare):
            bare_record = _mapping(record, f"manifest.backings.{backing}.bare_measurements[{index}]")
            _exact(bare_record, f"manifest.backings.{backing}.bare_measurements[{index}]", ("instrument_measurement_id", "measured_at_local", "reposition_id", "raw_spectrum_evidence", "reflectance"))
            bare_reflectances.append(
                _reflectance(bare_record["reflectance"], manifest["wavelength_nm"], f"manifest.backings.{backing}.bare_measurements[{index}].reflectance")
            )
        means = _reflectance(item["mean_reflectance"], manifest["wavelength_nm"], f"manifest.backings.{backing}.mean_reflectance")
        for position, mean in enumerate(means):
            if not math.isclose(mean, _mean([reflectance[position] for reflectance in bare_reflectances]), rel_tol=0.0, abs_tol=1e-12):
                _fail("BARE_BINDING", f"manifest.backings.{backing}.mean_reflectance", "does not equal the retained bare-record mean")

    condition_sha = sha256_bytes(canonical_json_bytes(manifest["locked_conditions"]))
    batches = {item["formula_family_id"]: item for item in _list(context["batches"], "context.batches")}
    measurements = _list(source["measurements"], "sources.open-measurements.measurements")
    expected_slots = {(card["card_id"], backing, position) for card in expected_cards for backing in _BACKINGS for position in _POSITIONS}
    observed_slots: set[tuple[str, str, str]] = set()
    for index, measurement in enumerate(measurements):
        item = _mapping(measurement, f"sources.open-measurements.measurements[{index}]")
        required = ("card_id", "backing", "reposition_id", "instrument_measurement_id", "position_note", "orientation", "measured_at_local", "raw_spectrum_evidence", "surface_status", "model_applicability_status", "backing_id", "backing_lot_id", "reflectance", "formula_family_id", "formula_id", "formula_batch_id", "split", "dft_band", "components", "dft_um", "dft_evidence", "locked_conditions_sha256", "target_kind")
        _exact(item, f"sources.open-measurements.measurements[{index}]", required)
        key = (item["card_id"], item["backing"], item["reposition_id"])
        if key not in expected_slots or key in observed_slots:
            _fail("READING_ROSTER", f"sources.open-measurements.measurements[{index}]", "does not match the fixed slot roster")
        observed_slots.add(key)
        card = card_by_id[item["card_id"]]
        for field in ("formula_family_id", "formula_id", "formula_batch_id", "split", "dft_band"):
            if item[field] != card[field]:
                _fail("READING_ROSTER", f"sources.open-measurements.measurements[{index}].{field}", "does not match its card")
        if item["target_kind"] != "measured_spectrum" or item["locked_conditions_sha256"] != condition_sha:
            _fail("READING_BINDING", f"sources.open-measurements.measurements[{index}]", "does not retain the measured target or condition hash")
        if not math.isclose(_number(item["dft_um"], f"sources.open-measurements.measurements[{index}].dft_um", positive=True), card["dft_by_backing"][item["backing"]]["dft_um"], rel_tol=0.0, abs_tol=1e-12) or item["dft_evidence"] != card["dft_by_backing"][item["backing"]]["dft_evidence"]:
            _fail("DFT_BINDING", f"sources.open-measurements.measurements[{index}]", "does not match the card/backing DFT record")
        if item["components"] != _formula_components(_mapping(batches[item["formula_family_id"]], "context.batch"), components):
            _fail("PREDECESSOR", f"sources.open-measurements.measurements[{index}].components", "does not match receipt-derived actual-NV fractions")
        _reflectance(item["reflectance"], manifest["wavelength_nm"], f"sources.open-measurements.measurements[{index}].reflectance")
    if observed_slots != expected_slots:
        _fail("READING_ROSTER", "sources.open-measurements.measurements", "does not retain the complete fixed roster")
    evidence = _mapping(source["evidence_bindings"], "sources.open-measurements.evidence_bindings")
    expected_bare = [record["raw_spectrum_evidence"] for backing in _BACKINGS for record in backings[backing]["bare_measurements"]]
    expected_dft = [card["dft_by_backing"][backing]["dft_evidence"] for card in cards for backing in _BACKINGS]
    expected_coated = [item["raw_spectrum_evidence"] for item in measurements]
    if evidence["bare_spectra"] != expected_bare or evidence["dft_records"] != expected_dft or evidence["coated_spectra"] != expected_coated:
        _fail("EVIDENCE_BINDING", "sources.open-measurements.evidence_bindings", "does not match the records it is required to bind")


def verify_open_measurement_admission(
    *,
    acquisition_receipt_path: Path | str,
    admission_receipt_path: Path | str,
    dataset_root: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
) -> dict[str, object]:
    """Reconstruct and reverify immutable open-only artifacts from their authorities."""

    measurement = _validate_root(measurement_root, "measurement_root")
    dataset = load_and_validate_open_selection_dataset(dataset_root)
    receipt_path = _validate_existing_file(
        admission_receipt_path, "admission_receipt_path"
    )
    try:
        resolved_receipt_path = receipt_path.resolve(strict=True)
    except OSError as error:
        _fail("RECEIPT_BINDING", "admission_receipt_path", str(error))
    if resolved_receipt_path != dataset.root / "admission-receipt.json":
        _fail(
            "RECEIPT_BINDING",
            "admission_receipt_path",
            "must identify dataset_root/admission-receipt.json",
        )
    receipt, receipt_sha = _verify_receipt(resolved_receipt_path)
    bindings = _mapping(receipt.get("bindings"), "admission_receipt.bindings")
    _exact(bindings, "admission_receipt.bindings", ("acquisition_preflight_receipt_sha256", "open_source_binding", "admission_input", "dataset_manifest", "open_measurements"))
    context = _open_context(
        receipt_path=acquisition_receipt_path,
        shared_root=shared_root,
        open_root=open_root,
    )
    input_binding = _mapping(
        bindings.get("admission_input"),
        "admission_receipt.bindings.admission_input",
    )
    input_value, expected_input_binding = _load_bound_admission_input(
        measurement, input_binding
    )
    expected_source, expected_manifest = _normalize_input(
        input_value,
        context=context,
        measurement_root=measurement,
        admission_binding=expected_input_binding,
    )
    expected_source_sha = _artifact_sha256(expected_source)
    expected_manifest["source_files"] = [
        {
            "path": "sources/open-measurements.json",
            "sha256": expected_source_sha,
            "kind": "open_measurement_records",
        }
    ]
    expected_manifest_sha = _artifact_sha256(expected_manifest)
    expected_receipt = _build_admission_receipt(
        context=context,
        admission_binding=expected_input_binding,
        counts=_mapping(expected_manifest["counts"], "expected_manifest.counts"),
        manifest_sha256=expected_manifest_sha,
        source_sha256=expected_source_sha,
    )
    expected_receipt_sha = _artifact_sha256(expected_receipt)

    if dataset.source != expected_source or dataset.open_measurements_sha256 != expected_source_sha:
        _fail(
            "SOURCE_RECONSTRUCTION",
            "sources/open-measurements.json",
            "does not equal the source reconstructed from the bound admission input",
        )
    if dataset.manifest != expected_manifest or dataset.manifest_sha256 != expected_manifest_sha:
        _fail(
            "MANIFEST_RECONSTRUCTION",
            "manifest.json",
            "does not equal the manifest reconstructed from current authorities",
        )
    if receipt != expected_receipt or receipt_sha != expected_receipt_sha:
        _fail(
            "RECEIPT_RECONSTRUCTION",
            "admission-receipt.json",
            "does not equal the canonical reconstructed admission receipt",
        )

    evidence = _mapping(
        expected_source["evidence_bindings"],
        "expected_source.evidence_bindings",
    )
    all_bindings = [
        _mapping(evidence["admission_input"], "evidence_bindings.admission_input"),
        _mapping(evidence["instrument_calibration"], "evidence_bindings.instrument_calibration"),
        _mapping(evidence["instrument_run_log"], "evidence_bindings.instrument_run_log"),
        *[
            _mapping(item, f"evidence_bindings.bare_spectra[{index}]")
            for index, item in enumerate(_list(evidence["bare_spectra"], "evidence_bindings.bare_spectra"))
        ],
        *[
            _mapping(item, f"evidence_bindings.dft_records[{index}]")
            for index, item in enumerate(_list(evidence["dft_records"], "evidence_bindings.dft_records"))
        ],
        *[
            _mapping(item, f"evidence_bindings.coated_spectra[{index}]")
            for index, item in enumerate(_list(evidence["coated_spectra"], "evidence_bindings.coated_spectra"))
        ],
    ]
    _assert_unique_bindings(all_bindings, "expected_source.evidence_bindings")
    return {
        "status": "open_measurement_admission_verified",
        "state": "OPEN_SELECTION_DATASET_ADMITTED",
        "dataset_manifest_sha256": dataset.manifest_sha256,
        "open_measurements_sha256": dataset.open_measurements_sha256,
        "admission_receipt_sha256": receipt_sha,
        "cards": 36,
        "readings": 216,
        "bare_backing_measurements": dict(dataset.manifest["counts"]["bare_backing_measurements"]),
        **_permissions(),
    }


def admit_open_measurements(
    *,
    acquisition_receipt_path: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
    admission_input_relative_path: str,
    output_dir: Path | str,
) -> dict[str, object]:
    """Reverify open acquisition, bind 36/216 real observations, and publish v1 artifacts."""

    measurement = _validate_root(measurement_root, "measurement_root")
    context = _open_context(receipt_path=acquisition_receipt_path, shared_root=shared_root, open_root=open_root)
    relative_input = _portable_relative_path(admission_input_relative_path, "admission_input_relative_path")
    try:
        input_value, input_sha = read_verified_json(measurement.joinpath(*PurePosixPath(relative_input).parts), require_sidecar=True, trusted_root=measurement)
    except CalibrationError as error:
        _fail("ADMISSION_INPUT", relative_input, str(error))
    input_binding = _binding(measurement, {"relative_path": relative_input, "record_locator": {"kind": "whole_file"}}, "admission_input")
    if input_binding["file_sha256"] != input_sha:
        _fail("ADMISSION_INPUT", relative_input, "changed while being read")
    source, manifest = _normalize_input(input_value, context=context, measurement_root=measurement, admission_binding=input_binding)
    output, staging, existed_empty = _staging(output_dir)
    try:
        source_sha = write_json_with_sha256(staging / "sources" / "open-measurements.json", source)
        manifest["source_files"] = [{"path": "sources/open-measurements.json", "sha256": source_sha, "kind": "open_measurement_records"}]
        manifest_sha = write_json_with_sha256(staging / "manifest.json", manifest)
        receipt = _build_admission_receipt(
            context=context,
            admission_binding=input_binding,
            counts=_mapping(manifest["counts"], "manifest.counts"),
            manifest_sha256=manifest_sha,
            source_sha256=source_sha,
        )
        receipt_sha = write_json_with_sha256(staging / "admission-receipt.json", receipt)
        load_and_validate_open_selection_dataset(staging)
        # Recheck immutable dataset/evidence bytes before the staging directory is published.
        verify_open_measurement_admission(
            acquisition_receipt_path=acquisition_receipt_path,
            admission_receipt_path=staging / "admission-receipt.json",
            dataset_root=staging,
            shared_root=shared_root,
            open_root=open_root,
            measurement_root=measurement,
        )
        _publish(output, staging, existed_empty)
        return {
            "status": receipt["status"],
            "state": receipt["state"],
            "dataset_manifest_sha256": manifest_sha,
            "open_measurements_sha256": source_sha,
            "admission_receipt_sha256": receipt_sha,
            "cards": 36,
            "readings": 216,
            "bare_backing_measurements": dict(manifest["counts"]["bare_backing_measurements"]),
            "output_dir": str(output),
            **_permissions(),
        }
    except OpenMeasurementAdmissionError:
        raise
    except (CalibrationError, OSError) as error:
        _fail("OUTPUT_WRITE", str(staging), str(error))
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
