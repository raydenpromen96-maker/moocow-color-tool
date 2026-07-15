from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(CALIBRATION_ROOT / "tests"))

from acquisition_preflight_fixtures import (
    assertPreflight,
    assert_permissions_all_false,
    assert_sidecar_matches,
    write_current_materials,
    write_frozen_pilot_prerequisite,
    write_pilot_batch_roots,
)
from km_calibration.acquisition_preflight import (
    COMPONENT_IDS,
    MASS_SOLIDS_NONVOLATILE_DENSITY,
    WET_DENSITY_VOLUME_SOLIDS,
    _rank_receipt,
    commit_holdout_custody,
    preflight_open_batches,
    preflight_pilot_materials,
)
from km_calibration.hashing import canonical_json_bytes, sha256_bytes, write_json_with_sha256


class AcquisitionPreflightUnitTests(unittest.TestCase):
    def _source(self, root: Path, *, route: str = MASS_SOLIDS_NONVOLATILE_DENSITY) -> dict[str, Any]:
        prerequisite = write_frozen_pilot_prerequisite(root)
        shared_root, materials = write_current_materials(root, prerequisite, route)
        roots = write_pilot_batch_roots(root, shared_root, materials, route)
        return {"prerequisite": prerequisite, "shared_root": shared_root, "materials": materials, **roots}

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        text = format(value.normalize(), "f")
        return "0" if text in {"", "-0"} else text

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_raw_json_with_sidecar(path: Path, raw: str) -> None:
        path.write_text(raw, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_name(f"{path.name}.sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")

    def _common(self, source: dict[str, Any], root: Path) -> Path:
        result = preflight_pilot_materials(
            **source["prerequisite"],
            shared_root=source["shared_root"],
            output_dir=root / "common-output",
        )
        assert_permissions_all_false(result)
        return root / "common-output" / "common-material-receipt.json"

    def _open(self, source: dict[str, Any], common_receipt: Path, root: Path) -> Path:
        result = preflight_open_batches(
            materials_receipt_path=common_receipt,
            open_batch_root=source["open_batch_root"],
            open_evidence_root=source["open_evidence_root"],
            output_dir=root / "open-output",
        )
        assert_permissions_all_false(result)
        return root / "open-output" / "open-batch-preflight-receipt.json"

    def _material_manifest(self, source: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        path = source["shared_root"] / "materials.json"
        return path, self._read_json(path)

    def _open_manifest(self, source: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        path = source["open_batch_root"] / "batches.json"
        return path, self._read_json(path)

    def _property_path(self, source: dict[str, Any], formula_key: str) -> Path:
        return source["shared_root"] / "properties" / f"{formula_key}.json"

    def _rewrite_component_mass(
        self,
        source: dict[str, Any],
        batch: dict[str, Any],
        formula_key: str,
        mass: Decimal,
    ) -> None:
        material = next(item for item in source["materials"] if item["formula_key"] == formula_key)
        component = next(item for item in batch["actual_components"] if item["component_id"] == material["component_id"])
        evidence_path = source["open_evidence_root"] / component["actual_weighing_evidence"]["relative_path"]
        weighing = self._read_json(evidence_path)
        matching_entries = [
            entry
            for entry in weighing["entries"]
            if entry["component_id"] == material["component_id"] and entry["physical_lot_id"] == material["physical_lot_id"]
        ]
        self.assertEqual(len(matching_entries), 1)
        matching_entries[0]["actual_wet_mass_g"] = self._decimal_text(mass)
        write_json_with_sha256(evidence_path, weighing)

        property_record = self._read_json(self._property_path(source, formula_key))
        properties = property_record["properties"]
        if material["conversion"]["route"] == MASS_SOLIDS_NONVOLATILE_DENSITY:
            volume = mass * Decimal(properties["nonvolatile_mass_fraction"]["value"]) / Decimal(
                properties["nonvolatile_density_g_ml"]["value"]
            )
        else:
            volume = mass / Decimal(properties["wet_density_g_ml"]["value"]) * Decimal(
                properties["component_nonvolatile_volume_fraction"]["value"]
            )
        component["actual_wet_mass_g"] = self._decimal_text(mass)
        component["nonvolatile_volume_ml"] = self._decimal_text(volume)
        volumes = {item["component_id"]: Decimal(item["nonvolatile_volume_ml"]) for item in batch["actual_components"]}
        total = sum(volumes.values(), Decimal(0))
        batch["actual_nv_vector"] = [
            self._decimal_text(volumes.get(component_id, Decimal(0)) / total) for component_id in COMPONENT_IDS
        ]
        batch["actual_nv_sum"] = self._decimal_text(sum((Decimal(value) for value in batch["actual_nv_vector"]), Decimal(0)))

    def _set_train_tint_masses(self, source: dict[str, Any], mass: Decimal) -> None:
        manifest_path, manifest = self._open_manifest(source)
        material_by_component = {item["component_id"]: item for item in source["materials"]}
        for batch in manifest["batches"]:
            if batch["split"] != "train" or batch["formula_family_id"] == "FAM-TR-BASIS-BASE":
                continue
            tint = next(item for item in batch["actual_components"] if item["component_id"] != "base-waterborne-clear")
            self._rewrite_component_mass(source, batch, material_by_component[tint["component_id"]]["formula_key"], mass)
        write_json_with_sha256(manifest_path, manifest)

    def test_accepts_exactly_fifteen_verified_current_lots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            receipt = self._common(source, root)
            value = self._read_json(receipt)
            self.assertEqual(len(value["materials"]), 15)

    def test_rejects_material_count_other_than_fifteen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manifest_path, manifest = self._material_manifest(source)
            manifest["materials"].pop()
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight(
                "MATERIAL_COUNT",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_reordered_component_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manifest_path, manifest = self._material_manifest(source)
            manifest["component_order"][1], manifest["component_order"][2] = manifest["component_order"][2], manifest["component_order"][1]
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight(
                "COMPONENT_ORDER",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_numeric_073_component_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manifest_path, manifest = self._material_manifest(source)
            manifest["component_order"][9]["formula_key"] = 73
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight(
                "COMPONENT_KEY_073",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_wrong_073_component_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manifest_path, manifest = self._material_manifest(source)
            manifest["component_order"][9]["component_id"] = "colorant-Y83S"
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight(
                "COMPONENT_KEY_073",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_catalog_only_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manifest_path, manifest = self._material_manifest(source)
            manifest["materials"][0]["label_verification"]["status"] = "catalog_only"
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight(
                "REGISTRY_LOT_VERIFICATION",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_placeholder_label_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manifest_path, manifest = self._material_manifest(source)
            manifest["materials"][0]["label_verification"]["verification_id"] = "REQUIRED_LABEL_VERIFICATION"
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight(
                "PLACEHOLDER",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_derives_nonvolatile_volume_with_mass_solids_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, route=MASS_SOLIDS_NONVOLATILE_DENSITY)
            common = self._common(source, root)
            self._open(source, common, root)
            rank = self._read_json(root / "open-output" / "actual-nv-rank-receipt.json")
            base = rank["rows"][1]["components"][0]
            expected_volume = Decimal("83") * Decimal("0.41") / Decimal("1.01")
            expected_fraction = expected_volume / (expected_volume + Decimal("12") * Decimal("0.411") / Decimal("1.02"))
            self.assertEqual(Decimal(base["nonvolatile_volume_ml_decimal"]), expected_volume)
            self.assertEqual(Decimal(base["x_decimal"]), expected_fraction)

    def test_derives_nonvolatile_volume_with_wet_density_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root, route=WET_DENSITY_VOLUME_SOLIDS)
            common = self._common(source, root)
            self._open(source, common, root)
            rank = self._read_json(root / "open-output" / "actual-nv-rank-receipt.json")
            base = rank["rows"][1]["components"][0]
            expected_volume = Decimal("83") / Decimal("1.03") * Decimal("0.43")
            expected_fraction = expected_volume / (expected_volume + Decimal("12") / Decimal("1.04") * Decimal("0.431"))
            self.assertEqual(Decimal(base["nonvolatile_volume_ml_decimal"]), expected_volume)
            self.assertEqual(Decimal(base["x_decimal"]), expected_fraction)

    def test_rejects_mixed_conversion_routes_inside_formula_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            receipt = self._read_json(common)
            material = next(item for item in receipt["materials"] if item["component_id"] == "colorant-Y83S")
            material["conversion_route"] = WET_DENSITY_VOLUME_SOLIDS
            material["properties"] = {
                "wet_density_g_ml": {
                    "property_record_id": "PROP-Y83S-WET",
                    "value": "1.04",
                    "unit": "g/mL",
                    "method": "validated wet density method",
                    "observed_at": "2026-07-14T21:00:00+09:00",
                },
                "component_nonvolatile_volume_fraction": {
                    "property_record_id": "PROP-Y83S-VOLUME",
                    "value": "0.431",
                    "unit": "fraction",
                    "method": "validated volume solids method",
                    "observed_at": "2026-07-14T21:01:00+09:00",
                },
            }
            receipt_payload = dict(receipt)
            receipt_payload.pop("receipt_payload_sha256")
            receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(receipt_payload))
            write_json_with_sha256(common, receipt)
            manifest_path, manifest = self._open_manifest(source)
            batch = manifest["batches"][1]
            tint = next(item for item in batch["actual_components"] if item["component_id"] == "colorant-Y83S")
            tint["conversion_route"] = WET_DENSITY_VOLUME_SOLIDS
            tint["nonvolatile_volume_ml"] = self._decimal_text(Decimal("12") / Decimal("1.04") * Decimal("0.431"))
            volumes = {item["component_id"]: Decimal(item["nonvolatile_volume_ml"]) for item in batch["actual_components"]}
            total = sum(volumes.values(), Decimal(0))
            batch["actual_nv_vector"] = [self._decimal_text(volumes.get(component_id, Decimal(0)) / total) for component_id in COMPONENT_IDS]
            batch["actual_nv_sum"] = self._decimal_text(sum((Decimal(value) for value in batch["actual_nv_vector"]), Decimal(0)))
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight("CONVERSION_ROUTE_MIXED", lambda: self._open(source, common, root))

    def test_rejects_noncanonical_mass_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            evidence = next((source["open_evidence_root"] / "weighings").glob("*.json"))
            payload = self._read_json(evidence)
            payload["entries"][0]["actual_wet_mass_unit"] = "mg"
            write_json_with_sha256(evidence, payload)
            assertPreflight("PROPERTY_UNIT", lambda: self._open(source, common, root))

    def test_rejects_noncanonical_density_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            property_path = self._property_path(source, "base")
            payload = self._read_json(property_path)
            payload["properties"]["nonvolatile_density_g_ml"]["unit"] = "kg/L"
            write_json_with_sha256(property_path, payload)
            assertPreflight(
                "PROPERTY_UNIT",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_boolean_semantic_numeric(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            evidence = next((source["open_evidence_root"] / "weighings").glob("*.json"))
            payload = self._read_json(evidence)
            payload["entries"][0]["actual_wet_mass_g"] = True
            write_json_with_sha256(evidence, payload)
            assertPreflight("NUMERIC_BOOL", lambda: self._open(source, common, root))

    def test_rejects_nan_semantic_numeric(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            property_path = self._property_path(source, "base")
            raw = property_path.read_text(encoding="utf-8").replace('"0.41"', "NaN")
            self._write_raw_json_with_sidecar(property_path, raw)
            assertPreflight(
                "NUMERIC_NONFINITE",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_infinite_semantic_numeric(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            property_path = self._property_path(source, "base")
            raw = property_path.read_text(encoding="utf-8").replace('"0.41"', "Infinity")
            self._write_raw_json_with_sidecar(property_path, raw)
            assertPreflight(
                "NUMERIC_NONFINITE",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_zero_wet_mass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            evidence = next((source["open_evidence_root"] / "weighings").glob("*.json"))
            payload = self._read_json(evidence)
            payload["entries"][0]["actual_wet_mass_g"] = "0"
            write_json_with_sha256(evidence, payload)
            assertPreflight("POSITIVE_NUMBER", lambda: self._open(source, common, root))

    def test_rejects_negative_wet_mass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            evidence = next((source["open_evidence_root"] / "weighings").glob("*.json"))
            payload = self._read_json(evidence)
            payload["entries"][0]["actual_wet_mass_g"] = "-1"
            write_json_with_sha256(evidence, payload)
            assertPreflight("POSITIVE_NUMBER", lambda: self._open(source, common, root))

    def test_rejects_zero_density(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            property_path = self._property_path(source, "base")
            payload = self._read_json(property_path)
            payload["properties"]["nonvolatile_density_g_ml"]["value"] = "0"
            write_json_with_sha256(property_path, payload)
            assertPreflight(
                "POSITIVE_NUMBER",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_negative_density(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            property_path = self._property_path(source, "base")
            payload = self._read_json(property_path)
            payload["properties"]["nonvolatile_density_g_ml"]["value"] = "-1"
            write_json_with_sha256(property_path, payload)
            assertPreflight(
                "POSITIVE_NUMBER",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_zero_nonvolatile_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            property_path = self._property_path(source, "base")
            payload = self._read_json(property_path)
            payload["properties"]["nonvolatile_mass_fraction"]["value"] = "0"
            write_json_with_sha256(property_path, payload)
            assertPreflight(
                "PROPERTY_FRACTION",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_excess_nonvolatile_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            property_path = self._property_path(source, "base")
            payload = self._read_json(property_path)
            payload["properties"]["nonvolatile_mass_fraction"]["value"] = "1.01"
            write_json_with_sha256(property_path, payload)
            assertPreflight(
                "PROPERTY_FRACTION",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_property_record_from_another_lot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            property_path = self._property_path(source, "base")
            payload = self._read_json(property_path)
            payload["physical_lot_id"] = "LOT-OTHER-01"
            write_json_with_sha256(property_path, payload)
            assertPreflight(
                "PROPERTY_LOT",
                lambda: preflight_pilot_materials(**source["prerequisite"], shared_root=source["shared_root"], output_dir=root / "bad-output"),
            )

    def test_rejects_actual_weighing_from_another_lot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            evidence = next((source["open_evidence_root"] / "weighings").glob("*.json"))
            payload = self._read_json(evidence)
            payload["entries"][0]["physical_lot_id"] = "LOT-OTHER-01"
            write_json_with_sha256(evidence, payload)
            assertPreflight("WEIGHING_LOT", lambda: self._open(source, common, root))

    def test_rejects_actual_weighing_bound_to_another_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            evidence = next((source["open_evidence_root"] / "weighings").glob("*.json"))
            payload = self._read_json(evidence)
            payload["formula_batch_id"] = "BATCH-OTHER"
            write_json_with_sha256(evidence, payload)
            assertPreflight("WEIGHING_BATCH", lambda: self._open(source, common, root))

    def test_rejects_generated_plan_as_actual_weighing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            evidence = next((source["open_evidence_root"] / "weighings").glob("*.json"))
            payload = self._read_json(evidence)
            payload["record_kind"] = "generated_weighing_plan"
            write_json_with_sha256(evidence, payload)
            assertPreflight("ACTUAL_WEIGHING_KIND", lambda: self._open(source, common, root))

    def test_sums_distinct_multiple_additions_before_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            self._open(source, common, root)
            rank = self._read_json(root / "open-output" / "actual-nv-rank-receipt.json")
            base = rank["rows"][1]["components"][0]
            self.assertEqual(len(base["weighing_event_ids"]), 2)
            self.assertEqual(Decimal(base["actual_wet_mass_g_decimal"]), Decimal("41.5") + Decimal("41.5"))
            self.assertEqual(
                Decimal(base["nonvolatile_volume_ml_decimal"]),
                Decimal(base["actual_wet_mass_g_decimal"]) * Decimal("0.41") / Decimal("1.01"),
            )

    def test_rejects_duplicate_weighing_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            evidence = next((source["open_evidence_root"] / "weighings").glob("*.json"))
            payload = self._read_json(evidence)
            payload["entries"].append(dict(payload["entries"][0]))
            write_json_with_sha256(evidence, payload)
            assertPreflight("WEIGHING_EVENT_DUPLICATE", lambda: self._open(source, common, root))

    def test_rejects_weighing_event_reused_by_another_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            evidence_paths = sorted((source["open_evidence_root"] / "weighings").glob("*.json"))
            first = self._read_json(evidence_paths[0])
            second = self._read_json(evidence_paths[1])
            second["entries"][0]["weighing_event_id"] = first["entries"][0]["weighing_event_id"]
            write_json_with_sha256(evidence_paths[1], second)
            assertPreflight("WEIGHING_EVENT_REUSE", lambda: self._open(source, common, root))

    def test_rejects_target_nv_substituted_for_actual_nv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            manifest_path, manifest = self._open_manifest(source)
            manifest["batches"][1]["actual_nv_vector"] = ["1"] + ["0"] * 14
            manifest["batches"][1]["actual_nv_sum"] = "1"
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight("ACTUAL_NV_MISMATCH", lambda: self._open(source, common, root))

    def test_uses_exact_zero_for_absent_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            self._open(source, common, root)
            rank = self._read_json(root / "open-output" / "actual-nv-rank-receipt.json")
            self.assertEqual(rank["rows"][1]["components"][2]["x_decimal"], "0")

    def test_rejects_noncanonical_actual_vector_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            manifest_path, manifest = self._open_manifest(source)
            order = manifest["batches"][0]["actual_nv_vector_component_order"]
            order[1], order[2] = order[2], order[1]
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight("COMPONENT_ORDER", lambda: self._open(source, common, root))

    def test_rejects_unregistered_positive_basis_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            manifest_path, manifest = self._open_manifest(source)
            batch = manifest["batches"][1]
            material = next(item for item in source["materials"] if item["formula_key"] == "Y74S")
            evidence_path = source["open_evidence_root"] / "weighings" / f"{batch['formula_batch_id']}-Y74S.json"
            extra_mass = Decimal("10")
            write_json_with_sha256(
                evidence_path,
                {
                    "schema_version": "moocow-actual-weighing-record-v2",
                    "record_kind": "actual_weighing_observation",
                    "formula_id": batch["formula_id"],
                    "formula_batch_id": batch["formula_batch_id"],
                    "entries": [
                        {
                            "weighing_record_id": f"WEIGH-{batch['formula_batch_id']}-Y74S",
                            "weighing_event_id": f"EVENT-{batch['formula_batch_id']}-Y74S-X",
                            "component_id": material["component_id"],
                            "physical_lot_id": material["physical_lot_id"],
                            "actual_wet_mass_g": "10",
                            "actual_wet_mass_unit": "g",
                            "weighed_at": "2026-07-14T22:00:00+09:00",
                            "weighing_method": "calibrated bench balance",
                        }
                    ],
                },
            )
            property_record = self._read_json(self._property_path(source, "Y74S"))
            volume = extra_mass * Decimal(property_record["properties"]["nonvolatile_mass_fraction"]["value"]) / Decimal(
                property_record["properties"]["nonvolatile_density_g_ml"]["value"]
            )
            batch["actual_components"].append(
                {
                    "component_id": material["component_id"],
                    "physical_lot_id": material["physical_lot_id"],
                    "conversion_route": MASS_SOLIDS_NONVOLATILE_DENSITY,
                    "actual_wet_mass_g": "10",
                    "nonvolatile_volume_ml": self._decimal_text(volume),
                    "actual_weighing_evidence": {"relative_path": evidence_path.relative_to(source["open_evidence_root"]).as_posix(), "record_locator": {"kind": "whole_file"}},
                }
            )
            volumes = {item["component_id"]: Decimal(item["nonvolatile_volume_ml"]) for item in batch["actual_components"]}
            total = sum(volumes.values(), Decimal(0))
            batch["actual_nv_vector"] = [self._decimal_text(volumes.get(component_id, Decimal(0)) / total) for component_id in COMPONENT_IDS]
            batch["actual_nv_sum"] = self._decimal_text(sum((Decimal(value) for value in batch["actual_nv_vector"]), Decimal(0)))
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight("BASIS_SUPPORT", lambda: self._open(source, common, root))

    def test_rejects_duplicate_formula_batch_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            manifest_path, manifest = self._open_manifest(source)
            manifest["batches"][-1]["formula_batch_id"] = manifest["batches"][0]["formula_batch_id"]
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight("FORMULA_BATCH_DUPLICATE", lambda: self._open(source, common, root))

    def test_rejects_duplicate_formula_family_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            manifest_path, manifest = self._open_manifest(source)
            manifest["batches"][-1]["formula_family_id"] = manifest["batches"][0]["formula_family_id"]
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight("FORMULA_FAMILY_DUPLICATE", lambda: self._open(source, common, root))

    def test_rejects_non_bijective_family_batch_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            manifest_path, manifest = self._open_manifest(source)
            manifest["batches"][0]["formula_batch_id"] = "BATCH-UNFROZEN"
            write_json_with_sha256(manifest_path, manifest)
            assertPreflight("FORMULA_FAMILY_BATCH", lambda: self._open(source, common, root))

    def test_rejects_cross_split_lineage_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            open_event = self._read_json(next((source["open_evidence_root"] / "weighings").glob("*.json")))["entries"][0]["weighing_event_id"]
            sealed_evidence = next((source["sealed_evidence_root"] / "weighings").glob("*.json"))
            payload = self._read_json(sealed_evidence)
            payload["entries"][0]["weighing_event_id"] = open_event
            write_json_with_sha256(sealed_evidence, payload)
            assertPreflight(
                "CROSS_SPLIT_ID",
                lambda: commit_holdout_custody(
                    materials_receipt_path=common,
                    open_batch_receipt_path=open_receipt,
                    sealed_holdout_batch_root=source["sealed_batch_root"],
                    sealed_evidence_root=source["sealed_evidence_root"],
                    custody_identity="sealed custodian",
                    custody_key_fingerprint="fingerprint-01",
                    signature_metadata={"algorithm": "external-manual-attestation", "signed_at": "2026-07-14T23:00:00+09:00"},
                    output_dir=root / "holdout-output",
                ),
            )

    def test_builds_rank_from_only_fifteen_train_formula_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            self._open(source, common, root)
            rank = self._read_json(root / "open-output" / "actual-nv-rank-receipt.json")
            self.assertEqual(rank["matrix_shape"], [15, 15])

    def test_rejects_validation_identifier_in_rank_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            self._open(source, common, root)
            batches = self._read_json(root / "open-output" / "open-batch-preflight-receipt.json")["batches"]
            train_batches = [batch for batch in batches if batch["split"] == "train"]
            train_batches[0]["formula_family_id"] = "FAM-VA-MIX-01"
            assertPreflight("RANK_SCOPE", lambda: _rank_receipt(train_batches))

    def test_rejects_holdout_identifier_in_rank_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            self._open(source, common, root)
            batches = self._read_json(root / "open-output" / "open-batch-preflight-receipt.json")["batches"]
            train_batches = [batch for batch in batches if batch["split"] == "train"]
            train_batches[0]["formula_family_id"] = "FAM-HO-MIX-01"
            assertPreflight("RANK_SCOPE", lambda: _rank_receipt(train_batches))

    def test_rejects_exactly_singular_actual_nv_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            self._set_train_tint_masses(source, Decimal("1e-400"))
            assertPreflight("RANK_DEFICIENT", lambda: self._open(source, common, root))

    def test_rejects_near_singular_matrix_at_or_below_standard_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            self._set_train_tint_masses(source, Decimal("1e-14"))
            assertPreflight("RANK_DEFICIENT", lambda: self._open(source, common, root))

    def test_accepts_full_rank_matrix_regardless_of_large_condition_number(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            self._set_train_tint_masses(source, Decimal("1e-10"))
            self._open(source, common, root)
            rank = self._read_json(root / "open-output" / "actual-nv-rank-receipt.json")["rank_method"]
            self.assertEqual(rank["numerical_rank"], 15)
            self.assertTrue(rank["condition_number_is_finite"])
            self.assertGreater(float.fromhex(rank["condition_number"]), 1e10)
            self.assertNotIn("condition_threshold", rank)


if __name__ == "__main__":
    unittest.main()
