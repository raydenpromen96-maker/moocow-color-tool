"""Pre-spectra material, batch, and custody receipts for the physical pilot.

This module deliberately has no dependency on the generic dataset loader, K/S
pipeline, evaluator, or any spectra parser.  Its only authority is the frozen
pilot design plus current-lot and actual-weighing evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import shutil
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence

import numpy as np

from .errors import CalibrationError, DatasetValidationError
from .hashing import (
    canonical_json_bytes,
    read_regular_file_snapshot,
    read_verified_json,
    sha256_bytes,
    verify_sha256_sidecar,
    write_json_with_sha256,
)
from .pilot import verify_pilot_design_receipt


COMPONENT_ORDER: tuple[tuple[str, str], ...] = (
    ("base", "base-waterborne-clear"),
    ("Y83S", "colorant-Y83S"),
    ("Y74S", "colorant-Y74S"),
    ("B150S", "colorant-B150S"),
    ("B153S", "colorant-B153S"),
    ("R254D", "colorant-R254D"),
    ("R101Y", "colorant-R101Y"),
    ("R101V", "colorant-R101V"),
    ("Y42S", "colorant-Y42S"),
    ("073", "colorant-073"),
    ("W064", "colorant-W064"),
    ("V23", "colorant-V23"),
    ("G7", "colorant-G7"),
    ("R122S", "colorant-R122S"),
    ("BK7H", "colorant-BK7H"),
)
FORMULA_KEYS = tuple(item[0] for item in COMPONENT_ORDER)
COMPONENT_IDS = tuple(item[1] for item in COMPONENT_ORDER)
COMPONENT_BY_KEY = dict(COMPONENT_ORDER)
KEY_BY_COMPONENT = {component_id: formula_key for formula_key, component_id in COMPONENT_ORDER}
TRAIN_FAMILIES = tuple("FAM-TR-BASIS-BASE" if key == "base" else f"FAM-TR-BASIS-{key}" for key in FORMULA_KEYS)
VALIDATION_FAMILIES = ("FAM-VA-MIX-01", "FAM-VA-MIX-02")
HOLDOUT_FAMILIES = ("FAM-HO-MIX-01", "FAM-HO-MIX-02", "FAM-HO-MIX-03")
MASS_SOLIDS_NONVOLATILE_DENSITY = "mass_solids_nonvolatile_density"
WET_DENSITY_VOLUME_SOLIDS = "wet_density_volume_solids"
ROUTES = {MASS_SOLIDS_NONVOLATILE_DENSITY, WET_DENSITY_VOLUME_SOLIDS}
PERMISSIONS = (
    "pilot_acquisition_permitted",
    "open_admission_permitted",
    "model_fitting_permitted",
    "holdout_release_permitted",
    "physical_ranking_enabled",
    "promotion_permitted",
)
_DECIMAL = re.compile(r"[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_PLACEHOLDER_MARKERS = ("required", "template", "placeholder", "catalog", "synthetic", "inferred", "not_yet")
_SCOPE_MARKERS = (
    "spectrum",
    "spectra",
    "reflectance",
    "measurement_id",
    "raw_reading",
    "model_",
    "fit_",
    "evaluation",
    "candidate",
    "admission",
    "release",
    "ranking",
    "promotion",
    "production_pass",
)


class AcquisitionPreflightError(CalibrationError):
    """A stable, non-secret-bearing validation failure for this boundary."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"[{code}] {path}: {message}")


def _fail(code: str, path: str, message: str) -> None:
    raise AcquisitionPreflightError(code, path, message)


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("TYPE", path, "must be an object")
    return value


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("TYPE", path, "must be an array")
    return value


def _text(value: object, path: str, *, placeholder: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("TEXT", path, "must be a non-empty string")
    result = value.strip()
    if placeholder and any(marker in result.casefold() for marker in _PLACEHOLDER_MARKERS):
        _fail("PLACEHOLDER", path, "must be a current physical value, not placeholder text")
    return result


def _timestamp(value: object, path: str) -> str:
    text = _text(value, path)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail("TIMESTAMP", path, "must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("TIMESTAMP", path, "must include a timezone offset")
    return text


def _decimal(value: object, path: str, *, positive: bool = False, fraction: bool = False) -> Decimal:
    if isinstance(value, bool):
        _fail("NUMERIC_BOOL", path, "must be a decimal number, not a boolean")
    if isinstance(value, float) and not math.isfinite(value):
        _fail("NUMERIC_NONFINITE", path, "must be a finite decimal")
    text = str(value)
    if not _DECIMAL.fullmatch(text):
        _fail("NUMERIC_NONFINITE", path, "must be a finite unsigned base-10 decimal")
    try:
        result = Decimal(text)
    except InvalidOperation:
        _fail("NUMERIC_NONFINITE", path, "must be a finite decimal")
    if positive and result <= 0:
        _fail("POSITIVE_NUMBER", path, "must be greater than zero")
    if fraction and not (Decimal(0) < result <= Decimal(1)):
        _fail("PROPERTY_FRACTION", path, "must be in (0, 1]")
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"", "-0"} else text


def _sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        _fail("SHA256", path, "must be a SHA-256 hex digest")
    return value.lower()


def _permissions() -> dict[str, bool]:
    return {name: False for name in PERMISSIONS}


def _assert_all_false(value: Mapping[str, Any], path: str) -> None:
    for permission in PERMISSIONS:
        if value.get(permission) is not False:
            _fail("PREFLIGHT_SCOPE", f"{path}.{permission}", "must remain false at acquisition preflight")


def _portable_relative_path(value: object, path: str) -> str:
    text = _text(value, path, placeholder=False)
    windows_path = PureWindowsPath(text)
    posix_path = PurePosixPath(text)
    parts = text.split("/")
    if (
        "\\" in text
        or "\x00" in text
        or any(character in '<>:"|?*' for character in text)
        or windows_path.is_absolute()
        or windows_path.drive
        or posix_path.is_absolute()
        or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)
    ):
        _fail("EVIDENCE_PATH", path, "must be a portable relative POSIX path without traversal")
    return text


def _read_json_snapshot(
    path: Path,
    *,
    trusted_root: Path | None,
    failure_code: str,
    require_sidecar: bool = False,
) -> tuple[Any, str]:
    try:
        if require_sidecar:
            return read_verified_json(path, require_sidecar=True, trusted_root=trusted_root)
        raw, digest = read_regular_file_snapshot(path, trusted_root=trusted_root)
    except CalibrationError as error:
        message = str(error)
        if "duplicate JSON key" in message:
            _fail("JSON_DUPLICATE_KEY", str(path), message)
        if "non-finite JSON constant" in message:
            _fail("NUMERIC_NONFINITE", str(path), message)
        _fail(failure_code, str(path), str(error))
    except OSError as error:
        _fail(failure_code, str(path), str(error))

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                _fail("JSON_DUPLICATE_KEY", str(path), f"contains duplicate key {key!r}")
            result[key] = item
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=lambda constant: _fail("NUMERIC_NONFINITE", str(path), f"contains non-finite JSON constant {constant}"),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        _fail(failure_code, str(path), f"cannot read canonical JSON: {error}")
    return value, digest


def _resolve_evidence(root: Path, locator: object, path: str, *, failure_code: str) -> tuple[Path, str, str]:
    item = _mapping(locator, path)
    if set(item) != {"relative_path", "record_locator"}:
        _fail("EVIDENCE_PATH", path, "must contain only relative_path and record_locator")
    relative_path = _portable_relative_path(item["relative_path"], f"{path}.relative_path")
    record_locator = _mapping(item["record_locator"], f"{path}.record_locator")
    if record_locator != {"kind": "whole_file"}:
        _fail("EVIDENCE_PATH", f"{path}.record_locator", "must be exactly a whole-file locator")
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    value, digest = _read_json_snapshot(
        candidate, trusted_root=root, failure_code=failure_code, require_sidecar=True
    )
    return candidate, relative_path, digest


def _resolve_raw_evidence(root: Path, locator: object, path: str, *, failure_code: str) -> tuple[Path, str, str]:
    item = _mapping(locator, path)
    if set(item) != {"relative_path", "record_locator"}:
        _fail("EVIDENCE_PATH", path, "must contain only relative_path and record_locator")
    relative_path = _portable_relative_path(item["relative_path"], f"{path}.relative_path")
    record_locator = _mapping(item["record_locator"], f"{path}.record_locator")
    if record_locator != {"kind": "whole_file"}:
        _fail("EVIDENCE_PATH", f"{path}.record_locator", "must be exactly a whole-file locator")
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        _, digest = read_regular_file_snapshot(candidate, trusted_root=root)
    except CalibrationError as error:
        _fail(failure_code, str(candidate), str(error))
    return candidate, relative_path, digest


