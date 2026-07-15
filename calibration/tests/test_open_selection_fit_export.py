from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = CALIBRATION_ROOT / "tests"
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from acquisition_preflight_fixtures import assert_permissions_all_false, run_cli
from km_calibration.acquisition_preflight import PERMISSIONS
from km_calibration.errors import CalibrationError, DatasetValidationError
from km_calibration.hashing import canonical_json_bytes, sha256_bytes, sha256_file, write_json_with_sha256
from km_calibration.open_measurement_admission import (
    ValidatedOpenSelectionDataset,
    admit_open_measurements,
    load_and_validate_open_selection_dataset,
)
import km_calibration.open_selection_fit_export as fit_export
from km_calibration.open_selection_fit_export import (
    run_open_selection_fit_export,
    verify_open_selection_fit_export,
)
from km_calibration.schema import load_and_validate_dataset
from open_measurement_admission_fixtures import write_valid_open_measurement_fixture
from open_selection_fit_export_fixtures import (
    COMPONENT_COUNT,
    REPOSITION_COUNT,
    TRAIN_CARD_COUNT,
    TRAIN_CELL_COUNT,
    VALIDATION_CARD_COUNT,
    VALIDATION_CELL_COUNT,
    WAVELENGTH_NM,
    write_fit_ready_open_selection_fixture,
)


_PACKAGE_FILES = {
    "fit-model.json",
    "fit-model.json.sha256",
    "selection-evaluation.json",
    "selection-evaluation.json.sha256",
    "fit-export-receipt.json",
    "fit-export-receipt.json.sha256",
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):  # pragma: no cover - test fixture invariant.
        raise AssertionError(f"{path.name} must contain a JSON object")
    return value


