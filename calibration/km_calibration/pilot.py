"""Fail-closed preregistration and freeze contract for the 45-card pilot.

This module freezes acquisition design only.  It deliberately does not import
the dataset loader, fitting pipeline, or any model-evaluation code.
"""

from __future__ import annotations

import copy
import csv
import json
import math
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .diagnostic import verify_preflight_receipt
from .evidence import EvidenceValidationError, bind_physical_label_evidence
from .errors import CalibrationError
from .hashing import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
    verify_sha256_sidecar,
    write_json_with_sha256,
)


PILOT_DESIGN_SCHEMA_VERSION = "moocow-physical-pilot-design-v1"
PILOT_DESIGN_RECEIPT_SCHEMA_VERSION = "moocow-physical-pilot-design-receipt-v1"
COMPONENT_ORDER = (
    "base",
    "Y83S",
    "Y74S",
    "B150S",
    "B153S",
    "R254D",
    "R101Y",
    "R101V",
    "Y42S",
    "073",
    "W064",
    "V23",
    "G7",
    "R122S",
    "BK7H",
)
BACKINGS = ("black", "white")
POSITIONS = ("POS01", "POS02", "POS03")
DFT_BANDS = ("DFT-L", "DFT-M", "DFT-H")

_COMPONENT_IDS = {
    "base": "base-waterborne-clear",
    **{code: f"colorant-{code}" for code in COMPONENT_ORDER[1:]},
}
_REGISTRY_ROOT_FIELDS = {
    "schema_version",
    "registry_status",
    "intended_use",
    "source_evidence",
    "concentration_basis_required_for_import",
    "spectral_data_policy",
    "components",
    "before_any_drawdown",
}
_REGISTRY_COMPONENT_FIELDS = {
    "component_id",
    "role",
    "catalog_code",
    "material_description",
    "product_name",
    "batch_id",
    "manufacturer_or_supplier",
    "wet_density_g_ml",
    "nonvolatile_mass_fraction",
    "nonvolatile_volume_fraction",
    "nonvolatile_density_g_ml",
    "cure_protocol",
    "application_dft_um",
    "lot_verification_status",
    "spectral_status",
    "physical_label_verification_id",
    "physical_label_verified_at",
    "physical_label_evidence",
}


