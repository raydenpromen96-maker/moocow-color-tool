from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = CALIBRATION_ROOT / "tests"
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from acquisition_preflight_fixtures import run_cli
from km_calibration.acquisition_preflight import COMPONENT_IDS, PERMISSIONS
from km_calibration.hashing import canonical_json_bytes, write_json_with_sha256
from km_calibration.km import finite_film_reflectance
import km_calibration.open_selection_recipe_solver as solver


def _authority() -> solver._Authority:
    wavelengths = np.asarray([400.0, 500.0, 600.0, 700.0], dtype=float)
    pairs = (
        ("base-waterborne-clear", "LOT-BASE"),
        ("colorant-R001", "LOT-R"),
        ("colorant-B001", "LOT-B"),
    )
    absorption = np.asarray(
        [
            [0.4, 0.5, 0.6, 0.7],
            [1.1, 8.0, 2.2, 0.9],
            [0.8, 1.5, 7.0, 5.5],
        ],
        dtype=float,
    )
    scattering = np.asarray(
        [
            [24.0, 25.0, 26.0, 27.0],
            [31.0, 30.0, 29.0, 28.0],
            [21.0, 22.0, 24.0, 26.0],
        ],
        dtype=float,
    )
    hashes = {
        "acquisition_preflight_receipt_sha256": "1" * 64,
        "admission_receipt_sha256": "2" * 64,
        "dataset_manifest_sha256": "3" * 64,
        "open_measurements_sha256": "4" * 64,
        "fit_model_sha256": "5" * 64,
        "selection_evaluation_sha256": "6" * 64,
        "fit_export_receipt_sha256": "7" * 64,
    }
    bindings = tuple(
        {
            "component_id": component_id,
            "physical_lot_id": lot_id,
            "conversion_route": "mass_solids_nonvolatile_density",
            "wet_g_per_nonvolatile_ml_decimal": factor,
            "property_record_ids": [f"PROP-{index}"],
            "property_evidence": {
                "relative_path": f"properties/{index}.json",
                "sha256": str(index + 1) * 64,
            },
        }
        for index, ((component_id, lot_id), factor) in enumerate(zip(pairs, ("1", "2", "1.5"), strict=True))
    )
    return solver._Authority(
        wavelengths=wavelengths,
        component_pairs=pairs,
        base_index=0,
        colorant_indexes={"colorant-R001": 1, "colorant-B001": 2},
        absorption=absorption,
        scattering=scattering,
        backings={
            "black": np.asarray([0.05, 0.055, 0.06, 0.065], dtype=float),
            "white": np.asarray([0.80, 0.79, 0.78, 0.77], dtype=float),
        },
        wet_g_per_nonvolatile_ml=(Decimal("1"), Decimal("2"), Decimal("1.5")),
        material_bindings=bindings,
        train_dft_range_um=(20.0, 60.0),
        open_maximum_total_colorant_fraction=0.40,
        open_per_colorant_maximum={"colorant-R001": 0.30, "colorant-B001": 0.20},
        hashes=hashes,
    )


