"""Synthetic transport tests for the receipt-derived open measurement pack."""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = CALIBRATION_ROOT / "tests"
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from km_calibration.open_measurement_admission import admit_open_measurements
from km_calibration.open_measurement_pack import (
    OpenMeasurementPackError,
    assemble_open_measurement_input,
    prepare_open_measurement_pack,
)
from open_measurement_admission_fixtures import write_valid_open_measurement_fixture


class OpenMeasurementPackTests(unittest.TestCase):
    def _prepare_completed_operator_files(self, root: Path) -> dict[str, object]:
        source = write_valid_open_measurement_fixture(root / "fixture")
        pack_root = root / "pack"
        prepared = prepare_open_measurement_pack(
            acquisition_receipt_path=source["acquisition_receipt"],
            shared_root=source["shared_root"],
            open_root=source["open_root"],
            output_dir=pack_root,
        )
        self.assertEqual(prepared["cards"], 36)
        self.assertEqual(prepared["coated_readings"], 216)
        self.assertFalse(prepared["production_pass"])

        operator = root / "operator"
        operator.mkdir()
        for template in (pack_root / "operator-input").iterdir():
            shutil.copyfile(template, operator / template.name.replace(".template", ""))

        payload = source["payload"]
        assert isinstance(payload, dict)
        conditions = payload["locked_conditions"]
        assert isinstance(conditions, dict)
        profile = {
            "schema_version": "moocow-open-measurement-operator-profile-v1",
            "measurement_session_id": payload["measurement_session_id"],
            "instrument_id": conditions["instrument_id"],
            "fixture_protocol_id": conditions["fixture_protocol_id"],
            "instrument_calibration_evidence_relative_path": conditions["instrument_calibration_evidence"]["relative_path"],
            "instrument_run_log_evidence_relative_path": conditions["instrument_run_log_evidence"]["relative_path"],
        }
        (operator / "measurement-profile.json").write_text(json.dumps(profile), encoding="utf-8")

        backings = payload["backings"]
        assert isinstance(backings, dict)
        self._write_csv(
            operator / "backings.csv",
            ("backing", "backing_id", "lot_id"),
            ((backing, backings[backing]["backing_id"], backings[backing]["lot_id"]) for backing in ("black", "white")),
        )

        bare_rows = []
        spectra_rows = []
        for backing in ("black", "white"):
            for record in backings[backing]["bare_measurements"]:
                bare_rows.append((backing, record["reposition_id"], record["instrument_measurement_id"], record["measured_at_local"], record["raw_spectrum_evidence"]["relative_path"]))
                slot = f"bare:{backing}:{record['reposition_id']}"
                spectra_rows.extend((slot, wavelength, value) for wavelength, value in zip(payload["wavelength_nm"], record["reflectance"], strict=True))
        self._write_csv(operator / "bare-readings.csv", ("backing", "reposition_id", "instrument_measurement_id", "measured_at_local", "raw_spectrum_evidence_relative_path"), bare_rows)

        dft_rows = []
        cards = payload["cards"]
        assert isinstance(cards, list)
        for card in cards:
            for backing in ("black", "white"):
                record = card["dft_by_backing"][backing]
                dft_rows.append((card["card_id"], backing, record["dft_measurement_id"], record["measured_at_local"], record["dft_evidence"]["relative_path"], ";".join(str(value) for value in record["dft_points_um"])))
        self._write_csv(operator / "dft-readings.csv", ("card_id", "backing", "dft_measurement_id", "measured_at_local", "dft_evidence_relative_path", "dft_points_um"), dft_rows)

        coated_rows = []
        readings = payload["readings"]
        assert isinstance(readings, list)
        for record in readings:
            coated_rows.append((record["card_id"], record["backing"], record["reposition_id"], record["instrument_measurement_id"], record["position_note"], record["orientation"], record["measured_at_local"], record["raw_spectrum_evidence"]["relative_path"], record["surface_status"], record["model_applicability_status"]))
            slot = f"coated:{record['card_id']}:{record['backing']}:{record['reposition_id']}"
            spectra_rows.extend((slot, wavelength, value) for wavelength, value in zip(payload["wavelength_nm"], record["reflectance"], strict=True))
        self._write_csv(operator / "coated-readings.csv", ("card_id", "backing", "reposition_id", "instrument_measurement_id", "position_note", "orientation", "measured_at_local", "raw_spectrum_evidence_relative_path", "surface_status", "model_applicability_status"), coated_rows)
        self._write_csv(operator / "spectra-long.csv", ("measurement_slot_id", "wavelength_nm", "reflectance"), spectra_rows)
        return {**source, "pack_root": pack_root, "operator": operator}

    @staticmethod
    def _write_csv(path: Path, headers: tuple[str, ...], rows: object) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(headers)
            writer.writerows(rows)

    @staticmethod
    def _read_csv(path: Path) -> list[list[str]]:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.reader(handle))

    @staticmethod
    def _assemble(source: dict[str, object], relative: str = "assembled/open-measurements-input.json") -> dict[str, object]:
        return assemble_open_measurement_input(
            acquisition_receipt_path=source["acquisition_receipt"],
            shared_root=source["shared_root"],
            open_root=source["open_root"],
            pack_root=source["pack_root"],
            operator_input_dir=source["operator"],
            measurement_root=source["measurement_root"],
            output_relative_path=relative,
        )

    def test_pack_is_incomplete_and_assembled_input_passes_existing_admission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = self._prepare_completed_operator_files(Path(temporary))
            result = self._assemble(source)
            self.assertEqual(result["cards"], 36)
            self.assertEqual(result["spectra_identities"], 222)
            self.assertTrue((source["measurement_root"] / "assembled" / "open-measurements-input.json.sha256").is_file())
            admission = admit_open_measurements(
                acquisition_receipt_path=source["acquisition_receipt"],
                shared_root=source["shared_root"],
                open_root=source["open_root"],
                measurement_root=source["measurement_root"],
                admission_input_relative_path="assembled/open-measurements-input.json",
                output_dir=Path(temporary) / "admission-output",
            )
            self.assertEqual(admission["state"], "OPEN_SELECTION_DATASET_ADMITTED")
            self.assertFalse(admission["promotion_permitted"])

    def test_placeholder_roster_grid_and_evidence_fail_before_publication(self) -> None:
        scenarios = (
            ("placeholder", "measurement-profile.json", lambda rows: rows.update({"instrument_id": "REQUIRED_INSTRUMENT"}), "PLACEHOLDER"),
            ("roster", "bare-readings.csv", lambda rows: rows.pop(), "ROSTER"),
            ("grid", "spectra-long.csv", lambda rows: rows.__setitem__(1, (rows[1][0], 371, rows[1][2])), "WAVELENGTH_GRID"),
            ("evidence", "bare-readings.csv", lambda rows: rows.__setitem__(1, (rows[1][0], rows[1][1], rows[1][2], rows[1][3], "missing/evidence.bin")), "EVIDENCE_PATH"),
        )
        for name, filename, mutate, code in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                source = self._prepare_completed_operator_files(Path(temporary))
                path = source["operator"] / filename
                if filename.endswith(".json"):
                    value = json.loads(path.read_text(encoding="utf-8"))
                    mutate(value)
                    path.write_text(json.dumps(value), encoding="utf-8")
                else:
                    with path.open("r", encoding="utf-8", newline="") as handle:
                        rows = list(csv.reader(handle))
                    mutate(rows)
                    self._write_csv(path, tuple(rows[0]), rows[1:])
                output = source["measurement_root"] / "assembled" / "failed.json"
                with self.assertRaises(OpenMeasurementPackError) as captured:
                    self._assemble(source, "assembled/failed.json")
                self.assertEqual(captured.exception.code, code)
                self.assertFalse(output.exists())
                self.assertFalse(output.with_name("failed.json.sha256").exists())

    def test_static_intermediate_links_cannot_escape_output_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._prepare_completed_operator_files(root / "source")
            outside = root / "outside"
            outside.mkdir()

            measurement_alias = source["measurement_root"] / "alias"
            pack_alias = root / "pack-alias"
            try:
                os.symlink(outside, measurement_alias, target_is_directory=True)
                os.symlink(outside, pack_alias, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks unavailable: {error}")

            with self.assertRaises(OpenMeasurementPackError) as assembled:
                self._assemble(source, "alias/nested/escaped.json")
            self.assertEqual(assembled.exception.code, "OUTPUT_PATH")
            self.assertFalse((outside / "nested" / "escaped.json").exists())

            with self.assertRaises(OpenMeasurementPackError) as prepared:
                prepare_open_measurement_pack(
                    acquisition_receipt_path=source["acquisition_receipt"],
                    shared_root=source["shared_root"],
                    open_root=source["open_root"],
                    output_dir=pack_alias / "nested" / "pack",
                )
            self.assertEqual(prepared.exception.code, "OUTPUT_PATH")
            self.assertFalse((outside / "nested" / "pack" / "pack-manifest.json").exists())

    def test_instrument_export_names_are_allowed_and_remain_admissible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._prepare_completed_operator_files(root)
            bare_path = source["operator"] / "bare-readings.csv"
            rows = self._read_csv(bare_path)
            original = source["measurement_root"] / rows[1][4]
            export_relative = "raw/instrument-export.bin"
            export_path = source["measurement_root"] / export_relative
            export_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, export_path)
            rows[1][4] = export_relative
            self._write_csv(bare_path, tuple(rows[0]), rows[1:])

            assembled = self._assemble(source, "assembled/instrument-export.json")
            self.assertEqual(assembled["status"], "open_measurement_input_assembled")
            admitted = admit_open_measurements(
                acquisition_receipt_path=source["acquisition_receipt"],
                shared_root=source["shared_root"],
                open_root=source["open_root"],
                measurement_root=source["measurement_root"],
                admission_input_relative_path="assembled/instrument-export.json",
                output_dir=root / "export-admission",
            )
            self.assertEqual(admitted["state"], "OPEN_SELECTION_DATASET_ADMITTED")

    def test_duplicate_evidence_content_and_non_open_scope_fail_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._prepare_completed_operator_files(root)
            bare_path = source["operator"] / "bare-readings.csv"
            rows = self._read_csv(bare_path)
            duplicate_relative = "raw/duplicate-content.bin"
            duplicate = source["measurement_root"] / duplicate_relative
            duplicate.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source["measurement_root"] / rows[1][4], duplicate)
            rows[2][4] = duplicate_relative
            self._write_csv(bare_path, tuple(rows[0]), rows[1:])

            output = source["measurement_root"] / "assembled" / "duplicate.json"
            with self.assertRaises(OpenMeasurementPackError) as captured:
                self._assemble(source, "assembled/duplicate.json")
            self.assertEqual(captured.exception.code, "EVIDENCE_BINDING")
            self.assertFalse(output.exists())
            self.assertFalse(output.with_name("duplicate.json.sha256").exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._prepare_completed_operator_files(root)
            profile_path = source["operator"] / "measurement-profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["instrument_id"] = "custody-meter"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")

            with self.assertRaises(OpenMeasurementPackError) as captured:
                self._assemble(source, "assembled/scope-rejected.json")
            self.assertEqual(captured.exception.code, "PACK_BINDING")
            self.assertFalse((source["measurement_root"] / "assembled" / "scope-rejected.json").exists())

    def test_input_limits_tampering_duplicate_ids_and_output_suffix_fail_closed(self) -> None:
        scenarios = ("row-limit", "pack-tamper", "duplicate-id", "duplicate-header", "output-suffix")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self._prepare_completed_operator_files(root)
                relative = "assembled/failed.json"
                expected_code = "INPUT_LIMIT"

                if scenario == "row-limit":
                    path = source["operator"] / "backings.csv"
                    rows = self._read_csv(path)
                    rows.append(list(rows[1]))
                    self._write_csv(path, tuple(rows[0]), rows[1:])
                elif scenario == "pack-tamper":
                    path = source["pack_root"] / "operator-input" / "backings.template.csv"
                    path.write_bytes(path.read_bytes() + b"tampered\n")
                    expected_code = "PACK_BINDING"
                elif scenario == "duplicate-id":
                    path = source["operator"] / "bare-readings.csv"
                    rows = self._read_csv(path)
                    rows[2][2] = rows[1][2]
                    self._write_csv(path, tuple(rows[0]), rows[1:])
                    expected_code = "DUPLICATE_ID"
                elif scenario == "duplicate-header":
                    path = source["operator"] / "bare-readings.csv"
                    rows = self._read_csv(path)
                    rows[0][1] = rows[0][0]
                    self._write_csv(path, tuple(rows[0]), rows[1:])
                    expected_code = "CSV_SCHEMA"
                else:
                    relative = "assembled/not-json.txt"
                    expected_code = "OUTPUT_PATH"

                with self.assertRaises(OpenMeasurementPackError) as captured:
                    self._assemble(source, relative)
                self.assertEqual(captured.exception.code, expected_code)
                output = source["measurement_root"].joinpath(*relative.split("/"))
                self.assertFalse(output.exists())
                self.assertFalse(output.with_name(f"{output.name}.sha256").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