class PilotValidationError(CalibrationError):
    """A machine-readable preregistration or receipt-validation failure."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"[{code}] {path}: {message}")


def _fail(code: str, path: str, message: str) -> None:
    raise PilotValidationError(code, path, message)


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("TYPE", path, "must be an object")
    return value


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("TYPE", path, "must be an array")
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("TEXT", path, "must be a non-empty string")
    return value


def _number(value: object, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("NUMBER", path, "must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        _fail("NUMBER", path, "must be a finite positive number" if positive else "must be finite")
    return result


def _sha256(value: object, path: str) -> str:
    text = _text(value, path).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        _fail("SHA256", path, "must be a lowercase SHA-256 digest")
    return text


def _exact_fields(value: Mapping[str, Any], path: str, required: Sequence[str]) -> None:
    expected = set(required)
    actual = set(value)
    if actual != expected:
        _fail("FIELDS", path, f"must contain exactly {sorted(expected)}; missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}")


def _reject_placeholders(value: object, path: str = "$") -> None:
    blocked = (
        "required",
        "template",
        "synthetic",
        "reference",
        "placeholder",
        "not_yet",
    )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_placeholders(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_placeholders(item, f"{path}[{index}]")
    elif isinstance(value, str) and any(marker in value.casefold() for marker in blocked):
        _fail("PLACEHOLDER", path, "contains a template, synthetic, reference, or unresolved value")


def _load_json_no_duplicates(path: Path) -> object:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail("JSON_DUPLICATE_KEY", str(path), f"contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs_hook)
    except (OSError, json.JSONDecodeError) as error:
        _fail("JSON_READ", str(path), str(error))


def _ensure_empty_output(output_dir: Path | str) -> Path:
    output = Path(output_dir)
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            _fail("OUTPUT_DIR", str(output), "must be a directory path")
        if any(output.iterdir()):
            _fail("OUTPUT_DIR_NOT_EMPTY", str(output), "must be empty so a failed rerun cannot reuse pass artifacts")
    return output


def _create_staging_output(output_dir: Path | str) -> tuple[Path, Path, bool]:
    output = _ensure_empty_output(output_dir)
    existed_empty = output.exists()
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        _fail("OUTPUT_STAGING", str(staging), str(error))
    return output, staging, existed_empty


def _publish_staging_output(*, output: Path, staging: Path, existed_empty: bool) -> None:
    try:
        if existed_empty:
            output.rmdir()
        staging.replace(output)
    except OSError as error:
        if existed_empty and not output.exists():
            try:
                output.mkdir(parents=True, exist_ok=False)
            except OSError as restore_error:
                _fail("OUTPUT_RESTORE", str(output), str(restore_error))
        _fail("OUTPUT_PUBLISH", str(output), str(error))


def _target_nv(**fractions: float) -> dict[str, float]:
    return {component: fractions[component] for component in COMPONENT_ORDER if component in fractions}


def _family_specs() -> tuple[tuple[str, str, tuple[str, ...], dict[str, float]], ...]:
    basis = [("BASE", _target_nv(base=1.0))] + [
        (code, _target_nv(base=0.85, **{code: 0.15})) for code in COMPONENT_ORDER[1:]
    ]
    return tuple(
        [("train", f"FAM-TR-BASIS-{code}", ("DFT-L", "DFT-H"), target) for code, target in basis]
        + [
            ("validation", "FAM-VA-MIX-01", DFT_BANDS, _target_nv(base=0.70, Y83S=0.15, B150S=0.15)),
            ("validation", "FAM-VA-MIX-02", DFT_BANDS, _target_nv(base=0.70, R254D=0.15, G7=0.15)),
            ("holdout", "FAM-HO-MIX-01", DFT_BANDS, _target_nv(base=0.70, Y74S=0.15, R122S=0.15)),
            ("holdout", "FAM-HO-MIX-02", DFT_BANDS, _target_nv(base=0.70, R101Y=0.15, B153S=0.15)),
            ("holdout", "FAM-HO-MIX-03", DFT_BANDS, _target_nv(base=0.70, BK7H=0.10, V23=0.10, Y42S=0.10)),
        ]
    )


def _fixed_roster() -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for split, family, bands, target in _family_specs():
        suffix = family.removeprefix("FAM-")
        for band in bands:
            rows.append(
                {
                    "roster_index": len(rows) + 1,
                    "split": split,
                    "formula_family_id": family,
                    "formula_id": f"FORM-{suffix}",
                    "card_id": f"CARD-{suffix}-{band}-001",
                    "dft_band": band,
                    "target_NV": target,
                }
            )
    return tuple(rows)


PILOT_CARD_ROSTER = _fixed_roster()


def _roster_with_commitments(*, template: bool) -> list[dict[str, Any]]:
    rows = []
    for row in PILOT_CARD_ROSTER:
        family = row["formula_family_id"]
        rows.append(
            {
                **copy.deepcopy(row),
                "formula_batch_id": f"REQUIRED_FORMULA_BATCH_ID_{family}" if template else f"BATCH-{family.removeprefix('FAM-')}",
                "randomization_plan_id": "REQUIRED_RANDOMIZATION_PLAN_ID" if template else "RANDOMIZATION-PLAN-REQUIRED-BEFORE-FREEZE",
            }
        )
    return rows


def _primary_reading_slots(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "split": row["split"],
            "formula_family_id": row["formula_family_id"],
            "card_id": row["card_id"],
            "dft_band": row["dft_band"],
            "backing": backing,
            "reposition_id": position,
            "sample_group_id": f"SG-{row['card_id']}-{backing.upper()}",
            "measurement_id": f"MSR-SG-{row['card_id']}-{backing.upper()}-{position}",
        }
        for row in rows
        for backing in BACKINGS
        for position in POSITIONS
    ]


def _roster_commitment(rows: Sequence[Mapping[str, Any]], *, split_name: str) -> dict[str, Any]:
    slots = _primary_reading_slots(rows)
    return {
        "commitment_kind": split_name,
        "card_count": len(rows),
        "formula_family_count": len({row["formula_family_id"] for row in rows}),
        "primary_reading_slot_count": len(slots),
        "roster_sha256": sha256_bytes(canonical_json_bytes(list(rows))),
        "primary_reading_slots_sha256": sha256_bytes(canonical_json_bytes(slots)),
    }


def _pilot_summary() -> dict[str, int]:
    return {
        "train_families": 15,
        "validation_families": 2,
        "holdout_families": 3,
        "train_cards": 30,
        "validation_cards": 6,
        "holdout_cards": 9,
        "cards": 45,
        "open_primary_reading_slots": 216,
        "holdout_primary_reading_slots": 54,
        "primary_reading_slots": 270,
    }


def build_pilot_design_template() -> dict[str, Any]:
    """Return a deterministic, deliberately non-freezable pilot design template."""
    return {
        "schema_version": PILOT_DESIGN_SCHEMA_VERSION,
        "template_status": "TEMPLATE_INVALID",
        "pilot_design_status": "TEMPLATE_INVALID",
        "dataset_status": "research_only",
        "component_order": list(COMPONENT_ORDER),
        "physical_ranking_enabled": False,
        "model_fitting_permitted": False,
        "holdout_release_permitted": False,
        "promotion_permitted": False,
        "dft_bands": {
            band: {"target_um": None, "acceptance_min_um": None, "acceptance_max_um": None}
            for band in DFT_BANDS
        },
        "roster": _roster_with_commitments(template=True),
    }


def _normalize_dft_bands(value: object) -> dict[str, dict[str, float]]:
    bands = _mapping(value, "design.dft_bands")
    if set(bands) != set(DFT_BANDS):
        _fail("DFT_BANDS", "design.dft_bands", f"must contain exactly {list(DFT_BANDS)}")
    result: dict[str, dict[str, float]] = {}
    for band in DFT_BANDS:
        entry = _mapping(bands[band], f"design.dft_bands.{band}")
        _exact_fields(entry, f"design.dft_bands.{band}", ("target_um", "acceptance_min_um", "acceptance_max_um"))
        target = _number(entry["target_um"], f"design.dft_bands.{band}.target_um", positive=True)
        lower = _number(entry["acceptance_min_um"], f"design.dft_bands.{band}.acceptance_min_um", positive=True)
        upper = _number(entry["acceptance_max_um"], f"design.dft_bands.{band}.acceptance_max_um", positive=True)
        if lower > target or target > upper:
            _fail("DFT_RANGE", f"design.dft_bands.{band}", "must satisfy acceptance_min_um <= target_um <= acceptance_max_um")
        result[band] = {"target_um": target, "acceptance_min_um": lower, "acceptance_max_um": upper}
    low, medium, high = (result[band] for band in DFT_BANDS)
    if not low["target_um"] < medium["target_um"] < high["target_um"]:
        _fail("DFT_TARGET_ORDER", "design.dft_bands", "must satisfy target DFT-L < DFT-M < DFT-H")
    if not low["acceptance_max_um"] < medium["acceptance_min_um"] or not medium["acceptance_max_um"] < high["acceptance_min_um"]:
        _fail("DFT_ACCEPTANCE_ORDER", "design.dft_bands", "acceptance intervals must be strictly ordered and non-overlapping")
    return result


def _normalize_target_nv(value: object, path: str) -> dict[str, float]:
    target = _mapping(value, path)
    result = {component: _number(amount, f"{path}.{component}", positive=True) for component, amount in target.items()}
    if not result or any(component not in COMPONENT_ORDER for component in result):
        _fail("TARGET_NV", path, "must name only fixed pilot components")
    if not math.isclose(sum(result.values()), 1.0, rel_tol=0.0, abs_tol=1e-12):
        _fail("TARGET_NV", path, "must sum to 1.0")
    return result


def _normalize_roster(value: object) -> list[dict[str, Any]]:
    raw_rows = _list(value, "design.roster")
    if len(raw_rows) != len(PILOT_CARD_ROSTER):
        _fail("ROSTER_COUNT", "design.roster", "must contain exactly 45 cards")
    rows: list[dict[str, Any]] = []
    expected_batch_family: dict[str, str] = {}
    expected_family_batch: dict[str, str] = {}
    plan_ids: set[str] = set()
    for index, (raw, expected) in enumerate(zip(raw_rows, PILOT_CARD_ROSTER, strict=True), start=1):
        row = _mapping(raw, f"design.roster[{index - 1}]")
        _exact_fields(
            row,
            f"design.roster[{index - 1}]",
            ("roster_index", "split", "formula_family_id", "formula_id", "formula_batch_id", "card_id", "dft_band", "target_NV", "randomization_plan_id"),
        )
        if isinstance(row["roster_index"], bool) or row["roster_index"] != index:
            _fail("ROSTER_INDEX", f"design.roster[{index - 1}].roster_index", "must equal the fixed roster position")
        normalized = {
            "roster_index": index,
            "split": _text(row["split"], f"design.roster[{index - 1}].split"),
            "formula_family_id": _text(row["formula_family_id"], f"design.roster[{index - 1}].formula_family_id"),
            "formula_id": _text(row["formula_id"], f"design.roster[{index - 1}].formula_id"),
            "formula_batch_id": _text(row["formula_batch_id"], f"design.roster[{index - 1}].formula_batch_id"),
            "card_id": _text(row["card_id"], f"design.roster[{index - 1}].card_id"),
            "dft_band": _text(row["dft_band"], f"design.roster[{index - 1}].dft_band"),
            "target_NV": _normalize_target_nv(row["target_NV"], f"design.roster[{index - 1}].target_NV"),
            "randomization_plan_id": _text(row["randomization_plan_id"], f"design.roster[{index - 1}].randomization_plan_id"),
        }
        for key in ("split", "formula_family_id", "formula_id", "card_id", "dft_band", "target_NV"):
            if normalized[key] != expected[key]:
                _fail("ROSTER", f"design.roster[{index - 1}].{key}", "does not match the fixed pilot roster")
        prior_family = expected_batch_family.setdefault(normalized["formula_batch_id"], normalized["formula_family_id"])
        if prior_family != normalized["formula_family_id"]:
            _fail("FORMULA_BATCH", f"design.roster[{index - 1}].formula_batch_id", "must not be shared across formula families")
        prior_batch = expected_family_batch.setdefault(normalized["formula_family_id"], normalized["formula_batch_id"])
        if prior_batch != normalized["formula_batch_id"]:
            _fail("FORMULA_FAMILY_BATCH", f"design.roster[{index - 1}].formula_batch_id", "each formula family must use exactly one formula_batch_id")
        plan_ids.add(normalized["randomization_plan_id"])
        rows.append(normalized)
    if len(plan_ids) != 1:
        _fail("RANDOMIZATION_PLAN", "design.roster", "must use one preregistered randomization_plan_id")
    return rows


def _normalize_design(value: object) -> dict[str, Any]:
    design = _mapping(value, "design")
    _reject_placeholders(design, "design")
    _exact_fields(
        design,
        "design",
        (
            "schema_version",
            "pilot_design_status",
            "dataset_status",
            "component_order",
            "physical_ranking_enabled",
            "model_fitting_permitted",
            "holdout_release_permitted",
            "promotion_permitted",
            "dft_bands",
            "roster",
        ),
    )
    if design["schema_version"] != PILOT_DESIGN_SCHEMA_VERSION:
        _fail("SCHEMA_VERSION", "design.schema_version", f"must be {PILOT_DESIGN_SCHEMA_VERSION}")
    if design["pilot_design_status"] != "pilot_design_preregistered":
        _fail("DESIGN_STATUS", "design.pilot_design_status", "must be pilot_design_preregistered")
    if design["dataset_status"] != "research_only":
        _fail("DATASET_STATUS", "design.dataset_status", "must be research_only")
    if _list(design["component_order"], "design.component_order") != list(COMPONENT_ORDER):
        _fail("COMPONENT_ORDER", "design.component_order", "must equal the fixed 15-component order")
    for flag in ("physical_ranking_enabled", "model_fitting_permitted", "holdout_release_permitted", "promotion_permitted"):
        if design[flag] is not False:
            _fail("PERMISSION", f"design.{flag}", "must remain false during design freeze")
    return {
        "schema_version": PILOT_DESIGN_SCHEMA_VERSION,
        "pilot_design_status": "pilot_design_preregistered",
        "dataset_status": "research_only",
        "component_order": list(COMPONENT_ORDER),
        "physical_ranking_enabled": False,
        "model_fitting_permitted": False,
        "holdout_release_permitted": False,
        "promotion_permitted": False,
        "dft_bands": _normalize_dft_bands(design["dft_bands"]),
        "roster": _normalize_roster(design["roster"]),
    }


def _normalize_registry(
    value: object, *, registry_evidence_root: Path | str
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    registry = _mapping(value, "registry")
    _reject_placeholders(registry, "registry")
    _exact_fields(registry, "registry", tuple(_REGISTRY_ROOT_FIELDS))
    if registry["schema_version"] != "moocow-current-batch-component-registry-v1":
        _fail("REGISTRY_SCHEMA", "registry.schema_version", "is not supported")
    components = _list(registry["components"], "registry.components")
    if len(components) != len(COMPONENT_ORDER):
        _fail("REGISTRY_COUNT", "registry.components", "must contain exactly 15 components")
    by_id: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(components):
        component = dict(_mapping(raw, f"registry.components[{index}]"))
        unknown = set(component) - _REGISTRY_COMPONENT_FIELDS
        if unknown:
            _fail("FIELDS", f"registry.components[{index}]", f"contains unknown fields {sorted(unknown)}")
        component_id = _text(component.get("component_id"), f"registry.components[{index}].component_id")
        if component_id in by_id:
            _fail("REGISTRY_COMPONENT", f"registry.components[{index}].component_id", "must not be duplicated")
        by_id[component_id] = component
    expected_ids = {_COMPONENT_IDS[component] for component in COMPONENT_ORDER}
    if set(by_id) != expected_ids:
        _fail("REGISTRY_COMPONENT", "registry.components", "must contain exactly the fixed base and 14 colorants")
    for component in COMPONENT_ORDER:
        component_id = _COMPONENT_IDS[component]
        entry = by_id[component_id]
        expected_role = "base" if component == "base" else "colorant"
        if entry.get("role") != expected_role:
            _fail("REGISTRY_ROLE", f"registry.components.{component_id}.role", f"must be {expected_role}")
        if entry.get("lot_verification_status") != "verified_physical_label":
            _fail("REGISTRY_LOT_VERIFICATION", f"registry.components.{component_id}.lot_verification_status", "must be verified_physical_label")
        _text(entry.get("batch_id"), f"registry.components.{component_id}.batch_id")
        _text(entry.get("product_name"), f"registry.components.{component_id}.product_name")
        _text(entry.get("manufacturer_or_supplier"), f"registry.components.{component_id}.manufacturer_or_supplier")
    try:
        physical_label_bindings = bind_physical_label_evidence(
            components, registry_evidence_root=registry_evidence_root
        )
    except EvidenceValidationError as error:
        _fail(error.code, error.path, error.message)
    by_component_id = {item["component_id"]: item for item in physical_label_bindings}
    return (
        copy.deepcopy(dict(registry)),
        by_id,
        [copy.deepcopy(by_component_id[_COMPONENT_IDS[component]]) for component in COMPONENT_ORDER],
    )


def _diagnostic_binding(receipt_path: Path, evidence_root: Path | str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reverify the prerequisite receipt before reading pilot input or writing output."""
    verification_result = verify_preflight_receipt(receipt_path=receipt_path, evidence_root=evidence_root)
    receipt = _mapping(_load_json_no_duplicates(receipt_path), "diagnostic_receipt")
    for flag in ("model_fitting_permitted", "physical_ranking_enabled", "promotion_permitted"):
        if receipt.get(flag) is not False:
            _fail("DIAGNOSTIC_PERMISSION", f"diagnostic_receipt.{flag}", "must remain false")
    bindings = _mapping(receipt.get("bindings"), "diagnostic_receipt.bindings")
    evidence_verification = copy.deepcopy(dict(_mapping(bindings.get("evidence_verification"), "diagnostic_receipt.bindings.evidence_verification")))
    normalized_path = receipt_path.with_name("normalized-diagnostic.json")
    normalized = dict(_mapping(_load_json_no_duplicates(normalized_path), "normalized_diagnostic"))
    binding = {
        "preflight_receipt_artifact_sha256": sha256_file(receipt_path),
        "preflight_receipt_payload_sha256": _sha256(receipt.get("receipt_payload_sha256"), "diagnostic_receipt.receipt_payload_sha256"),
        "normalized_diagnostic_sha256": _sha256(verification_result.get("diagnostic_payload_sha256"), "diagnostic_verification.diagnostic_payload_sha256"),
        "normalized_artifact_sha256": _sha256(receipt.get("normalized_artifact_sha256"), "diagnostic_receipt.normalized_artifact_sha256"),
        "evidence_verification": evidence_verification,
        "evidence_verification_sha256": _sha256(verification_result.get("evidence_verification_sha256"), "diagnostic_verification.evidence_verification_sha256"),
    }
    if _sha256(evidence_verification.get("evidence_verification_sha256"), "diagnostic_receipt.bindings.evidence_verification.evidence_verification_sha256") != binding["evidence_verification_sha256"]:
        _fail("DIAGNOSTIC_EVIDENCE_BINDING", "diagnostic_receipt.bindings.evidence_verification", "does not match the reverified diagnostic evidence")
    return binding, normalized


