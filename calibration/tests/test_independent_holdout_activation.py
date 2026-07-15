"""Contract and attack tests for independent holdout activation evidence."""

from __future__ import annotations

import copy
import base64
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = CALIBRATION_ROOT / "tests"
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

import km_calibration.independent_holdout_activation as activation
from km_calibration.hashing import canonical_json_bytes, sha256_bytes, write_json_with_sha256

from independent_holdout_activation_fixtures import (
    CIEDE2000_VECTORS,
    SealedReadGuard,
    assert_all_permissions_false,
    assert_public_receipt_has_no_sealed_values,
    patched_open_selection_dependencies,
    read_json,
    require_exact_inventory,
    sealed_private_sentinels,
    self_bind,
    synthetic_holdout_inventory,
    write_synthetic_holdout_fixture,
)


class IndependentHoldoutActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.fixture = write_synthetic_holdout_fixture(self.root / "source")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _run(
        self,
        output_dir: Path,
        *,
        release_ledger_dir: Path | None = None,
        fixture: object | None = None,
    ) -> dict[str, object]:
        selected = self.fixture if fixture is None else fixture
        with patched_open_selection_dependencies(activation, selected):
            return activation.run_independent_holdout_evaluation(
                **selected.run_kwargs(output_dir), release_ledger_dir=release_ledger_dir
            )

    def _verify(
        self,
        output_dir: Path,
        *,
        release_ledger_dir: Path | None = None,
        fixture: object | None = None,
    ) -> dict[str, object]:
        selected = self.fixture if fixture is None else fixture
        with patched_open_selection_dependencies(activation, selected):
            return activation.verify_independent_holdout_evaluation(
                **selected.verify_kwargs(output_dir), release_ledger_dir=release_ledger_dir
            )

    def _assert_rejected_after_sealed_mutation(self, payload: dict[str, object], code: str) -> None:
        self.fixture.rewrite_sealed(payload)
        with patched_open_selection_dependencies(activation, self.fixture):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.run_independent_holdout_evaluation(**self.fixture.run_kwargs(self.root / "rejected-output"))
        self.assertEqual(caught.exception.stage, "CUSTODY_INPUT")
        self.assertEqual(caught.exception.code, code)

    def test_fixture_has_the_fixed_component_lot_holdout_inventory(self) -> None:
        inventory = synthetic_holdout_inventory()
        self.assertEqual(inventory.family_count, 3)
        self.assertEqual(inventory.card_count, 9)
        self.assertEqual(inventory.cell_count, 18)
        self.assertEqual(inventory.reading_count, 54)
        self.assertEqual(len(self.fixture.sealed_payload["component_order"]), 15)
        self.assertEqual(len({item["physical_lot_id"] for item in self.fixture.sealed_payload["component_order"]}), 15)
        self.assertEqual([item["condition_id"] for item in read_json(self.fixture.colorimetry_profile_path)["conditions"]], ["D65/10", "A/10"])
        self.assertEqual(
            self.fixture.sealed_payload["locked_conditions_sha256"],
            sha256_bytes(canonical_json_bytes(self.fixture.open_dataset_manifest["locked_conditions"])),
        )

        evidence_hashes = [self.fixture.sealed_payload["evidence_manifest_sha256"]]
        evidence_hashes.extend(item["source_sha256"] for item in self.fixture.sealed_payload["backings"].values())
        evidence_hashes.extend(cell["evidence_sha256"] for cell in self.fixture.sealed_payload["cells"])
        evidence_hashes.extend(
            reading["source_sha256"]
            for cell in self.fixture.sealed_payload["cells"]
            for reading in cell["readings"]
        )
        self.assertEqual(len(evidence_hashes), len(set(evidence_hashes)))

        families: dict[str, list[dict[str, object]]] = {}
        for cell in self.fixture.sealed_payload["cells"]:
            families.setdefault(cell["formula_family_id"], []).append(cell)
        self.assertEqual(set(families), {"FAM-HO-MIX-01", "FAM-HO-MIX-02", "FAM-HO-MIX-03"})
        lineage_pairs = set()
        for cells in families.values():
            self.assertEqual(len(cells), 6)
            vectors = {
                tuple(component["nonvolatile_volume_fraction"] for component in cell["components"])
                for cell in cells
            }
            self.assertEqual(len(vectors), 1)
            lineage_pairs.add((cells[0]["formula_id"], cells[0]["formula_batch_id"]))
        self.assertEqual(len(lineage_pairs), 3)

        for condition in read_json(self.fixture.colorimetry_profile_path)["conditions"]:
            for axis, white in zip(("x", "y", "z"), condition["reference_white"], strict=True):
                self.assertAlmostEqual(white, sum(condition[f"{axis}_weight"]))
                self.assertTrue(all(weight >= 0.0 for weight in condition[f"{axis}_weight"]))
        for layer in read_json(self.fixture.repeatability_receipt_path)["layers"].values():
            self.assertIsInstance(layer["n"], int)
            self.assertEqual(layer["n"], len(layer["observed_values"]))
            self.assertTrue(all(value >= 0.0 for value in layer["observed_values"]))

    def test_synthetic_run_and_reverification_remain_ineligible(self) -> None:
        output = self.root / "evaluation"
        ledger = self.root / "synthetic-ledger"
        ledger.mkdir()
        result = self._run(output, release_ledger_dir=ledger)
        verified = self._verify(output, release_ledger_dir=ledger)
        detail = read_json(output / "sealed-holdout-evaluation-detail.json")
        receipt = read_json(output / "independent-holdout-review-receipt.json")

        for value in (result, verified, detail, receipt):
            assert_all_permissions_false(value)
        self.assertEqual(result["state"], "SYNTHETIC_EVALUATION_ONLY")
        self.assertEqual(result["verdict"], "INDETERMINATE")
        self.assertFalse(result["activation_review_eligible"])
        self.assertTrue(result["release_replay_protected"])
        self.assertEqual(verified["state"], "SYNTHETIC_EVALUATION_ONLY")
        self.assertEqual(verified["verdict"], "INDETERMINATE")
        self.assertFalse(verified["activation_review_eligible"])
        self.assertTrue(verified["release_replay_protected"])
        self.assertFalse(receipt["activation_review_eligible"])
        require_exact_inventory(receipt["counts"])
        self.assertGreater(detail["aggregate_metrics"]["spectral_rmse"]["median_improvement"], 0.0)

    def test_identical_logical_inputs_write_byte_deterministic_outputs(self) -> None:
        first = self.root / "first-output"
        second = self.root / "second-output"
        self._run(first)
        self._run(second)
        first_members = sorted(path.relative_to(first) for path in first.iterdir())
        self.assertEqual(first_members, sorted(path.relative_to(second) for path in second.iterdir()))
        for relative_path in first_members:
            self.assertEqual((first / relative_path).read_bytes(), (second / relative_path).read_bytes(), relative_path.as_posix())

    def test_matches_published_ciede2000_reference_vectors(self) -> None:
        for lab1, lab2, expected in CIEDE2000_VECTORS:
            self.assertAlmostEqual(activation._delta_e_2000(lab1, lab2), expected, delta=5e-5)

    def test_rejects_invalid_preregistration_signature(self) -> None:
        envelope = read_json(self.fixture.preregistration_envelope_path)
        signature = envelope["signatures"][0]["signature_base64"]
        raw_signature = base64.b64decode(signature)
        envelope["signatures"][0]["signature_base64"] = base64.b64encode(bytes([raw_signature[0] ^ 1, *raw_signature[1:]])).decode("ascii")
        write_json_with_sha256(self.fixture.preregistration_envelope_path, envelope)
        with patched_open_selection_dependencies(activation, self.fixture):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.verify_holdout_preregistration(**self.fixture.authority_kwargs)
        self.assertEqual(caught.exception.stage, "AUTHORITY")
        self.assertEqual(caught.exception.code, "INVALID_SIGNATURE")

    def test_rejects_custodian_signature_for_reviewer_preregistration(self) -> None:
        self.fixture.rewrite_preregistration(
            self.fixture.preregistration_payload,
            private_key=self.fixture.custodian_private_key,
            role="custodian",
            key_id="synthetic-custodian",
        )
        with patched_open_selection_dependencies(activation, self.fixture):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.verify_holdout_preregistration(**self.fixture.authority_kwargs)
        self.assertEqual(caught.exception.stage, "AUTHORITY")
        self.assertEqual(caught.exception.code, "SIGNATURE_ROLE")

    def test_rejects_candidate_authority_before_opening_sealed_input(self) -> None:
        events: list[str] = []
        guard = SealedReadGuard(self.fixture.sealed_input_path, events)
        with patched_open_selection_dependencies(activation, self.fixture, events):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                # A non-verifying candidate is the authority guard; the sealed path is a spy.
                original = self.fixture.candidate_verification["status"]
                self.fixture.candidate_verification["status"] = "not-verified"
                try:
                    activation.run_independent_holdout_evaluation(
                        **{**self.fixture.run_kwargs(self.root / "unreachable-output"), "sealed_input_path": guard}
                    )
                finally:
                    self.fixture.candidate_verification["status"] = original
        self.assertEqual(caught.exception.stage, "AUTHORITY")
        self.assertEqual(caught.exception.code, "CANDIDATE")
        self.assertEqual(events, ["candidate_verify"])

    def test_reverifies_open_authority_before_reading_sealed_input(self) -> None:
        events: list[str] = []
        guard = SealedReadGuard(self.fixture.sealed_input_path, events)
        with patched_open_selection_dependencies(activation, self.fixture, events):
            activation.run_independent_holdout_evaluation(
                **{**self.fixture.run_kwargs(self.root / "ordered-output"), "sealed_input_path": guard}
            )
        self.assertLess(events.index("candidate_verify"), events.index("sealed_input_read"))
        self.assertLess(events.index("open_dataset"), events.index("sealed_input_read"))

    def test_rejects_missing_holdout_cell(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["cells"].pop()
        self._assert_rejected_after_sealed_mutation(payload, "CELL_COUNT")

    def test_rejects_duplicate_holdout_cell(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["cells"][-1] = copy.deepcopy(payload["cells"][0])
        self._assert_rejected_after_sealed_mutation(payload, "CELL_DUPLICATE")

    def test_rejects_extra_holdout_cell(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["cells"].append(copy.deepcopy(payload["cells"][0]))
        self._assert_rejected_after_sealed_mutation(payload, "CELL_COUNT")

    def test_rejects_missing_reposition_reading(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["cells"][0]["readings"].pop()
        self._assert_rejected_after_sealed_mutation(payload, "REPOSITION_COUNT")

    def test_rejects_duplicate_reposition_reading(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["cells"][0]["readings"][2] = copy.deepcopy(payload["cells"][0]["readings"][0])
        self._assert_rejected_after_sealed_mutation(payload, "REPOSITION")

    def test_rejects_extra_reposition_reading(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["cells"][0]["readings"].append(copy.deepcopy(payload["cells"][0]["readings"][0]))
        self._assert_rejected_after_sealed_mutation(payload, "REPOSITION_COUNT")

    def test_rejects_nonascending_dft_order(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["cells"][4]["dft_um"] = 50.0
        self._assert_rejected_after_sealed_mutation(payload, "DFT_ORDER")

    def test_rejects_identical_black_and_white_backing_spectra(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["backings"]["white"]["mean_reflectance"] = copy.deepcopy(
            payload["backings"]["black"]["mean_reflectance"]
        )
        self._assert_rejected_after_sealed_mutation(payload, "BACKING")

    def test_rejects_component_lot_mismatch(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["component_order"][0]["physical_lot_id"] = "LOT-WRONG"
        self._assert_rejected_after_sealed_mutation(payload, "COMPONENT_ORDER")

    def test_rejects_component_order_mismatch(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["component_order"][0], payload["component_order"][1] = payload["component_order"][1], payload["component_order"][0]
        self._assert_rejected_after_sealed_mutation(payload, "COMPONENT_ORDER")

    def test_rejects_wavelength_grid_mismatch(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["wavelength_nm"][-1] = 710.0
        self._assert_rejected_after_sealed_mutation(payload, "WAVELENGTH")

    def test_rejects_open_identity_leakage(self) -> None:
        payload = copy.deepcopy(self.fixture.sealed_payload)
        payload["cells"][0]["formula_id"] = "OPEN-FORMULA-01"
        self._assert_rejected_after_sealed_mutation(payload, "SPLIT_LEAKAGE")

    def test_rejects_rehashed_stale_acceptance_profile_binding(self) -> None:
        profile = read_json(self.fixture.acceptance_profile_path)
        profile["thresholds"]["d65_de00_median_min_improvement"] = 0.25
        write_json_with_sha256(self.fixture.acceptance_profile_path, profile)
        with patched_open_selection_dependencies(activation, self.fixture):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.verify_holdout_preregistration(**self.fixture.authority_kwargs)
        self.assertEqual(caught.exception.stage, "AUTHORITY")
        self.assertEqual(caught.exception.code, "PREREGISTRATION")

    def test_rejects_rehashed_release_binding_change(self) -> None:
        release = copy.deepcopy(self.fixture.release_payload)
        release["custody_commitment_sha256"] = "0" * 64
        self.fixture.rewrite_release(release)
        with patched_open_selection_dependencies(activation, self.fixture):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.run_independent_holdout_evaluation(**self.fixture.run_kwargs(self.root / "release-rejected"))
        self.assertEqual(caught.exception.stage, "AUTHORITY")
        self.assertEqual(caught.exception.code, "RELEASE_BINDING")

    def test_rejects_synthetic_preregistration_relabelled_as_measured_before_release_or_sealed_access(self) -> None:
        preregistration = copy.deepcopy(self.fixture.preregistration_payload)
        preregistration["evidence_class"] = "measured_current_batch"
        self.fixture.rewrite_preregistration(preregistration)
        events: list[str] = []
        output = self.root / "measured-relabelled-output"
        release_guard = SealedReadGuard(self.fixture.release_envelope_path, events)
        sealed_guard = SealedReadGuard(self.fixture.sealed_input_path, events)
        with patched_open_selection_dependencies(activation, self.fixture, events):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.run_independent_holdout_evaluation(
                    **{
                        **self.fixture.run_kwargs(output),
                        "release_envelope_path": release_guard,
                        "sealed_input_path": sealed_guard,
                    }
                )
        self.assertEqual(caught.exception.stage, "AUTHORITY")
        self.assertEqual(caught.exception.code, "MEASURED_AUTHORITY_UNAVAILABLE")
        self.assertEqual(events, ["candidate_verify", "open_dataset"])
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".measured-relabelled-output.staging-*")), [])

    def test_rejects_reissued_logical_release_with_a_ledger_before_sealed_input(self) -> None:
        ledger = self.root / "replay-ledger"
        ledger.mkdir()
        self._run(self.root / "first-synthetic-output", release_ledger_dir=ledger)
        reissued_release = copy.deepcopy(self.fixture.release_payload)
        reissued_release["issued_at"] = "2026-07-15T00:01:00+00:00"
        reissued_release["one_time_nonce"] = "synthetic-nonce-0002"
        self.fixture.rewrite_release(reissued_release)
        events: list[str] = []
        guard = SealedReadGuard(self.fixture.sealed_input_path, events)
        with patched_open_selection_dependencies(activation, self.fixture, events):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.run_independent_holdout_evaluation(
                    **{
                        **self.fixture.run_kwargs(self.root / "second-synthetic-output"),
                        "sealed_input_path": guard,
                    },
                    release_ledger_dir=ledger,
                )
        self.assertEqual(caught.exception.stage, "ACTIVATION_DECISION")
        self.assertEqual(caught.exception.code, "RELEASE_REPLAY")
        self.assertEqual(events, ["candidate_verify", "open_dataset"])
        self.assertEqual(list(self.root.glob(".second-synthetic-output.staging-*")), [])

    def test_verification_with_a_synthetic_ledger_requires_the_retained_release_marker(self) -> None:
        output = self.root / "synthetic-marker-output"
        ledger = self.root / "marker-ledger"
        ledger.mkdir()
        self._run(output, release_ledger_dir=ledger)
        markers = list(ledger.iterdir())
        self.assertEqual(len(markers), 1)
        shutil.rmtree(markers[0])
        events: list[str] = []
        guard = SealedReadGuard(self.fixture.sealed_input_path, events)
        with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
            with patched_open_selection_dependencies(activation, self.fixture, events):
                activation.verify_independent_holdout_evaluation(
                    **{
                        **self.fixture.verify_kwargs(output),
                        "sealed_input_path": guard,
                    },
                    release_ledger_dir=ledger,
                )
        self.assertEqual(caught.exception.stage, "RECONSTRUCTION")
        self.assertEqual(caught.exception.code, "RELEASE_MARKER")
        self.assertEqual(events, ["candidate_verify", "open_dataset"])

    def test_tampered_ledger_marker_rejects_before_sealed_input(self) -> None:
        output = self.root / "tampered-marker-output"
        ledger = self.root / "tampered-marker-ledger"
        ledger.mkdir()
        self._run(output, release_ledger_dir=ledger)
        marker = next(ledger.iterdir())
        record_path = marker / "release-consumption.json"
        record = read_json(record_path)
        record["sealed_input_sha256"] = "0" * 64
        write_json_with_sha256(record_path, record)
        events: list[str] = []
        guard = SealedReadGuard(self.fixture.sealed_input_path, events)
        with patched_open_selection_dependencies(activation, self.fixture, events):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.verify_independent_holdout_evaluation(
                    **{
                        **self.fixture.verify_kwargs(output),
                        "sealed_input_path": guard,
                    },
                    release_ledger_dir=ledger,
                )
        self.assertEqual(caught.exception.stage, "RECONSTRUCTION")
        self.assertEqual(caught.exception.code, "RELEASE_MARKER")
        self.assertEqual(events, ["candidate_verify", "open_dataset"])

    def test_rejects_ledger_ancestor_symlink_before_sealed_input(self) -> None:
        external = self.root / "ledger-external"
        external.mkdir()
        ledger = external / "ledger"
        ledger.mkdir()
        redirected_parent = self.root / "ledger-parent-link"
        try:
            redirected_parent.symlink_to(external, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Directory symlink setup is unavailable: {error}")

        events: list[str] = []
        guard = SealedReadGuard(self.fixture.sealed_input_path, events)
        with patched_open_selection_dependencies(activation, self.fixture, events):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.run_independent_holdout_evaluation(
                    **{**self.fixture.run_kwargs(self.root / "unreachable-output"), "sealed_input_path": guard},
                    release_ledger_dir=redirected_parent / "ledger",
                )
        self.assertEqual(caught.exception.stage, "AUTHORITY")
        self.assertEqual(caught.exception.code, "RELEASE_LEDGER")
        self.assertEqual(events, ["candidate_verify", "open_dataset"])

    def test_rejects_output_ancestor_symlink_before_sealed_input(self) -> None:
        external = self.root / "output-external"
        external.mkdir()
        redirected_parent = self.root / "output-parent-link"
        try:
            redirected_parent.symlink_to(external, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Directory symlink setup is unavailable: {error}")

        events: list[str] = []
        guard = SealedReadGuard(self.fixture.sealed_input_path, events)
        with patched_open_selection_dependencies(activation, self.fixture, events):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.run_independent_holdout_evaluation(
                    **{**self.fixture.run_kwargs(redirected_parent / "output"), "sealed_input_path": guard}
                )
        self.assertEqual(caught.exception.stage, "ACTIVATION_DECISION")
        self.assertEqual(caught.exception.code, "OUTPUT_PATH")
        self.assertEqual(events, ["candidate_verify", "open_dataset"])

    def test_public_receipt_contains_no_sealed_holdout_values(self) -> None:
        output = self.root / "receipt-output"
        self._run(output)
        receipt = read_json(output / "independent-holdout-review-receipt.json")
        assert_public_receipt_has_no_sealed_values(receipt, sealed_private_sentinels())

    def test_reverification_rejects_a_rehashed_metric_mutation(self) -> None:
        output = self.root / "metric-output"
        self._run(output)
        detail_path = output / "sealed-holdout-evaluation-detail.json"
        detail = read_json(detail_path)
        detail["aggregate_metrics"]["spectral_rmse"]["candidate"]["median"] += 0.001
        write_json_with_sha256(detail_path, self_bind(detail, "detail_payload_sha256"))
        with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
            self._verify(output)
        self.assertEqual(caught.exception.stage, "RECONSTRUCTION")
        self.assertEqual(caught.exception.code, "MISMATCH")

    def test_rejects_nonempty_output_reuse_and_leaves_no_staging_directory(self) -> None:
        output = self.root / "reuse-output"
        self._run(output)
        self.assertEqual(list(self.root.glob(".reuse-output.staging-*")), [])
        second_fixture = write_synthetic_holdout_fixture(self.root / "second-source")
        with patched_open_selection_dependencies(activation, second_fixture):
            with self.assertRaises(activation.IndependentHoldoutActivationError) as caught:
                activation.run_independent_holdout_evaluation(**second_fixture.run_kwargs(output))
        self.assertEqual(caught.exception.stage, "ACTIVATION_DECISION")
        self.assertEqual(caught.exception.code, "OUTPUT_NOT_EMPTY")


if __name__ == "__main__":
    unittest.main()
