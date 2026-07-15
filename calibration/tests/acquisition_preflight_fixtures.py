"""Deterministic, temporary-only sources for acquisition-preflight tests."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = CALIBRATION_ROOT / "tests"
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from km_calibration.acquisition_preflight import (
    COMPONENT_BY_KEY,
    COMPONENT_IDS,
    COMPONENT_ORDER,
    HOLDOUT_FAMILIES,
    TRAIN_FAMILIES,
    VALIDATION_FAMILIES,
    AcquisitionPreflightError,
    MASS_SOLIDS_NONVOLATILE_DENSITY,
    WET_DENSITY_VOLUME_SOLIDS,
)
from km_calibration.hashing import sha256_bytes, verify_sha256_sidecar, write_json_with_sha256
from km_calibration.pilot import freeze_pilot_design
from test_pilot_holdout import _real_four_card_receipt, _registry_evidence_root, _write_preregistered_design, _write_verified_registry


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"", "-0"} else text


def write_frozen_pilot_prerequisite(root: Path) -> dict[str, Path]:
    """Build real four-card and frozen-pilot receipts under the test root."""
    parent = root / "parent"
    parent.mkdir(parents=True, exist_ok=True)
    design = _write_preregistered_design(parent / "pilot-design.json")
    registry = _write_verified_registry(parent / "registry.json")
    diagnostic_receipt, diagnostic_evidence_root = _real_four_card_receipt(parent)
    pilot_output = parent / "pilot-output"
    freeze_pilot_design(
        design_path=design,
        registry_path=registry,
        registry_evidence_root=_registry_evidence_root(registry),
        diagnostic_receipt_path=diagnostic_receipt,
        diagnostic_evidence_root=diagnostic_evidence_root,
        output_dir=pilot_output,
    )
    return {
        "design_path": design,
        "registry_path": registry,
        "registry_evidence_root": _registry_evidence_root(registry),
        "diagnostic_receipt_path": diagnostic_receipt,
        "diagnostic_evidence_root": diagnostic_evidence_root,
        "pilot_design_receipt_path": pilot_output / "pilot-design-receipt.json",
    }


def write_current_materials(root: Path, prerequisite: dict[str, Path], route: str = MASS_SOLIDS_NONVOLATILE_DENSITY) -> tuple[Path, list[dict[str, Any]]]:
    shared = root / "shared"
    labels = shared / "labels"
    properties = shared / "properties"
    labels.mkdir(parents=True, exist_ok=True)
    properties.mkdir(parents=True, exist_ok=True)
    registry = json.loads(prerequisite["registry_path"].read_text(encoding="utf-8"))
    by_component = {item["component_id"]: item for item in registry["components"]}
    records: list[dict[str, Any]] = []
    for index, (formula_key, component_id) in enumerate(COMPONENT_ORDER):
        lot_id = by_component[component_id]["batch_id"]
        label_relative = f"labels/{formula_key}.txt"
        (shared / label_relative).write_text(f"physical label {component_id} {lot_id}\n", encoding="utf-8")
        if route == MASS_SOLIDS_NONVOLATILE_DENSITY:
            properties_payload = {
                "nonvolatile_mass_fraction": {
                    "property_record_id": f"PROP-{formula_key}-MASS",
                    "value": _decimal_text(Decimal("0.41") + Decimal(index) / Decimal("1000")),
                    "unit": "fraction",
                    "method": "validated mass solids method",
                    "observed_at": "2026-07-14T21:00:00+09:00",
                },
                "nonvolatile_density_g_ml": {
                    "property_record_id": f"PROP-{formula_key}-DENSITY",
                    "value": _decimal_text(Decimal("1.01") + Decimal(index) / Decimal("100")),
                    "unit": "g/mL",
                    "method": "validated nonvolatile density method",
                    "observed_at": "2026-07-14T21:01:00+09:00",
                },
            }
        else:
            properties_payload = {
                "wet_density_g_ml": {
                    "property_record_id": f"PROP-{formula_key}-WET",
                    "value": _decimal_text(Decimal("1.03") + Decimal(index) / Decimal("100")),
                    "unit": "g/mL",
                    "method": "validated wet density method",
                    "observed_at": "2026-07-14T21:00:00+09:00",
                },
                "component_nonvolatile_volume_fraction": {
                    "property_record_id": f"PROP-{formula_key}-VOLUME",
                    "value": _decimal_text(Decimal("0.43") + Decimal(index) / Decimal("1000")),
                    "unit": "fraction",
                    "method": "validated volume solids method",
                    "observed_at": "2026-07-14T21:01:00+09:00",
                },
            }
        property_relative = f"properties/{formula_key}.json"
        write_json_with_sha256(
            shared / property_relative,
            {
                "schema_version": "moocow-conversion-property-record-v2",
                "record_kind": "current_lot_conversion_properties",
                "conversion_route": route,
                "component_id": component_id,
                "physical_lot_id": lot_id,
                "properties": properties_payload,
            },
        )
        records.append(
            {
                "formula_key": formula_key,
                "component_id": component_id,
                "physical_lot_id": lot_id,
                "product_name": f"verified current {formula_key}",
                "supplier": "Current Coatings Supply",
                "label_verification": {
                    "status": "verified_physical_label",
                    "verification_id": f"LABEL-{index + 1:02d}",
                    "verified_at": "2026-07-14T20:00:00+09:00",
                    "evidence": {"relative_path": label_relative, "record_locator": {"kind": "whole_file"}},
                },
                "conversion": {
                    "route": route,
                    "property_record_evidence": {"relative_path": property_relative, "record_locator": {"kind": "whole_file"}},
                },
            }
        )
    write_json_with_sha256(
        shared / "materials.json",
        {
            "schema_version": "moocow-pilot-material-lots-v1",
            "component_order": [{"formula_key": key, "component_id": component_id} for key, component_id in COMPONENT_ORDER],
            "materials": records,
        },
    )
    return shared, records


def _support_for_family(family: str) -> tuple[str, ...]:
    if family == "FAM-TR-BASIS-BASE":
        return ("base",)
    if family.startswith("FAM-TR-BASIS-"):
        return ("base", family.removeprefix("FAM-TR-BASIS-"))
    if family == "FAM-VA-MIX-01":
        return ("base", "Y83S", "B150S")
    if family == "FAM-VA-MIX-02":
        return ("base", "R254D", "G7")
    if family == "FAM-HO-MIX-01":
        return ("base", "Y74S", "R122S")
    if family == "FAM-HO-MIX-02":
        return ("base", "B153S", "R101Y")
    return ("base", "Y42S", "V23", "BK7H")


def _property_value(shared_root: Path, formula_key: str, route: str) -> tuple[Decimal, Decimal]:
    payload = json.loads((shared_root / "properties" / f"{formula_key}.json").read_text(encoding="utf-8"))
    properties = payload["properties"]
    if route == MASS_SOLIDS_NONVOLATILE_DENSITY:
        return Decimal(properties["nonvolatile_mass_fraction"]["value"]), Decimal(properties["nonvolatile_density_g_ml"]["value"])
    return Decimal(properties["wet_density_g_ml"]["value"]), Decimal(properties["component_nonvolatile_volume_fraction"]["value"])


def write_actual_weighings(
    *,
    batch_root: Path,
    evidence_root: Path,
    shared_root: Path,
    material_records: list[dict[str, Any]],
    family_id: str,
    split: str,
    route: str,
    multiple_base_addition: bool = False,
) -> dict[str, Any]:
    formula_id = f"FORM-{family_id.removeprefix('FAM-')}"
    batch_id = f"BATCH-{family_id.removeprefix('FAM-')}"
    material_by_key = {item["formula_key"]: item for item in material_records}
    actual_components: list[dict[str, Any]] = []
    volume_by_component: dict[str, Decimal] = {}
    for component_index, formula_key in enumerate(_support_for_family(family_id)):
        material = material_by_key[formula_key]
        wet_mass = Decimal("83") + Decimal(component_index * 7) if formula_key == "base" else Decimal("9") + Decimal(component_index * 3)
        entries = []
        if multiple_base_addition and formula_key == "base":
            entries.extend(
                [
                    {"suffix": "A", "mass": wet_mass / 2},
                    {"suffix": "B", "mass": wet_mass - wet_mass / 2},
                ]
            )
        else:
            entries.append({"suffix": "A", "mass": wet_mass})
        weighing_payload = {
            "schema_version": "moocow-actual-weighing-record-v2",
            "record_kind": "actual_weighing_observation",
            "formula_id": formula_id,
            "formula_batch_id": batch_id,
            "entries": [
                {
                    "weighing_record_id": f"WEIGH-{batch_id}-{formula_key}",
                    "weighing_event_id": f"EVENT-{batch_id}-{formula_key}-{entry['suffix']}",
                    "component_id": material["component_id"],
                    "physical_lot_id": material["physical_lot_id"],
                    "actual_wet_mass_g": _decimal_text(entry["mass"]),
                    "actual_wet_mass_unit": "g",
                    "weighed_at": "2026-07-14T22:00:00+09:00",
                    "weighing_method": "calibrated bench balance",
                }
                for entry in entries
            ],
        }
        relative = f"weighings/{batch_id}-{formula_key}.json"
        write_json_with_sha256(evidence_root / relative, weighing_payload)
        property_a, property_b = _property_value(shared_root, formula_key, route)
        volume = wet_mass * property_a / property_b if route == MASS_SOLIDS_NONVOLATILE_DENSITY else wet_mass / property_a * property_b
        volume_by_component[material["component_id"]] = volume
        actual_components.append(
            {
                "component_id": material["component_id"],
                "physical_lot_id": material["physical_lot_id"],
                "conversion_route": route,
                "actual_wet_mass_g": _decimal_text(wet_mass),
                "nonvolatile_volume_ml": _decimal_text(volume),
                "actual_weighing_evidence": {"relative_path": relative, "record_locator": {"kind": "whole_file"}},
            }
        )
    volume_sum = sum(volume_by_component.values(), Decimal(0))
    vector = [volume_by_component.get(component_id, Decimal(0)) / volume_sum for component_id in COMPONENT_IDS]
    return {
        "schema_version": "moocow-pilot-formula-batch-v1",
        "formula_family_id": family_id,
        "formula_id": formula_id,
        "formula_batch_id": batch_id,
        "split": split,
        "actual_components": actual_components,
        "actual_nv_vector_component_order": [key for key, _ in COMPONENT_ORDER],
        "actual_nv_vector": [_decimal_text(item) for item in vector],
        "actual_nv_sum": _decimal_text(sum(vector, Decimal(0))),
    }


def write_pilot_batch_roots(root: Path, shared_root: Path, material_records: list[dict[str, Any]], route: str = MASS_SOLIDS_NONVOLATILE_DENSITY) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    batches_by_kind = {
        "open": [(family, "train") for family in TRAIN_FAMILIES] + [(family, "validation") for family in VALIDATION_FAMILIES],
        "sealed": [(family, "holdout") for family in HOLDOUT_FAMILIES],
    }
    for kind, families in batches_by_kind.items():
        batch_root = root / kind
        evidence_root = batch_root / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        batches = [
            write_actual_weighings(
                batch_root=batch_root,
                evidence_root=evidence_root,
                shared_root=shared_root,
                material_records=material_records,
                family_id=family,
                split=split,
                route=route,
                multiple_base_addition=kind == "open" and family == TRAIN_FAMILIES[1],
            )
            for family, split in families
        ]
        write_json_with_sha256(batch_root / "batches.json", {"schema_version": "moocow-pilot-batches-v1", "batches": batches})
        roots[f"{kind}_batch_root"] = batch_root
        roots[f"{kind}_evidence_root"] = evidence_root
    return roots


def assertPreflight(code: str, callback: Callable[[], object]) -> AcquisitionPreflightError:
    with unittest.TestCase().assertRaises(AcquisitionPreflightError) as captured:
        callback()
    if captured.exception.code != code:
        raise AssertionError(f"expected {code}, got {captured.exception.code}")
    return captured.exception


def assert_permissions_all_false(value: dict[str, Any]) -> None:
    expected = {
        "pilot_acquisition_permitted",
        "open_admission_permitted",
        "model_fitting_permitted",
        "holdout_release_permitted",
        "physical_ranking_enabled",
        "promotion_permitted",
    }
    if set(key for key in value if key in expected) != expected or any(value[key] is not False for key in expected):
        raise AssertionError("expected the complete all-false permission vector")


def assert_no_holdout_raw(value: object) -> None:
    forbidden = ("fam-ho-", "formula_batch_id", "actual_wet_mass", "nonvolatile_volume", "actual_nv", "relative_path", "file_sha256", "dft", "reflectance", "measurement", "spectrum")
    allowed = {"sealed_batch_manifest_sha256", "holdout_release_permitted"}
    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key not in allowed and any(marker in str(key).casefold() for marker in forbidden):
                    raise AssertionError(f"prohibited holdout field: {key}")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and "fam-ho-" in item.casefold():
            raise AssertionError("prohibited holdout identifier")
    visit(value)


def assert_sidecar_matches(path: Path) -> None:
    expected = verify_sha256_sidecar(path)
    if expected != sha256_bytes(path.read_bytes()):
        raise AssertionError("sidecar did not bind exact bytes")


def copytree_without_rewriting(root: Path) -> Path:
    target = root.parent / f"{root.name}-copy"
    shutil.copytree(root, target)
    return target


def run_cli(args: list[str]) -> tuple[int, str, str]:
    from km_calibration.cli import main
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            result = main(args)
        except SystemExit as error:
            result = int(error.code)
    return int(result or 0), stdout.getvalue(), stderr.getvalue()