def _validate_diagnostic_lots(normalized_diagnostic: Mapping[str, Any], registry: Mapping[str, Mapping[str, Any]]) -> None:
    materials = _mapping(normalized_diagnostic.get("materials"), "normalized_diagnostic.materials")
    for material_name, component_id in (("base", _COMPONENT_IDS["base"]), ("w064", _COMPONENT_IDS["W064"])):
        material = _mapping(materials.get(material_name), f"normalized_diagnostic.materials.{material_name}")
        if material.get("component_id") != component_id:
            _fail("DIAGNOSTIC_COMPONENT", f"normalized_diagnostic.materials.{material_name}.component_id", "does not match the pilot registry")
        if material.get("batch_id") != registry[component_id]["batch_id"]:
            _fail("DIAGNOSTIC_LOT", f"normalized_diagnostic.materials.{material_name}.batch_id", "does not match the verified pilot registry lot")
    cards = _list(normalized_diagnostic.get("cards"), "normalized_diagnostic.cards")
    for index, raw_card in enumerate(cards):
        formula = _mapping(_mapping(raw_card, f"normalized_diagnostic.cards[{index}]").get("formula"), f"normalized_diagnostic.cards[{index}].formula")
        for component in _list(formula.get("components"), f"normalized_diagnostic.cards[{index}].formula.components"):
            item = _mapping(component, f"normalized_diagnostic.cards[{index}].formula.components[]")
            component_id = item.get("component_id")
            if component_id in {_COMPONENT_IDS["base"], _COMPONENT_IDS["W064"]} and item.get("physical_lot_id") != registry[component_id]["batch_id"]:
                _fail("DIAGNOSTIC_LOT", f"normalized_diagnostic.cards[{index}].formula.components", "contains a base or W064 lot inconsistent with the pilot registry")


