"""Synthetic-only authorities and sealed input for holdout-gate tests.

The fixture intentionally has the same fixed inventory as the evaluator but
contains only generated, ephemeral test material.  It never points at a real
sealed release, and its signed payloads are always ``synthetic_test_only``.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Mapping
from unittest import mock

import numpy as np
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from km_calibration.acquisition_preflight import HOLDOUT_FAMILIES, PERMISSIONS
from km_calibration.hashing import canonical_json_bytes, sha256_bytes, write_json_with_sha256


PERMISSION_KEYS = tuple(PERMISSIONS)
INACTIVE_RESULT_KEYS = (*PERMISSION_KEYS, "production_pass", "runtime_compatible")
HOLDOUT_FAMILY_COUNT = 3
HOLDOUT_CARD_COUNT = 9
HOLDOUT_CELL_COUNT = 18
HOLDOUT_READING_COUNT = 54
COMPONENT_COUNT = 15
POSITIONS = ("POS01", "POS02", "POS03")
WAVELENGTH_NM = (400.0, 475.0, 550.0, 625.0, 700.0)

# Published CIEDE2000 reference vectors mirrored from tests/color-core.test.js.
CIEDE2000_VECTORS = (
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
)


def _digest(label: str) -> str:
    return sha256_bytes(label.encode("utf-8"))


def _inactive_fields() -> dict[str, bool]:
    return {**{key: False for key in PERMISSION_KEYS}, "production_pass": False, "runtime_compatible": False}


def _public_key_base64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def _key_fingerprint(private_key: Ed25519PrivateKey) -> str:
    return sha256_bytes(base64.b64decode(_public_key_base64(private_key)))


def _signed_envelope(
    payload: Mapping[str, Any],
    *,
    private_key: Ed25519PrivateKey,
    role: str,
    key_id: str,
) -> dict[str, Any]:
    signature = private_key.sign(canonical_json_bytes(payload))
    return {
        "schema_version": "moocow-km-ed25519-signed-envelope-v1",
        "payload": dict(payload),
        "payload_sha256": sha256_bytes(canonical_json_bytes(payload)),
        "signatures": [
            {
                "role": role,
                "key_id": key_id,
                "algorithm": "Ed25519",
                "signature_base64": base64.b64encode(signature).decode("ascii"),
            }
        ],
    }


def _finite_film_prediction(
    concentrations: np.ndarray,
    components: list[dict[str, Any]],
    dft_um: float,
    backing: list[float],
) -> list[float]:
    """Independent, compact finite-film construction used only to make test data."""

    k_curves = np.asarray([item["K_mm_inv"] for item in components], dtype=float)
    s_curves = np.asarray([item["S_mm_inv"] for item in components], dtype=float)
    k_mix = concentrations @ k_curves
    s_mix = concentrations @ s_curves
    ratio = k_mix / s_mix
    b = np.sqrt(ratio * (ratio + 2.0))
    u = b / np.tanh(b * s_mix * (dft_um / 1000.0))
    result = (1.0 - np.asarray(backing, dtype=float) * (1.0 + ratio - u)) / (1.0 + ratio - np.asarray(backing, dtype=float) + u)
    if np.any((result <= 0.0) | (result >= 1.0)):  # pragma: no cover - fixture invariant.
        raise AssertionError("synthetic finite-film spectrum must stay inside the physical domain")
    return [float(value) for value in result]


def _component_order() -> list[dict[str, str]]:
    return [
        {"component_id": f"COMP-{index:02d}", "physical_lot_id": f"LOT-CURRENT-{index:02d}"}
        for index in range(COMPONENT_COUNT)
    ]


def _candidate_components(order: list[dict[str, str]], *, baseline: bool) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for index, pair in enumerate(order):
        scale = 1.33 if baseline else 1.0
        components.append(
            {
                **pair,
                "K_mm_inv": [scale * (7.0 + 0.6 * index + 0.25 * position) for position in range(len(WAVELENGTH_NM))],
                "S_mm_inv": [95.0 + 1.7 * index + 1.2 * position for position in range(len(WAVELENGTH_NM))],
            }
        )
    return components


def _condition(condition_id: str, illuminant: str, *, primary: bool) -> dict[str, Any]:
    multiplier = 1.0 if primary else 0.87
    weights = [multiplier / len(WAVELENGTH_NM)] * len(WAVELENGTH_NM)
    return {
        "condition_id": condition_id,
        "illuminant": illuminant,
        "observer": "10deg",
        "primary": primary,
        "x_weight": list(weights),
        "y_weight": list(weights),
        "z_weight": list(weights),
        "reference_white": [multiplier, multiplier, multiplier],
        "source_sha256": _digest(f"colorimetry:{condition_id}"),
    }


@dataclass(frozen=True)
class HoldoutInventory:
    """The immutable private-release shape, without any real evidence."""

    family_count: int = HOLDOUT_FAMILY_COUNT
    card_count: int = HOLDOUT_CARD_COUNT
    cell_count: int = HOLDOUT_CELL_COUNT
    reading_count: int = HOLDOUT_READING_COUNT
    reposition_ids: tuple[str, str, str] = POSITIONS


@dataclass
class SyntheticHoldoutFixture:
    """Paths and ephemeral authorities for one coherent synthetic evaluation."""

    root: Path
    authority_kwargs: dict[str, Any]
    candidate_verification: dict[str, str]
    open_dataset_manifest: dict[str, Any]
    open_dataset_source: dict[str, Any]
    candidate_export_root: Path
    baseline_model_path: Path
    repeatability_receipt_path: Path
    acceptance_profile_path: Path
    colorimetry_profile_path: Path
    custody_commitment_path: Path
    trust_store_path: Path
    preregistration_envelope_path: Path
    release_envelope_path: Path
    sealed_input_path: Path
    custodian_private_key: Ed25519PrivateKey
    reviewer_private_key: Ed25519PrivateKey
    preregistration_payload: dict[str, Any]
    release_payload: dict[str, Any]
    sealed_payload: dict[str, Any]

    def run_kwargs(self, output_dir: Path | os.PathLike[str]) -> dict[str, Any]:
        return {
            **self.authority_kwargs,
            "release_envelope_path": self.release_envelope_path,
            "sealed_input_path": self.sealed_input_path,
            "output_dir": output_dir,
        }

    def verify_kwargs(self, evaluation_root: Path | os.PathLike[str]) -> dict[str, Any]:
        return {
            **self.authority_kwargs,
            "release_envelope_path": self.release_envelope_path,
            "sealed_input_path": self.sealed_input_path,
            "evaluation_root": evaluation_root,
        }

    def rewrite_preregistration(
        self,
        payload: Mapping[str, Any],
        *,
        private_key: Ed25519PrivateKey | None = None,
        role: str = "reviewer",
        key_id: str = "synthetic-reviewer",
    ) -> str:
        self.preregistration_payload = copy.deepcopy(dict(payload))
        return write_json_with_sha256(
            self.preregistration_envelope_path,
            _signed_envelope(
                self.preregistration_payload,
                private_key=private_key or self.reviewer_private_key,
                role=role,
                key_id=key_id,
            ),
        )

    def rewrite_release(self, payload: Mapping[str, Any] | None = None) -> str:
        if payload is not None:
            self.release_payload = copy.deepcopy(dict(payload))
        return write_json_with_sha256(
            self.release_envelope_path,
            _signed_envelope(
                self.release_payload,
                private_key=self.custodian_private_key,
                role="custodian",
                key_id="synthetic-custodian",
            ),
        )

    def rewrite_sealed(self, payload: Mapping[str, Any], *, bind_release: bool = True) -> str:
        self.sealed_payload = copy.deepcopy(dict(payload))
        digest = write_json_with_sha256(self.sealed_input_path, self.sealed_payload)
        if bind_release:
            release = copy.deepcopy(self.release_payload)
            release["sealed_input_sha256"] = digest
            self.rewrite_release(release)
        return digest


class SealedReadGuard(os.PathLike[str]):
    """Records exactly when the evaluator first converts the sealed path."""

    def __init__(self, path: Path, events: list[str]) -> None:
        self._path = path
        self._events = events

    def __fspath__(self) -> str:
        self._events.append("sealed_input_read")
        return os.fspath(self._path)


def synthetic_holdout_inventory() -> HoldoutInventory:
    return HoldoutInventory()


def _build_sealed_cells(order: list[dict[str, str]], candidate_components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    backings = {
        "black": [0.055, 0.060, 0.065, 0.070, 0.075],
        "white": [0.760, 0.755, 0.750, 0.745, 0.740],
    }
    dft_by_band = {"DFT-L": 75.0, "DFT-M": 125.0, "DFT-H": 175.0}
    cells: list[dict[str, Any]] = []
    card_number = 0
    for family_index, family_id in enumerate(HOLDOUT_FAMILIES, start=1):
        fractions = np.zeros(COMPONENT_COUNT, dtype=float)
        fractions[0] = 0.70
        fractions[family_index] = 0.30
        components = [
            {
                **pair,
                "nonvolatile_volume_fraction": float(fraction),
            }
            for pair, fraction in zip(order, fractions, strict=True)
        ]
        for dft_band, dft_um in dft_by_band.items():
            card_number += 1
            card_id = f"CARD-HO-{card_number:02d}"
            for backing_name, backing_mean in backings.items():
                predicted = _finite_film_prediction(fractions, candidate_components, dft_um, backing_mean)
                readings = []
                for position, offset in zip(POSITIONS, (-0.0002, 0.0, 0.0002), strict=True):
                    readings.append(
                        {
                            "reposition_id": position,
                            "instrument_measurement_id": f"MEAS-HO-{card_number:02d}-{backing_name}-{position}",
                            "reflectance": [float(value + offset) for value in predicted],
                            "source_sha256": _digest(f"raw:{card_id}:{backing_name}:{position}"),
                        }
                    )
                cells.append(
                    {
                        "family_alias": f"H{family_index:02d}",
                        "formula_family_id": family_id,
                        "formula_id": f"FORM-HO-{family_index:02d}",
                        "formula_batch_id": f"FB-HO-{family_index:02d}",
                        "card_id": card_id,
                        "dft_band": dft_band,
                        "backing": backing_name,
                        "dft_um": dft_um,
                        "components": copy.deepcopy(components),
                        "readings": readings,
                        "evidence_sha256": _digest(f"cell:{card_id}:{backing_name}"),
                    }
                )
    return cells


def write_synthetic_holdout_fixture(root: Path) -> SyntheticHoldoutFixture:
    """Write a signed 15-component/3-family/9-card/18-cell test release.

    Every authority and sealed payload is unconditionally synthetic test data.
    """

    root.mkdir(parents=True, exist_ok=False)
    public = root / "public"
    sealed = root / "sealed"
    candidate_export_root = public / "candidate-export"
    candidate_export_root.mkdir(parents=True)
    sealed.mkdir()
    order = _component_order()
    candidate_components = _candidate_components(order, baseline=False)
    predecessor_bindings = {"open_selection_lineage_sha256": _digest("synthetic-open-lineage")}
    candidate_model = {
        "schema_version": "moocow-open-selection-km-fit-model-v1",
        "status": "open_selection_fit_candidate",
        "dataset_status": "open_selection_only",
        "wavelength_nm": list(WAVELENGTH_NM),
        "component_order": copy.deepcopy(order),
        "components": candidate_components,
        "predecessor_bindings": predecessor_bindings,
        **_inactive_fields(),
    }
    candidate_sha = write_json_with_sha256(candidate_export_root / "fit-model.json", candidate_model)
    candidate_verification = {
        "fit_model_sha256": candidate_sha,
        "selection_evaluation_sha256": _digest("synthetic-selection-evaluation"),
        "fit_export_receipt_sha256": _digest("synthetic-fit-export-receipt"),
        "status": "open_selection_fit_export_verified",
    }

    baseline_model_path = public / "baseline-model.json"
    baseline_model = {
        "schema_version": "moocow-km-finite-film-baseline-model-v1",
        "status": "baseline_frozen",
        "evidence_class": "synthetic_test_only",
        "wavelength_nm": list(WAVELENGTH_NM),
        "component_order": copy.deepcopy(order),
        "components": _candidate_components(order, baseline=True),
        "concentration_basis": "nonvolatile_volume_fraction",
        "candidate_predecessor_bindings": predecessor_bindings,
        **_inactive_fields(),
    }
    write_json_with_sha256(baseline_model_path, baseline_model)

    repeatability_receipt_path = public / "repeatability.json"
    repeatability = {
        "schema_version": "moocow-km-repeatability-baseline-v1",
        "status": "synthetic_test_only",
        "evidence_class": "synthetic_test_only",
        "derived_without_holdout": True,
        "layers": {
            name: {"n": 2, "observed_values": [0.001, 0.002], "source_sha256": _digest(f"repeat:{name}")}
            for name in ("same_position", "reposition", "bare_backing", "dft", "inter_card", "model_baseline")
        },
        **_inactive_fields(),
    }
    repeatability_sha = write_json_with_sha256(repeatability_receipt_path, repeatability)

    acceptance_profile_path = public / "acceptance-profile.json"
    acceptance_profile = {
        "schema_version": "moocow-km-holdout-acceptance-profile-v1",
        "status": "acceptance_profile_frozen",
        "evidence_class": "synthetic_test_only",
        "derived_without_holdout": True,
        "repeatability_receipt_sha256": repeatability_sha,
        "thresholds": {
            "d65_de00_median_min_improvement": 0.0,
            "d65_de00_p90_min_improvement": 0.0,
            "spectral_rmse_median_min_improvement": 0.0,
            "spectral_rmse_max_cell_degradation": 0.0,
            "alternate_de00_max_cell_degradation": 0.0,
            "contrast_rmse_max_card_degradation": 0.0,
        },
        **_inactive_fields(),
    }
    acceptance_sha = write_json_with_sha256(acceptance_profile_path, acceptance_profile)

    colorimetry_profile_path = public / "colorimetry-profile.json"
    colorimetry = {
        "schema_version": "moocow-km-colorimetry-profile-v1",
        "status": "colorimetry_profile_frozen",
        "evidence_class": "synthetic_test_only",
        "wavelength_nm": list(WAVELENGTH_NM),
        "conditions": [_condition("D65/10", "D65", primary=True), _condition("A/10", "A", primary=False)],
        **_inactive_fields(),
    }
    colorimetry_sha = write_json_with_sha256(colorimetry_profile_path, colorimetry)

    custody_commitment_path = public / "custody-commitment.json"
    custody = {
        "schema_version": "moocow-holdout-custody-commitment-v1",
        "status": "holdout_custody_committed",
        "state": "HOLDOUT_CUSTODY_COMMITTED",
        "counts": {"families": 3, "batches": 3, "cards": 9, "primary_reading_slots": 54},
        **_inactive_fields(),
    }
    custody_sha = write_json_with_sha256(custody_commitment_path, custody)

    custodian_private_key = Ed25519PrivateKey.generate()
    reviewer_private_key = Ed25519PrivateKey.generate()
    trust_store_path = public / "trust-store.json"
    trust_store = {
        "schema_version": "moocow-km-holdout-trust-store-v1",
        "keys": [
            {
                "role": "custodian",
                "key_id": "synthetic-custodian",
                "algorithm": "Ed25519",
                "public_key_base64": _public_key_base64(custodian_private_key),
                "fingerprint_sha256": _key_fingerprint(custodian_private_key),
            },
            {
                "role": "reviewer",
                "key_id": "synthetic-reviewer",
                "algorithm": "Ed25519",
                "public_key_base64": _public_key_base64(reviewer_private_key),
                "fingerprint_sha256": _key_fingerprint(reviewer_private_key),
            },
        ],
    }
    trust_store_sha = write_json_with_sha256(trust_store_path, trust_store)

    preregistration_envelope_path = public / "preregistration-envelope.json"
    preregistration_payload = {
        "schema_version": "moocow-km-holdout-preregistration-v1",
        "status": "holdout_preregistration_frozen",
        "evidence_class": "synthetic_test_only",
        "created_at": "2026-07-14T00:00:00+00:00",
        "candidate": {key: value for key, value in candidate_verification.items() if key != "status"},
        "baseline_model_sha256": sha256_bytes((baseline_model_path.read_bytes())),
        "repeatability_receipt_sha256": repeatability_sha,
        "acceptance_profile_sha256": acceptance_sha,
        "colorimetry_profile_sha256": colorimetry_sha,
        "custody_commitment_sha256": custody_sha,
        "trust_store_sha256": trust_store_sha,
        "evaluator_implementation_id": "moocow-independent-holdout-evaluator-v1",
        "custodian_key_fingerprint": _key_fingerprint(custodian_private_key),
        "reviewer_key_fingerprint": _key_fingerprint(reviewer_private_key),
    }
    preregistration_envelope = _signed_envelope(
        preregistration_payload,
        private_key=reviewer_private_key,
        role="reviewer",
        key_id="synthetic-reviewer",
    )
    preregistration_sha = write_json_with_sha256(preregistration_envelope_path, preregistration_envelope)

    sealed_input_path = sealed / "sealed-input.json"
    backings = {
        "black": {"mean_reflectance": [0.055, 0.060, 0.065, 0.070, 0.075], "source_sha256": _digest("backing:black")},
        "white": {"mean_reflectance": [0.760, 0.755, 0.750, 0.745, 0.740], "source_sha256": _digest("backing:white")},
    }
    open_dataset_manifest = {
        "locked_conditions": {
            "instrument": "synthetic-spectrophotometer-v1",
            "geometry": "d8-specular-excluded",
            "illuminant": "D65",
        }
    }
    sealed_payload = {
        "schema_version": "moocow-km-sealed-holdout-evaluation-input-v1",
        "evidence_class": "synthetic_test_only",
        "release_id": "synthetic-release-0001",
        "wavelength_nm": list(WAVELENGTH_NM),
        "locked_conditions_sha256": sha256_bytes(canonical_json_bytes(open_dataset_manifest["locked_conditions"])),
        "component_order": copy.deepcopy(order),
        "backings": backings,
        "cells": _build_sealed_cells(order, candidate_components),
        "evidence_manifest_sha256": _digest("synthetic-evidence-manifest"),
    }
    sealed_sha = write_json_with_sha256(sealed_input_path, sealed_payload)

    release_envelope_path = public / "release-envelope.json"
    release_payload = {
        "schema_version": "moocow-km-holdout-release-v1",
        "status": "holdout_release_authorized",
        "evidence_class": "synthetic_test_only",
        "issued_at": "2026-07-15T00:00:00+00:00",
        "release_id": "synthetic-release-0001",
        "one_time_nonce": "synthetic-nonce-0001",
        "preregistration_envelope_sha256": preregistration_sha,
        "sealed_input_sha256": sealed_sha,
        "custody_commitment_sha256": custody_sha,
        "custodian_key_fingerprint": _key_fingerprint(custodian_private_key),
    }
    release_envelope = _signed_envelope(
        release_payload,
        private_key=custodian_private_key,
        role="custodian",
        key_id="synthetic-custodian",
    )
    write_json_with_sha256(release_envelope_path, release_envelope)

    open_dataset_source = {
        "cards": [
            {
                "card_id": "OPEN-CARD-01",
                "formula_family_id": "FAM-OPEN-01",
                "formula_id": "OPEN-FORMULA-01",
                "formula_batch_id": "OPEN-BATCH-01",
            }
        ],
        "measurements": [{"instrument_measurement_id": "OPEN-MEASUREMENT-01"}],
    }
    authority_kwargs: dict[str, Any] = {
        "acquisition_receipt_path": public / "unused-acquisition.json",
        "admission_receipt_path": public / "unused-admission.json",
        "dataset_root": public / "unused-dataset",
        "shared_root": public / "unused-shared",
        "open_root": public / "unused-open",
        "measurement_root": public / "unused-measurements",
        "candidate_export_root": candidate_export_root,
        "baseline_model_path": baseline_model_path,
        "custody_commitment_path": custody_commitment_path,
        "repeatability_receipt_path": repeatability_receipt_path,
        "acceptance_profile_path": acceptance_profile_path,
        "colorimetry_profile_path": colorimetry_profile_path,
        "trust_store_path": trust_store_path,
        "preregistration_envelope_path": preregistration_envelope_path,
    }
    return SyntheticHoldoutFixture(
        root=root,
        authority_kwargs=authority_kwargs,
        candidate_verification=candidate_verification,
        open_dataset_manifest=open_dataset_manifest,
        open_dataset_source=open_dataset_source,
        candidate_export_root=candidate_export_root,
        baseline_model_path=baseline_model_path,
        repeatability_receipt_path=repeatability_receipt_path,
        acceptance_profile_path=acceptance_profile_path,
        colorimetry_profile_path=colorimetry_profile_path,
        custody_commitment_path=custody_commitment_path,
        trust_store_path=trust_store_path,
        preregistration_envelope_path=preregistration_envelope_path,
        release_envelope_path=release_envelope_path,
        sealed_input_path=sealed_input_path,
        custodian_private_key=custodian_private_key,
        reviewer_private_key=reviewer_private_key,
        preregistration_payload=preregistration_payload,
        release_payload=release_payload,
        sealed_payload=sealed_payload,
    )


@contextmanager
def patched_open_selection_dependencies(
    module: Any,
    fixture: SyntheticHoldoutFixture,
    events: list[str] | None = None,
) -> Iterator[None]:
    """Patch only the two intentionally expensive open-selection dependencies."""

    def verify_candidate(**_kwargs: Any) -> dict[str, str]:
        if events is not None:
            events.append("candidate_verify")
        return copy.deepcopy(fixture.candidate_verification)

    def load_open_dataset(_dataset_root: Path | str) -> SimpleNamespace:
        if events is not None:
            events.append("open_dataset")
        return SimpleNamespace(
            manifest=copy.deepcopy(fixture.open_dataset_manifest),
            source=copy.deepcopy(fixture.open_dataset_source),
        )

    with mock.patch.object(module, "verify_open_selection_fit_export", side_effect=verify_candidate), mock.patch.object(
        module, "load_and_validate_open_selection_dataset", side_effect=load_open_dataset
    ):
        yield


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):  # pragma: no cover - fixture invariant.
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def self_bind(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result[field] = ""
    payload = dict(result)
    payload.pop(field)
    result[field] = sha256_bytes(canonical_json_bytes(payload))
    return result


def assert_all_permissions_false(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in INACTIVE_RESULT_KEYS and child is not False:
                raise AssertionError(f"{path}.{key} must remain false")
            assert_all_permissions_false(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_all_permissions_false(child, f"{path}[{index}]")


def walk_values(value: object) -> Iterator[tuple[str, object]]:
    def walk(current: object, path: str) -> Iterator[tuple[str, object]]:
        yield path, current
        if isinstance(current, dict):
            for key in sorted(current):
                yield from walk(current[key], f"{path}.{key}")
        elif isinstance(current, list):
            for index, child in enumerate(current):
                yield from walk(child, f"{path}[{index}]")

    yield from walk(value, "$")


def assert_public_receipt_has_no_sealed_values(value: object, sentinels: set[str]) -> None:
    """Reject raw evidence identities while permitting public classes and hashes."""

    forbidden_key_parts = (
        "reflectance",
        "spectrum",
        "actual_nv",
        "formula_batch",
        "formula_family_id",
        "formula_id",
        "card_id",
        "measurement_id",
        "relative_path",
        "sealed_root",
        "evidence_path",
    )
    for path, child in walk_values(value):
        terminal = path.rsplit(".", maxsplit=1)[-1].casefold()
        if any(part in terminal for part in forbidden_key_parts):
            raise AssertionError(f"public receipt exposes a sealed field at {path}")
        if isinstance(child, str) and any(marker in child for marker in sentinels):
            raise AssertionError(f"public receipt exposes a sealed value at {path}")


def sealed_private_sentinels() -> set[str]:
    return {
        *HOLDOUT_FAMILIES,
        "FORM-HO-01",
        "FB-HO-01",
        "CARD-HO-01",
        "MEAS-HO-01-black-POS01",
    }


def require_exact_inventory(value: dict[str, Any]) -> None:
    inventory = synthetic_holdout_inventory()
    if value != {
        "families": inventory.family_count,
        "cards": inventory.card_count,
        "cells": inventory.cell_count,
        "readings": inventory.reading_count,
    }:
        raise AssertionError("fixture inventory must be exactly 3 families, 9 cards, 18 cells, and 54 readings")
