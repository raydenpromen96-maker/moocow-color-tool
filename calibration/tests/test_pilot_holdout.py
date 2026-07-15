from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(CALIBRATION_ROOT / "tests"))

from km_calibration.diagnostic import preflight_from_files
from km_calibration.errors import CalibrationError
from km_calibration.hashing import write_json_with_sha256
import km_calibration.pilot as pilot_module
from km_calibration.pilot import (
    BACKINGS,
    COMPONENT_ORDER,
    PILOT_CARD_ROSTER,
    POSITIONS,
    PilotValidationError,
    build_pilot_design_template,
    freeze_pilot_design,
    prepare_pilot,
    verify_pilot_design_receipt,
)
from test_diagnostic_import import _valid_payload


REGISTRY = CALIBRATION_ROOT / "protocols" / "current-batch-component-registry-v1.json"


def _component_code(component_id: str) -> str:
    return "base" if component_id == "base-waterborne-clear" else component_id.removeprefix("colorant-")


def _registry_evidence_root(path: Path) -> Path:
    return path.parent / f"{path.stem}-label-evidence"


def _write_verified_registry(path: Path) -> Path:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    evidence_root = _registry_evidence_root(path)
    labels = evidence_root / "labels"
    labels.mkdir(parents=True, exist_ok=True)
    for index, component in enumerate(registry["components"]):
        code = _component_code(component["component_id"])
        component["product_name"] = f"physical {code} material"
        component["manufacturer_or_supplier"] = "Example Coatings"
        component["batch_id"] = {"base": "LOT-BASE-01", "W064": "LOT-W064-01"}.get(code, f"LOT-{code}-01")
        component["lot_verification_status"] = "verified_physical_label"
        component["physical_label_verification_id"] = f"LABEL-CHECK-{index + 1:02d}"
        component["physical_label_verified_at"] = "2026-07-14T19:15:00+09:00"
        label_name = f"{component['component_id']}.txt"
        (labels / label_name).write_text(
            f"physical container label {component['component_id']} {component['batch_id']}\n",
            encoding="utf-8",
        )
        component["physical_label_evidence"] = {
            "relative_path": f"labels/{label_name}",
            "record_locator": {"kind": "whole_file"},
        }
        if "material_description" in component:
            component["material_description"] = "current physical base material"
    path.write_text(json.dumps(registry, sort_keys=True), encoding="utf-8")
    return path


def _write_preregistered_design(path: Path) -> Path:
    design = build_pilot_design_template()
    design.pop("template_status")
    design["pilot_design_status"] = "pilot_design_preregistered"
    design["dft_bands"] = {
        "DFT-L": {"target_um": 20.0, "acceptance_min_um": 15.0, "acceptance_max_um": 25.0},
        "DFT-M": {"target_um": 30.0, "acceptance_min_um": 26.0, "acceptance_max_um": 34.0},
        "DFT-H": {"target_um": 40.0, "acceptance_min_um": 36.0, "acceptance_max_um": 45.0},
    }
    for row in design["roster"]:
        row["formula_batch_id"] = f"BATCH-{row['formula_family_id'].removeprefix('FAM-')}"
        row["randomization_plan_id"] = "RANDOMIZATION-20260714-01"
    path.write_text(json.dumps(design, sort_keys=True), encoding="utf-8")
    return path


def _real_four_card_receipt(root: Path) -> tuple[Path, Path]:
    evidence_root = root / "diagnostic-evidence"
    input_path = root / "diagnostic-input.json"
    input_path.write_text(json.dumps(_valid_payload(evidence_root)), encoding="utf-8")
    output_dir = root / "four-card-output"
    preflight_from_files(
        input_format="json",
        input_path=input_path,
        evidence_root=evidence_root,
        output_dir=output_dir,
    )
    return output_dir / "preflight-receipt.json", evidence_root