def _registry_binding(
    registry_path: Path,
    registry: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
    physical_label_bindings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "registry_artifact_sha256": sha256_file(registry_path),
        "registry_payload_sha256": sha256_bytes(canonical_json_bytes(registry)),
        "component_lots": [
            {
                "component_id": _COMPONENT_IDS[component],
                "physical_lot_id": by_id[_COMPONENT_IDS[component]]["batch_id"],
                "lot_verification_status": "verified_physical_label",
            }
            for component in COMPONENT_ORDER
        ],
        "physical_label_evidence": [copy.deepcopy(dict(item)) for item in physical_label_bindings],
    }


def _input_design_binding(design_path: Path, design: Mapping[str, Any]) -> dict[str, str]:
    return {
        "design_artifact_sha256": sha256_file(design_path),
        "design_payload_sha256": sha256_bytes(canonical_json_bytes(design)),
    }


def _receipt_payload(
    *,
    design: Mapping[str, Any],
    normalized_artifact_sha256: str,
    input_design: Mapping[str, Any],
    registry: Mapping[str, Any],
    diagnostic: Mapping[str, Any],
) -> dict[str, Any]:
    open_rows = [row for row in design["roster"] if row["split"] in {"train", "validation"}]
    holdout_rows = [row for row in design["roster"] if row["split"] == "holdout"]
    return {
        "schema_version": PILOT_DESIGN_RECEIPT_SCHEMA_VERSION,
        "status": "pilot_roster_frozen",
        "normalized_pilot_design_sha256": sha256_bytes(canonical_json_bytes(design)),
        "normalized_artifact_sha256": normalized_artifact_sha256,
        "bindings": {
            "input_design": copy.deepcopy(dict(input_design)),
            "registry": copy.deepcopy(dict(registry)),
            "diagnostic": copy.deepcopy(dict(diagnostic)),
            "open_roster_commitment": _roster_commitment(open_rows, split_name="train_validation_open"),
            "holdout_roster_commitment": _roster_commitment(holdout_rows, split_name="holdout_sealed"),
        },
        "pilot_acquisition_permitted": True,
        "model_fitting_permitted": False,
        "holdout_release_permitted": False,
        "physical_ranking_enabled": False,
        "promotion_permitted": False,
    }


