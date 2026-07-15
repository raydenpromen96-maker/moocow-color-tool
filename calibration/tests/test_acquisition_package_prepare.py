from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(CALIBRATION_ROOT / "tests"))

from acquisition_preflight_fixtures import (
    assertPreflight,
    assert_permissions_all_false,
    assert_sidecar_matches,
    run_cli,
    write_frozen_pilot_prerequisite,
)
import km_calibration.acquisition_preflight as acquisition
import km_calibration.cli as calibration_cli
from km_calibration.acquisition_preflight import (
    COMPONENT_IDS,
    COMPONENT_ORDER,
    HOLDOUT_FAMILIES,
    MASS_SOLIDS_NONVOLATILE_DENSITY,
    PERMISSIONS,
    TRAIN_FAMILIES,
    VALIDATION_FAMILIES,
    WET_DENSITY_VOLUME_SOLIDS,
)


class AcquisitionPackagePrepareTests(unittest.TestCase):
    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _prepare(self, root: Path, route: str = MASS_SOLIDS_NONVOLATILE_DENSITY) -> tuple[Path, dict[str, object]]:
        output = root / "prepared-package"
        result = acquisition.prepare_acquisition_package(conversion_route=route, output_dir=output)
        return output, result

    @staticmethod
    def _assert_no_staging(root: Path, output: Path) -> None:
        if output.exists():
            raise AssertionError("failure must not publish an output root")
        staging = list(root.glob(f".{output.name}.staging-*"))
        if staging:
            raise AssertionError(f"failure must remove staging roots: {staging}")

    def test_prepares_mass_solids_template_route_with_only_its_property_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, result = self._prepare(Path(temporary), MASS_SOLIDS_NONVOLATILE_DENSITY)

            self.assertTrue(output.is_dir())
            self.assertEqual(result["status"], "prepared_template_only")
            self.assertEqual(result["conversion_route"], MASS_SOLIDS_NONVOLATILE_DENSITY)
            property_paths = sorted((output / "shared-template" / "operator-templates" / "properties").glob("*.json"))
            self.assertEqual(len(property_paths), 15)
            for path in property_paths:
                record = self._read_json(path)
                self.assertEqual(record["conversion_route"], MASS_SOLIDS_NONVOLATILE_DENSITY)
                self.assertEqual(
                    record["properties"],
                    {
                        "nonvolatile_mass_fraction": {
                            "property_record_id": record["properties"]["nonvolatile_mass_fraction"]["property_record_id"],
                            "value": None,
                            "unit": "fraction",
                            "method": record["properties"]["nonvolatile_mass_fraction"]["method"],
                            "observed_at": record["properties"]["nonvolatile_mass_fraction"]["observed_at"],
                        },
                        "nonvolatile_density_g_ml": {
                            "property_record_id": record["properties"]["nonvolatile_density_g_ml"]["property_record_id"],
                            "value": None,
                            "unit": "g/mL",
                            "method": record["properties"]["nonvolatile_density_g_ml"]["method"],
                            "observed_at": record["properties"]["nonvolatile_density_g_ml"]["observed_at"],
                        },
                    },
                )

    def test_prepares_wet_density_template_route_with_only_its_property_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, result = self._prepare(Path(temporary), WET_DENSITY_VOLUME_SOLIDS)

            self.assertTrue(output.is_dir())
            self.assertEqual(result["status"], "prepared_template_only")
            self.assertEqual(result["conversion_route"], WET_DENSITY_VOLUME_SOLIDS)
            property_paths = sorted((output / "shared-template" / "operator-templates" / "properties").glob("*.json"))
            self.assertEqual(len(property_paths), 15)
            for path in property_paths:
                record = self._read_json(path)
                self.assertEqual(record["conversion_route"], WET_DENSITY_VOLUME_SOLIDS)
                self.assertEqual(
                    record["properties"],
                    {
                        "wet_density_g_ml": {
                            "property_record_id": record["properties"]["wet_density_g_ml"]["property_record_id"],
                            "value": None,
                            "unit": "g/mL",
                            "method": record["properties"]["wet_density_g_ml"]["method"],
                            "observed_at": record["properties"]["wet_density_g_ml"]["observed_at"],
                        },
                        "component_nonvolatile_volume_fraction": {
                            "property_record_id": record["properties"]["component_nonvolatile_volume_fraction"]["property_record_id"],
                            "value": None,
                            "unit": "fraction",
                            "method": record["properties"]["component_nonvolatile_volume_fraction"]["method"],
                            "observed_at": record["properties"]["component_nonvolatile_volume_fraction"]["observed_at"],
                        },
                    },
                )

    def test_emits_exact_fixed_counts_component_order_and_string_073(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, result = self._prepare(Path(temporary))
            package = self._read_json(output / "package-template.json")
            materials = self._read_json(output / "shared-template" / "materials.json")
            open_batches = self._read_json(output / "open-template" / "batches.json")
            sealed_batches = self._read_json(output / "sealed-holdout-template" / "batches.json")

            expected_counts = {"materials": 15, "open_batches": 17, "sealed_batches": 3}
            self.assertEqual(result["counts"], expected_counts)
            self.assertEqual(package["counts"], expected_counts)
            expected_order = [{"formula_key": key, "component_id": component_id} for key, component_id in COMPONENT_ORDER]
            self.assertEqual(materials["component_order"], expected_order)
            self.assertEqual(
                [(row["formula_key"], row["component_id"]) for row in materials["materials"]],
                list(COMPONENT_ORDER),
            )
            self.assertEqual(materials["component_order"][9]["formula_key"], "073")
            self.assertIsInstance(materials["component_order"][9]["formula_key"], str)
            self.assertEqual(materials["component_order"][9]["component_id"], "colorant-073")
            self.assertEqual(
                [batch["formula_family_id"] for batch in open_batches["batches"]],
                list(TRAIN_FAMILIES + VALIDATION_FAMILIES),
            )
            self.assertEqual([batch["formula_family_id"] for batch in sealed_batches["batches"]], list(HOLDOUT_FAMILIES))

    def test_binds_every_generated_json_template_to_a_matching_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, _result = self._prepare(Path(temporary))
            json_paths = sorted(output.rglob("*.json"))

            self.assertGreater(len(json_paths), 0)
            for path in json_paths:
                self.assertTrue(path.with_name(f"{path.name}.sha256").is_file())
                assert_sidecar_matches(path)

    def test_keeps_live_evidence_artifact_free_and_permissions_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, result = self._prepare(Path(temporary))
            package = self._read_json(output / "package-template.json")

            assert_permissions_all_false(result)
            assert_permissions_all_false(package)
            self.assertFalse(package["contains_physical_evidence"])
            self.assertFalse(package["receipt_emitted"])
            for evidence_root in (
                output / "shared-template" / "evidence",
                output / "open-template" / "evidence",
                output / "sealed-holdout-template" / "evidence",
            ):
                self.assertEqual(list(evidence_root.rglob("*.json")), [])
            self.assertEqual(
                [path.relative_to(output / "shared-template" / "evidence" / "labels").as_posix() for path in (output / "shared-template" / "evidence" / "labels").rglob("*") if path.is_file()],
                ["README.md"],
            )
            forbidden_artifact_names = ("receipt", "rank", "dft", "spectrum", "signature", "custody", "raw-reading")
            filenames = [path.name.casefold() for path in output.rglob("*") if path.is_file()]
            self.assertFalse(any(marker in name for marker in forbidden_artifact_names for name in filenames))

    def test_public_projection_and_cli_response_exclude_sealed_raw_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "prepared-package"
            code, stdout, stderr = run_cli(
                [
                    "prepare-acquisition-package",
                    "--conversion-route",
                    MASS_SOLIDS_NONVOLATILE_DENSITY,
                    "--output-dir",
                    str(output),
                ]
            )
            response = json.loads(stdout)
            package = self._read_json(output / "package-template.json")

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(stdout, json.dumps(response, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
            public_safe_text = "\n".join(
                (
                    json.dumps(package, ensure_ascii=False, sort_keys=True),
                    (output / "README.md").read_text(encoding="utf-8"),
                    stdout,
                )
            ).casefold()
            for marker in (
                "formula_family_id",
                "formula_id",
                "formula_batch_id",
                "weighing_event_id",
                "relative_path",
                "sha256",
                "actual_wet_mass",
                "nonvolatile_volume",
                "actual_nv",
                "custody",
                "signature",
                "spectrum",
                "dft",
            ):
                self.assertNotIn(marker, public_safe_text)

            shared_and_open_text = "\n".join(
                path.read_text(encoding="utf-8")
                for root_path in (output / "shared-template", output / "open-template")
                for path in root_path.rglob("*")
                if path.is_file()
            ).casefold()
            for sealed_identity in (
                *HOLDOUT_FAMILIES,
                *(f"FORM-{family.removeprefix('FAM-')}" for family in HOLDOUT_FAMILIES),
                *(f"REQUIRED_FROZEN_FORMULA_BATCH_ID_{family.removeprefix('FAM-')}" for family in HOLDOUT_FAMILIES),
                *(f"REQUIRED_ACTUAL_WEIGHING_EVIDENCE_PATH_{family.removeprefix('FAM-')}" for family in HOLDOUT_FAMILIES),
            ):
                self.assertNotIn(sealed_identity.casefold(), shared_and_open_text)

    def test_sealed_template_uses_ordinal_filenames_and_fixed_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, _result = self._prepare(Path(temporary))
            sealed_batches = self._read_json(output / "sealed-holdout-template" / "batches.json")["batches"]
            weighing_directory = output / "sealed-holdout-template" / "operator-templates" / "weighings"
            weighing_paths = sorted(weighing_directory.glob("*.actual-weighing.template.json"))

            self.assertEqual(
                [path.name for path in weighing_paths],
                [f"batch-{index:02d}.actual-weighing.template.json" for index in range(1, 4)],
            )
            self.assertEqual([batch["formula_family_id"] for batch in sealed_batches], list(HOLDOUT_FAMILIES))
            for path, batch, family in zip(weighing_paths, sealed_batches, HOLDOUT_FAMILIES, strict=True):
                record = self._read_json(path)
                self.assertEqual(record["formula_id"], f"FORM-{family.removeprefix('FAM-')}")
                self.assertEqual(record["formula_id"], batch["formula_id"])
                self.assertEqual(record["formula_batch_id"], batch["formula_batch_id"])
                self.assertEqual(record["formula_batch_id"], f"REQUIRED_FROZEN_FORMULA_BATCH_ID_{family.removeprefix('FAM-')}")
                self.assertTrue(all(entry["component_id"] in COMPONENT_IDS for entry in record["entries"]))

    def test_generated_shared_template_fails_placeholder_with_valid_parent_and_no_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prerequisite = write_frozen_pilot_prerequisite(root)
            output, _result = self._prepare(root)
            receipt_output = root / "common-material-output"

            assertPreflight(
                "PLACEHOLDER",
                lambda: acquisition.preflight_pilot_materials(
                    **prerequisite,
                    shared_root=output / "shared-template",
                    output_dir=receipt_output,
                ),
            )
            self.assertFalse((receipt_output / "common-material-receipt.json").exists())
            self.assertFalse(receipt_output.exists())

    def test_cli_requires_conversion_route_without_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "missing-route-output"
            code, stdout, stderr = run_cli(["prepare-acquisition-package", "--output-dir", str(output)])

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("usage:", stderr)
            self.assertFalse(output.exists())

    def test_cli_rejects_invalid_conversion_route_without_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "invalid-route-output"
            code, stdout, stderr = run_cli(
                [
                    "prepare-acquisition-package",
                    "--conversion-route",
                    "not-a-route",
                    "--output-dir",
                    str(output),
                ]
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("invalid choice", stderr)
            self.assertFalse(output.exists())

    def test_rejects_nonempty_output_directory_without_mutating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "nonempty-output"
            output.mkdir()
            sentinel = output / "keep.txt"
            sentinel.write_text("preserve these bytes", encoding="utf-8")
            before = sentinel.read_bytes()

            assertPreflight(
                "OUTPUT_DIR_NOT_EMPTY",
                lambda: acquisition.prepare_acquisition_package(
                    conversion_route=MASS_SOLIDS_NONVOLATILE_DENSITY,
                    output_dir=output,
                ),
            )
            self.assertEqual(sentinel.read_bytes(), before)
            self.assertEqual([path.name for path in output.iterdir()], ["keep.txt"])

    def test_rejects_file_output_path_without_mutating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "file-output"
            output.write_bytes(b"preserve file bytes")
            before = output.read_bytes()

            assertPreflight(
                "OUTPUT_DIR",
                lambda: acquisition.prepare_acquisition_package(
                    conversion_route=MASS_SOLIDS_NONVOLATILE_DENSITY,
                    output_dir=output,
                ),
            )
            self.assertEqual(output.read_bytes(), before)

    def test_rejects_symlink_output_path_without_mutating_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "symlink-target"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("preserve symlink target", encoding="utf-8")
            output = root / "symlink-output"
            try:
                output.symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlink setup is unavailable: {error}")

            assertPreflight(
                "OUTPUT_DIR",
                lambda: acquisition.prepare_acquisition_package(
                    conversion_route=MASS_SOLIDS_NONVOLATILE_DENSITY,
                    output_dir=output,
                ),
            )
            self.assertTrue(output.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve symlink target")

    def test_cleans_staging_and_publishes_nothing_after_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "write-failure-output"
            original = acquisition.write_json_with_sha256
            calls = 0

            def fail_second_write(path: Path, value: object) -> str:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second JSON write failure")
                return original(path, value)

            with mock.patch.object(acquisition, "write_json_with_sha256", side_effect=fail_second_write):
                assertPreflight(
                    "OUTPUT_WRITE",
                    lambda: acquisition.prepare_acquisition_package(
                        conversion_route=MASS_SOLIDS_NONVOLATILE_DENSITY,
                        output_dir=output,
                    ),
                )
            self._assert_no_staging(root, output)

    def test_cleans_staging_and_publishes_nothing_after_self_check_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "self-check-failure-output"
            failure = acquisition.AcquisitionPreflightError("PREPARE_VALIDATION", "injected", "forced self-check failure")

            with mock.patch.object(acquisition, "_validate_prepared_acquisition_package", side_effect=failure):
                assertPreflight(
                    "PREPARE_VALIDATION",
                    lambda: acquisition.prepare_acquisition_package(
                        conversion_route=MASS_SOLIDS_NONVOLATILE_DENSITY,
                        output_dir=output,
                    ),
                )
            self._assert_no_staging(root, output)

    def test_generator_never_invokes_receipt_preflight_or_model_entry_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "poisoned-entrypoint-output"
            targets = (
                (acquisition, "_receipt_payload"),
                (acquisition, "_load_materials"),
                (acquisition, "_load_common_receipt"),
                (acquisition, "_load_batch_manifest"),
                (acquisition, "_rank_receipt"),
                (acquisition, "preflight_pilot_materials"),
                (acquisition, "preflight_open_batches"),
                (acquisition, "commit_holdout_custody"),
                (acquisition, "assemble_acquisition_preflight"),
                (acquisition, "verify_acquisition_preflight"),
                (calibration_cli, "load_and_validate_dataset"),
                (calibration_cli, "fit_km"),
                (calibration_cli, "evaluate_model"),
                (calibration_cli, "preflight_pilot_materials"),
                (calibration_cli, "preflight_open_batches"),
                (calibration_cli, "commit_holdout_custody"),
                (calibration_cli, "assemble_acquisition_preflight"),
                (calibration_cli, "verify_acquisition_preflight"),
            )
            poisoned: list[tuple[str, mock.Mock]] = []

            with ExitStack() as stack:
                for owner, name in targets:
                    poisoned.append(
                        (
                            name,
                            stack.enter_context(
                                mock.patch.object(
                                    owner,
                                    name,
                                    side_effect=AssertionError(f"generator invoked forbidden entry point: {name}"),
                                )
                            ),
                        )
                    )
                code, stdout, stderr = run_cli(
                    [
                        "prepare-acquisition-package",
                        "--conversion-route",
                        MASS_SOLIDS_NONVOLATILE_DENSITY,
                        "--output-dir",
                        str(output),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(json.loads(stdout)["status"], "prepared_template_only")
            for name, entry_point in poisoned:
                entry_point.assert_not_called(), name

    def test_returns_only_status_route_counts_and_six_false_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _output, result = self._prepare(Path(temporary))

            self.assertEqual(
                set(result),
                {"status", "conversion_route", "counts", *PERMISSIONS},
            )
            self.assertEqual(result["status"], "prepared_template_only")
            self.assertEqual(result["conversion_route"], MASS_SOLIDS_NONVOLATILE_DENSITY)
            self.assertEqual(result["counts"], {"materials": 15, "open_batches": 17, "sealed_batches": 3})
            assert_permissions_all_false(result)


if __name__ == "__main__":
    unittest.main()
