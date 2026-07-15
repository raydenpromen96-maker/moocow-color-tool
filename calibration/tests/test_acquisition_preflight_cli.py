from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(CALIBRATION_ROOT / "tests"))

from acquisition_preflight_fixtures import (
    assert_permissions_all_false,
    assert_sidecar_matches,
    copytree_without_rewriting,
    run_cli,
    write_current_materials,
    write_frozen_pilot_prerequisite,
    write_pilot_batch_roots,
)
from km_calibration.hashing import canonical_json_bytes, sha256_bytes, write_json_with_sha256


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


class AcquisitionPreflightCliTests(unittest.TestCase):
    def _source(self, root: Path) -> dict[str, object]:
        prerequisite = write_frozen_pilot_prerequisite(root)
        shared_root, materials = write_current_materials(root, prerequisite)
        return {
            "prerequisite": prerequisite,
            "shared_root": shared_root,
            **write_pilot_batch_roots(root, shared_root, materials),
        }

    def _rank_deficient_source(self, root: Path) -> dict[str, object]:
        """Make one otherwise-valid tint contribution underflow only in float64 rank math."""
        prerequisite = write_frozen_pilot_prerequisite(root)
        shared_root, materials = write_current_materials(root, prerequisite)
        property_path = shared_root / "properties" / "Y83S.json"
        property_record = json.loads(property_path.read_text(encoding="utf-8"))
        property_record["properties"]["nonvolatile_mass_fraction"]["value"] = "0." + ("0" * 400) + "1"
        write_json_with_sha256(property_path, property_record)
        return {
            "prerequisite": prerequisite,
            "shared_root": shared_root,
            **write_pilot_batch_roots(root, shared_root, materials),
        }

    def _material_args(self, source: dict[str, object], output: Path) -> list[str]:
        parent = source["prerequisite"]
        return [
            "preflight-pilot-materials",
            "--pilot-design-receipt", str(parent["pilot_design_receipt_path"]),
            "--design", str(parent["design_path"]),
            "--registry", str(parent["registry_path"]),
            "--registry-evidence-root", str(parent["registry_evidence_root"]),
            "--diagnostic-receipt", str(parent["diagnostic_receipt_path"]),
            "--diagnostic-evidence-root", str(parent["diagnostic_evidence_root"]),
            "--shared-root", str(source["shared_root"]),
            "--output-dir", str(output),
        ]

    def _open_args(self, source: dict[str, object], materials_receipt: Path, output: Path) -> list[str]:
        return [
            "preflight-open-batches",
            "--materials-receipt", str(materials_receipt),
            "--open-batch-root", str(source["open_batch_root"]),
            "--open-evidence-root", str(source["open_evidence_root"]),
            "--output-dir", str(output),
        ]

    def _signature_metadata(self, root: Path) -> Path:
        metadata = root / "signature-metadata.json"
        metadata.write_text(
            json.dumps({"algorithm": "external-manual-attestation", "signed_at": "2026-07-14T23:00:00+09:00"}),
            encoding="utf-8",
        )
        return metadata

    def _commit_args(self, source: dict[str, object], common: Path, open_receipt: Path, metadata: Path, output: Path) -> list[str]:
        return [
            "commit-holdout-custody",
            "--materials-receipt", str(common),
            "--open-batch-receipt", str(open_receipt),
            "--sealed-holdout-batch-root", str(source["sealed_batch_root"]),
            "--sealed-evidence-root", str(source["sealed_evidence_root"]),
            "--custody-identity", "independent sealed custodian",
            "--custody-key-fingerprint", "test-fingerprint-01",
            "--signature-metadata", str(metadata),
            "--output-dir", str(output),
        ]

    def _assert_success_stdout(self, outcome: tuple[int, str, str]) -> dict[str, object]:
        code, stdout, stderr = outcome
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(
            stdout,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        )
        return payload

    def _assert_typed_failure(self, outcome: tuple[int, str, str], code: str) -> None:
        return_code, stdout, stderr = outcome
        self.assertEqual(return_code, 2)
        self.assertEqual(stdout, "")
        self.assertRegex(stderr, rf"^ERROR: \[{code}\] .+\n$")

    def _assert_artifacts(self, output: Path, expected: set[str]) -> None:
        self.assertEqual(
            {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()},
            expected,
        )

    def _material_receipt(self, source: dict[str, object], root: Path) -> Path:
        output = root / "common-cli"
        self._assert_success_stdout(run_cli(self._material_args(source, output)))
        return output / "common-material-receipt.json"

    def _open_receipt(self, source: dict[str, object], common: Path, root: Path) -> Path:
        output = root / "open-cli"
        self._assert_success_stdout(run_cli(self._open_args(source, common, output)))
        return output / "open-batch-preflight-receipt.json"

    def _commitment(self, source: dict[str, object], common: Path, open_receipt: Path, root: Path) -> Path:
        output = root / "holdout-cli"
        self._assert_success_stdout(
            run_cli(self._commit_args(source, common, open_receipt, self._signature_metadata(root), output))
        )
        return output / "holdout-custody-commitment.json"

    def _final_receipt(self, open_receipt: Path, commitment: Path, root: Path) -> Path:
        output = root / "final-cli"
        self._assert_success_stdout(
            run_cli(
                [
                    "assemble-acquisition-preflight",
                    "--open-batch-receipt", str(open_receipt),
                    "--holdout-custody-commitment", str(commitment),
                    "--output-dir", str(output),
                ]
            )
        )
        return output / "acquisition-preflight-receipt.json"

    def _rewrite_receipt_payload(self, path: Path, receipt: dict[str, object]) -> None:
        payload = dict(receipt)
        payload.pop("receipt_payload_sha256")
        receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(payload))
        write_json_with_sha256(path, receipt)

    def test_material_preflight_cli_emits_common_material_state_with_all_permissions_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            output = root / "common-cli"
            result = self._assert_success_stdout(run_cli(self._material_args(source, output)))
            receipt_path = output / "common-material-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "common_materials_verified")
            self.assertEqual(result["state"], "COMMON_MATERIALS_VERIFIED")
            self.assertEqual(result["common_material_receipt_sha256"], sha256_bytes(receipt_path.read_bytes()))
            assert_permissions_all_false(result)
            self.assertEqual(receipt["state"], "COMMON_MATERIALS_VERIFIED")
            assert_permissions_all_false(receipt)
            assert_sidecar_matches(receipt_path)
            self._assert_artifacts(output, {"common-material-receipt.json", "common-material-receipt.json.sha256"})
            _assert_no_public_holdout_raw(receipt)

    def test_open_preflight_cli_emits_rank_and_open_skeleton_without_holdout_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._material_receipt(source, root)
            output = root / "open-cli"
            result = self._assert_success_stdout(run_cli(self._open_args(source, common, output)))
            open_receipt = output / "open-batch-preflight-receipt.json"
            rank_receipt = output / "actual-nv-rank-receipt.json"
            open_value = json.loads(open_receipt.read_text(encoding="utf-8"))
            rank_value = json.loads(rank_receipt.read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "open_batch_preflight_verified")
            self.assertEqual(result["state"], "OPEN_BATCH_PREFLIGHT_VERIFIED")
            assert_permissions_all_false(result)
            self.assertEqual(open_value["open_counts"]["total"], {"families": 17, "batches": 17, "cards": 36, "primary_reading_slots": 216})
            self.assertEqual(rank_value["rank_method"]["numerical_rank"], 15)
            self.assertEqual(len(rank_value["rank_method"]["singular_values_float64_hex"]), 15)
            self.assertTrue(rank_value["rank_method"]["condition_number_is_finite"])
            self.assertNotIn("condition_number_threshold", rank_value["rank_method"])
            assert_permissions_all_false(open_value)
            assert_sidecar_matches(open_receipt)
            assert_sidecar_matches(rank_receipt)
            self._assert_artifacts(
                output,
                {
                    "actual-nv-rank-receipt.json",
                    "actual-nv-rank-receipt.json.sha256",
                    "open-batch-preflight-receipt.json",
                    "open-batch-preflight-receipt.json.sha256",
                },
            )
            _assert_no_public_holdout_raw(open_value)

    def test_sealed_commitment_cli_emits_only_public_custody_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._material_receipt(source, root)
            open_receipt = self._open_receipt(source, common, root)
            output = root / "holdout-cli"
            result = self._assert_success_stdout(
                run_cli(self._commit_args(source, common, open_receipt, self._signature_metadata(root), output))
            )
            commitment_path = output / "holdout-custody-commitment.json"
            commitment = json.loads(commitment_path.read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "holdout_custody_committed")
            self.assertEqual(result["state"], "HOLDOUT_CUSTODY_COMMITTED")
            assert_permissions_all_false(result)
            self.assertEqual(commitment["counts"], {"families": 3, "batches": 3, "cards": 9, "primary_reading_slots": 54})
            self.assertIn("open_batch_preflight_receipt_sha256", commitment)
            self.assertNotIn("batches", commitment)
            assert_permissions_all_false(commitment)
            assert_sidecar_matches(commitment_path)
            self._assert_artifacts(output, {"holdout-custody-commitment.json", "holdout-custody-commitment.json.sha256"})
            _assert_no_public_holdout_raw(commitment)

    def test_assembly_and_verify_cli_preserve_acquisition_ready_without_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._material_receipt(source, root)
            open_receipt = self._open_receipt(source, common, root)
            commitment = self._commitment(source, common, open_receipt, root)
            final_output = root / "final-cli"
            assembled = self._assert_success_stdout(
                run_cli(
                    [
                        "assemble-acquisition-preflight",
                        "--open-batch-receipt", str(open_receipt),
                        "--holdout-custody-commitment", str(commitment),
                        "--output-dir", str(final_output),
                    ]
                )
            )
            final_receipt = final_output / "acquisition-preflight-receipt.json"
            copied_shared = copytree_without_rewriting(source["shared_root"])
            copied_open = copytree_without_rewriting(source["open_batch_root"])
            verified = self._assert_success_stdout(
                run_cli(
                    [
                        "verify-acquisition-preflight",
                        "--receipt", str(final_receipt),
                        "--shared-root", str(copied_shared),
                        "--open-root", str(copied_open),
                    ]
                )
            )
            final_value = json.loads(final_receipt.read_text(encoding="utf-8"))

            self.assertEqual(assembled["status"], "acquisition_preflight_ready")
            self.assertEqual(assembled["state"], "ACQUISITION_PREFLIGHT_READY")
            assert_permissions_all_false(assembled)
            self.assertEqual(verified["status"], "acquisition_preflight_verified")
            self.assertEqual(verified["state"], "ACQUISITION_PREFLIGHT_READY")
            self.assertTrue(verified["receipt_verified"])
            assert_permissions_all_false(verified)
            assert_permissions_all_false(final_value)
            assert_sidecar_matches(final_receipt)
            self._assert_artifacts(output=final_output, expected={"acquisition-preflight-receipt.json", "acquisition-preflight-receipt.json.sha256"})
            _assert_no_public_holdout_raw(final_value)

    def test_open_cli_rejects_undocumented_holdout_root_argument_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._material_receipt(source, root)
            output = root / "unknown-arg-output"
            code, stdout, stderr = run_cli(
                [
                    "preflight-open-batches",
                    "--materials-receipt", str(common),
                    "--open-batch-root", str(source["open_batch_root"]),
                    "--open-evidence-root", str(source["open_evidence_root"]),
                    "--sealed-holdout-root", str(source["sealed_batch_root"]),
                    "--output-dir", str(output),
                ]
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("usage:", stderr)
            self.assertIn("unrecognized arguments", stderr)
            self.assertFalse(output.exists())

    def test_open_cli_rejects_sealed_root_as_open_batch_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._material_receipt(source, root)
            output = root / "sealed-as-open-output"
            code, stdout, stderr = run_cli(
                [
                    "preflight-open-batches",
                    "--materials-receipt", str(common),
                    "--open-batch-root", str(source["sealed_batch_root"]),
                    "--open-evidence-root", str(source["sealed_evidence_root"]),
                    "--output-dir", str(output),
                ]
            )

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertRegex(stderr, r"^ERROR: \[OPEN_ROOT_SCOPE\] .+\n$")
            self.assertFalse(output.exists())

    def test_material_cli_reports_pilot_design_receipt_error_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            design_path = source["prerequisite"]["design_path"]
            design_path.write_text(design_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            output = root / "bad-material-output"

            self._assert_typed_failure(run_cli(self._material_args(source, output)), "PILOT_DESIGN_RECEIPT")
            self.assertFalse(output.exists())

    def test_open_cli_reports_rank_deficient_error_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._rank_deficient_source(root)
            common = self._material_receipt(source, root)
            output = root / "rank-deficient-output"

            self._assert_typed_failure(run_cli(self._open_args(source, common, output)), "RANK_DEFICIENT")
            self.assertFalse(output.exists())

    def test_sealed_cli_reports_holdout_batch_count_error_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._material_receipt(source, root)
            open_receipt = self._open_receipt(source, common, root)
            manifest_path = source["sealed_batch_root"] / "batches.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["batches"].pop()
            write_json_with_sha256(manifest_path, manifest)
            output = root / "bad-holdout-output"

            self._assert_typed_failure(
                run_cli(self._commit_args(source, common, open_receipt, self._signature_metadata(root), output)),
                "HOLDOUT_BATCH_COUNT",
            )
            self.assertFalse(output.exists())

    def test_assembly_cli_reports_open_holdout_leakage_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._material_receipt(source, root)
            open_receipt = self._open_receipt(source, common, root)
            commitment = self._commitment(source, common, open_receipt, root)
            open_payload = json.loads(open_receipt.read_text(encoding="utf-8"))
            open_payload["holdout_actual_wet_mass"] = "1"
            self._rewrite_receipt_payload(open_receipt, open_payload)
            output = root / "leaked-assembly-output"

            self._assert_typed_failure(
                run_cli(
                    [
                        "assemble-acquisition-preflight",
                        "--open-batch-receipt", str(open_receipt),
                        "--holdout-custody-commitment", str(commitment),
                        "--output-dir", str(output),
                    ]
                ),
                "OPEN_HOLDOUT_LEAKAGE",
            )
            self.assertFalse(output.exists())

    def test_verify_cli_reports_receipt_binding_error_after_copied_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            common = self._material_receipt(source, root)
            open_receipt = self._open_receipt(source, common, root)
            commitment = self._commitment(source, common, open_receipt, root)
            final_receipt = self._final_receipt(open_receipt, commitment, root)
            copied_shared = copytree_without_rewriting(source["shared_root"])
            copied_open = copytree_without_rewriting(source["open_batch_root"])
            property_path = copied_shared / "properties" / "base.json"
            property_payload = json.loads(property_path.read_text(encoding="utf-8"))
            property_payload["properties"]["nonvolatile_mass_fraction"]["value"] = "0.42"
            write_json_with_sha256(property_path, property_payload)

            self._assert_typed_failure(
                run_cli(
                    [
                        "verify-acquisition-preflight",
                        "--receipt", str(final_receipt),
                        "--shared-root", str(copied_shared),
                        "--open-root", str(copied_open),
                    ]
                ),
                "RECEIPT_BINDING",
            )


if __name__ == "__main__":
    unittest.main()