class PilotHoldoutTests(unittest.TestCase):
    def assertPilot(self, code: str, callback: object) -> None:
        with self.assertRaises(PilotValidationError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)
        return captured.exception

    def test_fixed_roster_counts_and_prepared_template_are_explicitly_invalid(self) -> None:
        self.assertEqual(len(PILOT_CARD_ROSTER), 45)
        self.assertEqual(tuple(COMPONENT_ORDER), ("base", "Y83S", "Y74S", "B150S", "B153S", "R254D", "R101Y", "R101V", "Y42S", "073", "W064", "V23", "G7", "R122S", "BK7H"))
        self.assertEqual({split: sum(row["split"] == split for row in PILOT_CARD_ROSTER) for split in ("train", "validation", "holdout")}, {"train": 30, "validation": 6, "holdout": 9})
        self.assertEqual(len(PILOT_CARD_ROSTER) * len(BACKINGS) * len(POSITIONS), 270)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = root / "pack"
            result = prepare_pilot(REGISTRY, pack)
            self.assertEqual(result["open_primary_reading_slots"], 216)
            self.assertEqual(result["holdout_primary_reading_slots"], 54)
            self.assertEqual(result["primary_reading_slots"], 270)
            self.assertEqual(len((pack / "open-primary-reading-roster.csv").read_text(encoding="utf-8").splitlines()) - 1, 216)
            self.assertEqual(len((pack / "holdout-primary-reading-roster.csv").read_text(encoding="utf-8").splitlines()) - 1, 54)
            self.assertIn("故意保持无效", (pack / "README.zh-CN.md").read_text(encoding="utf-8"))
            self.assertIn("deliberately invalid", (pack / "README.en.md").read_text(encoding="utf-8"))
            copied_registry = json.loads(next((pack / "evidence" / "registry").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(copied_registry["components"][0]["physical_label_evidence"]["record_locator"], {"kind": "whole_file"})
            self.assertTrue((pack / "evidence" / "labels" / "README.md").is_file())

            receipt, evidence_root = _real_four_card_receipt(root)
            registry = _write_verified_registry(root / "registry.json")
            self.assertPilot(
                "PLACEHOLDER",
                lambda: freeze_pilot_design(
                    design_path=pack / "pilot-design.template.json",
                    registry_path=registry,
                    registry_evidence_root=_registry_evidence_root(registry),
                    diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root,
                    output_dir=root / "template-freeze",
                ),
            )
            self.assertFalse((root / "template-freeze").exists())

    def test_freeze_requires_real_four_card_receipt_and_verified_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            design = _write_preregistered_design(root / "design.json")
            registry = _write_verified_registry(root / "registry.json")
            with self.assertRaises(CalibrationError):
                freeze_pilot_design(
                    design_path=design,
                    registry_path=registry,
                    registry_evidence_root=_registry_evidence_root(registry),
                    diagnostic_receipt_path=root / "missing-receipt.json",
                    diagnostic_evidence_root=root / "missing-evidence",
                    output_dir=root / "missing-receipt-output",
                )
            self.assertFalse((root / "missing-receipt-output").exists())

            receipt, evidence_root = _real_four_card_receipt(root)
            invalid_registry = json.loads(registry.read_text(encoding="utf-8"))
            invalid_registry["components"][0]["lot_verification_status"] = "CATALOG_EVIDENCE_ONLY"
            registry.write_text(json.dumps(invalid_registry), encoding="utf-8")
            self.assertPilot(
                "REGISTRY_LOT_VERIFICATION",
                lambda: freeze_pilot_design(
                    design_path=design,
                    registry_path=registry,
                    registry_evidence_root=_registry_evidence_root(registry),
                    diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root,
                    output_dir=root / "registry-output",
                ),
            )
            self.assertFalse((root / "registry-output").exists())

    def test_wrong_lot_dft_roster_and_nonempty_output_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt, evidence_root = _real_four_card_receipt(root)
            design = _write_preregistered_design(root / "design.json")
            registry = _write_verified_registry(root / "registry.json")

            wrong_lot = json.loads(registry.read_text(encoding="utf-8"))
            next(item for item in wrong_lot["components"] if item["component_id"] == "colorant-W064")["batch_id"] = "OTHER-LOT"
            registry.write_text(json.dumps(wrong_lot), encoding="utf-8")
            self.assertPilot(
                "DIAGNOSTIC_LOT",
                lambda: freeze_pilot_design(
                    design_path=design,
                    registry_path=registry,
                    registry_evidence_root=_registry_evidence_root(registry),
                    diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root,
                    output_dir=root / "lot-output",
                ),
            )
            self.assertFalse((root / "lot-output").exists())

            registry = _write_verified_registry(root / "registry.json")
            dft = json.loads(design.read_text(encoding="utf-8"))
            dft["dft_bands"]["DFT-M"]["target_um"] = 0.0
            design.write_text(json.dumps(dft), encoding="utf-8")
            self.assertPilot(
                "NUMBER",
                lambda: freeze_pilot_design(
                    design_path=design,
                    registry_path=registry,
                    registry_evidence_root=_registry_evidence_root(registry),
                    diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root,
                    output_dir=root / "dft-output",
                ),
            )
            self.assertFalse((root / "dft-output").exists())

            design = _write_preregistered_design(root / "design.json")
            wrong_roster = json.loads(design.read_text(encoding="utf-8"))
            wrong_roster["roster"][0]["card_id"] = "CARD-NOT-PREREGISTERED"
            design.write_text(json.dumps(wrong_roster), encoding="utf-8")
            self.assertPilot(
                "ROSTER",
                lambda: freeze_pilot_design(
                    design_path=design,
                    registry_path=registry,
                    registry_evidence_root=_registry_evidence_root(registry),
                    diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root,
                    output_dir=root / "roster-output",
                ),
            )
            self.assertFalse((root / "roster-output").exists())

            nonempty = root / "nonempty-output"
            nonempty.mkdir()
            (nonempty / "prior.txt").write_text("do not overwrite", encoding="utf-8")
            self.assertPilot(
                "OUTPUT_DIR_NOT_EMPTY",
                lambda: freeze_pilot_design(
                    design_path=_write_preregistered_design(root / "fresh-design.json"),
                    registry_path=registry,
                    registry_evidence_root=_registry_evidence_root(registry),
                    diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root,
                    output_dir=nonempty,
                ),
            )
            self.assertEqual((nonempty / "prior.txt").read_text(encoding="utf-8"), "do not overwrite")

    def test_frozen_design_is_portable_and_receipt_tampering_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt, evidence_root = _real_four_card_receipt(root)
            design = _write_preregistered_design(root / "design.json")
            registry = _write_verified_registry(root / "registry.json")
            output = root / "pilot-freeze"
            result = freeze_pilot_design(
                design_path=design,
                registry_path=registry,
                registry_evidence_root=_registry_evidence_root(registry),
                diagnostic_receipt_path=receipt,
                diagnostic_evidence_root=evidence_root,
                output_dir=output,
            )
            self.assertEqual(result["status"], "pilot_roster_frozen")
            self.assertTrue(result["pilot_acquisition_permitted"])
            self.assertFalse(result["model_fitting_permitted"])
            self.assertFalse(result["holdout_release_permitted"])
            self.assertFalse(result["physical_ranking_enabled"])
            self.assertFalse(result["promotion_permitted"])

            frozen = json.loads((output / "pilot-design-receipt.json").read_text(encoding="utf-8"))
            label_bindings = frozen["bindings"]["registry"]["physical_label_evidence"]
            self.assertEqual(len(label_bindings), 15)
            self.assertEqual(len({binding["component_id"] for binding in label_bindings}), 15)
            self.assertEqual(len({binding["physical_label_verification_id"] for binding in label_bindings}), 15)
            self.assertEqual(len({binding["physical_label_evidence"]["relative_path"] for binding in label_bindings}), 15)
            self.assertEqual(len({binding["file_sha256"] for binding in label_bindings}), 15)

            copied_evidence = root / "copied-evidence"
            shutil.copytree(evidence_root, copied_evidence)
            copied_registry_evidence = root / "copied-registry-evidence"
            shutil.copytree(_registry_evidence_root(registry), copied_registry_evidence)
            verified = verify_pilot_design_receipt(
                receipt_path=output / "pilot-design-receipt.json",
                design_path=design,
                registry_path=registry,
                registry_evidence_root=copied_registry_evidence,
                diagnostic_receipt_path=receipt,
                diagnostic_evidence_root=copied_evidence,
            )
            self.assertTrue(verified["pilot_design_receipt_verified"])

            copied_label = copied_registry_evidence / "labels" / "base-waterborne-clear.txt"
            copied_label.write_text("mutated copied physical label\n", encoding="utf-8")
            self.assertPilot(
                "REGISTRY_BINDING",
                lambda: verify_pilot_design_receipt(
                    receipt_path=output / "pilot-design-receipt.json",
                    design_path=design,
                    registry_path=registry,
                    registry_evidence_root=copied_registry_evidence,
                    diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=copied_evidence,
                ),
            )
            shutil.copyfile(
                _registry_evidence_root(registry) / "labels" / "base-waterborne-clear.txt",
                copied_label,
            )

            relocated_registry = root / "relocated-registry.json"
            shutil.copyfile(registry, relocated_registry)
            relocated_evidence = root / "relocated-registry-evidence"
            shutil.copytree(_registry_evidence_root(registry), relocated_evidence)
            relocated = json.loads(relocated_registry.read_text(encoding="utf-8"))
            original_relative_path = relocated["components"][0]["physical_label_evidence"]["relative_path"]
            relocated_relative_path = "labels/relocated-base-waterborne-clear.txt"
            shutil.move(
                relocated_evidence.joinpath(*original_relative_path.split("/")),
                relocated_evidence.joinpath(*relocated_relative_path.split("/")),
            )
            relocated["components"][0]["physical_label_evidence"]["relative_path"] = relocated_relative_path
            relocated_registry.write_text(json.dumps(relocated), encoding="utf-8")
            self.assertPilot(
                "REGISTRY_BINDING",
                lambda: verify_pilot_design_receipt(
                    receipt_path=output / "pilot-design-receipt.json",
                    design_path=design,
                    registry_path=relocated_registry,
                    registry_evidence_root=relocated_evidence,
                    diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=copied_evidence,
                ),
            )

            receipt_path = output / "pilot-design-receipt.json"
            tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
            tampered["bindings"]["registry"]["physical_label_evidence"][1] = copy.deepcopy(
                tampered["bindings"]["registry"]["physical_label_evidence"][0]
            )
            without_hash = dict(tampered)
            without_hash.pop("receipt_payload_sha256")
            from km_calibration.hashing import canonical_json_bytes, sha256_bytes

            tampered["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(without_hash))
            write_json_with_sha256(receipt_path, tampered)
            self.assertPilot(
                "REGISTRY_BINDING",
                lambda: verify_pilot_design_receipt(
                    receipt_path=receipt_path,
                    design_path=design,
                    registry_path=registry,
                    registry_evidence_root=copied_registry_evidence,
                    diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=copied_evidence,
                ),
            )

    def test_dft_and_formula_batch_attacks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt, evidence_root = _real_four_card_receipt(root)
            registry = _write_verified_registry(root / "registry.json")
            registry_evidence_root = _registry_evidence_root(registry)

            reversed_design = json.loads(_write_preregistered_design(root / "reversed.json").read_text(encoding="utf-8"))
            reversed_design["dft_bands"] = {
                "DFT-L": {"target_um": 30.0, "acceptance_min_um": 25.0, "acceptance_max_um": 35.0},
                "DFT-M": {"target_um": 20.0, "acceptance_min_um": 15.0, "acceptance_max_um": 24.0},
                "DFT-H": {"target_um": 40.0, "acceptance_min_um": 36.0, "acceptance_max_um": 45.0},
            }
            (root / "reversed.json").write_text(json.dumps(reversed_design), encoding="utf-8")
            self.assertPilot(
                "DFT_TARGET_ORDER",
                lambda: freeze_pilot_design(
                    design_path=root / "reversed.json", registry_path=registry,
                    registry_evidence_root=registry_evidence_root, diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root, output_dir=root / "reversed-output",
                ),
            )

            overlap_design = json.loads(_write_preregistered_design(root / "overlap.json").read_text(encoding="utf-8"))
            overlap_design["dft_bands"]["DFT-L"]["acceptance_max_um"] = 30.0
            overlap_design["dft_bands"]["DFT-M"]["acceptance_min_um"] = 25.0
            (root / "overlap.json").write_text(json.dumps(overlap_design), encoding="utf-8")
            self.assertPilot(
                "DFT_ACCEPTANCE_ORDER",
                lambda: freeze_pilot_design(
                    design_path=root / "overlap.json", registry_path=registry,
                    registry_evidence_root=registry_evidence_root, diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root, output_dir=root / "overlap-output",
                ),
            )

            touching_design = json.loads(_write_preregistered_design(root / "touching.json").read_text(encoding="utf-8"))
            touching_design["dft_bands"]["DFT-L"]["acceptance_max_um"] = 26.0
            (root / "touching.json").write_text(json.dumps(touching_design), encoding="utf-8")
            self.assertPilot(
                "DFT_ACCEPTANCE_ORDER",
                lambda: freeze_pilot_design(
                    design_path=root / "touching.json", registry_path=registry,
                    registry_evidence_root=registry_evidence_root, diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root, output_dir=root / "touching-output",
                ),
            )

            multi_batch = json.loads(_write_preregistered_design(root / "multi-batch.json").read_text(encoding="utf-8"))
            multi_batch["roster"][1]["formula_batch_id"] = "BATCH-REWORK-ATTACK"
            (root / "multi-batch.json").write_text(json.dumps(multi_batch), encoding="utf-8")
            self.assertPilot(
                "FORMULA_FAMILY_BATCH",
                lambda: freeze_pilot_design(
                    design_path=root / "multi-batch.json", registry_path=registry,
                    registry_evidence_root=registry_evidence_root, diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root, output_dir=root / "multi-batch-output",
                ),
            )

            cross_family = json.loads(_write_preregistered_design(root / "cross-family.json").read_text(encoding="utf-8"))
            cross_family["roster"][2]["formula_batch_id"] = cross_family["roster"][0]["formula_batch_id"]
            (root / "cross-family.json").write_text(json.dumps(cross_family), encoding="utf-8")
            self.assertPilot(
                "FORMULA_BATCH",
                lambda: freeze_pilot_design(
                    design_path=root / "cross-family.json", registry_path=registry,
                    registry_evidence_root=registry_evidence_root, diagnostic_receipt_path=receipt,
                    diagnostic_evidence_root=evidence_root, output_dir=root / "cross-family-output",
                ),
            )

    def test_label_evidence_attacks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt, evidence_root = _real_four_card_receipt(root)
            design = _write_preregistered_design(root / "design.json")
            registry = _write_verified_registry(root / "registry.json")
            registry_evidence_root = _registry_evidence_root(registry)

            for field, value, code in (
                ("physical_label_verification_id", "", "LABEL_TEXT"),
                ("physical_label_verified_at", "2026-07-14T19:15:00", "LABEL_TIMESTAMP_TIMEZONE"),
            ):
                attacked = json.loads(registry.read_text(encoding="utf-8"))
                attacked["components"][0][field] = value
                registry.write_text(json.dumps(attacked), encoding="utf-8")
                self.assertPilot(
                    code,
                    lambda: freeze_pilot_design(
                        design_path=design, registry_path=registry, registry_evidence_root=registry_evidence_root,
                        diagnostic_receipt_path=receipt, diagnostic_evidence_root=evidence_root,
                        output_dir=root / f"{code}-output",
                    ),
                )
                registry = _write_verified_registry(registry)

            attacked = json.loads(registry.read_text(encoding="utf-8"))
            attacked["components"][1]["physical_label_verification_id"] = attacked["components"][0]["physical_label_verification_id"]
            registry.write_text(json.dumps(attacked), encoding="utf-8")
            error = self.assertPilot(
                "LABEL_VERIFICATION_ID_REUSE",
                lambda: freeze_pilot_design(
                    design_path=design, registry_path=registry, registry_evidence_root=registry_evidence_root,
                    diagnostic_receipt_path=receipt, diagnostic_evidence_root=evidence_root, output_dir=root / "reused-id-output",
                ),
            )
            self.assertEqual(error.path, "registry.components[1].physical_label_verification_id")
            self.assertIn("registry.components[0]", error.message)
            self.assertNotIn("LABEL-CHECK-01", error.message)

            registry = _write_verified_registry(registry)
            attacked = json.loads(registry.read_text(encoding="utf-8"))
            attacked["components"][1]["physical_label_evidence"]["relative_path"] = attacked["components"][0]["physical_label_evidence"]["relative_path"]
            registry.write_text(json.dumps(attacked), encoding="utf-8")
            self.assertPilot(
                "LABEL_EVIDENCE_REUSE",
                lambda: freeze_pilot_design(
                    design_path=design, registry_path=registry, registry_evidence_root=registry_evidence_root,
                    diagnostic_receipt_path=receipt, diagnostic_evidence_root=evidence_root, output_dir=root / "reused-path-output",
                ),
            )

            registry = _write_verified_registry(registry)
            copied_label = registry_evidence_root / "labels" / "copied-identical-label.txt"
            shutil.copyfile(registry_evidence_root / "labels" / "base-waterborne-clear.txt", copied_label)
            attacked = json.loads(registry.read_text(encoding="utf-8"))
            attacked["components"][1]["physical_label_evidence"]["relative_path"] = "labels/copied-identical-label.txt"
            registry.write_text(json.dumps(attacked), encoding="utf-8")
            self.assertPilot(
                "LABEL_EVIDENCE_REUSE",
                lambda: freeze_pilot_design(
                    design_path=design, registry_path=registry, registry_evidence_root=registry_evidence_root,
                    diagnostic_receipt_path=receipt, diagnostic_evidence_root=evidence_root, output_dir=root / "copied-bytes-output",
                ),
            )

            attacked = json.loads(registry.read_text(encoding="utf-8"))
            attacked["components"][0]["physical_label_evidence"]["relative_path"] = "../outside-label.txt"
            registry.write_text(json.dumps(attacked), encoding="utf-8")
            self.assertPilot(
                "LABEL_PATH",
                lambda: freeze_pilot_design(
                    design_path=design, registry_path=registry, registry_evidence_root=registry_evidence_root,
                    diagnostic_receipt_path=receipt, diagnostic_evidence_root=evidence_root, output_dir=root / "unsafe-path-output",
                ),
            )

            registry = _write_verified_registry(registry)
            external_label = root / "external-label.txt"
            external_label.write_text("external physical label\n", encoding="utf-8")
            symlink_label = registry_evidence_root / "labels" / "linked-label.txt"
            try:
                symlink_label.symlink_to(external_label)
            except OSError:
                symlink_label = None
            if symlink_label is not None:
                attacked = json.loads(registry.read_text(encoding="utf-8"))
                attacked["components"][0]["physical_label_evidence"]["relative_path"] = "labels/linked-label.txt"
                registry.write_text(json.dumps(attacked), encoding="utf-8")
                self.assertPilot(
                    "LABEL_FILE",
                    lambda: freeze_pilot_design(
                        design_path=design, registry_path=registry, registry_evidence_root=registry_evidence_root,
                        diagnostic_receipt_path=receipt, diagnostic_evidence_root=evidence_root, output_dir=root / "symlink-output",
                    ),
                )

            registry = _write_verified_registry(registry)
            source_label = registry_evidence_root / "labels" / "base-waterborne-clear.txt"
            hard_link = registry_evidence_root / "labels" / "hard-linked-label.txt"
            try:
                hard_link.hardlink_to(source_label)
            except OSError:
                hard_link = None
            if hard_link is not None:
                attacked = json.loads(registry.read_text(encoding="utf-8"))
                attacked["components"][0]["physical_label_evidence"]["relative_path"] = "labels/hard-linked-label.txt"
                registry.write_text(json.dumps(attacked), encoding="utf-8")
                self.assertPilot(
                    "LABEL_FILE",
                    lambda: freeze_pilot_design(
                        design_path=design, registry_path=registry, registry_evidence_root=registry_evidence_root,
                        diagnostic_receipt_path=receipt, diagnostic_evidence_root=evidence_root, output_dir=root / "hard-link-output",
                    ),
                )

    def test_staging_cleanup_on_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_write = pilot_module.write_json_with_sha256
            calls = 0

            def fail_second_write(*args: object, **kwargs: object) -> str:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected write failure")
                return original_write(*args, **kwargs)

            with mock.patch("km_calibration.pilot.write_json_with_sha256", side_effect=fail_second_write):
                self.assertPilot("OUTPUT_WRITE", lambda: prepare_pilot(REGISTRY, root / "prepare-failure"))
            self.assertFalse((root / "prepare-failure").exists())
            self.assertEqual(list(root.glob(".prepare-failure.staging-*")), [])

            receipt, evidence_root = _real_four_card_receipt(root)
            design = _write_preregistered_design(root / "design.json")
            registry = _write_verified_registry(root / "registry.json")
            empty_output = root / "freeze-failure"
            empty_output.mkdir()
            calls = 0
            with mock.patch("km_calibration.pilot.write_json_with_sha256", side_effect=fail_second_write):
                self.assertPilot(
                    "OUTPUT_WRITE",
                    lambda: freeze_pilot_design(
                        design_path=design, registry_path=registry,
                        registry_evidence_root=_registry_evidence_root(registry), diagnostic_receipt_path=receipt,
                        diagnostic_evidence_root=evidence_root, output_dir=empty_output,
                    ),
                )
            self.assertTrue(empty_output.is_dir())
            self.assertEqual(list(empty_output.iterdir()), [])
            self.assertEqual(list(root.glob(".freeze-failure.staging-*")), [])


if __name__ == "__main__":
    unittest.main()
