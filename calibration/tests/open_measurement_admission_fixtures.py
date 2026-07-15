"""Test-only, nonphysical sources for open-measurement admission.

The numbers below exercise transport validation only.  They are deliberately
synthetic fixture values and must never be treated as physical observations.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Callable


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = CALIBRATION_ROOT / "tests"

import sys

sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from acquisition_preflight_fixtures import (
    assert_permissions_all_false,
    write_current_materials,
    write_frozen_pilot_prerequisite,
    write_pilot_batch_roots,
)
from km_calibration.acquisition_preflight import (
    assemble_acquisition_preflight,
    commit_holdout_custody,
    preflight_open_batches,
    preflight_pilot_materials,
    verify_acquisition_preflight,
)
from km_calibration.hashing import canonical_json_bytes, sha256_bytes, write_json_with_sha256


WAVELENGTH_NM = [360.0, 390.0, 420.0]
_BACKING_IDS = {"black": "FIXTURE-BACKING-BLACK", "white": "FIXTURE-BACKING-WHITE"}
_BACKING_LOTS = {"black": "FIXTURE-LOT-BLACK", "white": "FIXTURE-LOT-WHITE"}


def _evidence_locator(relative_path: str) -> dict[str, object]:
    return {"relative_path": relative_path, "record_locator": {"kind": "whole_file"}}


def _write_evidence(root: Path, relative_path: str, token: str) -> dict[str, object]:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"nonphysical open-admission fixture evidence\n{token}\n".encode("utf-8"))
    return _evidence_locator(relative_path)


def _synthetic_reflectance(seed: int) -> list[float]:
    """Return finite in-range fixture values that make no physical claim."""

    base = 0.10 + (seed % 13) * 0.02
    return [base, base + 0.01, base + 0.02]


def _synthetic_dft_points(dft_band: str) -> list[float]:
    """Only preserve the receipt band's ordering; these are not measurements."""

    base = {"DFT-L": 1.0, "DFT-M": 2.0, "DFT-H": 3.0}[dft_band]
    return [base, base + 0.1]


def _build_acquisition_receipt(root: Path) -> dict[str, object]:
    """Use the existing test-only preflight chain to publish a verified predecessor."""

    prerequisite = write_frozen_pilot_prerequisite(root)
    shared_root, materials = write_current_materials(root, prerequisite)
    batch_roots = write_pilot_batch_roots(root, shared_root, materials)

    common_output = root / "common-preflight"
    common_result = preflight_pilot_materials(
        **prerequisite,
        shared_root=shared_root,
        output_dir=common_output,
    )
    assert_permissions_all_false(common_result)
    common_receipt = common_output / "common-material-receipt.json"

    open_output = root / "open-preflight"
    open_result = preflight_open_batches(
        materials_receipt_path=common_receipt,
        open_batch_root=batch_roots["open_batch_root"],
        open_evidence_root=batch_roots["open_evidence_root"],
        output_dir=open_output,
    )
    assert_permissions_all_false(open_result)
    open_receipt_path = open_output / "open-batch-preflight-receipt.json"

    custody_output = root / "custody-preflight"
    custody_result = commit_holdout_custody(
        materials_receipt_path=common_receipt,
        open_batch_receipt_path=open_receipt_path,
        sealed_holdout_batch_root=batch_roots["sealed_batch_root"],
        sealed_evidence_root=batch_roots["sealed_evidence_root"],
        custody_identity="fixture independent custodian",
        custody_key_fingerprint="fixture-key-01",
        signature_metadata={"algorithm": "fixture-attestation", "signed_at": "2026-07-14T23:00:00+09:00"},
        output_dir=custody_output,
    )
    assert_permissions_all_false(custody_result)

    acquisition_output = root / "acquisition-preflight"
    assembled = assemble_acquisition_preflight(
        open_batch_receipt_path=open_receipt_path,
        holdout_custody_commitment_path=custody_output / "holdout-custody-commitment.json",
        output_dir=acquisition_output,
    )
    assert_permissions_all_false(assembled)
    acquisition_receipt = acquisition_output / "acquisition-preflight-receipt.json"
    verified = verify_acquisition_preflight(
        receipt_path=acquisition_receipt,
        shared_root=shared_root,
        open_root=batch_roots["open_batch_root"],
    )
    assert_permissions_all_false(verified)
    if not verified.get("receipt_verified"):
        raise AssertionError("fixture predecessor receipt did not reverify")

    return {
        "acquisition_receipt": acquisition_receipt,
        "shared_root": shared_root,
        "open_root": batch_roots["open_batch_root"],
        "open_receipt_path": open_receipt_path,
        "open_receipt": json.loads(open_receipt_path.read_text(encoding="utf-8")),
        "sealed_root_for_rejection": batch_roots["sealed_batch_root"],
    }


