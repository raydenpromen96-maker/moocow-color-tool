from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CALIBRATION_ROOT.parent
sys.path.insert(0, str(CALIBRATION_ROOT))

from km_calibration.errors import DatasetValidationError, IdentifiabilityError
from km_calibration.hashing import (
    read_json,
    read_verified_json,
    sha256_bytes,
    sha256_file,
    write_json_with_sha256,
)
from km_calibration.km import apply_saunderson, finite_film_reflectance, remove_saunderson
from km_calibration.pipeline import (
    _evaluate_splits,
    evaluate_model,
    export_candidate,
    fit_km,
    load_and_validate_model,
    write_evaluation,
    write_model,
)
from km_calibration.schema import load_and_validate_dataset, split_audit
from km_calibration.synthetic import generate_synthetic_dataset


FIXTURE = json.loads((REPO_ROOT / "tests" / "fixtures" / "physical" / "km_synthetic_fixture.json").read_text(encoding="utf-8"))


class KmCalibrationTests(unittest.TestCase):
    def _generate(self, parent: Path, *, noise_std: float | None = None) -> Path:
        root = parent / "dataset"
        generate_synthetic_dataset(
            root,
            seed=FIXTURE["seed"],
            noise_std=FIXTURE["noise_std"] if noise_std is None else noise_std,
        )
        return root

    @staticmethod
    def _rewrite_measurements_and_manifest(root: Path, source: dict) -> None:
        source_path = root / "sources" / "synthetic-measurements.json"
        write_json_with_sha256(source_path, source)
        manifest_path = root / "manifest.json"
        manifest = read_json(manifest_path)
        manifest["source_files"][0]["sha256"] = sha256_file(source_path)
        write_json_with_sha256(manifest_path, manifest)

    @staticmethod
    def _rewrite_as_research_only(root: Path) -> None:
        manifest_path = root / "manifest.json"
        manifest = read_json(manifest_path)
        manifest["dataset_status"] = "research_only"
        for descriptor in manifest["source_files"]:
            source_path = root / descriptor["path"]
            source = read_json(source_path)
            source["dataset_status"] = "research_only"
            for record in source.get("measurements", []):
                record["target_kind"] = "measured_spectrum"
            write_json_with_sha256(source_path, source)
            descriptor["sha256"] = sha256_file(source_path)
        write_json_with_sha256(manifest_path, manifest)

    def _research_dataset_with_test_bound_model(self, root: Path, model_path: Path):
        """Create a test fixture without exposing a production research fit path."""
        synthetic_dataset = load_and_validate_dataset(root)
        model = copy.deepcopy(fit_km(synthetic_dataset).model)
        self._rewrite_as_research_only(root)
        dataset = load_and_validate_dataset(root)
        model["status"] = dataset.dataset_status
        model["provenance"]["dataset_manifest_sha256"] = dataset.manifest_sha256
        model["provenance"]["source_files"] = [dict(source_hash) for source_hash in dataset.source_hashes]
        write_model(model_path, model)
        return dataset

    def test_thin_and_thick_limits_are_stable(self) -> None:
        black = np.full(31, 0.03)
        white = np.full(31, 0.95)
        k = np.linspace(2.0, 6.0, 31)
        s = np.full(31, 20.0)
        thin_black = finite_film_reflectance(k, s, 1e-6, black)
        thin_white = finite_film_reflectance(k, s, 1e-6, white)
        thick_black = finite_film_reflectance(k, s, 1.0, black)
        thick_white = finite_film_reflectance(k, s, 1.0, white)
        self.assertTrue(np.all(np.isfinite(thin_black)))
        self.assertLess(float(np.max(np.abs(thick_black - thick_white))), 1e-8)
        self.assertGreater(float(np.mean(np.abs(thin_black - thin_white))), 0.5)

    def test_black_white_difference_and_fixed_saunderson_round_trip(self) -> None:
        black = finite_film_reflectance(4.0, 20.0, 0.04, 0.03)
        white = finite_film_reflectance(4.0, 20.0, 0.04, 0.95)
        self.assertGreater(float(white - black), 0.01)
        intrinsic = np.linspace(0.05, 0.95, 31)
        config = {"mode": "fixed", "k1": 0.035, "k2": 0.075}
        self.assertTrue(np.allclose(remove_saunderson(apply_saunderson(intrinsic, config), config), intrinsic, atol=1e-12))

    def test_fixed_seed_generation_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as first_temp, tempfile.TemporaryDirectory() as second_temp:
            first = self._generate(Path(first_temp), noise_std=0.001)
            second = self._generate(Path(second_temp), noise_std=0.001)
            first_files = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
            second_files = sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())
            self.assertEqual(first_files, second_files)
            for relative_path in first_files:
                self.assertEqual((first / relative_path).read_bytes(), (second / relative_path).read_bytes())

    def test_schema_rejects_tamper_hash_split_and_hex_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            source_path = root / "sources" / "synthetic-measurements.json"
            source = read_json(source_path)
            source["measurements"][0]["reflectance"][0] += 0.001
            source_path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(DatasetValidationError, "SHA-256 mismatch"):
                load_and_validate_dataset(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            manifest_path = root / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["splits"]["holdout"].append(manifest["splits"]["train"][0])
            write_json_with_sha256(manifest_path, manifest)
            with self.assertRaisesRegex(DatasetValidationError, "leaks across"):
                load_and_validate_dataset(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            source_path = root / "sources" / "synthetic-measurements.json"
            source = read_json(source_path)
            source["measurements"][0]["hex"] = "#ffffff"
            self._rewrite_measurements_and_manifest(root, source)
            with self.assertRaisesRegex(DatasetValidationError, "non-physical field hex"):
                load_and_validate_dataset(root)

    def test_schema_accepts_normal_source_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            dataset = load_and_validate_dataset(root)
            self.assertEqual(dataset.source_hashes[0]["path"], "sources/synthetic-measurements.json")

    def test_validated_dataset_state_is_deeply_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = load_and_validate_dataset(self._generate(Path(temporary)))
            family = next(iter(dataset.family_splits))

            with self.assertRaises(TypeError):
                dataset.dataset_status = "research_only"
            with self.assertRaises(TypeError):
                dataset.manifest["dataset_status"] = "research_only"
            with self.assertRaises(TypeError):
                dataset.manifest["components"][0]["batch_id"] = "TAMPERED"
            with self.assertRaises(TypeError):
                dataset.manifest["wavelength_nm"][0] = 0.0
            with self.assertRaises(TypeError):
                dataset.records[0]["formula_family_id"] = "forged-family"
            with self.assertRaises(TypeError):
                dataset.records[0]["components"][0]["nonvolatile_volume_fraction"] = 0.0
            with self.assertRaises(TypeError):
                dataset.records[0]["reflectance"][0] = 0.0
            with self.assertRaises(TypeError):
                dataset.source_hashes[0]["sha256"] = "0" * 64
            with self.assertRaises(TypeError):
                dataset.family_splits[family] = "holdout"
            with self.assertRaises(TypeError):
                dataset.split_record_counts["train"] = 0
            with self.assertRaises(TypeError):
                dataset.split_audit_snapshot["record_counts"]["train"] = 0
            with self.assertRaises(TypeError):
                dataset.split_audit_snapshot["families"]["train"][0] = "forged-family"

    def test_schema_rejects_source_file_through_external_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = self._generate(parent)
            source_path = root / "sources" / "synthetic-measurements.json"
            external_directory = parent / "external-sources"
            external_directory.mkdir()
            external_source = external_directory / source_path.name
            external_source.write_bytes(source_path.read_bytes())
            escape_directory = root / "external-sources"
            try:
                escape_directory.symlink_to(external_directory, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"Directory symlink setup is unavailable: {error}")

            manifest_path = root / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["source_files"][0].update(
                {"path": f"external-sources/{source_path.name}", "sha256": sha256_file(external_source)}
            )
            write_json_with_sha256(manifest_path, manifest)
            with self.assertRaisesRegex(DatasetValidationError, "link or reparse point|must resolve within the dataset root"):
                load_and_validate_dataset(root)

    def test_schema_rejects_nonportable_source_paths_and_multiple_hard_links(self) -> None:
        for path in (
            "C:/synthetic-measurements.json",
            "../synthetic-measurements.json",
            "sources/./synthetic-measurements.json",
            "sources\\synthetic-measurements.json",
            "sources/synthetic-measurements.json ",
            "sources/CON.json",
            "sources/CON .json",
        ):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temporary:
                root = self._generate(Path(temporary))
                manifest_path = root / "manifest.json"
                manifest = read_json(manifest_path)
                manifest["source_files"][0]["path"] = path
                write_json_with_sha256(manifest_path, manifest)
                with self.assertRaisesRegex(DatasetValidationError, "unique relative safe path"):
                    load_and_validate_dataset(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            source_path = root / "sources" / "synthetic-measurements.json"
            hard_link = root / "sources" / "synthetic-measurements-hard-link.json"
            try:
                hard_link.hardlink_to(source_path)
            except OSError as error:
                self.skipTest(f"Hard-link setup is unavailable: {error}")

            manifest_path = root / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["source_files"][0].update(
                {"path": hard_link.relative_to(root).as_posix(), "sha256": sha256_file(hard_link)}
            )
            write_json_with_sha256(manifest_path, manifest)
            with self.assertRaisesRegex(DatasetValidationError, "must not have multiple hard links"):
                load_and_validate_dataset(root)

    def test_canonical_formula_composition_cannot_cross_family_splits_under_new_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            source_path = root / "sources" / "synthetic-measurements.json"
            source = read_json(source_path)
            copied = copy.deepcopy(
                next(
                    record
                    for record in source["measurements"]
                    if record["formula_family_id"] == "family-base"
                )
            )
            copied.update(
                {
                    "measurement_id": "copied-holdout-measurement",
                    "formula_family_id": "family-copied-holdout",
                    "formula_id": "formula-copied-holdout",
                    "formula_batch_id": "formula-copied-holdout-batch",
                    "card_id": "formula-copied-holdout-card",
                    "sample_group_id": "formula-copied-holdout-sample",
                    "repeat_id": "copied-r1",
                }
            )
            copied["components"][0]["nonvolatile_volume_fraction"] = 1.0000000000000002
            source["measurements"].append(copied)
            write_json_with_sha256(source_path, source)
            manifest_path = root / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["source_files"][0]["sha256"] = sha256_file(source_path)
            manifest["splits"]["holdout"].append("family-copied-holdout")
            write_json_with_sha256(manifest_path, manifest)
            with self.assertRaisesRegex(DatasetValidationError, "Canonical formula composition crosses"):
                load_and_validate_dataset(root)

    def test_all_split_family_arrays_must_be_nonempty(self) -> None:
        for split in ("train", "validation", "holdout"):
            with self.subTest(split=split), tempfile.TemporaryDirectory() as temporary:
                root = self._generate(Path(temporary))
                manifest_path = root / "manifest.json"
                manifest = read_json(manifest_path)
                manifest["splits"][split] = []
                write_json_with_sha256(manifest_path, manifest)
                with self.assertRaisesRegex(DatasetValidationError, f"splits.{split} must be a non-empty array"):
                    load_and_validate_dataset(root)

    def test_hash_valid_model_with_wrong_component_batch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            dataset = load_and_validate_dataset(root)
            model_path = Path(temporary) / "model.json"
            write_model(model_path, fit_km(dataset).model)
            tampered_model = read_json(model_path)
            tampered_model["components"][0]["batch_id"] = "TAMPERED-BATCH"
            write_model(model_path, tampered_model)
            with self.assertRaisesRegex(DatasetValidationError, "batch_id does not match manifest"):
                load_and_validate_model(model_path, dataset)

    def test_noiseless_joint_fit_reconstructs_and_exports_synthetic_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            dataset = load_and_validate_dataset(root)
            audit = split_audit(dataset)
            self.assertEqual(audit["record_counts"], {"train": 60, "validation": 12, "holdout": 12})
            outcome = fit_km(dataset)
            for component in outcome.model["components"]:
                self.assertTrue(np.all(np.asarray(component["K_mm_inv"]) >= 0))
                self.assertTrue(np.all(np.asarray(component["S_mm_inv"]) > 0))
            model_path = Path(temporary) / "model.json"
            write_model(model_path, outcome.model)
            evaluation, _model_sha256 = evaluate_model(dataset, model_path)
            self.assertEqual(set(evaluation["metrics"]), {"train", "validation", "holdout"})
            for split in ("train", "validation", "holdout"):
                self.assertLess(evaluation["metrics"][split]["reflectance_max_abs"], 2e-7)
            evaluation_path = Path(temporary) / "evaluation.json"
            write_evaluation(evaluation_path, evaluation)
            receipt_path = Path(temporary) / "receipt.json"
            receipt, _receipt_sha256 = export_candidate(dataset, model_path, evaluation_path, receipt_path)
            self.assertEqual(receipt["status"], "synthetic_only")
            self.assertFalse(receipt["production_pass"])
            self.assertFalse(receipt["promotion"]["production_pass"])
            self.assertTrue(receipt_path.with_name("receipt.json.sha256").exists())
            written_receipt = read_json(receipt_path)
            binding_paths = [
                written_receipt["bindings"]["manifest"]["path"],
                written_receipt["bindings"]["model"]["path"],
                written_receipt["bindings"]["evaluation"]["path"],
                *(item["path"] for item in written_receipt["bindings"]["source_files"]),
            ]
            self.assertEqual(binding_paths[:3], ["manifest.json", "model.json", "evaluation.json"])
            self.assertTrue(all(not Path(path).is_absolute() for path in binding_paths))
            self.assertTrue(all(str(Path(temporary).resolve()) not in path for path in binding_paths))
            self.assertTrue(all(str(REPO_ROOT.resolve()) not in path for path in binding_paths))

    def test_ratio_only_design_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            source_path = root / "sources" / "synthetic-measurements.json"
            source = read_json(source_path)
            source["measurements"] = [
                record for record in source["measurements"] if record["backing"] == "white"
            ]
            self._rewrite_measurements_and_manifest(root, source)
            dataset = load_and_validate_dataset(root)
            with self.assertRaisesRegex(IdentifiabilityError, "joint black and white"):
                fit_km(dataset)

    def test_research_evaluation_excludes_holdout_and_preserves_hash_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            model_path = Path(temporary) / "model.json"
            dataset = self._research_dataset_with_test_bound_model(root, model_path)

            evaluation, model_sha256 = evaluate_model(dataset, model_path)

            self.assertEqual(set(evaluation["metrics"]), {"train", "validation"})
            self.assertNotIn("holdout", evaluation["metrics"])
            self.assertEqual(evaluation["dataset_manifest_sha256"], dataset.manifest_sha256)
            self.assertEqual(evaluation["model_sha256"], model_sha256)
            self.assertEqual(model_sha256, sha256_file(model_path))
            evaluation_path = Path(temporary) / "evaluation.json"
            write_evaluation(evaluation_path, evaluation)
            receipt, _receipt_sha256 = export_candidate(
                dataset,
                model_path,
                evaluation_path,
                Path(temporary) / "receipt.json",
            )
            self.assertEqual(receipt["status"], "research_only")
            with self.assertRaises(TypeError):
                evaluate_model(dataset, model_path, splits=("holdout",))  # type: ignore[call-arg]

    def test_generic_fit_rejects_research_only_without_receipt_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            self._rewrite_as_research_only(root)
            dataset = load_and_validate_dataset(root)
            with self.assertRaisesRegex(DatasetValidationError, "fit-pilot-selection"):
                fit_km(dataset)

    def test_research_public_dataset_hides_holdout_and_rejects_mutable_split_exploit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            model_path = Path(temporary) / "model.json"
            dataset = self._research_dataset_with_test_bound_model(root, model_path)
            holdout_family = next(
                family for family, split in dataset.family_splits.items() if split == "holdout"
            )
            audit = split_audit(dataset)
            self.assertEqual(audit["record_counts"], {"train": 60, "validation": 12, "holdout": 12})
            self.assertFalse(
                any(dataset.family_splits[record["formula_family_id"]] == "holdout" for record in dataset.records)
            )

            evaluation, _model_sha256 = evaluate_model(dataset, model_path)
            with self.assertRaises(TypeError):
                dataset.family_splits[holdout_family] = "train"
            with self.assertRaises(TypeError):
                dataset.manifest["splits"]["holdout"] = ()
            evaluation_after_mutation, _model_sha256 = evaluate_model(dataset, model_path)

            self.assertEqual(dataset.dataset_status, "research_only")
            self.assertEqual(evaluation["status"], "research_only")
            self.assertEqual(set(evaluation["metrics"]), {"train", "validation"})
            self.assertEqual(evaluation_after_mutation, evaluation)
            self.assertEqual(evaluation["metrics"]["train"]["records"], 60)
            with self.assertRaisesRegex(DatasetValidationError, "Research-only holdout evaluation is forbidden"):
                _evaluate_splits(dataset, model_path, splits=("holdout",))

            evaluation["metrics"]["train"]["records"] = 72
            evaluation_path = Path(temporary) / "forged-count-evaluation.json"
            write_evaluation(evaluation_path, evaluation)
            with self.assertRaisesRegex(DatasetValidationError, "validated split-count authority"):
                export_candidate(dataset, model_path, evaluation_path, Path(temporary) / "receipt.json")

    def test_export_rejects_forged_research_holdout_only_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            model_path = Path(temporary) / "model.json"
            dataset = self._research_dataset_with_test_bound_model(root, model_path)
            evaluation, _model_sha256 = evaluate_model(dataset, model_path)
            evaluation["metrics"] = {"holdout": evaluation["metrics"]["train"]}
            evaluation_path = Path(temporary) / "forged-evaluation.json"
            write_evaluation(evaluation_path, evaluation)

            with self.assertRaisesRegex(DatasetValidationError, "metrics must contain exactly train, validation"):
                export_candidate(dataset, model_path, evaluation_path, Path(temporary) / "receipt.json")

    def test_verified_json_rejects_duplicate_keys_and_binds_parse_to_hashed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            original = b'{"value":"original"}'
            replacement = b'{"value":"replacement"}'
            path.write_bytes(original)
            original_json_loads = json.loads

            def swap_path_then_parse(*args: object, **kwargs: object) -> object:
                path.write_bytes(replacement)
                return original_json_loads(*args, **kwargs)

            with mock.patch("km_calibration.hashing.json.loads", side_effect=swap_path_then_parse):
                parsed, digest = read_verified_json(path, expected_sha256=sha256_bytes(original))
            self.assertEqual(parsed, {"value": "original"})
            self.assertEqual(digest, sha256_bytes(original))
            self.assertEqual(path.read_bytes(), replacement)

            duplicate_keys = b'{"value":1,"value":2}'
            path.write_bytes(duplicate_keys)
            with self.assertRaisesRegex(DatasetValidationError, "duplicate JSON key"):
                read_verified_json(path, expected_sha256=sha256_bytes(duplicate_keys))

            invalid_utf8 = b'\xff'
            path.write_bytes(invalid_utf8)
            with self.assertRaisesRegex(DatasetValidationError, "Cannot read JSON artifact"):
                read_verified_json(path, expected_sha256=sha256_bytes(invalid_utf8))

    def test_model_and_evaluation_artifacts_reject_multiple_hard_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            dataset = load_and_validate_dataset(root)
            model_path = Path(temporary) / "model.json"
            write_model(model_path, fit_km(dataset).model)
            evaluation, _model_sha256 = evaluate_model(dataset, model_path)
            evaluation_path = Path(temporary) / "evaluation.json"
            write_evaluation(evaluation_path, evaluation)

            evaluation_link = Path(temporary) / "evaluation-hard-link.json"
            try:
                evaluation_link.hardlink_to(evaluation_path)
            except OSError as error:
                self.skipTest(f"Hard-link setup is unavailable: {error}")
            evaluation_link.with_name(f"{evaluation_link.name}.sha256").write_text(
                f"{sha256_file(evaluation_link)}  {evaluation_link.name}\n", encoding="ascii"
            )
            with self.assertRaisesRegex(DatasetValidationError, "multiple hard links"):
                export_candidate(dataset, model_path, evaluation_link, Path(temporary) / "receipt.json")

            model_link = Path(temporary) / "model-hard-link.json"
            model_link.hardlink_to(model_path)
            model_link.with_name(f"{model_link.name}.sha256").write_text(
                f"{sha256_file(model_link)}  {model_link.name}\n", encoding="ascii"
            )
            with self.assertRaisesRegex(DatasetValidationError, "multiple hard links"):
                load_and_validate_model(model_link, dataset)

    def test_internal_evaluation_scope_rejects_empty_and_unknown_splits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._generate(Path(temporary))
            dataset = load_and_validate_dataset(root)
            model_path = Path(temporary) / "model.json"
            write_model(model_path, fit_km(dataset).model)

            with self.assertRaisesRegex(DatasetValidationError, "must not be empty"):
                _evaluate_splits(dataset, model_path, splits=())
            with self.assertRaisesRegex(DatasetValidationError, "Unknown evaluation split"):
                _evaluate_splits(dataset, model_path, splits=("unknown",))


if __name__ == "__main__":
    unittest.main()