def _contains_scope_field(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).casefold()
            if key_text == "dft_um" or any(marker in key_text for marker in _SCOPE_MARKERS):
                _fail("PREFLIGHT_SCOPE", f"{path}.{key}", "spectra, DFT actuals, or model fields are outside acquisition preflight")
            _contains_scope_field(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _contains_scope_field(item, f"{path}[{index}]")


def _create_staging_output(output_dir: Path | str) -> tuple[Path, Path, bool]:
    output = Path(output_dir)
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            _fail("OUTPUT_DIR", str(output), "must be a non-link directory path")
        if any(output.iterdir()):
            _fail("OUTPUT_DIR_NOT_EMPTY", str(output), "must be empty")
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        _fail("OUTPUT_WRITE", str(staging), str(error))
    return output, staging, output.exists()


def _publish_staging_output(*, output: Path, staging: Path, existed_empty: bool) -> None:
    try:
        if existed_empty:
            output.rmdir()
        staging.replace(output)
    except OSError as error:
        if existed_empty and not output.exists():
            try:
                output.mkdir(parents=True, exist_ok=False)
            except OSError as restore_error:
                _fail("OUTPUT_WRITE", str(output), str(restore_error))
        _fail("OUTPUT_WRITE", str(output), str(error))


def _publish(output_dir: Path | str, build: Any, verify: Any) -> dict[str, Any]:
    output: Path | None = None
    staging: Path | None = None
    try:
        output, staging, existed_empty = _create_staging_output(output_dir)
        result = build(staging)
        verify(staging)
        _publish_staging_output(output=output, staging=staging, existed_empty=existed_empty)
        result["output_dir"] = str(output)
        return result
    except AcquisitionPreflightError:
        raise
    except OSError as error:
        _fail("OUTPUT_WRITE", str(staging or output_dir), str(error))
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _template_property_fields(conversion_route: str) -> tuple[tuple[str, str], ...]:
    if conversion_route == MASS_SOLIDS_NONVOLATILE_DENSITY:
        return (
            ("nonvolatile_mass_fraction", "fraction"),
            ("nonvolatile_density_g_ml", "g/mL"),
        )
    if conversion_route == WET_DENSITY_VOLUME_SOLIDS:
        return (
            ("wet_density_g_ml", "g/mL"),
            ("component_nonvolatile_volume_fraction", "fraction"),
        )
    _fail("PROPERTY_ROUTE", "conversion_route", "must be an approved conversion route")


def _template_support(family_id: str) -> tuple[str, ...]:
    supports = {
        "FAM-VA-MIX-01": ("base", "Y83S", "B150S"),
        "FAM-VA-MIX-02": ("base", "R254D", "G7"),
        "FAM-HO-MIX-01": ("base", "Y74S", "R122S"),
        "FAM-HO-MIX-02": ("base", "B153S", "R101Y"),
        "FAM-HO-MIX-03": ("base", "Y42S", "V23", "BK7H"),
    }
    if family_id == "FAM-TR-BASIS-BASE":
        return ("base",)
    if family_id.startswith("FAM-TR-BASIS-"):
        formula_key = family_id.removeprefix("FAM-TR-BASIS-")
        if formula_key in COMPONENT_BY_KEY:
            return ("base", formula_key)
    try:
        return supports[family_id]
    except KeyError:
        _fail("FORMULA_FAMILY_BATCH", "formula_family_id", "is not a fixed pilot family")


def _template_component_order() -> list[dict[str, str]]:
    return [{"formula_key": formula_key, "component_id": component_id} for formula_key, component_id in COMPONENT_ORDER]


def _template_material_manifest(conversion_route: str) -> dict[str, Any]:
    return {
        "schema_version": "moocow-pilot-material-lots-v1",
        "component_order": _template_component_order(),
        "materials": [
            {
                "formula_key": formula_key,
                "component_id": component_id,
                "physical_lot_id": f"REQUIRED_PHYSICAL_LOT_ID_{formula_key}",
                "product_name": f"REQUIRED_PRODUCT_NAME_{formula_key}",
                "supplier": f"REQUIRED_SUPPLIER_{formula_key}",
                "label_verification": {
                    "status": "verified_physical_label",
                    "verification_id": f"REQUIRED_LABEL_VERIFICATION_ID_{formula_key}",
                    "verified_at": f"REQUIRED_LABEL_VERIFIED_AT_{formula_key}",
                    "evidence": {
                        "relative_path": f"REQUIRED_LABEL_EVIDENCE_PATH_{formula_key}",
                        "record_locator": {"kind": "whole_file"},
                    },
                },
                "conversion": {
                    "route": conversion_route,
                    "property_record_evidence": {
                        "relative_path": f"evidence/properties/{formula_key}.conversion-properties.json",
                        "record_locator": {"kind": "whole_file"},
                    },
                },
            }
            for formula_key, component_id in COMPONENT_ORDER
        ],
    }


def _template_property_record(*, formula_key: str, component_id: str, conversion_route: str) -> dict[str, Any]:
    return {
        "schema_version": "moocow-conversion-property-record-v2",
        "record_kind": "current_lot_conversion_properties",
        "component_id": component_id,
        "physical_lot_id": f"REQUIRED_PHYSICAL_LOT_ID_{formula_key}",
        "conversion_route": conversion_route,
        "properties": {
            property_name: {
                "property_record_id": f"REQUIRED_{formula_key}_{property_name}_RECORD_ID",
                "value": None,
                "unit": unit,
                "method": f"REQUIRED_{formula_key}_{property_name}_METHOD",
                "observed_at": f"REQUIRED_{formula_key}_{property_name}_OBSERVED_AT",
            }
            for property_name, unit in _template_property_fields(conversion_route)
        },
    }


def _template_batch(*, family_id: str, split: str, conversion_route: str) -> dict[str, Any]:
    support = _template_support(family_id)
    batch_id = f"REQUIRED_FROZEN_FORMULA_BATCH_ID_{family_id.removeprefix('FAM-')}"
    return {
        "schema_version": "moocow-pilot-formula-batch-v1",
        "formula_family_id": family_id,
        "formula_id": _expected_formula_id(family_id),
        "formula_batch_id": batch_id,
        "split": split,
        "actual_components": [
            {
                "component_id": COMPONENT_BY_KEY[formula_key],
                "physical_lot_id": f"REQUIRED_PHYSICAL_LOT_ID_{formula_key}",
                "conversion_route": conversion_route,
                "actual_wet_mass_g": None,
                "nonvolatile_volume_ml": None,
                "actual_weighing_evidence": {
                    "relative_path": f"REQUIRED_ACTUAL_WEIGHING_EVIDENCE_PATH_{family_id.removeprefix('FAM-')}_{formula_key}",
                    "record_locator": {"kind": "whole_file"},
                },
            }
            for formula_key in support
        ],
        "actual_nv_vector_component_order": list(FORMULA_KEYS),
        "actual_nv_vector": [None for _ in COMPONENT_ORDER],
        "actual_nv_sum": None,
    }


def _template_weighing_record(batch: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "moocow-actual-weighing-record-v2",
        "record_kind": "actual_weighing_observation",
        "formula_id": batch["formula_id"],
        "formula_batch_id": batch["formula_batch_id"],
        "entries": [
            {
                "weighing_record_id": f"REQUIRED_WEIGHING_RECORD_ID_{component['component_id']}",
                "weighing_event_id": f"REQUIRED_WEIGHING_EVENT_ID_{component['component_id']}",
                "component_id": component["component_id"],
                "physical_lot_id": component["physical_lot_id"],
                "actual_wet_mass_g": None,
                "actual_wet_mass_unit": "g",
                "weighed_at": f"REQUIRED_WEIGHED_AT_{component['component_id']}",
                "weighing_method": f"REQUIRED_WEIGHING_METHOD_{component['component_id']}",
            }
            for component in _list(batch["actual_components"], "template.actual_components")
        ],
    }


def _require_template_placeholder(value: object, path: str) -> None:
    if not isinstance(value, str) or not value.startswith("REQUIRED_"):
        _fail("PREPARE_VALIDATION", path, "must remain a REQUIRED_ placeholder")


def _validate_prepared_acquisition_package(output: Path, *, conversion_route: str) -> None:
    json_files = sorted(output.rglob("*.json"))
    if not json_files:
        _fail("PREPARE_VALIDATION", str(output), "must contain JSON templates")
    values: dict[Path, Any] = {}
    for path in json_files:
        try:
            verify_sha256_sidecar(path)
        except CalibrationError as error:
            _fail("PREPARE_VALIDATION", str(path), str(error))
        try:
            values[path] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            _fail("PREPARE_VALIDATION", str(path), str(error))

    package_path = output / "package-template.json"
    package = _mapping(values.get(package_path), str(package_path))
    package_keys = {
        "schema_version",
        "status",
        "conversion_route",
        "counts",
        "contains_physical_evidence",
        "receipt_emitted",
        *PERMISSIONS,
    }
    if set(package) != package_keys:
        _fail("PREPARE_VALIDATION", str(package_path), "must contain only template status, route, counts, evidence flags, and permissions")
    if package.get("schema_version") != "moocow-acquisition-package-template-v1" or package.get("status") != "prepared_template_only":
        _fail("PREPARE_VALIDATION", str(package_path), "has an invalid template schema or status")
    if package.get("conversion_route") != conversion_route or package.get("contains_physical_evidence") is not False or package.get("receipt_emitted") is not False:
        _fail("PREPARE_VALIDATION", str(package_path), "must remain a template-only route declaration")
    if package.get("counts") != {"materials": 15, "open_batches": 17, "sealed_batches": 3}:
        _fail("PREPARE_VALIDATION", str(package_path), "must declare the fixed 15/17/3 counts")
    _assert_all_false(package, str(package_path))

    materials_path = output / "shared-template" / "materials.json"
    materials = _mapping(values.get(materials_path), str(materials_path))
    _contains_scope_field(materials, str(materials_path))
    if materials.get("schema_version") != "moocow-pilot-material-lots-v1":
        _fail("PREPARE_VALIDATION", str(materials_path), "must retain the current materials schema")
    _validate_component_order(materials.get("component_order"), f"{materials_path}.component_order")
    material_rows = _list(materials.get("materials"), f"{materials_path}.materials")
    if len(material_rows) != len(COMPONENT_ORDER):
        _fail("PREPARE_VALIDATION", str(materials_path), "must contain exactly 15 material templates")
    for index, (formula_key, component_id) in enumerate(COMPONENT_ORDER):
        material = _mapping(material_rows[index], f"{materials_path}.materials[{index}]")
        if material.get("formula_key") != formula_key or material.get("component_id") != component_id:
            _fail("PREPARE_VALIDATION", f"{materials_path}.materials[{index}]", "must retain the fixed component identity")
        for key in ("physical_lot_id", "product_name", "supplier"):
            _require_template_placeholder(material.get(key), f"{materials_path}.materials[{index}].{key}")
        label = _mapping(material.get("label_verification"), f"{materials_path}.materials[{index}].label_verification")
        if label.get("status") != "verified_physical_label":
            _fail("PREPARE_VALIDATION", f"{materials_path}.materials[{index}].label_verification.status", "must retain the required label status")
        _require_template_placeholder(label.get("verification_id"), f"{materials_path}.materials[{index}].label_verification.verification_id")
        _require_template_placeholder(label.get("verified_at"), f"{materials_path}.materials[{index}].label_verification.verified_at")
        label_evidence = _mapping(label.get("evidence"), f"{materials_path}.materials[{index}].label_verification.evidence")
        _require_template_placeholder(label_evidence.get("relative_path"), f"{materials_path}.materials[{index}].label_verification.evidence.relative_path")
        if label_evidence.get("record_locator") != {"kind": "whole_file"}:
            _fail("PREPARE_VALIDATION", f"{materials_path}.materials[{index}].label_verification.evidence.record_locator", "must retain the whole-file locator")
        conversion = _mapping(material.get("conversion"), f"{materials_path}.materials[{index}].conversion")
        if conversion.get("route") != conversion_route:
            _fail("PREPARE_VALIDATION", f"{materials_path}.materials[{index}].conversion.route", "must match the selected route")

    property_dir = output / "shared-template" / "operator-templates" / "properties"
    property_templates = sorted(property_dir.glob("*.conversion-properties.template.json"))
    if len(property_templates) != len(COMPONENT_ORDER):
        _fail("PREPARE_VALIDATION", str(property_dir), "must contain exactly 15 property templates")
    property_paths = {path.name: path for path in property_templates}
    for formula_key, component_id in COMPONENT_ORDER:
        path = property_paths.get(f"{formula_key}.conversion-properties.template.json")
        if path is None:
            _fail("PREPARE_VALIDATION", str(property_dir), f"is missing the {formula_key} property template")
        record = _mapping(values.get(path), str(path))
        _contains_scope_field(record, str(path))
        if (
            record.get("schema_version") != "moocow-conversion-property-record-v2"
            or record.get("record_kind") != "current_lot_conversion_properties"
            or record.get("component_id") != component_id
            or record.get("conversion_route") != conversion_route
        ):
            _fail("PREPARE_VALIDATION", str(path), "has an invalid property template identity")
        _require_template_placeholder(record.get("physical_lot_id"), f"{path}.physical_lot_id")
        properties = _mapping(record.get("properties"), f"{path}.properties")
        expected_fields = dict(_template_property_fields(conversion_route))
        if set(properties) != set(expected_fields):
            _fail("PREPARE_VALIDATION", f"{path}.properties", "must contain only the selected route fields")
        for property_name, unit in expected_fields.items():
            item = _mapping(properties[property_name], f"{path}.properties.{property_name}")
            if item.get("value") is not None or item.get("unit") != unit:
                _fail("PREPARE_VALIDATION", f"{path}.properties.{property_name}", "must retain its null observation and canonical unit")
            for key in ("property_record_id", "method", "observed_at"):
                _require_template_placeholder(item.get(key), f"{path}.properties.{property_name}.{key}")

    open_path = output / "open-template" / "batches.json"
    sealed_path = output / "sealed-holdout-template" / "batches.json"
    open_manifest = _mapping(values.get(open_path), str(open_path))
    sealed_manifest = _mapping(values.get(sealed_path), str(sealed_path))
    for path, manifest, expected_families, expected_splits in (
        (open_path, open_manifest, TRAIN_FAMILIES + VALIDATION_FAMILIES, ("train",) * 15 + ("validation",) * 2),
        (sealed_path, sealed_manifest, HOLDOUT_FAMILIES, ("holdout",) * 3),
    ):
        _contains_scope_field(manifest, str(path))
        if manifest.get("schema_version") != "moocow-pilot-batches-v1":
            _fail("PREPARE_VALIDATION", str(path), "must retain the current batches schema")
        batches = _list(manifest.get("batches"), f"{path}.batches")
        if len(batches) != len(expected_families):
            _fail("PREPARE_VALIDATION", str(path), "has an invalid fixed batch count")
        for index, (family_id, split) in enumerate(zip(expected_families, expected_splits, strict=True)):
            batch = _mapping(batches[index], f"{path}.batches[{index}]")
            if (
                batch.get("schema_version") != "moocow-pilot-formula-batch-v1"
                or batch.get("formula_family_id") != family_id
                or batch.get("formula_id") != _expected_formula_id(family_id)
                or batch.get("split") != split
                or batch.get("actual_nv_vector_component_order") != list(FORMULA_KEYS)
                or batch.get("actual_nv_vector") != [None for _ in COMPONENT_ORDER]
                or batch.get("actual_nv_sum") is not None
            ):
                _fail("PREPARE_VALIDATION", f"{path}.batches[{index}]", "has an invalid fixed batch template")
            _require_template_placeholder(batch.get("formula_batch_id"), f"{path}.batches[{index}].formula_batch_id")
            components = _list(batch.get("actual_components"), f"{path}.batches[{index}].actual_components")
            expected_support = _template_support(family_id)
            if [item.get("component_id") if isinstance(item, Mapping) else None for item in components] != [COMPONENT_BY_KEY[key] for key in expected_support]:
                _fail("PREPARE_VALIDATION", f"{path}.batches[{index}].actual_components", "must retain the fixed component support")
            for component in components:
                item = _mapping(component, f"{path}.batches[{index}].actual_components[]")
                if item.get("conversion_route") != conversion_route or item.get("actual_wet_mass_g") is not None or item.get("nonvolatile_volume_ml") is not None:
                    _fail("PREPARE_VALIDATION", f"{path}.batches[{index}].actual_components", "must retain the selected route and null observations")
                _require_template_placeholder(item.get("physical_lot_id"), f"{path}.batches[{index}].actual_components.physical_lot_id")
                evidence = _mapping(item.get("actual_weighing_evidence"), f"{path}.batches[{index}].actual_components.actual_weighing_evidence")
                _require_template_placeholder(evidence.get("relative_path"), f"{path}.batches[{index}].actual_components.actual_weighing_evidence.relative_path")
                if evidence.get("record_locator") != {"kind": "whole_file"}:
                    _fail("PREPARE_VALIDATION", f"{path}.batches[{index}].actual_components.actual_weighing_evidence.record_locator", "must retain the whole-file locator")
    _assert_open_has_no_holdout(open_manifest, str(open_path))

    for root, expected_count, expected_names in (
        (output / "open-template" / "operator-templates" / "weighings", 17, {f"{family}.actual-weighing.template.json" for family in TRAIN_FAMILIES + VALIDATION_FAMILIES}),
        (output / "sealed-holdout-template" / "operator-templates" / "weighings", 3, {f"batch-{index:02d}.actual-weighing.template.json" for index in range(1, 4)}),
    ):
        templates = sorted(root.glob("*.actual-weighing.template.json"))
        if len(templates) != expected_count or {path.name for path in templates} != expected_names:
            _fail("PREPARE_VALIDATION", str(root), "has an invalid weighing-template roster")
        for path in templates:
            record = _mapping(values.get(path), str(path))
            _contains_scope_field(record, str(path))
            if record.get("schema_version") != "moocow-actual-weighing-record-v2" or record.get("record_kind") != "actual_weighing_observation":
                _fail("PREPARE_VALIDATION", str(path), "has an invalid weighing template schema")
            _require_template_placeholder(record.get("formula_batch_id"), f"{path}.formula_batch_id")
            entries = _list(record.get("entries"), f"{path}.entries")
            if not entries:
                _fail("PREPARE_VALIDATION", f"{path}.entries", "must retain unresolved component entries")
            for entry in entries:
                item = _mapping(entry, f"{path}.entries[]")
                if item.get("actual_wet_mass_g") is not None or item.get("actual_wet_mass_unit") != "g":
                    _fail("PREPARE_VALIDATION", f"{path}.entries", "must retain null masses in g")
                for key in ("weighing_record_id", "weighing_event_id", "physical_lot_id", "weighed_at", "weighing_method"):
                    _require_template_placeholder(item.get(key), f"{path}.entries.{key}")

    for evidence_root in (
        output / "shared-template" / "evidence",
        output / "open-template" / "evidence",
        output / "sealed-holdout-template" / "evidence",
    ):
        if any(evidence_root.rglob("*.json")):
            _fail("PREPARE_VALIDATION", str(evidence_root), "must not contain JSON evidence records")

    public_paths = [package_path, output / "README.md", *(output / "shared-template").rglob("*"), *(output / "open-template").rglob("*")]
    for path in public_paths:
        if not path.is_file():
            continue
        if path == package_path:
            text = canonical_json_bytes({key: value for key, value in package.items() if key != "holdout_release_permitted"}).decode("utf-8").casefold()
        else:
            text = path.read_text(encoding="utf-8").casefold()
        if "fam-ho-" in text or "holdout" in text:
            _fail("PREPARE_VALIDATION", str(path), "public-safe projection must not contain sealed identities")
    if any("formula_batch_id" in path.read_text(encoding="utf-8") for path in (output / "shared-template").rglob("*.json")):
        _fail("PREPARE_VALIDATION", str(output / "shared-template"), "must not contain formula batch identities")
    forbidden_names = ("receipt", "rank", "dft", "spectrum", "signature", "custody", "raw-reading")
    for path in output.rglob("*"):
        if path.is_file() and any(marker in path.name.casefold() for marker in forbidden_names):
            _fail("PREPARE_VALIDATION", str(path), "must not contain a forbidden preflight artifact")


def prepare_acquisition_package(*, conversion_route: str, output_dir: Path | str) -> dict[str, Any]:
    """Publish a private, deliberately invalid 15/17/3 acquisition-template package."""

    _template_property_fields(conversion_route)
    open_batches = [
        _template_batch(family_id=family_id, split="train", conversion_route=conversion_route)
        for family_id in TRAIN_FAMILIES
    ] + [
        _template_batch(family_id=family_id, split="validation", conversion_route=conversion_route)
        for family_id in VALIDATION_FAMILIES
    ]
    sealed_batches = [
        _template_batch(family_id=family_id, split="holdout", conversion_route=conversion_route)
        for family_id in HOLDOUT_FAMILIES
    ]

    def build(staging: Path) -> dict[str, Any]:
        package = {
            "schema_version": "moocow-acquisition-package-template-v1",
            "status": "prepared_template_only",
            "conversion_route": conversion_route,
            "counts": {"materials": 15, "open_batches": 17, "sealed_batches": 3},
            "contains_physical_evidence": False,
            "receipt_emitted": False,
            **_permissions(),
        }
        write_json_with_sha256(staging / "package-template.json", package)
        (staging / "README.md").write_text(
            "# Acquisition package template\n\n"
            "This private package is deliberately incomplete. It contains no physical evidence and emits no receipt. "
            "Only copy `shared-template` and `open-template` into an open working area; keep the private subtree separate.\n",
            encoding="utf-8",
            newline="\n",
        )
        shared = staging / "shared-template"
        open_root = staging / "open-template"
        sealed = staging / "sealed-holdout-template"
        write_json_with_sha256(shared / "materials.json", _template_material_manifest(conversion_route))
        (shared / "evidence" / "labels").mkdir(parents=True, exist_ok=True)
        (shared / "evidence" / "labels" / "README.md").write_text(
            "# Physical label evidence\n\nNo label artifact is created here. Add one real whole-file label only after observation.\n",
            encoding="utf-8",
            newline="\n",
        )
        (shared / "evidence" / "properties").mkdir(parents=True, exist_ok=True)
        (shared / "evidence" / "properties" / "README.md").write_text(
            "# Conversion records\n\nNo property record is created here. Copy a completed canonical record here only after observation and create a fresh sidecar.\n",
            encoding="utf-8",
            newline="\n",
        )
        for formula_key, component_id in COMPONENT_ORDER:
            write_json_with_sha256(
                shared / "operator-templates" / "properties" / f"{formula_key}.conversion-properties.template.json",
                _template_property_record(formula_key=formula_key, component_id=component_id, conversion_route=conversion_route),
            )
        write_json_with_sha256(open_root / "batches.json", {"schema_version": "moocow-pilot-batches-v1", "batches": open_batches})
        (open_root / "evidence" / "weighings").mkdir(parents=True, exist_ok=True)
        (open_root / "evidence" / "weighings" / "README.md").write_text(
            "# Actual weighings\n\nNo weighing record is created here. Copy a completed canonical record here only after observation and create a fresh sidecar.\n",
            encoding="utf-8",
            newline="\n",
        )
        for batch in open_batches:
            write_json_with_sha256(
                open_root / "operator-templates" / "weighings" / f"{batch['formula_family_id']}.actual-weighing.template.json",
                _template_weighing_record(batch),
            )
        write_json_with_sha256(sealed / "batches.json", {"schema_version": "moocow-pilot-batches-v1", "batches": sealed_batches})
        (sealed / "evidence" / "weighings").mkdir(parents=True, exist_ok=True)
        (sealed / "evidence" / "weighings" / "README.md").write_text(
            "# Actual weighings\n\nNo weighing record is created here. Keep completed canonical records in this private subtree.\n",
            encoding="utf-8",
            newline="\n",
        )
        for index, batch in enumerate(sealed_batches, start=1):
            write_json_with_sha256(
                sealed / "operator-templates" / "weighings" / f"batch-{index:02d}.actual-weighing.template.json",
                _template_weighing_record(batch),
            )
        (sealed / "SEALED_OPERATOR_README.md").write_text(
            "# Private operator instructions\n\nKeep this subtree private. Replace placeholders only from observed physical evidence and frozen mappings.\n",
            encoding="utf-8",
            newline="\n",
        )
        return {
            "status": "prepared_template_only",
            "conversion_route": conversion_route,
            "counts": {"materials": 15, "open_batches": 17, "sealed_batches": 3},
            **_permissions(),
        }

    result = _publish(
        output_dir,
        build,
        lambda staging: _validate_prepared_acquisition_package(staging, conversion_route=conversion_route),
    )
    result.pop("output_dir", None)
    return result


def _receipt_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(payload))
    return payload