def _pilot_registry_template(registry: Mapping[str, Any]) -> dict[str, Any]:
    template = copy.deepcopy(dict(registry))
    for raw_component in _list(template.get("components"), "registry.components"):
        component = _mapping(raw_component, "registry.components[]")
        component_id = _text(component.get("component_id"), "registry.components[].component_id")
        component["lot_verification_status"] = "REQUIRED_PHYSICAL_LABEL_VERIFICATION"
        component["physical_label_verification_id"] = f"REQUIRED_PHYSICAL_LABEL_VERIFICATION_ID_{component_id}"
        component["physical_label_verified_at"] = "REQUIRED_ISO8601_TIMESTAMP_WITH_TIMEZONE"
        component["physical_label_evidence"] = {
            "relative_path": f"labels/REQUIRED_{component_id}_PHYSICAL_LABEL_FILE",
            "record_locator": {"kind": "whole_file"},
        }
    return template


def _validate_prepared_pilot_pack(output: Path) -> None:
    for name in ("pilot-design.template.json", "pilot-45-card-roster.json"):
        try:
            verify_sha256_sidecar(output / name)
        except CalibrationError as error:
            _fail("PREPARE_VALIDATION", str(output / name), str(error))
    labels_readme = output / "evidence" / "labels" / "README.md"
    if not labels_readme.is_file() or "whole-file" not in labels_readme.read_text(encoding="utf-8"):
        _fail("PREPARE_VALIDATION", str(labels_readme), "must explain the whole-file physical label requirement")


