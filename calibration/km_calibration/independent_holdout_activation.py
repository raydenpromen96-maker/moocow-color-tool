"""Custody-isolated, non-activating independent holdout evaluation.

This boundary is intentionally separate from open-selection fitting. It verifies
the current open candidate before reading a sealed evaluation input, verifies
role-pinned Ed25519 envelopes, reconstructs baseline/candidate predictions, and
emits review evidence whose runtime and production permissions remain false.
"""

from __future__ import annotations

import base64
import binascii
import copy
import datetime as dt
import math
import os
import shutil
import stat
import statistics
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .acquisition_preflight import HOLDOUT_FAMILIES, PERMISSIONS
from .errors import CalibrationError, DatasetValidationError
from .hashing import (
    canonical_json_bytes,
    read_verified_json,
    sha256_bytes,
    write_json_with_sha256,
)
from .open_measurement_admission import load_and_validate_open_selection_dataset
from .open_selection_fit_export import verify_open_selection_fit_export


TRUST_STORE_SCHEMA = "moocow-km-holdout-trust-store-v1"
SIGNED_ENVELOPE_SCHEMA = "moocow-km-ed25519-signed-envelope-v1"
PREREGISTRATION_SCHEMA = "moocow-km-holdout-preregistration-v1"
RELEASE_SCHEMA = "moocow-km-holdout-release-v1"
BASELINE_MODEL_SCHEMA = "moocow-km-finite-film-baseline-model-v1"
REPEATABILITY_SCHEMA = "moocow-km-repeatability-baseline-v1"
ACCEPTANCE_PROFILE_SCHEMA = "moocow-km-holdout-acceptance-profile-v1"
COLORIMETRY_PROFILE_SCHEMA = "moocow-km-colorimetry-profile-v1"
SEALED_INPUT_SCHEMA = "moocow-km-sealed-holdout-evaluation-input-v1"
DETAIL_SCHEMA = "moocow-independent-holdout-evaluation-detail-v1"
REVIEW_RECEIPT_SCHEMA = "moocow-independent-holdout-review-receipt-v1"
RELEASE_CONSUMPTION_SCHEMA = "moocow-km-holdout-release-consumption-v1"
RELEASE_TUPLE_SCHEMA = "moocow-km-holdout-evaluation-tuple-v1"
EVALUATOR_IMPLEMENTATION_ID = "moocow-independent-holdout-evaluator-v1"

EVIDENCE_CLASSES = {"measured_current_batch", "synthetic_test_only"}
BACKINGS = ("black", "white")
DFT_BANDS = ("DFT-L", "DFT-M", "DFT-H")
POSITIONS = ("POS01", "POS02", "POS03")
EXPECTED_COUNTS = {"families": 3, "cards": 9, "cells": 18, "readings": 54}
OUTPUT_FILES = {
    "sealed-holdout-evaluation-detail.json",
    "sealed-holdout-evaluation-detail.json.sha256",
    "independent-holdout-review-receipt.json",
    "independent-holdout-review-receipt.json.sha256",
}
RELEASE_LEDGER_FILES = {
    "release-consumption.json",
    "release-consumption.json.sha256",
}
WINDOWS_REPARSE_POINT = 0x400
ROUND_TOLERANCE = 32.0 * np.finfo(float).eps


class IndependentHoldoutActivationError(CalibrationError):
    """Stable staged failure for the independent holdout boundary."""

    def __init__(self, stage: str, code: str, path: str, message: str) -> None:
        self.stage = stage
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"[{stage}:{code}] {path}: {message}")


def _fail(stage: str, code: str, path: str, message: str) -> None:
    raise IndependentHoldoutActivationError(stage, code, path, message)


def _mapping(value: object, path: str, *, stage: str = "AUTHORITY") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(stage, "TYPE", path, "must be an object")
    return value


def _list(value: object, path: str, *, stage: str = "AUTHORITY") -> list[Any]:
    if not isinstance(value, list):
        _fail(stage, "TYPE", path, "must be an array")
    return value


def _exact(value: Mapping[str, Any], expected: Sequence[str], path: str, *, stage: str) -> None:
    if set(value) != set(expected):
        _fail(stage, "FIELDS", path, f"must contain exactly {sorted(expected)}")