def _verify_receipt(path: Path, *, schema_version: str, status: str) -> tuple[dict[str, Any], str]:
    value, digest = _read_json_snapshot(path, trusted_root=None, failure_code="RECEIPT_SIDECAR", require_sidecar=True)
    receipt = dict(_mapping(value, str(path)))
    if receipt.get("schema_version") != schema_version or receipt.get("status") != status:
        _fail("RECEIPT_BINDING", str(path), "has an unsupported schema or status")
    payload_sha = _sha256(receipt.get("receipt_payload_sha256"), f"{path}.receipt_payload_sha256")
    payload = dict(receipt)
    payload.pop("receipt_payload_sha256")
    if sha256_bytes(canonical_json_bytes(payload)) != payload_sha:
        _fail("RECEIPT_BINDING", f"{path}.receipt_payload_sha256", "does not bind the receipt payload")
    _assert_all_false(receipt, str(path))
    return receipt, digest


def _validate_component_order(value: object, path: str) -> None:
    order = _list(value, path)
    expected = [{"formula_key": formula_key, "component_id": component_id} for formula_key, component_id in COMPONENT_ORDER]
    if order == expected:
        return
    if len(order) > 9 and isinstance(order[9], Mapping) and order[9].get("formula_key") != "073":
        _fail("COMPONENT_KEY_073", f"{path}[9].formula_key", "must be the string key 073")
    if any(isinstance(item, Mapping) and item.get("formula_key") == "073" and item.get("component_id") != "colorant-073" for item in order):
        _fail("COMPONENT_KEY_073", path, "073 must map only to colorant-073")
    _fail("COMPONENT_ORDER", path, "must equal the fixed ordered formula-key/component-ID bijection")