def _assert_nonpromotable(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in {"production_pass", *PERMISSIONS}:
                if item is not False:
                    raise AssertionError(f"{child} must remain false")
            _assert_nonpromotable(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_nonpromotable(item, f"{path}[{index}]")


def _replace_digest(value: object, old: str, new: str) -> object:
    if isinstance(value, dict):
        return {key: _replace_digest(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_digest(item, old, new) for item in value]
    return new if value == old else value


class OpenSelectionFitExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.source = write_fit_ready_open_selection_fixture(cls.root / "source")
        cls.dataset_root = cls.root / "admitted"
        admit_open_measurements(
            acquisition_receipt_path=cls.source["acquisition_receipt"],
            shared_root=cls.source["shared_root"],
            open_root=cls.source["open_root"],
            measurement_root=cls.source["measurement_root"],
            admission_input_relative_path=cls.source["admission_input_relative_path"],
            output_dir=cls.dataset_root,
        )
        cls.admission_receipt = cls.dataset_root / "admission-receipt.json"
        cls.export_root = cls.root / "export"
        cls.fit_result = cls._run_fit(cls.source, cls.dataset_root, cls.admission_receipt, cls.export_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    @staticmethod
    def _run_fit(
        source: dict[str, object], dataset_root: Path, admission_receipt: Path, export_root: Path
    ) -> dict[str, Any]:
        result = run_open_selection_fit_export(
            acquisition_receipt_path=source["acquisition_receipt"],
            admission_receipt_path=admission_receipt,
            dataset_root=dataset_root,
            shared_root=source["shared_root"],
            open_root=source["open_root"],
            measurement_root=source["measurement_root"],
            output_dir=export_root,
        )
        if not isinstance(result, dict):  # pragma: no cover - public API contract.
            raise AssertionError("fit/export result must be a JSON-compatible object")
        return result

    @classmethod
    def _verify(cls, source: dict[str, object], dataset_root: Path, admission_receipt: Path, export_root: Path) -> dict[str, Any]:
        result = verify_open_selection_fit_export(
            acquisition_receipt_path=source["acquisition_receipt"],
            admission_receipt_path=admission_receipt,
            dataset_root=dataset_root,
            shared_root=source["shared_root"],
            open_root=source["open_root"],
            measurement_root=source["measurement_root"],
            export_root=export_root,
        )
        if not isinstance(result, dict):  # pragma: no cover - public API contract.
            raise AssertionError("verification result must be a JSON-compatible object")
        return result

    @staticmethod
    def _artifacts(export_root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        return (
            _read_json(export_root / "fit-model.json"),
            _read_json(export_root / "selection-evaluation.json"),
            _read_json(export_root / "fit-export-receipt.json"),
        )

    def test_fixture_covers_the_required_open_selection_grid_and_roster(self) -> None:
        self.assertEqual(WAVELENGTH_NM[0], 400.0)
        self.assertEqual(WAVELENGTH_NM[-1], 700.0)
        self.assertTrue(all(right - left <= 20.0 for left, right in zip(WAVELENGTH_NM, WAVELENGTH_NM[1:])))
        self.assertEqual(len(self.source["truth_component_curves"]), COMPONENT_COUNT)
        self.assertEqual(len(self.source["expected_cell_spectra"]), TRAIN_CELL_COUNT + VALIDATION_CELL_COUNT)

    def test_legacy_three_point_admission_fixture_fails_the_fit_readiness_grid_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = write_valid_open_measurement_fixture(root / "three-point-source")
            dataset_root = root / "three-point-admitted"
            admit_open_measurements(
                acquisition_receipt_path=source["acquisition_receipt"],
                shared_root=source["shared_root"],
                open_root=source["open_root"],
                measurement_root=source["measurement_root"],
                admission_input_relative_path=source["admission_input_relative_path"],
                output_dir=dataset_root,
            )

            with self.assertRaises(CalibrationError):
                self._run_fit(source, dataset_root, dataset_root / "admission-receipt.json", root / "three-point-export")

    def test_fit_reports_grouped_train_and_validation_cell_counts(self) -> None:
        model, evaluation, _receipt = self._artifacts(self.export_root)
        self.assertEqual(
            model["fit_spec"]["grouping"],
            {
                "train_cells": TRAIN_CELL_COUNT,
                "validation_cells": VALIDATION_CELL_COUNT,
                "repositions_per_cell": REPOSITION_COUNT,
                "cell_value": "arithmetic_mean",
            },
        )
        self.assertEqual(model["projection_bindings"]["train"]["cell_count"], TRAIN_CELL_COUNT)
        self.assertEqual(model["projection_bindings"]["validation"]["cell_count"], VALIDATION_CELL_COUNT)
        self.assertEqual(evaluation["metrics"]["train"]["cell_count"], TRAIN_CELL_COUNT)
        self.assertEqual(evaluation["metrics"]["validation"]["cell_count"], VALIDATION_CELL_COUNT)
        self.assertEqual(len(self.source["payload"]["readings"]), (TRAIN_CELL_COUNT + VALIDATION_CELL_COUNT) * REPOSITION_COUNT)
        for candidate in evaluation["selection"]["candidates"]:
            if candidate["valid"]:
                self.assertEqual(len(candidate["starts"]), 4)
                self.assertIn("bound_counts", candidate)

    def test_noiseless_fixture_recovers_the_truth_component_curves(self) -> None:
        model, _evaluation, _receipt = self._artifacts(self.export_root)
        recovered = model["components"]
        expected = self.source["truth_component_curves"]
        self.assertIsInstance(recovered, list)
        self.assertEqual(len(recovered), COMPONENT_COUNT)
        for actual, truth in zip(recovered, expected, strict=True):
            self.assertEqual(
                (actual["component_id"], actual["physical_lot_id"]),
                (truth["component_id"], truth["physical_lot_id"]),
            )
            np.testing.assert_allclose(actual["K_mm_inv"], truth["k_mm_inv"], rtol=1e-5, atol=1e-8)
            np.testing.assert_allclose(actual["S_mm_inv"], truth["s_mm_inv"], rtol=1e-5, atol=1e-8)

    def test_noiseless_fixture_recovers_validation_cell_reflectance(self) -> None:
        _model, evaluation, _receipt = self._artifacts(self.export_root)
        self.assertLessEqual(evaluation["metrics"]["validation"]["global"]["max_abs"], 2e-7)

    def test_verifier_rejects_an_extra_package_member(self) -> None:
        forged_export = self.root / "extra-member-export"
        shutil.copytree(self.export_root, forged_export)
        (forged_export / "unexpected.json").write_text("{}\n", encoding="utf-8")

        with self.assertRaises(CalibrationError):
            self._verify(self.source, self.dataset_root, self.admission_receipt, forged_export)

    def test_candidate_package_has_exact_six_file_tree(self) -> None:
        self.assertEqual({path.relative_to(self.export_root).as_posix() for path in self.export_root.rglob("*") if path.is_file()}, _PACKAGE_FILES)
        self.assertFalse(any(path.is_dir() for path in self.export_root.iterdir()))

    def test_candidate_artifacts_retain_only_nonpromotable_permissions(self) -> None:
        model, evaluation, receipt = self._artifacts(self.export_root)
        for artifact in (model, evaluation, receipt, self.fit_result):
            self.assertEqual(artifact["dataset_status"], "open_selection_only")
            self.assertIs(artifact["production_pass"], False)
            assert_permissions_all_false(artifact)
            _assert_nonpromotable(artifact)

    def test_candidate_artifacts_use_the_open_selection_export_schemas(self) -> None:
        model, evaluation, receipt = self._artifacts(self.export_root)
        self.assertEqual(model["schema_version"], "moocow-open-selection-km-fit-model-v1")
        self.assertEqual(evaluation["schema_version"], "moocow-open-selection-km-selection-evaluation-v1")
        self.assertEqual(receipt["schema_version"], "moocow-open-selection-km-fit-export-receipt-v1")

    def test_fit_export_is_byte_deterministic(self) -> None:
        repeated_export = self.root / "repeat-export"
        self._run_fit(self.source, self.dataset_root, self.admission_receipt, repeated_export)
        for relative_path in sorted(_PACKAGE_FILES):
            self.assertEqual(
                (self.export_root / relative_path).read_bytes(),
                (repeated_export / relative_path).read_bytes(),
            )

    def test_verifier_accepts_the_canonical_export(self) -> None:
        verification = self._verify(self.source, self.dataset_root, self.admission_receipt, self.export_root)
        self.assertEqual(verification["status"], "open_selection_fit_export_verified")
        self.assertEqual(verification["state"], "OPEN_SELECTION_FIT_EXPORTED")
        self.assertIs(verification["production_pass"], False)
        self.assertIs(verification["runtime_compatible"], False)
        assert_permissions_all_false(verification)
        _model, _evaluation, receipt = self._artifacts(self.export_root)
        for name in (
            "acquisition_preflight_receipt_sha256",
            "admission_receipt_sha256",
            "dataset_manifest_sha256",
            "open_measurements_sha256",
        ):
            self.assertEqual(verification[name], receipt["bindings"][name])

    def test_verifier_rejects_a_current_model_with_stale_cross_artifact_references(self) -> None:
        forged_export = self.root / "stale-model-reference-export"
        shutil.copytree(self.export_root, forged_export)
        model = _read_json(forged_export / "fit-model.json")
        model["components"][0]["K_mm_inv"][0] += 1e-10
        write_json_with_sha256(forged_export / "fit-model.json", model)

        with self.assertRaises(CalibrationError) as raised:
            self._verify(self.source, self.dataset_root, self.admission_receipt, forged_export)
        self.assertIn("[BINDING]", str(raised.exception))

    def test_verifier_rejects_a_current_evaluation_with_a_stale_receipt_reference(self) -> None:
        forged_export = self.root / "stale-evaluation-reference-export"
        shutil.copytree(self.export_root, forged_export)
        evaluation = _read_json(forged_export / "selection-evaluation.json")
        evaluation["metrics"]["train"]["global"]["rmse"] += 1e-10
        write_json_with_sha256(forged_export / "selection-evaluation.json", evaluation)

        with self.assertRaises(CalibrationError) as raised:
            self._verify(self.source, self.dataset_root, self.admission_receipt, forged_export)
        self.assertIn("[BINDING]", str(raised.exception))

    def test_verifier_rejects_a_fully_rehashed_stale_authority_before_numerical_work(self) -> None:
        forged_export = self.root / "stale-authority-export"
        shutil.copytree(self.export_root, forged_export)
        model, evaluation, receipt = self._artifacts(forged_export)
        stale_manifest_sha256 = "f" * 64
        model["predecessor_bindings"]["dataset_manifest_sha256"] = stale_manifest_sha256
        model_sha256 = write_json_with_sha256(forged_export / "fit-model.json", model)

        evaluation["predecessor_bindings"]["dataset_manifest_sha256"] = stale_manifest_sha256
        evaluation["model"]["sha256"] = model_sha256
        selected_regularization = evaluation["selection"]["selected_regularization"]
        selected_start_index = evaluation["selection"]["selected_start_index"]
        selected = next(
            candidate
            for candidate in evaluation["selection"]["candidates"]
            if candidate.get("valid") is True
            and candidate.get("regularization") == selected_regularization
            and candidate.get("selected_start_index") == selected_start_index
        )
        selected["model_payload_sha256"] = sha256_bytes(canonical_json_bytes(model))
        evaluation_sha256 = write_json_with_sha256(forged_export / "selection-evaluation.json", evaluation)

        receipt["bindings"]["dataset_manifest_sha256"] = stale_manifest_sha256
        receipt["bindings"]["fit_model"]["sha256"] = model_sha256
        receipt["bindings"]["selection_evaluation"]["sha256"] = evaluation_sha256
        receipt_payload = dict(receipt)
        receipt_payload.pop("receipt_payload_sha256", None)
        receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(receipt_payload))
        write_json_with_sha256(forged_export / "fit-export-receipt.json", receipt)

        with mock.patch.object(fit_export, "_verify_stored_metrics", side_effect=AssertionError("late metric verification called")) as metrics_mock:
            with mock.patch.object(fit_export, "_fit_export_objects", side_effect=AssertionError("late refit called")) as refit_mock:
                with self.assertRaises(CalibrationError) as raised:
                    self._verify(self.source, self.dataset_root, self.admission_receipt, forged_export)
        self.assertIn("[BINDING]", str(raised.exception))
        metrics_mock.assert_not_called()
        refit_mock.assert_not_called()

    def test_verifier_semantically_rejects_a_rehashed_model_curve_mutation(self) -> None:
        forged_export = self.root / "forged-export"
        shutil.copytree(self.export_root, forged_export)
        model, evaluation, receipt = self._artifacts(forged_export)
        original_model_sha = sha256_file(forged_export / "fit-model.json")
        original_evaluation_sha = sha256_file(forged_export / "selection-evaluation.json")
        model["components"][0]["K_mm_inv"][0] += 0.01
        replacement_model_sha = write_json_with_sha256(forged_export / "fit-model.json", model)
        evaluation = _replace_digest(evaluation, original_model_sha, replacement_model_sha)
        replacement_evaluation_sha = write_json_with_sha256(forged_export / "selection-evaluation.json", evaluation)
        receipt = _replace_digest(receipt, original_model_sha, replacement_model_sha)
        receipt = _replace_digest(receipt, original_evaluation_sha, replacement_evaluation_sha)
        if not isinstance(receipt, dict):  # pragma: no cover - helper invariant.
            raise AssertionError("receipt replacement must preserve object shape")
        receipt_payload = dict(receipt)
        receipt_payload.pop("receipt_payload_sha256", None)
        receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(receipt_payload))
        write_json_with_sha256(forged_export / "fit-export-receipt.json", receipt)

        with self.assertRaises(CalibrationError):
            self._verify(self.source, self.dataset_root, self.admission_receipt, forged_export)

    def test_verifier_rejects_an_upstream_shared_root_binding_mismatch(self) -> None:
        copied_shared_root = self.root / "mismatched-shared"
        shutil.copytree(self.source["shared_root"], copied_shared_root)
        (copied_shared_root / "labels" / "base.txt").write_text("tampered fixture label\n", encoding="utf-8")
        mismatched_source = {**self.source, "shared_root": copied_shared_root}

        with self.assertRaises(CalibrationError):
            self._verify(mismatched_source, self.dataset_root, self.admission_receipt, self.export_root)

    def test_legacy_generic_loader_cannot_consume_an_open_selection_export(self) -> None:
        with self.assertRaises(DatasetValidationError):
            load_and_validate_dataset(self.export_root)

    def test_legacy_fit_command_cannot_consume_an_open_selection_export(self) -> None:
        output_model = self.root / "legacy-model.json"
        code, stdout, stderr = run_cli(["fit-km", "--dataset", str(self.export_root), "--output-model", str(output_model)])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("ERROR:"))
        self.assertFalse(output_model.exists())

class OpenSelectionFitExportFastContractTests(unittest.TestCase):
    def test_exposed_analytic_jacobian_agrees_with_finite_differences(self) -> None:
        checker = getattr(fit_export, "_analytic_jacobian_finite_difference_check", None)
        if checker is None:
            self.skipTest("the core module does not expose a Jacobian verification helper")
        diagnostics = checker()
        self.assertLessEqual(diagnostics["max_abs_error"], 1e-6)
        self.assertLessEqual(diagnostics["max_relative_error"], 1e-4)

    def test_fit_open_selection_cli_rejects_an_independent_evaluation_flag_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "candidate"
            code, stdout, stderr = run_cli(
                [
                    "fit-open-selection-candidate",
                    "--acquisition-receipt", str(root / "acquisition.json"),
                    "--admission-receipt", str(root / "admission.json"),
                    "--dataset-root", str(root / "dataset"),
                    "--shared-root", str(root / "shared"),
                    "--open-root", str(root / "open"),
                    "--measurement-root", str(root / "measurements"),
                    "--output-dir", str(output_dir),
                    "--independent-evaluation-root", str(root / "independent"),
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("unrecognized arguments: --independent-evaluation-root", stderr)
            self.assertFalse(output_dir.exists())

    def test_verify_open_selection_cli_rejects_a_ranking_flag_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            export_root = root / "candidate"
            code, stdout, stderr = run_cli(
                [
                    "verify-open-selection-candidate-export",
                    "--acquisition-receipt", str(root / "acquisition.json"),
                    "--admission-receipt", str(root / "admission.json"),
                    "--dataset-root", str(root / "dataset"),
                    "--shared-root", str(root / "shared"),
                    "--open-root", str(root / "open"),
                    "--measurement-root", str(root / "measurements"),
                    "--export-root", str(export_root),
                    "--enable-ranking",
                ]
            )
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("unrecognized arguments: --enable-ranking", stderr)
            self.assertFalse(export_root.exists())


class OpenSelectionFitExportReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.source_fixture = write_fit_ready_open_selection_fixture(cls.root / "source")
        cls.dataset_root = cls.root / "admitted"
        admit_open_measurements(
            acquisition_receipt_path=cls.source_fixture["acquisition_receipt"],
            shared_root=cls.source_fixture["shared_root"],
            open_root=cls.source_fixture["open_root"],
            measurement_root=cls.source_fixture["measurement_root"],
            admission_input_relative_path=cls.source_fixture["admission_input_relative_path"],
            output_dir=cls.dataset_root,
        )
        cls.dataset = load_and_validate_open_selection_dataset(cls.dataset_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def _mutated_dataset(
        self,
        *,
        manifest: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
    ) -> ValidatedOpenSelectionDataset:
        return ValidatedOpenSelectionDataset(
            root=self.dataset.root,
            manifest=copy.deepcopy(dict(self.dataset.manifest)) if manifest is None else manifest,
            source=copy.deepcopy(dict(self.dataset.source)) if source is None else source,
            manifest_sha256=self.dataset.manifest_sha256,
            open_measurements_sha256=self.dataset.open_measurements_sha256,
        )

    def test_fit_readiness_rejects_a_missing_backing(self) -> None:
        manifest = copy.deepcopy(dict(self.dataset.manifest))
        manifest["backings"].pop("white")
        with self.assertRaises(CalibrationError):
            fit_export._build_fit_data(self._mutated_dataset(manifest=manifest))

    def test_fit_readiness_rejects_a_train_family_without_both_dft_bands(self) -> None:
        source = copy.deepcopy(dict(self.dataset.source))
        family = next(card["formula_family_id"] for card in source["cards"] if card["split"] == "train")
        for card in source["cards"]:
            if card["formula_family_id"] == family and card["dft_band"] == "DFT-H":
                card["dft_band"] = "DFT-L"
        for measurement in source["measurements"]:
            if measurement["formula_family_id"] == family and measurement["dft_band"] == "DFT-H":
                measurement["dft_band"] = "DFT-L"
        with self.assertRaises(CalibrationError):
            fit_export._build_fit_data(self._mutated_dataset(source=source))

    def test_fit_readiness_rejects_a_missing_reposition_reading(self) -> None:
        source = copy.deepcopy(dict(self.dataset.source))
        removed = source["measurements"].pop(0)
        duplicate = copy.deepcopy(source["measurements"][-1])
        duplicate["instrument_measurement_id"] = f"{removed['instrument_measurement_id']}-DUPLICATE"
        source["measurements"].append(duplicate)
        with self.assertRaises(CalibrationError):
            fit_export._build_fit_data(self._mutated_dataset(source=source))

    def test_fit_readiness_rejects_a_rank_deficient_actual_nv_design(self) -> None:
        source = copy.deepcopy(dict(self.dataset.source))
        train_families = sorted({card["formula_family_id"] for card in source["cards"] if card["split"] == "train"})
        source_family, replacement_family = train_families[:2]
        source_components = copy.deepcopy(
            next(
                measurement["components"]
                for measurement in source["measurements"]
                if measurement["formula_family_id"] == source_family
            )
        )
        for measurement in source["measurements"]:
            if measurement["formula_family_id"] == replacement_family:
                measurement["components"] = copy.deepcopy(source_components)
        with self.assertRaises(CalibrationError):
            fit_export._build_fit_data(self._mutated_dataset(source=source))

    def test_fit_readiness_rejects_a_changed_component_lot(self) -> None:
        source = copy.deepcopy(dict(self.dataset.source))
        source["measurements"][0]["components"][0]["physical_lot_id"] = "LOT-CHANGED"
        with self.assertRaises(CalibrationError):
            fit_export._build_fit_data(self._mutated_dataset(source=source))

    def test_fit_readiness_rejects_a_changed_component_order(self) -> None:
        source = copy.deepcopy(dict(self.dataset.source))
        components = source["measurements"][0]["components"]
        components[0], components[1] = components[1], components[0]
        with self.assertRaises(CalibrationError):
            fit_export._build_fit_data(self._mutated_dataset(source=source))

    def test_finite_film_prediction_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(CalibrationError):
            fit_export._strict_reflectance_and_partials(
                np.asarray([-1.0]),
                np.asarray([0.1]),
                np.asarray([0.05]),
                np.asarray([0.5]),
            )

    def test_projection_is_canonical_when_measurement_records_are_reordered(self) -> None:
        source = copy.deepcopy(dict(self.dataset.source))
        first_measurement = source["measurements"][0]
        target = (first_measurement["card_id"], first_measurement["backing"])
        expected_by_reposition: dict[str, float] = {}
        for measurement in source["measurements"]:
            if (measurement["card_id"], measurement["backing"]) == target:
                offset = {"POS01": 0.0001, "POS02": 0.0002, "POS03": 0.0003}[measurement["reposition_id"]]
                measurement["reflectance"] = [min(1.0, float(value) + offset) for value in measurement["reflectance"]]
                expected_by_reposition[measurement["reposition_id"]] = measurement["reflectance"][0]
        reordered = copy.deepcopy(source)
        reordered["measurements"].reverse()

        baseline_data = fit_export._build_fit_data(self._mutated_dataset(source=source))
        reordered_data = fit_export._build_fit_data(self._mutated_dataset(source=reordered))
        self.assertEqual(
            canonical_json_bytes(baseline_data.train_projection),
            canonical_json_bytes(reordered_data.train_projection),
        )
        target_cell = next(
            cell for cell in baseline_data.train_cells if (cell.card_id, cell.backing) == target
        )
        self.assertEqual(target_cell.reposition_ids, ("POS01", "POS02", "POS03"))
        self.assertEqual(
            target_cell.raw_replicates[:, 0].tolist(),
            [expected_by_reposition[position] for position in target_cell.reposition_ids],
        )


class OpenSelectionFitExportTrainIsolationTests(unittest.TestCase):
    def test_validation_only_mutation_preserves_train_projection_seed_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline_source = write_fit_ready_open_selection_fixture(root / "baseline-source")
            baseline_dataset_root = root / "baseline-admitted"
            admit_open_measurements(
                acquisition_receipt_path=baseline_source["acquisition_receipt"],
                shared_root=baseline_source["shared_root"],
                open_root=baseline_source["open_root"],
                measurement_root=baseline_source["measurement_root"],
                admission_input_relative_path=baseline_source["admission_input_relative_path"],
                output_dir=baseline_dataset_root,
            )
            mutated_source = write_fit_ready_open_selection_fixture(root / "mutated-source")
            validation_cards = {
                str(card["card_id"])
                for card in mutated_source["open_receipt"]["card_skeleton"]
                if card["split"] == "validation"
            }
            for reading in mutated_source["payload"]["readings"]:
                if reading["card_id"] in validation_cards:
                    reading["reflectance"] = [float(value) + 0.001 for value in reading["reflectance"]]
            write_json_with_sha256(mutated_source["admission_input_path"], mutated_source["payload"])
            mutated_dataset_root = root / "mutated-admitted"
            admit_open_measurements(
                acquisition_receipt_path=mutated_source["acquisition_receipt"],
                shared_root=mutated_source["shared_root"],
                open_root=mutated_source["open_root"],
                measurement_root=mutated_source["measurement_root"],
                admission_input_relative_path=mutated_source["admission_input_relative_path"],
                output_dir=mutated_dataset_root,
            )

            baseline_dataset, baseline_data, _baseline_predecessor = fit_export._admission_context(
                acquisition_receipt_path=baseline_source["acquisition_receipt"],
                admission_receipt_path=baseline_dataset_root / "admission-receipt.json",
                dataset_root=baseline_dataset_root,
                shared_root=baseline_source["shared_root"],
                open_root=baseline_source["open_root"],
                measurement_root=baseline_source["measurement_root"],
            )
            mutated_dataset, mutated_data, _mutated_predecessor = fit_export._admission_context(
                acquisition_receipt_path=mutated_source["acquisition_receipt"],
                admission_receipt_path=mutated_dataset_root / "admission-receipt.json",
                dataset_root=mutated_dataset_root,
                shared_root=mutated_source["shared_root"],
                open_root=mutated_source["open_root"],
                measurement_root=mutated_source["measurement_root"],
            )
            baseline_train_sha256 = sha256_bytes(canonical_json_bytes(baseline_data.train_projection))
            mutated_train_sha256 = sha256_bytes(canonical_json_bytes(mutated_data.train_projection))
            self.assertEqual(baseline_train_sha256, mutated_train_sha256)
            self.assertNotEqual(baseline_dataset.manifest_sha256, mutated_dataset.manifest_sha256)

            for regularization in fit_export._REGULARIZATION_GRID:
                with self.subTest(regularization=regularization):
                    baseline_candidate = fit_export._fit_candidate(
                        baseline_data, baseline_train_sha256, regularization
                    )
                    mutated_candidate = fit_export._fit_candidate(
                        mutated_data, mutated_train_sha256, regularization
                    )
                    self.assertIs(baseline_candidate["valid"], True)
                    self.assertIs(mutated_candidate["valid"], True)
                    for field in (
                        "regularization",
                        "objective",
                        "selected_start_index",
                        "starts",
                        "converged",
                        "optimizer_status",
                        "optimizer_nfev",
                        "optimizer_njev",
                        "optimizer_optimality",
                        "bound_counts",
                        "jacobian_by_wavelength",
                    ):
                        self.assertEqual(baseline_candidate[field], mutated_candidate[field])
                    for field in ("eta", "rho", "S_mm_inv", "K_mm_inv", "train_prediction"):
                        np.testing.assert_array_equal(baseline_candidate[field], mutated_candidate[field])

            class CapturedCandidateSeed(RuntimeError):
                pass

            captured_hashes: list[str] = []

            def capture_seed(_data: object, seed_lineage_sha256: str, _regularization: float) -> object:
                captured_hashes.append(seed_lineage_sha256)
                raise CapturedCandidateSeed()

            with mock.patch.object(fit_export, "_fit_candidate", side_effect=capture_seed):
                with self.assertRaises(CapturedCandidateSeed):
                    OpenSelectionFitExportTests._run_fit(
                        baseline_source,
                        baseline_dataset_root,
                        baseline_dataset_root / "admission-receipt.json",
                        root / "baseline-candidate",
                    )
                with self.assertRaises(CapturedCandidateSeed):
                    OpenSelectionFitExportTests._run_fit(
                        mutated_source,
                        mutated_dataset_root,
                        mutated_dataset_root / "admission-receipt.json",
                        root / "mutated-candidate",
                    )

            self.assertEqual(captured_hashes, [baseline_train_sha256, mutated_train_sha256])


if __name__ == "__main__":
    unittest.main()