def _prepare_pilot_build(registry_path: Path | str, output_dir: Path | str) -> dict[str, Any]:
    """Create a deterministic, deliberately invalid 45-card operator pack."""
    registry_file = Path(registry_path)
    registry = _mapping(_load_json_no_duplicates(registry_file), "registry")
    if registry.get("schema_version") != "moocow-current-batch-component-registry-v1":
        _fail("REGISTRY_SCHEMA", str(registry_file), "is not supported")
    components = _list(registry.get("components"), "registry.components")
    expected_ids = {_COMPONENT_IDS[component] for component in COMPONENT_ORDER}
    component_ids = {item.get("component_id") for item in components if isinstance(item, Mapping)}
    if component_ids != expected_ids:
        _fail("REGISTRY_COMPONENT", str(registry_file), "must declare the fixed base and 14 colorants")
    output = _ensure_empty_output(output_dir)
    template = build_pilot_design_template()
    registry_template = _pilot_registry_template(registry)
    static_roster = [copy.deepcopy(row) for row in PILOT_CARD_ROSTER]
    open_slots = _primary_reading_slots([row for row in static_roster if row["split"] in {"train", "validation"}])
    holdout_slots = _primary_reading_slots([row for row in static_roster if row["split"] == "holdout"])
    output.mkdir(parents=True, exist_ok=True)
    evidence_registry = output / "evidence" / "registry" / registry_file.name
    evidence_registry.parent.mkdir(parents=True, exist_ok=True)
    write_json_with_sha256(evidence_registry, registry_template)
    labels_readme = output / "evidence" / "labels" / "README.md"
    labels_readme.parent.mkdir(parents=True, exist_ok=True)
    labels_readme.write_text(
        "# Physical label evidence required\n\n"
        "Replace every registry placeholder with one real current-lot container-label file under this directory. "
        "Each `physical_label_evidence` locator must be a portable relative `labels/...` path with `record_locator.kind=whole_file`. "
        "Freeze and re-verification read every file and bind its size, file SHA-256, and whole-file record SHA-256; templates cannot freeze.\n",
        encoding="utf-8",
        newline="\n",
    )
    template_path = output / "pilot-design.template.json"
    roster_path = output / "pilot-45-card-roster.json"
    write_json_with_sha256(template_path, template)
    write_json_with_sha256(roster_path, static_roster)
    for path, rows in ((output / "open-primary-reading-roster.csv", open_slots), (output / "holdout-primary-reading-roster.csv", holdout_slots)):
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    (output / "README.en.md").write_text(
        "# 45-card pilot acquisition pack\n\n"
        "This pack is deliberately invalid and cannot be frozen until every template value is replaced by real, current-lot physical evidence. It creates no model, evaluates no holdout, and never enables physical ranking or promotion.\n\n"
        "1. Update the copied registry from 15 physical container labels. Every component needs `lot_verification_status=verified_physical_label`, a non-placeholder verification ID, a timezone-aware ISO timestamp, and a whole-file `physical_label_evidence` locator under `evidence/labels/`. Do not infer a missing lot.\n"
        "2. Complete `pilot-design.template.json`: remove `template_status`, set `pilot_design_status` to `pilot_design_preregistered`, provide one real randomization plan ID and one real batch ID per formula family.\n"
        "3. Preregister real positive DFT-L/M/H values: target L < M < H and acceptance intervals must be strictly ordered and non-overlapping. Do not invent DFT values, material properties, condition-number gates, or performance thresholds.\n"
        "4. Complete and independently reverify the real four-card receipt first. Its base and W064 lots must equal this pilot registry.\n"
        "5. Freeze only with `freeze-pilot-design --registry-evidence-root evidence`; it writes the two hash-bound artifacts into a new empty directory. The open roster has 216 primary slots and the sealed holdout roster has 54.\n"
        "6. Use `verify-pilot-design-receipt --registry-evidence-root evidence` after copying evidence. Freeze permits acquisition only; fitting, holdout release, physical ranking, and promotion remain false.\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "README.zh-CN.md").write_text(
        "# 45 卡 pilot 采集包\n\n"
        "本包故意保持无效；只有用当前真实批次的物理证据替换全部模板字段后才可冻结。它不拟合模型、不查看 holdout，也绝不启用物理排序或晋级。\n\n"
        "1. 按 15 个实体容器标签更新复制的 registry：固定 base 加 14 个色浆必须齐全，且每个 `lot_verification_status` 必须为 `verified_physical_label`。缺失批号不得猜测。\n"
        "2. 完成 `pilot-design.template.json`：删除 `template_status`，把 `pilot_design_status` 改为 `pilot_design_preregistered`；登记一个真实随机化计划 ID，并为每个 formula family 登记一个真实批次 ID。\n"
        "3. 预注册真实、正数的 DFT-L/M/H 目标值、最小值和最大值，目标值必须位于范围内。不得虚构 DFT、物性、条件数门槛或性能门槛。\n"
        "4. 先完成并重新验证真实四卡 receipt；其中 base 和 W064 批号必须与本 pilot registry 一致。\n"
        "5. 只可用 `freeze-pilot-design` 冻结到新的空目录。开放 roster 有 216 个主读数槽，封存 holdout roster 有 54 个。\n"
        "6. 复制证据后使用 `verify-pilot-design-receipt` 重验。冻结只允许采集；拟合、holdout 释放、物理排序和晋级始终为 false。\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "README.zh-CN.md").write_text(
        "# 45-card pilot 采集包\n\n"
        "本包故意保持无效。15 个组件都必须填写真实物理标签：`verified_physical_label` 状态、非占位 verification ID、带时区的 ISO 时间戳，以及 `evidence/labels/` 下的 whole-file locator。冻结和重验会读取每个标签文件并绑定大小、文件 SHA-256 与 record SHA-256。\n\n"
        "DFT 必须满足 L < M < H；三个 acceptance 区间严格有序且不得接触或重叠。使用 `freeze-pilot-design --registry-evidence-root evidence` 和 `verify-pilot-design-receipt --registry-evidence-root evidence`。冻结仅允许采集；拟合、holdout 释放、物理排序和晋级始终为 false。\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "status": "prepared_template_only",
        "output_dir": str(output),
        "registry_sha256": sha256_file(registry_file),
        **_pilot_summary(),
        "files": sorted(path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()),
    }