def _validate_label(record: Mapping[str, Any], root: Path, path: str) -> dict[str, Any]:
    label = _mapping(record.get("label_verification"), f"{path}.label_verification")
    if label.get("status") != "verified_physical_label":
        _fail("REGISTRY_LOT_VERIFICATION", f"{path}.label_verification.status", "must be verified_physical_label")
    verification_id = _text(label.get("verification_id"), f"{path}.label_verification.verification_id")
    verified_at = _timestamp(label.get("verified_at"), f"{path}.label_verification.verified_at")
    evidence_path, relative_path, file_sha256 = _resolve_raw_evidence(
        root, label.get("evidence"), f"{path}.label_verification.evidence", failure_code="RECEIPT_BINDING"
    )
    return {
        "verification_id": verification_id,
        "verified_at": verified_at,
        "relative_path": relative_path,
        "file_sha256": file_sha256,
        "size_bytes": evidence_path.stat().st_size,
    }


def _validate_property_record(
    record: Mapping[str, Any], root: Path, path: str
) -> tuple[str, dict[str, Any], str, str]:
    conversion = _mapping(record.get("conversion"), f"{path}.conversion")
    route = conversion.get("route")
    if route not in ROUTES:
        _fail("PROPERTY_ROUTE", f"{path}.conversion.route", "must be an approved conversion route")
    property_path, relative_path, file_sha256 = _resolve_evidence(
        root, conversion.get("property_record_evidence"), f"{path}.conversion.property_record_evidence", failure_code="RECEIPT_BINDING"
    )
    raw, _ = _read_json_snapshot(property_path, trusted_root=root, failure_code="RECEIPT_BINDING", require_sidecar=True)
    property_record = _mapping(raw, f"{path}.conversion.property_record")
    _contains_scope_field(property_record, f"{path}.conversion.property_record")
    if property_record.get("schema_version") != "moocow-conversion-property-record-v2" or property_record.get("record_kind") != "current_lot_conversion_properties":
        _fail("PROPERTY_ROUTE", str(property_path), "is not a current-lot conversion-property record")
    component_id = _text(record.get("component_id"), f"{path}.component_id")
    physical_lot_id = _text(record.get("physical_lot_id"), f"{path}.physical_lot_id")
    if property_record.get("conversion_route") != route:
        _fail("PROPERTY_ROUTE", str(property_path), "does not match the material conversion route")
    if property_record.get("component_id") != component_id or property_record.get("physical_lot_id") != physical_lot_id:
        _fail("PROPERTY_LOT", str(property_path), "does not match the material component and physical lot")
    properties = _mapping(property_record.get("properties"), f"{property_path}.properties")
    required = (
        ("nonvolatile_mass_fraction", "fraction", True),
        ("nonvolatile_density_g_ml", "g/mL", False),
    ) if route == MASS_SOLIDS_NONVOLATILE_DENSITY else (
        ("wet_density_g_ml", "g/mL", False),
        ("component_nonvolatile_volume_fraction", "fraction", True),
    )
    if set(properties) != {item[0] for item in required}:
        _fail("PROPERTY_ROUTE", f"{property_path}.properties", "must contain only fields for its declared route")
    normalized: dict[str, Any] = {}
    for property_name, unit, is_fraction in required:
        item = _mapping(properties[property_name], f"{property_path}.properties.{property_name}")
        if item.get("unit") != unit:
            _fail("PROPERTY_UNIT", f"{property_path}.properties.{property_name}.unit", f"must be {unit}")
        normalized[property_name] = {
            "property_record_id": _text(item.get("property_record_id"), f"{property_path}.properties.{property_name}.property_record_id"),
            "value": _decimal(item.get("value"), f"{property_path}.properties.{property_name}.value", positive=not is_fraction, fraction=is_fraction),
            "unit": unit,
            "method": _text(item.get("method"), f"{property_path}.properties.{property_name}.method"),
            "observed_at": _timestamp(item.get("observed_at"), f"{property_path}.properties.{property_name}.observed_at"),
        }
    return route, normalized, relative_path, file_sha256