def _request(
    authority: solver._Authority,
    *,
    fractions: np.ndarray | None = None,
    increment: str = "1",
    maximum_mass: str = "200",
    target_mass: str = "101",
    mass_error: str = "1",
) -> solver._Request:
    truth = np.asarray([0.70, 0.20, 0.10], dtype=float) if fractions is None else fractions
    cells = []
    for cell_id, backing, dft_um in (
        ("TARGET-BLACK", "black", 32.0),
        ("TARGET-WHITE", "white", 41.0),
    ):
        reflected = finite_film_reflectance(
            truth @ authority.absorption,
            truth @ authority.scattering,
            dft_um / 1000.0,
            authority.backings[backing],
        )
        cells.append(
            solver._TargetCell(
                cell_id=cell_id,
                backing=backing,
                dft_um=dft_um,
                weight=1.0,
                reflectance=reflected,
            )
        )
    profiles = tuple(
        solver._DispenseComponent(
            component_id=component_id,
            physical_lot_id=lot_id,
            increment_g=Decimal(increment),
            minimum_nonzero_g=Decimal(increment),
            maximum_wet_mass_g=Decimal(maximum_mass),
            minimum_ticks=1,
            maximum_ticks=int(Decimal(maximum_mass) / Decimal(increment)),
        )
        for component_id, lot_id in authority.component_pairs
    )
    value = {
        "schema_version": solver.REQUEST_SCHEMA,
        "status": "open_selection_recipe_requested",
        "request_id": "REQ-SYNTHETIC-001",
        "evidence_class": "synthetic_test_only",
        "target": {},
        "search_policy": {},
        "batch": {},
    }
    return solver._Request(
        value=value,
        relative_path="recipe-request.json",
        sha256="8" * 64,
        evidence_class="synthetic_test_only",
        request_id="REQ-SYNTHETIC-001",
        target_id="TARGET-SYNTHETIC",
        cells=tuple(cells),
        target_evidence={
            "relative_path": "evidence/target.json",
            "sha256": "9" * 64,
            "record_locator": {"kind": "whole_file"},
            "record_schema_version": solver.TARGET_EVIDENCE_SCHEMA,
        },
        dispense_evidence={
            "relative_path": "evidence/dispense.json",
            "sha256": "a" * 64,
            "record_locator": {"kind": "whole_file"},
            "record_schema_version": solver.DISPENSE_EVIDENCE_SCHEMA,
        },
        allowed_colorants=("colorant-R001", "colorant-B001"),
        maximum_colorants=2,
        maximum_total_colorant_fraction=0.40,
        per_colorant_maximum={"colorant-R001": 0.30, "colorant-B001": 0.20},
        target_wet_mass_g=Decimal(target_mass),
        maximum_total_mass_error_g=Decimal(mass_error),
        dispense_components=profiles,
        dispense_profile_id="BALANCE-SYNTHETIC-1G",
    )


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _authority_loader_fixture(root: Path) -> dict[str, object]:
    export_root = root / "fit-export"
    export_root.mkdir()
    wavelengths = [400.0, 500.0, 600.0, 700.0]
    pairs = [(component_id, f"LOT-{index:02d}") for index, component_id in enumerate(COMPONENT_IDS)]
    components = [
        {
            "component_id": component_id,
            "physical_lot_id": lot_id,
            "K_mm_inv": [0.2 + 0.01 * index] * len(wavelengths),
            "S_mm_inv": [20.0 + index] * len(wavelengths),
        }
        for index, (component_id, lot_id) in enumerate(pairs)
    ]
    model = {
        "schema_version": "moocow-open-selection-km-fit-model-v1",
        "dataset_status": "open_selection_only",
        "status": "open_selection_fit_candidate",
        "production_pass": False,
        **{permission: False for permission in PERMISSIONS},
        "runtime_compatible": False,
        "saunderson": {"mode": "off"},
        "wavelength_nm": wavelengths,
        "component_order": [
            {"component_id": component_id, "physical_lot_id": lot_id}
            for component_id, lot_id in pairs
        ],
        "components": components,
    }
    model_sha = write_json_with_sha256(export_root / "fit-model.json", model)
    evaluation_sha = write_json_with_sha256(export_root / "selection-evaluation.json", {})
    receipt_sha = write_json_with_sha256(export_root / "fit-export-receipt.json", {})
    admission_path = root / "admission-receipt.json"
    admission_sha = write_json_with_sha256(admission_path, {})
    materials = [
        {
            "component_id": component_id,
            "physical_lot_id": lot_id,
            "conversion_route": "mass_solids_nonvolatile_density",
            "properties": {
                "nonvolatile_mass_fraction": {
                    "value": "0.5",
                    "property_record_id": f"NV-MASS-{index:02d}",
                },
                "nonvolatile_density_g_ml": {
                    "value": "1.2",
                    "property_record_id": f"NV-DENSITY-{index:02d}",
                },
            },
            "property_evidence": {
                "relative_path": f"properties/component-{index:02d}.json",
                "file_sha256": _digest(f"property-{index}"),
            },
        }
        for index, (component_id, lot_id) in enumerate(pairs)
    ]
    context = {
        "acquisition_preflight_receipt_sha256": _digest("acquisition"),
        "materials": materials,
    }
    measurements = []
    for active_index in range(1, len(pairs)):
        fractions = [0.0] * len(pairs)
        fractions[0] = 0.85
        fractions[active_index] = 0.15
        measurements.append(
            {
                "split": "train",
                "dft_um": 20.0 if active_index % 2 else 40.0,
                "components": [
                    {
                        "component_id": component_id,
                        "physical_lot_id": lot_id,
                        "nonvolatile_volume_fraction": fractions[index],
                    }
                    for index, (component_id, lot_id) in enumerate(pairs)
                ],
            }
        )
    dataset = SimpleNamespace(
        manifest={
            "wavelength_nm": wavelengths,
            "backings": {
                "black": {"mean_reflectance": [0.04, 0.05, 0.06, 0.07]},
                "white": {"mean_reflectance": [0.90, 0.89, 0.88, 0.87]},
            },
        },
        source={"measurements": measurements},
        manifest_sha256=_digest("manifest"),
        open_measurements_sha256=_digest("open-measurements"),
    )
    verification = {
        "status": "open_selection_fit_export_verified",
        "state": "OPEN_SELECTION_FIT_EXPORTED",
        "production_pass": False,
        **{permission: False for permission in PERMISSIONS},
        "runtime_compatible": False,
        "acquisition_preflight_receipt_sha256": context["acquisition_preflight_receipt_sha256"],
        "admission_receipt_sha256": admission_sha,
        "dataset_manifest_sha256": dataset.manifest_sha256,
        "open_measurements_sha256": dataset.open_measurements_sha256,
        "fit_model_sha256": model_sha,
        "selection_evaluation_sha256": evaluation_sha,
        "fit_export_receipt_sha256": receipt_sha,
    }
    return {
        "export_root": export_root,
        "admission_path": admission_path,
        "admission_sha": admission_sha,
        "model": model,
        "context": context,
        "dataset": dataset,
        "verification": verification,
        "pairs": pairs,
    }