def prepare_pilot(registry_path: Path | str, output_dir: Path | str) -> dict[str, Any]:
    """Publish a complete invalid operator pack from a sibling staging directory."""
    output: Path | None = None
    staging: Path | None = None
    try:
        output, staging, existed_empty = _create_staging_output(output_dir)
        result = _prepare_pilot_build(registry_path, staging)
        _validate_prepared_pilot_pack(staging)
        _publish_staging_output(output=output, staging=staging, existed_empty=existed_empty)
        result["output_dir"] = str(output)
        result["files"] = sorted(path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file())
        return result
    except OSError as error:
        _fail("OUTPUT_WRITE", str(staging or output_dir), str(error))
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _freeze_pilot_design_build(
    *,
    design_path: Path | str,
    registry_path: Path | str,
    registry_evidence_root: Path | str,
    diagnostic_receipt_path: Path | str,
    diagnostic_evidence_root: Path | str,
    output_dir: Path | str,
) -> dict[str, Any]:
    """Freeze only a verified 45-card acquisition design into a new empty directory."""
    output = _ensure_empty_output(output_dir)
    diagnostic_receipt_file = Path(diagnostic_receipt_path)
    diagnostic_binding, normalized_diagnostic = _diagnostic_binding(diagnostic_receipt_file, diagnostic_evidence_root)
    design_file = Path(design_path)
    registry_file = Path(registry_path)
    design = _normalize_design(_load_json_no_duplicates(design_file))
    registry, registry_by_id, physical_label_bindings = _normalize_registry(
        _load_json_no_duplicates(registry_file),
        registry_evidence_root=registry_evidence_root,
    )
    _validate_diagnostic_lots(normalized_diagnostic, registry_by_id)
    normalized_path = output / "normalized-pilot-design.json"
    receipt_path = output / "pilot-design-receipt.json"
    normalized_artifact_sha256 = write_json_with_sha256(normalized_path, design)
    receipt = _receipt_payload(
        design=design,
        normalized_artifact_sha256=normalized_artifact_sha256,
        input_design=_input_design_binding(design_file, design),
        registry=_registry_binding(registry_file, registry, registry_by_id, physical_label_bindings),
        diagnostic=diagnostic_binding,
    )
    receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(receipt))
    receipt_sha256 = write_json_with_sha256(receipt_path, receipt)
    return {
        "status": "pilot_roster_frozen",
        "output_dir": str(output),
        "normalized_pilot_design_sha256": receipt["normalized_pilot_design_sha256"],
        "normalized_artifact_sha256": normalized_artifact_sha256,
        "pilot_design_receipt_sha256": receipt_sha256,
        **_pilot_summary(),
        "pilot_acquisition_permitted": True,
        "model_fitting_permitted": False,
        "holdout_release_permitted": False,
        "physical_ranking_enabled": False,
        "promotion_permitted": False,
    }


