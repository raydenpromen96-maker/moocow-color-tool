from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(CALIBRATION_ROOT / "tests"))

from acquisition_preflight_fixtures import (
    assertPreflight,
    assert_permissions_all_false,
    assert_sidecar_matches,
    copytree_without_rewriting,
    write_current_materials,
    write_frozen_pilot_prerequisite,
    write_pilot_batch_roots,
)
import km_calibration.acquisition_preflight as acquisition
from km_calibration.acquisition_preflight import HOLDOUT_FAMILIES, TRAIN_FAMILIES, VALIDATION_FAMILIES
from km_calibration.hashing import sha256_bytes, write_json_with_sha256


_PUBLIC_HOLDOUT_KEYS = {
    "holdout",
    "holdout_custody_commitment_sha256",
    "holdout_formula_mapping_sha256",
    "holdout_release_permitted",
    "sealed_batch_manifest_sha256",
}


def _assert_no_public_holdout_raw(value: object) -> None:
    """Recursively reject raw sealed identifiers and undisclosed sealed fields."""

    def visit(item: object, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                key_text = str(key).casefold()
                if ("holdout" in key_text or "sealed" in key_text) and key_text not in _PUBLIC_HOLDOUT_KEYS:
                    raise AssertionError(f"prohibited public holdout field at {path}.{key}")
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
        elif isinstance(item, str):
            lowered = item.casefold()
            if "fam-ho-" in lowered or "sealed-holdout" in lowered:
                raise AssertionError(f"prohibited public holdout value at {path}")

    visit(value, "$")


class AcquisitionPreflightIntegrationTests(unittest.TestCase):
    def _source(self, root: Path) -> dict[str, object]:
        prerequisite = write_frozen_pilot_prerequisite(root)
        shared_root, materials = write_current_materials(root, prerequisite)
        return {
            "prerequisite": prerequisite,
            "shared_root": shared_root,
            **write_pilot_batch_roots(root, shared_root, materials),
        }

    def _common(self, source: dict[str, object], root: Path) -> Path:
        result = acquisition.preflight_pilot_materials(
            **source["prerequisite"],
            shared_root=source["shared_root"],
            output_dir=root / "common-output",
        )
        assert_permissions_all_false(result)
        return root / "common-output" / "common-material-receipt.json"

    def _open(self, source: dict[str, object], common: Path, root: Path) -> Path:
        result = acquisition.preflight_open_batches(
            materials_receipt_path=common,
            open_batch_root=source["open_batch_root"],
            open_evidence_root=source["open_evidence_root"],
            output_dir=root / "open-output",
        )
        assert_permissions_all_false(result)
        return root / "open-output" / "open-batch-preflight-receipt.json"

    def _commit_holdout(self, source: dict[str, object], common: Path, open_receipt: Path, root: Path) -> Path:
        result = acquisition.commit_holdout_custody(
            materials_receipt_path=common,
            open_batch_receipt_path=open_receipt,
            sealed_holdout_batch_root=source["sealed_batch_root"],
            sealed_evidence_root=source["sealed_evidence_root"],
            custody_identity="independent sealed custodian",
            custody_key_fingerprint="test-fingerprint-01",
            signature_metadata={"algorithm": "external-manual-attestation", "signed_at": "2026-07-14T23:00:00+09:00"},
            output_dir=root / "holdout-output",
        )
        assert_permissions_all_false(result)
        return root / "holdout-output" / "holdout-custody-commitment.json"

    def _assemble(self, open_receipt: Path, custody: Path, root: Path) -> Path:
        result = acquisition.assemble_acquisition_preflight(
            open_batch_receipt_path=open_receipt,
            holdout_custody_commitment_path=custody,
            output_dir=root / "final-output",
        )
        assert_permissions_all_false(result)
        return root / "final-output" / "acquisition-preflight-receipt.json"

    def _assert_open_scope_field_rejected(self, field: str, value: object) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            manifest_path = source["open_batch_root"] / "batches.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["batches"][0][field] = value
            write_json_with_sha256(manifest_path, manifest)
            output = root / "rejected-output"
            assertPreflight(
                "PREFLIGHT_SCOPE",
                lambda: acquisition.preflight_open_batches(
                    materials_receipt_path=common,
                    open_batch_root=source["open_batch_root"],
                    open_evidence_root=source["open_evidence_root"],
                    output_dir=output,
                ),
            )
            self.assertFalse(output.exists())

    def test_material_preflight_binds_valid_pilot_and_four_card_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            prerequisite = source["prerequisite"]
            output = root / "common-output"
            result = acquisition.preflight_pilot_materials(
                **prerequisite,
                shared_root=source["shared_root"],
                output_dir=output,
            )
            receipt_path = output / "common-material-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "common_materials_verified")
            self.assertEqual(result["state"], "COMMON_MATERIALS_VERIFIED")
            assert_permissions_all_false(result)
            self.assertEqual(receipt["state"], "COMMON_MATERIALS_VERIFIED")
            self.assertEqual(receipt["parent_bindings"]["pilot_design_receipt_sha256"], sha256_bytes(prerequisite["pilot_design_receipt_path"].read_bytes()))
            self.assertEqual(receipt["parent_bindings"]["diagnostic_receipt_sha256"], sha256_bytes(prerequisite["diagnostic_receipt_path"].read_bytes()))
            self.assertEqual(receipt["parent_bindings"]["design_artifact_sha256"], sha256_bytes(prerequisite["design_path"].read_bytes()))
            self.assertEqual(receipt["parent_bindings"]["registry_artifact_sha256"], sha256_bytes(prerequisite["registry_path"].read_bytes()))
            assert_sidecar_matches(receipt_path)

    def test_material_preflight_rejects_changed_parent_design_after_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            prerequisite = source["prerequisite"]
            design_path = prerequisite["design_path"]
            design_path.write_text(design_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            output = root / "changed-parent-output"

            assertPreflight(
                "PILOT_DESIGN_RECEIPT",
                lambda: acquisition.preflight_pilot_materials(
                    **prerequisite,
                    shared_root=source["shared_root"],
                    output_dir=output,
                ),
            )
            self.assertFalse(output.exists())

    def test_open_preflight_accepts_exactly_fifteen_train_and_two_validation_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            receipt = json.loads(open_receipt.read_text(encoding="utf-8"))

            self.assertEqual(receipt["status"], "open_batch_preflight_verified")
            self.assertEqual(receipt["state"], "OPEN_BATCH_PREFLIGHT_VERIFIED")
            self.assertEqual(receipt["open_counts"]["train"]["families"], 15)
            self.assertEqual(receipt["open_counts"]["train"]["batches"], 15)
            self.assertEqual(receipt["open_counts"]["validation"]["families"], 2)
            self.assertEqual(receipt["open_counts"]["validation"]["batches"], 2)
            self.assertEqual(receipt["open_counts"]["total"]["families"], 17)
            self.assertEqual(receipt["open_counts"]["total"]["batches"], 17)
            assert_permissions_all_false(receipt)
            assert_sidecar_matches(open_receipt)

    def test_sealed_commitment_accepts_exactly_three_holdout_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            commitment_path = self._commit_holdout(source, common, open_receipt, root)
            commitment = json.loads(commitment_path.read_text(encoding="utf-8"))

            self.assertEqual(commitment["status"], "holdout_custody_committed")
            self.assertEqual(commitment["state"], "HOLDOUT_CUSTODY_COMMITTED")
            self.assertEqual(commitment["counts"]["families"], 3)
            self.assertEqual(commitment["counts"]["batches"], 3)
            assert_permissions_all_false(commitment)
            assert_sidecar_matches(commitment_path)

    def test_open_preflight_materializes_exact_train_and_validation_card_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            cards = json.loads(open_receipt.read_text(encoding="utf-8"))["card_skeleton"]
            expected_slots = {("black", "POS01"), ("black", "POS02"), ("black", "POS03"), ("white", "POS01"), ("white", "POS02"), ("white", "POS03")}
            expected_train_cards = {
                f"CARD-{family.removeprefix('FAM-')}-{band}-001"
                for family in TRAIN_FAMILIES
                for band in ("DFT-L", "DFT-H")
            }
            expected_validation_cards = {
                f"CARD-{family.removeprefix('FAM-')}-{band}-001"
                for family in VALIDATION_FAMILIES
                for band in ("DFT-L", "DFT-M", "DFT-H")
            }

            self.assertEqual(len(cards), 36)
            self.assertEqual(
                {card["card_id"] for card in cards if card["split"] == "train"},
                expected_train_cards,
            )
            self.assertEqual(
                {card["card_id"] for card in cards if card["split"] == "validation"},
                expected_validation_cards,
            )
            self.assertEqual(sum(len(card["primary_reading_slots"]) for card in cards if card["split"] == "train"), 180)
            self.assertEqual(sum(len(card["primary_reading_slots"]) for card in cards if card["split"] == "validation"), 36)
            for card in cards:
                self.assertEqual(
                    {(slot["backing"], slot["reposition_id"]) for slot in card["primary_reading_slots"]},
                    expected_slots,
                )

    def test_sealed_commitment_materializes_exact_holdout_card_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            commitment_path = self._commit_holdout(source, common, open_receipt, root)
            commitment = json.loads(commitment_path.read_text(encoding="utf-8"))

            self.assertEqual(set(HOLDOUT_FAMILIES), {"FAM-HO-MIX-01", "FAM-HO-MIX-02", "FAM-HO-MIX-03"})
            self.assertEqual(commitment["counts"]["cards"], 9)
            self.assertEqual(commitment["counts"]["primary_reading_slots"], 54)
            self.assertNotIn("card_skeleton", commitment)

    def test_open_receipts_expose_no_holdout_raw_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            commitment_path = self._commit_holdout(source, common, open_receipt, root)
            final_receipt = self._assemble(open_receipt, commitment_path, root)

            for artifact in (common, open_receipt, commitment_path, final_receipt):
                _assert_no_public_holdout_raw(json.loads(artifact.read_text(encoding="utf-8")))

    def test_preflight_rejects_actual_dft_input_field(self) -> None:
        self._assert_open_scope_field_rejected("dft_um", "30")

    def test_reverification_succeeds_after_copying_shared_and_open_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            commitment_path = self._commit_holdout(source, common, open_receipt, root)
            final_receipt = self._assemble(open_receipt, commitment_path, root)
            copied_shared = copytree_without_rewriting(source["shared_root"])
            copied_open = copytree_without_rewriting(source["open_batch_root"])
            verified = acquisition.verify_acquisition_preflight(
                receipt_path=final_receipt,
                shared_root=copied_shared,
                open_root=copied_open,
            )

            self.assertEqual(verified["status"], "acquisition_preflight_verified")
            self.assertEqual(verified["state"], "ACQUISITION_PREFLIGHT_READY")
            self.assertTrue(verified["receipt_verified"])
            assert_permissions_all_false(verified)
            final_text = final_receipt.read_text(encoding="utf-8")
            self.assertNotIn(str(source["shared_root"]), final_text)
            self.assertNotIn(str(source["open_batch_root"]), final_text)

    def test_reverification_rejects_tampered_receipt_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            commitment_path = self._commit_holdout(source, common, open_receipt, root)
            final_receipt = self._assemble(open_receipt, commitment_path, root)
            final_receipt.write_text(final_receipt.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            assertPreflight(
                "RECEIPT_SIDECAR",
                lambda: acquisition.verify_acquisition_preflight(
                    receipt_path=final_receipt,
                    shared_root=source["shared_root"],
                    open_root=source["open_batch_root"],
                ),
            )

    def test_reverification_rejects_tampered_bound_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            commitment_path = self._commit_holdout(source, common, open_receipt, root)
            final_receipt = self._assemble(open_receipt, commitment_path, root)
            copied_shared = copytree_without_rewriting(source["shared_root"])
            copied_open = copytree_without_rewriting(source["open_batch_root"])
            property_path = copied_shared / "properties" / "base.json"
            property_payload = json.loads(property_path.read_text(encoding="utf-8"))
            property_payload["properties"]["nonvolatile_mass_fraction"]["value"] = "0.42"
            write_json_with_sha256(property_path, property_payload)

            assertPreflight(
                "RECEIPT_BINDING",
                lambda: acquisition.verify_acquisition_preflight(
                    receipt_path=final_receipt,
                    shared_root=copied_shared,
                    open_root=copied_open,
                ),
            )

    def test_rejects_spectrum_input_field_before_publication(self) -> None:
        self._assert_open_scope_field_rejected("spectrum_source", "raw/example.csv")

    def test_rejects_evaluation_input_field_before_publication(self) -> None:
        self._assert_open_scope_field_rejected("evaluation_score", "0.1")

    def test_rejects_candidate_input_field_before_publication(self) -> None:
        self._assert_open_scope_field_rejected("candidate_id", "CANDIDATE-01")

    def test_rejects_open_admission_input_field_before_publication(self) -> None:
        self._assert_open_scope_field_rejected("open_admission_permitted", True)

    def test_success_path_never_invokes_generic_dataset_fit_or_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            with (
                mock.patch("km_calibration.schema.load_and_validate_dataset", side_effect=AssertionError("dataset loader called")),
                mock.patch("km_calibration.pipeline.fit_km", side_effect=AssertionError("fit called")),
                mock.patch("km_calibration.pipeline.evaluate_model", side_effect=AssertionError("evaluator called")),
            ):
                common = self._common(source, root)
                self._open(source, common, root)

    def test_sealed_commitment_rejects_weighing_event_reused_from_open_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            open_receipt = self._open(source, common, root)
            open_evidence = next((source["open_evidence_root"] / "weighings").glob("*.json"))
            open_event_id = json.loads(open_evidence.read_text(encoding="utf-8"))["entries"][0]["weighing_event_id"]
            sealed_evidence = next((source["sealed_evidence_root"] / "weighings").glob("*.json"))
            sealed_payload = json.loads(sealed_evidence.read_text(encoding="utf-8"))
            sealed_payload["entries"][0]["weighing_event_id"] = open_event_id
            write_json_with_sha256(sealed_evidence, sealed_payload)
            output = root / "reused-event-holdout-output"

            assertPreflight(
                "CROSS_SPLIT_ID",
                lambda: acquisition.commit_holdout_custody(
                    materials_receipt_path=common,
                    open_batch_receipt_path=open_receipt,
                    sealed_holdout_batch_root=source["sealed_batch_root"],
                    sealed_evidence_root=source["sealed_evidence_root"],
                    custody_identity="independent sealed custodian",
                    custody_key_fingerprint="test-fingerprint-01",
                    signature_metadata={"algorithm": "external-manual-attestation"},
                    output_dir=output,
                ),
            )
            self.assertFalse(output.exists())

    def test_open_command_rejects_the_sealed_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            output = root / "bad-open-output"

            assertPreflight(
                "OPEN_ROOT_SCOPE",
                lambda: acquisition.preflight_open_batches(
                    materials_receipt_path=common,
                    open_batch_root=source["sealed_batch_root"],
                    open_evidence_root=source["sealed_evidence_root"],
                    output_dir=output,
                ),
            )
            self.assertFalse(output.exists())

    def test_rejects_reuse_of_nonempty_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            prerequisite = source["prerequisite"]
            output = root / "common-output"
            acquisition.preflight_pilot_materials(**prerequisite, shared_root=source["shared_root"], output_dir=output)
            before = {
                path.relative_to(output).as_posix(): path.read_bytes()
                for path in output.rglob("*")
                if path.is_file()
            }

            assertPreflight(
                "OUTPUT_DIR_NOT_EMPTY",
                lambda: acquisition.preflight_pilot_materials(
                    **prerequisite,
                    shared_root=source["shared_root"],
                    output_dir=output,
                ),
            )
            after = {
                path.relative_to(output).as_posix(): path.read_bytes()
                for path in output.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_removes_staging_and_publishes_nothing_after_injected_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._common(source, root)
            output = root / "write-failure-output"
            original = acquisition.write_json_with_sha256
            calls = 0

            def fail_second(path: Path, value: object) -> str:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second-write failure")
                return original(path, value)

            with mock.patch.object(acquisition, "write_json_with_sha256", side_effect=fail_second):
                assertPreflight(
                    "OUTPUT_WRITE",
                    lambda: acquisition.preflight_open_batches(
                        materials_receipt_path=common,
                        open_batch_root=source["open_batch_root"],
                        open_evidence_root=source["open_evidence_root"],
                        output_dir=output,
                    ),
                )
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(f".{output.name}.staging-*")), [])


if __name__ == "__main__":
    unittest.main()