class OpenSelectionRecipeSolverUnitTests(unittest.TestCase):
    def test_continuous_search_recovers_known_two_colorant_truth(self) -> None:
        authority = _authority()
        request = _request(authority)
        candidates = solver._continuous_candidates(authority, request)
        best = candidates[0]
        np.testing.assert_allclose(best["fractions"], [0.70, 0.20, 0.10], rtol=0.0, atol=2e-6)
        self.assertLess(best["metrics"]["spectral_rmse"], 1e-7)
        self.assertEqual(best["active_support"], ["colorant-R001", "colorant-B001"])

    def test_black_only_white_only_and_paired_targets_recover_forward_truth(self) -> None:
        authority = _authority()
        paired = _request(authority)
        cases = {
            "black_only": replace(paired, cells=(paired.cells[0],)),
            "white_only": replace(paired, cells=(paired.cells[1],)),
            "paired": paired,
        }
        for name, request in cases.items():
            with self.subTest(name=name):
                best = solver._continuous_candidates(authority, request)[0]
                np.testing.assert_allclose(best["fractions"], [0.70, 0.20, 0.10], rtol=0.0, atol=1e-6)
                self.assertLess(best["metrics"]["spectral_rmse"], 2e-8)

    def test_any_support_with_no_valid_optimizer_result_fails_closed(self) -> None:
        authority = _authority()
        request = _request(authority)

        def fake_minimize(_objective, start, **_kwargs):
            values = np.asarray(start, dtype=float)
            if len(values) == 2:
                return SimpleNamespace(
                    success=False,
                    x=values,
                    status=9,
                    message="forced two-color failure",
                    nit=0,
                    nfev=0,
                )
            return SimpleNamespace(
                success=True,
                x=values,
                status=0,
                message="forced valid result",
                nit=1,
                nfev=1,
            )

        with mock.patch.object(solver, "minimize", side_effect=fake_minimize):
            with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "OPTIMIZATION_INCOMPLETE"):
                solver._continuous_candidates(authority, request)

    def test_total_optimizer_evaluation_budget_fails_closed(self) -> None:
        authority = _authority()
        request = _request(authority)
        with mock.patch.object(solver, "_MAX_TOTAL_OPTIMIZER_EVALUATIONS", 1):
            with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "OPTIMIZATION_BUDGET"):
                solver._continuous_candidates(authority, request)

    def test_quantized_recipe_is_recomputed_from_actual_wet_mass_lattice(self) -> None:
        authority = _authority()
        request = _request(authority, increment="1", target_mass="101", mass_error="1")
        continuous = solver._continuous_candidates(authority, request)
        quantized = solver._quantized_candidates(authority, request, continuous)
        best = quantized.candidates[0]
        self.assertGreater(quantized.lattice_evaluations, len(quantized.candidates))
        selected = solver._selected_payload(authority, request, continuous, best)

        wet_masses = [Decimal(item["wet_mass_g_decimal"]) for item in selected["quantized"]["wet_masses"]]
        actual = solver._quantized_fraction_vector(authority, wet_masses)
        np.testing.assert_allclose(
            actual,
            [item["nonvolatile_volume_fraction"] for item in selected["quantized"]["components"]],
            rtol=0.0,
            atol=1e-15,
        )
        recomputed = solver._evaluate_fractions(authority, request, actual)
        self.assertEqual(
            canonical_json_bytes(recomputed),
            canonical_json_bytes(selected["quantized"]["metrics"]),
        )
        diagnostic = selected["quantization_degradation"]
        self.assertEqual(diagnostic["signed_delta_definition"], "quantized_minus_continuous")
        self.assertEqual(diagnostic["effect"], "equivalent_within_numerical_tolerance")
        self.assertLess(diagnostic["spectral_rmse_delta"], 0.0)
        self.assertLessEqual(
            abs(Decimal(selected["quantized"]["total_mass_error_g_decimal"])),
            request.maximum_total_mass_error_g,
        )

    def test_batched_objective_is_exactly_partition_invariant(self) -> None:
        authority = _authority()
        request = _request(authority)
        rows = np.asarray(
            [
                [0.70, 0.20, 0.10],
                [0.75, 0.15, 0.10],
                [0.80, 0.05, 0.15],
            ],
            dtype=float,
        )
        batched = solver._objective_mse_batch(authority, request, rows)
        singles = np.asarray(
            [solver._objective_mse_batch(authority, request, row[np.newaxis, :])[0] for row in rows],
            dtype=float,
        )
        np.testing.assert_array_equal(batched, singles)

    def test_streaming_top_k_matches_full_small_lattice_sort(self) -> None:
        authority = _authority()
        request = _request(authority, increment="1", target_mass="101", mass_error="1")
        continuous = solver._continuous_candidates(authority, request)
        normal = solver._quantized_candidates(authority, request, continuous)
        with mock.patch.object(solver, "_RETAINED_QUANTIZED_CANDIDATES", 100_000), mock.patch.object(
            solver,
            "_LATTICE_BATCH_SIZE",
            3,
        ):
            exhaustive = solver._quantized_candidates(authority, request, continuous)
        self.assertEqual(normal.lattice_evaluations, exhaustive.lattice_evaluations)
        self.assertGreater(len(exhaustive.candidates), len(normal.candidates))
        self.assertEqual(
            [candidate["ticks"] for candidate in normal.candidates],
            [candidate["ticks"] for candidate in exhaustive.candidates[: len(normal.candidates)]],
        )
        self.assertEqual(
            [candidate["metrics"]["objective_mse"] for candidate in normal.candidates],
            [candidate["metrics"]["objective_mse"] for candidate in exhaustive.candidates[: len(normal.candidates)]],
        )

    def test_candidate_objects_are_deterministic_and_never_grant_authority(self) -> None:
        authority = _authority()
        request = _request(authority)
        first = solver._build_objects(authority, request)
        second = solver._build_objects(authority, request)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        candidate, receipt = first
        for value in (candidate, receipt):
            self.assertFalse(value["production_pass"])
            self.assertFalse(value["runtime_compatible"])
            self.assertFalse(value["physical_accuracy_verified"])
            self.assertFalse(value["independent_holdout_passed"])
            self.assertFalse(value["production_executable"])
            self.assertTrue(value["laboratory_trial_only"])
        self.assertFalse(candidate["search_diagnostics"]["global_lattice_optimum_proven"])
        self.assertEqual(candidate["uncertainty"]["status"], "insufficient_data")

    def test_infeasible_declared_lattice_fails_closed(self) -> None:
        authority = _authority()
        request = _request(
            authority,
            increment="1",
            maximum_mass="10",
            target_mass="101",
            mass_error="0.1",
        )
        continuous = solver._continuous_candidates(authority, request)
        with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "INFEASIBLE_LATTICE"):
            solver._quantized_candidates(authority, request, continuous)

    def test_both_conversion_routes_produce_wet_grams_per_nv_ml(self) -> None:
        route_one = {
            "conversion_route": "mass_solids_nonvolatile_density",
            "properties": {
                "nonvolatile_mass_fraction": {"value": "0.5"},
                "nonvolatile_density_g_ml": {"value": "1.25"},
            },
        }
        route_two = {
            "conversion_route": "wet_density_volume_solids",
            "properties": {
                "wet_density_g_ml": {"value": "1.4"},
                "component_nonvolatile_volume_fraction": {"value": "0.7"},
            },
        }
        self.assertEqual(solver._conversion_factor(route_one, "route_one"), ("mass_solids_nonvolatile_density", Decimal("2.5")))
        self.assertEqual(solver._conversion_factor(route_two, "route_two"), ("wet_density_volume_solids", Decimal("2")))
        invalid_mass_fraction = copy.deepcopy(route_one)
        invalid_mass_fraction["properties"]["nonvolatile_mass_fraction"]["value"] = "1.01"
        with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "PROPERTY_ROUTE"):
            solver._conversion_factor(invalid_mass_fraction, "invalid_mass_fraction")
        invalid_volume_fraction = copy.deepcopy(route_two)
        invalid_volume_fraction["properties"]["component_nonvolatile_volume_fraction"]["value"] = "1.01"
        with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "PROPERTY_ROUTE"):
            solver._conversion_factor(invalid_volume_fraction, "invalid_volume_fraction")

    def test_extreme_decimal_and_tick_inputs_fail_closed(self) -> None:
        for value in ("1e1000", "1e-20", "9" * 129):
            with self.subTest(value=value), self.assertRaises(solver.OpenSelectionRecipeSolverError):
                solver._decimal(value, "request.value", positive=True)
        with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "DISPENSE_PROFILE"):
            solver._tick_count(Decimal("1000000001"), Decimal("1"), "request.maximum_wet_mass_g")