def freeze_pilot_design(
    *,
    design_path: Path | str,
    registry_path: Path | str,
    registry_evidence_root: Path | str,
    diagnostic_receipt_path: Path | str,
    diagnostic_evidence_root: Path | str,
    output_dir: Path | str,
) -> dict[str, Any]:
    """Validate and publish a frozen acquisition design from sibling staging."""
    output: Path | None = None
    staging: Path | None = None
    try:
        output, staging, existed_empty = _create_staging_output(output_dir)
        result = _freeze_pilot_design_build(
            design_path=design_path,
            registry_path=registry_path,
            registry_evidence_root=registry_evidence_root,
            diagnostic_receipt_path=diagnostic_receipt_path,
            diagnostic_evidence_root=diagnostic_evidence_root,
            output_dir=staging,
        )
        verify_pilot_design_receipt(
            receipt_path=staging / "pilot-design-receipt.json",
            design_path=design_path,
            registry_path=registry_path,
            registry_evidence_root=registry_evidence_root,
            diagnostic_receipt_path=diagnostic_receipt_path,
            diagnostic_evidence_root=diagnostic_evidence_root,
        )
        _publish_staging_output(output=output, staging=staging, existed_empty=existed_empty)
        result["output_dir"] = str(output)
        return result
    except OSError as error:
        _fail("OUTPUT_WRITE", str(staging or output_dir), str(error))
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def verify_pilot_design_receipt(
    *,
    receipt_path: Path | str,
    design_path: Path | str,
    registry_path: Path | str,
    registry_evidence_root: Path | str,
    diagnostic_receipt_path: Path | str,
    diagnostic_evidence_root: Path | str,
) -> dict[str, Any]:
    """Reverify a frozen pilot design, its registry, and its four-card prerequisite."""
    receipt_file = Path(receipt_path)
    try:
        verify_sha256_sidecar(receipt_file)
    except CalibrationError as error:
        _fail("PILOT_RECEIPT_SIDECAR", str(receipt_file), str(error))
    receipt = _mapping(_load_json_no_duplicates(receipt_file), "pilot_receipt")
    _exact_fields(
        receipt,
        "pilot_receipt",
        (
            "schema_version",
            "status",
            "normalized_pilot_design_sha256",
            "normalized_artifact_sha256",
            "bindings",
            "pilot_acquisition_permitted",
            "model_fitting_permitted",
            "holdout_release_permitted",
            "physical_ranking_enabled",
            "promotion_permitted",
            "receipt_payload_sha256",
        ),
    )
    if receipt["schema_version"] != PILOT_DESIGN_RECEIPT_SCHEMA_VERSION:
        _fail("PILOT_RECEIPT_SCHEMA", "pilot_receipt.schema_version", "is not supported")
    if receipt["status"] != "pilot_roster_frozen":
        _fail("PILOT_RECEIPT_STATUS", "pilot_receipt.status", "must be pilot_roster_frozen")
    if receipt["pilot_acquisition_permitted"] is not True:
        _fail("PILOT_PERMISSION", "pilot_receipt.pilot_acquisition_permitted", "must be true")
    for flag in ("model_fitting_permitted", "holdout_release_permitted", "physical_ranking_enabled", "promotion_permitted"):
        if receipt[flag] is not False:
            _fail("PILOT_PERMISSION", f"pilot_receipt.{flag}", "must remain false")
    receipt_without_hash = dict(receipt)
    receipt_without_hash.pop("receipt_payload_sha256")
    if sha256_bytes(canonical_json_bytes(receipt_without_hash)) != _sha256(receipt["receipt_payload_sha256"], "pilot_receipt.receipt_payload_sha256"):
        _fail("PILOT_RECEIPT_PAYLOAD_SHA256", "pilot_receipt.receipt_payload_sha256", "does not bind the receipt payload")
    normalized_path = receipt_file.with_name("normalized-pilot-design.json")
    try:
        normalized_artifact_sha256 = verify_sha256_sidecar(normalized_path)
    except CalibrationError as error:
        _fail("NORMALIZED_PILOT_ARTIFACT", str(normalized_path), str(error))
    if normalized_artifact_sha256 != _sha256(receipt["normalized_artifact_sha256"], "pilot_receipt.normalized_artifact_sha256"):
        _fail("NORMALIZED_PILOT_ARTIFACT_SHA256", "pilot_receipt.normalized_artifact_sha256", "does not match normalized-pilot-design.json")
    design = _normalize_design(_load_json_no_duplicates(normalized_path))
    if sha256_bytes(canonical_json_bytes(design)) != _sha256(receipt["normalized_pilot_design_sha256"], "pilot_receipt.normalized_pilot_design_sha256"):
        _fail("NORMALIZED_PILOT_PAYLOAD_SHA256", "pilot_receipt.normalized_pilot_design_sha256", "does not bind normalized-pilot-design.json")
    bindings = _mapping(receipt["bindings"], "pilot_receipt.bindings")
    _exact_fields(bindings, "pilot_receipt.bindings", ("input_design", "registry", "diagnostic", "open_roster_commitment", "holdout_roster_commitment"))
    registry_file = Path(registry_path)
    registry, registry_by_id, physical_label_bindings = _normalize_registry(
        _load_json_no_duplicates(registry_file),
        registry_evidence_root=registry_evidence_root,
    )
    expected_registry = _registry_binding(
        registry_file, registry, registry_by_id, physical_label_bindings
    )
    if canonical_json_bytes(bindings["registry"]) != canonical_json_bytes(expected_registry):
        _fail("REGISTRY_BINDING", "pilot_receipt.bindings.registry", "does not match the revalidated registry")
    diagnostic_binding, normalized_diagnostic = _diagnostic_binding(Path(diagnostic_receipt_path), diagnostic_evidence_root)
    if canonical_json_bytes(bindings["diagnostic"]) != canonical_json_bytes(diagnostic_binding):
        _fail("DIAGNOSTIC_BINDING", "pilot_receipt.bindings.diagnostic", "does not match the reverified four-card receipt or evidence")
    _validate_diagnostic_lots(normalized_diagnostic, registry_by_id)
    design_file = Path(design_path)
    verified_input_design = _normalize_design(_load_json_no_duplicates(design_file))
    expected_input = _input_design_binding(design_file, verified_input_design)
    input_binding = _mapping(bindings["input_design"], "pilot_receipt.bindings.input_design")
    _exact_fields(input_binding, "pilot_receipt.bindings.input_design", ("design_artifact_sha256", "design_payload_sha256"))
    if canonical_json_bytes(input_binding) != canonical_json_bytes(expected_input):
        _fail("INPUT_DESIGN_BINDING", "pilot_receipt.bindings.input_design", "does not match the revalidated input design")
    open_rows = [row for row in design["roster"] if row["split"] in {"train", "validation"}]
    holdout_rows = [row for row in design["roster"] if row["split"] == "holdout"]
    if canonical_json_bytes(bindings["open_roster_commitment"]) != canonical_json_bytes(_roster_commitment(open_rows, split_name="train_validation_open")):
        _fail("OPEN_ROSTER_BINDING", "pilot_receipt.bindings.open_roster_commitment", "does not match the frozen open roster")
    if canonical_json_bytes(bindings["holdout_roster_commitment"]) != canonical_json_bytes(_roster_commitment(holdout_rows, split_name="holdout_sealed")):
        _fail("HOLDOUT_ROSTER_BINDING", "pilot_receipt.bindings.holdout_roster_commitment", "does not match the frozen holdout roster")
    return {
        "status": "pilot_roster_frozen",
        "pilot_design_receipt_verified": True,
        "pilot_acquisition_permitted": True,
        "model_fitting_permitted": False,
        "holdout_release_permitted": False,
        "physical_ranking_enabled": False,
        "promotion_permitted": False,
        **_pilot_summary(),
    }