def _load_materials(shared_root: Path | str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = Path(shared_root).absolute()
    manifest_path = root / "materials.json"
    payload, manifest_sha = _read_json_snapshot(manifest_path, trusted_root=root, failure_code="RECEIPT_BINDING", require_sidecar=True)
    manifest = _mapping(payload, "materials")
    _contains_scope_field(manifest, "materials")
    if manifest.get("schema_version") != "moocow-pilot-material-lots-v1":
        _fail("MATERIAL_COUNT", "materials.schema_version", "is not supported")
    _validate_component_order(manifest.get("component_order"), "materials.component_order")
    records = _list(manifest.get("materials"), "materials.materials")
    if len(records) != len(COMPONENT_ORDER):
        _fail("MATERIAL_COUNT", "materials.materials", "must contain exactly 15 material records")
    normalized: list[dict[str, Any]] = []
    seen_lots: set[tuple[str, str]] = set()
    seen_label_ids: set[str] = set()
    seen_label_paths: set[str] = set()
    seen_label_hashes: set[str] = set()
    for index, expected in enumerate(COMPONENT_ORDER):
        formula_key, component_id = expected
        record = _mapping(records[index], f"materials.materials[{index}]")
        if record.get("formula_key") != formula_key or record.get("component_id") != component_id:
            if formula_key == "073" or record.get("formula_key") in {73, "73"}:
                _fail("COMPONENT_KEY_073", f"materials.materials[{index}]", "must retain the string formula key 073")
            _fail("COMPONENT_ORDER", f"materials.materials[{index}]", "is not in the fixed component order")
        physical_lot_id = _text(record.get("physical_lot_id"), f"materials.materials[{index}].physical_lot_id")
        lot_key = (component_id, physical_lot_id)
        if lot_key in seen_lots:
            _fail("PROPERTY_LOT", f"materials.materials[{index}].physical_lot_id", "must be unique per component")
        seen_lots.add(lot_key)
        label = _validate_label(record, root, f"materials.materials[{index}]")
        label_identity = (
            label["verification_id"],
            label["relative_path"],
            label["file_sha256"],
        )
        if (
            label_identity[0] in seen_label_ids
            or label_identity[1] in seen_label_paths
            or label_identity[2] in seen_label_hashes
        ):
            _fail(
                "EVIDENCE_RECORD_DUPLICATE",
                f"materials.materials[{index}].label_verification",
                "must bind a unique verification ID, path, and physical-label file",
            )
        seen_label_ids.add(label_identity[0])
        seen_label_paths.add(label_identity[1])
        seen_label_hashes.add(label_identity[2])
        route, properties, property_relative_path, property_file_sha256 = _validate_property_record(record, root, f"materials.materials[{index}]")
        normalized.append(
            {
                "formula_key": formula_key,
                "component_id": component_id,
                "role": "base" if formula_key == "base" else "colorant",
                "physical_lot_id": physical_lot_id,
                "product_name": _text(record.get("product_name"), f"materials.materials[{index}].product_name"),
                "supplier": _text(record.get("supplier"), f"materials.materials[{index}].supplier"),
                "label_verification": label,
                "conversion_route": route,
                "properties": {
                    name: {**item, "value": _decimal_text(item["value"])} for name, item in properties.items()
                },
                "property_evidence": {
                    "relative_path": property_relative_path,
                    "file_sha256": property_file_sha256,
                },
            }
        )
    if len({item["conversion_route"] for item in normalized}) != 1:
        _fail(
            "CONVERSION_ROUTE_MIXED",
            "materials.materials",
            "all 15 current-lot materials must use one declared acquisition conversion route",
        )
    return normalized, {"materials_manifest_sha256": manifest_sha}


def _validate_parent(
    *,
    pilot_design_receipt_path: Path | str,
    design_path: Path | str,
    registry_path: Path | str,
    registry_evidence_root: Path | str,
    diagnostic_receipt_path: Path | str,
    diagnostic_evidence_root: Path | str,
) -> dict[str, Any]:
    try:
        verify_pilot_design_receipt(
            receipt_path=pilot_design_receipt_path,
            design_path=design_path,
            registry_path=registry_path,
            registry_evidence_root=registry_evidence_root,
            diagnostic_receipt_path=diagnostic_receipt_path,
            diagnostic_evidence_root=diagnostic_evidence_root,
        )
        pilot_sha = verify_sha256_sidecar(Path(pilot_design_receipt_path))
    except CalibrationError as error:
        _fail("PILOT_DESIGN_RECEIPT", str(pilot_design_receipt_path), str(error))
    try:
        diagnostic_sha = verify_sha256_sidecar(Path(diagnostic_receipt_path))
    except CalibrationError as error:
        _fail("DIAGNOSTIC_RECEIPT", str(diagnostic_receipt_path), str(error))
    pilot_receipt, _ = _read_json_snapshot(
        Path(pilot_design_receipt_path),
        trusted_root=None,
        failure_code="PILOT_DESIGN_RECEIPT",
        require_sidecar=True,
    )
    try:
        component_lots = _mapping(_mapping(_mapping(pilot_receipt, "pilot_receipt").get("bindings"), "pilot_receipt.bindings").get("registry"), "pilot_receipt.bindings.registry").get("component_lots")
    except AcquisitionPreflightError:
        raise
    component_lots_value = _list(component_lots, "pilot_receipt.bindings.registry.component_lots")
    parent_lots = {
        _text(_mapping(item, "pilot_receipt.bindings.registry.component_lots[]").get("component_id"), "pilot_receipt.bindings.registry.component_lots[].component_id", placeholder=False): _text(
            _mapping(item, "pilot_receipt.bindings.registry.component_lots[]").get("physical_lot_id"),
            "pilot_receipt.bindings.registry.component_lots[].physical_lot_id",
        )
        for item in component_lots_value
    }
    if set(parent_lots) != set(COMPONENT_IDS):
        _fail("PILOT_DESIGN_RECEIPT", str(pilot_design_receipt_path), "does not bind all 15 registry component lots")
    normalized_design_path = Path(pilot_design_receipt_path).with_name("normalized-pilot-design.json")
    normalized_design, normalized_design_sha = _read_json_snapshot(
        normalized_design_path,
        trusted_root=None,
        failure_code="PILOT_DESIGN_RECEIPT",
        require_sidecar=True,
    )
    if normalized_design_sha != _sha256(_mapping(pilot_receipt, "pilot_receipt").get("normalized_artifact_sha256"), "pilot_receipt.normalized_artifact_sha256"):
        _fail("PILOT_DESIGN_RECEIPT", str(normalized_design_path), "does not match the frozen pilot receipt")
    roster = _list(_mapping(normalized_design, "normalized_pilot_design").get("roster"), "normalized_pilot_design.roster")
    roster_by_family: dict[str, dict[str, str]] = {}
    for index, row in enumerate(roster):
        item = _mapping(row, f"normalized_pilot_design.roster[{index}]")
        family_id = _text(item.get("formula_family_id"), f"normalized_pilot_design.roster[{index}].formula_family_id", placeholder=False)
        mapping = {
            "formula_family_id": family_id,
            "formula_id": _text(item.get("formula_id"), f"normalized_pilot_design.roster[{index}].formula_id", placeholder=False),
            "formula_batch_id": _text(item.get("formula_batch_id"), f"normalized_pilot_design.roster[{index}].formula_batch_id", placeholder=False),
            "split": _text(item.get("split"), f"normalized_pilot_design.roster[{index}].split", placeholder=False),
        }
        previous = roster_by_family.get(family_id)
        if previous is not None and previous != mapping:
            _fail("PILOT_DESIGN_RECEIPT", "normalized_pilot_design.roster", "does not have one frozen family/formula/batch mapping")
        roster_by_family[family_id] = mapping
    if set(roster_by_family) != set(TRAIN_FAMILIES + VALIDATION_FAMILIES + HOLDOUT_FAMILIES):
        _fail("PILOT_DESIGN_RECEIPT", "normalized_pilot_design.roster", "does not contain the fixed 20 family mappings")
    open_formula_mapping = [
        roster_by_family[family] for family in TRAIN_FAMILIES + VALIDATION_FAMILIES
    ]
    holdout_formula_mapping = [roster_by_family[family] for family in HOLDOUT_FAMILIES]
    return {
        "pilot_design_receipt_sha256": pilot_sha,
        "diagnostic_receipt_sha256": diagnostic_sha,
        "design_artifact_sha256": sha256_bytes(read_regular_file_snapshot(Path(design_path))[0]),
        "registry_artifact_sha256": sha256_bytes(read_regular_file_snapshot(Path(registry_path))[0]),
        "registry_component_lots": parent_lots,
        "open_formula_mapping_sha256": sha256_bytes(canonical_json_bytes(open_formula_mapping)),
        "holdout_formula_mapping_sha256": sha256_bytes(canonical_json_bytes(holdout_formula_mapping)),
    }


def _verify_common_receipt_sources(receipt: Mapping[str, Any], shared_root: Path | str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    materials, binding = _load_materials(shared_root)
    expected = receipt.get("materials")
    if canonical_json_bytes(materials) != canonical_json_bytes(expected):
        _fail("RECEIPT_BINDING", "common_material_receipt.materials", "does not match the revalidated shared root")
    if receipt.get("shared_source_binding") != binding:
        _fail("RECEIPT_BINDING", "common_material_receipt.shared_source_binding", "does not match the shared root")
    return materials, binding


def preflight_pilot_materials(
    *,
    pilot_design_receipt_path: Path | str,
    design_path: Path | str,
    registry_path: Path | str,
    registry_evidence_root: Path | str,
    diagnostic_receipt_path: Path | str,
    diagnostic_evidence_root: Path | str,
    shared_root: Path | str,
    output_dir: Path | str,
) -> dict[str, Any]:
    """Publish a verified 15-current-lot receipt without admitting spectra."""

    parent = _validate_parent(
        pilot_design_receipt_path=pilot_design_receipt_path,
        design_path=design_path,
        registry_path=registry_path,
        registry_evidence_root=registry_evidence_root,
        diagnostic_receipt_path=diagnostic_receipt_path,
        diagnostic_evidence_root=diagnostic_evidence_root,
    )
    materials, source_binding = _load_materials(shared_root)
    for material in materials:
        if parent["registry_component_lots"][material["component_id"]] != material["physical_lot_id"]:
            _fail(
                "PROPERTY_LOT",
                f"materials.{material['component_id']}.physical_lot_id",
                "does not match the frozen pilot registry lot",
            )

    def build(staging: Path) -> dict[str, Any]:
        receipt = _receipt_payload(
            {
                "schema_version": "moocow-common-material-receipt-v1",
                "status": "common_materials_verified",
                "state": "COMMON_MATERIALS_VERIFIED",
                "parent_bindings": parent,
                "shared_source_binding": source_binding,
                "materials": materials,
                **_permissions(),
            }
        )
        digest = write_json_with_sha256(staging / "common-material-receipt.json", receipt)
        return {"status": receipt["status"], "state": receipt["state"], "common_material_receipt_sha256": digest, **_permissions()}

    def verify(staging: Path) -> None:
        receipt, _ = _verify_receipt(staging / "common-material-receipt.json", schema_version="moocow-common-material-receipt-v1", status="common_materials_verified")
        _verify_common_receipt_sources(receipt, shared_root)

    return _publish(output_dir, build, verify)


def _load_common_receipt(path: Path | str, *, shared_root: Path | str | None = None) -> tuple[dict[str, Any], str]:
    receipt, digest = _verify_receipt(Path(path), schema_version="moocow-common-material-receipt-v1", status="common_materials_verified")
    if receipt.get("state") != "COMMON_MATERIALS_VERIFIED":
        _fail("RECEIPT_BINDING", str(path), "has an invalid material receipt state")
    if shared_root is not None:
        _verify_common_receipt_sources(receipt, shared_root)
    return receipt, digest


def _load_batch_manifest(root: Path | str, *, split_kind: str) -> tuple[list[Mapping[str, Any]], str]:
    directory = Path(root).absolute()
    value, digest = _read_json_snapshot(directory / "batches.json", trusted_root=directory, failure_code="RECEIPT_BINDING", require_sidecar=True)
    manifest = _mapping(value, "batches")
    if manifest.get("schema_version") != "moocow-pilot-batches-v1":
        _fail("RECEIPT_BINDING", "batches.schema_version", "is not supported")
    batches = _list(manifest.get("batches"), "batches.batches")
    _contains_scope_field(batches, "batches.batches")
    identifiers = json.dumps(batches, ensure_ascii=False)
    if split_kind == "open" and ("FAM-HO-" in identifiers or "holdout" in identifiers.casefold()):
        _fail("OPEN_ROOT_SCOPE", "batches.batches", "open roots must not contain sealed-holdout identities")
    return [_mapping(item, f"batches.batches[{index}]") for index, item in enumerate(batches)], digest


def _actual_weighing(
    *,
    root: Path,
    locator: object,
    batch: Mapping[str, Any],
    path: str,
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], str, str]:
    evidence_path, relative_path, file_sha256 = _resolve_evidence(root, locator, path, failure_code="RECEIPT_BINDING")
    raw, _ = _read_json_snapshot(evidence_path, trusted_root=root, failure_code="RECEIPT_BINDING", require_sidecar=True)
    record = _mapping(raw, str(evidence_path))
    _contains_scope_field(record, str(evidence_path))
    if record.get("schema_version") != "moocow-actual-weighing-record-v2" or record.get("record_kind") != "actual_weighing_observation":
        _fail("ACTUAL_WEIGHING_KIND", str(evidence_path), "must be a canonical actual-weighing observation")
    if record.get("formula_batch_id") != batch.get("formula_batch_id") or record.get("formula_id") != batch.get("formula_id"):
        _fail("WEIGHING_BATCH", str(evidence_path), "does not match its formula batch and formula")
    events_by_component: dict[tuple[str, str], list[dict[str, Any]]] = {}
    event_ids: set[str] = set()
    for index, raw_entry in enumerate(_list(record.get("entries"), f"{evidence_path}.entries")):
        entry = _mapping(raw_entry, f"{evidence_path}.entries[{index}]")
        event_id = _text(entry.get("weighing_event_id"), f"{evidence_path}.entries[{index}].weighing_event_id")
        if event_id in event_ids:
            _fail("WEIGHING_EVENT_DUPLICATE", f"{evidence_path}.entries[{index}].weighing_event_id", "is duplicated")
        event_ids.add(event_id)
        if entry.get("actual_wet_mass_unit") != "g":
            _fail("PROPERTY_UNIT", f"{evidence_path}.entries[{index}].actual_wet_mass_unit", "must be g")
        normalized = {
            "weighing_event_id": event_id,
            "weighing_record_id": _text(entry.get("weighing_record_id"), f"{evidence_path}.entries[{index}].weighing_record_id"),
            "component_id": _text(entry.get("component_id"), f"{evidence_path}.entries[{index}].component_id"),
            "physical_lot_id": _text(entry.get("physical_lot_id"), f"{evidence_path}.entries[{index}].physical_lot_id"),
            "actual_wet_mass_g": _decimal(entry.get("actual_wet_mass_g"), f"{evidence_path}.entries[{index}].actual_wet_mass_g", positive=True),
            "weighed_at": _timestamp(entry.get("weighed_at"), f"{evidence_path}.entries[{index}].weighed_at"),
            "weighing_method": _text(entry.get("weighing_method"), f"{evidence_path}.entries[{index}].weighing_method"),
        }
        events_by_component.setdefault((normalized["component_id"], normalized["physical_lot_id"]), []).append(normalized)
    return events_by_component, relative_path, file_sha256


def _family_support(family_id: str) -> set[str]:
    if family_id == "FAM-TR-BASIS-BASE":
        return {"base-waterborne-clear"}
    if family_id.startswith("FAM-TR-BASIS-"):
        key = family_id.removeprefix("FAM-TR-BASIS-")
        if key in COMPONENT_BY_KEY:
            return {"base-waterborne-clear", COMPONENT_BY_KEY[key]}
    return set()


def _expected_formula_id(family_id: str) -> str:
    return f"FORM-{family_id.removeprefix('FAM-')}"


def _derive_batch(
    batch: Mapping[str, Any],
    *,
    material_by_component: Mapping[str, Mapping[str, Any]],
    evidence_root: Path,
    seen_event_ids: dict[str, tuple[str, str]],
    expected_split: str,
) -> dict[str, Any]:
    required = {"schema_version", "formula_batch_id", "formula_family_id", "formula_id", "split", "actual_components", "actual_nv_vector_component_order", "actual_nv_vector", "actual_nv_sum"}
    if set(batch) - required:
        _contains_scope_field(batch, "batch")
    if batch.get("schema_version") != "moocow-pilot-formula-batch-v1":
        _fail("RECEIPT_BINDING", "batch.schema_version", "is not supported")
    family_id = _text(batch.get("formula_family_id"), "batch.formula_family_id")
    formula_id = _text(batch.get("formula_id"), "batch.formula_id")
    batch_id = _text(batch.get("formula_batch_id"), "batch.formula_batch_id")
    if batch.get("split") != expected_split:
        _fail("FORMULA_FAMILY_BATCH", "batch.split", "does not match the expected split")
    if formula_id != _expected_formula_id(family_id):
        _fail("FORMULA_FAMILY_BATCH", "batch.formula_id", "does not match the preregistered family")
    _validate_component_order(
        [{"formula_key": item, "component_id": COMPONENT_BY_KEY.get(item)} for item in _list(batch.get("actual_nv_vector_component_order"), "batch.actual_nv_vector_component_order")],
        "batch.actual_nv_vector_component_order",
    )
    components = _list(batch.get("actual_components"), "batch.actual_components")
    if not components:
        _fail("BASIS_SUPPORT", "batch.actual_components", "must contain actual positive components")
    derived: dict[str, dict[str, Any]] = {}
    routes: set[str] = set()
    referenced_event_groups: dict[
        tuple[str, str], dict[tuple[str, str], list[dict[str, Any]]]
    ] = {}
    consumed_event_ids: set[str] = set()
    for index, raw_component in enumerate(components):
        item = _mapping(raw_component, f"batch.actual_components[{index}]")
        component_id = _text(item.get("component_id"), f"batch.actual_components[{index}].component_id")
        if component_id in derived:
            _fail("FORMULA_BATCH_DUPLICATE", f"batch.actual_components[{index}].component_id", "must occur once; additions belong in its weighing evidence")
        material = material_by_component.get(component_id)
        if material is None:
            _fail("COMPONENT_ORDER", f"batch.actual_components[{index}].component_id", "is not a registered component")
        lot_id = _text(item.get("physical_lot_id"), f"batch.actual_components[{index}].physical_lot_id")
        if lot_id != material["physical_lot_id"]:
            _fail("WEIGHING_LOT", f"batch.actual_components[{index}].physical_lot_id", "does not match the verified material lot")
        if item.get("conversion_route") != material["conversion_route"]:
            _fail("PROPERTY_ROUTE", f"batch.actual_components[{index}].conversion_route", "does not match the verified material route")
        events, evidence_path, evidence_sha = _actual_weighing(
            root=evidence_root,
            locator=item.get("actual_weighing_evidence"),
            batch=batch,
            path=f"batch.actual_components[{index}].actual_weighing_evidence",
        )
        referenced_event_groups[(evidence_path, evidence_sha)] = events
        matching = events.get((component_id, lot_id), [])
        if not matching:
            _fail("WEIGHING_LOT", f"batch.actual_components[{index}].actual_weighing_evidence", "contains no matching component and lot addition")
        wet_mass = sum((event["actual_wet_mass_g"] for event in matching), Decimal(0))
        for event in matching:
            previous = seen_event_ids.get(event["weighing_event_id"])
            location = (batch_id, component_id)
            if previous is not None and previous != location:
                _fail("WEIGHING_EVENT_REUSE", f"batch.actual_components[{index}].actual_weighing_evidence", "reuses an event from another batch or component")
            seen_event_ids[event["weighing_event_id"]] = location
            consumed_event_ids.add(event["weighing_event_id"])
        route = material["conversion_route"]
        routes.add(route)
        properties = material["properties"]
        if route == MASS_SOLIDS_NONVOLATILE_DENSITY:
            volume = wet_mass * Decimal(properties["nonvolatile_mass_fraction"]["value"]) / Decimal(properties["nonvolatile_density_g_ml"]["value"])
        else:
            volume = wet_mass / Decimal(properties["wet_density_g_ml"]["value"]) * Decimal(properties["component_nonvolatile_volume_fraction"]["value"])
        persisted_mass = _decimal(item.get("actual_wet_mass_g"), f"batch.actual_components[{index}].actual_wet_mass_g", positive=True)
        persisted_volume = _decimal(item.get("nonvolatile_volume_ml"), f"batch.actual_components[{index}].nonvolatile_volume_ml", positive=True)
        if persisted_mass != wet_mass or persisted_volume != volume:
            _fail("ACTUAL_NV_MISMATCH", f"batch.actual_components[{index}]", "does not equal the recomputed weighing/property evidence")
        derived[component_id] = {
            "component_id": component_id,
            "physical_lot_id": lot_id,
            "conversion_route": route,
            "actual_wet_mass_g": wet_mass,
            "nonvolatile_volume_ml": volume,
            "weighing_event_ids": [event["weighing_event_id"] for event in matching],
            "weighing_evidence": {"relative_path": evidence_path, "file_sha256": evidence_sha},
            "property_record_ids": sorted(value["property_record_id"] for value in properties.values()),
            "property_evidence_sha256": material["property_evidence"]["file_sha256"],
        }
    allowed_pairs = {
        (component_id, item["physical_lot_id"])
        for component_id, item in derived.items()
    }
    for events_by_component in referenced_event_groups.values():
        for pair, events in events_by_component.items():
            if pair not in allowed_pairs:
                code = "BASIS_SUPPORT" if pair[0] not in derived else "WEIGHING_LOT"
                _fail(code, "batch.actual_components", "weighing evidence contains an unbound positive component or lot")
            if any(event["weighing_event_id"] not in consumed_event_ids for event in events):
                _fail(
                    "RECEIPT_BINDING",
                    "batch.actual_components",
                    "weighing evidence contains an event not bound by the formula components",
                )
    if len(routes) != 1:
        _fail("CONVERSION_ROUTE_MIXED", "batch.actual_components", "positive components must use one conversion route")
    volume_sum = sum((item["nonvolatile_volume_ml"] for item in derived.values()), Decimal(0))
    if volume_sum <= 0:
        _fail("POSITIVE_NUMBER", "batch.actual_components", "must have positive total nonvolatile volume")
    vector = [derived.get(component_id, {}).get("nonvolatile_volume_ml", Decimal(0)) / volume_sum for component_id in COMPONENT_IDS]
    persisted_vector = [_decimal(value, f"batch.actual_nv_vector[{index}]") for index, value in enumerate(_list(batch.get("actual_nv_vector"), "batch.actual_nv_vector"))]
    if len(persisted_vector) != len(COMPONENT_IDS) or persisted_vector != vector:
        _fail("ACTUAL_NV_MISMATCH", "batch.actual_nv_vector", "does not equal the recomputed actual-NV vector")
    persisted_sum = _decimal(batch.get("actual_nv_sum"), "batch.actual_nv_sum")
    if persisted_sum != sum(vector, Decimal(0)):
        _fail("ACTUAL_NV_MISMATCH", "batch.actual_nv_sum", "does not equal the recomputed actual-NV sum")
    if expected_split == "train":
        support = _family_support(family_id)
        if not support or set(derived) != support:
            _fail("BASIS_SUPPORT", "batch.actual_components", "does not match the required train-basis support")
    return {
        "formula_family_id": family_id,
        "formula_id": formula_id,
        "formula_batch_id": batch_id,
        "split": expected_split,
        "conversion_route": next(iter(routes)),
        "components": [
            {
                "component_id": item["component_id"],
                "physical_lot_id": item["physical_lot_id"],
                "conversion_route": item["conversion_route"],
                "actual_wet_mass_g_decimal": _decimal_text(item["actual_wet_mass_g"]),
                "nonvolatile_volume_ml_decimal": _decimal_text(item["nonvolatile_volume_ml"]),
                "weighing_event_ids": item["weighing_event_ids"],
                "weighing_evidence": item["weighing_evidence"],
                "property_record_ids": item["property_record_ids"],
                "property_evidence_sha256": item["property_evidence_sha256"],
            }
            for item in (derived.get(component_id) for component_id in COMPONENT_IDS)
            if item is not None
        ],
        "actual_nv_vector": [_decimal_text(value) for value in vector],
        "actual_nv_sum": _decimal_text(sum(vector, Decimal(0))),
    }


def diagnose_actual_nv_rank(matrix: Sequence[Sequence[float]]) -> dict[str, Any]:
    """Return the fixed float64 SVD diagnostic without applying any condition ceiling."""
    values = np.asarray(matrix, dtype=np.float64)
    if values.shape != (15, 15):
        _fail("RANK_SCOPE", "matrix", "must be exactly 15 by 15 actual train-basis rows")
    singular_values = np.linalg.svd(values, compute_uv=False)
    sigma_max = float(singular_values[0])
    tolerance = max(values.shape) * float.fromhex("0x1.0000000000000p-52") * sigma_max
    numerical_rank = int(np.count_nonzero(singular_values > tolerance))
    sigma_min = float(singular_values[-1])
    finite = sigma_min != 0.0
    condition: float | str = float(sigma_max / sigma_min) if finite else "infinite"
    return {
        "dtype": "IEEE-754-binary64",
        "machine_epsilon": float.fromhex("0x1.0000000000000p-52").hex(),
        "tolerance_rule": "max(m,n)*eps*sigma_max",
        "strict_comparison": "sigma_i > tolerance",
        "tolerance_float64_hex": float(tolerance).hex(),
        "singular_values_float64_hex": [float(value).hex() for value in singular_values],
        "numerical_rank": numerical_rank,
        "condition_number": float(condition).hex() if finite else condition,
        "condition_number_is_finite": finite,
    }


def _rank_receipt(train_batches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = {batch["formula_family_id"]: batch for batch in train_batches}
    if set(ordered) != set(TRAIN_FAMILIES) or len(ordered) != len(TRAIN_FAMILIES):
        _fail("RANK_SCOPE", "train_batches", "must contain only the 15 fixed train-basis families")
    rows = [ordered[family] for family in TRAIN_FAMILIES]
    matrix = [[float(Decimal(value)) for value in row["actual_nv_vector"]] for row in rows]
    diagnostics = diagnose_actual_nv_rank(matrix)
    if diagnostics["numerical_rank"] != 15:
        _fail("RANK_DEFICIENT", "actual_nv_matrix", "does not have numerical rank 15 under the fixed float64 tolerance")
    result_rows: list[dict[str, Any]] = []
    for row, float_row in zip(rows, matrix, strict=True):
        components = {item["component_id"]: item for item in row["components"]}
        values = []
        for component_id, decimal_value, float_value in zip(COMPONENT_IDS, row["actual_nv_vector"], float_row, strict=True):
            component = components.get(component_id)
            values.append(
                {
                    "component_id": component_id,
                    "actual_wet_mass_g_decimal": component["actual_wet_mass_g_decimal"] if component else "0",
                    "nonvolatile_volume_ml_decimal": component["nonvolatile_volume_ml_decimal"] if component else "0",
                    "x_decimal": decimal_value,
                    "x_float64_hex": float(float_value).hex(),
                    "weighing_event_ids": component["weighing_event_ids"] if component else [],
                    "property_record_ids": component["property_record_ids"] if component else [],
                    "evidence_sha256": component["weighing_evidence"]["file_sha256"] if component else None,
                }
            )
        result_rows.append({
            "formula_family_id": row["formula_family_id"],
            "formula_id": row["formula_id"],
            "formula_batch_id": row["formula_batch_id"],
            "conversion_route": row["conversion_route"],
            "components": values,
        })
    matrix_hex = [[float(value).hex() for value in row] for row in matrix]
    return _receipt_payload(
        {
            "schema_version": "moocow-actual-nv-rank-receipt-v1",
            "status": "actual_nv_rank_verified",
            "analysis_scope": "train_design_only_pre_spectra",
            "component_order": [{"formula_key": key, "component_id": component_id} for key, component_id in COMPONENT_ORDER],
            "formula_family_order": list(TRAIN_FAMILIES),
            "matrix_shape": [15, 15],
            "matrix_float64_hex": matrix_hex,
            "matrix_sha256": sha256_bytes(canonical_json_bytes(matrix_hex)),
            "rows": result_rows,
            "rank_method": diagnostics,
            **_permissions(),
        }
    )


def _skeleton(batch: Mapping[str, Any]) -> list[dict[str, Any]]:
    bands = ("DFT-L", "DFT-H") if batch["split"] == "train" else ("DFT-L", "DFT-M", "DFT-H")
    suffix = batch["formula_family_id"].removeprefix("FAM-")
    cards = []
    for band in bands:
        cards.append(
            {
                "card_id": f"CARD-{suffix}-{band}-001",
                "formula_family_id": batch["formula_family_id"],
                "formula_id": batch["formula_id"],
                "formula_batch_id": batch["formula_batch_id"],
                "split": batch["split"],
                "dft_band": band,
                "primary_reading_slots": [
                    {"backing": backing, "reposition_id": position}
                    for backing in ("black", "white")
                    for position in ("POS01", "POS02", "POS03")
                ],
            }
        )
    return cards


def _validate_batch_collection(
    *,
    batches: Sequence[Mapping[str, Any]],
    materials: Sequence[Mapping[str, Any]],
    evidence_root: Path | str,
    split_kind: str,
    frozen_formula_mapping_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    expected_counts = {"open": {"train": 15, "validation": 2, "total": 17}, "holdout": {"holdout": 3, "total": 3}}[split_kind]
    if len(batches) != expected_counts["total"]:
        _fail("OPEN_BATCH_COUNT" if split_kind == "open" else "HOLDOUT_BATCH_COUNT", "batches.batches", f"must contain exactly {expected_counts['total']} batches")
    material_by_component = {item["component_id"]: item for item in materials}
    frozen_mapping_sha256 = _sha256(
        frozen_formula_mapping_sha256,
        f"{split_kind}_formula_mapping_sha256",
    )
    family_order = TRAIN_FAMILIES + VALIDATION_FAMILIES if split_kind == "open" else HOLDOUT_FAMILIES
    identity_by_family: dict[str, dict[str, str]] = {}
    identity_batch_ids: set[str] = set()
    for index, raw in enumerate(batches):
        identity_path = f"batches.batches[{index}]"
        split = _text(raw.get("split"), f"{identity_path}.split", placeholder=False)
        if split_kind == "open" and split not in {"train", "validation"}:
            _fail("OPEN_ROOT_SCOPE", f"{identity_path}.split", "must be train or validation")
        if split_kind == "holdout" and split != "holdout":
            _fail("HOLDOUT_BATCH_COUNT", f"{identity_path}.split", "must be holdout")
        family_id = _text(raw.get("formula_family_id"), f"{identity_path}.formula_family_id", placeholder=False)
        formula_id = _text(raw.get("formula_id"), f"{identity_path}.formula_id", placeholder=False)
        batch_id = _text(raw.get("formula_batch_id"), f"{identity_path}.formula_batch_id", placeholder=False)
        if family_id in identity_by_family:
            _fail("FORMULA_FAMILY_DUPLICATE", f"{identity_path}.formula_family_id", "is duplicated")
        if batch_id in identity_batch_ids:
            _fail("FORMULA_BATCH_DUPLICATE", f"{identity_path}.formula_batch_id", "is duplicated")
        identity_batch_ids.add(batch_id)
        identity_by_family[family_id] = {
            "formula_family_id": family_id,
            "formula_id": formula_id,
            "formula_batch_id": batch_id,
            "split": split,
        }
    if set(identity_by_family) != set(family_order):
        _fail("FORMULA_FAMILY_BATCH", "batches.batches", "does not match the preregistered family roster")
    identity_mapping = [identity_by_family[family] for family in family_order]
    if sha256_bytes(canonical_json_bytes(identity_mapping)) != frozen_mapping_sha256:
        _fail(
            "FORMULA_FAMILY_BATCH",
            "batches.batches",
            "does not match the frozen pilot family/formula/batch commitment",
        )
    evidence_directory = Path(evidence_root).absolute()
    seen_batch_ids: set[str] = set()
    seen_families: set[str] = set()
    seen_event_ids: dict[str, tuple[str, str]] = {}
    normalized: list[dict[str, Any]] = []
    for raw in batches:
        split = raw.get("split")
        if split_kind == "open" and split not in {"train", "validation"}:
            _fail("OPEN_ROOT_SCOPE", "batch.split", "must be train or validation")
        if split_kind == "holdout" and split != "holdout":
            _fail("HOLDOUT_BATCH_COUNT", "batch.split", "must be holdout")
        batch = _derive_batch(
            raw,
            material_by_component=material_by_component,
            evidence_root=evidence_directory,
            seen_event_ids=seen_event_ids,
            expected_split=str(split),
        )
        if batch["formula_batch_id"] in seen_batch_ids:
            _fail("FORMULA_BATCH_DUPLICATE", "batch.formula_batch_id", "is duplicated")
        if batch["formula_family_id"] in seen_families:
            _fail("FORMULA_FAMILY_DUPLICATE", "batch.formula_family_id", "is duplicated")
        seen_batch_ids.add(batch["formula_batch_id"])
        seen_families.add(batch["formula_family_id"])
        normalized.append(batch)
    expected_families = set(TRAIN_FAMILIES + VALIDATION_FAMILIES) if split_kind == "open" else set(HOLDOUT_FAMILIES)
    if seen_families != expected_families:
        _fail("FORMULA_FAMILY_BATCH", "batches.batches", "does not match the preregistered family roster")
    normalized_by_family = {batch["formula_family_id"]: batch for batch in normalized}
    observed_mapping = [
        {
            "formula_family_id": normalized_by_family[family]["formula_family_id"],
            "formula_id": normalized_by_family[family]["formula_id"],
            "formula_batch_id": normalized_by_family[family]["formula_batch_id"],
            "split": normalized_by_family[family]["split"],
        }
        for family in family_order
    ]
    if sha256_bytes(canonical_json_bytes(observed_mapping)) != frozen_mapping_sha256:
        _fail(
            "FORMULA_FAMILY_BATCH",
            "batches.batches",
            "does not match the frozen pilot family/formula/batch commitment",
        )
    cards = [card for batch in normalized for card in _skeleton(batch)]
    slots = sum(len(card["primary_reading_slots"]) for card in cards)
    expected_cards = 36 if split_kind == "open" else 9
    expected_slots = 216 if split_kind == "open" else 54
    if len(cards) != expected_cards:
        _fail("SKELETON_CARD_COUNT", "card_skeleton", "does not match the fixed roster")
    if slots != expected_slots:
        _fail("SKELETON_SLOT_COUNT", "card_skeleton", "does not match the fixed roster")
    return normalized, {"families": len(normalized), "batches": len(normalized), "cards": len(cards), "primary_reading_slots": slots}


def preflight_open_batches(
    *,
    materials_receipt_path: Path | str,
    open_batch_root: Path | str,
    open_evidence_root: Path | str,
    output_dir: Path | str,
) -> dict[str, Any]:
    """Publish open-only batch and actual-NV rank receipts; holdout is unaccepted."""
    materials_receipt, materials_sha = _load_common_receipt(materials_receipt_path)
    batches, batches_sha = _load_batch_manifest(open_batch_root, split_kind="open")
    normalized, counts = _validate_batch_collection(
        batches=batches,
        materials=_list(materials_receipt.get("materials"), "common_material_receipt.materials"),
        evidence_root=open_evidence_root,
        split_kind="open",
        frozen_formula_mapping_sha256=_mapping(
            materials_receipt.get("parent_bindings"),
            "common_material_receipt.parent_bindings",
        ).get("open_formula_mapping_sha256"),
    )
    train_batches = [batch for batch in normalized if batch["split"] == "train"]
    validation_batches = [batch for batch in normalized if batch["split"] == "validation"]
    rank_receipt = _rank_receipt(train_batches)
    skeleton = [card for batch in normalized for card in _skeleton(batch)]
    normalized_materials_sha = sha256_bytes(canonical_json_bytes(materials_receipt["materials"]))
    normalized_batches_sha = sha256_bytes(canonical_json_bytes(normalized))
    open_formula_mapping_sha256 = _sha256(
        _mapping(
            materials_receipt.get("parent_bindings"),
            "common_material_receipt.parent_bindings",
        ).get("open_formula_mapping_sha256"),
        "common_material_receipt.parent_bindings.open_formula_mapping_sha256",
    )

    def build(staging: Path) -> dict[str, Any]:
        rank_sha = write_json_with_sha256(staging / "actual-nv-rank-receipt.json", rank_receipt)
        open_receipt = _receipt_payload(
            {
                "schema_version": "moocow-open-batch-preflight-receipt-v1",
                "status": "open_batch_preflight_verified",
                "state": "OPEN_BATCH_PREFLIGHT_VERIFIED",
                "common_material_receipt_sha256": materials_sha,
                "open_batch_manifest_sha256": batches_sha,
                "actual_nv_rank_receipt_sha256": rank_sha,
                "open_counts": {
                    "train": {"families": len(train_batches), "batches": len(train_batches), "cards": 30, "primary_reading_slots": 180},
                    "validation": {"families": len(validation_batches), "batches": len(validation_batches), "cards": 6, "primary_reading_slots": 36},
                    "total": counts,
                },
                "batch_source_binding": {
                    "materials_manifest_sha256": materials_receipt["shared_source_binding"]["materials_manifest_sha256"],
                    "open_batch_manifest_sha256": batches_sha,
                    "normalized_materials_sha256": normalized_materials_sha,
                    "normalized_open_batches_sha256": normalized_batches_sha,
                    "actual_nv_rank_payload_sha256": rank_receipt["receipt_payload_sha256"],
                    "open_formula_mapping_sha256": open_formula_mapping_sha256,
                },
                "batches": normalized,
                "card_skeleton": skeleton,
                **_permissions(),
            }
        )
        open_sha = write_json_with_sha256(staging / "open-batch-preflight-receipt.json", open_receipt)
        return {"status": open_receipt["status"], "state": open_receipt["state"], "actual_nv_rank_receipt_sha256": rank_sha, "open_batch_preflight_receipt_sha256": open_sha, **_permissions()}

    def verify(staging: Path) -> None:
        rank, _ = _verify_receipt(staging / "actual-nv-rank-receipt.json", schema_version="moocow-actual-nv-rank-receipt-v1", status="actual_nv_rank_verified")
        if rank["rank_method"]["numerical_rank"] != 15:
            _fail("RANK_DEFICIENT", "actual_nv_rank_receipt", "must retain rank 15")
        receipt, _ = _verify_receipt(staging / "open-batch-preflight-receipt.json", schema_version="moocow-open-batch-preflight-receipt-v1", status="open_batch_preflight_verified")
        if "FAM-HO-" in canonical_json_bytes(receipt).decode("utf-8"):
            _fail("OPEN_HOLDOUT_LEAKAGE", "open_batch_preflight_receipt", "must not contain holdout raw data")

    return _publish(output_dir, build, verify)


def commit_holdout_custody(
    *,
    materials_receipt_path: Path | str,
    open_batch_receipt_path: Path | str,
    sealed_holdout_batch_root: Path | str,
    sealed_evidence_root: Path | str,
    custody_identity: str,
    custody_key_fingerprint: str,
    signature_metadata: Mapping[str, Any],
    output_dir: Path | str,
) -> dict[str, Any]:
    """Validate sealed records privately and publish only aggregate custody data."""
    materials_receipt, materials_sha = _load_common_receipt(materials_receipt_path)
    open_receipt, open_receipt_sha = _verify_receipt(
        Path(open_batch_receipt_path),
        schema_version="moocow-open-batch-preflight-receipt-v1",
        status="open_batch_preflight_verified",
    )
    _assert_open_has_no_holdout(open_receipt)
    if open_receipt.get("common_material_receipt_sha256") != materials_sha:
        _fail(
            "RECEIPT_BINDING",
            "open_batch_receipt.common_material_receipt_sha256",
            "does not bind the supplied common-material receipt",
        )
    batches, batches_sha = _load_batch_manifest(sealed_holdout_batch_root, split_kind="holdout")
    normalized_holdout, counts = _validate_batch_collection(
        batches=batches,
        materials=_list(materials_receipt.get("materials"), "common_material_receipt.materials"),
        evidence_root=sealed_evidence_root,
        split_kind="holdout",
        frozen_formula_mapping_sha256=_mapping(
            materials_receipt.get("parent_bindings"),
            "common_material_receipt.parent_bindings",
        ).get("holdout_formula_mapping_sha256"),
    )
    open_event_ids = {
        _text(event_id, "open_batch_receipt.batches[].components[].weighing_event_ids[]", placeholder=False)
        for batch in _list(open_receipt.get("batches"), "open_batch_receipt.batches")
        for component in _list(
            _mapping(batch, "open_batch_receipt.batches[]").get("components"),
            "open_batch_receipt.batches[].components",
        )
        for event_id in _list(
            _mapping(component, "open_batch_receipt.batches[].components[]").get("weighing_event_ids"),
            "open_batch_receipt.batches[].components[].weighing_event_ids",
        )
    }
    sealed_event_ids = {
        event_id
        for batch in normalized_holdout
        for component in batch["components"]
        for event_id in component["weighing_event_ids"]
    }
    if open_event_ids.intersection(sealed_event_ids):
        _fail(
            "CROSS_SPLIT_ID",
            "sealed_holdout.weighing_event_ids",
            "a sealed weighing event reuses an identity already bound to the open receipt",
        )
    custody = {
        "custody_identity": _text(custody_identity, "custody_identity"),
        "custody_key_fingerprint": _text(custody_key_fingerprint, "custody_key_fingerprint"),
        "signature_metadata": dict(_mapping(signature_metadata, "signature_metadata")),
    }
    _assert_commitment_metadata_is_public(custody["signature_metadata"])

    def build(staging: Path) -> dict[str, Any]:
        receipt = _receipt_payload(
            {
                "schema_version": "moocow-holdout-custody-commitment-v1",
                "status": "holdout_custody_committed",
                "state": "HOLDOUT_CUSTODY_COMMITTED",
                "pilot_design_commitment_sha256": materials_receipt["parent_bindings"]["pilot_design_receipt_sha256"],
                "open_batch_preflight_receipt_sha256": open_receipt_sha,
                "sealed_batch_manifest_sha256": batches_sha,
                "counts": counts,
                "custody": custody,
                **_permissions(),
            }
        )
        digest = write_json_with_sha256(staging / "holdout-custody-commitment.json", receipt)
        return {"status": receipt["status"], "state": receipt["state"], "holdout_custody_commitment_sha256": digest, **_permissions()}

    def verify(staging: Path) -> None:
        receipt, _ = _verify_receipt(staging / "holdout-custody-commitment.json", schema_version="moocow-holdout-custody-commitment-v1", status="holdout_custody_committed")
        raw = canonical_json_bytes(receipt).decode("utf-8").casefold()
        forbidden = ("formula_batch_id", "formula_family_id", "card_id", "actual_wet_mass", "nonvolatile_volume", "actual_nv", "relative_path", "file_sha256", "dft", "reflectance", "measurement")
        if any(marker in raw for marker in forbidden):
            _fail("OPEN_HOLDOUT_LEAKAGE", "holdout_custody_commitment", "must not publish raw holdout evidence")

    return _publish(output_dir, build, verify)


def _assert_open_has_no_holdout(value: object, path: str = "open_receipt") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) != "holdout_release_permitted" and ("holdout" in str(key).casefold() or "sealed" in str(key).casefold()):
                _fail("OPEN_HOLDOUT_LEAKAGE", f"{path}.{key}", "open receipt contains a prohibited holdout field")
            _assert_open_has_no_holdout(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_open_has_no_holdout(item, f"{path}[{index}]")
    elif isinstance(value, str) and ("FAM-HO-" in value or "sealed-holdout" in value.casefold()):
        _fail("OPEN_HOLDOUT_LEAKAGE", path, "open receipt contains a prohibited holdout value")


def _assert_commitment_metadata_is_public(value: object, path: str = "signature_metadata") -> None:
    forbidden = ("formula_batch", "formula_family", "card_id", "actual_wet_mass", "nonvolatile_volume", "actual_nv", "property", "weighing", "relative_path", "file_sha256", "dft", "reflectance", "measurement", "spectrum", "raw_reading")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if any(marker in str(key).casefold() for marker in forbidden):
                _fail("OPEN_HOLDOUT_LEAKAGE", f"{path}.{key}", "custody metadata must not contain raw holdout evidence")
            _assert_commitment_metadata_is_public(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_commitment_metadata_is_public(item, f"{path}[{index}]")
    elif isinstance(value, str) and ("FAM-HO-" in value or "sealed-holdout" in value.casefold()):
        _fail("OPEN_HOLDOUT_LEAKAGE", path, "custody metadata must not contain raw holdout identifiers")


def assemble_acquisition_preflight(
    *,
    open_batch_receipt_path: Path | str,
    holdout_custody_commitment_path: Path | str,
    output_dir: Path | str,
) -> dict[str, Any]:
    """Bind open receipt and opaque custody commitment into a still-disabled receipt."""
    open_receipt, open_sha = _verify_receipt(Path(open_batch_receipt_path), schema_version="moocow-open-batch-preflight-receipt-v1", status="open_batch_preflight_verified")
    custody, custody_sha = _verify_receipt(Path(holdout_custody_commitment_path), schema_version="moocow-holdout-custody-commitment-v1", status="holdout_custody_committed")
    _assert_open_has_no_holdout(open_receipt)
    if custody.get("open_batch_preflight_receipt_sha256") != open_sha:
        _fail(
            "RECEIPT_BINDING",
            "holdout_custody_commitment.open_batch_preflight_receipt_sha256",
            "does not bind the supplied open-batch receipt used for cross-split checking",
        )
    if open_receipt.get("state") != "OPEN_BATCH_PREFLIGHT_VERIFIED" or custody.get("state") != "HOLDOUT_CUSTODY_COMMITTED":
        _fail("RECEIPT_BINDING", "receipt.state", "does not allow acquisition-preflight assembly")

    def build(staging: Path) -> dict[str, Any]:
        receipt = _receipt_payload(
            {
                "schema_version": "moocow-acquisition-preflight-receipt-v1",
                "status": "acquisition_preflight_ready",
                "state": "ACQUISITION_PREFLIGHT_READY",
                "open_batch_preflight_receipt_sha256": open_sha,
                "holdout_custody_commitment_sha256": custody_sha,
                "public_counts": {
                    "open": open_receipt["open_counts"]["total"],
                    "holdout": custody["counts"],
                },
                "open_source_binding": open_receipt["batch_source_binding"],
                **_permissions(),
            }
        )
        digest = write_json_with_sha256(staging / "acquisition-preflight-receipt.json", receipt)
        return {"status": receipt["status"], "state": receipt["state"], "acquisition_preflight_receipt_sha256": digest, **_permissions()}

    def verify(staging: Path) -> None:
        _verify_receipt(staging / "acquisition-preflight-receipt.json", schema_version="moocow-acquisition-preflight-receipt-v1", status="acquisition_preflight_ready")

    return _publish(output_dir, build, verify)


def verify_acquisition_preflight(
    *,
    receipt_path: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
) -> dict[str, Any]:
    """Re-open only portable shared/open roots; sealed custody is never an input."""
    receipt, _ = _verify_receipt(Path(receipt_path), schema_version="moocow-acquisition-preflight-receipt-v1", status="acquisition_preflight_ready")
    if receipt.get("state") != "ACQUISITION_PREFLIGHT_READY":
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt.state", "must be ACQUISITION_PREFLIGHT_READY")
    materials, shared_binding = _load_materials(shared_root)
    source_binding = _mapping(receipt.get("open_source_binding"), "acquisition_preflight_receipt.open_source_binding")
    if shared_binding["materials_manifest_sha256"] != source_binding.get("materials_manifest_sha256"):
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt.open_source_binding", "does not match copied shared material bytes")
    if sha256_bytes(canonical_json_bytes(materials)) != source_binding.get("normalized_materials_sha256"):
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt.open_source_binding", "does not match copied shared evidence bindings")
    batches, batches_sha = _load_batch_manifest(open_root, split_kind="open")
    if batches_sha != source_binding.get("open_batch_manifest_sha256"):
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt.open_source_binding", "does not match copied open batch bytes")
    evidence_root = Path(open_root) / "evidence"
    normalized, counts = _validate_batch_collection(
        batches=batches,
        materials=materials,
        evidence_root=evidence_root,
        split_kind="open",
        frozen_formula_mapping_sha256=source_binding.get("open_formula_mapping_sha256"),
    )
    if sha256_bytes(canonical_json_bytes(normalized)) != source_binding.get("normalized_open_batches_sha256"):
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt.open_source_binding", "does not match copied open evidence bindings")
    rank = _rank_receipt([batch for batch in normalized if batch["split"] == "train"])
    if rank["receipt_payload_sha256"] != source_binding.get("actual_nv_rank_payload_sha256"):
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt.open_source_binding", "does not match the revalidated rank receipt")
    if counts != receipt["public_counts"]["open"] or rank["rank_method"]["numerical_rank"] != 15:
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt", "does not match revalidated shared/open evidence")
    return {"status": "acquisition_preflight_verified", "state": "ACQUISITION_PREFLIGHT_READY", "receipt_verified": True, **_permissions()}


def load_verified_open_acquisition_context(
    *,
    receipt_path: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
) -> dict[str, Any]:
    """Return only the reverified open acquisition projection for a downstream boundary.

    The acquisition verifier remains the authority for the predecessor receipt.
    This helper deliberately reconstructs only material, open-batch, and card
    information from the supplied shared/open roots; it does not return any
    custody commitment or non-open batch data.
    """

    verification = verify_acquisition_preflight(
        receipt_path=receipt_path,
        shared_root=shared_root,
        open_root=open_root,
    )
    if (
        verification.get("status") != "acquisition_preflight_verified"
        or verification.get("state") != "ACQUISITION_PREFLIGHT_READY"
        or verification.get("receipt_verified") is not True
    ):
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt", "did not pass predecessor verification")

    receipt, receipt_sha = _verify_receipt(
        Path(receipt_path),
        schema_version="moocow-acquisition-preflight-receipt-v1",
        status="acquisition_preflight_ready",
    )
    return verify_open_acquisition_projection(
        shared_root=shared_root,
        open_root=open_root,
        open_source_binding=_mapping(
            receipt.get("open_source_binding"), "acquisition_preflight_receipt.open_source_binding"
        ),
        acquisition_preflight_receipt_sha256=receipt_sha,
    )


def verify_open_acquisition_projection(
    *,
    shared_root: Path | str,
    open_root: Path | str,
    open_source_binding: Mapping[str, Any],
    acquisition_preflight_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Revalidate only the portable shared/open acquisition projection.

    This is intentionally narrower than :func:`verify_acquisition_preflight`:
    it accepts no non-open path and verifies the persisted open source binding
    against copied shared/open roots.  Callers with the predecessor receipt
    must call ``verify_acquisition_preflight`` first.
    """

    source_binding = dict(open_source_binding)
    expected_binding_keys = {
        "materials_manifest_sha256",
        "open_batch_manifest_sha256",
        "normalized_materials_sha256",
        "normalized_open_batches_sha256",
        "actual_nv_rank_payload_sha256",
        "open_formula_mapping_sha256",
    }
    if set(source_binding) != expected_binding_keys:
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt.open_source_binding", "has an unsupported shape")

    materials, shared_binding = _load_materials(shared_root)
    batches, batches_sha = _load_batch_manifest(open_root, split_kind="open")
    normalized, counts = _validate_batch_collection(
        batches=batches,
        materials=materials,
        evidence_root=Path(open_root) / "evidence",
        split_kind="open",
        frozen_formula_mapping_sha256=source_binding["open_formula_mapping_sha256"],
    )
    if (
        shared_binding["materials_manifest_sha256"] != source_binding["materials_manifest_sha256"]
        or batches_sha != source_binding["open_batch_manifest_sha256"]
        or sha256_bytes(canonical_json_bytes(materials)) != source_binding["normalized_materials_sha256"]
        or sha256_bytes(canonical_json_bytes(normalized)) != source_binding["normalized_open_batches_sha256"]
    ):
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt.open_source_binding", "does not match the revalidated open projection")
    rank = _rank_receipt([batch for batch in normalized if batch["split"] == "train"])
    if (
        rank["receipt_payload_sha256"] != source_binding["actual_nv_rank_payload_sha256"]
        or rank["rank_method"]["numerical_rank"] != 15
    ):
        _fail("RECEIPT_BINDING", "acquisition_preflight_receipt.open_source_binding", "does not match the revalidated actual-NV rank receipt")
    if counts != {"families": 17, "batches": 17, "cards": 36, "primary_reading_slots": 216}:
        _fail("RECEIPT_BINDING", "open_source_binding", "does not produce the fixed open roster")

    card_skeleton = [card for batch in normalized for card in _skeleton(batch)]
    result = {
        "open_source_binding": source_binding,
        "materials": materials,
        "batches": normalized,
        "card_skeleton": card_skeleton,
    }
    if acquisition_preflight_receipt_sha256 is not None:
        result["acquisition_preflight_receipt_sha256"] = _sha256(
            acquisition_preflight_receipt_sha256,
            "acquisition_preflight_receipt_sha256",
        )
    return result