def write_valid_open_measurement_fixture(root: Path) -> dict[str, object]:
    """Create the contract-minimum 36-card, 216-reading open admission input."""

    predecessor = _build_acquisition_receipt(root)
    measurement_root = root / "measurement-source"
    measurement_root.mkdir(parents=True, exist_ok=True)
    open_receipt = predecessor["open_receipt"]
    if not isinstance(open_receipt, dict):  # pragma: no cover - fixture invariant.
        raise AssertionError("open predecessor receipt must be an object")
    skeleton = open_receipt["card_skeleton"]
    if not isinstance(skeleton, list) or len(skeleton) != 36:  # pragma: no cover - fixture invariant.
        raise AssertionError("preflight fixture did not produce 36 open cards")

    calibration_evidence = _write_evidence(measurement_root, "instrument/calibration.txt", "calibration")
    run_log_evidence = _write_evidence(measurement_root, "instrument/run-log.txt", "run-log")

    bare_measurements: dict[str, list[dict[str, object]]] = {"black": [], "white": []}
    for backing_index, backing in enumerate(("black", "white")):
        for position_index in range(3):
            token = f"bare-{backing}-{position_index + 1:02d}"
            bare_measurements[backing].append(
                {
                    "instrument_measurement_id": f"FIXTURE-BARE-{backing.upper()}-{position_index + 1:02d}",
                    "measured_at_local": f"2026-07-14T23:0{position_index}:00+09:00",
                    "reposition_id": f"POS{position_index + 1:02d}",
                    "raw_spectrum_evidence": _write_evidence(measurement_root, f"bare/{token}.bin", token),
                    "reflectance": _synthetic_reflectance(backing_index * 3 + position_index),
                }
            )

    cards: list[dict[str, object]] = []
    readings: list[dict[str, object]] = []
    reading_index = 0
    for card_index, skeleton_card in enumerate(skeleton):
        if not isinstance(skeleton_card, dict):  # pragma: no cover - fixture invariant.
            raise AssertionError("card skeleton entry must be an object")
        card_id = str(skeleton_card["card_id"])
        dft_band = str(skeleton_card["dft_band"])
        dft_by_backing: dict[str, object] = {}
        for backing in ("black", "white"):
            dft_token = f"dft-{card_index + 1:02d}-{backing}"
            dft_by_backing[backing] = {
                "dft_measurement_id": f"FIXTURE-DFT-{card_index + 1:02d}-{backing.upper()}",
                "measured_at_local": "2026-07-14T23:30:00+09:00",
                "dft_points_um": _synthetic_dft_points(dft_band),
                "dft_evidence": _write_evidence(measurement_root, f"dft/{dft_token}.bin", dft_token),
            }
        cards.append({"card_id": card_id, "dft_by_backing": dft_by_backing})

        slots = skeleton_card["primary_reading_slots"]
        if not isinstance(slots, list) or len(slots) != 6:  # pragma: no cover - fixture invariant.
            raise AssertionError("card skeleton must have six primary reading slots")
        for slot in slots:
            if not isinstance(slot, dict):  # pragma: no cover - fixture invariant.
                raise AssertionError("reading slot must be an object")
            backing = str(slot["backing"])
            position = str(slot["reposition_id"])
            reading_index += 1
            token = f"coated-{reading_index:03d}"
            readings.append(
                {
                    "card_id": card_id,
                    "backing": backing,
                    "reposition_id": position,
                    "instrument_measurement_id": f"FIXTURE-COATED-{reading_index:03d}",
                    "position_note": f"fixture slot {position}",
                    "orientation": "fixture-reference-axis",
                    "measured_at_local": "2026-07-14T23:45:00+09:00",
                    "raw_spectrum_evidence": _write_evidence(measurement_root, f"spectra/{token}.bin", token),
                    "surface_status": "accepted_uniform_dry_film",
                    "model_applicability_status": "accepted_for_km_diagnostic",
                    "backing_id": _BACKING_IDS[backing],
                    "backing_lot_id": _BACKING_LOTS[backing],
                    "reflectance": _synthetic_reflectance(reading_index),
                }
            )

    payload: dict[str, object] = {
        "schema_version": "moocow-open-measurement-admission-input-v1",
        "measurement_session_id": "fixture-open-session-001",
        "wavelength_nm": WAVELENGTH_NM,
        "locked_conditions": {
            "instrument_id": "fixture-instrument-nonphysical",
            "instrument_calibration_evidence": calibration_evidence,
            "instrument_run_log_evidence": run_log_evidence,
            "fixture_protocol_id": "nonphysical-transport-test-v1",
        },
        "backings": {
            backing: {
                "backing_id": _BACKING_IDS[backing],
                "lot_id": _BACKING_LOTS[backing],
                "bare_measurements": bare_measurements[backing],
            }
            for backing in ("black", "white")
        },
        "cards": cards,
        "readings": readings,
    }
    if len(cards) != 36 or len(readings) != 216:  # pragma: no cover - fixture invariant.
        raise AssertionError("fixture did not create the contract roster")

    admission_input_relative_path = "admission/open-measurements-input.json"
    admission_input_path = measurement_root / admission_input_relative_path
    write_json_with_sha256(admission_input_path, payload)
    return {
        **predecessor,
        "measurement_root": measurement_root,
        "admission_input_relative_path": admission_input_relative_path,
        "admission_input_path": admission_input_path,
        "payload": payload,
    }


