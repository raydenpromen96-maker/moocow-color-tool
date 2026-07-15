from __future__ import annotations

import copy
import contextlib
import csv
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CALIBRATION_ROOT.parent
sys.path.insert(0, str(CALIBRATION_ROOT))

from km_calibration.cli import main
from km_calibration.diagnostic import (
    BACKINGS,
    CARD_ROSTER,
    MASS_SOLIDS_NONVOLATILE_DENSITY,
    CSV_COLUMNS,
    POSITIONS,
    TARGET_DEVIATION_STATUS,
    WET_DENSITY_VOLUME_SOLIDS,
    DiagnosticValidationError,
    generate_weighing_plan,
    normalize_diagnostic_csv,
    normalize_diagnostic_json,
    preflight_four_card,
    preflight_from_files,
    prepare_four_card,
    structural_preflight_four_card,
    verify_evidence_bindings,
    verify_preflight_receipt,
)
from km_calibration.errors import DatasetValidationError
from km_calibration.hashing import canonical_json_bytes, sha256_bytes, write_json_with_sha256
from km_calibration.schema import load_and_validate_dataset


WAVELENGTHS = [float(wavelength) for wavelength in range(400, 701, 20)]
REGISTRY = CALIBRATION_ROOT / "protocols" / "current-batch-component-registry-v1.json"


def _locator(relative_path: str, *, offset: int | None = None, length: int | None = None) -> dict[str, object]:
    if offset is None:
        return {"relative_path": relative_path, "record_locator": {"kind": "whole_file"}}
    assert length is not None
    return {
        "relative_path": relative_path,
        "record_locator": {"kind": "byte_range", "byte_offset": offset, "byte_length": length},
    }


def _write(root: Path, relative_path: str, content: bytes | str) -> dict[str, object]:
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return _locator(relative_path)


def _conditions(root: Path) -> dict[str, object]:
    return {
        "instrument_make_model": "Example Spectrophotometer 1",
        "instrument_serial_number": "SP-0001",
        "instrument_software_version": "2.0.1",
        "instrument_firmware_version": "1.8.0",
        "instrument_calibration_id": "CAL-20260714-01",
        "instrument_calibration_timestamp": "2026-07-14T09:00:00+09:00",
        "instrument_calibration_result": "pass",
        "instrument_calibration_evidence": _write(root, "instrument/calibration.txt", "calibration pass"),
        "instrument_run_log_evidence": _write(root, "instrument/run-log.txt", "run log"),
        "white_standard_id": "WHITE-REF-01",
        "black_calibration_mode": "black-trap",
        "measurement_geometry": "d/8",
        "aperture_mm": 8.0,
        "specular_condition": "SCI",
        "uv_setting": "UV-included",
        "measurement_mode": "reflectance",
        "illuminant": "D65",
        "observer": "10deg",
        "wavelength_start_nm": WAVELENGTHS[0],
        "wavelength_end_nm": WAVELENGTHS[-1],
        "wavelength_interval_nm": WAVELENGTHS[1] - WAVELENGTHS[0],
        "wavelength_unit": "nm",
        "spectral_bandpass_nm": 10.0,
        "reflectance_scale": "fraction",
        "cure_protocol": "ambient cure",
        "cure_start": "2026-07-13T09:00:00+09:00",
        "cure_end": "2026-07-14T08:00:00+09:00",
        "age_at_measurement_h": 25.0,
        "cure_temperature_c_observed": 23.5,
        "cure_rh_pct_observed": 50.0,
        "airflow_note": "ambient monitored airflow",
        "application_method": "drawdown bar",
        "applicator_or_wft_target": "100 um wet-film target",
        "operator_id": "operator-01",
    }


def _physical_lot(component_id: str) -> str:
    return "LOT-BASE-01" if component_id == "base-waterborne-clear" else "LOT-W064-01"


def _verified_registry_bytes() -> bytes:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    for component in registry["components"]:
        component_id = component["component_id"]
        if component_id in {"base-waterborne-clear", "colorant-W064"}:
            component["batch_id"] = _physical_lot(component_id)
            component["lot_verification_status"] = "verified_physical_label"
            component["product_name"] = f"verified {component_id}"
            component["manufacturer_or_supplier"] = "Example Coatings"
    return json.dumps(registry, sort_keys=True).encode("utf-8")


def _property_records(root: Path, component_id: str, route: str) -> dict[str, object]:
    slug = "base" if component_id == "base-waterborne-clear" else "w064"
    property_names = (
        ("nonvolatile_mass_fraction", "fraction", 0.5),
        ("nonvolatile_density_g_ml", "g/mL", 1.0),
    ) if route == MASS_SOLIDS_NONVOLATILE_DENSITY else (
        ("wet_density_g_ml", "g/mL", 1.0),
        ("component_nonvolatile_volume_fraction", "fraction", 0.5),
    )
    records = {
        property_name: {
            "property_record_id": f"PROP-{slug.upper()}-{property_name.upper()}",
            "component_id": component_id,
            "physical_lot_id": _physical_lot(component_id),
            "value": value,
            "unit": unit,
            "method": f"validated {property_name} method",
            "observed_at": "2026-07-13T08:10:00+09:00",
        }
        for property_name, unit, value in property_names
    }
    locator = _write(
        root,
        f"properties/{slug}-{route}.json",
        canonical_json_bytes(
            {
                "schema_version": "moocow-conversion-property-record-v2",
                "record_kind": "current_lot_conversion_properties",
                "conversion_route": route,
                "component_id": component_id,
                "physical_lot_id": _physical_lot(component_id),
                "properties": {
                    name: {
                        field: value
                        for field, value in record.items()
                        if field not in {"component_id", "physical_lot_id"}
                    }
                    for name, record in records.items()
                },
            }
        ),
    )
    return {
        name: {**record, "property_record_evidence": copy.deepcopy(locator)}
        for name, record in records.items()
    }


def _formula(
    root: Path,
    components: tuple[tuple[str, float], ...],
    name: str,
    *,
    route: str = MASS_SOLIDS_NONVOLATILE_DENSITY,
) -> dict[str, object]:
    formula_id = f"FORM-{name}"
    formula_batch_id = f"BATCH-{name}"
    component_rows: list[tuple[str, float, dict[str, object]]] = []
    weighing_entries: list[dict[str, object]] = []
    for component_id, target_fraction in components:
        if len(components) == 1:
            actual_wet_mass_g = 10.0
        else:
            actual_wet_mass_g = 17.0 if component_id == "base-waterborne-clear" else 3.0
        slug = "base" if component_id == "base-waterborne-clear" else "w064"
        entry: dict[str, object] = {
            "weighing_record_id": f"WEIGH-{name}",
            "weighing_event_id": f"WEIGH-{name}-{slug}",
            "component_id": component_id,
            "physical_lot_id": _physical_lot(component_id),
            "actual_wet_mass_g": actual_wet_mass_g,
            "actual_wet_mass_unit": "g",
            "weighing_method": "net mass by difference",
            "weighed_at": "2026-07-13T08:20:00+09:00",
        }
        weighing_entries.append(entry)
        component_rows.append((component_id, target_fraction, entry))
    weighing_locator = _write(
        root,
        f"weighing/{name}.actual-weighing.json",
        canonical_json_bytes(
            {
                "schema_version": "moocow-actual-weighing-record-v2",
                "record_kind": "actual_weighing_observation",
                "formula_id": formula_id,
                "formula_batch_id": formula_batch_id,
                "entries": weighing_entries,
            }
        ),
    )
    raw_components: list[dict[str, object]] = []
    for component_id, target_fraction, entry in component_rows:
        raw_components.append(
            {
                "component_id": component_id,
                "physical_lot_id": _physical_lot(component_id),
                "target_nonvolatile_volume_fraction": target_fraction,
                "actual_weighing": {
                    "record_kind": "actual_weighing_observation",
                    **entry,
                    "weighing_record_evidence": copy.deepcopy(weighing_locator),
                },
                "property_records": _property_records(root, component_id, route),
            }
        )
    return {
        "formula_id": formula_id,
        "formula_batch_id": formula_batch_id,
        "formula_stage": "four_card_diagnostic",
        "conversion_route": route,
        "components": raw_components,
    }


