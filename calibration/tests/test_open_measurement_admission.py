from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(CALIBRATION_ROOT / "tests"))

from acquisition_preflight_fixtures import (
    assert_permissions_all_false,
    assert_sidecar_matches,
    run_cli,
)
from km_calibration.errors import CalibrationError, DatasetValidationError
from km_calibration.hashing import canonical_json_bytes, sha256_bytes, sha256_file, write_json_with_sha256
from km_calibration.open_measurement_admission import (
    OpenMeasurementAdmissionError,
    admit_open_measurements,
    load_and_validate_open_selection_dataset,
    verify_open_measurement_admission,
)
from km_calibration.schema import load_and_validate_dataset
from open_measurement_admission_fixtures import (
    copytree_without_rewriting,
    rehash_published_admission,
    rewrite_admission_input,
    write_valid_open_measurement_fixture,
)


_PERMISSION_KEYS = {
    "pilot_acquisition_permitted",
    "open_admission_permitted",
    "model_fitting_permitted",
    "holdout_release_permitted",
    "physical_ranking_enabled",
    "promotion_permitted",
}
_OUTPUT_FILES = {
    "manifest.json",
    "manifest.json.sha256",
    "sources/open-measurements.json",
    "sources/open-measurements.json.sha256",
    "admission-receipt.json",
    "admission-receipt.json.sha256",
}


