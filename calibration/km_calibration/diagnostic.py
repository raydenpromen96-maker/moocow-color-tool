"""Fail-closed four-card physical-acquisition normalization and preflight.

This module deliberately owns a distinct acquisition contract.  It never
imports the research-dataset loader or any fitting code, so a successful
four-card preflight cannot be mistaken for model-ready evidence.
"""

from __future__ import annotations

import copy
import csv
import datetime as dt
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from .errors import CalibrationError
from .hashing import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
    verify_sha256_sidecar,
    write_json_with_sha256,
)


DIAGNOSTIC_SCHEMA_VERSION = "moocow-physical-diagnostic-acquisition-v2"
PREFLIGHT_RECEIPT_SCHEMA_VERSION = "moocow-physical-diagnostic-preflight-receipt-v2"
EVIDENCE_VERIFICATION_SCHEMA_VERSION = "moocow-physical-evidence-verification-v1"
WEIGHING_PLAN_INPUT_SCHEMA_VERSION = "moocow-four-card-weighing-plan-input-v2"
WEIGHING_PLAN_SCHEMA_VERSION = "moocow-four-card-weighing-plan-v2"
CONVERSION_PROPERTY_RECORD_SCHEMA_VERSION = "moocow-conversion-property-record-v2"
ACTUAL_WEIGHING_RECORD_SCHEMA_VERSION = "moocow-actual-weighing-record-v2"
MASS_SOLIDS_NONVOLATILE_DENSITY = "mass_solids_nonvolatile_density"
WET_DENSITY_VOLUME_SOLIDS = "wet_density_volume_solids"
CONVERSION_ROUTES = (
    MASS_SOLIDS_NONVOLATILE_DENSITY,
    WET_DENSITY_VOLUME_SOLIDS,
)
TARGET_DEVIATION_STATUS = "reported_no_physical_threshold"
_COMPUTATIONAL_ABS_TOLERANCE = 1e-12
_PROPERTY_MATRIX = {
    MASS_SOLIDS_NONVOLATILE_DENSITY: (
        ("nonvolatile_mass_fraction", "fraction", True),
        ("nonvolatile_density_g_ml", "g/mL", False),
    ),
    WET_DENSITY_VOLUME_SOLIDS: (
        ("wet_density_g_ml", "g/mL", False),
        ("component_nonvolatile_volume_fraction", "fraction", True),
    ),
}
CSV_COLUMNS = (
    "card_id",
    "backing",
    "reposition_id",
    "instrument_measurement_id",
    "position_note",
    "orientation",
    "wavelength_nm",
    "reflectance",
)
CARD_ROSTER = (
    ("CARD-DX-BASE-DFT-L-001", "FAM-DX-BASE", "DFT-L", (("base-waterborne-clear", 1.0),)),
    (
        "CARD-DX-W064-DFT-L-001",
        "FAM-DX-W064",
        "DFT-L",
        (("base-waterborne-clear", 0.85), ("colorant-W064", 0.15)),
    ),
    ("CARD-DX-BASE-DFT-H-001", "FAM-DX-BASE", "DFT-H", (("base-waterborne-clear", 1.0),)),
    (
        "CARD-DX-W064-DFT-H-001",
        "FAM-DX-W064",
        "DFT-H",
        (("base-waterborne-clear", 0.85), ("colorant-W064", 0.15)),
    ),
)
BACKINGS = ("black", "white")
POSITIONS = ("POS01", "POS02", "POS03")
_CARD_BY_ID = {item[0]: item for item in CARD_ROSTER}
_CARD_INDEX = {card_id: index for index, (card_id, *_rest) in enumerate(CARD_ROSTER)}
_BACKING_INDEX = {name: index for index, name in enumerate(BACKINGS)}
_POSITION_INDEX = {name: index for index, name in enumerate(POSITIONS)}
_FAMILY_TARGETS: dict[str, tuple[tuple[str, float], ...]] = {}
for _card_id, _family, _band, _components in CARD_ROSTER:
    _FAMILY_TARGETS.setdefault(_family, _components)