class OpenSelectionRecipeAuthorityTests(unittest.TestCase):
    @staticmethod
    def _load(fixture: dict[str, object]) -> solver._Authority:
        with mock.patch.object(
            solver,
            "verify_open_selection_fit_export",
            return_value=fixture["verification"],
        ), mock.patch.object(
            solver,
            "load_verified_open_acquisition_context",
            return_value=fixture["context"],
        ), mock.patch.object(
            solver,
            "load_and_validate_open_selection_dataset",
            return_value=fixture["dataset"],
        ):
            return solver._load_authority(
                acquisition_receipt_path="unused-acquisition.json",
                admission_receipt_path=fixture["admission_path"],
                dataset_root="unused-dataset",
                shared_root="unused-shared",
                open_root="unused-open",
                measurement_root="unused-measurement",
                fit_export_root=fixture["export_root"],
            )

    def test_loader_binds_all_current_lots_properties_and_measured_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _authority_loader_fixture(Path(temporary))
            authority = self._load(fixture)
            self.assertEqual(list(authority.component_pairs), fixture["pairs"])
            self.assertEqual(authority.train_dft_range_um, (20.0, 40.0))
            self.assertAlmostEqual(authority.open_maximum_total_colorant_fraction, 0.15)
            self.assertTrue(all(value == 0.15 for value in authority.open_per_colorant_maximum.values()))
            self.assertEqual(authority.hashes["admission_receipt_sha256"], fixture["admission_sha"])
            self.assertTrue(all(value == Decimal("2.4") for value in authority.wet_g_per_nonvolatile_ml))

    def test_loader_rejects_permission_hash_lot_property_and_domain_drift(self) -> None:
        mutations = (
            "verification_permission",
            "model_permission",
            "model_changed_after_verification",
            "acquisition_changed_after_verification",
            "admission_changed_after_verification",
            "dataset_changed_after_verification",
            "open_measurements_changed_after_verification",
            "evaluation_changed_after_verification",
            "fit_receipt_changed_after_verification",
            "current_lot",
            "property_fraction",
            "degenerate_dft_domain",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                fixture = _authority_loader_fixture(Path(temporary))
                if mutation == "verification_permission":
                    fixture["verification"][PERMISSIONS[-1]] = True
                elif mutation == "model_permission":
                    fixture["model"][PERMISSIONS[-1]] = True
                    fixture["verification"]["fit_model_sha256"] = write_json_with_sha256(
                        fixture["export_root"] / "fit-model.json",
                        fixture["model"],
                    )
                elif mutation == "model_changed_after_verification":
                    fixture["model"]["status"] = "tampered"
                    write_json_with_sha256(fixture["export_root"] / "fit-model.json", fixture["model"])
                elif mutation == "acquisition_changed_after_verification":
                    fixture["context"]["acquisition_preflight_receipt_sha256"] = _digest("replacement-acquisition")
                elif mutation == "admission_changed_after_verification":
                    write_json_with_sha256(fixture["admission_path"], {"changed": True})
                elif mutation == "dataset_changed_after_verification":
                    fixture["dataset"].manifest_sha256 = _digest("replacement-manifest")
                elif mutation == "open_measurements_changed_after_verification":
                    fixture["dataset"].open_measurements_sha256 = _digest("replacement-open-measurements")
                elif mutation == "evaluation_changed_after_verification":
                    write_json_with_sha256(
                        fixture["export_root"] / "selection-evaluation.json",
                        {"changed": True},
                    )
                elif mutation == "fit_receipt_changed_after_verification":
                    write_json_with_sha256(
                        fixture["export_root"] / "fit-export-receipt.json",
                        {"changed": True},
                    )
                elif mutation == "current_lot":
                    fixture["context"]["materials"][1]["physical_lot_id"] = "LOT-WRONG"
                elif mutation == "property_fraction":
                    fixture["context"]["materials"][1]["properties"]["nonvolatile_mass_fraction"][
                        "value"
                    ] = "1.01"
                elif mutation == "degenerate_dft_domain":
                    for measurement in fixture["dataset"].source["measurements"]:
                        measurement["dft_um"] = 20.0
                with self.assertRaises(solver.OpenSelectionRecipeSolverError):
                    self._load(fixture)


class OpenSelectionRecipeSolverBoundaryTests(unittest.TestCase):
    def test_protocol_template_is_parseable_but_deliberately_not_executable(self) -> None:
        protocol = CALIBRATION_ROOT / "protocols" / "open-selection-recipe-solver-v1"
        template_path = protocol / "recipe-request.template.json"
        template = json.loads(template_path.read_text(encoding="utf-8"))
        target_template_path = protocol / "target-spectrum-evidence.template.json"
        target_template = json.loads(target_template_path.read_text(encoding="utf-8"))
        dispense_template_path = protocol / "dispense-profile-evidence.template.json"
        dispense_template = json.loads(dispense_template_path.read_text(encoding="utf-8"))
        self.assertEqual(template["schema_version"], solver.REQUEST_SCHEMA)
        self.assertEqual(set(template["target"]), {"evidence"})
        self.assertEqual(set(template["batch"]), {"target_wet_mass_g", "dispense_profile_evidence"})
        self.assertEqual(target_template["schema_version"], solver.TARGET_EVIDENCE_SCHEMA)
        self.assertEqual(target_template["wavelength_nm"], [])
        self.assertEqual(dispense_template["schema_version"], solver.DISPENSE_EVIDENCE_SCHEMA)
        self.assertEqual(len(dispense_template["components"]), 15)
        self.assertIsNone(template["batch"]["target_wet_mass_g"])
        for path in (template_path, target_template_path, dispense_template_path):
            self.assertFalse(path.with_name(f"{path.name}.sha256").exists())

    def test_atomic_export_and_reconstruction_reject_candidate_tampering(self) -> None:
        authority = _authority()
        request = _request(authority)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "candidate"
            with mock.patch.object(solver, "_load_inputs", return_value=(authority, request)):
                result = solver.solve_open_selection_recipe_candidate(
                    acquisition_receipt_path="unused",
                    admission_receipt_path="unused",
                    dataset_root="unused",
                    shared_root="unused",
                    open_root="unused",
                    measurement_root="unused",
                    fit_export_root="unused",
                    request_root="unused",
                    request_relative_path="recipe-request.json",
                    output_dir=output,
                )
                self.assertEqual(result["status"], "laboratory_trial_recipe_candidate_exported")
                verified = solver.verify_open_selection_recipe_candidate(
                    acquisition_receipt_path="unused",
                    admission_receipt_path="unused",
                    dataset_root="unused",
                    shared_root="unused",
                    open_root="unused",
                    measurement_root="unused",
                    fit_export_root="unused",
                    request_root="unused",
                    request_relative_path="recipe-request.json",
                    candidate_root=output,
                )
                self.assertEqual(verified["status"], "laboratory_trial_recipe_candidate_verified")

                candidate_path = output / "recipe-candidate.json"
                candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
                candidate["selected"]["quantized"]["components"][1]["nonvolatile_volume_fraction"] += 0.01
                write_json_with_sha256(candidate_path, candidate)
                with self.assertRaises(solver.OpenSelectionRecipeSolverError):
                    solver.verify_open_selection_recipe_candidate(
                        acquisition_receipt_path="unused",
                        admission_receipt_path="unused",
                        dataset_root="unused",
                        shared_root="unused",
                        open_root="unused",
                        measurement_root="unused",
                        fit_export_root="unused",
                        request_root="unused",
                        request_relative_path="recipe-request.json",
                        candidate_root=output,
                    )

    def test_infeasible_request_creates_no_output_or_staging_tree(self) -> None:
        authority = _authority()
        request = _request(
            authority,
            increment="1",
            maximum_mass="10",
            target_mass="101",
            mass_error="0.1",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "candidate"
            with mock.patch.object(solver, "_load_inputs", return_value=(authority, request)):
                with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "INFEASIBLE_LATTICE"):
                    solver.solve_open_selection_recipe_candidate(
                        acquisition_receipt_path="unused",
                        admission_receipt_path="unused",
                        dataset_root="unused",
                        shared_root="unused",
                        open_root="unused",
                        measurement_root="unused",
                        fit_export_root="unused",
                        request_root="unused",
                        request_relative_path="recipe-request.json",
                        output_dir=output,
                    )
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".candidate.staging-*")), [])

    def test_request_parser_derives_target_and_dispense_values_from_structured_evidence(self) -> None:
        authority = _authority()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            base_request = _request(authority)

            def write_evidence(name: str, payload: dict[str, object]) -> dict[str, object]:
                path = evidence_root / name
                digest = write_json_with_sha256(path, payload)
                return {
                    "relative_path": f"evidence/{name}",
                    "sha256": digest,
                    "record_locator": {"kind": "whole_file"},
                }

            target_record = {
                "schema_version": solver.TARGET_EVIDENCE_SCHEMA,
                "status": "open_selection_target_spectrum_recorded",
                "evidence_class": "synthetic_test_only",
                "target_id": "TARGET-PARSER",
                "wavelength_nm": authority.wavelengths.tolist(),
                "cells": [
                    {
                        "cell_id": cell.cell_id,
                        "backing": cell.backing,
                        "dft_um": cell.dft_um,
                        "weight": cell.weight,
                        "reflectance": cell.reflectance.tolist(),
                    }
                    for cell in base_request.cells
                ],
            }
            dispense_record = {
                "schema_version": solver.DISPENSE_EVIDENCE_SCHEMA,
                "status": "open_selection_dispense_profile_recorded",
                "profile_id": "BALANCE-PARSER",
                "maximum_total_mass_error_g": 1,
                "components": [
                    {
                        "component_id": component_id,
                        "physical_lot_id": lot_id,
                        "increment_g": 1,
                        "minimum_nonzero_g": 1,
                        "maximum_wet_mass_g": 200,
                    }
                    for component_id, lot_id in authority.component_pairs
                ],
            }
            target_binding = write_evidence("target.json", target_record)
            dispense_binding = write_evidence("dispense.json", dispense_record)
            value = {
                "schema_version": solver.REQUEST_SCHEMA,
                "status": "open_selection_recipe_requested",
                "request_id": "REQ-PARSER-001",
                "evidence_class": "synthetic_test_only",
                "target": {"evidence": target_binding},
                "search_policy": {
                    "allowed_colorant_component_ids": list(base_request.allowed_colorants),
                    "maximum_colorants": 2,
                    "maximum_total_colorant_nonvolatile_volume_fraction": 0.4,
                    "per_colorant_maximum_nonvolatile_volume_fraction": {
                        "colorant-R001": 0.3,
                        "colorant-B001": 0.2,
                    },
                },
                "batch": {
                    "target_wet_mass_g": 101,
                    "dispense_profile_evidence": dispense_binding,
                },
            }
            request_path = root / "recipe-request.json"
            request_sha = write_json_with_sha256(request_path, value)
            raw, relative, digest = solver._read_request_json(root, "recipe-request.json")
            self.assertEqual(digest, request_sha)
            parsed = solver._parse_request(
                raw,
                relative_path=relative,
                request_sha256=digest,
                request_root=root,
                authority=authority,
            )
            self.assertEqual(parsed.request_id, "REQ-PARSER-001")
            self.assertEqual(parsed.target_id, target_record["target_id"])
            self.assertEqual(parsed.dispense_profile_id, dispense_record["profile_id"])
            self.assertEqual(parsed.target_evidence["record_schema_version"], solver.TARGET_EVIDENCE_SCHEMA)
            self.assertEqual(parsed.dispense_evidence["record_schema_version"], solver.DISPENSE_EVIDENCE_SCHEMA)

            duplicate = copy.deepcopy(value)
            duplicate["batch"]["dispense_profile_evidence"] = copy.deepcopy(duplicate["target"]["evidence"])
            with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "EVIDENCE_REUSE"):
                solver._parse_request(
                    duplicate,
                    relative_path=relative,
                    request_sha256=digest,
                    request_root=root,
                    authority=authority,
                )

            arbitrary_binding = write_evidence("arbitrary.json", {"note": "not target evidence"})
            arbitrary = copy.deepcopy(value)
            arbitrary["target"]["evidence"] = arbitrary_binding
            with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "TARGET_EVIDENCE|SCHEMA|TYPE|TEXT"):
                solver._parse_request(
                    arbitrary,
                    relative_path=relative,
                    request_sha256=digest,
                    request_root=root,
                    authority=authority,
                )

            outside_target_record = copy.deepcopy(target_record)
            outside_target_record["cells"][0]["dft_um"] = 61
            outside_dft = copy.deepcopy(value)
            outside_dft["target"]["evidence"] = write_evidence("target-outside-dft.json", outside_target_record)
            with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "TARGET_DFT_EXTRAPOLATION"):
                solver._parse_request(
                    outside_dft,
                    relative_path=relative,
                    request_sha256=digest,
                    request_root=root,
                    authority=authority,
                )

            outside_total = copy.deepcopy(value)
            outside_total["search_policy"]["maximum_total_colorant_nonvolatile_volume_fraction"] = 0.41
            with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "SEARCH_EXTRAPOLATION"):
                solver._parse_request(
                    outside_total,
                    relative_path=relative,
                    request_sha256=digest,
                    request_root=root,
                    authority=authority,
                )

            outside_component = copy.deepcopy(value)
            outside_component["search_policy"]["per_colorant_maximum_nonvolatile_volume_fraction"][
                "colorant-R001"
            ] = 0.31
            with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "SEARCH_EXTRAPOLATION"):
                solver._parse_request(
                    outside_component,
                    relative_path=relative,
                    request_sha256=digest,
                    request_root=root,
                    authority=authority,
                )

            wrong_profile_record = copy.deepcopy(dispense_record)
            wrong_profile_record["components"][1]["physical_lot_id"] = "LOT-WRONG"
            wrong_lot = copy.deepcopy(value)
            wrong_lot["batch"]["dispense_profile_evidence"] = write_evidence(
                "dispense-wrong-lot.json",
                wrong_profile_record,
            )
            with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "DISPENSE_PROFILE"):
                solver._parse_request(
                    wrong_lot,
                    relative_path=relative,
                    request_sha256=digest,
                    request_root=root,
                    authority=authority,
                )

            lab_adapter = copy.deepcopy(value)
            lab_adapter["target"]["lab"] = [50, 0, 0]
            with self.assertRaisesRegex(solver.OpenSelectionRecipeSolverError, "SCHEMA"):
                solver._parse_request(
                    lab_adapter,
                    relative_path=relative,
                    request_sha256=digest,
                    request_root=root,
                    authority=authority,
                )