def _assert_open_only(value: object, path: str = "$") -> None:
    """Reject every sealed/holdout lexical leak except the required false bit."""

    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).casefold()
            if key != "holdout_release_permitted" and ("holdout" in lowered or "sealed" in lowered):
                raise AssertionError(f"prohibited open-only key at {path}.{key}")
            _assert_open_only(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_open_only(child, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.casefold()
        if "fam-ho-" in lowered or "sealed-holdout" in lowered:
            raise AssertionError(f"prohibited open-only value at {path}")


def _assert_portable_paths(value: object, path: str = "$") -> None:
    """Output artifacts may bind logical paths, never caller-specific source paths."""

    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"path", "relative_path"}:
                if not isinstance(child, str):
                    raise AssertionError(f"non-text logical path at {path}.{key}")
                logical = PurePosixPath(child)
                if child != logical.as_posix() or logical.is_absolute() or ".." in logical.parts or "\\" in child:
                    raise AssertionError(f"non-portable path at {path}.{key}: {child!r}")
            _assert_portable_paths(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_portable_paths(child, f"{path}[{index}]")


class OpenMeasurementAdmissionTests(unittest.TestCase):
    def _source(self, root: Path) -> dict[str, object]:
        return write_valid_open_measurement_fixture(root)

    def _admit(self, source: dict[str, object], output: Path) -> dict[str, object]:
        return admit_open_measurements(
            acquisition_receipt_path=source["acquisition_receipt"],
            shared_root=source["shared_root"],
            open_root=source["open_root"],
            measurement_root=source["measurement_root"],
            admission_input_relative_path=source["admission_input_relative_path"],
            output_dir=output,
        )

    def _published(self, root: Path) -> tuple[dict[str, object], Path]:
        source = self._source(root)
        output = root / "admitted"
        self._admit(source, output)
        return source, output

    def _verify(
        self,
        source: dict[str, object],
        output: Path,
        *,
        acquisition_receipt_path: Path | None = None,
    ) -> dict[str, object]:
        return verify_open_measurement_admission(
            acquisition_receipt_path=acquisition_receipt_path or source["acquisition_receipt"],
            admission_receipt_path=output / "admission-receipt.json",
            dataset_root=output,
            shared_root=source["shared_root"],
            open_root=source["open_root"],
            measurement_root=source["measurement_root"],
        )

    def _assert_rehashed_chain(self, output: Path, forged: dict[str, object]) -> None:
        manifest_path = output / "manifest.json"
        source_path = output / "sources" / "open-measurements.json"
        receipt_path = output / "admission-receipt.json"
        for path in (manifest_path, source_path, receipt_path):
            assert_sidecar_matches(path)
        self.assertEqual(forged["source_sha256"], sha256_file(source_path))
        self.assertEqual(forged["manifest_sha256"], sha256_file(manifest_path))
        self.assertEqual(forged["receipt_sha256"], sha256_file(receipt_path))
        manifest = forged["manifest"]
        receipt = forged["receipt"]
        self.assertEqual(manifest["source_files"][0]["sha256"], forged["source_sha256"])
        self.assertEqual(receipt["bindings"]["open_measurements"]["sha256"], forged["source_sha256"])
        self.assertEqual(receipt["bindings"]["dataset_manifest"]["sha256"], forged["manifest_sha256"])
        receipt_payload = dict(receipt)
        payload_sha256 = receipt_payload.pop("receipt_payload_sha256")
        self.assertEqual(payload_sha256, sha256_bytes(canonical_json_bytes(receipt_payload)))
        assert_permissions_all_false(manifest)
        assert_permissions_all_false(receipt)

    def _assert_rehashed_output_rejected(
        self,
        source: dict[str, object],
        output: Path,
        mutation: object,
    ) -> OpenMeasurementAdmissionError:
        forged = rehash_published_admission(output, mutation)  # type: ignore[arg-type]
        self._assert_rehashed_chain(output, forged)
        with self.assertRaises(OpenMeasurementAdmissionError) as captured:
            self._verify(source, output)
        return captured.exception

    def _assert_no_publication(self, output: Path) -> None:
        self.assertFalse(output.exists())
        self.assertEqual(list(output.parent.glob(f".{output.name}.staging-*")), [])

    def _assert_admission_failure(self, output: Path, callback: object) -> OpenMeasurementAdmissionError:
        with self.assertRaises(OpenMeasurementAdmissionError) as captured:
            callback()  # type: ignore[operator]
        self._assert_no_publication(output)
        return captured.exception

    def _assert_artifacts(self, output: Path, result: dict[str, object]) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        self.assertEqual(
            {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()},
            _OUTPUT_FILES,
        )
        manifest_path = output / "manifest.json"
        source_path = output / "sources" / "open-measurements.json"
        receipt_path = output / "admission-receipt.json"
        for path in (manifest_path, source_path, receipt_path):
            assert_sidecar_matches(path)

        self.assertEqual(
            set(result),
            {
                "status",
                "state",
                "dataset_manifest_sha256",
                "open_measurements_sha256",
                "admission_receipt_sha256",
                "cards",
                "readings",
                "bare_backing_measurements",
                "output_dir",
                *_PERMISSION_KEYS,
            },
        )
        self.assertEqual(result["status"], "open_measurements_admitted")
        self.assertEqual(result["state"], "OPEN_SELECTION_DATASET_ADMITTED")
        self.assertEqual(result["dataset_manifest_sha256"], sha256_file(manifest_path))
        self.assertEqual(result["open_measurements_sha256"], sha256_file(source_path))
        self.assertEqual(result["admission_receipt_sha256"], sha256_file(receipt_path))
        self.assertEqual(result["cards"], 36)
        self.assertEqual(result["readings"], 216)
        self.assertEqual(result["bare_backing_measurements"], {"black": 3, "white": 3})
        assert_permissions_all_false(result)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source = json.loads(source_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest),
            {
                "schema_version",
                "dataset_status",
                "production_pass",
                *_PERMISSION_KEYS,
                "concentration_basis",
                "wavelength_nm",
                "locked_conditions",
                "saunderson",
                "components",
                "backings",
                "splits",
                "counts",
                "predecessor",
                "source_files",
            },
        )
        self.assertEqual(manifest["schema_version"], "moocow-open-selection-dataset-v1")
        self.assertEqual(manifest["dataset_status"], "open_selection_only")
        self.assertIs(manifest["production_pass"], False)
        self.assertEqual(manifest["saunderson"], {"mode": "off"})
        self.assertEqual(manifest["counts"], {"cards": 36, "coated_readings": 216, "bare_backing_measurements": {"black": 3, "white": 3}})
        self.assertEqual(set(manifest["splits"]), {"train", "validation"})
        self.assertEqual(len(manifest["splits"]["train"]), 30)
        self.assertEqual(len(manifest["splits"]["validation"]), 6)
        assert_permissions_all_false(manifest)

        self.assertEqual(
            set(source),
            {
                "schema_version",
                "dataset_status",
                "wavelength_nm",
                "locked_conditions",
                "measurement_session_id",
                "cards",
                "measurements",
                "evidence_bindings",
            },
        )
        self.assertEqual(source["schema_version"], "moocow-open-measurements-source-v1")
        self.assertEqual(source["dataset_status"], "open_selection_only")
        self.assertEqual(len(source["cards"]), 36)
        self.assertEqual(len(source["measurements"]), 216)
        self.assertEqual(
            set(source["evidence_bindings"]),
            {"admission_input", "instrument_calibration", "instrument_run_log", "bare_spectra", "dft_records", "coated_spectra"},
        )
        self.assertEqual(len(source["evidence_bindings"]["bare_spectra"]), 6)
        self.assertEqual(len(source["evidence_bindings"]["dft_records"]), 72)
        self.assertEqual(len(source["evidence_bindings"]["coated_spectra"]), 216)

        self.assertEqual(
            set(receipt),
            {
                "schema_version",
                "status",
                "state",
                "production_pass",
                *_PERMISSION_KEYS,
                "bindings",
                "counts",
                "receipt_payload_sha256",
            },
        )
        self.assertEqual(receipt["schema_version"], "moocow-open-measurement-admission-receipt-v1")
        self.assertEqual(receipt["status"], "open_measurements_admitted")
        self.assertEqual(receipt["state"], "OPEN_SELECTION_DATASET_ADMITTED")
        self.assertIs(receipt["production_pass"], False)
        self.assertEqual(receipt["counts"], manifest["counts"])
        assert_permissions_all_false(receipt)
        _assert_open_only([manifest, source, receipt])
        _assert_portable_paths([manifest, source, receipt])
        return manifest, source, receipt

    def test_admits_full_open_roster_with_exact_nonpromotable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._admit(self._source(root), root / "admitted")

            self._assert_artifacts(root / "admitted", result)

    def test_cli_admits_and_portably_reverifies_copied_open_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            output = root / "admitted-cli"
            code, stdout, stderr = run_cli(
                [
                    "admit-open-measurements",
                    "--acquisition-receipt", str(source["acquisition_receipt"]),
                    "--shared-root", str(source["shared_root"]),
                    "--open-root", str(source["open_root"]),
                    "--measurement-root", str(source["measurement_root"]),
                    "--admission-input", str(source["admission_input_relative_path"]),
                    "--output-dir", str(output),
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            result = json.loads(stdout)
            self.assertEqual(stdout, json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
            self._assert_artifacts(output, result)

            copied_shared = copytree_without_rewriting(source["shared_root"], root / "copied-shared")
            copied_open = copytree_without_rewriting(source["open_root"], root / "copied-open")
            copied_measurements = copytree_without_rewriting(source["measurement_root"], root / "copied-measurements")
            copied_dataset = copytree_without_rewriting(output, root / "copied-admission")
            copied_acquisition = copytree_without_rewriting(
                source["acquisition_receipt"].parent,
                root / "copied-acquisition",
            ) / source["acquisition_receipt"].name
            verify_code, verify_stdout, verify_stderr = run_cli(
                [
                    "verify-open-measurement-admission",
                    "--acquisition-receipt", str(copied_acquisition),
                    "--admission-receipt", str(copied_dataset / "admission-receipt.json"),
                    "--dataset-root", str(copied_dataset),
                    "--shared-root", str(copied_shared),
                    "--open-root", str(copied_open),
                    "--measurement-root", str(copied_measurements),
                ]
            )
            self.assertEqual(verify_code, 0)
            self.assertEqual(verify_stderr, "")
            verified = json.loads(verify_stdout)
            self.assertTrue(verified["status"].endswith("_verified"))
            self.assertEqual(verified["state"], "OPEN_SELECTION_DATASET_ADMITTED")
            self.assertEqual(
                set(verified),
                {
                    "status",
                    "state",
                    "dataset_manifest_sha256",
                    "open_measurements_sha256",
                    "admission_receipt_sha256",
                    "cards",
                    "readings",
                    "bare_backing_measurements",
                    *_PERMISSION_KEYS,
                },
            )
            assert_permissions_all_false(verified)
            _assert_open_only(verified)

    def test_derives_formula_identity_and_composition_only_from_reverified_open_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            result = self._admit(source, root / "admitted")
            manifest, records, _receipt = self._assert_artifacts(root / "admitted", result)
            open_receipt = source["open_receipt"]
            self.assertIsInstance(open_receipt, dict)
            expected_cards = {card["card_id"]: card for card in open_receipt["card_skeleton"]}
            expected_batches = {batch["formula_batch_id"]: batch for batch in open_receipt["batches"]}
            expected_component_pairs = {
                (component["component_id"], component["physical_lot_id"])
                for batch in expected_batches.values()
                for component in batch["components"]
            }
            observed_component_pairs = {
                (component["component_id"], component["physical_lot_id"])
                for component in manifest["components"]
            }
            self.assertEqual(observed_component_pairs, expected_component_pairs)
            self.assertEqual(len(records["measurements"]), 216)
            for record in records["measurements"]:
                card = expected_cards[record["card_id"]]
                self.assertEqual(record["formula_family_id"], card["formula_family_id"])
                self.assertEqual(record["formula_id"], card["formula_id"])
                self.assertEqual(record["formula_batch_id"], card["formula_batch_id"])
                self.assertEqual(record["split"], card["split"])
                self.assertEqual(record["dft_band"], card["dft_band"])
                batch = expected_batches[card["formula_batch_id"]]
                expected_fractions = dict(zip(
                    [component["component_id"] for component in manifest["components"]],
                    [float(value) for value in batch["actual_nv_vector"]],
                    strict=True,
                ))
                self.assertAlmostEqual(
                    sum(component["nonvolatile_volume_fraction"] for component in record["components"]),
                    1.0,
                    places=12,
                )
                self.assertEqual(
                    [
                        (
                            component["component_id"],
                            component["physical_lot_id"],
                            component["nonvolatile_volume_fraction"],
                        )
                        for component in record["components"]
                    ],
                    [
                        (
                            component["component_id"],
                            component["physical_lot_id"],
                            expected_fractions[component["component_id"]],
                        )
                        for component in manifest["components"]
                    ],
                )

    def test_rejects_operator_supplied_formula_composition_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            rewrite_admission_input(
                source,
                lambda payload: payload["readings"][0].update({"formula_id": "operator-supplied-formula"}),
            )
            output = root / "forbidden-composition"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_missing_card_from_receipt_derived_roster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            rewrite_admission_input(source, lambda payload: payload["cards"].pop())
            output = root / "missing-card"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_unknown_card_and_missing_dft_backing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)

            def corrupt(payload: dict[str, object]) -> None:
                payload["cards"][0]["card_id"] = "FIXTURE-UNKNOWN-CARD"
                payload["cards"][1]["dft_by_backing"].pop("white")

            rewrite_admission_input(source, corrupt)
            output = root / "invalid-card-dft"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_reversed_receipt_dft_band_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)

            def reverse_dft(payload: dict[str, object]) -> None:
                cards = {card["card_id"]: card for card in payload["cards"]}
                first_train_family = next(
                    card["formula_family_id"]
                    for card in source["open_receipt"]["card_skeleton"]
                    if card["split"] == "train" and card["dft_band"] == "DFT-L"
                )
                high_card_id = next(
                    card["card_id"]
                    for card in source["open_receipt"]["card_skeleton"]
                    if card["formula_family_id"] == first_train_family and card["dft_band"] == "DFT-H"
                )
                cards[high_card_id]["dft_by_backing"]["black"]["dft_points_um"] = [0.1]

            rewrite_admission_input(source, reverse_dft)
            output = root / "reversed-dft-order"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_coated_reading_count_below_receipt_roster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            rewrite_admission_input(source, lambda payload: payload["readings"].pop())
            output = root / "missing-reading"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_duplicate_coated_slot_with_a_unique_measurement_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)

            def duplicate_slot(payload: dict[str, object]) -> None:
                duplicate = dict(payload["readings"][0])
                duplicate["instrument_measurement_id"] = "FIXTURE-COATED-DUPLICATE-ID"
                payload["readings"][-1] = duplicate

            rewrite_admission_input(source, duplicate_slot)
            output = root / "duplicate-slot"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_bare_backing_with_fewer_than_three_spectra(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            rewrite_admission_input(source, lambda payload: payload["backings"]["black"]["bare_measurements"].pop())
            output = root / "missing-bare"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_duplicate_instrument_id_across_bare_and_coated_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)

            def duplicate_identifier(payload: dict[str, object]) -> None:
                payload["backings"]["black"]["bare_measurements"][0]["instrument_measurement_id"] = payload["readings"][0]["instrument_measurement_id"]

            rewrite_admission_input(source, duplicate_identifier)
            output = root / "duplicate-instrument-id"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_dft_id_reused_from_bare_measurement_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)

            def duplicate_dft_identifier(payload: dict[str, object]) -> None:
                payload["cards"][0]["dft_by_backing"]["black"]["dft_measurement_id"] = payload["backings"]["black"]["bare_measurements"][0]["instrument_measurement_id"]

            rewrite_admission_input(source, duplicate_dft_identifier)
            output = root / "dft-id-reused-from-bare"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_dft_id_reused_from_coated_measurement_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)

            def duplicate_dft_identifier(payload: dict[str, object]) -> None:
                payload["cards"][0]["dft_by_backing"]["black"]["dft_measurement_id"] = payload["readings"][0]["instrument_measurement_id"]

            rewrite_admission_input(source, duplicate_dft_identifier)
            output = root / "dft-id-reused-from-coated"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_nonuniform_reflectance_grid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            rewrite_admission_input(source, lambda payload: payload["backings"]["white"]["bare_measurements"][0]["reflectance"].append(0.4))
            output = root / "invalid-grid"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_nonpositive_dft_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            rewrite_admission_input(source, lambda payload: payload["cards"][0]["dft_by_backing"]["black"].update({"dft_points_um": [0.0]}))
            output = root / "nonpositive-dft"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_byte_range_evidence_locator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            rewrite_admission_input(
                source,
                lambda payload: payload["readings"][0]["raw_spectrum_evidence"].update(
                    {"record_locator": {"kind": "byte_range", "byte_offset": 0, "byte_length": 1}}
                ),
            )
            output = root / "byte-range"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_traversal_locator_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            rewrite_admission_input(
                source,
                lambda payload: payload["readings"][0]["raw_spectrum_evidence"].update({"relative_path": "../outside.bin"}),
            )
            output = root / "traversal"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_hard_linked_evidence_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            evidence = source["payload"]["readings"][0]["raw_spectrum_evidence"]["relative_path"]
            original = source["measurement_root"] / evidence
            linked_target = source["measurement_root"] / "unsafe-hardlink-target.bin"
            linked_target.write_bytes(original.read_bytes())
            original.unlink()
            os.link(linked_target, original)
            output = root / "hard-link"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_symbolic_linked_evidence_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            evidence = source["payload"]["readings"][0]["raw_spectrum_evidence"]["relative_path"]
            original = source["measurement_root"] / evidence
            linked_target = source["measurement_root"] / "unsafe-symlink-target.bin"
            linked_target.write_bytes(original.read_bytes())
            original.unlink()
            os.symlink(linked_target, original)
            output = root / "symbolic-link"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_reused_evidence_across_dft_and_coated_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)

            def reuse(payload: dict[str, object]) -> None:
                payload["cards"][0]["dft_by_backing"]["black"]["dft_evidence"] = dict(payload["readings"][0]["raw_spectrum_evidence"])

            rewrite_admission_input(source, reuse)
            output = root / "reused-evidence"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_sealed_token_in_an_admission_input_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            rewrite_admission_input(source, lambda payload: payload["locked_conditions"].update({"fixture_protocol_id": "sealed-holdout"}))
            output = root / "leaked-token"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_rejects_a_sealed_root_in_the_open_root_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            source["open_root"] = source["sealed_root_for_rejection"]
            output = root / "sealed-open-root"

            self._assert_admission_failure(output, lambda: self._admit(source, output))

    def test_cli_rejects_unknown_holdout_or_sealed_flags_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for unknown_flag in ("--sealed-root", "--holdout-root"):
                output = root / unknown_flag.removeprefix("--")
                code, stdout, stderr = run_cli(
                    [
                        "admit-open-measurements",
                        "--acquisition-receipt", str(root / "receipt.json"),
                        "--shared-root", str(root),
                        "--open-root", str(root),
                        "--measurement-root", str(root),
                        "--admission-input", "admission/input.json",
                        "--output-dir", str(output),
                        unknown_flag, str(root),
                    ]
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("unrecognized arguments", stderr)
                self.assertFalse(output.exists())

    def test_rejects_nonempty_output_without_overwriting_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            output = root / "nonempty-output"
            output.mkdir()
            sentinel = output / "preserve.txt"
            sentinel.write_text("do not overwrite", encoding="utf-8")

            with self.assertRaises(OpenMeasurementAdmissionError):
                self._admit(source, output)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite")
            self.assertEqual(list(output.parent.glob(f".{output.name}.staging-*")), [])

    def test_verifier_rejects_semantically_tampered_permission_with_refreshed_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            output = root / "admitted"
            self._admit(source, output)
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["promotion_permitted"] = True
            write_json_with_sha256(manifest_path, manifest)

            with self.assertRaises(OpenMeasurementAdmissionError):
                self._verify(source, output)

    def test_verifier_rejects_fully_rehashed_reflectance_forgery_against_bound_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))

            def forge_reflectance(
                _manifest: dict[str, object],
                records: dict[str, object],
                _receipt: dict[str, object],
            ) -> None:
                measurement = records["measurements"][0]
                measurement["reflectance"] = [0.99 for _ in measurement["reflectance"]]

            self._assert_rehashed_output_rejected(source, output, forge_reflectance)

    def test_verifier_rejects_fully_rehashed_measurement_metadata_forgery_against_bound_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))

            def forge_metadata(
                _manifest: dict[str, object],
                records: dict[str, object],
                _receipt: dict[str, object],
            ) -> None:
                measurement = records["measurements"][0]
                measurement["position_note"] = "forged schema-valid position note"
                measurement["orientation"] = "forged-schema-valid-axis"
                measurement["measured_at_local"] = "2026-07-15T00:45:00+09:00"

            self._assert_rehashed_output_rejected(source, output, forge_metadata)

    def test_verifier_rejects_fully_rehashed_postpublication_dft_order_reversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))

            def reverse_dft_order(
                _manifest: dict[str, object],
                records: dict[str, object],
                _receipt: dict[str, object],
            ) -> None:
                cards = records["cards"]
                low_card = next(card for card in cards if card["split"] == "train" and card["dft_band"] == "DFT-L")
                high_card = next(
                    card
                    for card in cards
                    if card["formula_family_id"] == low_card["formula_family_id"] and card["dft_band"] == "DFT-H"
                )
                high_dft = high_card["dft_by_backing"]["black"]
                high_dft["dft_points_um"] = [0.1]
                high_dft["dft_um"] = 0.1
                for measurement in records["measurements"]:
                    if measurement["card_id"] == high_card["card_id"] and measurement["backing"] == "black":
                        measurement["dft_um"] = 0.1

            self._assert_rehashed_output_rejected(source, output, reverse_dft_order)

    def test_verifier_rejects_fully_rehashed_duplicate_coated_measurement_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))

            def duplicate_coated_id(
                _manifest: dict[str, object],
                records: dict[str, object],
                _receipt: dict[str, object],
            ) -> None:
                records["measurements"][1]["instrument_measurement_id"] = records["measurements"][0]["instrument_measurement_id"]

            self._assert_rehashed_output_rejected(source, output, duplicate_coated_id)

    def test_verifier_rejects_fully_rehashed_duplicate_bare_and_coated_measurement_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))

            def duplicate_bare_id(
                manifest: dict[str, object],
                records: dict[str, object],
                _receipt: dict[str, object],
            ) -> None:
                manifest["backings"]["black"]["bare_measurements"][0]["instrument_measurement_id"] = records["measurements"][0]["instrument_measurement_id"]

            self._assert_rehashed_output_rejected(source, output, duplicate_bare_id)

    def test_verifier_rejects_fully_rehashed_duplicate_dft_measurement_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))

            def duplicate_dft_id(
                _manifest: dict[str, object],
                records: dict[str, object],
                _receipt: dict[str, object],
            ) -> None:
                first = records["cards"][0]["dft_by_backing"]["black"]
                second = records["cards"][1]["dft_by_backing"]["black"]
                second["dft_measurement_id"] = first["dft_measurement_id"]

            self._assert_rehashed_output_rejected(source, output, duplicate_dft_id)

    def test_verifier_rejects_fully_rehashed_duplicate_evidence_bytes_under_a_new_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))

            def duplicate_evidence_bytes(
                _manifest: dict[str, object],
                records: dict[str, object],
                _receipt: dict[str, object],
            ) -> None:
                card = records["cards"][0]
                original_binding = copy.deepcopy(card["dft_by_backing"]["black"]["dft_evidence"])
                forged_binding = copy.deepcopy(original_binding)
                forged_binding["relative_path"] = "dft/duplicate-content-different-path.bin"
                original_path = source["measurement_root"].joinpath(*PurePosixPath(original_binding["relative_path"]).parts)
                forged_path = source["measurement_root"].joinpath(*PurePosixPath(forged_binding["relative_path"]).parts)
                forged_path.write_bytes(original_path.read_bytes())
                card["dft_by_backing"]["black"]["dft_evidence"] = forged_binding
                for measurement in records["measurements"]:
                    if measurement["card_id"] == card["card_id"] and measurement["backing"] == "black":
                        measurement["dft_evidence"] = copy.deepcopy(forged_binding)
                dft_bindings = records["evidence_bindings"]["dft_records"]
                binding_index = next(index for index, binding in enumerate(dft_bindings) if binding == original_binding)
                dft_bindings[binding_index] = copy.deepcopy(forged_binding)

            self._assert_rehashed_output_rejected(source, output, duplicate_evidence_bytes)

    def test_verifier_rejects_fully_rehashed_forged_predecessor_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))

            def forge_predecessor_sha(
                manifest: dict[str, object],
                _records: dict[str, object],
                receipt: dict[str, object],
            ) -> None:
                forged_sha256 = "0" * 64
                manifest["predecessor"]["acquisition_preflight_receipt_sha256"] = forged_sha256
                receipt["bindings"]["acquisition_preflight_receipt_sha256"] = forged_sha256

            self._assert_rehashed_output_rejected(source, output, forge_predecessor_sha)

    def test_verifier_rejects_extra_unbound_file_in_published_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))
            (output / "unexpected.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaises(OpenMeasurementAdmissionError):
                self._verify(source, output)

    def test_verifier_rejects_extra_unbound_directory_in_published_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output = self._published(Path(temporary))
            (output / "unexpected" / "nested").mkdir(parents=True)

            with self.assertRaises(OpenMeasurementAdmissionError):
                self._verify(source, output)

    def test_open_selection_schema_is_rejected_by_legacy_fit_evaluate_and_export_loaders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "admitted"
            self._admit(self._source(root), output)

            with self.assertRaises(DatasetValidationError):
                load_and_validate_dataset(output)
            dataset = load_and_validate_open_selection_dataset(output)
            self.assertEqual(type(dataset).__name__, "ValidatedOpenSelectionDataset")
            for command, artifact_flag, artifact_path in (
                ("fit-km", "--output-model", root / "model.json"),
                ("evaluate", "--output", root / "evaluation.json"),
                ("export-candidate", "--output-receipt", root / "candidate.json"),
            ):
                arguments = [command, "--dataset", str(output)]
                if command == "evaluate":
                    arguments.extend(["--model", str(root / "unused-model.json")])
                elif command == "export-candidate":
                    arguments.extend(["--model", str(root / "unused-model.json"), "--evaluation", str(root / "unused-evaluation.json")])
                arguments.extend([artifact_flag, str(artifact_path)])
                code, stdout, stderr = run_cli(arguments)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("ERROR:"))
                self.assertFalse(artifact_path.exists())

    def test_admission_succeeds_when_fitter_evaluator_exporter_and_runtime_entrypoints_are_poisoned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            output = root / "poison-guard"
            import km_calibration.pipeline as pipeline

            def forbidden(*_args: object, **_kwargs: object) -> object:
                raise AssertionError("open admission invoked a forbidden downstream entrypoint")

            with (
                mock.patch.object(pipeline, "fit_km", side_effect=forbidden),
                mock.patch.object(pipeline, "evaluate_model", side_effect=forbidden),
                mock.patch.object(pipeline, "export_candidate", side_effect=forbidden),
            ):
                result = self._admit(source, output)

            self._assert_artifacts(output, result)


if __name__ == "__main__":
    unittest.main()