class DiagnosticValidationError(CalibrationError):
    """A machine-readable field/code diagnostic for acquisition preflight."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"[{code}] {path}: {message}")


@dataclass(frozen=True)
class StructuralDiagnosticBundle:
    """Canonical v2 transport with locators but without file-derived bindings."""

    payload: dict[str, Any]
    canonical_bytes: bytes
    structural_sha256: str

    @property
    def canonical_sha256(self) -> str:
        """Compatibility alias for callers that only need the structural digest."""
        return self.structural_sha256


DiagnosticBundle = StructuralDiagnosticBundle


@dataclass(frozen=True)
class EvidenceReadyBundle:
    """A structural bundle whose locators were cryptographically materialized."""

    payload: dict[str, Any]
    canonical_bytes: bytes
    diagnostic_payload_sha256: str
    evidence_verification: dict[str, Any]
    evidence_verification_sha256: str


@dataclass
class EvidenceUse:
    """One schema-owned evidence field discovered during materialization."""

    logical_path: str
    evidence_kind: str
    parent: MutableMapping[str, Any]
    field: str
    semantic_context: Mapping[str, Any] | None = None

    @property
    def locator(self) -> Mapping[str, Any]:
        return _mapping(self.parent[self.field], self.logical_path)


@dataclass(frozen=True)
class PreflightReport:
    """Derived readiness evidence; it carries no fitting or promotion state."""

    payload: dict[str, Any]


def _fail(code: str, path: str, message: str) -> None:
    raise DiagnosticValidationError(code, path, message)


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("TYPE", path, "must be an object")
    return value


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("TYPE", path, "must be an array")
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str):
        _fail("TYPE", path, "must be a non-empty string")
    normalized = value.strip()
    if not normalized:
        _fail("REQUIRED_TEXT", path, "must be a non-empty string")
    lowered = normalized.lower()
    if normalized.upper().startswith(("TEMPLATE_", "REQUIRED_")):
        _fail("PLACEHOLDER", path, "contains an unresolved placeholder")
    if "synthetic" in lowered or "reference_only" in lowered:
        _fail("NON_MEASURED_EVIDENCE", path, "cannot contain synthetic or reference_only evidence")
    return normalized


def _number(value: object, path: str, *, positive: bool = False) -> float:
    if not _is_number(value):
        _fail("FINITE_NUMBER", path, "must be a finite numeric value")
    result = float(value)
    if positive and result <= 0:
        _fail("POSITIVE_NUMBER", path, "must be greater than zero")
    return result


def _sha256(value: object, path: str) -> str:
    text = _text(value, path).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        _fail("SHA256", path, "must be a 64-character SHA-256 hexadecimal digest")
    if text == "0" * 64:
        _fail("SHA256_ZERO", path, "cannot be the all-zero non-evidence digest")
    return text


def _timestamp(value: object, path: str) -> str:
    text = _text(value, path)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail("TIMESTAMP", path, "must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("TIMESTAMP_TIMEZONE", path, "must include a timezone offset")
    return text


def _timestamp_value(value: object, path: str) -> dt.datetime:
    return dt.datetime.fromisoformat(_timestamp(value, path).replace("Z", "+00:00"))


def _relative_posix_evidence_path(value: object, path: str) -> str:
    text = _text(value, path)
    windows_path = PureWindowsPath(text)
    posix_path = PurePosixPath(text)
    if (
        "\\" in text
        or "\x00" in text
        or any(character in '<>:"|?*' for character in text)
        or windows_path.is_absolute()
        or windows_path.drive
        or posix_path.is_absolute()
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        _fail("EVIDENCE_PATH", path, "must be a portable relative path without traversal")
    return text


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("INTEGER", path, f"must be an integer greater than or equal to {minimum}")
    return value


def _normalize_evidence_locator(value: object, path: str) -> dict[str, Any]:
    raw = _mapping(value, path)
    _exact_fields(raw, path, ("relative_path", "record_locator"))
    relative_path = _relative_posix_evidence_path(raw["relative_path"], f"{path}.relative_path")
    record = _mapping(raw["record_locator"], f"{path}.record_locator")
    kind = _text(record.get("kind"), f"{path}.record_locator.kind")
    if kind == "whole_file":
        _exact_fields(record, f"{path}.record_locator", ("kind",))
        normalized_record: dict[str, Any] = {"kind": "whole_file"}
    elif kind == "byte_range":
        _exact_fields(record, f"{path}.record_locator", ("kind", "byte_offset", "byte_length"))
        normalized_record = {
            "kind": "byte_range",
            "byte_offset": _integer(record["byte_offset"], f"{path}.record_locator.byte_offset"),
            "byte_length": _integer(record["byte_length"], f"{path}.record_locator.byte_length", minimum=1),
        }
    else:
        _fail("EVIDENCE_LOCATOR_KIND", f"{path}.record_locator.kind", "must be whole_file or byte_range")
    return {"relative_path": relative_path, "record_locator": normalized_record}


def _resolve_evidence_root(root: Path | str) -> Path:
    try:
        resolved = Path(root).resolve(strict=True)
    except OSError as error:
        _fail("EVIDENCE_ROOT", str(root), str(error))
    if not resolved.is_dir():
        _fail("EVIDENCE_ROOT", str(root), "must be an existing directory")
    return resolved


def _resolve_evidence_file(locator: Mapping[str, Any], *, root: Path, path: str) -> Path:
    relative_path = _relative_posix_evidence_path(locator.get("relative_path"), f"{path}.relative_path")
    try:
        candidate = root.joinpath(*PurePosixPath(relative_path).parts).resolve(strict=True)
    except OSError as error:
        _fail("EVIDENCE_FILE", f"{path}.relative_path", str(error))
    try:
        candidate.relative_to(root)
    except ValueError:
        _fail("EVIDENCE_ROOT_ESCAPE", f"{path}.relative_path", "resolves outside evidence_root")
    if not candidate.is_file():
        _fail("EVIDENCE_FILE", f"{path}.relative_path", "must resolve to a regular file")
    return candidate


def _materialize_evidence_record(
    locator: Mapping[str, Any], *, root: Path, path: str, file_cache: dict[str, tuple[int, int, str]] | None = None
) -> tuple[dict[str, Any], bytes]:
    """Open an evidence file, prove it stayed stable, and hash its exact locator range."""
    normalized = _normalize_evidence_locator(locator, path)
    candidate = _resolve_evidence_file(normalized, root=root, path=path)
    cache_key = normalized["relative_path"]
    try:
        before = candidate.stat()
    except OSError as error:
        _fail("EVIDENCE_FILE", f"{path}.relative_path", str(error))
    cached = file_cache.get(cache_key) if file_cache is not None else None
    if cached is not None and cached[:2] == (before.st_size, before.st_mtime_ns):
        file_sha256 = cached[2]
    else:
        try:
            file_sha256 = sha256_file(candidate)
        except OSError as error:
            _fail("EVIDENCE_FILE", f"{path}.relative_path", str(error))
    record_locator = normalized["record_locator"]
    if record_locator["kind"] == "whole_file":
        byte_offset = 0
        byte_length = before.st_size
    else:
        byte_offset = record_locator["byte_offset"]
        byte_length = record_locator["byte_length"]
    if byte_offset + byte_length > before.st_size:
        _fail("EVIDENCE_RANGE", f"{path}.record_locator", "must remain within the referenced file")
    try:
        with candidate.open("rb") as handle:
            handle.seek(byte_offset)
            record_bytes = handle.read(byte_length)
    except OSError as error:
        _fail("EVIDENCE_FILE", f"{path}.relative_path", str(error))
    if len(record_bytes) != byte_length:
        _fail("EVIDENCE_RANGE", f"{path}.record_locator", "could not read the requested byte range")
    try:
        after = candidate.stat()
    except OSError as error:
        _fail("EVIDENCE_FILE", f"{path}.relative_path", str(error))
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        _fail("EVIDENCE_FILE_MUTATED", f"{path}.relative_path", "changed while evidence was being hashed")
    if file_cache is not None:
        file_cache[cache_key] = (before.st_size, before.st_mtime_ns, file_sha256)
    binding = {
        "relative_path": normalized["relative_path"],
        "file_sha256": file_sha256,
        "size_bytes": before.st_size,
        "record_locator": {
            "kind": record_locator["kind"],
            "byte_offset": byte_offset,
            "byte_length": byte_length,
            "record_sha256": sha256_bytes(record_bytes),
        },
    }
    return binding, record_bytes


def _materialize_evidence(
    locator: Mapping[str, Any], *, root: Path, path: str, file_cache: dict[str, tuple[int, int, str]] | None = None
) -> dict[str, Any]:
    binding, _record_bytes = _materialize_evidence_record(
        locator, root=root, path=path, file_cache=file_cache
    )
    return binding


def _exact_fields(value: Mapping[str, Any], path: str, required: Iterable[str]) -> None:
    expected = set(required)
    actual = set(value)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        _fail("MISSING_FIELD", path, f"missing required fields: {', '.join(missing)}")
    if unexpected:
        _fail("UNKNOWN_FIELD", path, f"contains unsupported fields: {', '.join(unexpected)}")


def _reject_unmeasured_strings(value: object, path: str = "$") -> None:
    """Reject sentinel/source classes anywhere, including otherwise ignored notes."""
    if value is None:
        _fail("NULL", path, "null is not valid acquisition evidence")
    if isinstance(value, str):
        _text(value, path)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_unmeasured_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_unmeasured_strings(item, f"{path}[{index}]")


def _normalize_grid(value: object, path: str) -> list[float]:
    raw = _list(value, path)
    if len(raw) < 3:
        _fail("GRID_LENGTH", path, "must contain at least three wavelength values")
    wavelengths = [_number(item, f"{path}[{index}]", positive=True) for index, item in enumerate(raw)]
    if len(set(wavelengths)) != len(wavelengths):
        _fail("GRID_DUPLICATE", path, "cannot contain duplicate wavelengths")
    wavelengths.sort()
    intervals = [later - earlier for earlier, later in zip(wavelengths, wavelengths[1:])]
    if any(interval <= 0 for interval in intervals) or not all(
        math.isclose(interval, intervals[0], rel_tol=0.0, abs_tol=1e-9) for interval in intervals[1:]
    ):
        _fail("GRID_NONUNIFORM", path, "must be strictly increasing and uniformly spaced")
    if wavelengths[0] < 360 or wavelengths[-1] > 830:
        _fail("GRID_RANGE", path, "must stay within the supported 360-830 nm range")
    if wavelengths[0] > 400 or wavelengths[-1] < 700 or intervals[0] > 20:
        _fail("VISIBLE_GRID", path, "must cover 400-700 nm or wider at a uniform interval no greater than 20 nm")
    return wavelengths


def _reflectance_scale(value: object, path: str) -> str:
    text = _text(value, path)
    if text not in {"fraction", "percent"}:
        _fail("REFLECTANCE_SCALE", path, "must be exactly fraction or percent")
    return text


def _normalize_reflectance(value: object, path: str, scale: str, expected_length: int) -> list[float]:
    raw = _list(value, path)
    if len(raw) != expected_length:
        _fail("REFLECTANCE_LENGTH", path, "must match the declared wavelength grid length")
    upper = 1.0 if scale == "fraction" else 100.0
    normalized: list[float] = []
    for index, item in enumerate(raw):
        reading = _number(item, f"{path}[{index}]")
        if not 0 <= reading <= upper:
            _fail("REFLECTANCE_RANGE", f"{path}[{index}]", f"must be in [0, {upper:g}] for {scale} scale")
        normalized.append(reading if scale == "fraction" else reading / 100.0)
    return normalized


def _normalize_locked_conditions(value: object, wavelength_nm: list[float]) -> dict[str, Any]:
    path = "locked_conditions"
    raw = _mapping(value, path)
    fields = (
        "instrument_make_model",
        "instrument_serial_number",
        "instrument_software_version",
        "instrument_firmware_version",
        "instrument_calibration_id",
        "instrument_calibration_timestamp",
        "instrument_calibration_result",
        "instrument_calibration_evidence",
        "instrument_run_log_evidence",
        "white_standard_id",
        "black_calibration_mode",
        "measurement_geometry",
        "aperture_mm",
        "specular_condition",
        "uv_setting",
        "measurement_mode",
        "illuminant",
        "observer",
        "wavelength_start_nm",
        "wavelength_end_nm",
        "wavelength_interval_nm",
        "wavelength_unit",
        "spectral_bandpass_nm",
        "reflectance_scale",
        "cure_protocol",
        "cure_start",
        "cure_end",
        "age_at_measurement_h",
        "cure_temperature_c_observed",
        "cure_rh_pct_observed",
        "airflow_note",
        "application_method",
        "applicator_or_wft_target",
        "operator_id",
    )
    _exact_fields(raw, path, fields)
    scale = _reflectance_scale(raw["reflectance_scale"], f"{path}.reflectance_scale")
    start = _number(raw["wavelength_start_nm"], f"{path}.wavelength_start_nm", positive=True)
    end = _number(raw["wavelength_end_nm"], f"{path}.wavelength_end_nm", positive=True)
    interval = _number(raw["wavelength_interval_nm"], f"{path}.wavelength_interval_nm", positive=True)
    if raw["wavelength_unit"] != "nm":
        _fail("WAVELENGTH_UNIT", f"{path}.wavelength_unit", "must be exactly nm")
    if not (
        math.isclose(start, wavelength_nm[0], rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(end, wavelength_nm[-1], rel_tol=0.0, abs_tol=1e-9)
        and math.isclose(interval, wavelength_nm[1] - wavelength_nm[0], rel_tol=0.0, abs_tol=1e-9)
    ):
        _fail("GRID_CONDITION_MISMATCH", path, "declared start/end/interval do not match wavelength_nm")
    cure_start = _timestamp(raw["cure_start"], f"{path}.cure_start")
    cure_end = _timestamp(raw["cure_end"], f"{path}.cure_end")
    if _timestamp_value(cure_end, f"{path}.cure_end") <= _timestamp_value(cure_start, f"{path}.cure_start"):
        _fail("CURE_TIMELINE", path, "cure_end must be later than cure_start")
    if _text(raw["instrument_calibration_result"], f"{path}.instrument_calibration_result") != "pass":
        _fail("CALIBRATION_RESULT", f"{path}.instrument_calibration_result", "must be the explicit pass state")
    observed_rh = _number(raw["cure_rh_pct_observed"], f"{path}.cure_rh_pct_observed")
    if not 0 <= observed_rh <= 100:
        _fail("RH_RANGE", f"{path}.cure_rh_pct_observed", "must be in [0, 100]")
    result: dict[str, Any] = {}
    for field in fields:
        if field in {"aperture_mm", "spectral_bandpass_nm", "age_at_measurement_h"}:
            result[field] = _number(raw[field], f"{path}.{field}", positive=True)
        elif field == "cure_temperature_c_observed":
            result[field] = _number(raw[field], f"{path}.{field}")
        elif field == "cure_rh_pct_observed":
            result[field] = observed_rh
        elif field in {"wavelength_start_nm", "wavelength_end_nm", "wavelength_interval_nm"}:
            result[field] = _number(raw[field], f"{path}.{field}", positive=True)
        elif field == "reflectance_scale":
            result[field] = "fraction"
        elif field in {"instrument_calibration_evidence", "instrument_run_log_evidence"}:
            result[field] = _normalize_evidence_locator(raw[field], f"{path}.{field}")
        elif field in {"cure_start", "cure_end", "instrument_calibration_timestamp"}:
            result[field] = _timestamp(raw[field], f"{path}.{field}")
        else:
            result[field] = _text(raw[field], f"{path}.{field}")
    return result


def _normalize_materials(value: object) -> dict[str, dict[str, str]]:
    raw = _mapping(value, "materials")
    _exact_fields(raw, "materials", ("base", "w064"))
    result: dict[str, dict[str, str]] = {}
    for name, expected_component in (("base", "base-waterborne-clear"), ("w064", "colorant-W064")):
        material = _mapping(raw[name], f"materials.{name}")
        fields = (
            "component_id",
            "product_name",
            "manufacturer_or_supplier",
            "batch_id",
            "physical_label_verification_status",
            "physical_label_verification_id",
            "physical_label_verified_at",
            "physical_label_evidence",
        )
        _exact_fields(material, f"materials.{name}", fields)
        component_id = _text(material["component_id"], f"materials.{name}.component_id")
        if component_id != expected_component:
            _fail("MATERIAL_ID", f"materials.{name}.component_id", f"must be {expected_component}")
        verification = _text(material["physical_label_verification_status"], f"materials.{name}.physical_label_verification_status")
        if verification != "verified_physical_label":
            _fail("LOT_VERIFICATION", f"materials.{name}.physical_label_verification_status", "must be verified_physical_label; catalog-only evidence is not accepted")
        result[name] = {
            "component_id": component_id,
            "product_name": _text(material["product_name"], f"materials.{name}.product_name"),
            "manufacturer_or_supplier": _text(material["manufacturer_or_supplier"], f"materials.{name}.manufacturer_or_supplier"),
            "batch_id": _text(material["batch_id"], f"materials.{name}.batch_id"),
            "physical_label_verification_status": verification,
            "physical_label_verification_id": _text(material["physical_label_verification_id"], f"materials.{name}.physical_label_verification_id"),
            "physical_label_verified_at": _timestamp(material["physical_label_verified_at"], f"materials.{name}.physical_label_verified_at"),
            "physical_label_evidence": _normalize_evidence_locator(
                material["physical_label_evidence"], f"materials.{name}.physical_label_evidence"
            ),
        }
    return result


def _normalize_dft_bands(value: object) -> dict[str, dict[str, float]]:
    raw = _mapping(value, "dft_bands")
    _exact_fields(raw, "dft_bands", ("DFT-L", "DFT-H"))
    result: dict[str, dict[str, float]] = {}
    for name in ("DFT-L", "DFT-H"):
        band = _mapping(raw[name], f"dft_bands.{name}")
        _exact_fields(band, f"dft_bands.{name}", ("target_um", "acceptance_min_um", "acceptance_max_um"))
        target = _number(band["target_um"], f"dft_bands.{name}.target_um", positive=True)
        minimum = _number(band["acceptance_min_um"], f"dft_bands.{name}.acceptance_min_um", positive=True)
        maximum = _number(band["acceptance_max_um"], f"dft_bands.{name}.acceptance_max_um", positive=True)
        if minimum > maximum or not minimum <= target <= maximum:
            _fail("DFT_BAND", f"dft_bands.{name}", "must satisfy acceptance_min_um <= target_um <= acceptance_max_um")
        result[name] = {"target_um": target, "acceptance_min_um": minimum, "acceptance_max_um": maximum}
    return result


def _conversion_route(value: object, path: str) -> str:
    route = _text(value, path)
    if route not in CONVERSION_ROUTES:
        _fail("CONVERSION_ROUTE", path, f"must be one of {list(CONVERSION_ROUTES)}")
    return route


def _normalize_property_record(
    value: object,
    path: str,
    *,
    component_id: str,
    physical_lot_id: str,
    expected_unit: str,
    fraction_value: bool,
) -> dict[str, Any]:
    raw = _mapping(value, path)
    fields = (
        "property_record_id",
        "component_id",
        "physical_lot_id",
        "value",
        "unit",
        "method",
        "observed_at",
        "property_record_evidence",
    )
    _exact_fields(raw, path, fields)
    record_component_id = _text(raw["component_id"], f"{path}.component_id")
    if record_component_id != component_id:
        _fail("PROPERTY_COMPONENT", f"{path}.component_id", f"must match {component_id}")
    record_lot_id = _text(raw["physical_lot_id"], f"{path}.physical_lot_id")
    if record_lot_id != physical_lot_id:
        _fail("PROPERTY_LOT", f"{path}.physical_lot_id", f"must match {physical_lot_id}")
    unit = _text(raw["unit"], f"{path}.unit")
    if unit != expected_unit:
        _fail("PROPERTY_UNIT", f"{path}.unit", f"must be exactly {expected_unit}")
    property_value = _number(raw["value"], f"{path}.value", positive=True)
    if fraction_value and property_value > 1:
        _fail("PROPERTY_FRACTION", f"{path}.value", "must be in (0, 1]")
    return {
        "property_record_id": _text(raw["property_record_id"], f"{path}.property_record_id"),
        "component_id": record_component_id,
        "physical_lot_id": record_lot_id,
        "value": property_value,
        "unit": unit,
        "method": _text(raw["method"], f"{path}.method"),
        "observed_at": _timestamp(raw["observed_at"], f"{path}.observed_at"),
        "property_record_evidence": _normalize_evidence_locator(
            raw["property_record_evidence"], f"{path}.property_record_evidence"
        ),
    }


def _normalize_property_records(
    value: object,
    path: str,
    *,
    route: str,
    component_id: str,
    physical_lot_id: str,
) -> dict[str, dict[str, Any]]:
    raw = _mapping(value, path)
    matrix = _PROPERTY_MATRIX[route]
    _exact_fields(raw, path, (name for name, _unit, _fraction in matrix))
    return {
        name: _normalize_property_record(
            raw[name],
            f"{path}.{name}",
            component_id=component_id,
            physical_lot_id=physical_lot_id,
            expected_unit=unit,
            fraction_value=fraction,
        )
        for name, unit, fraction in matrix
    }


def _normalize_actual_weighing(
    value: object,
    path: str,
    *,
    component_id: str,
    physical_lot_id: str,
) -> dict[str, Any]:
    raw = _mapping(value, path)
    fields = (
        "record_kind",
        "weighing_record_id",
        "weighing_event_id",
        "component_id",
        "physical_lot_id",
        "actual_wet_mass_g",
        "actual_wet_mass_unit",
        "weighing_method",
        "weighed_at",
        "weighing_record_evidence",
    )
    _exact_fields(raw, path, fields)
    if raw["record_kind"] != "actual_weighing_observation":
        _fail(
            "ACTUAL_WEIGHING_KIND",
            f"{path}.record_kind",
            "must be actual_weighing_observation; a weighing plan is not actual evidence",
        )
    weighing_component_id = _text(raw["component_id"], f"{path}.component_id")
    if weighing_component_id != component_id:
        _fail("WEIGHING_COMPONENT", f"{path}.component_id", f"must match {component_id}")
    weighing_lot_id = _text(raw["physical_lot_id"], f"{path}.physical_lot_id")
    if weighing_lot_id != physical_lot_id:
        _fail("WEIGHING_LOT", f"{path}.physical_lot_id", f"must match {physical_lot_id}")
    unit = _text(raw["actual_wet_mass_unit"], f"{path}.actual_wet_mass_unit")
    if unit != "g":
        _fail("WEIGHING_UNIT", f"{path}.actual_wet_mass_unit", "must be exactly g")
    return {
        "record_kind": "actual_weighing_observation",
        "weighing_record_id": _text(raw["weighing_record_id"], f"{path}.weighing_record_id"),
        "weighing_event_id": _text(raw["weighing_event_id"], f"{path}.weighing_event_id"),
        "component_id": weighing_component_id,
        "physical_lot_id": weighing_lot_id,
        "actual_wet_mass_g": _number(raw["actual_wet_mass_g"], f"{path}.actual_wet_mass_g", positive=True),
        "actual_wet_mass_unit": unit,
        "weighing_method": _text(raw["weighing_method"], f"{path}.weighing_method"),
        "weighed_at": _timestamp(raw["weighed_at"], f"{path}.weighed_at"),
        "weighing_record_evidence": _normalize_evidence_locator(
            raw["weighing_record_evidence"], f"{path}.weighing_record_evidence"
        ),
    }


def _component_nonvolatile_volume_ml(
    route: str, *, wet_mass_g: float, property_records: Mapping[str, Mapping[str, Any]]
) -> float:
    if route == MASS_SOLIDS_NONVOLATILE_DENSITY:
        return (
            wet_mass_g
            * float(property_records["nonvolatile_mass_fraction"]["value"])
            / float(property_records["nonvolatile_density_g_ml"]["value"])
        )
    return (
        wet_mass_g
        / float(property_records["wet_density_g_ml"]["value"])
        * float(property_records["component_nonvolatile_volume_fraction"]["value"])
    )


def _planned_wet_mass_g(
    route: str,
    *,
    target_fraction: float,
    planned_total_nonvolatile_volume_ml: float,
    property_records: Mapping[str, Mapping[str, Any]],
) -> float:
    target_volume = target_fraction * planned_total_nonvolatile_volume_ml
    if route == MASS_SOLIDS_NONVOLATILE_DENSITY:
        return (
            target_volume
            * float(property_records["nonvolatile_density_g_ml"]["value"])
            / float(property_records["nonvolatile_mass_fraction"]["value"])
        )
    return (
        target_volume
        * float(property_records["wet_density_g_ml"]["value"])
        / float(property_records["component_nonvolatile_volume_fraction"]["value"])
    )


def _normalize_formula(
    value: object,
    path: str,
    expected_components: tuple[tuple[str, float], ...],
    material_lots: Mapping[str, str],
) -> dict[str, Any]:
    raw = _mapping(value, path)
    fields = ("formula_id", "formula_batch_id", "formula_stage", "conversion_route", "components")
    _exact_fields(raw, path, fields)
    if raw["formula_stage"] != "four_card_diagnostic":
        _fail("FORMULA_STAGE", f"{path}.formula_stage", "must be four_card_diagnostic; pilot payloads use a separate importer")
    route = _conversion_route(raw["conversion_route"], f"{path}.conversion_route")
    raw_components = _list(raw["components"], f"{path}.components")
    expected = dict(expected_components)
    components: dict[str, dict[str, Any]] = {}
    for index, raw_component in enumerate(raw_components):
        component_path = f"{path}.components[{index}]"
        component = _mapping(raw_component, component_path)
        _exact_fields(
            component,
            component_path,
            (
                "component_id",
                "physical_lot_id",
                "target_nonvolatile_volume_fraction",
                "actual_weighing",
                "property_records",
            ),
        )
        component_id = _text(component["component_id"], f"{component_path}.component_id")
        if component_id in components:
            _fail("FORMULA_DUPLICATE_COMPONENT", f"{component_path}.component_id", "must not repeat a component")
        if component_id not in expected:
            _fail("FORMULA_COMPONENTS", f"{component_path}.component_id", f"must be one of {sorted(expected)}")
        physical_lot_id = _text(component["physical_lot_id"], f"{component_path}.physical_lot_id")
        if material_lots.get(component_id) != physical_lot_id:
            _fail(
                "PHYSICAL_LOT",
                f"{component_path}.physical_lot_id",
                "must match the physical-label-verified material lot",
            )
        target_fraction = _number(
            component["target_nonvolatile_volume_fraction"],
            f"{component_path}.target_nonvolatile_volume_fraction",
            positive=True,
        )
        if target_fraction > 1:
            _fail("FORMULA_TARGET", f"{component_path}.target_nonvolatile_volume_fraction", "must be in (0, 1]")
        if not math.isclose(
            target_fraction,
            expected[component_id],
            rel_tol=0.0,
            abs_tol=_COMPUTATIONAL_ABS_TOLERANCE,
        ):
            _fail(
                "FORMULA_ROSTER",
                f"{component_path}.target_nonvolatile_volume_fraction",
                f"must be the fixed target {expected[component_id]:g} for this diagnostic card",
            )
        actual_weighing = _normalize_actual_weighing(
            component["actual_weighing"],
            f"{component_path}.actual_weighing",
            component_id=component_id,
            physical_lot_id=physical_lot_id,
        )
        property_records = _normalize_property_records(
            component["property_records"],
            f"{component_path}.property_records",
            route=route,
            component_id=component_id,
            physical_lot_id=physical_lot_id,
        )
        components[component_id] = {
            "physical_lot_id": physical_lot_id,
            "target_nonvolatile_volume_fraction": target_fraction,
            "actual_weighing": actual_weighing,
            "property_records": property_records,
        }
    if set(components) != set(expected):
        _fail("FORMULA_COMPONENTS", f"{path}.components", f"must contain exactly {sorted(expected)}")
    target_sum = math.fsum(item["target_nonvolatile_volume_fraction"] for item in components.values())
    if not math.isclose(target_sum, 1.0, rel_tol=0.0, abs_tol=_COMPUTATIONAL_ABS_TOLERANCE):
        _fail("FORMULA_TARGET_SUM", f"{path}.components", "fixed target fractions must sum to 1")
    volumes = {
        component_id: _component_nonvolatile_volume_ml(
            route,
            wet_mass_g=float(item["actual_weighing"]["actual_wet_mass_g"]),
            property_records=item["property_records"],
        )
        for component_id, item in components.items()
    }
    total_volume = math.fsum(volumes.values())
    if not math.isfinite(total_volume) or total_volume <= 0:
        _fail("FORMULA_CONVERSION", f"{path}.components", "must yield positive finite nonvolatile volume")
    normalized_components = [
        {"component_id": component_id, **components[component_id]} for component_id in sorted(components)
    ]
    conversion_components = []
    for component_id in sorted(components):
        target_fraction = float(components[component_id]["target_nonvolatile_volume_fraction"])
        actual_fraction = volumes[component_id] / total_volume
        conversion_components.append(
            {
                "component_id": component_id,
                "nonvolatile_volume_ml": volumes[component_id],
                "actual_nonvolatile_volume_fraction": actual_fraction,
                "target_nonvolatile_volume_fraction": target_fraction,
                "target_deviation_fraction": actual_fraction - target_fraction,
            }
        )
    return {
        "formula_id": _text(raw["formula_id"], f"{path}.formula_id"),
        "formula_batch_id": _text(raw["formula_batch_id"], f"{path}.formula_batch_id"),
        "formula_stage": "four_card_diagnostic",
        "conversion_route": route,
        "components": normalized_components,
        "conversion_result": {
            "derivation": "software_derived_from_actual_weighing",
            "target_deviation_status": TARGET_DEVIATION_STATUS,
            "total_nonvolatile_volume_ml": total_volume,
            "components": conversion_components,
        },
    }


def _normalize_dft_region(value: object, path: str, band: Mapping[str, float]) -> dict[str, Any]:
    raw = _mapping(value, path)
    fields = (
        "dft_method",
        "dft_instrument_id",
        "dft_verification_id",
        "dft_measured_at",
        "dft_record_evidence",
        "locations",
    )
    _exact_fields(raw, path, fields)
    locations = _list(raw["locations"], f"{path}.locations")
    if len(locations) < 3:
        _fail("DFT_LOCATION_COUNT", f"{path}.locations", "must contain at least three mapped DFT readings")
    normalized_locations: list[dict[str, Any]] = []
    seen_locations: set[str] = set()
    for index, item in enumerate(locations):
        location = _mapping(item, f"{path}.locations[{index}]")
        _exact_fields(location, f"{path}.locations[{index}]", ("location_id", "dft_um"))
        location_id = _text(location["location_id"], f"{path}.locations[{index}].location_id")
        if location_id in seen_locations:
            _fail("DFT_LOCATION_DUPLICATE", f"{path}.locations[{index}].location_id", "must be unique")
        seen_locations.add(location_id)
        normalized_locations.append({"location_id": location_id, "dft_um": _number(location["dft_um"], f"{path}.locations[{index}].dft_um", positive=True)})
    values = [item["dft_um"] for item in normalized_locations]
    mean = statistics.fmean(values)
    spread = statistics.stdev(values)
    if not band["acceptance_min_um"] <= mean <= band["acceptance_max_um"]:
        _fail("DFT_BAND", path, "computed DFT mean is outside the pre-registered acceptance range")
    return {
        "dft_method": _text(raw["dft_method"], f"{path}.dft_method"),
        "dft_instrument_id": _text(raw["dft_instrument_id"], f"{path}.dft_instrument_id"),
        "dft_verification_id": _text(raw["dft_verification_id"], f"{path}.dft_verification_id"),
        "dft_measured_at": _timestamp(raw["dft_measured_at"], f"{path}.dft_measured_at"),
        "dft_record_evidence": _normalize_evidence_locator(raw["dft_record_evidence"], f"{path}.dft_record_evidence"),
        "locations": sorted(normalized_locations, key=lambda item: item["location_id"]),
        "mean_um": mean,
        "sd_um": spread,
    }


def _normalize_cards(
    value: object,
    dft_bands: Mapping[str, Mapping[str, float]],
    materials: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    raw_cards = _list(value, "cards")
    if len(raw_cards) != len(CARD_ROSTER):
        _fail("CARDINALITY", "cards", "must contain exactly the four fixed diagnostic cards")
    cards: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw_cards):
        raw = _mapping(item, f"cards[{index}]")
        fields = ("card_id", "formula_family_id", "dft_band", "formula", "dft_by_backing")
        _exact_fields(raw, f"cards[{index}]", fields)
        card_id = _text(raw["card_id"], f"cards[{index}].card_id")
        if card_id not in _CARD_BY_ID:
            _fail("UNKNOWN_CARD", f"cards[{index}].card_id", "is not one of the four fixed diagnostic cards")
        if card_id in cards:
            _fail("CARD_DUPLICATE", f"cards[{index}].card_id", "must be unique")
        _card_id, expected_family, expected_band, expected_components = _CARD_BY_ID[card_id]
        family = _text(raw["formula_family_id"], f"cards[{index}].formula_family_id")
        if family != expected_family:
            _fail("CARD_FAMILY", f"cards[{index}].formula_family_id", f"must be {expected_family}")
        band_name = _text(raw["dft_band"], f"cards[{index}].dft_band")
        if band_name != expected_band:
            _fail("CARD_DFT_BAND", f"cards[{index}].dft_band", f"must be {expected_band}")
        dft_by_backing = _mapping(raw["dft_by_backing"], f"cards[{index}].dft_by_backing")
        _exact_fields(dft_by_backing, f"cards[{index}].dft_by_backing", BACKINGS)
        material_lots = {
            material["component_id"]: material["batch_id"] for material in materials.values()
        }
        cards[card_id] = {
            "card_id": card_id,
            "formula_family_id": family,
            "dft_band": band_name,
            "formula": _normalize_formula(
                raw["formula"],
                f"cards[{index}].formula",
                expected_components,
                material_lots,
            ),
            "dft_by_backing": {
                backing: _normalize_dft_region(dft_by_backing[backing], f"cards[{index}].dft_by_backing.{backing}", dft_bands[band_name])
                for backing in BACKINGS
            },
        }
    if set(cards) != set(_CARD_BY_ID):
        _fail("CARD_ROSTER", "cards", f"must contain exactly {list(_CARD_BY_ID)}")
    return [cards[card_id] for card_id, *_rest in CARD_ROSTER]


def _normalize_backings(
    value: object,
    source_wavelength_nm: list[float],
    canonical_wavelength_nm: list[float],
    scale: str,
) -> dict[str, dict[str, Any]]:
    raw_backings = _mapping(value, "backings")
    _exact_fields(raw_backings, "backings", BACKINGS)
    result: dict[str, dict[str, Any]] = {}
    for backing_name in BACKINGS:
        raw = _mapping(raw_backings[backing_name], f"backings.{backing_name}")
        fields = ("backing_id", "manufacturer", "product", "lot_id", "storage_state", "region_description", "measurements")
        _exact_fields(raw, f"backings.{backing_name}", fields)
        measurements = _list(raw["measurements"], f"backings.{backing_name}.measurements")
        if not measurements:
            _fail("BACKING_EVIDENCE", f"backings.{backing_name}.measurements", "must contain separately measured bare backing spectra")
        normalized_measurements: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(measurements):
            measurement = _mapping(item, f"backings.{backing_name}.measurements[{index}]")
            fields = (
                "instrument_measurement_id",
                "measured_at_local",
                "raw_export_evidence",
                "evidence_class",
                "reflectance",
            )
            _exact_fields(measurement, f"backings.{backing_name}.measurements[{index}]", fields)
            measurement_id = _text(measurement["instrument_measurement_id"], f"backings.{backing_name}.measurements[{index}].instrument_measurement_id")
            if measurement_id in seen_ids:
                _fail("BACKING_MEASUREMENT_DUPLICATE", f"backings.{backing_name}.measurements[{index}].instrument_measurement_id", "must be unique")
            seen_ids.add(measurement_id)
            if measurement["evidence_class"] != "measured_current_batch":
                _fail("EVIDENCE_CLASS", f"backings.{backing_name}.measurements[{index}].evidence_class", "must be measured_current_batch")
            normalized_measurements.append(
                {
                    "instrument_measurement_id": measurement_id,
                    "measured_at_local": _timestamp(measurement["measured_at_local"], f"backings.{backing_name}.measurements[{index}].measured_at_local"),
                    "raw_export_evidence": _normalize_evidence_locator(
                        measurement["raw_export_evidence"],
                        f"backings.{backing_name}.measurements[{index}].raw_export_evidence",
                    ),
                    "evidence_class": "measured_current_batch",
                    "reflectance": _reorder_reflectance(
                        _normalize_reflectance(
                            measurement["reflectance"],
                            f"backings.{backing_name}.measurements[{index}].reflectance",
                            scale,
                            len(source_wavelength_nm),
                        ),
                        source_wavelength_nm,
                        canonical_wavelength_nm,
                    ),
                }
            )
        result[backing_name] = {
            "backing_id": _text(raw["backing_id"], f"backings.{backing_name}.backing_id"),
            "manufacturer": _text(raw["manufacturer"], f"backings.{backing_name}.manufacturer"),
            "product": _text(raw["product"], f"backings.{backing_name}.product"),
            "lot_id": _text(raw["lot_id"], f"backings.{backing_name}.lot_id"),
            "storage_state": _text(raw["storage_state"], f"backings.{backing_name}.storage_state"),
            "region_description": _text(raw["region_description"], f"backings.{backing_name}.region_description"),
            "measurements": sorted(normalized_measurements, key=lambda item: item["instrument_measurement_id"]),
        }
    return result


def _normalize_reading_metadata(value: object, path: str, conditions_sha256: str, backings: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    raw = _mapping(value, path)
    fields = (
        "card_id",
        "backing",
        "reposition_id",
        "instrument_measurement_id",
        "position_note",
        "orientation",
        "measured_at_local",
        "raw_spectrum_evidence",
        "evidence_class",
        "surface_status",
        "model_applicability_status",
        "backing_id",
        "backing_lot_id",
    )
    _exact_fields(raw, path, fields)
    card_id = _text(raw["card_id"], f"{path}.card_id")
    if card_id not in _CARD_BY_ID:
        _fail("UNKNOWN_CARD", f"{path}.card_id", "is not one of the four fixed diagnostic cards")
    backing = _text(raw["backing"], f"{path}.backing")
    if backing not in BACKINGS:
        _fail("BACKING", f"{path}.backing", "must be black or white")
    reposition_id = _text(raw["reposition_id"], f"{path}.reposition_id")
    if reposition_id not in POSITIONS:
        _fail("REPOSITION", f"{path}.reposition_id", "must be POS01, POS02, or POS03")
    if raw["evidence_class"] != "measured_current_batch":
        _fail("EVIDENCE_CLASS", f"{path}.evidence_class", "must be measured_current_batch")
    if _text(raw["backing_id"], f"{path}.backing_id") != backings[backing]["backing_id"]:
        _fail("BACKING_MISMATCH", f"{path}.backing_id", "does not match the declared backing")
    if _text(raw["backing_lot_id"], f"{path}.backing_lot_id") != backings[backing]["lot_id"]:
        _fail("BACKING_MISMATCH", f"{path}.backing_lot_id", "does not match the declared backing lot")
    return {
        "card_id": card_id,
        "backing": backing,
        "reposition_id": reposition_id,
        "instrument_measurement_id": _text(raw["instrument_measurement_id"], f"{path}.instrument_measurement_id"),
        "position_note": _text(raw["position_note"], f"{path}.position_note"),
        "orientation": _text(raw["orientation"], f"{path}.orientation"),
        "measured_at_local": _timestamp(raw["measured_at_local"], f"{path}.measured_at_local"),
        "raw_spectrum_evidence": _normalize_evidence_locator(
            raw["raw_spectrum_evidence"], f"{path}.raw_spectrum_evidence"
        ),
        "evidence_class": "measured_current_batch",
        "surface_status": _text(raw["surface_status"], f"{path}.surface_status"),
        "model_applicability_status": _text(raw["model_applicability_status"], f"{path}.model_applicability_status"),
        "locked_conditions_sha256": conditions_sha256,
        "backing_id": backings[backing]["backing_id"],
        "backing_lot_id": backings[backing]["lot_id"],
    }


def _reading_key(reading: Mapping[str, Any]) -> tuple[int, int, int]:
    return (_CARD_INDEX[reading["card_id"]], _BACKING_INDEX[reading["backing"]], _POSITION_INDEX[reading["reposition_id"]])


def _reorder_reflectance(
    reflectance: Sequence[float], source_wavelength_nm: Sequence[float], canonical_wavelength_nm: Sequence[float]
) -> list[float]:
    source_index = {wavelength: index for index, wavelength in enumerate(source_wavelength_nm)}
    return [reflectance[source_index[wavelength]] for wavelength in canonical_wavelength_nm]


def _normalize_readings_from_json(
    value: object,
    source_wavelength_nm: list[float],
    canonical_wavelength_nm: list[float],
    scale: str,
    conditions_sha256: str,
    backings: Mapping[str, Mapping[str, Any]],
    cards: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    raw_readings = _list(value, "readings")
    if len(raw_readings) != 24:
        if len(raw_readings) >= 270:
            _fail("PILOT_ISOLATION", "readings", "a 45-card pilot requires the later pilot importer")
        _fail("READING_CARDINALITY", "readings", f"must contain exactly 24 records, got {len(raw_readings)}")
    readings: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int]] = set()
    for index, item in enumerate(raw_readings):
        raw = _mapping(item, f"readings[{index}]")
        fields = (
            "card_id",
            "backing",
            "reposition_id",
            "instrument_measurement_id",
            "position_note",
            "orientation",
            "measured_at_local",
            "raw_spectrum_evidence",
            "evidence_class",
            "surface_status",
            "model_applicability_status",
            "backing_id",
            "backing_lot_id",
            "reflectance",
        )
        _exact_fields(raw, f"readings[{index}]", fields)
        metadata = _normalize_reading_metadata(
            {name: item for name, item in raw.items() if name != "reflectance"},
            f"readings[{index}]",
            conditions_sha256,
            backings,
        )
        key = _reading_key(metadata)
        if key in seen:
            _fail("READING_DUPLICATE", f"readings[{index}]", "duplicates a card/backing/reposition record")
        seen.add(key)
        source_reflectance = _normalize_reflectance(
            raw["reflectance"], f"readings[{index}].reflectance", scale, len(source_wavelength_nm)
        )
        metadata["reflectance"] = _reorder_reflectance(
            source_reflectance, source_wavelength_nm, canonical_wavelength_nm
        )
        metadata["dft_um"] = cards[metadata["card_id"]]["dft_by_backing"][metadata["backing"]]["mean_um"]
        readings.append(metadata)
    return _validate_roster(readings)


def _validate_roster(readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = {
        (card_id, backing, position)
        for card_id, *_rest in CARD_ROSTER
        for backing in BACKINGS
        for position in POSITIONS
    }
    observed = {(item["card_id"], item["backing"], item["reposition_id"]) for item in readings}
    if len(readings) != 24:
        if len(readings) > 24:
            _fail("PILOT_ISOLATION", "readings", "must contain exactly 24 records; a 45-card pilot requires the later pilot importer")
        _fail("READING_CARDINALITY", "readings", f"must contain exactly 24 records, got {len(readings)}")
    missing = sorted(expected - observed)
    extra = sorted(observed - expected)
    if missing or extra:
        _fail("READING_ROSTER", "readings", f"missing={missing}, extra={extra}")
    return sorted(readings, key=_reading_key)


def _validate_formula_provenance(cards: Sequence[Mapping[str, Any]]) -> None:
    bindings: dict[str, dict[str, bytes]] = {
        field: {} for field in ("formula_id", "formula_batch_id")
    }
    weighing_events: dict[str, bytes] = {}
    component_properties: dict[tuple[str, str, str, str], bytes] = {}
    for index, card in enumerate(cards):
        formula = card["formula"]
        signature = canonical_json_bytes(
            {
                "formula_family_id": card["formula_family_id"],
                "formula_id": formula["formula_id"],
                "formula_batch_id": formula["formula_batch_id"],
                "formula_stage": formula["formula_stage"],
                "conversion_route": formula["conversion_route"],
                "components": formula["components"],
            }
        )
        for field, values in bindings.items():
            identifier = formula[field]
            existing = values.setdefault(identifier, signature)
            if existing != signature:
                _fail(
                    "FORMULA_PROVENANCE",
                    f"cards[{index}].formula.{field}",
                    "must bind one formula family and complete composition/evidence tuple",
                )
        for component in formula["components"]:
            actual_weighing = component["actual_weighing"]
            event_signature = canonical_json_bytes(
                {
                    "formula_batch_id": formula["formula_batch_id"],
                    "component_id": component["component_id"],
                    "physical_lot_id": component["physical_lot_id"],
                    "actual_weighing": actual_weighing,
                }
            )
            existing_event = weighing_events.setdefault(
                actual_weighing["weighing_event_id"], event_signature
            )
            if existing_event != event_signature:
                _fail(
                    "FORMULA_WEIGHING_PROVENANCE",
                    f"cards[{index}].formula.components.{component['component_id']}.actual_weighing.weighing_event_id",
                    "must bind one formula batch, component, physical lot, mass, and evidence locator",
                )
            for property_name, property_record in component["property_records"].items():
                key = (
                    component["component_id"],
                    component["physical_lot_id"],
                    formula["conversion_route"],
                    property_name,
                )
                property_signature = canonical_json_bytes(property_record)
                existing_properties = component_properties.setdefault(key, property_signature)
                if existing_properties != property_signature:
                    _fail(
                        "FORMULA_COMPONENT_PROPERTIES",
                        f"cards[{index}].formula.components.{component['component_id']}.property_records.{property_name}",
                        "must keep each conversion property record consistent for the current component lot and route",
                    )


def _validate_measurement_integrity(payload: Mapping[str, Any]) -> None:
    conditions = payload["locked_conditions"]
    calibration_time = _timestamp_value(
        conditions["instrument_calibration_timestamp"], "locked_conditions.instrument_calibration_timestamp"
    )
    cure_start = _timestamp_value(conditions["cure_start"], "locked_conditions.cure_start")
    cure_end = _timestamp_value(conditions["cure_end"], "locked_conditions.cure_end")
    if cure_end <= cure_start:
        _fail("CURE_TIMELINE", "locked_conditions", "cure_end must be later than cure_start")

    for card_index, card in enumerate(payload["cards"]):
        for component_index, component in enumerate(card["formula"]["components"]):
            path = (
                f"cards[{card_index}].formula.components[{component_index}]"
                ".actual_weighing.weighed_at"
            )
            weighed_at = _timestamp_value(component["actual_weighing"]["weighed_at"], path)
            if weighed_at > cure_start:
                _fail(
                    "WEIGHING_CURE_TIMELINE",
                    path,
                    "must be no later than locked_conditions.cure_start",
                )

    measurement_ids: dict[str, str] = {}
    def register(identifier: str, path: str) -> None:
        existing_id = measurement_ids.setdefault(identifier, path)
        if existing_id != path:
            _fail("INSTRUMENT_MEASUREMENT_DUPLICATE", path, f"duplicates instrument measurement identity at {existing_id}")

    for backing in BACKINGS:
        measurements = payload["backings"][backing]["measurements"]
        if len(measurements) < 3:
            _fail(
                "BACKING_MEASUREMENT_COUNT",
                f"backings.{backing}.measurements",
                "must contain at least three independently identified bare backing measurements",
            )
        for index, measurement in enumerate(measurements):
            path = f"backings.{backing}.measurements[{index}]"
            if _timestamp_value(measurement["measured_at_local"], f"{path}.measured_at_local") <= calibration_time:
                _fail("CALIBRATION_TIMELINE", f"{path}.measured_at_local", "must be after instrument calibration")
            register(measurement["instrument_measurement_id"], path)

    for index, reading in enumerate(payload["readings"]):
        path = f"readings[{index}]"
        measured_at = _timestamp_value(reading["measured_at_local"], f"{path}.measured_at_local")
        if measured_at <= calibration_time:
            _fail("CALIBRATION_TIMELINE", f"{path}.measured_at_local", "must be after instrument calibration")
        if measured_at <= cure_end:
            _fail("COATED_CURE_TIMELINE", f"{path}.measured_at_local", "must be after cure_end")
        register(reading["instrument_measurement_id"], path)

    backing_means = {
        backing: [
            statistics.fmean(values)
            for values in zip(*(measurement["reflectance"] for measurement in payload["backings"][backing]["measurements"]))
        ]
        for backing in BACKINGS
    }
    if backing_means["black"] == backing_means["white"]:
        _fail("BACKING_SPECTRA_IDENTICAL", "backings", "black and white canonical backing mean spectra must not be exactly equal")


def _normalize_common(
    payload: object, *, csv_mode: bool
) -> tuple[dict[str, Any], list[float], list[float], str, str, dict[str, Any]]:
    _reject_unmeasured_strings(payload)
    raw = _mapping(payload, "diagnostic")
    common_fields = (
        "schema_version",
        "acquisition_status",
        "physical_ranking_enabled",
        "model_fitting_permitted",
        "diagnostic_id",
        "registry_snapshot_evidence",
        "wavelength_nm",
        "locked_conditions",
        "materials",
        "dft_bands",
        "backings",
        "cards",
    )
    expected = common_fields + (("reading_metadata",) if csv_mode else ("readings",))
    _exact_fields(raw, "diagnostic", expected)
    if raw["schema_version"] != DIAGNOSTIC_SCHEMA_VERSION:
        _fail("SCHEMA_VERSION", "schema_version", f"must be {DIAGNOSTIC_SCHEMA_VERSION}")
    if raw["acquisition_status"] != "diagnostic_measured":
        _fail("ACQUISITION_STATUS", "acquisition_status", "must be diagnostic_measured")
    if raw["physical_ranking_enabled"] is not False:
        _fail("RANKING_GATE", "physical_ranking_enabled", "must be false")
    if raw["model_fitting_permitted"] is not False:
        _fail("FITTING_GATE", "model_fitting_permitted", "must be false")
    source_grid = [_number(item, f"wavelength_nm[{index}]", positive=True) for index, item in enumerate(_list(raw["wavelength_nm"], "wavelength_nm"))]
    canonical_grid = _normalize_grid(raw["wavelength_nm"], "wavelength_nm")
    locked_conditions = _normalize_locked_conditions(raw["locked_conditions"], canonical_grid)
    conditions_sha256 = sha256_bytes(canonical_json_bytes(locked_conditions))
    scale = _reflectance_scale(_mapping(raw["locked_conditions"], "locked_conditions")["reflectance_scale"], "locked_conditions.reflectance_scale")
    normalized = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "acquisition_status": "diagnostic_measured",
        "physical_ranking_enabled": False,
        "model_fitting_permitted": False,
        "diagnostic_id": _text(raw["diagnostic_id"], "diagnostic_id"),
        "registry_snapshot_evidence": _normalize_evidence_locator(
            raw["registry_snapshot_evidence"], "registry_snapshot_evidence"
        ),
        "wavelength_nm": canonical_grid,
        "locked_conditions": locked_conditions,
        "materials": _normalize_materials(raw["materials"]),
        "dft_bands": _normalize_dft_bands(raw["dft_bands"]),
    }
    normalized["backings"] = _normalize_backings(raw["backings"], source_grid, canonical_grid, scale)
    normalized["cards"] = _normalize_cards(
        raw["cards"], normalized["dft_bands"], normalized["materials"]
    )
    _validate_formula_provenance(normalized["cards"])
    return normalized, source_grid, canonical_grid, scale, conditions_sha256, raw


def normalize_diagnostic_json(payload: object) -> DiagnosticBundle:
    """Normalize a complete four-card JSON transport into its canonical payload."""
    normalized, source_grid, canonical_grid, scale, conditions_sha256, raw = _normalize_common(payload, csv_mode=False)
    cards = {item["card_id"]: item for item in normalized["cards"]}
    normalized["readings"] = _normalize_readings_from_json(
        raw["readings"], source_grid, canonical_grid, scale, conditions_sha256, normalized["backings"], cards
    )
    _validate_measurement_integrity(normalized)
    canonical = canonical_json_bytes(normalized)
    return DiagnosticBundle(normalized, canonical, sha256_bytes(canonical))


def normalize_diagnostic_csv(manifest: object, csv_rows: Sequence[Mapping[str, str]]) -> DiagnosticBundle:
    """Normalize a long CSV transport plus strict manifest into its canonical payload."""
    normalized, source_grid, canonical_grid, scale, conditions_sha256, raw = _normalize_common(manifest, csv_mode=True)
    cards = {item["card_id"]: item for item in normalized["cards"]}
    metadata_by_key: dict[tuple[int, int, int], dict[str, Any]] = {}
    for index, item in enumerate(_list(raw["reading_metadata"], "reading_metadata")):
        metadata = _normalize_reading_metadata(item, f"reading_metadata[{index}]", conditions_sha256, normalized["backings"])
        key = _reading_key(metadata)
        if key in metadata_by_key:
            _fail("READING_DUPLICATE", f"reading_metadata[{index}]", "duplicates a card/backing/reposition record")
        metadata_by_key[key] = metadata
    if set(metadata_by_key) != {
        (_CARD_INDEX[card_id], _BACKING_INDEX[backing], _POSITION_INDEX[position])
        for card_id, *_rest in CARD_ROSTER
        for backing in BACKINGS
        for position in POSITIONS
    }:
        _fail("READING_ROSTER", "reading_metadata", "must declare the fixed 24-reading roster")
    groups: dict[tuple[int, int, int], dict[str, Any]] = {}
    expected_wavelengths = set(canonical_grid)
    for row_index, row in enumerate(csv_rows, start=2):
        if set(row) != set(CSV_COLUMNS):
            _fail("CSV_HEADER", "csv", "does not have the required long-spectrum columns")
        metadata_stub = {
            "card_id": row["card_id"],
            "backing": row["backing"],
            "reposition_id": row["reposition_id"],
            "instrument_measurement_id": row["instrument_measurement_id"],
            "position_note": row["position_note"],
            "orientation": row["orientation"],
            "measured_at_local": metadata_by_key.get(
                (_CARD_INDEX.get(row["card_id"].strip(), -1), _BACKING_INDEX.get(row["backing"].strip(), -1), _POSITION_INDEX.get(row["reposition_id"].strip(), -1)),
                {},
            ).get("measured_at_local"),
            "raw_spectrum_evidence": metadata_by_key.get(
                (_CARD_INDEX.get(row["card_id"].strip(), -1), _BACKING_INDEX.get(row["backing"].strip(), -1), _POSITION_INDEX.get(row["reposition_id"].strip(), -1)),
                {},
            ).get("raw_spectrum_evidence"),
            "evidence_class": "measured_current_batch",
            "surface_status": metadata_by_key.get(
                (_CARD_INDEX.get(row["card_id"].strip(), -1), _BACKING_INDEX.get(row["backing"].strip(), -1), _POSITION_INDEX.get(row["reposition_id"].strip(), -1)),
                {},
            ).get("surface_status"),
            "model_applicability_status": metadata_by_key.get(
                (_CARD_INDEX.get(row["card_id"].strip(), -1), _BACKING_INDEX.get(row["backing"].strip(), -1), _POSITION_INDEX.get(row["reposition_id"].strip(), -1)),
                {},
            ).get("model_applicability_status"),
            "backing_id": metadata_by_key.get(
                (_CARD_INDEX.get(row["card_id"].strip(), -1), _BACKING_INDEX.get(row["backing"].strip(), -1), _POSITION_INDEX.get(row["reposition_id"].strip(), -1)),
                {},
            ).get("backing_id"),
            "backing_lot_id": metadata_by_key.get(
                (_CARD_INDEX.get(row["card_id"].strip(), -1), _BACKING_INDEX.get(row["backing"].strip(), -1), _POSITION_INDEX.get(row["reposition_id"].strip(), -1)),
                {},
            ).get("backing_lot_id"),
        }
        try:
            key = _reading_key(_normalize_reading_metadata(metadata_stub, f"csv[{row_index}]", conditions_sha256, normalized["backings"]))
        except KeyError:
            _fail("CSV_ROSTER", f"csv[{row_index}]", "contains an unknown card/backing/reposition combination")
        metadata = metadata_by_key[key]
        for name in ("instrument_measurement_id", "position_note", "orientation"):
            if _text(row[name], f"csv[{row_index}].{name}") != metadata[name]:
                _fail("CSV_METADATA", f"csv[{row_index}].{name}", "must match its manifest reading_metadata value")
        wavelength = _number_from_csv(row["wavelength_nm"], f"csv[{row_index}].wavelength_nm", positive=True)
        if wavelength not in expected_wavelengths:
            _fail("GRID_MISMATCH", f"csv[{row_index}].wavelength_nm", "is not in the declared wavelength grid")
        reflectance = _number_from_csv(row["reflectance"], f"csv[{row_index}].reflectance")
        upper = 1.0 if scale == "fraction" else 100.0
        if not 0 <= reflectance <= upper:
            _fail("REFLECTANCE_RANGE", f"csv[{row_index}].reflectance", f"must be in [0, {upper:g}] for {scale} scale")
        group = groups.setdefault(key, {"metadata": metadata, "reflectance": {}})
        if wavelength in group["reflectance"]:
            _fail("GRID_DUPLICATE", f"csv[{row_index}].wavelength_nm", "is duplicated within one reading")
        group["reflectance"][wavelength] = reflectance if scale == "fraction" else reflectance / 100.0
    readings: list[dict[str, Any]] = []
    for key, metadata in metadata_by_key.items():
        if key not in groups:
            _fail("READING_MISSING", "csv", f"is missing rows for {metadata['card_id']}/{metadata['backing']}/{metadata['reposition_id']}")
        group = groups[key]
        if set(group["reflectance"]) != expected_wavelengths:
            missing = sorted(expected_wavelengths - set(group["reflectance"]))
            _fail("GRID_GAP", "csv", f"is missing wavelengths {missing} for {metadata['card_id']}/{metadata['backing']}/{metadata['reposition_id']}")
        reading = dict(metadata)
        reading["reflectance"] = [group["reflectance"][wavelength] for wavelength in canonical_grid]
        reading["dft_um"] = cards[reading["card_id"]]["dft_by_backing"][reading["backing"]]["mean_um"]
        readings.append(reading)
    normalized["readings"] = _validate_roster(readings)
    _validate_measurement_integrity(normalized)
    canonical = canonical_json_bytes(normalized)
    return DiagnosticBundle(normalized, canonical, sha256_bytes(canonical))


def _number_from_csv(value: object, path: str, *, positive: bool = False) -> float:
    text = _text(value, path)
    try:
        parsed = float(text)
    except ValueError:
        _fail("CSV_NUMBER", path, "must use a finite dot-decimal number")
    if not math.isfinite(parsed):
        _fail("FINITE_NUMBER", path, "must be a finite numeric value")
    if positive and parsed <= 0:
        _fail("POSITIVE_NUMBER", path, "must be greater than zero")
    return parsed


def _summary(vectors: Sequence[Sequence[float]]) -> dict[str, Any]:
    count = len(vectors)
    means = [statistics.fmean(row) for row in zip(*vectors)]
    standard_deviations = [statistics.stdev(row) if count > 1 else 0.0 for row in zip(*vectors)]
    return {
        "count": count,
        "mean_reflectance": means,
        "sample_sd_reflectance": standard_deviations,
        "max_sample_sd": max(standard_deviations),
        "threshold_applied": False,
    }


def structural_preflight_four_card(bundle: StructuralDiagnosticBundle) -> PreflightReport:
    """Validate every non-filesystem acquisition gate without claiming evidence readiness."""
    payload = bundle.payload
    for index, reading in enumerate(payload["readings"]):
        if reading["surface_status"] != "accepted_uniform_dry_film":
            _fail(
                "SURFACE_STATUS",
                f"readings[{index}].surface_status",
                "must be accepted_uniform_dry_film for a passing diagnostic",
            )
        if reading["model_applicability_status"] != "accepted_for_km_diagnostic":
            _fail(
                "MODEL_APPLICABILITY_STATUS",
                f"readings[{index}].model_applicability_status",
                "must be accepted_for_km_diagnostic for a passing diagnostic",
            )
    groups: dict[str, list[list[float]]] = {}
    for reading in payload["readings"]:
        group = f"{reading['card_id']}/{reading['backing']}"
        groups.setdefault(group, []).append(reading["reflectance"])
    dft_summary: dict[str, Any] = {}
    cards = {card["card_id"]: card for card in payload["cards"]}
    for family in ("FAM-DX-BASE", "FAM-DX-W064"):
        family_cards = [card for card in payload["cards"] if card["formula_family_id"] == family]
        low = next(card for card in family_cards if card["dft_band"] == "DFT-L")
        high = next(card for card in family_cards if card["dft_band"] == "DFT-H")
        dft_summary[family] = {}
        for backing in BACKINGS:
            low_mean = low["dft_by_backing"][backing]["mean_um"]
            high_mean = high["dft_by_backing"][backing]["mean_um"]
            if high_mean <= low_mean:
                _fail("DFT_ORDER", f"cards.{family}.{backing}", "DFT-H mean must be greater than DFT-L mean")
            dft_summary[family][backing] = {
                "dft_l_mean_um": low_mean,
                "dft_l_sd_um": low["dft_by_backing"][backing]["sd_um"],
                "dft_h_mean_um": high_mean,
                "dft_h_sd_um": high["dft_by_backing"][backing]["sd_um"],
                "dft_h_greater_than_dft_l": True,
            }
    backing_summary = {
        name: _summary([measurement["reflectance"] for measurement in payload["backings"][name]["measurements"]])
        for name in BACKINGS
    }
    report = {
        "schema_version": "moocow-physical-diagnostic-preflight-report-v2",
        "status": "structural_valid",
        "diagnostic_payload_sha256": bundle.structural_sha256,
        "record_coverage": {
            "coated_readings": len(payload["readings"]),
            "expected_coated_readings": 24,
            "bare_backing_measurements": {name: len(payload["backings"][name]["measurements"]) for name in BACKINGS},
        },
        "spectral_repeatability": {name: _summary(vectors) for name, vectors in sorted(groups.items())},
        "bare_backing_repeatability": backing_summary,
        "dft_summary": dft_summary,
        "readiness": {
            "structural_valid": True,
            "evidence_verified": False,
            "current_diagnostic_ready": False,
            "reason": "No evidence root has been cryptographically verified.",
            "pilot_registry_ready": False,
            "pilot_registry_reason": "Four-card diagnostic evidence is not a 45-card/270-reading pilot registry.",
        },
        "gates": {
            "model_fitting_permitted": False,
            "physical_ranking_enabled": False,
            "promotion_permitted": False,
        },
    }
    return PreflightReport(report)


def _collect_evidence_locators(payload: Mapping[str, Any]) -> list[EvidenceUse]:
    """Enumerate only contract-owned evidence fields; arbitrary *_evidence keys are never trusted."""
    uses: list[EvidenceUse] = []

    def add(
        parent: MutableMapping[str, Any],
        field: str,
        logical_path: str,
        evidence_kind: str,
        semantic_context: Mapping[str, Any] | None = None,
    ) -> None:
        if field not in parent:
            _fail("MISSING_FIELD", logical_path, f"missing required evidence field {field}")
        uses.append(EvidenceUse(logical_path, evidence_kind, parent, field, semantic_context))

    root = _mapping(payload, "diagnostic")
    add(root, "registry_snapshot_evidence", "registry_snapshot_evidence", "registry_snapshot")
    conditions = _mapping(root["locked_conditions"], "locked_conditions")
    add(conditions, "instrument_calibration_evidence", "locked_conditions.instrument_calibration_evidence", "instrument_calibration")
    add(conditions, "instrument_run_log_evidence", "locked_conditions.instrument_run_log_evidence", "instrument_run_log")
    materials = _mapping(root["materials"], "materials")
    for name in ("base", "w064"):
        material = _mapping(materials[name], f"materials.{name}")
        add(material, "physical_label_evidence", f"materials.{name}.physical_label_evidence", "physical_label")
    for card_index, card_value in enumerate(_list(root["cards"], "cards")):
        card = _mapping(card_value, f"cards[{card_index}]")
        formula = _mapping(card["formula"], f"cards[{card_index}].formula")
        for component_index, component_value in enumerate(_list(formula["components"], f"cards[{card_index}].formula.components")):
            component = _mapping(component_value, f"cards[{card_index}].formula.components[{component_index}]")
            actual_weighing = _mapping(
                component["actual_weighing"],
                f"cards[{card_index}].formula.components[{component_index}].actual_weighing",
            )
            add(
                actual_weighing,
                "weighing_record_evidence",
                f"cards[{card_index}].formula.components[{component_index}].actual_weighing.weighing_record_evidence",
                "actual_weighing_record",
                {
                    "formula_id": formula["formula_id"],
                    "formula_batch_id": formula["formula_batch_id"],
                },
            )
            property_records = _mapping(
                component["property_records"],
                f"cards[{card_index}].formula.components[{component_index}].property_records",
            )
            for property_name in sorted(property_records):
                property_record = _mapping(
                    property_records[property_name],
                    f"cards[{card_index}].formula.components[{component_index}].property_records.{property_name}",
                )
                add(
                    property_record,
                    "property_record_evidence",
                    f"cards[{card_index}].formula.components[{component_index}].property_records.{property_name}.property_record_evidence",
                    "property_record",
                    {
                        "conversion_route": formula["conversion_route"],
                        "property_name": property_name,
                    },
                )
        dft_by_backing = _mapping(card["dft_by_backing"], f"cards[{card_index}].dft_by_backing")
        for backing in BACKINGS:
            region = _mapping(dft_by_backing[backing], f"cards[{card_index}].dft_by_backing.{backing}")
            add(region, "dft_record_evidence", f"cards[{card_index}].dft_by_backing.{backing}.dft_record_evidence", "dft_record")
    backings = _mapping(root["backings"], "backings")
    for backing in BACKINGS:
        measurements = _list(_mapping(backings[backing], f"backings.{backing}")["measurements"], f"backings.{backing}.measurements")
        for measurement_index, measurement_value in enumerate(measurements):
            measurement = _mapping(measurement_value, f"backings.{backing}.measurements[{measurement_index}]")
            add(measurement, "raw_export_evidence", f"backings.{backing}.measurements[{measurement_index}].raw_export_evidence", "raw_bare_spectrum")
    for reading_index, reading_value in enumerate(_list(root["readings"], "readings")):
        reading = _mapping(reading_value, f"readings[{reading_index}]")
        add(reading, "raw_spectrum_evidence", f"readings[{reading_index}].raw_spectrum_evidence", "raw_coated_spectrum")
    return uses


def _validate_measurement_locator_uniqueness(uses: Sequence[EvidenceUse]) -> None:
    measurement_kinds = {"dft_record", "raw_bare_spectrum", "raw_coated_spectrum"}
    ranges_by_file: dict[str, list[tuple[int, int, str]]] = {}
    for use in uses:
        if use.evidence_kind not in measurement_kinds:
            continue
        binding = _mapping(use.parent[use.field], use.logical_path)
        record = _mapping(binding["record_locator"], f"{use.logical_path}.record_locator")
        file_sha256 = _sha256(binding["file_sha256"], f"{use.logical_path}.file_sha256")
        start = _integer(record["byte_offset"], f"{use.logical_path}.record_locator.byte_offset")
        length = _integer(record["byte_length"], f"{use.logical_path}.record_locator.byte_length", minimum=1)
        end = start + length
        for other_start, other_end, other_path in ranges_by_file.setdefault(file_sha256, []):
            if start == other_start and end == other_end:
                _fail("EVIDENCE_RECORD_DUPLICATE", use.logical_path, f"reuses the exact evidence range bound at {other_path}")
            if start < other_end and other_start < end:
                _fail("EVIDENCE_RECORD_OVERLAP", use.logical_path, f"overlaps evidence range bound at {other_path}")
        ranges_by_file[file_sha256].append((start, end, use.logical_path))


def _load_json_bytes_no_duplicates(data: bytes, path: str) -> object:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail("JSON_DUPLICATE_KEY", path, f"contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs_hook)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("JSON_READ", path, str(error))


def _load_canonical_evidence_json(data: bytes, path: str) -> object:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail("JSON_DUPLICATE_KEY", path, f"contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        _fail("EVIDENCE_JSON_UTF8", path, str(error))
    try:
        return json.loads(text, object_pairs_hook=pairs_hook)
    except json.JSONDecodeError as error:
        _fail("EVIDENCE_JSON_MALFORMED", path, str(error))


def _canonical_record_fields(
    value: Mapping[str, Any], path: str, required: Iterable[str]
) -> None:
    required_fields = set(required)
    actual_fields = set(value)
    missing = sorted(required_fields - actual_fields)
    unexpected = sorted(actual_fields - required_fields - {"operator_note"})
    if missing:
        _fail("MISSING_FIELD", path, f"missing required fields: {', '.join(missing)}")
    if unexpected:
        _fail("UNKNOWN_FIELD", path, f"contains unsupported fields: {', '.join(unexpected)}")
    if "operator_note" in value:
        _text(value["operator_note"], f"{path}.operator_note")


def _evidence_field_match(
    actual: object, expected: object, *, code: str, path: str, field: str
) -> None:
    if actual != expected:
        _fail(code, f"{path}.{field}", "must exactly match the bound manifest or plan field")


def _validate_property_evidence_bytes(
    data: bytes,
    *,
    expected: Mapping[str, Any],
    conversion_route: str,
    property_name: str,
    path: str,
) -> None:
    raw = _mapping(_load_canonical_evidence_json(data, path), path)
    if raw.get("schema_version") != CONVERSION_PROPERTY_RECORD_SCHEMA_VERSION:
        _fail(
            "PROPERTY_EVIDENCE_SCHEMA",
            f"{path}.schema_version",
            f"must be {CONVERSION_PROPERTY_RECORD_SCHEMA_VERSION}",
        )
    if raw.get("record_kind") != "current_lot_conversion_properties":
        _fail(
            "PROPERTY_EVIDENCE_KIND",
            f"{path}.record_kind",
            "must be current_lot_conversion_properties",
        )
    _canonical_record_fields(
        raw,
        path,
        (
            "schema_version",
            "record_kind",
            "conversion_route",
            "component_id",
            "physical_lot_id",
            "properties",
        ),
    )
    canonical_route = _text(raw["conversion_route"], f"{path}.conversion_route")
    _evidence_field_match(
        canonical_route,
        conversion_route,
        code="PROPERTY_EVIDENCE_FIELD_MISMATCH",
        path=path,
        field="conversion_route",
    )
    component_id = _text(raw["component_id"], f"{path}.component_id")
    physical_lot_id = _text(raw["physical_lot_id"], f"{path}.physical_lot_id")
    _evidence_field_match(
        component_id,
        expected["component_id"],
        code="PROPERTY_EVIDENCE_FIELD_MISMATCH",
        path=path,
        field="component_id",
    )
    _evidence_field_match(
        physical_lot_id,
        expected["physical_lot_id"],
        code="PROPERTY_EVIDENCE_FIELD_MISMATCH",
        path=path,
        field="physical_lot_id",
    )
    properties = _mapping(raw["properties"], f"{path}.properties")
    matrix = {name: (unit, fraction) for name, unit, fraction in _PROPERTY_MATRIX[conversion_route]}
    missing = sorted(set(matrix) - set(properties))
    if missing:
        _fail(
            "PROPERTY_EVIDENCE_RECORD_MISSING",
            f"{path}.properties",
            f"missing route property records: {', '.join(missing)}",
        )
    unexpected = sorted(set(properties) - set(matrix))
    if unexpected:
        _fail(
            "PROPERTY_EVIDENCE_RECORD_UNEXPECTED",
            f"{path}.properties",
            f"contains properties outside the exact route map: {', '.join(unexpected)}",
        )
    normalized_properties: dict[str, dict[str, Any]] = {}
    for name, (expected_unit, fraction_value) in matrix.items():
        entry_path = f"{path}.properties.{name}"
        entry = _mapping(properties[name], entry_path)
        _exact_fields(
            entry,
            entry_path,
            ("property_record_id", "value", "unit", "method", "observed_at"),
        )
        unit = _text(entry["unit"], f"{entry_path}.unit")
        if unit != expected_unit:
            _fail(
                "PROPERTY_EVIDENCE_FIELD_MISMATCH",
                f"{entry_path}.unit",
                f"must be exactly {expected_unit}",
            )
        property_value = _number(entry["value"], f"{entry_path}.value", positive=True)
        if fraction_value and property_value > 1:
            _fail(
                "PROPERTY_EVIDENCE_FIELD_MISMATCH",
                f"{entry_path}.value",
                "fraction must be in (0, 1]",
            )
        normalized_properties[name] = {
            "property_record_id": _text(entry["property_record_id"], f"{entry_path}.property_record_id"),
            "value": property_value,
            "unit": unit,
            "method": _text(entry["method"], f"{entry_path}.method"),
            "observed_at": _timestamp(entry["observed_at"], f"{entry_path}.observed_at"),
        }
    if property_name not in normalized_properties:  # pragma: no cover - structural route owns this name.
        _fail("PROPERTY_EVIDENCE_RECORD_MISSING", f"{path}.properties", f"missing {property_name}")
    canonical_property = normalized_properties[property_name]
    for field in ("property_record_id", "value", "unit", "method", "observed_at"):
        _evidence_field_match(
            canonical_property[field],
            expected[field],
            code="PROPERTY_EVIDENCE_FIELD_MISMATCH",
            path=f"{path}.properties.{property_name}",
            field=field,
        )


def _validate_actual_weighing_evidence_bytes(
    data: bytes,
    *,
    expected: Mapping[str, Any],
    formula_id: str,
    formula_batch_id: str,
    path: str,
) -> None:
    raw = _mapping(_load_canonical_evidence_json(data, path), path)
    if raw.get("schema_version") != ACTUAL_WEIGHING_RECORD_SCHEMA_VERSION:
        _fail(
            "WEIGHING_EVIDENCE_SCHEMA",
            f"{path}.schema_version",
            f"must be {ACTUAL_WEIGHING_RECORD_SCHEMA_VERSION}",
        )
    if raw.get("record_kind") != "actual_weighing_observation":
        _fail(
            "WEIGHING_EVIDENCE_KIND",
            f"{path}.record_kind",
            "must be actual_weighing_observation; a weighing plan is never actual evidence",
        )
    _canonical_record_fields(
        raw,
        path,
        ("schema_version", "record_kind", "formula_id", "formula_batch_id", "entries"),
    )
    canonical_formula_id = _text(raw["formula_id"], f"{path}.formula_id")
    canonical_batch_id = _text(raw["formula_batch_id"], f"{path}.formula_batch_id")
    _evidence_field_match(
        canonical_formula_id,
        formula_id,
        code="WEIGHING_EVIDENCE_FIELD_MISMATCH",
        path=path,
        field="formula_id",
    )
    _evidence_field_match(
        canonical_batch_id,
        formula_batch_id,
        code="WEIGHING_EVIDENCE_FIELD_MISMATCH",
        path=path,
        field="formula_batch_id",
    )
    entries = _list(raw["entries"], f"{path}.entries")
    normalized_entries: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(entries):
        entry_path = f"{path}.entries[{index}]"
        entry = _mapping(item, entry_path)
        _exact_fields(
            entry,
            entry_path,
            (
                "weighing_record_id",
                "weighing_event_id",
                "component_id",
                "physical_lot_id",
                "actual_wet_mass_g",
                "actual_wet_mass_unit",
                "weighing_method",
                "weighed_at",
            ),
        )
        event_id = _text(entry["weighing_event_id"], f"{entry_path}.weighing_event_id")
        if event_id in normalized_entries:
            _fail(
                "WEIGHING_EVIDENCE_EVENT_DUPLICATE",
                f"{entry_path}.weighing_event_id",
                "must identify exactly one entry in the canonical weighing record",
            )
        unit = _text(entry["actual_wet_mass_unit"], f"{entry_path}.actual_wet_mass_unit")
        if unit != "g":
            _fail(
                "WEIGHING_EVIDENCE_FIELD_MISMATCH",
                f"{entry_path}.actual_wet_mass_unit",
                "must be exactly g",
            )
        normalized_entries[event_id] = {
            "weighing_record_id": _text(entry["weighing_record_id"], f"{entry_path}.weighing_record_id"),
            "weighing_event_id": event_id,
            "component_id": _text(entry["component_id"], f"{entry_path}.component_id"),
            "physical_lot_id": _text(entry["physical_lot_id"], f"{entry_path}.physical_lot_id"),
            "actual_wet_mass_g": _number(entry["actual_wet_mass_g"], f"{entry_path}.actual_wet_mass_g", positive=True),
            "actual_wet_mass_unit": unit,
            "weighing_method": _text(entry["weighing_method"], f"{entry_path}.weighing_method"),
            "weighed_at": _timestamp(entry["weighed_at"], f"{entry_path}.weighed_at"),
        }
    event_id = expected["weighing_event_id"]
    canonical_entry = normalized_entries.get(event_id)
    if canonical_entry is None:
        _fail(
            "WEIGHING_EVIDENCE_EVENT_MISSING",
            f"{path}.entries",
            f"must contain exactly one entry for weighing_event_id {event_id}",
        )
    for field in (
        "weighing_record_id",
        "weighing_event_id",
        "component_id",
        "physical_lot_id",
        "actual_wet_mass_g",
        "actual_wet_mass_unit",
        "weighing_method",
        "weighed_at",
    ):
        _evidence_field_match(
            canonical_entry[field],
            expected[field],
            code="WEIGHING_EVIDENCE_FIELD_MISMATCH",
            path=f"{path}.entries.{event_id}",
            field=field,
        )


def _validate_registry_snapshot(
    locator: Mapping[str, Any], binding: Mapping[str, Any], *, root: Path, path: str
) -> dict[str, Mapping[str, Any]]:
    record = _mapping(locator["record_locator"], f"{path}.record_locator")
    if record.get("kind") != "whole_file":
        _fail("REGISTRY_SNAPSHOT", f"{path}.record_locator.kind", "must be whole_file so the parsed registry is fully bound")
    candidate = _resolve_evidence_file(locator, root=root, path=path)
    try:
        data = candidate.read_bytes()
    except OSError as error:
        _fail("EVIDENCE_FILE", f"{path}.relative_path", str(error))
    if sha256_bytes(data) != binding["file_sha256"]:
        _fail("EVIDENCE_FILE_HASH", path, "registry snapshot changed during verification")
    registry = _mapping(_load_json_bytes_no_duplicates(data, path), path)
    if registry.get("schema_version") != "moocow-current-batch-component-registry-v1":
        _fail("REGISTRY_SCHEMA", path, "must use moocow-current-batch-component-registry-v1")
    components = _list(registry.get("components"), f"{path}.components")
    components_by_id: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(components):
        component = _mapping(item, f"{path}.components[{index}]")
        component_id = _text(component.get("component_id"), f"{path}.components[{index}].component_id")
        if component_id in components_by_id:
            _fail(
                "REGISTRY_COMPONENT_DUPLICATE",
                f"{path}.components[{index}].component_id",
                "must not duplicate a registry component",
            )
        components_by_id[component_id] = component
    required = {"base-waterborne-clear", "colorant-W064"}
    if not required <= set(components_by_id):
        _fail("REGISTRY", path, f"must declare {sorted(required)}")
    return components_by_id


def _verified_registry_lots(
    registry_components: Mapping[str, Mapping[str, Any]],
    component_ids: Iterable[str],
    *,
    path: str,
) -> dict[str, str]:
    lots: dict[str, str] = {}
    for component_id in sorted(set(component_ids)):
        component = registry_components.get(component_id)
        if component is None:
            _fail("REGISTRY", path, f"does not declare required component {component_id}")
        status = component.get("lot_verification_status")
        if status != "verified_physical_label":
            _fail(
                "REGISTRY_LOT_VERIFICATION",
                f"{path}.components.{component_id}.lot_verification_status",
                "must be verified_physical_label in the bound registry snapshot",
            )
        batch_id = component.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip() or batch_id.upper().startswith(("REQUIRED_", "TEMPLATE_")):
            _fail(
                "REGISTRY_PHYSICAL_LOT",
                f"{path}.components.{component_id}.batch_id",
                "must contain the physical-label-verified lot; no catalog or invented fallback is allowed",
            )
        lots[component_id] = batch_id.strip()
    return lots


def _validate_diagnostic_registry_lots(
    payload: Mapping[str, Any], registry_components: Mapping[str, Mapping[str, Any]]
) -> None:
    material_by_component = {
        material["component_id"]: material
        for material in _mapping(payload["materials"], "materials").values()
    }
    registry_lots = _verified_registry_lots(
        registry_components,
        material_by_component,
        path="registry_snapshot_evidence",
    )
    for component_id, material in material_by_component.items():
        if material["batch_id"] != registry_lots[component_id]:
            _fail(
                "REGISTRY_LOT_MISMATCH",
                f"materials.{component_id}.batch_id",
                "must match the component/physical lot in the verified registry snapshot",
            )


def _normalize_weighing_plan_input(payload: object) -> dict[str, Any]:
    _reject_unmeasured_strings(payload)
    raw = _mapping(payload, "weighing_plan_input")
    fields = (
        "schema_version",
        "plan_id",
        "formula_family_id",
        "formula_id",
        "formula_batch_id",
        "formula_stage",
        "conversion_route",
        "planned_total_nonvolatile_volume_ml",
        "planned_total_nonvolatile_volume_unit",
        "registry_snapshot_evidence",
        "components",
    )
    _exact_fields(raw, "weighing_plan_input", fields)
    if raw["schema_version"] != WEIGHING_PLAN_INPUT_SCHEMA_VERSION:
        _fail(
            "WEIGHING_PLAN_SCHEMA",
            "weighing_plan_input.schema_version",
            f"must be {WEIGHING_PLAN_INPUT_SCHEMA_VERSION}",
        )
    if raw["formula_stage"] != "four_card_diagnostic":
        _fail(
            "FORMULA_STAGE",
            "weighing_plan_input.formula_stage",
            "must be four_card_diagnostic; pilot plans use a separate contract",
        )
    route = _conversion_route(raw["conversion_route"], "weighing_plan_input.conversion_route")
    family = _text(raw["formula_family_id"], "weighing_plan_input.formula_family_id")
    expected_components = _FAMILY_TARGETS.get(family)
    if expected_components is None:
        _fail(
            "FORMULA_FAMILY",
            "weighing_plan_input.formula_family_id",
            f"must be one of {sorted(_FAMILY_TARGETS)}",
        )
    expected = dict(expected_components)
    raw_components = _list(raw["components"], "weighing_plan_input.components")
    components: dict[str, dict[str, Any]] = {}
    for index, raw_component in enumerate(raw_components):
        component_path = f"weighing_plan_input.components[{index}]"
        component = _mapping(raw_component, component_path)
        _exact_fields(
            component,
            component_path,
            (
                "component_id",
                "physical_lot_id",
                "target_nonvolatile_volume_fraction",
                "property_records",
            ),
        )
        component_id = _text(component["component_id"], f"{component_path}.component_id")
        if component_id in components:
            _fail("FORMULA_DUPLICATE_COMPONENT", f"{component_path}.component_id", "must not repeat a component")
        if component_id not in expected:
            _fail("FORMULA_COMPONENTS", f"{component_path}.component_id", f"must be one of {sorted(expected)}")
        physical_lot_id = _text(component["physical_lot_id"], f"{component_path}.physical_lot_id")
        target_fraction = _number(
            component["target_nonvolatile_volume_fraction"],
            f"{component_path}.target_nonvolatile_volume_fraction",
            positive=True,
        )
        if target_fraction > 1:
            _fail("FORMULA_TARGET", f"{component_path}.target_nonvolatile_volume_fraction", "must be in (0, 1]")
        if not math.isclose(
            target_fraction,
            expected[component_id],
            rel_tol=0.0,
            abs_tol=_COMPUTATIONAL_ABS_TOLERANCE,
        ):
            _fail(
                "FORMULA_ROSTER",
                f"{component_path}.target_nonvolatile_volume_fraction",
                f"must be the fixed target {expected[component_id]:g} for {family}",
            )
        components[component_id] = {
            "physical_lot_id": physical_lot_id,
            "target_nonvolatile_volume_fraction": target_fraction,
            "property_records": _normalize_property_records(
                component["property_records"],
                f"{component_path}.property_records",
                route=route,
                component_id=component_id,
                physical_lot_id=physical_lot_id,
            ),
        }
    if set(components) != set(expected):
        _fail(
            "FORMULA_COMPONENTS",
            "weighing_plan_input.components",
            f"must contain exactly {sorted(expected)}",
        )
    target_sum = math.fsum(item["target_nonvolatile_volume_fraction"] for item in components.values())
    if not math.isclose(target_sum, 1.0, rel_tol=0.0, abs_tol=_COMPUTATIONAL_ABS_TOLERANCE):
        _fail("FORMULA_TARGET_SUM", "weighing_plan_input.components", "fixed target fractions must sum to 1")
    planned_unit = _text(
        raw["planned_total_nonvolatile_volume_unit"],
        "weighing_plan_input.planned_total_nonvolatile_volume_unit",
    )
    if planned_unit != "mL":
        _fail(
            "WEIGHING_PLAN_UNIT",
            "weighing_plan_input.planned_total_nonvolatile_volume_unit",
            "must be exactly mL",
        )
    return {
        "schema_version": WEIGHING_PLAN_INPUT_SCHEMA_VERSION,
        "plan_id": _text(raw["plan_id"], "weighing_plan_input.plan_id"),
        "formula_family_id": family,
        "formula_id": _text(raw["formula_id"], "weighing_plan_input.formula_id"),
        "formula_batch_id": _text(raw["formula_batch_id"], "weighing_plan_input.formula_batch_id"),
        "formula_stage": "four_card_diagnostic",
        "conversion_route": route,
        "planned_total_nonvolatile_volume_ml": _number(
            raw["planned_total_nonvolatile_volume_ml"],
            "weighing_plan_input.planned_total_nonvolatile_volume_ml",
            positive=True,
        ),
        "planned_total_nonvolatile_volume_unit": "mL",
        "registry_snapshot_evidence": _normalize_evidence_locator(
            raw["registry_snapshot_evidence"], "weighing_plan_input.registry_snapshot_evidence"
        ),
        "components": [
            {"component_id": component_id, **components[component_id]}
            for component_id in sorted(components)
        ],
    }


def generate_weighing_plan(payload: object, *, evidence_root: Path | str) -> dict[str, Any]:
    """Generate target wet masses from a bound current-lot property route.

    The returned artifact is planning data only. It deliberately has no actual
    weighing fields and cannot satisfy the diagnostic actual-weighing schema.
    """
    normalized = _normalize_weighing_plan_input(payload)
    root = _resolve_evidence_root(evidence_root)
    materialized = copy.deepcopy(normalized)
    file_cache: dict[str, tuple[int, int, str]] = {}
    files: dict[tuple[str, str], dict[str, Any]] = {}
    records: list[dict[str, Any]] = []

    def bind(
        parent: MutableMapping[str, Any], field: str, logical_path: str
    ) -> tuple[dict[str, Any], dict[str, Any], bytes]:
        locator = _normalize_evidence_locator(parent[field], logical_path)
        binding, record_bytes = _materialize_evidence_record(
            locator, root=root, path=logical_path, file_cache=file_cache
        )
        parent[field] = binding
        files[(binding["relative_path"], binding["file_sha256"])] = {
            "relative_path": binding["relative_path"],
            "file_sha256": binding["file_sha256"],
            "size_bytes": binding["size_bytes"],
        }
        record = binding["record_locator"]
        records.append(
            {
                "logical_path": logical_path,
                "relative_path": binding["relative_path"],
                "byte_offset": record["byte_offset"],
                "byte_length": record["byte_length"],
                "record_sha256": record["record_sha256"],
            }
        )
        return locator, binding, record_bytes

    registry_locator, registry_binding, _registry_bytes = bind(
        materialized,
        "registry_snapshot_evidence",
        "weighing_plan_input.registry_snapshot_evidence",
    )
    for component_index, component in enumerate(materialized["components"]):
        for property_name in sorted(component["property_records"]):
            property_record = component["property_records"][property_name]
            logical_path = (
                f"weighing_plan_input.components[{component_index}]"
                f".property_records.{property_name}.property_record_evidence"
            )
            _locator_value, _binding, record_bytes = bind(
                property_record,
                "property_record_evidence",
                logical_path,
            )
            _validate_property_evidence_bytes(
                record_bytes,
                expected=property_record,
                conversion_route=materialized["conversion_route"],
                property_name=property_name,
                path=logical_path,
            )
    registry_components = _validate_registry_snapshot(
        registry_locator,
        registry_binding,
        root=root,
        path="weighing_plan_input.registry_snapshot_evidence",
    )
    registry_lots = _verified_registry_lots(
        registry_components,
        (component["component_id"] for component in materialized["components"]),
        path="weighing_plan_input.registry_snapshot_evidence",
    )
    for component_index, component in enumerate(materialized["components"]):
        if component["physical_lot_id"] != registry_lots[component["component_id"]]:
            _fail(
                "REGISTRY_LOT_MISMATCH",
                f"weighing_plan_input.components[{component_index}].physical_lot_id",
                "must match the component/physical lot in the verified registry snapshot",
            )
    verification = {
        "schema_version": EVIDENCE_VERIFICATION_SCHEMA_VERSION,
        "files": sorted(files.values(), key=lambda item: (item["relative_path"], item["file_sha256"])),
        "records": sorted(records, key=lambda item: item["logical_path"]),
    }
    verification_sha256 = sha256_bytes(canonical_json_bytes(verification))
    verification["evidence_verification_sha256"] = verification_sha256
    planned_total = float(materialized["planned_total_nonvolatile_volume_ml"])
    plan_components: list[dict[str, Any]] = []
    for component in materialized["components"]:
        target_wet_mass = _planned_wet_mass_g(
            materialized["conversion_route"],
            target_fraction=float(component["target_nonvolatile_volume_fraction"]),
            planned_total_nonvolatile_volume_ml=planned_total,
            property_records=component["property_records"],
        )
        plan_components.append(
            {
                "record_kind": "planned_target_wet_mass_not_actual",
                "component_id": component["component_id"],
                "physical_lot_id": component["physical_lot_id"],
                "target_nonvolatile_volume_fraction": component["target_nonvolatile_volume_fraction"],
                "property_records": component["property_records"],
                "target_wet_mass_g": target_wet_mass,
                "target_wet_mass_unit": "g",
            }
        )
    return {
        "schema_version": WEIGHING_PLAN_SCHEMA_VERSION,
        "plan_status": "planned_not_actual",
        "plan_is_actual_weighing_evidence": False,
        "generated_from_plan_input_sha256": sha256_bytes(canonical_json_bytes(normalized)),
        "plan_id": materialized["plan_id"],
        "formula_family_id": materialized["formula_family_id"],
        "formula_id": materialized["formula_id"],
        "formula_batch_id": materialized["formula_batch_id"],
        "formula_stage": "four_card_diagnostic",
        "conversion_route": materialized["conversion_route"],
        "planned_total_nonvolatile_volume_ml": planned_total,
        "planned_total_nonvolatile_volume_unit": "mL",
        "planned_total_wet_mass_g": math.fsum(
            component["target_wet_mass_g"] for component in plan_components
        ),
        "registry_snapshot_evidence": materialized["registry_snapshot_evidence"],
        "components": plan_components,
        "evidence_verification": verification,
    }


def generate_weighing_plan_from_file(
    *, input_path: Path | str, evidence_root: Path | str, output_path: Path | str
) -> dict[str, Any]:
    source = Path(input_path)
    output = Path(output_path)
    try:
        if source.resolve() == output.resolve():
            _fail("WEIGHING_PLAN_OUTPUT", str(output), "must not overwrite the plan input")
    except OSError as error:
        _fail("WEIGHING_PLAN_OUTPUT", str(output), str(error))
    plan = generate_weighing_plan(
        _load_json_no_duplicates(source), evidence_root=evidence_root
    )
    output_sha256 = write_json_with_sha256(output, plan)
    return {
        "status": "planned_not_actual",
        "plan_is_actual_weighing_evidence": False,
        "output_path": str(output),
        "output_sha256": output_sha256,
        "components": len(plan["components"]),
        "conversion_route": plan["conversion_route"],
    }


def verify_evidence_bindings(
    bundle: StructuralDiagnosticBundle, *, evidence_root: Path | str
) -> EvidenceReadyBundle:
    """Materialize every approved locator under an explicit evidence root."""
    root = _resolve_evidence_root(evidence_root)
    payload = copy.deepcopy(bundle.payload)
    uses = _collect_evidence_locators(payload)
    file_cache: dict[str, tuple[int, int, str]] = {}
    files: dict[tuple[str, str], dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    registry_locator: Mapping[str, Any] | None = None
    registry_binding: Mapping[str, Any] | None = None
    for use in uses:
        locator = _normalize_evidence_locator(use.locator, use.logical_path)
        binding, record_bytes = _materialize_evidence_record(
            locator, root=root, path=use.logical_path, file_cache=file_cache
        )
        use.parent[use.field] = binding
        if use.evidence_kind == "property_record":
            context = _mapping(use.semantic_context, f"{use.logical_path}.semantic_context")
            _validate_property_evidence_bytes(
                record_bytes,
                expected=use.parent,
                conversion_route=context["conversion_route"],
                property_name=context["property_name"],
                path=use.logical_path,
            )
        elif use.evidence_kind == "actual_weighing_record":
            context = _mapping(use.semantic_context, f"{use.logical_path}.semantic_context")
            _validate_actual_weighing_evidence_bytes(
                record_bytes,
                expected=use.parent,
                formula_id=context["formula_id"],
                formula_batch_id=context["formula_batch_id"],
                path=use.logical_path,
            )
        files[(binding["relative_path"], binding["file_sha256"])] = {
            "relative_path": binding["relative_path"],
            "file_sha256": binding["file_sha256"],
            "size_bytes": binding["size_bytes"],
        }
        record = binding["record_locator"]
        records.append(
            {
                "logical_path": use.logical_path,
                "relative_path": binding["relative_path"],
                "byte_offset": record["byte_offset"],
                "byte_length": record["byte_length"],
                "record_sha256": record["record_sha256"],
            }
        )
        if use.evidence_kind == "registry_snapshot":
            registry_locator = locator
            registry_binding = binding
    _validate_measurement_locator_uniqueness(uses)
    if registry_locator is None or registry_binding is None:  # pragma: no cover - collector is schema-owned.
        _fail("REGISTRY", "registry_snapshot_evidence", "is required")
    registry_components = _validate_registry_snapshot(
        registry_locator, registry_binding, root=root, path="registry_snapshot_evidence"
    )
    _validate_diagnostic_registry_lots(payload, registry_components)
    verification = {
        "schema_version": EVIDENCE_VERIFICATION_SCHEMA_VERSION,
        "files": sorted(files.values(), key=lambda item: (item["relative_path"], item["file_sha256"])),
        "records": sorted(records, key=lambda item: item["logical_path"]),
    }
    verification_sha256 = sha256_bytes(canonical_json_bytes(verification))
    verification["evidence_verification_sha256"] = verification_sha256
    canonical = canonical_json_bytes(payload)
    return EvidenceReadyBundle(
        payload=payload,
        canonical_bytes=canonical,
        diagnostic_payload_sha256=sha256_bytes(canonical),
        evidence_verification=verification,
        evidence_verification_sha256=verification_sha256,
    )


def _evidence_ready_report(evidence_bundle: EvidenceReadyBundle) -> PreflightReport:
    evidence_structural_bundle = StructuralDiagnosticBundle(
        evidence_bundle.payload,
        evidence_bundle.canonical_bytes,
        evidence_bundle.diagnostic_payload_sha256,
    )
    report = copy.deepcopy(structural_preflight_four_card(evidence_structural_bundle).payload)
    report["status"] = "evidence_ready"
    report["diagnostic_payload_sha256"] = evidence_bundle.diagnostic_payload_sha256
    report["readiness"] = {
        "structural_valid": True,
        "evidence_verified": True,
        "current_diagnostic_ready": True,
        "pilot_registry_ready": False,
        "pilot_registry_reason": "Four-card diagnostic evidence is not a 45-card/270-reading pilot registry.",
    }
    report["evidence_verification_sha256"] = evidence_bundle.evidence_verification_sha256
    return PreflightReport(report)


def preflight_four_card(
    bundle: StructuralDiagnosticBundle, *, evidence_root: Path | str
) -> PreflightReport:
    """Return evidence_ready only after structural and physical-file verification both pass."""
    structural_preflight_four_card(bundle)
    return _evidence_ready_report(verify_evidence_bindings(bundle, evidence_root=evidence_root))


def _load_json_no_duplicates(path: Path) -> object:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail("JSON_DUPLICATE_KEY", str(path), f"contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs_hook)
    except (OSError, json.JSONDecodeError) as error:
        _fail("JSON_READ", str(path), str(error))


def _read_long_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None:
                _fail("CSV_HEADER", str(path), "is empty")
            normalized_header = [item.strip() for item in header]
            if len(set(normalized_header)) != len(normalized_header):
                _fail("CSV_HEADER_DUPLICATE", str(path), "contains duplicate column names")
            if set(normalized_header) != set(CSV_COLUMNS) or len(normalized_header) != len(CSV_COLUMNS):
                missing = sorted(set(CSV_COLUMNS) - set(normalized_header))
                unknown = sorted(set(normalized_header) - set(CSV_COLUMNS))
                _fail("CSV_HEADER", str(path), f"must contain exactly {CSV_COLUMNS}; missing={missing}, unknown={unknown}")
            rows: list[dict[str, str]] = []
            for row_index, row in enumerate(reader, start=2):
                if len(row) != len(normalized_header):
                    _fail("CSV_ROW", f"{path}:{row_index}", "does not have the same number of columns as the header")
                rows.append(dict(zip(normalized_header, row, strict=True)))
            return rows
    except OSError as error:
        _fail("CSV_READ", str(path), str(error))


def _template_evidence(relative_path: str) -> dict[str, Any]:
    return {"relative_path": relative_path, "record_locator": {"kind": "whole_file"}}


def _template_dft_region() -> dict[str, Any]:
    return {
        "dft_method": "REQUIRED_DFT_METHOD",
        "dft_instrument_id": "REQUIRED_DFT_INSTRUMENT_ID",
        "dft_verification_id": "REQUIRED_DFT_VERIFICATION_ID",
        "dft_measured_at": "REQUIRED_ISO8601_TIMESTAMP",
        "dft_record_evidence": _template_evidence("REQUIRED_DFT_RECORD_PATH"),
        "locations": [
            {"location_id": "REQUIRED_DFT_LOCATION_01", "dft_um": None},
            {"location_id": "REQUIRED_DFT_LOCATION_02", "dft_um": None},
            {"location_id": "REQUIRED_DFT_LOCATION_03", "dft_um": None},
        ],
    }


def _template_property_record(
    component_id: str, property_name: str, unit: str
) -> dict[str, Any]:
    return {
        "property_record_id": f"REQUIRED_{property_name.upper()}_RECORD_ID",
        "component_id": component_id,
        "physical_lot_id": "REQUIRED_PHYSICAL_LOT",
        "value": None,
        "unit": unit,
        "method": f"REQUIRED_{property_name.upper()}_METHOD",
        "observed_at": "REQUIRED_ISO8601_TIMESTAMP",
        "property_record_evidence": _template_evidence(
            f"REQUIRED_{property_name.upper()}_PROPERTY_RECORD_PATH"
        ),
    }


def _template_property_records(component_id: str, route: str) -> dict[str, Any]:
    return {
        property_name: _template_property_record(component_id, property_name, unit)
        for property_name, unit, _fraction in _PROPERTY_MATRIX[route]
    }


def _template_actual_weighing(component_id: str) -> dict[str, Any]:
    return {
        "record_kind": "actual_weighing_observation",
        "weighing_record_id": "REQUIRED_ACTUAL_WEIGHING_RECORD_ID",
        "weighing_event_id": "REQUIRED_COMPONENT_WEIGHING_EVENT_ID",
        "component_id": component_id,
        "physical_lot_id": "REQUIRED_PHYSICAL_LOT",
        "actual_wet_mass_g": None,
        "actual_wet_mass_unit": "g",
        "weighing_method": "REQUIRED_WEIGHING_METHOD",
        "weighed_at": "REQUIRED_ISO8601_TIMESTAMP",
        "weighing_record_evidence": _template_evidence(
            "REQUIRED_ACTUAL_WEIGHING_RECORD_PATH"
        ),
    }


def _template_formula(
    expected_components: tuple[tuple[str, float], ...], route: str
) -> dict[str, Any]:
    return {
        "formula_id": "REQUIRED_FORMULA_ID",
        "formula_batch_id": "REQUIRED_FORMULA_BATCH_ID",
        "formula_stage": "four_card_diagnostic",
        "conversion_route": route,
        "components": [
            {
                "component_id": component_id,
                "physical_lot_id": "REQUIRED_PHYSICAL_LOT",
                "target_nonvolatile_volume_fraction": fraction,
                "actual_weighing": _template_actual_weighing(component_id),
                "property_records": _template_property_records(component_id, route),
            }
            for component_id, fraction in expected_components
        ],
    }


def build_four_card_template(
    registry_relative_path: str = "registry/current-batch-component-registry-v1.json",
    *,
    conversion_route: str = MASS_SOLIDS_NONVOLATILE_DENSITY,
) -> dict[str, Any]:
    """Return the deterministic, deliberately invalid operator-pack manifest."""
    route = _conversion_route(conversion_route, "conversion_route")
    cards = []
    metadata = []
    for card_id, family, band, components in CARD_ROSTER:
        cards.append(
            {
                "card_id": card_id,
                "formula_family_id": family,
                "dft_band": band,
                "formula": _template_formula(components, route),
                "dft_by_backing": {backing: _template_dft_region() for backing in BACKINGS},
            }
        )
        for backing in BACKINGS:
            for position in POSITIONS:
                metadata.append(
                    {
                        "card_id": card_id,
                        "backing": backing,
                        "reposition_id": position,
                        "instrument_measurement_id": "REQUIRED_INSTRUMENT_MEASUREMENT_ID",
                        "position_note": "REQUIRED_REPOSITIONED_LOCATION_NOTE",
                        "orientation": "REQUIRED_SAMPLE_ORIENTATION",
                        "measured_at_local": "REQUIRED_ISO8601_TIMESTAMP",
                        "raw_spectrum_evidence": _template_evidence("REQUIRED_RAW_SPECTRUM_RECORD_PATH"),
                        "evidence_class": "REQUIRED_measured_current_batch",
                        "surface_status": "REQUIRED_SURFACE_STATUS",
                        "model_applicability_status": "REQUIRED_MODEL_APPLICABILITY_STATUS",
                        "backing_id": "REQUIRED_BACKING_ID",
                        "backing_lot_id": "REQUIRED_BACKING_LOT_ID",
                    }
                )
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "acquisition_status": "diagnostic_measured",
        "physical_ranking_enabled": False,
        "model_fitting_permitted": False,
        "diagnostic_id": "REQUIRED_DIAGNOSTIC_RUN_ID",
        "registry_snapshot_evidence": _template_evidence(registry_relative_path),
        "wavelength_nm": [],
        "locked_conditions": {
            "instrument_make_model": "REQUIRED_INSTRUMENT_MAKE_MODEL",
            "instrument_serial_number": "REQUIRED_INSTRUMENT_SERIAL",
            "instrument_software_version": "REQUIRED_SOFTWARE_VERSION",
            "instrument_firmware_version": "REQUIRED_FIRMWARE_VERSION",
            "instrument_calibration_id": "REQUIRED_CALIBRATION_EVENT_ID",
            "instrument_calibration_timestamp": "REQUIRED_ISO8601_TIMESTAMP",
            "instrument_calibration_result": "REQUIRED_CALIBRATION_RESULT",
            "instrument_calibration_evidence": _template_evidence("REQUIRED_INSTRUMENT_CALIBRATION_RECORD_PATH"),
            "instrument_run_log_evidence": _template_evidence("REQUIRED_INSTRUMENT_RUN_LOG_PATH"),
            "white_standard_id": "REQUIRED_WHITE_STANDARD_ID",
            "black_calibration_mode": "REQUIRED_BLACK_CALIBRATION_MODE",
            "measurement_geometry": "REQUIRED_MEASUREMENT_GEOMETRY",
            "aperture_mm": None,
            "specular_condition": "REQUIRED_SPECULAR_CONDITION",
            "uv_setting": "REQUIRED_UV_SETTING",
            "measurement_mode": "REQUIRED_MEASUREMENT_MODE",
            "illuminant": "REQUIRED_ILLUMINANT",
            "observer": "REQUIRED_OBSERVER",
            "wavelength_start_nm": None,
            "wavelength_end_nm": None,
            "wavelength_interval_nm": None,
            "wavelength_unit": "nm",
            "spectral_bandpass_nm": None,
            "reflectance_scale": "fraction",
            "cure_protocol": "REQUIRED_CURE_PROTOCOL",
            "cure_start": "REQUIRED_ISO8601_TIMESTAMP",
            "cure_end": "REQUIRED_ISO8601_TIMESTAMP",
            "age_at_measurement_h": None,
            "cure_temperature_c_observed": None,
            "cure_rh_pct_observed": None,
            "airflow_note": "REQUIRED_AIRFLOW_NOTE",
            "application_method": "REQUIRED_APPLICATION_METHOD",
            "applicator_or_wft_target": "REQUIRED_APPLICATOR_OR_WFT_TARGET",
            "operator_id": "REQUIRED_OPERATOR_ID",
        },
        "materials": {
            name: {
                "component_id": component_id,
                "product_name": "REQUIRED_PHYSICAL_PRODUCT_NAME",
                "manufacturer_or_supplier": "REQUIRED_SUPPLIER",
                "batch_id": "REQUIRED_PHYSICAL_LOT",
                "physical_label_verification_status": "REQUIRED_verified_physical_label",
                "physical_label_verification_id": "REQUIRED_PHYSICAL_LABEL_VERIFICATION_ID",
                "physical_label_verified_at": "REQUIRED_ISO8601_TIMESTAMP",
                "physical_label_evidence": _template_evidence("REQUIRED_PHYSICAL_LABEL_RECORD_PATH"),
            }
            for name, component_id in (("base", "base-waterborne-clear"), ("w064", "colorant-W064"))
        },
        "dft_bands": {
            "DFT-L": {"target_um": None, "acceptance_min_um": None, "acceptance_max_um": None},
            "DFT-H": {"target_um": None, "acceptance_min_um": None, "acceptance_max_um": None},
        },
        "backings": {
            backing: {
                "backing_id": "REQUIRED_BACKING_ID",
                "manufacturer": "REQUIRED_BACKING_MANUFACTURER",
                "product": "REQUIRED_BACKING_PRODUCT",
                "lot_id": "REQUIRED_BACKING_LOT_ID",
                "storage_state": "REQUIRED_BACKING_STORAGE_STATE",
                "region_description": "REQUIRED_REGION_DESCRIPTION",
                "measurements": [],
            }
            for backing in BACKINGS
        },
        "cards": cards,
        "reading_metadata": metadata,
    }


def build_weighing_plan_input_template(
    *,
    formula_family_id: str,
    conversion_route: str,
    registry_relative_path: str = "registry/current-batch-component-registry-v1.json",
) -> dict[str, Any]:
    route = _conversion_route(conversion_route, "conversion_route")
    expected_components = _FAMILY_TARGETS.get(formula_family_id)
    if expected_components is None:
        _fail("FORMULA_FAMILY", "formula_family_id", f"must be one of {sorted(_FAMILY_TARGETS)}")
    return {
        "schema_version": WEIGHING_PLAN_INPUT_SCHEMA_VERSION,
        "plan_id": "REQUIRED_WEIGHING_PLAN_ID",
        "formula_family_id": formula_family_id,
        "formula_id": "REQUIRED_FORMULA_ID",
        "formula_batch_id": "REQUIRED_FORMULA_BATCH_ID",
        "formula_stage": "four_card_diagnostic",
        "conversion_route": route,
        "planned_total_nonvolatile_volume_ml": None,
        "planned_total_nonvolatile_volume_unit": "mL",
        "registry_snapshot_evidence": _template_evidence(registry_relative_path),
        "components": [
            {
                "component_id": component_id,
                "physical_lot_id": "REQUIRED_PHYSICAL_LOT",
                "target_nonvolatile_volume_fraction": fraction,
                "property_records": _template_property_records(component_id, route),
            }
            for component_id, fraction in expected_components
        ],
    }


def build_property_record_template(*, conversion_route: str) -> dict[str, Any]:
    route = _conversion_route(conversion_route, "conversion_route")
    return {
        "schema_version": CONVERSION_PROPERTY_RECORD_SCHEMA_VERSION,
        "record_kind": "current_lot_conversion_properties",
        "conversion_route": route,
        "component_id": "REQUIRED_COMPONENT_ID",
        "physical_lot_id": "REQUIRED_PHYSICAL_LOT",
        "properties": {
            property_name: {
                "property_record_id": f"REQUIRED_{property_name.upper()}_RECORD_ID",
                "value": None,
                "unit": unit,
                "method": f"REQUIRED_{property_name.upper()}_METHOD",
                "observed_at": "REQUIRED_ISO8601_TIMESTAMP",
            }
            for property_name, unit, _fraction in _PROPERTY_MATRIX[route]
        },
        "operator_note": "This completed canonical JSON object is the only accepted semantic property record. Save it under evidence/properties and bind each copied property field to the exact whole-file or byte-range object; arbitrary text evidence is rejected.",
    }


def build_actual_weighing_capture_template() -> dict[str, Any]:
    return {
        "schema_version": ACTUAL_WEIGHING_RECORD_SCHEMA_VERSION,
        "record_kind": "actual_weighing_observation",
        "formula_id": "REQUIRED_FORMULA_ID",
        "formula_batch_id": "REQUIRED_FORMULA_BATCH_ID",
        "entries": [
            {
                "weighing_record_id": "REQUIRED_ACTUAL_WEIGHING_RECORD_ID",
                "weighing_event_id": "REQUIRED_COMPONENT_WEIGHING_EVENT_ID",
                "component_id": "REQUIRED_COMPONENT_ID",
                "physical_lot_id": "REQUIRED_PHYSICAL_LOT",
                "actual_wet_mass_g": None,
                "actual_wet_mass_unit": "g",
                "weighing_method": "REQUIRED_WEIGHING_METHOD",
                "weighed_at": "REQUIRED_ISO8601_TIMESTAMP",
            }
        ],
        "operator_note": "This canonical JSON object is the only accepted semantic actual-weighing record and may contain multiple unique events. Bind each manifest event to this exact whole-file or byte-range object; arbitrary text or a weighing plan is rejected.",
    }


def prepare_four_card(registry_path: Path | str, output_dir: Path | str) -> dict[str, Any]:
    """Create deterministic, explicitly incomplete operator files from a registry."""
    registry_file = Path(registry_path)
    registry = _load_json_no_duplicates(registry_file)
    registry_mapping = _mapping(registry, "registry")
    if registry_mapping.get("schema_version") != "moocow-current-batch-component-registry-v1":
        _fail("REGISTRY_SCHEMA", str(registry_file), "must use moocow-current-batch-component-registry-v1")
    components = _list(registry_mapping.get("components"), "registry.components")
    component_ids = {item.get("component_id") for item in components if isinstance(item, Mapping)}
    required = {"base-waterborne-clear", "colorant-W064"}
    if not required <= component_ids:
        _fail("REGISTRY", str(registry_file), f"must declare {sorted(required)}")
    registry_sha256 = sha256_file(registry_file)
    output = Path(output_dir)
    if output.exists():
        if not output.is_dir():
            _fail("OUTPUT_DIR", str(output), "must be a directory path")
        if any(output.iterdir()):
            _fail(
                "OUTPUT_DIR_NOT_EMPTY",
                str(output),
                "must be empty so legacy or stale operator files cannot survive regeneration",
            )
    output.mkdir(parents=True, exist_ok=True)
    evidence_dir = output / "evidence"
    for relative_directory in ("registry", "labels", "weighing", "properties", "dft", "instrument", "raw"):
        (evidence_dir / relative_directory).mkdir(parents=True, exist_ok=True)
    registry_snapshot = evidence_dir / "registry" / "current-batch-component-registry-v1.json"
    try:
        registry_snapshot.write_bytes(registry_file.read_bytes())
    except OSError as error:
        _fail("REGISTRY", str(registry_file), str(error))
    roster_path = output / "fixed-24-reading-roster.csv"
    spectra_path = output / "spectra-long.template.csv"
    readme_path = output / "OPERATOR_README.md"
    manifest_sha256_by_route: dict[str, str] = {}
    generated_files: list[str] = []
    for route in CONVERSION_ROUTES:
        manifest_path = output / f"diagnostic-manifest.{route}.template.json"
        manifest_sha256_by_route[route] = write_json_with_sha256(
            manifest_path, build_four_card_template(conversion_route=route)
        )
        generated_files.extend([manifest_path.name, f"{manifest_path.name}.sha256"])
        property_path = output / f"property-record.{route}.template.json"
        write_json_with_sha256(
            property_path, build_property_record_template(conversion_route=route)
        )
        generated_files.extend([property_path.name, f"{property_path.name}.sha256"])
        for family, slug in (("FAM-DX-BASE", "base"), ("FAM-DX-W064", "w064")):
            plan_input_path = output / f"weighing-plan-input.{route}.{slug}.template.json"
            write_json_with_sha256(
                plan_input_path,
                build_weighing_plan_input_template(
                    formula_family_id=family, conversion_route=route
                ),
            )
            generated_files.extend(
                [plan_input_path.name, f"{plan_input_path.name}.sha256"]
            )
    actual_weighing_path = output / "actual-weighing-record.template.json"
    write_json_with_sha256(actual_weighing_path, build_actual_weighing_capture_template())
    generated_files.extend(
        [actual_weighing_path.name, f"{actual_weighing_path.name}.sha256"]
    )
    roster_lines = ["card_id,backing,reposition_id"] + [
        f"{card_id},{backing},{position}"
        for card_id, *_rest in CARD_ROSTER
        for backing in BACKINGS
        for position in POSITIONS
    ]
    roster_path.write_text("\n".join(roster_lines) + "\n", encoding="utf-8", newline="\n")
    spectra_path.write_text(",".join(CSV_COLUMNS) + "\n", encoding="utf-8", newline="\n")
    readme_path.write_text(
        "# Four-Card Diagnostic Operator Pack v2\n\n"
        "This pack is deliberately invalid until every REQUIRED_ value is replaced with current physical evidence. "
        "It is a diagnostic-only preflight: it cannot fit a model, rank a formula, or promote a production artifact.\n\n"
        "1. Update the copied registry snapshot from physical labels. Both base-waterborne-clear and colorant-W064 must carry their real batch_id and lot_verification_status=verified_physical_label; do not invent a missing lot.\n"
        "2. Choose exactly one route: mass_solids_nonvolatile_density or wet_density_volume_solids. Use only the matching property, plan-input, and diagnostic-manifest templates. Mixed-route fields are rejected.\n"
        "3. Fill the matching moocow-conversion-property-record-v2 canonical JSON for each current physical lot and save it beneath evidence/properties. Each property locator must select that exact JSON object; arbitrary text files are rejected. The route, component, lot, record ID, value, unit, method, and timezone-aware observed_at must exactly match every copied manifest or plan field.\n"
        "4. Fill one weighing-plan input per formula batch and run generate-weighing-plan. Plan generation re-parses the bound canonical property JSON before calculating masses. The generated target_wet_mass_g values are a plan only, never actual weighing evidence.\n"
        "5. Weigh each component no later than cure_start and save the observed events in a moocow-actual-weighing-record-v2 canonical JSON beneath evidence/weighing. One file may contain multiple unique events, but each manifest locator must select the exact JSON object and every formula, batch, event, record, component, lot, mass, unit, method, and timezone-aware time field must match. Arbitrary text and weighing-plan records are rejected.\n"
        "6. Record measured DFT locations, cure/application conditions, at least three bare spectra per backing, and exactly 24 coated spectra. Do not enter DFT means/SDs, normalized fractions, deviations, or digests; software derives them. Use distinct non-overlapping byte ranges for shared raw or DFT exports.\n"
        "7. Validate structure, run evidence-root preflight, then independently verify the receipt after copying the evidence root. A passing receipt remains diagnostic-only and never enables fitting, ranking, or promotion.\n\n"
        "Example locator command:\n\n"
        "    moocow-km-calibration bind-evidence-record --evidence-root acquisition-pack/evidence --relative-path raw/run.csv --byte-offset 0 --byte-length 120\n\n"
        "Generate a route-specific weighing plan:\n\n"
        "    moocow-km-calibration generate-weighing-plan --input acquisition-pack/weighing-plan-input.mass_solids_nonvolatile_density.w064.template.json --evidence-root acquisition-pack/evidence --output acquisition-pack/weighing-plan.w064.generated.json\n\n"
        "Structural validation command:\n\n"
        "    moocow-km-calibration validate-four-card-structure --format csv --manifest acquisition-pack/diagnostic-manifest.mass_solids_nonvolatile_density.template.json --input acquisition-pack/spectra-long.template.csv\n\n"
        "Required preflight command:\n\n"
        "    moocow-km-calibration preflight-four-card --format csv --manifest acquisition-pack/diagnostic-manifest.mass_solids_nonvolatile_density.template.json --input acquisition-pack/spectra-long.template.csv --evidence-root acquisition-pack/evidence --output-dir preflight-output\n\n"
        "Independent receipt verification after relocating/copying evidence:\n\n"
        "    moocow-km-calibration verify-four-card-receipt --receipt preflight-output/preflight-receipt.json --evidence-root copied-evidence\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "status": "prepared_template_only",
        "output_dir": str(output),
        "registry_sha256": registry_sha256,
        "manifest_sha256_by_route": manifest_sha256_by_route,
        "roster_records": 24,
        "files": sorted(
            generated_files
            + [
                roster_path.name,
                spectra_path.name,
                readme_path.name,
                str(registry_snapshot.relative_to(output)).replace("\\", "/"),
            ]
        ),
    }


def _load_bundle_from_files(
    *, input_format: str, input_path: Path | str, manifest_path: Path | str | None = None
) -> tuple[StructuralDiagnosticBundle, dict[str, Any]]:
    source = Path(input_path)
    if input_format == "json":
        if manifest_path is not None:
            _fail("CLI_ARGUMENT", "--manifest", "is only valid with --format csv")
        return normalize_diagnostic_json(_load_json_no_duplicates(source)), {
            "input_transport": {"path": source.name, "sha256": sha256_file(source)}
        }
    if input_format == "csv":
        if manifest_path is None:
            _fail("CLI_ARGUMENT", "--manifest", "is required with --format csv")
        manifest_file = Path(manifest_path)
        return normalize_diagnostic_csv(_load_json_no_duplicates(manifest_file), _read_long_csv(source)), {
            "input_transport": {"path": source.name, "sha256": sha256_file(source)},
            "manifest_transport": {"path": manifest_file.name, "sha256": sha256_file(manifest_file)},
        }
    _fail("CLI_ARGUMENT", "--format", "must be json or csv")


def validate_structure_from_files(
    *, input_format: str, input_path: Path | str, manifest_path: Path | str | None = None
) -> dict[str, Any]:
    """Read JSON/CSV transport and report only structural validity."""
    bundle, _input_bindings = _load_bundle_from_files(
        input_format=input_format, input_path=input_path, manifest_path=manifest_path
    )
    return structural_preflight_four_card(bundle).payload


def bind_evidence_record(
    *,
    evidence_root: Path | str,
    relative_path: str,
    whole_file: bool = False,
    byte_offset: int | None = None,
    byte_length: int | None = None,
) -> dict[str, Any]:
    """Validate a portable locator without exposing or accepting a digest."""
    if whole_file:
        if byte_offset is not None or byte_length is not None:
            _fail("CLI_ARGUMENT", "bind-evidence-record", "--whole-file cannot be combined with byte range values")
        record_locator: dict[str, Any] = {"kind": "whole_file"}
    else:
        if byte_offset is None or byte_length is None:
            _fail("CLI_ARGUMENT", "bind-evidence-record", "provide --whole-file or both --byte-offset and --byte-length")
        record_locator = {"kind": "byte_range", "byte_offset": byte_offset, "byte_length": byte_length}
    locator = _normalize_evidence_locator(
        {"relative_path": relative_path, "record_locator": record_locator}, "evidence_locator"
    )
    _materialize_evidence(locator, root=_resolve_evidence_root(evidence_root), path="evidence_locator")
    return locator


def preflight_from_files(
    *,
    input_format: str,
    input_path: Path | str,
    output_dir: Path | str,
    evidence_root: Path | str,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Run an isolated preflight and write only pass artifacts after all gates succeed."""
    output = Path(output_dir)
    if output.exists():
        if not output.is_dir():
            _fail("OUTPUT_DIR", str(output), "must be a directory path")
        if any(output.iterdir()):
            _fail("OUTPUT_DIR_NOT_EMPTY", str(output), "must be empty so a failed rerun cannot reuse prior pass artifacts")
    bundle, input_bindings = _load_bundle_from_files(
        input_format=input_format, input_path=input_path, manifest_path=manifest_path
    )
    structural_preflight_four_card(bundle)
    evidence_bundle = verify_evidence_bindings(bundle, evidence_root=evidence_root)
    report = _evidence_ready_report(evidence_bundle)
    normalized_path = output / "normalized-diagnostic.json"
    receipt_path = output / "preflight-receipt.json"
    normalized_sha256 = write_json_with_sha256(normalized_path, evidence_bundle.payload)
    receipt = {
        "schema_version": PREFLIGHT_RECEIPT_SCHEMA_VERSION,
        "status": "evidence_ready",
        "diagnostic_payload_sha256": evidence_bundle.diagnostic_payload_sha256,
        "normalized_artifact_sha256": normalized_sha256,
        "bindings": {**input_bindings, "evidence_verification": evidence_bundle.evidence_verification},
        "report": report.payload,
        "model_fitting_permitted": False,
        "physical_ranking_enabled": False,
        "promotion_permitted": False,
    }
    receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(receipt))
    receipt_sha256 = write_json_with_sha256(receipt_path, receipt)
    return {
        "status": "evidence_ready",
        "normalized_diagnostic_sha256": evidence_bundle.diagnostic_payload_sha256,
        "normalized_artifact_sha256": normalized_sha256,
        "preflight_receipt_sha256": receipt_sha256,
        "output_dir": str(output),
        "coated_readings": 24,
        "model_fitting_permitted": False,
        "physical_ranking_enabled": False,
    }