class OpenSelectionRecipeSolverCliTests(unittest.TestCase):
    @staticmethod
    def _authority_arguments(root: Path) -> list[str]:
        return [
            "--acquisition-receipt", str(root / "acquisition.json"),
            "--admission-receipt", str(root / "admission.json"),
            "--dataset-root", str(root / "dataset"),
            "--shared-root", str(root / "shared"),
            "--open-root", str(root / "open"),
            "--measurement-root", str(root / "measurements"),
            "--fit-export-root", str(root / "fit-export"),
            "--request-root", str(root / "request"),
            "--request-relative-path", "recipe-request.json",
        ]

    def test_solve_cli_maps_only_declared_offline_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "candidate"
            expected = {
                "status": "laboratory_trial_recipe_candidate_exported",
                "production_pass": False,
                "runtime_compatible": False,
            }
            with mock.patch.object(solver, "solve_open_selection_recipe_candidate", return_value=expected) as call:
                code, stdout, stderr = run_cli(
                    [
                        "solve-open-selection-recipe-candidate",
                        *self._authority_arguments(root),
                        "--output-dir", str(output),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(json.loads(stdout), expected)
            call.assert_called_once_with(
                acquisition_receipt_path=root / "acquisition.json",
                admission_receipt_path=root / "admission.json",
                dataset_root=root / "dataset",
                shared_root=root / "shared",
                open_root=root / "open",
                measurement_root=root / "measurements",
                fit_export_root=root / "fit-export",
                request_root=root / "request",
                request_relative_path="recipe-request.json",
                output_dir=output,
            )

    def test_verify_cli_maps_candidate_root_without_mutation_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            expected = {
                "status": "laboratory_trial_recipe_candidate_verified",
                "production_pass": False,
                "runtime_compatible": False,
            }
            with mock.patch.object(solver, "verify_open_selection_recipe_candidate", return_value=expected) as call:
                code, stdout, stderr = run_cli(
                    [
                        "verify-open-selection-recipe-candidate",
                        *self._authority_arguments(root),
                        "--candidate-root", str(candidate),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(json.loads(stdout), expected)
            call.assert_called_once_with(
                acquisition_receipt_path=root / "acquisition.json",
                admission_receipt_path=root / "admission.json",
                dataset_root=root / "dataset",
                shared_root=root / "shared",
                open_root=root / "open",
                measurement_root=root / "measurements",
                fit_export_root=root / "fit-export",
                request_root=root / "request",
                request_relative_path="recipe-request.json",
                candidate_root=candidate,
            )

    def test_recipe_commands_reject_activation_holdout_and_production_flags_before_io(self) -> None:
        forbidden = [
            ["--activate"],
            ["--holdout-root", "sealed"],
            ["--promote"],
            ["--production"],
            ["--runtime-output", "runtime.json"],
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for command, final_option in (
                ("solve-open-selection-recipe-candidate", ["--output-dir", str(root / "output")]),
                ("verify-open-selection-recipe-candidate", ["--candidate-root", str(root / "candidate")]),
            ):
                for extra in forbidden:
                    with self.subTest(command=command, extra=extra):
                        code, stdout, stderr = run_cli(
                            [command, *self._authority_arguments(root), *final_option, *extra]
                        )
                        self.assertEqual(code, 2)
                        self.assertEqual(stdout, "")
                        self.assertIn("unrecognized arguments:", stderr)
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