def _weighing_plan_input(
    root: Path,
    route: str,
    *,
    formula_family_id: str = "FAM-DX-W064",
) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    registry_locator = _write(
        root,
        "registry/current-batch-component-registry-v1.json",
        _verified_registry_bytes(),
    )
    targets = (
        (("base-waterborne-clear", 1.0),)
        if formula_family_id == "FAM-DX-BASE"
        else (("base-waterborne-clear", 0.85), ("colorant-W064", 0.15))
    )
    return {
        "schema_version": "moocow-four-card-weighing-plan-input-v2",
        "plan_id": f"PLAN-{formula_family_id}-{route}",
        "formula_family_id": formula_family_id,
        "formula_id": f"FORM-{formula_family_id}-{route}",
        "formula_batch_id": f"BATCH-{formula_family_id}-{route}",
        "formula_stage": "four_card_diagnostic",
        "conversion_route": route,
        "planned_total_nonvolatile_volume_ml": 10.0,
        "planned_total_nonvolatile_volume_unit": "mL",
        "registry_snapshot_evidence": registry_locator,
        "components": [
            {
                "component_id": component_id,
                "physical_lot_id": _physical_lot(component_id),
                "target_nonvolatile_volume_fraction": target_fraction,
                "property_records": _property_records(root, component_id, route),
            }
            for component_id, target_fraction in targets
        ],
    }


def _dft_region(root: Path, mean: float, label: str) -> dict[str, object]:
    values = [mean - 1.0, mean, mean + 1.0]
    return {
        "dft_method": "cross-section micrometer",
        "dft_instrument_id": "DFT-01",
        "dft_verification_id": f"DFT-CHECK-{label}",
        "dft_measured_at": "2026-07-14T08:30:00+09:00",
        "dft_record_evidence": _write(root, f"dft/{label}.txt", f"dft {label}"),
        "locations": [
            {"location_id": f"{label}-01", "dft_um": values[0]},
            {"location_id": f"{label}-02", "dft_um": values[1]},
            {"location_id": f"{label}-03", "dft_um": values[2]},
        ],
    }


def _valid_payload(
    root: Path, *, route: str = MASS_SOLIDS_NONVOLATILE_DENSITY
) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    registry_locator = _write(
        root,
        "registry/current-batch-component-registry-v1.json",
        _verified_registry_bytes(),
    )
    conditions = _conditions(root)
    cards: list[dict[str, object]] = []
    readings: list[dict[str, object]] = []
    for card_index, (card_id, family, band, components) in enumerate(CARD_ROSTER):
        dft_mean = 20.0 if band == "DFT-L" else 40.0
        cards.append(
            {
                "card_id": card_id,
                "formula_family_id": family,
                "dft_band": band,
                "formula": _formula(root, components, card_id, route=route),
                "dft_by_backing": {
                    backing: _dft_region(root, dft_mean, f"{card_id}-{backing}") for backing in BACKINGS
                },
            }
        )
        for backing_index, backing in enumerate(BACKINGS):
            for position_index, position in enumerate(POSITIONS):
                offset = card_index * 0.02 + backing_index * 0.01 + position_index * 0.001
                readings.append(
                    {
                        "card_id": card_id,
                        "backing": backing,
                        "reposition_id": position,
                        "instrument_measurement_id": f"MSR-{card_index}-{backing_index}-{position_index}",
                        "position_note": f"mapped location {position}",
                        "orientation": "0deg top-mark",
                        "measured_at_local": "2026-07-14T10:00:00+09:00",
                        "raw_spectrum_evidence": _write(root, f"raw/{card_id}-{backing}-{position}.csv", f"raw {card_id} {backing} {position}"),
                        "evidence_class": "measured_current_batch",
                        "surface_status": "accepted_uniform_dry_film",
                        "model_applicability_status": "accepted_for_km_diagnostic",
                        "backing_id": f"CARD-{backing.upper()}",
                        "backing_lot_id": "CHART-LOT-01",
                        "reflectance": [0.15 + offset + wavelength_index * 0.01 for wavelength_index in range(len(WAVELENGTHS))],
                    }
                )
    return {
        "schema_version": "moocow-physical-diagnostic-acquisition-v2",
        "acquisition_status": "diagnostic_measured",
        "physical_ranking_enabled": False,
        "model_fitting_permitted": False,
        "diagnostic_id": "DX-20260714-01",
        "registry_snapshot_evidence": registry_locator,
        "wavelength_nm": list(WAVELENGTHS),
        "locked_conditions": conditions,
        "materials": {
            name: {
                "component_id": component_id,
                "product_name": f"verified {name} material",
                "manufacturer_or_supplier": "Example Coatings",
                "batch_id": _physical_lot(component_id),
                "physical_label_verification_status": "verified_physical_label",
                "physical_label_verification_id": f"LABEL-{name.upper()}-01",
                "physical_label_verified_at": "2026-07-14T08:00:00+09:00",
                "physical_label_evidence": _write(root, f"labels/{name}.txt", f"label {name}"),
            }
            for name, component_id in (("base", "base-waterborne-clear"), ("w064", "colorant-W064"))
        },
        "dft_bands": {
            "DFT-L": {"target_um": 20.0, "acceptance_min_um": 10.0, "acceptance_max_um": 30.0},
            "DFT-H": {"target_um": 40.0, "acceptance_min_um": 30.0, "acceptance_max_um": 50.0},
        },
        "backings": {
            backing: {
                "backing_id": f"CARD-{backing.upper()}",
                "manufacturer": "Example Chart Maker",
                "product": "contrast chart",
                "lot_id": "CHART-LOT-01",
                "storage_state": "clean and dry",
                "region_description": f"{backing} contrast region",
                "measurements": [
                    {
                        "instrument_measurement_id": f"BARE-{backing.upper()}-{index + 1:02}",
                        "measured_at_local": f"2026-07-14T09:3{index}:00+09:00",
                        "raw_export_evidence": _write(root, f"raw/bare-{backing}-{index + 1:02}.csv", f"bare {backing} {index}"),
                        "evidence_class": "measured_current_batch",
                        "reflectance": [
                            (0.05 if backing == "black" else 0.80) + index * 0.001 + wavelength_index * 0.01
                            for wavelength_index in range(len(WAVELENGTHS))
                        ],
                    }
                    for index in range(3)
                ],
            }
            for backing in BACKINGS
        },
        "cards": cards,
        "readings": readings,
    }


def _rebind_conditions(payload: dict[str, object]) -> None:
    readings = payload["readings"]
    assert isinstance(readings, list)
    for reading in readings:
        assert isinstance(reading, dict)
        reading.pop("locked_conditions_sha256", None)


def _csv_manifest_and_rows(payload: dict[str, object]) -> tuple[dict[str, object], list[dict[str, str]]]:
    manifest = copy.deepcopy(payload)
    raw_readings = manifest.pop("readings")
    assert isinstance(raw_readings, list)
    manifest["reading_metadata"] = [
        {key: value for key, value in reading.items() if key != "reflectance"} for reading in raw_readings
    ]
    rows: list[dict[str, str]] = []
    for reading in raw_readings:
        assert isinstance(reading, dict)
        for wavelength, reflectance in zip(payload["wavelength_nm"], reading["reflectance"], strict=True):
            rows.append(
                {
                    "card_id": str(reading["card_id"]),
                    "backing": str(reading["backing"]),
                    "reposition_id": str(reading["reposition_id"]),
                    "instrument_measurement_id": str(reading["instrument_measurement_id"]),
                    "position_note": str(reading["position_note"]),
                    "orientation": str(reading["orientation"]),
                    "wavelength_nm": f"{float(wavelength):g}",
                    "reflectance": repr(float(reflectance)),
                }
            )
    return manifest, list(reversed(rows))