def verify_preflight_receipt(*, receipt_path: Path | str, evidence_root: Path | str) -> dict[str, Any]:
    """Reverify a portable v2 receipt against a fresh copy of its evidence root."""
    receipt_file = Path(receipt_path)
    try:
        verify_sha256_sidecar(receipt_file)
    except CalibrationError as error:
        _fail("RECEIPT_SIDECAR", str(receipt_file), str(error))
    receipt = _mapping(_load_json_no_duplicates(receipt_file), "receipt")
    if receipt.get("schema_version") != PREFLIGHT_RECEIPT_SCHEMA_VERSION:
        _fail("RECEIPT_SCHEMA", "receipt.schema_version", f"must be {PREFLIGHT_RECEIPT_SCHEMA_VERSION}")
    if receipt.get("status") != "evidence_ready":
        _fail("RECEIPT_STATUS", "receipt.status", "must be evidence_ready")
    receipt_payload_sha256 = _sha256(receipt.get("receipt_payload_sha256"), "receipt.receipt_payload_sha256")
    receipt_without_self_hash = dict(receipt)
    receipt_without_self_hash.pop("receipt_payload_sha256", None)
    if sha256_bytes(canonical_json_bytes(receipt_without_self_hash)) != receipt_payload_sha256:
        _fail("RECEIPT_PAYLOAD_SHA256", "receipt.receipt_payload_sha256", "does not bind the receipt payload")
    normalized_path = receipt_file.with_name("normalized-diagnostic.json")
    try:
        normalized_artifact_sha256 = verify_sha256_sidecar(normalized_path)
    except CalibrationError as error:
        _fail("NORMALIZED_ARTIFACT", str(normalized_path), str(error))
    if normalized_artifact_sha256 != _sha256(receipt.get("normalized_artifact_sha256"), "receipt.normalized_artifact_sha256"):
        _fail("NORMALIZED_ARTIFACT_SHA256", "receipt.normalized_artifact_sha256", "does not match the normalized artifact")
    normalized_payload = _mapping(_load_json_no_duplicates(normalized_path), "normalized-diagnostic")
    diagnostic_payload_sha256 = sha256_bytes(canonical_json_bytes(normalized_payload))
    if diagnostic_payload_sha256 != _sha256(receipt.get("diagnostic_payload_sha256"), "receipt.diagnostic_payload_sha256"):
        _fail("NORMALIZED_PAYLOAD_SHA256", "receipt.diagnostic_payload_sha256", "does not bind normalized-diagnostic.json")
    bindings = _mapping(receipt.get("bindings"), "receipt.bindings")
    verification = _mapping(bindings.get("evidence_verification"), "receipt.bindings.evidence_verification")
    if verification.get("schema_version") != EVIDENCE_VERIFICATION_SCHEMA_VERSION:
        _fail("EVIDENCE_VERIFICATION_SCHEMA", "receipt.bindings.evidence_verification.schema_version", "is not supported")
    expected_verification_sha256 = _sha256(
        verification.get("evidence_verification_sha256"), "receipt.bindings.evidence_verification.evidence_verification_sha256"
    )
    verification_without_hash = dict(verification)
    verification_without_hash.pop("evidence_verification_sha256", None)
    if sha256_bytes(canonical_json_bytes(verification_without_hash)) != expected_verification_sha256:
        _fail("EVIDENCE_VERIFICATION_SHA256", "receipt.bindings.evidence_verification", "does not bind its file and record lists")
    files = _list(verification.get("files"), "receipt.bindings.evidence_verification.files")
    records = _list(verification.get("records"), "receipt.bindings.evidence_verification.records")
    if files != sorted(files, key=lambda item: (item["relative_path"], item["file_sha256"])):
        _fail("EVIDENCE_VERIFICATION_ORDER", "receipt.bindings.evidence_verification.files", "must be sorted canonically")
    if records != sorted(records, key=lambda item: item["logical_path"]):
        _fail("EVIDENCE_VERIFICATION_ORDER", "receipt.bindings.evidence_verification.records", "must be sorted canonically")
    root = _resolve_evidence_root(evidence_root)
    expected_files: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(files):
        file_binding = _mapping(item, f"receipt.bindings.evidence_verification.files[{index}]")
        _exact_fields(file_binding, f"receipt.bindings.evidence_verification.files[{index}]", ("relative_path", "file_sha256", "size_bytes"))
        relative_path = _relative_posix_evidence_path(file_binding["relative_path"], f"receipt.bindings.evidence_verification.files[{index}].relative_path")
        expected_files[relative_path] = file_binding
        actual = _materialize_evidence(
            {"relative_path": relative_path, "record_locator": {"kind": "whole_file"}},
            root=root,
            path=f"receipt.bindings.evidence_verification.files[{index}]",
        )
        if actual["file_sha256"] != _sha256(file_binding["file_sha256"], f"receipt.bindings.evidence_verification.files[{index}].file_sha256"):
            _fail("EVIDENCE_FILE_HASH", f"receipt.bindings.evidence_verification.files[{index}]", "file hash no longer matches receipt")
        if actual["size_bytes"] != _integer(file_binding["size_bytes"], f"receipt.bindings.evidence_verification.files[{index}].size_bytes"):
            _fail("EVIDENCE_FILE_SIZE", f"receipt.bindings.evidence_verification.files[{index}]", "file size no longer matches receipt")
    for index, item in enumerate(records):
        record = _mapping(item, f"receipt.bindings.evidence_verification.records[{index}]")
        _exact_fields(
            record,
            f"receipt.bindings.evidence_verification.records[{index}]",
            ("logical_path", "relative_path", "byte_offset", "byte_length", "record_sha256"),
        )
        relative_path = _relative_posix_evidence_path(record["relative_path"], f"receipt.bindings.evidence_verification.records[{index}].relative_path")
        if relative_path not in expected_files:
            _fail("EVIDENCE_RECORD_FILE", f"receipt.bindings.evidence_verification.records[{index}]", "references an undeclared evidence file")
        actual = _materialize_evidence(
            {
                "relative_path": relative_path,
                "record_locator": {
                    "kind": "byte_range",
                    "byte_offset": record["byte_offset"],
                    "byte_length": record["byte_length"],
                },
            },
            root=root,
            path=f"receipt.bindings.evidence_verification.records[{index}]",
        )
        if actual["record_locator"]["record_sha256"] != _sha256(record["record_sha256"], f"receipt.bindings.evidence_verification.records[{index}].record_sha256"):
            _fail("EVIDENCE_RECORD_HASH", f"receipt.bindings.evidence_verification.records[{index}]", "record bytes no longer match receipt")
    return {
        "status": "evidence_still_matches_receipt",
        "evidence_still_matches_receipt": True,
        "diagnostic_payload_sha256": diagnostic_payload_sha256,
        "evidence_verification_sha256": expected_verification_sha256,
        "model_fitting_permitted": False,
        "physical_ranking_enabled": False,
        "promotion_permitted": False,
    }