def _text(value: object, path: str, *, stage: str, allow_synthetic: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(stage, "TEXT", path, "must be a non-empty string")
    result = value.strip()
    markers = ("required", "template", "placeholder", "not_yet")
    if any(marker in result.casefold() for marker in markers) or (
        not allow_synthetic and "synthetic" in result.casefold()
    ):
        _fail(stage, "PLACEHOLDER", path, "must not be a placeholder")
    return result


def _sha256(value: object, path: str, *, stage: str) -> str:
    if not isinstance(value, str):
        _fail(stage, "SHA256", path, "must be a SHA-256 digest")
    result = value.lower()
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        _fail(stage, "SHA256", path, "must be a SHA-256 digest")
    return result


def _number(value: object, path: str, *, stage: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        _fail(stage, "NUMBER", path, "must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        _fail(stage, "NUMBER", path, "must be a finite number")
    if not math.isfinite(result) or (positive and result <= 0.0):
        _fail(stage, "NUMBER", path, "must be finite" + (" and positive" if positive else ""))
    return result


def _timestamp(value: object, path: str, *, stage: str) -> dt.datetime:
    text = _text(value, path, stage=stage)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail(stage, "TIMESTAMP", path, "must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(stage, "TIMESTAMP", path, "must include a timezone offset")
    return parsed


def _evidence_class(value: object, path: str, *, stage: str) -> str:
    if value not in EVIDENCE_CLASSES:
        _fail(stage, "EVIDENCE_CLASS", path, f"must be one of {sorted(EVIDENCE_CLASSES)}")
    return str(value)


def _permissions() -> dict[str, bool]:
    return {permission: False for permission in PERMISSIONS}


def _assert_inactive(value: Mapping[str, Any], path: str, *, stage: str) -> None:
    for permission in PERMISSIONS:
        if value.get(permission) is not False:
            _fail(stage, "PERMISSION", f"{path}.{permission}", "must remain false")
    for key in ("production_pass", "runtime_compatible"):
        if key in value and value.get(key) is not False:
            _fail(stage, "PERMISSION", f"{path}.{key}", "must remain false")


def _canonical_file_digest(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value) + b"\n")


def _read_canonical(path: Path | str, label: str, *, stage: str) -> tuple[dict[str, Any], str]:
    candidate = _validate_no_reparse_ancestors(path, stage=stage, code="ARTIFACT_PATH")
    try:
        value, digest = read_verified_json(candidate, require_sidecar=True, trusted_root=candidate.parent)
    except (DatasetValidationError, OSError, ValueError) as error:
        _fail(stage, "ARTIFACT", label, str(error))
    result = dict(_mapping(value, label, stage=stage))
    if digest != _canonical_file_digest(result):
        _fail(stage, "NONCANONICAL", label, "must use canonical JSON bytes")
    return result, digest


def _payload_digest(value: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(value))


@dataclass(frozen=True)
class _TrustedKey:
    role: str
    key_id: str
    fingerprint: str
    public_key: Ed25519PublicKey


def _decode_base64(value: object, path: str, *, stage: str, expected_length: int) -> bytes:
    text = _text(value, path, stage=stage)
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        _fail(stage, "BASE64", path, "must be canonical base64")
    if len(raw) != expected_length or base64.b64encode(raw).decode("ascii") != text:
        _fail(stage, "BASE64", path, f"must encode exactly {expected_length} bytes")
    return raw


def _load_trust_store(path: Path | str) -> tuple[dict[str, _TrustedKey], str]:
    value, digest = _read_canonical(path, "trust_store", stage="AUTHORITY")
    _exact(value, ("schema_version", "keys"), "trust_store", stage="AUTHORITY")
    if value.get("schema_version") != TRUST_STORE_SCHEMA:
        _fail("AUTHORITY", "SCHEMA", "trust_store.schema_version", "is not supported")
    keys: dict[str, _TrustedKey] = {}
    roles: set[str] = set()
    fingerprints: set[str] = set()
    for index, raw_item in enumerate(_list(value.get("keys"), "trust_store.keys")):
        item = _mapping(raw_item, f"trust_store.keys[{index}]")
        _exact(
            item,
            ("role", "key_id", "algorithm", "public_key_base64", "fingerprint_sha256"),
            f"trust_store.keys[{index}]",
            stage="AUTHORITY",
        )
        role = _text(item.get("role"), f"trust_store.keys[{index}].role", stage="AUTHORITY")
        if role not in {"custodian", "reviewer"} or role in roles:
            _fail("AUTHORITY", "ROLE", f"trust_store.keys[{index}].role", "must be one unique custodian or reviewer role")
        if item.get("algorithm") != "Ed25519":
            _fail("AUTHORITY", "ALGORITHM", f"trust_store.keys[{index}].algorithm", "must be Ed25519")
        key_id = _text(item.get("key_id"), f"trust_store.keys[{index}].key_id", stage="AUTHORITY")
        if key_id in keys:
            _fail("AUTHORITY", "KEY_ID", f"trust_store.keys[{index}].key_id", "must be unique")
        raw_key = _decode_base64(
            item.get("public_key_base64"),
            f"trust_store.keys[{index}].public_key_base64",
            stage="AUTHORITY",
            expected_length=32,
        )
        fingerprint = _sha256(item.get("fingerprint_sha256"), f"trust_store.keys[{index}].fingerprint_sha256", stage="AUTHORITY")
        if fingerprint != sha256_bytes(raw_key) or fingerprint in fingerprints:
            _fail("AUTHORITY", "FINGERPRINT", f"trust_store.keys[{index}]", "fingerprint is stale, duplicated, or does not bind the public key")
        keys[key_id] = _TrustedKey(role, key_id, fingerprint, Ed25519PublicKey.from_public_bytes(raw_key))
        roles.add(role)
        fingerprints.add(fingerprint)
    if roles != {"custodian", "reviewer"} or len(keys) != 2:
        _fail("AUTHORITY", "ROLE", "trust_store.keys", "must contain exactly one distinct custodian and one reviewer key")
    return keys, digest


def _verify_envelope(
    path: Path | str,
    *,
    keys: Mapping[str, _TrustedKey],
    required_role: str,
    payload_schema: str,
    label: str,
) -> tuple[dict[str, Any], str]:
    envelope, digest = _read_canonical(path, label, stage="AUTHORITY")
    _exact(envelope, ("schema_version", "payload", "payload_sha256", "signatures"), label, stage="AUTHORITY")
    if envelope.get("schema_version") != SIGNED_ENVELOPE_SCHEMA:
        _fail("AUTHORITY", "SCHEMA", f"{label}.schema_version", "is not supported")
    payload = dict(_mapping(envelope.get("payload"), f"{label}.payload"))
    if payload.get("schema_version") != payload_schema:
        _fail("AUTHORITY", "SCHEMA", f"{label}.payload.schema_version", "is not supported")
    payload_sha = _sha256(envelope.get("payload_sha256"), f"{label}.payload_sha256", stage="AUTHORITY")
    if payload_sha != _payload_digest(payload):
        _fail("AUTHORITY", "PAYLOAD_HASH", f"{label}.payload_sha256", "does not bind the canonical payload")
    signatures = _list(envelope.get("signatures"), f"{label}.signatures")
    if len(signatures) != 1:
        _fail("AUTHORITY", "SIGNATURE_COUNT", f"{label}.signatures", "must contain exactly one role signature")
    signature = _mapping(signatures[0], f"{label}.signatures[0]")
    _exact(signature, ("role", "key_id", "algorithm", "signature_base64"), f"{label}.signatures[0]", stage="AUTHORITY")
    if signature.get("role") != required_role or signature.get("algorithm") != "Ed25519":
        _fail("AUTHORITY", "SIGNATURE_ROLE", f"{label}.signatures[0]", f"must be an Ed25519 {required_role} signature")
    key_id = _text(signature.get("key_id"), f"{label}.signatures[0].key_id", stage="AUTHORITY")
    trusted = keys.get(key_id)
    if trusted is None or trusted.role != required_role:
        _fail("AUTHORITY", "UNTRUSTED_KEY", f"{label}.signatures[0].key_id", "is not pinned for the required role")
    signature_bytes = _decode_base64(
        signature.get("signature_base64"),
        f"{label}.signatures[0].signature_base64",
        stage="AUTHORITY",
        expected_length=64,
    )
    try:
        trusted.public_key.verify(signature_bytes, canonical_json_bytes(payload))
    except InvalidSignature:
        _fail("AUTHORITY", "INVALID_SIGNATURE", f"{label}.signatures[0]", "does not authenticate the canonical payload")
    return payload, digest


def _read_candidate_model(export_root: Path | str, expected_sha256: str) -> dict[str, Any]:
    root = Path(export_root)
    model, digest = _read_canonical(root / "fit-model.json", "candidate.fit-model.json", stage="AUTHORITY")
    if digest != expected_sha256:
        _fail("AUTHORITY", "CANDIDATE_HASH", "candidate.fit-model.json", "does not match the reverified candidate")
    if model.get("schema_version") != "moocow-open-selection-km-fit-model-v1":
        _fail("AUTHORITY", "SCHEMA", "candidate.fit-model.json.schema_version", "is not the open-selection finite-film model")
    _assert_inactive(model, "candidate.fit-model.json", stage="AUTHORITY")
    if model.get("status") != "open_selection_fit_candidate" or model.get("dataset_status") != "open_selection_only":
        _fail("AUTHORITY", "CANDIDATE_STATUS", "candidate.fit-model.json", "is not a frozen open-selection candidate")
    return model


def _validate_model_curves(
    model: Mapping[str, Any],
    *,
    path: str,
    wavelengths: Sequence[float] | None = None,
    component_order: Sequence[tuple[str, str]] | None = None,
) -> tuple[list[float], list[tuple[str, str]], np.ndarray, np.ndarray]:
    raw_wavelengths = _list(model.get("wavelength_nm"), f"{path}.wavelength_nm")
    grid = [_number(value, f"{path}.wavelength_nm[{index}]", stage="AUTHORITY") for index, value in enumerate(raw_wavelengths)]
    if len(grid) < 3 or any(right <= left for left, right in zip(grid, grid[1:])):
        _fail("AUTHORITY", "WAVELENGTH", f"{path}.wavelength_nm", "must be a strictly increasing grid")
    if wavelengths is not None and list(wavelengths) != grid:
        _fail("AUTHORITY", "WAVELENGTH", f"{path}.wavelength_nm", "does not match the candidate grid")

    raw_order = _list(model.get("component_order"), f"{path}.component_order")
    order: list[tuple[str, str]] = []
    for index, raw_item in enumerate(raw_order):
        item = _mapping(raw_item, f"{path}.component_order[{index}]")
        _exact(item, ("component_id", "physical_lot_id"), f"{path}.component_order[{index}]", stage="AUTHORITY")
        pair = (
            _text(item.get("component_id"), f"{path}.component_order[{index}].component_id", stage="AUTHORITY"),
            _text(item.get("physical_lot_id"), f"{path}.component_order[{index}].physical_lot_id", stage="AUTHORITY"),
        )
        if pair in order:
            _fail("AUTHORITY", "COMPONENT_ORDER", f"{path}.component_order[{index}]", "must be unique")
        order.append(pair)
    if len(order) != 15:
        _fail("AUTHORITY", "COMPONENT_COUNT", f"{path}.component_order", "must contain the current base plus 14 colorants")
    if component_order is not None and list(component_order) != order:
        _fail("AUTHORITY", "COMPONENT_ORDER", f"{path}.component_order", "does not match the candidate component/lot order")

    raw_components = _list(model.get("components"), f"{path}.components")
    if len(raw_components) != len(order):
        _fail("AUTHORITY", "COMPONENT_COUNT", f"{path}.components", "must match component_order")
    absorption: list[list[float]] = []
    scattering: list[list[float]] = []
    for index, raw_item in enumerate(raw_components):
        item = _mapping(raw_item, f"{path}.components[{index}]")
        if (item.get("component_id"), item.get("physical_lot_id")) != order[index]:
            _fail("AUTHORITY", "COMPONENT_ORDER", f"{path}.components[{index}]", "does not match component_order")
        k_values = [_number(value, f"{path}.components[{index}].K_mm_inv[{position}]", stage="AUTHORITY") for position, value in enumerate(_list(item.get("K_mm_inv"), f"{path}.components[{index}].K_mm_inv"))]
        s_values = [_number(value, f"{path}.components[{index}].S_mm_inv[{position}]", stage="AUTHORITY", positive=True) for position, value in enumerate(_list(item.get("S_mm_inv"), f"{path}.components[{index}].S_mm_inv"))]
        if len(k_values) != len(grid) or len(s_values) != len(grid) or any(value < 0.0 for value in k_values):
            _fail("AUTHORITY", "CURVE", f"{path}.components[{index}]", "must contain finite K>=0 and S>0 on the complete wavelength grid")
        absorption.append(k_values)
        scattering.append(s_values)
    return grid, order, np.asarray(absorption, dtype=float), np.asarray(scattering, dtype=float)


def _validate_baseline(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], *, evidence_class: str
) -> tuple[np.ndarray, np.ndarray]:
    if baseline.get("schema_version") != BASELINE_MODEL_SCHEMA or baseline.get("status") != "baseline_frozen":
        _fail("AUTHORITY", "BASELINE", "baseline_model", "must be a frozen finite-film baseline")
    if _evidence_class(baseline.get("evidence_class"), "baseline_model.evidence_class", stage="AUTHORITY") != evidence_class:
        _fail("AUTHORITY", "EVIDENCE_CLASS", "baseline_model.evidence_class", "does not match preregistration")
    _assert_inactive(baseline, "baseline_model", stage="AUTHORITY")
    candidate_grid, candidate_order, _candidate_k, _candidate_s = _validate_model_curves(candidate, path="candidate_model")
    _grid, _order, baseline_k, baseline_s = _validate_model_curves(
        baseline,
        path="baseline_model",
        wavelengths=candidate_grid,
        component_order=candidate_order,
    )
    if baseline.get("concentration_basis") != "nonvolatile_volume_fraction":
        _fail("AUTHORITY", "BASELINE", "baseline_model.concentration_basis", "must be nonvolatile_volume_fraction")
    if baseline.get("candidate_predecessor_bindings") != candidate.get("predecessor_bindings"):
        _fail("AUTHORITY", "BASELINE_LINEAGE", "baseline_model.candidate_predecessor_bindings", "does not bind the current candidate lineage")
    return baseline_k, baseline_s


def _validate_repeatability(receipt: Mapping[str, Any], *, evidence_class: str) -> None:
    if receipt.get("schema_version") != REPEATABILITY_SCHEMA:
        _fail("AUTHORITY", "SCHEMA", "repeatability_receipt.schema_version", "is not supported")
    if _evidence_class(receipt.get("evidence_class"), "repeatability_receipt.evidence_class", stage="AUTHORITY") != evidence_class:
        _fail("AUTHORITY", "EVIDENCE_CLASS", "repeatability_receipt.evidence_class", "does not match preregistration")
    expected_status = "measured_complete" if evidence_class == "measured_current_batch" else "synthetic_test_only"
    if receipt.get("status") != expected_status or receipt.get("derived_without_holdout") is not True:
        _fail("AUTHORITY", "REPEATABILITY", "repeatability_receipt", "must be complete, pre-holdout, and match its evidence class")
    _assert_inactive(receipt, "repeatability_receipt", stage="AUTHORITY")
    layers = _mapping(receipt.get("layers"), "repeatability_receipt.layers")
    required = {"same_position", "reposition", "bare_backing", "dft", "inter_card", "model_baseline"}
    if set(layers) != required:
        _fail("AUTHORITY", "REPEATABILITY", "repeatability_receipt.layers", f"must contain exactly {sorted(required)}")
    for name in sorted(required):
        layer = _mapping(layers[name], f"repeatability_receipt.layers.{name}")
        sample_count = _number(
            layer.get("n"),
            f"repeatability_receipt.layers.{name}.n",
            stage="AUTHORITY",
            positive=True,
        )
        if not sample_count.is_integer() or int(sample_count) < 2:
            _fail("AUTHORITY", "REPEATABILITY", f"repeatability_receipt.layers.{name}.n", "must retain at least two observations")
        _sha256(layer.get("source_sha256"), f"repeatability_receipt.layers.{name}.source_sha256", stage="AUTHORITY")
        values = _list(layer.get("observed_values"), f"repeatability_receipt.layers.{name}.observed_values")
        if len(values) != int(sample_count):
            _fail("AUTHORITY", "REPEATABILITY", f"repeatability_receipt.layers.{name}.observed_values", "must retain all counted observations")
        for index, value in enumerate(values):
            observed = _number(value, f"repeatability_receipt.layers.{name}.observed_values[{index}]", stage="AUTHORITY")
            if observed < 0.0:
                _fail("AUTHORITY", "REPEATABILITY", f"repeatability_receipt.layers.{name}.observed_values[{index}]", "must be a non-negative observed magnitude")


THRESHOLD_KEYS = (
    "d65_de00_median_min_improvement",
    "d65_de00_p90_min_improvement",
    "spectral_rmse_median_min_improvement",
    "spectral_rmse_max_cell_degradation",
    "alternate_de00_max_cell_degradation",
    "contrast_rmse_max_card_degradation",
)


def _validate_acceptance_profile(
    profile: Mapping[str, Any], *, evidence_class: str, repeatability_sha256: str
) -> dict[str, float]:
    if profile.get("schema_version") != ACCEPTANCE_PROFILE_SCHEMA or profile.get("status") != "acceptance_profile_frozen":
        _fail("AUTHORITY", "PROFILE", "acceptance_profile", "must be a frozen acceptance profile")
    if _evidence_class(profile.get("evidence_class"), "acceptance_profile.evidence_class", stage="AUTHORITY") != evidence_class:
        _fail("AUTHORITY", "EVIDENCE_CLASS", "acceptance_profile.evidence_class", "does not match preregistration")
    if profile.get("derived_without_holdout") is not True or profile.get("repeatability_receipt_sha256") != repeatability_sha256:
        _fail("AUTHORITY", "PROFILE", "acceptance_profile", "must be derived before holdout and bind the repeatability receipt")
    _assert_inactive(profile, "acceptance_profile", stage="AUTHORITY")
    thresholds = _mapping(profile.get("thresholds"), "acceptance_profile.thresholds")
    _exact(thresholds, THRESHOLD_KEYS, "acceptance_profile.thresholds", stage="AUTHORITY")
    result: dict[str, float] = {}
    for key in THRESHOLD_KEYS:
        value = _number(thresholds.get(key), f"acceptance_profile.thresholds.{key}", stage="AUTHORITY")
        if value < 0.0:
            _fail("AUTHORITY", "PROFILE", f"acceptance_profile.thresholds.{key}", "must be a non-negative predeclared magnitude")
        result[key] = value
    return result


def _validate_colorimetry_profile(
    profile: Mapping[str, Any], *, evidence_class: str, wavelengths: Sequence[float]
) -> list[dict[str, Any]]:
    if profile.get("schema_version") != COLORIMETRY_PROFILE_SCHEMA or profile.get("status") != "colorimetry_profile_frozen":
        _fail("AUTHORITY", "COLORIMETRY", "colorimetry_profile", "must be frozen")
    if _evidence_class(profile.get("evidence_class"), "colorimetry_profile.evidence_class", stage="AUTHORITY") != evidence_class:
        _fail("AUTHORITY", "EVIDENCE_CLASS", "colorimetry_profile.evidence_class", "does not match preregistration")
    if profile.get("wavelength_nm") != list(wavelengths):
        _fail("AUTHORITY", "COLORIMETRY_GRID", "colorimetry_profile.wavelength_nm", "must match the model grid without extrapolation")
    _assert_inactive(profile, "colorimetry_profile", stage="AUTHORITY")
    conditions: list[dict[str, Any]] = []
    seen: set[str] = set()
    primary_count = 0
    for index, raw_item in enumerate(_list(profile.get("conditions"), "colorimetry_profile.conditions")):
        item = _mapping(raw_item, f"colorimetry_profile.conditions[{index}]")
        _exact(
            item,
            ("condition_id", "illuminant", "observer", "primary", "x_weight", "y_weight", "z_weight", "reference_white", "source_sha256"),
            f"colorimetry_profile.conditions[{index}]",
            stage="AUTHORITY",
        )
        condition_id = _text(item.get("condition_id"), f"colorimetry_profile.conditions[{index}].condition_id", stage="AUTHORITY")
        if condition_id in seen or item.get("observer") != "10deg":
            _fail("AUTHORITY", "COLORIMETRY", f"colorimetry_profile.conditions[{index}]", "must have a unique id and 10deg observer")
        primary = item.get("primary") is True
        illuminant = _text(item.get("illuminant"), f"colorimetry_profile.conditions[{index}].illuminant", stage="AUTHORITY")
        if primary:
            primary_count += 1
            if condition_id != "D65/10" or illuminant != "D65":
                _fail("AUTHORITY", "COLORIMETRY", f"colorimetry_profile.conditions[{index}]", "the sole primary must be D65/10")
        weights: dict[str, list[float]] = {}
        for axis in ("x", "y", "z"):
            raw_values = _list(item.get(f"{axis}_weight"), f"colorimetry_profile.conditions[{index}].{axis}_weight")
            values = [_number(value, f"colorimetry_profile.conditions[{index}].{axis}_weight[{position}]", stage="AUTHORITY") for position, value in enumerate(raw_values)]
            if len(values) != len(wavelengths) or any(value < 0.0 for value in values) or math.fsum(values) <= 0.0:
                _fail("AUTHORITY", "COLORIMETRY_GRID", f"colorimetry_profile.conditions[{index}].{axis}_weight", "must cover the complete model grid")
            weights[axis] = values
        white = [_number(value, f"colorimetry_profile.conditions[{index}].reference_white[{position}]", stage="AUTHORITY", positive=True) for position, value in enumerate(_list(item.get("reference_white"), f"colorimetry_profile.conditions[{index}].reference_white"))]
        if len(white) != 3:
            _fail("AUTHORITY", "COLORIMETRY", f"colorimetry_profile.conditions[{index}].reference_white", "must contain XYZ")
        perfect_diffuser_white = [math.fsum(weights[axis]) for axis in ("x", "y", "z")]
        if any(
            not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
            for actual, expected in zip(white, perfect_diffuser_white, strict=True)
        ):
            _fail(
                "AUTHORITY",
                "COLORIMETRY_WHITE",
                f"colorimetry_profile.conditions[{index}].reference_white",
                "must equal the supplied integration weights applied to a perfect diffuser",
            )
        _sha256(item.get("source_sha256"), f"colorimetry_profile.conditions[{index}].source_sha256", stage="AUTHORITY")
        conditions.append({"condition_id": condition_id, "illuminant": illuminant, "primary": primary, "weights": weights, "reference_white": white})
        seen.add(condition_id)
    if primary_count != 1 or len(conditions) < 2:
        _fail("AUTHORITY", "COLORIMETRY", "colorimetry_profile.conditions", "must contain D65/10 and at least one alternate 10deg condition")
    return conditions


@dataclass(frozen=True)
class _AuthorityContext:
    evidence_class: str
    candidate: Mapping[str, Any]
    candidate_k: np.ndarray
    candidate_s: np.ndarray
    baseline: Mapping[str, Any]
    baseline_k: np.ndarray
    baseline_s: np.ndarray
    wavelengths: tuple[float, ...]
    component_order: tuple[tuple[str, str], ...]
    thresholds: Mapping[str, float]
    color_conditions: tuple[Mapping[str, Any], ...]
    preregistration_payload: Mapping[str, Any]
    preregistration_sha256: str
    custody_sha256: str
    locked_conditions_sha256: str
    hashes: Mapping[str, str]
    keys: Mapping[str, _TrustedKey]
    open_identifiers: frozenset[str]


def _authority_context(
    *,
    acquisition_receipt_path: Path | str,
    admission_receipt_path: Path | str,
    dataset_root: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
    candidate_export_root: Path | str,
    baseline_model_path: Path | str,
    custody_commitment_path: Path | str,
    repeatability_receipt_path: Path | str,
    acceptance_profile_path: Path | str,
    colorimetry_profile_path: Path | str,
    trust_store_path: Path | str,
    preregistration_envelope_path: Path | str,
) -> _AuthorityContext:
    # The current open authority is deliberately checked before any sealed path
    # is accepted or opened by the evaluation entry point.
    try:
        candidate_verification = verify_open_selection_fit_export(
            acquisition_receipt_path=acquisition_receipt_path,
            admission_receipt_path=admission_receipt_path,
            dataset_root=dataset_root,
            shared_root=shared_root,
            open_root=open_root,
            measurement_root=measurement_root,
            export_root=candidate_export_root,
        )
    except CalibrationError as error:
        _fail("AUTHORITY", "CANDIDATE", "candidate_export_root", str(error))
    if candidate_verification.get("status") != "open_selection_fit_export_verified":
        _fail("AUTHORITY", "CANDIDATE", "candidate_export_root", "did not reverify")
    candidate_hashes = {
        "fit_model_sha256": _sha256(candidate_verification.get("fit_model_sha256"), "candidate.fit_model_sha256", stage="AUTHORITY"),
        "selection_evaluation_sha256": _sha256(candidate_verification.get("selection_evaluation_sha256"), "candidate.selection_evaluation_sha256", stage="AUTHORITY"),
        "fit_export_receipt_sha256": _sha256(candidate_verification.get("fit_export_receipt_sha256"), "candidate.fit_export_receipt_sha256", stage="AUTHORITY"),
    }
    candidate = _read_candidate_model(candidate_export_root, candidate_hashes["fit_model_sha256"])
    wavelengths, component_order, candidate_k, candidate_s = _validate_model_curves(candidate, path="candidate_model")
    open_dataset = load_and_validate_open_selection_dataset(dataset_root)
    open_locked_conditions = _mapping(
        open_dataset.manifest.get("locked_conditions"),
        "open_dataset.manifest.locked_conditions",
        stage="AUTHORITY",
    )
    locked_conditions_sha256 = _payload_digest(dict(open_locked_conditions))
    open_identifiers: set[str] = set()
    for card in _list(open_dataset.source.get("cards"), "open_dataset.cards"):
        item = _mapping(card, "open_dataset.cards[]")
        for key in ("card_id", "formula_family_id", "formula_id", "formula_batch_id"):
            if isinstance(item.get(key), str):
                open_identifiers.add(item[key])
    for measurement in _list(open_dataset.source.get("measurements"), "open_dataset.measurements"):
        item = _mapping(measurement, "open_dataset.measurements[]")
        if isinstance(item.get("instrument_measurement_id"), str):
            open_identifiers.add(item["instrument_measurement_id"])

    baseline, baseline_sha = _read_canonical(baseline_model_path, "baseline_model", stage="AUTHORITY")
    repeatability, repeatability_sha = _read_canonical(repeatability_receipt_path, "repeatability_receipt", stage="AUTHORITY")
    acceptance, acceptance_sha = _read_canonical(acceptance_profile_path, "acceptance_profile", stage="AUTHORITY")
    colorimetry, colorimetry_sha = _read_canonical(colorimetry_profile_path, "colorimetry_profile", stage="AUTHORITY")
    custody, custody_sha = _read_canonical(custody_commitment_path, "custody_commitment", stage="AUTHORITY")
    keys, trust_store_sha = _load_trust_store(trust_store_path)
    preregistration, preregistration_sha = _verify_envelope(
        preregistration_envelope_path,
        keys=keys,
        required_role="reviewer",
        payload_schema=PREREGISTRATION_SCHEMA,
        label="preregistration_envelope",
    )

    _exact(
        preregistration,
        (
            "schema_version", "status", "evidence_class", "created_at", "candidate",
            "baseline_model_sha256", "repeatability_receipt_sha256", "acceptance_profile_sha256",
            "colorimetry_profile_sha256", "custody_commitment_sha256", "trust_store_sha256",
            "evaluator_implementation_id", "custodian_key_fingerprint", "reviewer_key_fingerprint",
        ),
        "preregistration",
        stage="AUTHORITY",
    )
    if preregistration.get("status") != "holdout_preregistration_frozen" or preregistration.get("evaluator_implementation_id") != EVALUATOR_IMPLEMENTATION_ID:
        _fail("AUTHORITY", "PREREGISTRATION", "preregistration", "must freeze this evaluator implementation")
    evidence_class = _evidence_class(preregistration.get("evidence_class"), "preregistration.evidence_class", stage="AUTHORITY")
    if evidence_class == "measured_current_batch":
        _fail(
            "AUTHORITY",
            "MEASURED_AUTHORITY_UNAVAILABLE",
            "preregistration.evidence_class",
            "measured evaluation remains closed until physical provenance, traceable criteria, DFT-band evidence, canonical colorimetry, and a pinned external authority are implemented",
        )
    _timestamp(preregistration.get("created_at"), "preregistration.created_at", stage="AUTHORITY")
    if dict(_mapping(preregistration.get("candidate"), "preregistration.candidate")) != candidate_hashes:
        _fail("AUTHORITY", "PREREGISTRATION", "preregistration.candidate", "does not bind the current reverified candidate")
    bound_hashes = {
        "baseline_model_sha256": baseline_sha,
        "repeatability_receipt_sha256": repeatability_sha,
        "acceptance_profile_sha256": acceptance_sha,
        "colorimetry_profile_sha256": colorimetry_sha,
        "custody_commitment_sha256": custody_sha,
        "trust_store_sha256": trust_store_sha,
    }
    for key, expected in bound_hashes.items():
        if preregistration.get(key) != expected:
            _fail("AUTHORITY", "PREREGISTRATION", f"preregistration.{key}", "does not bind the supplied artifact")
    by_role = {trusted.role: trusted for trusted in keys.values()}
    if preregistration.get("custodian_key_fingerprint") != by_role["custodian"].fingerprint or preregistration.get("reviewer_key_fingerprint") != by_role["reviewer"].fingerprint:
        _fail("AUTHORITY", "PREREGISTRATION", "preregistration.*_key_fingerprint", "does not bind the distinct trusted role keys")

    baseline_k, baseline_s = _validate_baseline(baseline, candidate, evidence_class=evidence_class)
    _validate_repeatability(repeatability, evidence_class=evidence_class)
    thresholds = _validate_acceptance_profile(acceptance, evidence_class=evidence_class, repeatability_sha256=repeatability_sha)
    color_conditions = _validate_colorimetry_profile(colorimetry, evidence_class=evidence_class, wavelengths=wavelengths)
    if custody.get("schema_version") != "moocow-holdout-custody-commitment-v1" or custody.get("status") != "holdout_custody_committed" or custody.get("state") != "HOLDOUT_CUSTODY_COMMITTED":
        _fail("AUTHORITY", "CUSTODY", "custody_commitment", "is not the frozen custody commitment")
    _assert_inactive(custody, "custody_commitment", stage="AUTHORITY")
    counts = _mapping(custody.get("counts"), "custody_commitment.counts")
    if counts.get("families") != 3 or counts.get("batches") != 3 or counts.get("cards") != 9 or counts.get("primary_reading_slots") != 54:
        _fail("AUTHORITY", "CUSTODY_COUNTS", "custody_commitment.counts", "must retain the fixed 3/3/9/54 commitment")

    return _AuthorityContext(
        evidence_class=evidence_class,
        candidate=candidate,
        candidate_k=candidate_k,
        candidate_s=candidate_s,
        baseline=baseline,
        baseline_k=baseline_k,
        baseline_s=baseline_s,
        wavelengths=tuple(wavelengths),
        component_order=tuple(component_order),
        thresholds=thresholds,
        color_conditions=tuple(color_conditions),
        preregistration_payload=preregistration,
        preregistration_sha256=preregistration_sha,
        custody_sha256=custody_sha,
        locked_conditions_sha256=locked_conditions_sha256,
        hashes={
            **candidate_hashes,
            **bound_hashes,
            "locked_conditions_sha256": locked_conditions_sha256,
        },
        keys=keys,
        open_identifiers=frozenset(open_identifiers),
    )


def verify_holdout_preregistration(**kwargs: Any) -> dict[str, Any]:
    """Verify all public pre-release authorities without opening sealed input."""

    context = _authority_context(**kwargs)
    return {
        "status": "holdout_preregistration_verified",
        "state": "HOLDOUT_PREREGISTRATION_FROZEN",
        "evidence_class": context.evidence_class,
        "preregistration_envelope_sha256": context.preregistration_sha256,
        "sealed_input_opened": False,
        "activation_review_eligible": False,
        "production_pass": False,
        "runtime_compatible": False,
        **_permissions(),
    }


@dataclass(frozen=True)
class _HoldoutCell:
    family_alias: str
    formula_family_id: str
    formula_id: str
    formula_batch_id: str
    card_id: str
    dft_band: str
    backing: str
    dft_um: float
    concentrations: np.ndarray
    raw_replicates: np.ndarray
    observed: np.ndarray


@dataclass(frozen=True)
class _VerifiedRelease:
    payload: Mapping[str, Any]
    sha256: str
    sealed_input_sha256: str


@dataclass(frozen=True)
class _SealedInput:
    payload: Mapping[str, Any]
    sha256: str
    release_payload: Mapping[str, Any]
    release_sha256: str
    backings: Mapping[str, np.ndarray]
    cells: tuple[_HoldoutCell, ...]


def _reflectance(value: object, wavelengths: Sequence[float], path: str) -> np.ndarray:
    raw = _list(value, path, stage="CUSTODY_INPUT")
    if len(raw) != len(wavelengths):
        _fail("CUSTODY_INPUT", "REFLECTANCE", path, "must match the frozen wavelength grid")
    result = np.asarray(
        [_number(item, f"{path}[{index}]", stage="CUSTODY_INPUT") for index, item in enumerate(raw)],
        dtype=float,
    )
    if np.any((result < 0.0) | (result > 1.0)):
        _fail("CUSTODY_INPUT", "REFLECTANCE", path, "must remain in [0, 1]")
    return result


def _verify_release(
    context: _AuthorityContext,
    *,
    release_envelope_path: Path | str,
) -> _VerifiedRelease:
    release, release_sha = _verify_envelope(
        release_envelope_path,
        keys=context.keys,
        required_role="custodian",
        payload_schema=RELEASE_SCHEMA,
        label="release_envelope",
    )
    _exact(
        release,
        (
            "schema_version", "status", "evidence_class", "issued_at", "release_id",
            "one_time_nonce", "preregistration_envelope_sha256", "sealed_input_sha256",
            "custody_commitment_sha256", "custodian_key_fingerprint",
        ),
        "release",
        stage="AUTHORITY",
    )
    if release.get("status") != "holdout_release_authorized":
        _fail("AUTHORITY", "RELEASE", "release.status", "must be holdout_release_authorized")
    evidence_class = _evidence_class(release.get("evidence_class"), "release.evidence_class", stage="AUTHORITY")
    if evidence_class != context.evidence_class:
        _fail("AUTHORITY", "EVIDENCE_CLASS", "release.evidence_class", "does not match preregistration")
    issued_at = _timestamp(release.get("issued_at"), "release.issued_at", stage="AUTHORITY")
    preregistered_at = _timestamp(context.preregistration_payload.get("created_at"), "preregistration.created_at", stage="AUTHORITY")
    if issued_at <= preregistered_at:
        _fail("AUTHORITY", "RELEASE_TIME", "release.issued_at", "must be after preregistration freeze")
    _text(release.get("release_id"), "release.release_id", stage="AUTHORITY")
    nonce = _text(release.get("one_time_nonce"), "release.one_time_nonce", stage="AUTHORITY")
    if len(nonce) < 16:
        _fail("AUTHORITY", "RELEASE_NONCE", "release.one_time_nonce", "must be a nontrivial one-time nonce")
    if release.get("preregistration_envelope_sha256") != context.preregistration_sha256 or release.get("custody_commitment_sha256") != context.custody_sha256:
        _fail("AUTHORITY", "RELEASE_BINDING", "release", "does not bind the current preregistration and custody commitment")
    custodian = next(key for key in context.keys.values() if key.role == "custodian")
    if release.get("custodian_key_fingerprint") != custodian.fingerprint:
        _fail("AUTHORITY", "RELEASE_BINDING", "release.custodian_key_fingerprint", "does not bind the pinned custodian key")
    sealed_input_sha256 = _sha256(
        release.get("sealed_input_sha256"),
        "release.sealed_input_sha256",
        stage="AUTHORITY",
    )
    return _VerifiedRelease(release, release_sha, sealed_input_sha256)


def _release_and_sealed_input(
    context: _AuthorityContext,
    *,
    release_envelope_path: Path | str,
    sealed_input_path: Path | str,
    verified_release: _VerifiedRelease | None = None,
) -> _SealedInput:
    release_authority = verified_release or _verify_release(
        context, release_envelope_path=release_envelope_path
    )
    release = release_authority.payload
    release_sha = release_authority.sha256

    # This is the first sealed read in the evaluation entry point.
    sealed, sealed_sha = _read_canonical(sealed_input_path, "sealed_input", stage="CUSTODY_INPUT")
    if release_authority.sealed_input_sha256 != sealed_sha:
        _fail("CUSTODY_INPUT", "RELEASE_BINDING", "sealed_input", "does not match the custodian-signed release")
    _exact(
        sealed,
        (
            "schema_version", "evidence_class", "release_id", "wavelength_nm",
            "locked_conditions_sha256", "component_order", "backings", "cells",
            "evidence_manifest_sha256",
        ),
        "sealed_input",
        stage="CUSTODY_INPUT",
    )
    if sealed.get("schema_version") != SEALED_INPUT_SCHEMA or sealed.get("release_id") != release.get("release_id"):
        _fail("CUSTODY_INPUT", "SCHEMA", "sealed_input", "does not match the signed release schema/id")
    if _evidence_class(sealed.get("evidence_class"), "sealed_input.evidence_class", stage="CUSTODY_INPUT") != context.evidence_class:
        _fail("CUSTODY_INPUT", "EVIDENCE_CLASS", "sealed_input.evidence_class", "does not match the signed release")
    if sealed.get("wavelength_nm") != list(context.wavelengths):
        _fail("CUSTODY_INPUT", "WAVELENGTH", "sealed_input.wavelength_nm", "does not match the frozen candidate grid")
    sealed_conditions_sha256 = _sha256(
        sealed.get("locked_conditions_sha256"),
        "sealed_input.locked_conditions_sha256",
        stage="CUSTODY_INPUT",
    )
    if sealed_conditions_sha256 != context.locked_conditions_sha256:
        _fail(
            "CUSTODY_INPUT",
            "LOCKED_CONDITIONS",
            "sealed_input.locked_conditions_sha256",
            "does not match the current reverified open measurement conditions",
        )
    evidence_manifest_sha256 = _sha256(
        sealed.get("evidence_manifest_sha256"),
        "sealed_input.evidence_manifest_sha256",
        stage="CUSTODY_INPUT",
    )
    seen_evidence_hashes: set[str] = {evidence_manifest_sha256}

    raw_order = _list(sealed.get("component_order"), "sealed_input.component_order", stage="CUSTODY_INPUT")
    order: list[tuple[str, str]] = []
    for index, raw_item in enumerate(raw_order):
        item = _mapping(raw_item, f"sealed_input.component_order[{index}]", stage="CUSTODY_INPUT")
        _exact(item, ("component_id", "physical_lot_id"), f"sealed_input.component_order[{index}]", stage="CUSTODY_INPUT")
        order.append((item.get("component_id"), item.get("physical_lot_id")))
    if order != list(context.component_order):
        _fail("CUSTODY_INPUT", "COMPONENT_ORDER", "sealed_input.component_order", "does not match the frozen candidate component/lot order")

    raw_backings = _mapping(sealed.get("backings"), "sealed_input.backings", stage="CUSTODY_INPUT")
    _exact(raw_backings, BACKINGS, "sealed_input.backings", stage="CUSTODY_INPUT")
    backings: dict[str, np.ndarray] = {}
    for backing in BACKINGS:
        item = _mapping(raw_backings[backing], f"sealed_input.backings.{backing}", stage="CUSTODY_INPUT")
        _exact(item, ("mean_reflectance", "source_sha256"), f"sealed_input.backings.{backing}", stage="CUSTODY_INPUT")
        backings[backing] = _reflectance(item.get("mean_reflectance"), context.wavelengths, f"sealed_input.backings.{backing}.mean_reflectance")
        source_sha = _sha256(item.get("source_sha256"), f"sealed_input.backings.{backing}.source_sha256", stage="CUSTODY_INPUT")
        if source_sha in seen_evidence_hashes:
            _fail("CUSTODY_INPUT", "EVIDENCE_REUSE", f"sealed_input.backings.{backing}.source_sha256", "reuses sealed evidence bytes")
        seen_evidence_hashes.add(source_sha)
    if np.array_equal(backings["black"], backings["white"]):
        _fail("CUSTODY_INPUT", "BACKING", "sealed_input.backings", "black and white backing means must be distinct")

    expected_aliases = {family: f"H{index:02d}" for index, family in enumerate(HOLDOUT_FAMILIES, start=1)}
    raw_cells = _list(sealed.get("cells"), "sealed_input.cells", stage="CUSTODY_INPUT")
    if len(raw_cells) != EXPECTED_COUNTS["cells"]:
        _fail("CUSTODY_INPUT", "CELL_COUNT", "sealed_input.cells", "must contain exactly 18 card/backing cells")
    cells: list[_HoldoutCell] = []
    seen_cell_keys: set[tuple[str, str]] = set()
    seen_measurement_ids: set[str] = set()
    card_records: dict[str, tuple[str, str, str, str, str]] = {}
    reading_count = 0
    allow_synthetic_identifiers = context.evidence_class == "synthetic_test_only"
    for index, raw_item in enumerate(raw_cells):
        item = _mapping(raw_item, f"sealed_input.cells[{index}]", stage="CUSTODY_INPUT")
        _exact(
            item,
            (
                "family_alias", "formula_family_id", "formula_id", "formula_batch_id",
                "card_id", "dft_band", "backing", "dft_um", "components", "readings",
                "evidence_sha256",
            ),
            f"sealed_input.cells[{index}]",
            stage="CUSTODY_INPUT",
        )
        family = _text(item.get("formula_family_id"), f"sealed_input.cells[{index}].formula_family_id", stage="CUSTODY_INPUT")
        alias = _text(item.get("family_alias"), f"sealed_input.cells[{index}].family_alias", stage="CUSTODY_INPUT")
        if family not in expected_aliases or alias != expected_aliases[family]:
            _fail("CUSTODY_INPUT", "HOLDOUT_FAMILY", f"sealed_input.cells[{index}]", "must use the fixed holdout family and public alias")
        formula_id = _text(item.get("formula_id"), f"sealed_input.cells[{index}].formula_id", stage="CUSTODY_INPUT", allow_synthetic=allow_synthetic_identifiers)
        batch_id = _text(item.get("formula_batch_id"), f"sealed_input.cells[{index}].formula_batch_id", stage="CUSTODY_INPUT", allow_synthetic=allow_synthetic_identifiers)
        card_id = _text(item.get("card_id"), f"sealed_input.cells[{index}].card_id", stage="CUSTODY_INPUT", allow_synthetic=allow_synthetic_identifiers)
        backing = item.get("backing")
        dft_band = item.get("dft_band")
        if backing not in BACKINGS or dft_band not in DFT_BANDS:
            _fail("CUSTODY_INPUT", "ROSTER", f"sealed_input.cells[{index}]", "must retain black/white and DFT-L/M/H")
        identifiers = (family, formula_id, batch_id, card_id)
        if any(identifier in context.open_identifiers for identifier in identifiers):
            _fail("CUSTODY_INPUT", "SPLIT_LEAKAGE", f"sealed_input.cells[{index}]", "reuses an open train/validation identity")
        key = (card_id, str(backing))
        if key in seen_cell_keys:
            _fail("CUSTODY_INPUT", "CELL_DUPLICATE", f"sealed_input.cells[{index}]", "duplicates a card/backing cell")
        seen_cell_keys.add(key)
        card_identity = (family, alias, formula_id, batch_id, str(dft_band))
        previous = card_records.get(card_id)
        if previous is not None and previous != card_identity:
            _fail("CUSTODY_INPUT", "CARD_ID", f"sealed_input.cells[{index}].card_id", "is reused with different lineage")
        card_records[card_id] = card_identity
        dft_um = _number(item.get("dft_um"), f"sealed_input.cells[{index}].dft_um", stage="CUSTODY_INPUT", positive=True)

        components = _list(item.get("components"), f"sealed_input.cells[{index}].components", stage="CUSTODY_INPUT")
        if len(components) != len(context.component_order):
            _fail("CUSTODY_INPUT", "COMPONENT_COUNT", f"sealed_input.cells[{index}].components", "must match component_order")
        concentrations: list[float] = []
        for component_index, raw_component in enumerate(components):
            component = _mapping(raw_component, f"sealed_input.cells[{index}].components[{component_index}]", stage="CUSTODY_INPUT")
            _exact(component, ("component_id", "physical_lot_id", "nonvolatile_volume_fraction"), f"sealed_input.cells[{index}].components[{component_index}]", stage="CUSTODY_INPUT")
            if (component.get("component_id"), component.get("physical_lot_id")) != context.component_order[component_index]:
                _fail("CUSTODY_INPUT", "COMPONENT_ORDER", f"sealed_input.cells[{index}].components[{component_index}]", "does not match the frozen component/lot order")
            fraction = _number(component.get("nonvolatile_volume_fraction"), f"sealed_input.cells[{index}].components[{component_index}].nonvolatile_volume_fraction", stage="CUSTODY_INPUT")
            if fraction < 0.0 or fraction > 1.0:
                _fail("CUSTODY_INPUT", "CONCENTRATION", f"sealed_input.cells[{index}].components[{component_index}]", "must remain in [0, 1]")
            concentrations.append(fraction)
        if not math.isclose(math.fsum(concentrations), 1.0, rel_tol=0.0, abs_tol=1e-12):
            _fail("CUSTODY_INPUT", "CONCENTRATION", f"sealed_input.cells[{index}].components", "must sum to one")

        readings = _list(item.get("readings"), f"sealed_input.cells[{index}].readings", stage="CUSTODY_INPUT")
        if len(readings) != 3:
            _fail("CUSTODY_INPUT", "REPOSITION_COUNT", f"sealed_input.cells[{index}].readings", "must contain exactly POS01/POS02/POS03")
        by_position: dict[str, np.ndarray] = {}
        for reading_index, raw_reading in enumerate(readings):
            reading = _mapping(raw_reading, f"sealed_input.cells[{index}].readings[{reading_index}]", stage="CUSTODY_INPUT")
            _exact(reading, ("reposition_id", "instrument_measurement_id", "reflectance", "source_sha256"), f"sealed_input.cells[{index}].readings[{reading_index}]", stage="CUSTODY_INPUT")
            position = reading.get("reposition_id")
            measurement_id = _text(reading.get("instrument_measurement_id"), f"sealed_input.cells[{index}].readings[{reading_index}].instrument_measurement_id", stage="CUSTODY_INPUT", allow_synthetic=allow_synthetic_identifiers)
            source_sha = _sha256(reading.get("source_sha256"), f"sealed_input.cells[{index}].readings[{reading_index}].source_sha256", stage="CUSTODY_INPUT")
            if position not in POSITIONS or position in by_position:
                _fail("CUSTODY_INPUT", "REPOSITION", f"sealed_input.cells[{index}].readings[{reading_index}]", "must contain each fixed reposition once")
            if measurement_id in seen_measurement_ids or measurement_id in context.open_identifiers:
                _fail("CUSTODY_INPUT", "MEASUREMENT_ID", f"sealed_input.cells[{index}].readings[{reading_index}]", "reuses an open or sealed identity")
            if source_sha in seen_evidence_hashes:
                _fail("CUSTODY_INPUT", "EVIDENCE_REUSE", f"sealed_input.cells[{index}].readings[{reading_index}]", "reuses raw evidence bytes")
            seen_measurement_ids.add(measurement_id)
            seen_evidence_hashes.add(source_sha)
            by_position[str(position)] = _reflectance(reading.get("reflectance"), context.wavelengths, f"sealed_input.cells[{index}].readings[{reading_index}].reflectance")
            reading_count += 1
        if set(by_position) != set(POSITIONS):
            _fail("CUSTODY_INPUT", "REPOSITION", f"sealed_input.cells[{index}].readings", "must contain exactly POS01/POS02/POS03")
        cell_evidence_sha = _sha256(item.get("evidence_sha256"), f"sealed_input.cells[{index}].evidence_sha256", stage="CUSTODY_INPUT")
        if cell_evidence_sha in seen_evidence_hashes:
            _fail("CUSTODY_INPUT", "EVIDENCE_REUSE", f"sealed_input.cells[{index}].evidence_sha256", "reuses sealed evidence bytes")
        seen_evidence_hashes.add(cell_evidence_sha)
        raw_replicates = np.vstack([by_position[position] for position in POSITIONS])
        cells.append(
            _HoldoutCell(
                family_alias=alias,
                formula_family_id=family,
                formula_id=formula_id,
                formula_batch_id=batch_id,
                card_id=card_id,
                dft_band=str(dft_band),
                backing=str(backing),
                dft_um=dft_um,
                concentrations=np.asarray(concentrations, dtype=float),
                raw_replicates=raw_replicates,
                observed=np.mean(raw_replicates, axis=0),
            )
        )

    if len(card_records) != EXPECTED_COUNTS["cards"] or reading_count != EXPECTED_COUNTS["readings"]:
        _fail("CUSTODY_INPUT", "ROSTER_COUNT", "sealed_input", "must contain exactly 9 cards and 54 readings")
    family_lineages: set[tuple[str, str]] = set()
    for family, alias in expected_aliases.items():
        family_cards = {cell.card_id: cell for cell in cells if cell.formula_family_id == family}
        if len(family_cards) != 3 or {cell.dft_band for cell in family_cards.values()} != set(DFT_BANDS):
            _fail("CUSTODY_INPUT", "FAMILY_ROSTER", family, "must contain one card at each DFT band")
        lineages = {(cell.formula_id, cell.formula_batch_id) for cell in cells if cell.formula_family_id == family}
        if len(lineages) != 1:
            _fail("CUSTODY_INPUT", "FAMILY_LINEAGE", family, "must retain one frozen formula and formula batch")
        family_lineages.update(lineages)
        family_vectors = [cell.concentrations for cell in cells if cell.formula_family_id == family]
        if any(not np.array_equal(family_vectors[0], vector) for vector in family_vectors[1:]):
            _fail("CUSTODY_INPUT", "FORMULA_BINDING", family, "all DFT/backing cells must retain the same actual-NV formula")
        for cell in family_cards.values():
            paired = [item for item in cells if item.card_id == cell.card_id]
            if {item.backing for item in paired} != set(BACKINGS):
                _fail("CUSTODY_INPUT", "BACKING_ROSTER", cell.card_id, "must contain both black and white cells")
            if len(paired) != 2 or not np.array_equal(paired[0].concentrations, paired[1].concentrations):
                _fail("CUSTODY_INPUT", "FORMULA_BINDING", cell.card_id, "black and white cells must retain the same actual-NV formula")
        for backing in BACKINGS:
            by_band = {
                item.dft_band: item.dft_um
                for item in cells
                if item.formula_family_id == family and item.backing == backing
            }
            values = [by_band[band] for band in DFT_BANDS]
            if any(right <= left for left, right in zip(values, values[1:])):
                _fail("CUSTODY_INPUT", "DFT_ORDER", f"{family}.{backing}", "must preserve measured DFT-L < DFT-M < DFT-H")
    if len(family_lineages) != EXPECTED_COUNTS["families"]:
        _fail("CUSTODY_INPUT", "FAMILY_LINEAGE", "sealed_input.cells", "each holdout family must retain a distinct formula batch")
    ordered = tuple(sorted(cells, key=lambda cell: (cell.family_alias, DFT_BANDS.index(cell.dft_band), BACKINGS.index(cell.backing))))
    return _SealedInput(sealed, sealed_sha, release, release_sha, backings, ordered)


def _finite_film_reflectance(
    absorption: np.ndarray, scattering: np.ndarray, thickness_mm: float, backing: np.ndarray
) -> np.ndarray:
    if (
        absorption.ndim != 1
        or scattering.shape != absorption.shape
        or backing.shape != absorption.shape
        or not np.all(np.isfinite(absorption))
        or not np.all(np.isfinite(scattering))
        or not np.all(np.isfinite(backing))
        or np.any(absorption < 0.0)
        or np.any(scattering <= 0.0)
        or thickness_mm <= 0.0
        or np.any((backing < 0.0) | (backing > 1.0))
    ):
        _fail("RECONSTRUCTION", "PREDICTION", "finite_film", "received invalid finite-film inputs")
    ratio = absorption / scattering
    optical_thickness = scattering * thickness_mm
    a = 1.0 + ratio
    b_squared = ratio * (ratio + 2.0)
    b = np.sqrt(b_squared)
    z = b * optical_thickness
    small = np.abs(z) < 1e-5
    u = np.empty_like(z)
    if np.any(small):
        q = optical_thickness[small]
        b2 = b_squared[small]
        u[small] = 1.0 / q + b2 * q / 3.0 - (b2**2) * q**3 / 45.0 + 2.0 * (b2**3) * q**5 / 945.0
    if np.any(~small):
        z_large = z[~small]
        coth = np.ones_like(z_large)
        regular = z_large <= 20.0
        coth[regular] = 1.0 / np.tanh(z_large[regular])
        u[~small] = b[~small] * coth
    numerator = 1.0 - backing * (a - u)
    denominator = a - backing + u
    if np.any(denominator <= 0.0) or not np.all(np.isfinite(denominator)):
        _fail("RECONSTRUCTION", "PREDICTION", "finite_film", "produced an invalid denominator")
    result = numerator / denominator
    if not np.all(np.isfinite(result)) or np.any((result < -ROUND_TOLERANCE) | (result > 1.0 + ROUND_TOLERANCE)):
        _fail("RECONSTRUCTION", "PREDICTION", "finite_film", "produced reflectance outside [0, 1]")
    return np.clip(result, 0.0, 1.0)


def _predict_cell(cell: _HoldoutCell, k_curves: np.ndarray, s_curves: np.ndarray, backing: np.ndarray) -> np.ndarray:
    absorption = cell.concentrations @ k_curves
    scattering = cell.concentrations @ s_curves
    return _finite_film_reflectance(absorption, scattering, cell.dft_um / 1000.0, backing)


def _spectral_metrics(predicted: np.ndarray, observed: np.ndarray) -> dict[str, float]:
    error = predicted - observed
    absolute = np.abs(error)
    return {
        "rmse": float(np.sqrt(np.mean(error**2))),
        "mae": float(np.mean(absolute)),
        "p95_abs": float(np.quantile(absolute, 0.95, method="higher")),
        "max_abs": float(np.max(absolute)),
    }


def _xyz_to_lab(xyz: Sequence[float], white: Sequence[float]) -> tuple[float, float, float]:
    delta = 6.0 / 29.0

    def transform(value: float) -> float:
        return value ** (1.0 / 3.0) if value > delta**3 else value / (3.0 * delta**2) + 4.0 / 29.0

    scaled = [float(value) / float(reference) for value, reference in zip(xyz, white, strict=True)]
    fx, fy, fz = (transform(value) for value in scaled)
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def _spectrum_to_lab(reflectance: np.ndarray, condition: Mapping[str, Any]) -> tuple[float, float, float]:
    weights = _mapping(condition["weights"], "condition.weights", stage="RECONSTRUCTION")
    xyz = [float(np.dot(reflectance, np.asarray(weights[axis], dtype=float))) for axis in ("x", "y", "z")]
    if any(not math.isfinite(value) or value < 0.0 for value in xyz):
        _fail("RECONSTRUCTION", "COLORIMETRY", str(condition["condition_id"]), "produced invalid XYZ")
    return _xyz_to_lab(xyz, condition["reference_white"])


def _delta_e_2000(lab1: Sequence[float], lab2: Sequence[float]) -> float:
    l1, a1, b1 = (float(value) for value in lab1)
    l2, a2, b2 = (float(value) for value in lab2)
    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    average_c = (c1 + c2) / 2.0
    average_c7 = average_c**7
    g = 0.5 * (1.0 - math.sqrt(average_c7 / (average_c7 + 25.0**7)))
    a1p = (1.0 + g) * a1
    a2p = (1.0 + g) * a2
    c1p = math.hypot(a1p, b1)
    c2p = math.hypot(a2p, b2)

    def hue(bb: float, aa: float) -> float:
        if bb == 0.0 and aa == 0.0:
            return 0.0
        return math.degrees(math.atan2(bb, aa)) % 360.0

    h1p = hue(b1, a1p)
    h2p = hue(b2, a2p)
    delta_lp = l2 - l1
    delta_cp = c2p - c1p
    delta_hp_angle = 0.0
    if c1p * c2p != 0.0:
        difference = h2p - h1p
        if abs(difference) <= 180.0:
            delta_hp_angle = difference
        else:
            delta_hp_angle = difference - 360.0 if difference > 180.0 else difference + 360.0
    delta_hp = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(delta_hp_angle / 2.0))
    average_lp = (l1 + l2) / 2.0
    average_cp = (c1p + c2p) / 2.0
    if c1p * c2p == 0.0:
        average_hp = h1p + h2p
    elif abs(h1p - h2p) <= 180.0:
        average_hp = (h1p + h2p) / 2.0
    else:
        average_hp = (h1p + h2p + (360.0 if h1p + h2p < 360.0 else -360.0)) / 2.0
    t = (
        1.0
        - 0.17 * math.cos(math.radians(average_hp - 30.0))
        + 0.24 * math.cos(math.radians(2.0 * average_hp))
        + 0.32 * math.cos(math.radians(3.0 * average_hp + 6.0))
        - 0.20 * math.cos(math.radians(4.0 * average_hp - 63.0))
    )
    delta_theta = 30.0 * math.exp(-((average_hp - 275.0) / 25.0) ** 2)
    average_cp7 = average_cp**7
    rc = 2.0 * math.sqrt(average_cp7 / (average_cp7 + 25.0**7))
    sl = 1.0 + 0.015 * (average_lp - 50.0) ** 2 / math.sqrt(20.0 + (average_lp - 50.0) ** 2)
    sc = 1.0 + 0.045 * average_cp
    sh = 1.0 + 0.015 * average_cp * t
    rt = -math.sin(math.radians(2.0 * delta_theta)) * rc
    return math.sqrt(
        (delta_lp / sl) ** 2
        + (delta_cp / sc) ** 2
        + (delta_hp / sh) ** 2
        + rt * (delta_cp / sc) * (delta_hp / sh)
    )


def _nearest_rank_p90(values: Sequence[float]) -> float:
    if not values:
        _fail("RECONSTRUCTION", "AGGREGATION", "p90", "requires at least one value")
    ordered = sorted(float(value) for value in values)
    return ordered[math.ceil(0.90 * len(ordered)) - 1]


def _aggregate(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        _fail("RECONSTRUCTION", "AGGREGATION", "metrics", "requires at least one value")
    return {
        "n": len(values),
        "median": float(statistics.median(values)),
        "p90_nearest_rank": _nearest_rank_p90(values),
        "max": float(max(values)),
    }


def _cell_evaluations(context: _AuthorityContext, sealed: _SealedInput) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for cell in sealed.cells:
        backing = sealed.backings[cell.backing]
        candidate_prediction = _predict_cell(cell, context.candidate_k, context.candidate_s, backing)
        baseline_prediction = _predict_cell(cell, context.baseline_k, context.baseline_s, backing)
        candidate_spectral = _spectral_metrics(candidate_prediction, cell.observed)
        baseline_spectral = _spectral_metrics(baseline_prediction, cell.observed)
        color: dict[str, Any] = {}
        for condition in context.color_conditions:
            observed_lab = _spectrum_to_lab(cell.observed, condition)
            candidate_lab = _spectrum_to_lab(candidate_prediction, condition)
            baseline_lab = _spectrum_to_lab(baseline_prediction, condition)
            color[str(condition["condition_id"])] = {
                "candidate_de00": _delta_e_2000(observed_lab, candidate_lab),
                "baseline_de00": _delta_e_2000(observed_lab, baseline_lab),
            }
        wavelength_sd = np.std(cell.raw_replicates, axis=0, ddof=1)
        replicate_rmse = {
            position: float(np.sqrt(np.mean((cell.raw_replicates[index] - cell.observed) ** 2)))
            for index, position in enumerate(POSITIONS)
        }
        results.append(
            {
                "family_alias": cell.family_alias,
                "formula_family_id": cell.formula_family_id,
                "formula_id": cell.formula_id,
                "formula_batch_id": cell.formula_batch_id,
                "card_id": cell.card_id,
                "dft_band": cell.dft_band,
                "backing": cell.backing,
                "dft_um": cell.dft_um,
                "reposition_dispersion": {
                    "median_wavelength_sd": float(statistics.median(wavelength_sd.tolist())),
                    "p90_wavelength_sd": _nearest_rank_p90(wavelength_sd.tolist()),
                    "replicate_rmse_to_cell_mean": replicate_rmse,
                },
                "candidate": {"spectral": candidate_spectral, "de00": {key: value["candidate_de00"] for key, value in color.items()}},
                "baseline": {"spectral": baseline_spectral, "de00": {key: value["baseline_de00"] for key, value in color.items()}},
                "delta": {
                    "spectral_rmse_improvement": baseline_spectral["rmse"] - candidate_spectral["rmse"],
                    "de00_improvement": {key: value["baseline_de00"] - value["candidate_de00"] for key, value in color.items()},
                },
                "observed_mean_reflectance": cell.observed.tolist(),
                "candidate_predicted_reflectance": candidate_prediction.tolist(),
                "baseline_predicted_reflectance": baseline_prediction.tolist(),
            }
        )
    return results


def _comparative_error_summary(
    candidate_values: Sequence[float], baseline_values: Sequence[float]
) -> dict[str, Any]:
    if len(candidate_values) != len(baseline_values) or not candidate_values:
        _fail("RECONSTRUCTION", "AGGREGATION", "comparative_error", "requires paired non-empty values")
    candidate = [float(value) for value in candidate_values]
    baseline = [float(value) for value in baseline_values]
    candidate_aggregate = _aggregate(candidate)
    baseline_aggregate = _aggregate(baseline)
    return {
        "candidate": candidate_aggregate,
        "baseline": baseline_aggregate,
        "median_improvement": float(baseline_aggregate["median"] - candidate_aggregate["median"]),
        "p90_improvement": float(
            baseline_aggregate["p90_nearest_rank"]
            - candidate_aggregate["p90_nearest_rank"]
        ),
        "max_unit_degradation": max(
            candidate_value - baseline_value
            for candidate_value, baseline_value in zip(candidate, baseline, strict=True)
        ),
    }


def _cell_strata(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    dimensions = {
        "by_family_alias": "family_alias",
        "by_dft_band": "dft_band",
        "by_backing": "backing",
    }
    result: dict[str, Any] = {}
    for output_name, field in dimensions.items():
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for cell in cells:
            groups.setdefault(str(cell[field]), []).append(cell)
        result[output_name] = {}
        for group_name, group_cells in sorted(groups.items()):
            spectral = _comparative_error_summary(
                [float(cell["candidate"]["spectral"]["rmse"]) for cell in group_cells],
                [float(cell["baseline"]["spectral"]["rmse"]) for cell in group_cells],
            )
            condition_ids = sorted(group_cells[0]["candidate"]["de00"])
            result[output_name][group_name] = {
                "spectral_rmse": spectral,
                "de00": {
                    condition_id: _comparative_error_summary(
                        [float(cell["candidate"]["de00"][condition_id]) for cell in group_cells],
                        [float(cell["baseline"]["de00"][condition_id]) for cell in group_cells],
                    )
                    for condition_id in condition_ids
                },
            }
    return result


def _contrast_strata(contrasts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for output_name, field in (("by_family_alias", "family_alias"), ("by_dft_band", "dft_band")):
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for contrast in contrasts:
            groups.setdefault(str(contrast[field]), []).append(contrast)
        result[output_name] = {
            group_name: _comparative_error_summary(
                [float(item["candidate_rmse"]) for item in group],
                [float(item["baseline_rmse"]) for item in group],
            )
            for group_name, group in sorted(groups.items())
        }
    return result


def _contrast_evaluations(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Mapping[str, Any]]] = {}
    for cell in cells:
        grouped.setdefault((str(cell["family_alias"]), str(cell["card_id"]), str(cell["dft_band"])), {})[str(cell["backing"])] = cell
    results: list[dict[str, Any]] = []
    for (alias, card_id, dft_band), pair in sorted(grouped.items()):
        if set(pair) != set(BACKINGS):
            _fail("RECONSTRUCTION", "CONTRAST", card_id, "requires paired black/white cells")
        observed = np.asarray(pair["black"]["observed_mean_reflectance"], dtype=float) - np.asarray(pair["white"]["observed_mean_reflectance"], dtype=float)
        candidate = np.asarray(pair["black"]["candidate_predicted_reflectance"], dtype=float) - np.asarray(pair["white"]["candidate_predicted_reflectance"], dtype=float)
        baseline = np.asarray(pair["black"]["baseline_predicted_reflectance"], dtype=float) - np.asarray(pair["white"]["baseline_predicted_reflectance"], dtype=float)
        candidate_rmse = float(np.sqrt(np.mean((candidate - observed) ** 2)))
        baseline_rmse = float(np.sqrt(np.mean((baseline - observed) ** 2)))
        results.append(
            {
                "family_alias": alias,
                "card_id": card_id,
                "dft_band": dft_band,
                "candidate_rmse": candidate_rmse,
                "baseline_rmse": baseline_rmse,
                "improvement": baseline_rmse - candidate_rmse,
            }
        )
    return results


def _decision(
    context: _AuthorityContext, cells: Sequence[Mapping[str, Any]], contrasts: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_rmse = [float(cell["candidate"]["spectral"]["rmse"]) for cell in cells]
    baseline_rmse = [float(cell["baseline"]["spectral"]["rmse"]) for cell in cells]
    spectral_summary = _comparative_error_summary(candidate_rmse, baseline_rmse)
    spectral_summary["max_cell_degradation"] = spectral_summary.pop("max_unit_degradation")
    conditions: dict[str, Any] = {}
    for condition in context.color_conditions:
        condition_id = str(condition["condition_id"])
        candidate_values = [float(cell["candidate"]["de00"][condition_id]) for cell in cells]
        baseline_values = [float(cell["baseline"]["de00"][condition_id]) for cell in cells]
        condition_summary = _comparative_error_summary(candidate_values, baseline_values)
        condition_summary["max_cell_degradation"] = condition_summary.pop("max_unit_degradation")
        conditions[condition_id] = condition_summary
    contrast_candidate = [float(item["candidate_rmse"]) for item in contrasts]
    contrast_baseline = [float(item["baseline_rmse"]) for item in contrasts]
    contrast_summary = _comparative_error_summary(contrast_candidate, contrast_baseline)
    contrast_summary["max_card_degradation"] = contrast_summary.pop("max_unit_degradation")
    aggregate = {
        "spectral_rmse": spectral_summary,
        "de00": conditions,
        "black_white_contrast_rmse": contrast_summary,
        "cell_strata": _cell_strata(cells),
        "black_white_contrast_strata": _contrast_strata(contrasts),
    }
    primary = conditions["D65/10"]
    alternate_ids = [str(condition["condition_id"]) for condition in context.color_conditions if not condition["primary"]]
    outcomes = {
        "d65_de00_median_improvement": primary["median_improvement"] >= context.thresholds["d65_de00_median_min_improvement"],
        "d65_de00_p90_improvement": primary["p90_improvement"] >= context.thresholds["d65_de00_p90_min_improvement"],
        "spectral_rmse_median_improvement": aggregate["spectral_rmse"]["median_improvement"] >= context.thresholds["spectral_rmse_median_min_improvement"],
        "spectral_rmse_no_cell_degradation": aggregate["spectral_rmse"]["max_cell_degradation"] <= context.thresholds["spectral_rmse_max_cell_degradation"],
        "alternate_illuminant_no_cell_degradation": all(conditions[condition_id]["max_cell_degradation"] <= context.thresholds["alternate_de00_max_cell_degradation"] for condition_id in alternate_ids),
        "black_white_contrast_no_card_degradation": aggregate["black_white_contrast_rmse"]["max_card_degradation"] <= context.thresholds["contrast_rmse_max_card_degradation"],
    }
    return aggregate, {"all_pass": all(outcomes.values()), "outcomes": outcomes}


def _self_bind(value: dict[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result[field] = ""
    payload = dict(result)
    payload.pop(field)
    result[field] = _payload_digest(payload)
    return result


def _evaluation_objects(
    context: _AuthorityContext, sealed: _SealedInput, *, replay_protected: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    cells = _cell_evaluations(context, sealed)
    contrasts = _contrast_evaluations(cells)
    aggregate, criteria_decision = _decision(context, cells, contrasts)
    profile_would_pass = bool(criteria_decision["all_pass"])
    if context.evidence_class != "synthetic_test_only":
        _fail(
            "ACTIVATION_DECISION",
            "MEASURED_AUTHORITY_UNAVAILABLE",
            "evaluation.evidence_class",
            "only non-activating synthetic software validation is implemented",
        )
    verdict = "INDETERMINATE"
    state = "SYNTHETIC_EVALUATION_ONLY"
    activation_review_eligible = False
    decision = {
        "all_pass": False,
        "criteria_would_pass": profile_would_pass,
        "outcomes": criteria_decision["outcomes"],
    }

    bindings = {
        **dict(context.hashes),
        "preregistration_envelope_sha256": context.preregistration_sha256,
        "release_envelope_sha256": sealed.release_sha256,
        "sealed_input_sha256": sealed.sha256,
        "evidence_manifest_sha256": sealed.payload["evidence_manifest_sha256"],
        "evaluator_implementation_id": EVALUATOR_IMPLEMENTATION_ID,
    }
    detail = _self_bind(
        {
            "schema_version": DETAIL_SCHEMA,
            "status": "independent_holdout_evaluated",
            "state": state,
            "evidence_class": context.evidence_class,
            "verdict": verdict,
            "profile_would_pass": profile_would_pass,
            "activation_review_eligible": activation_review_eligible,
            "release_replay_protected": replay_protected,
            "production_pass": False,
            "runtime_compatible": False,
            **_permissions(),
            "counts": dict(EXPECTED_COUNTS),
            "bindings": bindings,
            "thresholds": dict(context.thresholds),
            "aggregate_metrics": aggregate,
            "decision": decision,
            "cells": cells,
            "black_white_contrast": contrasts,
            "detail_payload_sha256": "",
        },
        "detail_payload_sha256",
    )

    public_cells = [
        {
            "family_alias": cell["family_alias"],
            "dft_band": cell["dft_band"],
            "backing": cell["backing"],
            "candidate": copy.deepcopy(cell["candidate"]),
            "baseline": copy.deepcopy(cell["baseline"]),
            "delta": copy.deepcopy(cell["delta"]),
        }
        for cell in cells
    ]
    public_contrasts = [
        {key: value for key, value in contrast.items() if key != "card_id"}
        for contrast in contrasts
    ]
    receipt = _self_bind(
        {
            "schema_version": REVIEW_RECEIPT_SCHEMA,
            "status": "independent_holdout_evaluated",
            "state": state,
            "evidence_class": context.evidence_class,
            "verdict": verdict,
            "activation_review_eligible": activation_review_eligible,
            "release_replay_protected": replay_protected,
            "production_pass": False,
            "runtime_compatible": False,
            **_permissions(),
            "counts": dict(EXPECTED_COUNTS),
            "bindings": {
                **bindings,
                "sealed_detail_payload_sha256": detail["detail_payload_sha256"],
            },
            "aggregate_metrics": aggregate,
            "decision": decision,
            "alias_cells": public_cells,
            "alias_black_white_contrast": public_contrasts,
            "receipt_payload_sha256": "",
        },
        "receipt_payload_sha256",
    )
    return detail, receipt


def _is_reparse(path: Path) -> bool:
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & WINDOWS_REPARSE_POINT)
    except OSError:
        return False


def _validate_no_reparse_ancestors(
    path: Path | str, *, stage: str, code: str
) -> Path:
    candidate = Path(path).absolute()
    for component in [*reversed(candidate.parents), candidate]:
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            _fail(stage, code, str(component), str(error))
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_POINT
        ):
            _fail(
                stage,
                code,
                str(component),
                "path and every existing ancestor must be free of symlinks and reparse points",
            )
    return candidate


def _release_consumption_record(
    context: _AuthorityContext, release: _VerifiedRelease
) -> dict[str, Any]:
    return {
        "schema_version": RELEASE_CONSUMPTION_SCHEMA,
        "status": "holdout_release_consumed",
        "evidence_class": context.evidence_class,
        "evaluation_tuple_sha256": _release_tuple_sha256(context),
        "release_envelope_sha256": release.sha256,
        "preregistration_envelope_sha256": context.preregistration_sha256,
        "sealed_input_sha256": release.sealed_input_sha256,
        "evaluator_implementation_id": EVALUATOR_IMPLEMENTATION_ID,
    }


def _release_tuple_sha256(context: _AuthorityContext) -> str:
    return _payload_digest(
        {
            "schema_version": RELEASE_TUPLE_SCHEMA,
            "preregistration_envelope_sha256": context.preregistration_sha256,
            "custody_commitment_sha256": context.custody_sha256,
            "authority_hashes": dict(context.hashes),
            "evaluator_implementation_id": EVALUATOR_IMPLEMENTATION_ID,
        }
    )


def _release_marker_path(
    release_ledger_dir: Path | str,
    context: _AuthorityContext,
    *,
    stage: str,
) -> tuple[Path, Path]:
    ledger = _validate_release_ledger_dir(release_ledger_dir, stage=stage)
    return ledger, ledger / f"evaluation-{_release_tuple_sha256(context)}"


def _validate_release_ledger_dir(
    release_ledger_dir: Path | str, *, stage: str
) -> Path:
    ledger = _validate_no_reparse_ancestors(
        release_ledger_dir, stage=stage, code="RELEASE_LEDGER"
    )
    if (
        not ledger.exists()
        or not ledger.is_dir()
        or ledger.is_symlink()
        or _is_reparse(ledger)
    ):
        _fail(stage, "RELEASE_LEDGER", str(ledger), "must be an existing ordinary directory")
    return ledger


def _authority_and_release_ledger(
    authority_kwargs: Mapping[str, Any],
    release_ledger_dir: Path | str | None,
) -> tuple[_AuthorityContext, Path | None]:
    context = _authority_context(**authority_kwargs)
    ledger = (
        _validate_release_ledger_dir(release_ledger_dir, stage="AUTHORITY")
        if release_ledger_dir is not None
        else None
    )
    return context, ledger


def _validate_release_marker_tree(marker: Path, *, stage: str) -> None:
    _validate_no_reparse_ancestors(marker, stage=stage, code="RELEASE_MARKER")
    if (
        not marker.exists()
        or not marker.is_dir()
        or marker.is_symlink()
        or _is_reparse(marker)
    ):
        _fail(stage, "RELEASE_MARKER", str(marker), "must be an ordinary directory")
    files: set[str] = set()
    for entry in marker.iterdir():
        if entry.is_symlink() or _is_reparse(entry) or not entry.is_file():
            _fail(stage, "RELEASE_MARKER", str(entry), "must be a declared ordinary file")
        files.add(entry.name)
    if files != RELEASE_LEDGER_FILES:
        _fail(
            stage,
            "RELEASE_MARKER",
            str(marker),
            f"must contain exactly {sorted(RELEASE_LEDGER_FILES)}",
        )


def _rollback_release_marker(marker: Path | None, ledger: Path | None) -> None:
    if marker is None or ledger is None or marker.parent != ledger or not marker.exists():
        return
    try:
        _validate_no_reparse_ancestors(ledger, stage="ACTIVATION_DECISION", code="RELEASE_LEDGER")
        _validate_no_reparse_ancestors(marker, stage="ACTIVATION_DECISION", code="RELEASE_MARKER")
    except IndependentHoldoutActivationError:
        return
    if marker.is_symlink() or _is_reparse(marker) or not marker.is_dir():
        return
    shutil.rmtree(marker, ignore_errors=True)


def _acquire_release_marker(
    release_ledger_dir: Path | str,
    context: _AuthorityContext,
    release: _VerifiedRelease,
) -> tuple[Path, Path]:
    ledger, marker = _release_marker_path(
        release_ledger_dir, context, stage="ACTIVATION_DECISION"
    )
    _validate_no_reparse_ancestors(
        ledger, stage="ACTIVATION_DECISION", code="RELEASE_LEDGER"
    )
    try:
        marker.mkdir()
    except FileExistsError:
        _fail(
            "ACTIVATION_DECISION",
            "RELEASE_REPLAY",
            str(marker),
            "this signed holdout release has already been consumed",
        )
    except OSError as error:
        _fail("ACTIVATION_DECISION", "RELEASE_LEDGER", str(marker), str(error))

    try:
        _validate_no_reparse_ancestors(
            marker, stage="ACTIVATION_DECISION", code="RELEASE_MARKER"
        )
        expected = _release_consumption_record(context, release)
        expected_sha = write_json_with_sha256(marker / "release-consumption.json", expected)
        _validate_release_marker_tree(marker, stage="ACTIVATION_DECISION")
        observed, observed_sha = _read_canonical(
            marker / "release-consumption.json",
            "release_consumption",
            stage="ACTIVATION_DECISION",
        )
        if observed != expected or observed_sha != expected_sha:
            _fail(
                "ACTIVATION_DECISION",
                "RELEASE_MARKER",
                str(marker),
                "does not match deterministic construction",
            )
    except Exception:
        _rollback_release_marker(marker, ledger)
        raise
    return ledger, marker


def _verify_release_marker(
    release_ledger_dir: Path | str,
    context: _AuthorityContext,
    release: _VerifiedRelease,
) -> None:
    _ledger, marker = _release_marker_path(
        release_ledger_dir, context, stage="RECONSTRUCTION"
    )
    _validate_no_reparse_ancestors(
        marker, stage="RECONSTRUCTION", code="RELEASE_MARKER"
    )
    _validate_release_marker_tree(marker, stage="RECONSTRUCTION")
    observed, _observed_sha = _read_canonical(
        marker / "release-consumption.json",
        "release_consumption",
        stage="RECONSTRUCTION",
    )
    if observed != _release_consumption_record(context, release):
        _fail(
            "RECONSTRUCTION",
            "RELEASE_MARKER",
            str(marker),
            "does not bind the current release, preregistration, and sealed input",
        )


def _prepare_output(output_dir: Path | str) -> tuple[Path, Path, bool]:
    output = _validate_no_reparse_ancestors(
        output_dir, stage="ACTIVATION_DECISION", code="OUTPUT_PATH"
    )
    parent = _validate_no_reparse_ancestors(
        output.parent, stage="ACTIVATION_DECISION", code="OUTPUT_PARENT"
    )
    if not parent.exists() or not parent.is_dir() or parent.is_symlink() or _is_reparse(parent):
        _fail("ACTIVATION_DECISION", "OUTPUT_PARENT", str(parent), "must be an existing ordinary directory")
    existed_empty = False
    if output.exists():
        if output.is_symlink() or _is_reparse(output) or not output.is_dir():
            _fail("ACTIVATION_DECISION", "OUTPUT", str(output), "must be absent or an empty ordinary directory")
        if any(output.iterdir()):
            _fail("ACTIVATION_DECISION", "OUTPUT_NOT_EMPTY", str(output), "must be empty")
        existed_empty = True
    staging = parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        _validate_no_reparse_ancestors(
            parent, stage="ACTIVATION_DECISION", code="OUTPUT_PARENT"
        )
        staging.mkdir()
    except OSError as error:
        _fail("ACTIVATION_DECISION", "OUTPUT", str(staging), str(error))
    _validate_no_reparse_ancestors(
        staging, stage="ACTIVATION_DECISION", code="OUTPUT_PATH"
    )
    return output, staging, existed_empty


def _validate_output_tree(root: Path) -> None:
    _validate_no_reparse_ancestors(
        root, stage="ACTIVATION_DECISION", code="OUTPUT_TREE"
    )
    files: set[str] = set()
    for entry in root.iterdir():
        if entry.is_symlink() or _is_reparse(entry) or not entry.is_file():
            _fail("ACTIVATION_DECISION", "OUTPUT_TREE", str(entry), "must be a declared ordinary file")
        files.add(entry.name)
    if files != OUTPUT_FILES:
        _fail("ACTIVATION_DECISION", "OUTPUT_TREE", str(root), f"must contain exactly {sorted(OUTPUT_FILES)}")


def _read_evaluation_objects(root: Path | str) -> tuple[dict[str, Any], str, dict[str, Any], str]:
    candidate = _validate_no_reparse_ancestors(
        root, stage="RECONSTRUCTION", code="OUTPUT_TREE"
    )
    if not candidate.exists() or not candidate.is_dir() or candidate.is_symlink() or _is_reparse(candidate):
        _fail("RECONSTRUCTION", "OUTPUT_TREE", str(candidate), "must be an ordinary evaluation directory")
    _validate_output_tree(candidate)
    detail, detail_sha = _read_canonical(candidate / "sealed-holdout-evaluation-detail.json", "evaluation.detail", stage="RECONSTRUCTION")
    receipt, receipt_sha = _read_canonical(candidate / "independent-holdout-review-receipt.json", "evaluation.receipt", stage="RECONSTRUCTION")
    return detail, detail_sha, receipt, receipt_sha


def _validate_public_receipt_scope(value: object, path: str = "public_receipt") -> None:
    forbidden_keys = (
        "reflectance",
        "actual_nv",
        "formula_family_id",
        "formula_id",
        "formula_batch_id",
        "card_id",
        "measurement_id",
        "relative_path",
        "sealed_root",
        "evidence_path",
    )
    if isinstance(value, Mapping):
        for key, child in value.items():
            if any(marker in str(key).casefold() for marker in forbidden_keys):
                _fail("ACTIVATION_DECISION", "PUBLIC_LEAKAGE", f"{path}.{key}", "contains a private holdout field")
            _validate_public_receipt_scope(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_public_receipt_scope(child, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.casefold()
        if any(marker in lowered for marker in ("fam-ho-", "form-ho-", "fb-ho-", "card-ho-", "sealed-holdout")):
            _fail("ACTIVATION_DECISION", "PUBLIC_LEAKAGE", path, "contains a private holdout identifier")


def run_independent_holdout_evaluation(
    *,
    release_envelope_path: Path | str,
    sealed_input_path: Path | str,
    output_dir: Path | str,
    release_ledger_dir: Path | str | None = None,
    **authority_kwargs: Any,
) -> dict[str, Any]:
    """Evaluate one signed release and atomically publish a non-activating review package."""

    context, validated_ledger = _authority_and_release_ledger(
        authority_kwargs, release_ledger_dir
    )
    release = _verify_release(
        context, release_envelope_path=release_envelope_path
    )
    output, staging, existed_empty = _prepare_output(output_dir)
    ledger: Path | None = None
    marker: Path | None = None
    try:
        if validated_ledger is not None:
            ledger, marker = _acquire_release_marker(
                validated_ledger, context, release
            )
        sealed = _release_and_sealed_input(
            context,
            release_envelope_path=release_envelope_path,
            sealed_input_path=sealed_input_path,
            verified_release=release,
        )
        detail, receipt = _evaluation_objects(
            context, sealed, replay_protected=marker is not None
        )
        _validate_public_receipt_scope(receipt)
        detail_sha = write_json_with_sha256(staging / "sealed-holdout-evaluation-detail.json", detail)
        receipt_sha = write_json_with_sha256(staging / "independent-holdout-review-receipt.json", receipt)
        staged_detail, staged_detail_sha, staged_receipt, staged_receipt_sha = _read_evaluation_objects(staging)
        if staged_detail != detail or staged_receipt != receipt or staged_detail_sha != detail_sha or staged_receipt_sha != receipt_sha:
            _fail("ACTIVATION_DECISION", "READBACK", str(staging), "differs from deterministic construction")
        _validate_no_reparse_ancestors(
            output.parent, stage="ACTIVATION_DECISION", code="OUTPUT_PARENT"
        )
        _validate_no_reparse_ancestors(
            staging, stage="ACTIVATION_DECISION", code="OUTPUT_PATH"
        )
        if output.exists():
            _validate_no_reparse_ancestors(
                output, stage="ACTIVATION_DECISION", code="OUTPUT_PATH"
            )
        if existed_empty:
            output.rmdir()
        os.replace(staging, output)
    except Exception:
        if staging.exists() and not staging.is_symlink() and not _is_reparse(staging):
            shutil.rmtree(staging, ignore_errors=True)
        if existed_empty and not output.exists():
            output.mkdir(exist_ok=True)
        _rollback_release_marker(marker, ledger)
        raise
    return {
        "status": "independent_holdout_evaluated",
        "state": receipt["state"],
        "evidence_class": receipt["evidence_class"],
        "verdict": receipt["verdict"],
        "activation_review_eligible": receipt["activation_review_eligible"],
        "release_replay_protected": receipt["release_replay_protected"],
        "production_pass": False,
        "runtime_compatible": False,
        "detail_sha256": detail_sha,
        "review_receipt_sha256": receipt_sha,
        **_permissions(),
    }


def verify_independent_holdout_evaluation(
    *,
    release_envelope_path: Path | str,
    sealed_input_path: Path | str,
    evaluation_root: Path | str,
    release_ledger_dir: Path | str | None = None,
    **authority_kwargs: Any,
) -> dict[str, Any]:
    """Reconstruct and compare every evaluation artifact from current authority inputs."""

    context, validated_ledger = _authority_and_release_ledger(
        authority_kwargs, release_ledger_dir
    )
    release = _verify_release(
        context, release_envelope_path=release_envelope_path
    )
    if validated_ledger is not None:
        _verify_release_marker(validated_ledger, context, release)
    sealed = _release_and_sealed_input(
        context,
        release_envelope_path=release_envelope_path,
        sealed_input_path=sealed_input_path,
        verified_release=release,
    )
    expected_detail, expected_receipt = _evaluation_objects(
        context, sealed, replay_protected=validated_ledger is not None
    )
    _validate_public_receipt_scope(expected_receipt)
    detail, detail_sha, receipt, receipt_sha = _read_evaluation_objects(evaluation_root)
    if detail.get("detail_payload_sha256") != _self_bind({**detail, "detail_payload_sha256": ""}, "detail_payload_sha256")["detail_payload_sha256"]:
        _fail("RECONSTRUCTION", "SELF_HASH", "evaluation.detail", "does not self-bind")
    if receipt.get("receipt_payload_sha256") != _self_bind({**receipt, "receipt_payload_sha256": ""}, "receipt_payload_sha256")["receipt_payload_sha256"]:
        _fail("RECONSTRUCTION", "SELF_HASH", "evaluation.receipt", "does not self-bind")
    if detail != expected_detail or receipt != expected_receipt:
        _fail("RECONSTRUCTION", "MISMATCH", str(evaluation_root), "does not match deterministic reconstruction")
    _validate_public_receipt_scope(receipt)
    return {
        "status": "independent_holdout_evaluation_verified",
        "state": receipt["state"],
        "evidence_class": receipt["evidence_class"],
        "verdict": receipt["verdict"],
        "activation_review_eligible": receipt["activation_review_eligible"],
        "release_replay_protected": receipt["release_replay_protected"],
        "production_pass": False,
        "runtime_compatible": False,
        "detail_sha256": detail_sha,
        "review_receipt_sha256": receipt_sha,
        **_permissions(),
    }