def _shuffle_json_wavelengths(payload: dict[str, object]) -> dict[str, object]:
    shuffled = copy.deepcopy(payload)
    original = list(shuffled["wavelength_nm"])
    permutation = list(range(2, len(original))) + [0, 1]
    shuffled["wavelength_nm"] = [original[index] for index in permutation]
    for backing in shuffled["backings"].values():
        for measurement in backing["measurements"]:
            measurement["reflectance"] = [measurement["reflectance"][index] for index in permutation]
    for reading in shuffled["readings"]:
        reading["reflectance"] = [reading["reflectance"][index] for index in permutation]
    shuffled["readings"].reverse()
    return shuffled


class DiagnosticImportTests(unittest.TestCase):
    def assertDiagnostic(self, code: str, callback: object) -> None:
        with self.assertRaises(DiagnosticValidationError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)

    def test_json_csv_structural_parity_and_never_ready_without_evidence_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            json_bundle = normalize_diagnostic_json(_shuffle_json_wavelengths(payload))
            manifest, rows = _csv_manifest_and_rows(payload)
            csv_bundle = normalize_diagnostic_csv(manifest, rows)
            self.assertEqual(json_bundle.canonical_bytes, csv_bundle.canonical_bytes)
            self.assertEqual(json_bundle.structural_sha256, csv_bundle.structural_sha256)
            report = structural_preflight_four_card(json_bundle).payload
            self.assertEqual(report["status"], "structural_valid")
            self.assertFalse(report["readiness"]["current_diagnostic_ready"])
            self.assertFalse(report["readiness"]["evidence_verified"])
            self.assertFalse(report["gates"]["model_fitting_permitted"])
            json_evidence = verify_evidence_bindings(json_bundle, evidence_root=root)
            csv_evidence = verify_evidence_bindings(csv_bundle, evidence_root=root)
            self.assertEqual(json_evidence.canonical_bytes, csv_evidence.canonical_bytes)
            self.assertEqual(json_evidence.evidence_verification_sha256, csv_evidence.evidence_verification_sha256)

    def test_both_conversion_routes_derive_actuals_and_report_deviation_without_threshold(self) -> None:
        for route in (MASS_SOLIDS_NONVOLATILE_DENSITY, WET_DENSITY_VOLUME_SOLIDS):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "evidence"
                payload = _valid_payload(root, route=route)
                formula = normalize_diagnostic_json(payload).payload["cards"][1]["formula"]
                result = formula["conversion_result"]
                self.assertEqual(result["target_deviation_status"], TARGET_DEVIATION_STATUS)
                self.assertEqual(
                    set(result),
                    {
                        "derivation",
                        "target_deviation_status",
                        "total_nonvolatile_volume_ml",
                        "components",
                    },
                )
                self.assertAlmostEqual(result["total_nonvolatile_volume_ml"], 10.0)
                by_component = {item["component_id"]: item for item in result["components"]}
                self.assertAlmostEqual(by_component["base-waterborne-clear"]["nonvolatile_volume_ml"], 8.5)
                self.assertAlmostEqual(by_component["colorant-W064"]["nonvolatile_volume_ml"], 1.5)
                self.assertAlmostEqual(by_component["base-waterborne-clear"]["actual_nonvolatile_volume_fraction"], 0.85)
                self.assertAlmostEqual(by_component["colorant-W064"]["actual_nonvolatile_volume_fraction"], 0.15)

                deviated = copy.deepcopy(payload)
                deviated_component = deviated["cards"][1]["formula"]["components"][1]
                deviated_component["actual_weighing"]["actual_wet_mass_g"] = 4.0
                deviated_formula = normalize_diagnostic_json(deviated).payload["cards"][1]["formula"]
                deviated_result = {
                    item["component_id"]: item
                    for item in deviated_formula["conversion_result"]["components"]
                }
                expected_w064_fraction = 2.0 / 10.5
                self.assertAlmostEqual(
                    deviated_result["colorant-W064"]["actual_nonvolatile_volume_fraction"],
                    expected_w064_fraction,
                )
                self.assertAlmostEqual(
                    deviated_result["colorant-W064"]["target_deviation_fraction"],
                    expected_w064_fraction - 0.15,
                )
                self.assertEqual(
                    deviated_formula["conversion_result"]["target_deviation_status"],
                    "reported_no_physical_threshold",
                )

    def test_conversion_route_field_matrix_units_and_derived_inputs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            free_text_route = copy.deepcopy(payload)
            free_text_route["cards"][0]["formula"]["conversion_route"] = "use the usual route"
            self.assertDiagnostic("CONVERSION_ROUTE", lambda: normalize_diagnostic_json(free_text_route))
            hybrid = copy.deepcopy(payload)
            hybrid["cards"][0]["formula"]["components"][0]["property_records"]["wet_density_g_ml"] = copy.deepcopy(
                hybrid["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_density_g_ml"]
            )
            self.assertDiagnostic("UNKNOWN_FIELD", lambda: normalize_diagnostic_json(hybrid))
            route2 = _valid_payload(root / "route2", route=WET_DENSITY_VOLUME_SOLIDS)
            route2_hybrid = copy.deepcopy(route2)
            route2_hybrid["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_mass_fraction"] = copy.deepcopy(
                route2_hybrid["cards"][0]["formula"]["components"][0]["property_records"]["component_nonvolatile_volume_fraction"]
            )
            self.assertDiagnostic("UNKNOWN_FIELD", lambda: normalize_diagnostic_json(route2_hybrid))
            route2_bad_fraction = copy.deepcopy(route2)
            route2_bad_fraction["cards"][0]["formula"]["components"][0]["property_records"]["component_nonvolatile_volume_fraction"]["value"] = 1.1
            self.assertDiagnostic("PROPERTY_FRACTION", lambda: normalize_diagnostic_json(route2_bad_fraction))
            missing = copy.deepcopy(payload)
            del missing["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_mass_fraction"]
            self.assertDiagnostic("MISSING_FIELD", lambda: normalize_diagnostic_json(missing))
            invalid_unit = copy.deepcopy(payload)
            invalid_unit["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_density_g_ml"]["unit"] = "kg/L"
            self.assertDiagnostic("PROPERTY_UNIT", lambda: normalize_diagnostic_json(invalid_unit))
            missing_method = copy.deepcopy(payload)
            missing_method["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_density_g_ml"]["method"] = ""
            self.assertDiagnostic("REQUIRED_TEXT", lambda: normalize_diagnostic_json(missing_method))
            missing_timezone = copy.deepcopy(payload)
            missing_timezone["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_density_g_ml"]["observed_at"] = "2026-07-14T08:10:00"
            self.assertDiagnostic("TIMESTAMP_TIMEZONE", lambda: normalize_diagnostic_json(missing_timezone))
            zero_actual_mass = copy.deepcopy(payload)
            zero_actual_mass["cards"][0]["formula"]["components"][0]["actual_weighing"]["actual_wet_mass_g"] = 0
            self.assertDiagnostic("POSITIVE_NUMBER", lambda: normalize_diagnostic_json(zero_actual_mass))
            plan_as_actual = copy.deepcopy(payload)
            plan_as_actual["cards"][0]["formula"]["components"][0]["actual_weighing"]["record_kind"] = "planned_target_wet_mass_not_actual"
            self.assertDiagnostic("ACTUAL_WEIGHING_KIND", lambda: normalize_diagnostic_json(plan_as_actual))
            derived_formula = copy.deepcopy(payload)
            derived_formula["cards"][0]["formula"]["conversion_result"] = {}
            self.assertDiagnostic("UNKNOWN_FIELD", lambda: normalize_diagnostic_json(derived_formula))
            derived_component = copy.deepcopy(payload)
            derived_component["cards"][0]["formula"]["components"][0]["nonvolatile_volume_ml"] = 5.0
            self.assertDiagnostic("UNKNOWN_FIELD", lambda: normalize_diagnostic_json(derived_component))
            derived_dft = copy.deepcopy(payload)
            derived_dft["cards"][0]["dft_by_backing"]["black"]["reported_mean_um"] = 20.0
            self.assertDiagnostic("UNKNOWN_FIELD", lambda: normalize_diagnostic_json(derived_dft))
            typed_digest = copy.deepcopy(payload)
            typed_digest["readings"][0]["locked_conditions_sha256"] = "f" * 64
            self.assertDiagnostic("UNKNOWN_FIELD", lambda: normalize_diagnostic_json(typed_digest))
            pilot = copy.deepcopy(payload)
            pilot["cards"][0]["formula"]["formula_stage"] = "pilot"
            self.assertDiagnostic("FORMULA_STAGE", lambda: normalize_diagnostic_json(pilot))

    def test_canonical_property_evidence_is_shared_and_matches_every_semantic_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            properties = payload["cards"][0]["formula"]["components"][0]["property_records"]
            self.assertEqual(
                properties["nonvolatile_mass_fraction"]["property_record_evidence"],
                properties["nonvolatile_density_g_ml"]["property_record_evidence"],
            )
            self.assertEqual(
                preflight_four_card(normalize_diagnostic_json(payload), evidence_root=root).payload["status"],
                "evidence_ready",
            )

        cases = {
            "conversion_route": lambda record: record.__setitem__("conversion_route", WET_DENSITY_VOLUME_SOLIDS),
            "component_id": lambda record: record.__setitem__("component_id", "colorant-W064"),
            "physical_lot_id": lambda record: record.__setitem__("physical_lot_id", "OTHER-LOT"),
            "property_record_id": lambda record: record["properties"]["nonvolatile_mass_fraction"].__setitem__("property_record_id", "OTHER-RECORD"),
            "value": lambda record: record["properties"]["nonvolatile_mass_fraction"].__setitem__("value", 0.6),
            "unit": lambda record: record["properties"]["nonvolatile_mass_fraction"].__setitem__("unit", "percent"),
            "method": lambda record: record["properties"]["nonvolatile_mass_fraction"].__setitem__("method", "different method"),
            "observed_at": lambda record: record["properties"]["nonvolatile_mass_fraction"].__setitem__("observed_at", "2026-07-13T08:11:00+09:00"),
        }
        for field, mutate in cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "evidence"
                payload = _valid_payload(root)
                property_record = payload["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_mass_fraction"]
                evidence_path = root / property_record["property_record_evidence"]["relative_path"]
                canonical_record = json.loads(evidence_path.read_text(encoding="utf-8"))
                mutate(canonical_record)
                evidence_path.write_bytes(canonical_json_bytes(canonical_record))
                self.assertDiagnostic(
                    "PROPERTY_EVIDENCE_FIELD_MISMATCH",
                    lambda: verify_evidence_bindings(normalize_diagnostic_json(payload), evidence_root=root),
                )

        special_cases = (
            ("schema", "PROPERTY_EVIDENCE_SCHEMA", lambda record: record.__setitem__("schema_version", "wrong-schema")),
            ("kind", "PROPERTY_EVIDENCE_KIND", lambda record: record.__setitem__("record_kind", "catalog_property_curve")),
            ("missing", "PROPERTY_EVIDENCE_RECORD_MISSING", lambda record: record["properties"].pop("nonvolatile_mass_fraction")),
        )
        for name, expected_code, mutate in special_cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "evidence"
                payload = _valid_payload(root)
                property_record = payload["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_mass_fraction"]
                evidence_path = root / property_record["property_record_evidence"]["relative_path"]
                canonical_record = json.loads(evidence_path.read_text(encoding="utf-8"))
                mutate(canonical_record)
                evidence_path.write_bytes(canonical_json_bytes(canonical_record))
                self.assertDiagnostic(
                    expected_code,
                    lambda: verify_evidence_bindings(normalize_diagnostic_json(payload), evidence_root=root),
                )

    def test_canonical_actual_weighing_supports_unique_entries_and_matches_every_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            components = payload["cards"][1]["formula"]["components"]
            self.assertEqual(
                components[0]["actual_weighing"]["weighing_record_evidence"],
                components[1]["actual_weighing"]["weighing_record_evidence"],
            )
            weighing_path = root / components[0]["actual_weighing"]["weighing_record_evidence"]["relative_path"]
            entries = json.loads(weighing_path.read_text(encoding="utf-8"))["entries"]
            self.assertEqual(len(entries), 2)
            self.assertEqual(len({entry["weighing_event_id"] for entry in entries}), 2)
            self.assertEqual(
                preflight_four_card(normalize_diagnostic_json(payload), evidence_root=root).payload["status"],
                "evidence_ready",
            )

        cases = {
            "formula_id": lambda record: record.__setitem__("formula_id", "OTHER-FORMULA"),
            "formula_batch_id": lambda record: record.__setitem__("formula_batch_id", "OTHER-BATCH"),
            "weighing_record_id": lambda record: record["entries"][0].__setitem__("weighing_record_id", "OTHER-RECORD"),
            "component_id": lambda record: record["entries"][0].__setitem__("component_id", "colorant-W064"),
            "physical_lot_id": lambda record: record["entries"][0].__setitem__("physical_lot_id", "OTHER-LOT"),
            "actual_wet_mass_g": lambda record: record["entries"][0].__setitem__("actual_wet_mass_g", 11.0),
            "actual_wet_mass_unit": lambda record: record["entries"][0].__setitem__("actual_wet_mass_unit", "kg"),
            "weighing_method": lambda record: record["entries"][0].__setitem__("weighing_method", "different method"),
            "weighed_at": lambda record: record["entries"][0].__setitem__("weighed_at", "2026-07-13T08:21:00+09:00"),
        }
        for field, mutate in cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "evidence"
                payload = _valid_payload(root)
                actual = payload["cards"][0]["formula"]["components"][0]["actual_weighing"]
                evidence_path = root / actual["weighing_record_evidence"]["relative_path"]
                canonical_record = json.loads(evidence_path.read_text(encoding="utf-8"))
                mutate(canonical_record)
                evidence_path.write_bytes(canonical_json_bytes(canonical_record))
                self.assertDiagnostic(
                    "WEIGHING_EVIDENCE_FIELD_MISMATCH",
                    lambda: verify_evidence_bindings(normalize_diagnostic_json(payload), evidence_root=root),
                )

        event_cases = (
            ("missing_event", "WEIGHING_EVIDENCE_EVENT_MISSING", lambda record: record["entries"][0].__setitem__("weighing_event_id", "OTHER-EVENT")),
            ("duplicate_event", "WEIGHING_EVIDENCE_EVENT_DUPLICATE", lambda record: record["entries"].append(copy.deepcopy(record["entries"][0]))),
            ("schema", "WEIGHING_EVIDENCE_SCHEMA", lambda record: record.__setitem__("schema_version", "wrong-schema")),
            ("kind", "WEIGHING_EVIDENCE_KIND", lambda record: record.__setitem__("record_kind", "planned_target_wet_mass_not_actual")),
        )
        for name, expected_code, mutate in event_cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "evidence"
                payload = _valid_payload(root)
                actual = payload["cards"][0]["formula"]["components"][0]["actual_weighing"]
                evidence_path = root / actual["weighing_record_evidence"]["relative_path"]
                canonical_record = json.loads(evidence_path.read_text(encoding="utf-8"))
                mutate(canonical_record)
                evidence_path.write_bytes(canonical_json_bytes(canonical_record))
                self.assertDiagnostic(
                    expected_code,
                    lambda: verify_evidence_bindings(normalize_diagnostic_json(payload), evidence_root=root),
                )

    def test_canonical_json_bytes_and_ranges_fail_closed(self) -> None:
        invalid_cases = (
            ("invalid_utf8", "EVIDENCE_JSON_UTF8", lambda original: b"\xff\xfe"),
            ("malformed", "EVIDENCE_JSON_MALFORMED", lambda original: b"{"),
            ("duplicate_key", "JSON_DUPLICATE_KEY", lambda original: b'{"schema_version":"duplicate",' + original[1:]),
        )
        for name, expected_code, corrupt in invalid_cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "evidence"
                payload = _valid_payload(root)
                property_record = payload["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_mass_fraction"]
                evidence_path = root / property_record["property_record_evidence"]["relative_path"]
                evidence_path.write_bytes(corrupt(evidence_path.read_bytes()))
                self.assertDiagnostic(
                    expected_code,
                    lambda: verify_evidence_bindings(normalize_diagnostic_json(payload), evidence_root=root),
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            original_relative = "properties/base-mass_solids_nonvolatile_density.json"
            canonical_bytes = (root / original_relative).read_bytes()
            prefix = b"unrelated-prefix\n"
            ranged_relative = "properties/shared-ranged-property-record.bin"
            (root / ranged_relative).write_bytes(prefix + canonical_bytes + b"X")
            exact_locator = _locator(
                ranged_relative, offset=len(prefix), length=len(canonical_bytes)
            )
            for card in payload["cards"]:
                for component in card["formula"]["components"]:
                    for property_record in component["property_records"].values():
                        if property_record["property_record_evidence"]["relative_path"] == original_relative:
                            property_record["property_record_evidence"] = copy.deepcopy(exact_locator)
            exact_bundle = normalize_diagnostic_json(payload)
            materialized = verify_evidence_bindings(exact_bundle, evidence_root=root)
            ranged_records = [
                record
                for record in materialized.evidence_verification["records"]
                if record["relative_path"] == ranged_relative
            ]
            self.assertTrue(ranged_records)
            self.assertTrue(all(record["byte_length"] == len(canonical_bytes) for record in ranged_records))
            for delta in (-1, 1):
                with self.subTest(range_delta=delta):
                    bad = copy.deepcopy(payload)
                    for card in bad["cards"]:
                        for component in card["formula"]["components"]:
                            for property_record in component["property_records"].values():
                                locator = property_record["property_record_evidence"]
                                if locator["relative_path"] == ranged_relative:
                                    locator["record_locator"]["byte_length"] = len(canonical_bytes) + delta
                    self.assertDiagnostic(
                        "EVIDENCE_JSON_MALFORMED",
                        lambda bad=bad: verify_evidence_bindings(normalize_diagnostic_json(bad), evidence_root=root),
                    )

    def test_preflight_and_plan_crosscheck_canonical_properties_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_root = temporary_root / "evidence"
            payload = _valid_payload(evidence_root)
            property_record = payload["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_mass_fraction"]
            evidence_path = evidence_root / property_record["property_record_evidence"]["relative_path"]
            canonical_record = json.loads(evidence_path.read_text(encoding="utf-8"))
            canonical_record["properties"]["nonvolatile_mass_fraction"]["value"] = 0.6
            evidence_path.write_bytes(canonical_json_bytes(canonical_record))
            input_path = temporary_root / "diagnostic.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            output_dir = temporary_root / "preflight-output"
            self.assertDiagnostic(
                "PROPERTY_EVIDENCE_FIELD_MISMATCH",
                lambda: preflight_from_files(
                    input_format="json",
                    input_path=input_path,
                    evidence_root=evidence_root,
                    output_dir=output_dir,
                ),
            )
            self.assertFalse(output_dir.exists())

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_root = temporary_root / "evidence"
            plan_input = _weighing_plan_input(evidence_root, MASS_SOLIDS_NONVOLATILE_DENSITY)
            plan_input["components"][0]["property_records"]["nonvolatile_mass_fraction"]["value"] = 0.6
            input_path = temporary_root / "plan-input.json"
            output_path = temporary_root / "plan-output.json"
            input_path.write_text(json.dumps(plan_input), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "generate-weighing-plan",
                        "--input",
                        str(input_path),
                        "--evidence-root",
                        str(evidence_root),
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("PROPERTY_EVIDENCE_FIELD_MISMATCH", stderr.getvalue())
            self.assertFalse(output_path.exists())

    def test_actual_weighing_must_not_be_after_cure_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            after = copy.deepcopy(payload)
            after["cards"][0]["formula"]["components"][0]["actual_weighing"]["weighed_at"] = "2026-07-13T09:00:01+09:00"
            self.assertDiagnostic(
                "WEIGHING_CURE_TIMELINE", lambda: normalize_diagnostic_json(after)
            )
            at_start = copy.deepcopy(payload)
            at_start["cards"][0]["formula"]["components"][0]["actual_weighing"]["weighed_at"] = at_start["locked_conditions"]["cure_start"]
            self.assertEqual(
                structural_preflight_four_card(normalize_diagnostic_json(at_start)).payload["status"],
                "structural_valid",
            )

    def test_weighing_plan_inverts_both_routes_and_cli_writes_plan_not_actual(self) -> None:
        for route in (MASS_SOLIDS_NONVOLATILE_DENSITY, WET_DENSITY_VOLUME_SOLIDS):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as temporary:
                temporary_root = Path(temporary)
                evidence_root = temporary_root / "evidence"
                plan_input = _weighing_plan_input(evidence_root, route)
                plan = generate_weighing_plan(plan_input, evidence_root=evidence_root)
                self.assertEqual(plan["plan_status"], "planned_not_actual")
                self.assertFalse(plan["plan_is_actual_weighing_evidence"])
                self.assertNotIn("actual_wet_mass_g", json.dumps(plan))
                plan_with_actual = copy.deepcopy(plan_input)
                plan_with_actual["components"][0]["actual_weighing"] = {}
                self.assertDiagnostic(
                    "UNKNOWN_FIELD",
                    lambda: generate_weighing_plan(
                        plan_with_actual, evidence_root=evidence_root
                    ),
                )
                by_component = {item["component_id"]: item for item in plan["components"]}
                self.assertAlmostEqual(by_component["base-waterborne-clear"]["target_wet_mass_g"], 17.0)
                self.assertAlmostEqual(by_component["colorant-W064"]["target_wet_mass_g"], 3.0)
                recovered = []
                for component in plan["components"]:
                    properties = component["property_records"]
                    if route == MASS_SOLIDS_NONVOLATILE_DENSITY:
                        volume = (
                            component["target_wet_mass_g"]
                            * properties["nonvolatile_mass_fraction"]["value"]
                            / properties["nonvolatile_density_g_ml"]["value"]
                        )
                    else:
                        volume = (
                            component["target_wet_mass_g"]
                            / properties["wet_density_g_ml"]["value"]
                            * properties["component_nonvolatile_volume_fraction"]["value"]
                        )
                    recovered.append((component["component_id"], volume))
                recovered_total = sum(value for _component_id, value in recovered)
                for component_id, volume in recovered:
                    self.assertAlmostEqual(
                        volume / recovered_total,
                        by_component[component_id]["target_nonvolatile_volume_fraction"],
                    )
                input_path = temporary_root / "plan-input.json"
                output_path = temporary_root / "plan-output.json"
                input_path.write_text(json.dumps(plan_input), encoding="utf-8")
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "generate-weighing-plan",
                            "--input",
                            str(input_path),
                            "--evidence-root",
                            str(evidence_root),
                            "--output",
                            str(output_path),
                        ]
                    )
                self.assertEqual(exit_code, 0, stderr.getvalue())
                self.assertEqual(json.loads(stdout.getvalue())["status"], "planned_not_actual")
                self.assertTrue(output_path.is_file())
                self.assertTrue(output_path.with_name(f"{output_path.name}.sha256").is_file())

    def test_preflight_receipt_is_portable_and_rechecks_every_evidence_class(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_root = temporary_root / "evidence"
            payload = _valid_payload(evidence_root)
            input_path = temporary_root / "completed.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            result = preflight_from_files(
                input_format="json",
                input_path=input_path,
                evidence_root=evidence_root,
                output_dir=temporary_root / "output",
            )
            self.assertEqual(result["status"], "evidence_ready")
            receipt_path = temporary_root / "output" / "preflight-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["status"], "evidence_ready")
            self.assertIn("evidence_verification", receipt["bindings"])
            self.assertNotIn(str(evidence_root), json.dumps(receipt))
            copied_root = temporary_root / "copied-evidence"
            shutil.copytree(evidence_root, copied_root)
            report = verify_preflight_receipt(receipt_path=receipt_path, evidence_root=copied_root)
            self.assertTrue(report["evidence_still_matches_receipt"])
            for relative in (
                "labels/base.txt",
                "weighing/CARD-DX-BASE-DFT-L-001.actual-weighing.json",
                "properties/base-mass_solids_nonvolatile_density.json",
                "dft/CARD-DX-BASE-DFT-L-001-black.txt",
                "instrument/calibration.txt",
                "instrument/run-log.txt",
                "raw/bare-black-01.csv",
                "raw/CARD-DX-BASE-DFT-L-001-black-POS01.csv",
                "registry/current-batch-component-registry-v1.json",
            ):
                target = copied_root / relative
                target.write_bytes(target.read_bytes() + b" changed")
                self.assertDiagnostic("EVIDENCE_FILE_HASH", lambda: verify_preflight_receipt(receipt_path=receipt_path, evidence_root=copied_root))
                shutil.copy2(evidence_root / relative, target)

    def test_real_evidence_failures_leave_no_preflight_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_root = temporary_root / "evidence"
            payload = _valid_payload(evidence_root)
            bundle = normalize_diagnostic_json(payload)
            self.assertDiagnostic("EVIDENCE_ROOT", lambda: verify_evidence_bindings(bundle, evidence_root=temporary_root / "missing"))
            missing = copy.deepcopy(payload)
            missing["materials"]["base"]["physical_label_evidence"]["relative_path"] = "labels/missing.txt"
            self.assertDiagnostic("EVIDENCE_FILE", lambda: verify_evidence_bindings(normalize_diagnostic_json(missing), evidence_root=evidence_root))
            traversal = copy.deepcopy(payload)
            traversal["readings"][0]["raw_spectrum_evidence"]["relative_path"] = "raw/../escape.csv"
            self.assertDiagnostic("EVIDENCE_PATH", lambda: normalize_diagnostic_json(traversal))
            absolute = copy.deepcopy(payload)
            absolute["readings"][0]["raw_spectrum_evidence"]["relative_path"] = "C:/escape.csv"
            self.assertDiagnostic("EVIDENCE_PATH", lambda: normalize_diagnostic_json(absolute))
            invalid_range = copy.deepcopy(payload)
            invalid_range["readings"][0]["raw_spectrum_evidence"] = _locator("raw/CARD-DX-BASE-DFT-L-001-black-POS01.csv", offset=0, length=10000)
            self.assertDiagnostic("EVIDENCE_RANGE", lambda: verify_evidence_bindings(normalize_diagnostic_json(invalid_range), evidence_root=evidence_root))
            empty_range = copy.deepcopy(payload)
            empty_range["readings"][0]["raw_spectrum_evidence"] = _locator("raw/CARD-DX-BASE-DFT-L-001-black-POS01.csv", offset=0, length=0)
            self.assertDiagnostic("INTEGER", lambda: normalize_diagnostic_json(empty_range))
            directory = copy.deepcopy(payload)
            (evidence_root / "raw" / "directory").mkdir()
            directory["readings"][0]["raw_spectrum_evidence"] = _locator("raw/directory")
            self.assertDiagnostic("EVIDENCE_FILE", lambda: verify_evidence_bindings(normalize_diagnostic_json(directory), evidence_root=evidence_root))
            original_sha256_file = __import__("km_calibration.diagnostic", fromlist=["sha256_file"]).sha256_file
            mutated = {"done": False}

            def mutate_after_hash(path: Path) -> str:
                digest = original_sha256_file(path)
                if path.name == "base.txt" and not mutated["done"]:
                    path.write_bytes(path.read_bytes() + b" mutation")
                    mutated["done"] = True
                return digest

            with patch("km_calibration.diagnostic.sha256_file", side_effect=mutate_after_hash):
                self.assertDiagnostic("EVIDENCE_FILE_MUTATED", lambda: verify_evidence_bindings(bundle, evidence_root=evidence_root))
            input_path = temporary_root / "bad.json"
            input_path.write_text(json.dumps(missing), encoding="utf-8")
            self.assertDiagnostic(
                "EVIDENCE_FILE",
                lambda: preflight_from_files(
                    input_format="json", input_path=input_path, evidence_root=evidence_root, output_dir=temporary_root / "output"
                ),
            )
            self.assertFalse((temporary_root / "output").exists())

    def test_real_symlink_escape_is_rejected_or_skipped_with_platform_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_root = temporary_root / "evidence"
            payload = _valid_payload(evidence_root)
            outside_file = temporary_root / "outside-label.txt"
            outside_file.write_text("outside evidence", encoding="utf-8")
            link_path = evidence_root / "labels" / "outside-link.txt"
            try:
                link_path.symlink_to(outside_file)
            except OSError as error:
                self.skipTest(
                    f"platform does not permit Python symlink creation for the escape probe: {error}"
                )
            payload["materials"]["base"]["physical_label_evidence"] = _locator(
                "labels/outside-link.txt"
            )
            self.assertDiagnostic(
                "EVIDENCE_ROOT_ESCAPE",
                lambda: verify_evidence_bindings(
                    normalize_diagnostic_json(payload), evidence_root=evidence_root
                ),
            )

    def test_shared_raw_export_accepts_nonoverlapping_ranges_and_rejects_reuse_or_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            raw_uses = [
                *[measurement for backing in BACKINGS for measurement in payload["backings"][backing]["measurements"]],
                *payload["readings"],
            ]
            shared = b"".join(f"record-{index:02d}\n".encode("ascii") for index in range(len(raw_uses)))
            (root / "raw" / "shared-export.csv").write_bytes(shared)
            cursor = 0
            for index, item in enumerate(raw_uses):
                segment = f"record-{index:02d}\n".encode("ascii")
                field = "raw_export_evidence" if "raw_export_evidence" in item else "raw_spectrum_evidence"
                item[field] = _locator("raw/shared-export.csv", offset=cursor, length=len(segment))
                cursor += len(segment)
            bundle = normalize_diagnostic_json(payload)
            self.assertEqual(preflight_four_card(bundle, evidence_root=root).payload["status"], "evidence_ready")
            duplicate = copy.deepcopy(payload)
            duplicate["readings"][1]["raw_spectrum_evidence"] = copy.deepcopy(duplicate["readings"][0]["raw_spectrum_evidence"])
            self.assertDiagnostic("EVIDENCE_RECORD_DUPLICATE", lambda: verify_evidence_bindings(normalize_diagnostic_json(duplicate), evidence_root=root))
            overlap = copy.deepcopy(payload)
            overlap["readings"][1]["raw_spectrum_evidence"] = _locator("raw/shared-export.csv", offset=1, length=6)
            self.assertDiagnostic("EVIDENCE_RECORD_OVERLAP", lambda: verify_evidence_bindings(normalize_diagnostic_json(overlap), evidence_root=root))
            cross_class = copy.deepcopy(payload)
            cross_class["backings"]["black"]["measurements"][0]["raw_export_evidence"] = copy.deepcopy(cross_class["readings"][0]["raw_spectrum_evidence"])
            self.assertDiagnostic("EVIDENCE_RECORD_DUPLICATE", lambda: verify_evidence_bindings(normalize_diagnostic_json(cross_class), evidence_root=root))

    def test_shared_dft_export_requires_distinct_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            regions = [card["dft_by_backing"][backing] for card in payload["cards"] for backing in BACKINGS]
            segments = [f"dft-{index:02d}\n".encode("ascii") for index in range(len(regions))]
            (root / "dft" / "shared.txt").write_bytes(b"".join(segments))
            offset = 0
            for region, segment in zip(regions, segments, strict=True):
                region["dft_record_evidence"] = _locator("dft/shared.txt", offset=offset, length=len(segment))
                offset += len(segment)
            self.assertEqual(preflight_four_card(normalize_diagnostic_json(payload), evidence_root=root).payload["status"], "evidence_ready")
            duplicate = copy.deepcopy(payload)
            duplicate["cards"][0]["dft_by_backing"]["white"]["dft_record_evidence"] = copy.deepcopy(
                duplicate["cards"][0]["dft_by_backing"]["black"]["dft_record_evidence"]
            )
            self.assertDiagnostic("EVIDENCE_RECORD_DUPLICATE", lambda: verify_evidence_bindings(normalize_diagnostic_json(duplicate), evidence_root=root))

    def test_registry_parsing_and_v1_schema_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            bundle = normalize_diagnostic_json(payload)
            registry = root / "registry" / "current-batch-component-registry-v1.json"
            registry.write_text('{"schema_version":"x","components":[]}', encoding="utf-8")
            self.assertDiagnostic("REGISTRY_SCHEMA", lambda: verify_evidence_bindings(bundle, evidence_root=root))
            registry.write_text('{"schema_version":"moocow-current-batch-component-registry-v1","schema_version":"x","components":[]}', encoding="utf-8")
            self.assertDiagnostic("JSON_DUPLICATE_KEY", lambda: verify_evidence_bindings(bundle, evidence_root=root))
            registry.write_text('{"schema_version":"moocow-current-batch-component-registry-v1","components":[]}', encoding="utf-8")
            self.assertDiagnostic("REGISTRY", lambda: verify_evidence_bindings(bundle, evidence_root=root))
            legacy = copy.deepcopy(payload)
            legacy["schema_version"] = "moocow-physical-diagnostic-acquisition-v1"
            self.assertDiagnostic("SCHEMA_VERSION", lambda: normalize_diagnostic_json(legacy))

    def test_component_property_weighing_and_registry_physical_lots_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            component_lot = copy.deepcopy(payload)
            component_lot["cards"][0]["formula"]["components"][0]["physical_lot_id"] = "OTHER-LOT"
            self.assertDiagnostic("PHYSICAL_LOT", lambda: normalize_diagnostic_json(component_lot))
            property_lot = copy.deepcopy(payload)
            property_lot["cards"][0]["formula"]["components"][0]["property_records"]["nonvolatile_density_g_ml"]["physical_lot_id"] = "OTHER-LOT"
            self.assertDiagnostic("PROPERTY_LOT", lambda: normalize_diagnostic_json(property_lot))
            weighing_lot = copy.deepcopy(payload)
            weighing_lot["cards"][0]["formula"]["components"][0]["actual_weighing"]["physical_lot_id"] = "OTHER-LOT"
            self.assertDiagnostic("WEIGHING_LOT", lambda: normalize_diagnostic_json(weighing_lot))

            bundle = normalize_diagnostic_json(payload)
            registry_path = root / "registry" / "current-batch-component-registry-v1.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            base = next(
                item for item in registry["components"] if item["component_id"] == "base-waterborne-clear"
            )
            base["batch_id"] = "OTHER-LOT"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            self.assertDiagnostic(
                "REGISTRY_LOT_MISMATCH",
                lambda: verify_evidence_bindings(bundle, evidence_root=root),
            )
            base["batch_id"] = "LOT-BASE-01"
            base["lot_verification_status"] = "CATALOG_EVIDENCE_ONLY"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            self.assertDiagnostic(
                "REGISTRY_LOT_VERIFICATION",
                lambda: verify_evidence_bindings(bundle, evidence_root=root),
            )

    def test_repaired_structural_gates_remain_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            payload = _valid_payload(root)
            reused_measurement = copy.deepcopy(payload)
            for reading in reused_measurement["readings"]:
                reading["instrument_measurement_id"] = "MSR-REUSED"
            self.assertDiagnostic("INSTRUMENT_MEASUREMENT_DUPLICATE", lambda: normalize_diagnostic_json(reused_measurement))
            cross_family = copy.deepcopy(payload)
            cross_family["cards"][1]["formula"]["formula_batch_id"] = cross_family["cards"][0]["formula"]["formula_batch_id"]
            self.assertDiagnostic("FORMULA_PROVENANCE", lambda: normalize_diagnostic_json(cross_family))
            one_bare = copy.deepcopy(payload)
            one_bare["backings"]["black"]["measurements"] = one_bare["backings"]["black"]["measurements"][:1]
            self.assertDiagnostic("BACKING_MEASUREMENT_COUNT", lambda: normalize_diagnostic_json(one_bare))
            equal_backings = copy.deepcopy(payload)
            for index, measurement in enumerate(equal_backings["backings"]["white"]["measurements"]):
                measurement["reflectance"] = list(equal_backings["backings"]["black"]["measurements"][index]["reflectance"])
            self.assertDiagnostic("BACKING_SPECTRA_IDENTICAL", lambda: normalize_diagnostic_json(equal_backings))
            bad_calibration = copy.deepcopy(payload)
            bad_calibration["locked_conditions"]["instrument_calibration_result"] = "failed"
            _rebind_conditions(bad_calibration)
            self.assertDiagnostic("CALIBRATION_RESULT", lambda: normalize_diagnostic_json(bad_calibration))
            bad_rh = copy.deepcopy(payload)
            bad_rh["locked_conditions"]["cure_rh_pct_observed"] = 150.0
            _rebind_conditions(bad_rh)
            self.assertDiagnostic("RH_RANGE", lambda: normalize_diagnostic_json(bad_rh))
            bad_grid = copy.deepcopy(payload)
            bad_grid["wavelength_nm"][1] = 405.0
            self.assertDiagnostic("GRID_NONUNIFORM", lambda: normalize_diagnostic_json(bad_grid))
            timezone_naive = copy.deepcopy(payload)
            timezone_naive["locked_conditions"]["instrument_calibration_timestamp"] = "2026-07-14T09:00:00"
            _rebind_conditions(timezone_naive)
            self.assertDiagnostic("TIMESTAMP_TIMEZONE", lambda: normalize_diagnostic_json(timezone_naive))
            before_cure = copy.deepcopy(payload)
            before_cure["locked_conditions"]["instrument_calibration_timestamp"] = "2026-07-14T06:00:00+09:00"
            before_cure["readings"][0]["measured_at_local"] = "2026-07-14T07:00:00+09:00"
            _rebind_conditions(before_cure)
            self.assertDiagnostic("COATED_CURE_TIMELINE", lambda: normalize_diagnostic_json(before_cure))
            bad_surface = copy.deepcopy(payload)
            bad_surface["readings"][0]["surface_status"] = "uneven_film"
            self.assertDiagnostic("SURFACE_STATUS", lambda: structural_preflight_four_card(normalize_diagnostic_json(bad_surface)))
            bad_model = copy.deepcopy(payload)
            bad_model["readings"][0]["model_applicability_status"] = "hold_for_review"
            self.assertDiagnostic("MODEL_APPLICABILITY_STATUS", lambda: structural_preflight_four_card(normalize_diagnostic_json(bad_model)))

    def test_csv_cli_bind_and_receipt_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_root = temporary_root / "evidence"
            payload = _valid_payload(evidence_root)
            manifest, rows = _csv_manifest_and_rows(payload)
            manifest_path = temporary_root / "manifest.json"
            csv_path = temporary_root / "readings.csv"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(rows)
            csv_path.write_text(output.getvalue(), encoding="utf-8-sig", newline="")
            self.assertEqual(main(["validate-four-card-structure", "--format", "csv", "--manifest", str(manifest_path), "--input", str(csv_path)]), 0)
            self.assertEqual(
                main(["bind-evidence-record", "--evidence-root", str(evidence_root), "--relative-path", "labels/base.txt", "--whole-file"]),
                0,
            )
            self.assertEqual(
                main(["preflight-four-card", "--format", "csv", "--manifest", str(manifest_path), "--input", str(csv_path), "--output-dir", str(temporary_root / "no-root")]),
                2,
            )
            result = preflight_from_files(
                input_format="csv",
                input_path=csv_path,
                manifest_path=manifest_path,
                evidence_root=evidence_root,
                output_dir=temporary_root / "output",
            )
            self.assertEqual(result["status"], "evidence_ready")
            receipt_path = temporary_root / "output" / "preflight-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            original_receipt = copy.deepcopy(receipt)
            receipt["bindings"]["evidence_verification"]["evidence_verification_sha256"] = "f" * 64
            receipt_without_hash = dict(receipt)
            receipt_without_hash.pop("receipt_payload_sha256")
            receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(receipt_without_hash))
            write_json_with_sha256(receipt_path, receipt)
            self.assertDiagnostic("EVIDENCE_VERIFICATION_SHA256", lambda: verify_preflight_receipt(receipt_path=receipt_path, evidence_root=evidence_root))
            write_json_with_sha256(receipt_path, original_receipt)
            receipt_path.with_name(f"{receipt_path.name}.sha256").write_text("not-a-digest\n", encoding="ascii")
            self.assertDiagnostic("RECEIPT_SIDECAR", lambda: verify_preflight_receipt(receipt_path=receipt_path, evidence_root=evidence_root))
            changed_payload_sha = copy.deepcopy(original_receipt)
            changed_payload_sha["diagnostic_payload_sha256"] = "a" * 64
            changed_without_self_hash = dict(changed_payload_sha)
            changed_without_self_hash.pop("receipt_payload_sha256")
            changed_payload_sha["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(changed_without_self_hash))
            write_json_with_sha256(receipt_path, changed_payload_sha)
            self.assertDiagnostic("NORMALIZED_PAYLOAD_SHA256", lambda: verify_preflight_receipt(receipt_path=receipt_path, evidence_root=evidence_root))

    def test_legacy_v1_cli_rejection_writes_no_stdout_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_root = temporary_root / "evidence"
            payload = _valid_payload(evidence_root)
            payload["schema_version"] = "moocow-physical-diagnostic-acquisition-v1"
            input_path = temporary_root / "legacy-v1.json"
            output_dir = temporary_root / "legacy-output"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "preflight-four-card",
                        "--format",
                        "json",
                        "--input",
                        str(input_path),
                        "--evidence-root",
                        str(evidence_root),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("SCHEMA_VERSION", stderr.getvalue())
            self.assertFalse(output_dir.exists())

    def test_verify_receipt_cli_accepts_relocation_and_rejects_tampering_without_success_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_root = temporary_root / "evidence"
            payload = _valid_payload(evidence_root)
            input_path = temporary_root / "completed.json"
            output_dir = temporary_root / "preflight"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            preflight_from_files(
                input_format="json",
                input_path=input_path,
                evidence_root=evidence_root,
                output_dir=output_dir,
            )
            copied_root = temporary_root / "relocated-evidence"
            shutil.copytree(evidence_root, copied_root)
            receipt_path = output_dir / "preflight-receipt.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "verify-four-card-receipt",
                        "--receipt",
                        str(receipt_path),
                        "--evidence-root",
                        str(copied_root),
                    ]
                )
            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertTrue(json.loads(stdout.getvalue())["evidence_still_matches_receipt"])

            tampered = copied_root / "labels" / "base.txt"
            tampered.write_bytes(tampered.read_bytes() + b" tampered")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = main(
                    [
                        "verify-four-card-receipt",
                        "--receipt",
                        str(receipt_path),
                        "--evidence-root",
                        str(copied_root),
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("EVIDENCE_FILE_HASH", stderr.getvalue())

    def test_prepared_pack_is_deterministic_and_placeholders_remain_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first_result = prepare_four_card(REGISTRY, first)
            second_result = prepare_four_card(REGISTRY, second)
            self.assertEqual(first_result["registry_sha256"], second_result["registry_sha256"])
            for filename in first_result["files"]:
                self.assertEqual((first / filename).read_bytes(), (second / filename).read_bytes())
            self.assertTrue((first / "evidence" / "registry" / "current-batch-component-registry-v1.json").is_file())
            self.assertFalse((first / "diagnostic-manifest.template.json").exists())
            self.assertTrue((first / "diagnostic-manifest.mass_solids_nonvolatile_density.template.json").is_file())
            self.assertTrue((first / "diagnostic-manifest.wet_density_volume_solids.template.json").is_file())
            self.assertIn("plan only, never actual weighing evidence", (first / "OPERATOR_README.md").read_text(encoding="utf-8"))
            self.assertDiagnostic(
                "PLACEHOLDER",
                lambda: normalize_diagnostic_json(
                    json.loads(
                        (
                            first
                            / "diagnostic-manifest.mass_solids_nonvolatile_density.template.json"
                        ).read_text(encoding="utf-8")
                    )
                ),
            )

    def test_output_reuse_and_dataset_fitter_isolation_remain_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_root = temporary_root / "evidence"
            payload = _valid_payload(evidence_root)
            input_path = temporary_root / "input.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            output_dir = temporary_root / "output"
            self.assertEqual(
                preflight_from_files(input_format="json", input_path=input_path, evidence_root=evidence_root, output_dir=output_dir)["status"],
                "evidence_ready",
            )
            prior_receipt = (output_dir / "preflight-receipt.json").read_bytes()
            self.assertDiagnostic(
                "OUTPUT_DIR_NOT_EMPTY",
                lambda: preflight_from_files(input_format="json", input_path=input_path, evidence_root=evidence_root, output_dir=output_dir),
            )
            self.assertEqual((output_dir / "preflight-receipt.json").read_bytes(), prior_receipt)
            bundle = normalize_diagnostic_json(payload)
            with patch("km_calibration.schema.load_and_validate_dataset", side_effect=AssertionError("dataset loader called")), patch(
                "km_calibration.pipeline.fit_km", side_effect=AssertionError("fitter called")
            ):
                preflight_four_card(bundle, evidence_root=evidence_root)
            materialized = verify_evidence_bindings(bundle, evidence_root=evidence_root)
            with tempfile.TemporaryDirectory() as artifact_temporary:
                artifact_root = Path(artifact_temporary)
                write_json_with_sha256(artifact_root / "manifest.json", materialized.payload)
                with self.assertRaisesRegex(DatasetValidationError, "Unsupported dataset schema_version"):
                    load_and_validate_dataset(artifact_root)


if __name__ == "__main__":
    unittest.main()