def rewrite_admission_input(source: dict[str, object], mutation: Callable[[dict[str, object]], None]) -> dict[str, object]:
    """Mutate the sidecar-bound admission input without changing any predecessor bytes."""

    input_path = source["admission_input_path"]
    if not isinstance(input_path, Path):  # pragma: no cover - fixture invariant.
        raise AssertionError("fixture admission path must be a Path")
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    mutation(payload)
    write_json_with_sha256(input_path, payload)
    source["payload"] = payload
    return payload


def rehash_published_admission(
    output_dir: Path,
    mutation: Callable[[dict[str, object], dict[str, object], dict[str, object]], None],
) -> dict[str, object]:
    """Forge published values while keeping every output hash internally consistent.

    The bound admission input and predecessor receipt are deliberately left
    untouched.  Verification must therefore reconstruct from those sources,
    rather than trusting this fully rehashed output chain.
    """

    manifest_path = output_dir / "manifest.json"
    source_path = output_dir / "sources" / "open-measurements.json"
    receipt_path = output_dir / "admission-receipt.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = json.loads(source_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    mutation(manifest, records, receipt)

    source_sha256 = write_json_with_sha256(source_path, records)
    manifest["source_files"][0]["sha256"] = source_sha256
    manifest_sha256 = write_json_with_sha256(manifest_path, manifest)
    receipt["bindings"]["open_measurements"]["sha256"] = source_sha256
    receipt["bindings"]["dataset_manifest"]["sha256"] = manifest_sha256
    receipt_payload = dict(receipt)
    receipt_payload.pop("receipt_payload_sha256", None)
    receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(receipt_payload))
    receipt_sha256 = write_json_with_sha256(receipt_path, receipt)
    return {
        "manifest": manifest,
        "source": records,
        "receipt": receipt,
        "manifest_sha256": manifest_sha256,
        "source_sha256": source_sha256,
        "receipt_sha256": receipt_sha256,
    }


def copytree_without_rewriting(root: Path, destination: Path) -> Path:
    """Copy bytes as-is for portable verification tests."""

    shutil.copytree(root, destination)
    return destination


def immutable_copy(value: dict[str, object]) -> dict[str, object]:
    """Return a mutable test input without aliasing fixture state."""

    return copy.deepcopy(value)
