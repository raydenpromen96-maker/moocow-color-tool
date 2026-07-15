"""Bounded K-M fitting, evaluation, and receipt export."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from scipy.optimize import least_squares

from .errors import DatasetValidationError, IdentifiabilityError
from .hashing import canonical_json_bytes, read_verified_json, sha256_bytes, write_json_with_sha256
from .km import apply_saunderson, finite_film_reflectance, mix_coefficients, validate_saunderson
from .schema import ValidatedDataset


MODEL_SCHEMA_VERSION = "moocow-km-two-constant-model-v1"
EVALUATION_SCHEMA_VERSION = "moocow-km-evaluation-v1"
RECEIPT_SCHEMA_VERSION = "moocow-km-research-receipt-v1"
_EVALUATION_SPLITS = ("train", "validation", "holdout")
_SYNTHETIC_EVALUATION_SPLITS = _EVALUATION_SPLITS
_RESEARCH_SELECTION_SPLITS = ("train", "validation")


@dataclass(frozen=True)
class FitOutcome:
    model: dict[str, Any]
    metrics: dict[str, Any]


def _component_ids(dataset: ValidatedDataset) -> list[str]:
    return [component["component_id"] for component in dataset.manifest["components"]]


def _records_for_split(dataset: ValidatedDataset, split: str) -> list[Mapping[str, Any]]:
    records = [
        record
        for record in dataset.records
        if dataset.family_splits[record["formula_family_id"]] == split
    ]
    expected_count = dataset.split_record_counts.get(split)
    if expected_count is None or len(records) != expected_count:
        raise DatasetValidationError("Dataset public records do not match validated split-count authority")
    return records


def _design_matrix(records: list[Mapping[str, Any]], component_ids: list[str]) -> np.ndarray:
    index = {component_id: position for position, component_id in enumerate(component_ids)}
    matrix = np.zeros((len(records), len(component_ids)), dtype=float)
    for row, record in enumerate(records):
        for component in record["components"]:
            matrix[row, index[component["component_id"]]] = float(component["nonvolatile_volume_fraction"])
    return matrix


def _assert_identifiable(records: list[Mapping[str, Any]], component_ids: list[str]) -> None:
    unique_thicknesses = {float(record["dft_um"]) for record in records}
    backings = {record["backing"] for record in records}
    if len(unique_thicknesses) < 2:
        raise IdentifiabilityError(
            "Fail closed: one DFT only identifies thickness-coupled K*t/S*t behavior, not transferable mm^-1 curves"
        )
    if backings != {"black", "white"}:
        raise IdentifiabilityError(
            "Fail closed: joint black and white backing data is required to separate finite-film optical behavior"
        )
    rank = int(np.linalg.matrix_rank(_design_matrix(records, component_ids)))
    if rank < len(component_ids):
        raise IdentifiabilityError(
            "Fail closed: formula mixture design is rank deficient and only admits ratio/coupled parameter recovery"
        )


def _split_metrics(errors: np.ndarray) -> dict[str, float]:
    if errors.size == 0:
        return {"reflectance_rmse": float("nan"), "reflectance_mae": float("nan"), "reflectance_max_abs": float("nan")}
    absolute = np.abs(errors)
    return {
        "reflectance_rmse": float(np.sqrt(np.mean(errors**2))),
        "reflectance_mae": float(np.mean(absolute)),
        "reflectance_max_abs": float(np.max(absolute)),
    }


def _model_component_curves(model: Mapping[str, Any]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for component in model["components"]:
        component_id = component["component_id"]
        k_curve = np.asarray(component["K_mm_inv"], dtype=float)
        s_curve = np.asarray(component["S_mm_inv"], dtype=float)
        curves[component_id] = (k_curve, s_curve)
    return curves


def _predict_record(model: Mapping[str, Any], dataset: ValidatedDataset, record: Mapping[str, Any]) -> np.ndarray:
    curves = _model_component_curves(model)
    k_mix, s_mix = mix_coefficients(record["components"], curves)
    backing = np.asarray(dataset.manifest["backings"][record["backing"]]["reflectance"], dtype=float)
    intrinsic = finite_film_reflectance(k_mix, s_mix, float(record["dft_um"]) / 1000.0, backing)
    return apply_saunderson(intrinsic, model["saunderson"])


def fit_km(dataset: ValidatedDataset, *, model_version: str = "km-synthetic-v1", max_nfev: int = 3000) -> FitOutcome:
    """Fit synthetic data only; physical research fitting has no v1 entry point."""
    if dataset.dataset_status == "research_only":
        raise DatasetValidationError(
            "Research-only physical fitting is forbidden; use the future receipt-gated fit-pilot-selection entry point"
        )
    if dataset.dataset_status != "synthetic_only":
        raise DatasetValidationError(f"Unsupported dataset_status for generic fit-km: {dataset.dataset_status!r}")
    training = _records_for_split(dataset, "train")
    component_ids = _component_ids(dataset)
    _assert_identifiable(training, component_ids)
    wavelengths = np.asarray(dataset.manifest["wavelength_nm"], dtype=float)
    concentrations = _design_matrix(training, component_ids)
    thicknesses = np.asarray([float(record["dft_um"]) / 1000.0 for record in training], dtype=float)
    backing_by_record = np.vstack(
        [np.asarray(dataset.manifest["backings"][record["backing"]]["reflectance"], dtype=float) for record in training]
    )
    observed = np.vstack([np.asarray(record["reflectance"], dtype=float) for record in training])
    saunderson = validate_saunderson(dataset.manifest["saunderson"])
    parameter_count = len(component_ids) * 2
    fitted_k = np.empty((len(component_ids), len(wavelengths)), dtype=float)
    fitted_s = np.empty((len(component_ids), len(wavelengths)), dtype=float)
    residuals: list[np.ndarray] = []
    jacobian_ranks: list[int] = []

    for wavelength_index in range(len(wavelengths)):
        target = observed[:, wavelength_index]
        backing = backing_by_record[:, wavelength_index]

        def residual(parameters: np.ndarray) -> np.ndarray:
            k_mix = concentrations @ parameters[: len(component_ids)]
            s_mix = concentrations @ parameters[len(component_ids) :]
            intrinsic = finite_film_reflectance(k_mix, s_mix, thicknesses, backing)
            return apply_saunderson(intrinsic, saunderson) - target

        result = least_squares(
            residual,
            x0=np.concatenate((np.full(len(component_ids), 1.0), np.full(len(component_ids), 20.0))),
            bounds=(
                np.concatenate((np.zeros(len(component_ids)), np.full(len(component_ids), 1e-6))),
                np.full(parameter_count, 500.0),
            ),
            ftol=1e-12,
            xtol=1e-12,
            gtol=1e-12,
            max_nfev=max_nfev,
        )
        jacobian_rank = int(np.linalg.matrix_rank(result.jac))
        if not result.success:
            raise IdentifiabilityError(
                f"Fail closed: bounded fit failed at {wavelengths[wavelength_index]:g} nm: {result.message}"
            )
        if jacobian_rank < parameter_count:
            raise IdentifiabilityError(
                "Fail closed: optimizer Jacobian is rank deficient; data cannot recover independent mm^-1 K and S"
            )
        fitted_k[:, wavelength_index] = result.x[: len(component_ids)]
        fitted_s[:, wavelength_index] = result.x[len(component_ids) :]
        residuals.append(result.fun)
        jacobian_ranks.append(jacobian_rank)

    all_residuals = np.concatenate(residuals)
    components = []
    manifest_components = {component["component_id"]: component for component in dataset.manifest["components"]}
    for index, component_id in enumerate(component_ids):
        components.append(
            {
                "component_id": component_id,
                "batch_id": manifest_components[component_id]["batch_id"],
                "K_mm_inv": fitted_k[index].tolist(),
                "S_mm_inv": fitted_s[index].tolist(),
            }
        )
    metrics = {
        "training_records": len(training),
        "training_fit": _split_metrics(all_residuals),
        "jacobian_rank_min": min(jacobian_ranks),
        "jacobian_rank_required": parameter_count,
        "dft_um_used": sorted({float(record["dft_um"]) for record in training}),
        "backings_used": sorted({record["backing"] for record in training}),
    }
    model = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "model_version": model_version,
        "status": dataset.dataset_status,
        "physical_ranking_enabled": False,
        "concentration_basis": "nonvolatile_volume_fraction",
        "wavelength_nm": dataset.manifest["wavelength_nm"],
        "saunderson": saunderson,
        "components": components,
        "provenance": {
            "dataset_manifest_sha256": dataset.manifest_sha256,
            "source_files": [dict(source_hash) for source_hash in dataset.source_hashes],
            "fit_split": "train",
        },
        "fit": metrics,
    }
    return FitOutcome(model=model, metrics=metrics)


def write_model(path: Path | str, model: Mapping[str, Any]) -> str:
    return write_json_with_sha256(Path(path), dict(model))


def load_and_validate_model(path: Path | str, dataset: ValidatedDataset) -> tuple[dict[str, Any], str]:
    model_path = Path(path).absolute()
    try:
        model_path.relative_to(dataset.root)
    except ValueError:
        model_trusted_root = None
    else:
        model_trusted_root = dataset.root
    model_raw, model_sha256 = read_verified_json(
        model_path,
        require_sidecar=True,
        trusted_root=model_trusted_root,
    )
    model = model_raw
    if not isinstance(model, dict) or model.get("schema_version") != MODEL_SCHEMA_VERSION:
        raise DatasetValidationError("Unsupported or malformed K-M model JSON")
    if model.get("status") != dataset.dataset_status:
        raise DatasetValidationError("Model status does not match dataset status")
    if model.get("physical_ranking_enabled") is not False:
        raise DatasetValidationError("A v1 model may never enable physical ranking")
    if model.get("concentration_basis") != "nonvolatile_volume_fraction":
        raise DatasetValidationError("Model concentration_basis must be nonvolatile_volume_fraction")
    if model.get("wavelength_nm") != list(dataset.manifest["wavelength_nm"]):
        raise DatasetValidationError("Model wavelength_nm does not match dataset")
    validate_saunderson(model.get("saunderson"))
    provenance = model.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("dataset_manifest_sha256") != dataset.manifest_sha256:
        raise DatasetValidationError("Model is not bound to this dataset manifest SHA-256")
    expected_sources = [dict(source_hash) for source_hash in dataset.source_hashes]
    if provenance.get("source_files") != expected_sources:
        raise DatasetValidationError("Model source-file SHA-256 bindings do not match the dataset")
    expected_components = _component_ids(dataset)
    raw_components = model.get("components")
    if (
        not isinstance(raw_components, list)
        or not all(isinstance(item, dict) for item in raw_components)
        or [item.get("component_id") for item in raw_components] != expected_components
    ):
        raise DatasetValidationError("Model components do not exactly match manifest component order")
    manifest_components = dataset.manifest["components"]
    for component, manifest_component in zip(raw_components, manifest_components):
        if component.get("batch_id") != manifest_component["batch_id"]:
            raise DatasetValidationError(
                f"Model component {component['component_id']} batch_id does not match manifest"
            )
        for field, strict_positive in (("K_mm_inv", False), ("S_mm_inv", True)):
            curve = np.asarray(component.get(field), dtype=float)
            if curve.shape != (len(dataset.manifest["wavelength_nm"]),) or not np.all(np.isfinite(curve)):
                raise DatasetValidationError(f"Model {component['component_id']} {field} has an invalid wavelength curve")
            if np.any(curve <= 0 if strict_positive else curve < 0):
                bound = "positive" if strict_positive else "non-negative"
                raise DatasetValidationError(f"Model {component['component_id']} {field} must be {bound}")
    return model, model_sha256


def _evaluate_splits(
    dataset: ValidatedDataset,
    model_path: Path | str,
    *,
    splits: tuple[str, ...],
) -> tuple[dict[str, Any], str]:
    """Evaluate an explicitly declared metric scope for a receipt-gated caller."""
    requested_splits = tuple(splits)
    if not requested_splits:
        raise DatasetValidationError("Evaluation split scope must not be empty")
    unknown_splits = set(requested_splits).difference(_EVALUATION_SPLITS)
    if unknown_splits:
        raise DatasetValidationError(f"Unknown evaluation split(s): {', '.join(sorted(unknown_splits))}")
    if len(set(requested_splits)) != len(requested_splits):
        raise DatasetValidationError("Evaluation split scope must not contain duplicates")
    if dataset.dataset_status == "research_only" and "holdout" in requested_splits:
        raise DatasetValidationError(
            "Research-only holdout evaluation is forbidden; use a future independent receipt-gated holdout entry point"
        )

    model, model_sha256 = load_and_validate_model(model_path, dataset)
    metrics: dict[str, Any] = {}
    for split in requested_splits:
        expected_count = dataset.split_record_counts[split]
        errors = []
        for record in _records_for_split(dataset, split):
            prediction = _predict_record(model, dataset, record)
            errors.append(prediction - np.asarray(record["reflectance"], dtype=float))
        if len(errors) != expected_count:
            raise DatasetValidationError("Evaluation records do not match validated split-count authority")
        metrics[split] = {
            "records": expected_count,
            **_split_metrics(np.concatenate(errors) if errors else np.asarray([], dtype=float)),
        }
    evaluation = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "status": dataset.dataset_status,
        "production_pass": False,
        "dataset_manifest_sha256": dataset.manifest_sha256,
        "model_sha256": model_sha256,
        "metrics": metrics,
        "promotion_note": "Synthetic or research evaluation is not a production-ranking gate; an untouched real measured-spectrum holdout is still required.",
    }
    return evaluation, model_sha256


def evaluate_model(dataset: ValidatedDataset, model_path: Path | str) -> tuple[dict[str, Any], str]:
    """Evaluate public synthetic or research-selection scope without releasing research holdout metrics."""
    status = dataset.dataset_status
    if status == "synthetic_only":
        splits = _SYNTHETIC_EVALUATION_SPLITS
    elif status == "research_only":
        splits = _RESEARCH_SELECTION_SPLITS
    else:
        raise DatasetValidationError(f"Unsupported dataset_status for evaluation: {status!r}")
    return _evaluate_splits(dataset, model_path, splits=splits)


def write_evaluation(path: Path | str, evaluation: Mapping[str, Any]) -> str:
    return write_json_with_sha256(Path(path), dict(evaluation))


def export_candidate(
    dataset: ValidatedDataset,
    model_path: Path | str,
    evaluation_path: Path | str,
    receipt_path: Path | str,
) -> tuple[dict[str, Any], str]:
    """Export an immutable research receipt that can never become production_pass."""
    model_file = Path(model_path).absolute()
    _model, model_sha256 = load_and_validate_model(model_file, dataset)
    evaluation_file = Path(evaluation_path).absolute()
    try:
        evaluation_file.relative_to(dataset.root)
    except ValueError:
        evaluation_trusted_root = None
    else:
        evaluation_trusted_root = dataset.root
    evaluation_raw, evaluation_sha256 = read_verified_json(
        evaluation_file,
        require_sidecar=True,
        trusted_root=evaluation_trusted_root,
    )
    evaluation = evaluation_raw
    expected_evaluation_fields = {
        "schema_version",
        "status",
        "production_pass",
        "dataset_manifest_sha256",
        "model_sha256",
        "metrics",
        "promotion_note",
    }
    if not isinstance(evaluation, dict) or set(evaluation) != expected_evaluation_fields:
        raise DatasetValidationError("Malformed evaluation JSON")
    if evaluation.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise DatasetValidationError("Unsupported evaluation schema_version")
    if evaluation.get("status") != dataset.dataset_status:
        raise DatasetValidationError("Evaluation status does not match dataset status")
    if evaluation.get("dataset_manifest_sha256") != dataset.manifest_sha256 or evaluation.get("model_sha256") != model_sha256:
        raise DatasetValidationError("Evaluation hashes do not bind this dataset/model pair")
    if evaluation.get("production_pass") is not False:
        raise DatasetValidationError("Refusing export: v1 receipts may never carry production_pass")
    if not isinstance(evaluation["promotion_note"], str) or not evaluation["promotion_note"].strip():
        raise DatasetValidationError("Evaluation promotion_note must be a non-empty string")

    metrics = evaluation["metrics"]
    expected_splits = (
        _SYNTHETIC_EVALUATION_SPLITS
        if dataset.dataset_status == "synthetic_only"
        else _RESEARCH_SELECTION_SPLITS
    )
    if not isinstance(metrics, dict) or set(metrics) != set(expected_splits):
        raise DatasetValidationError(
            f"Evaluation metrics must contain exactly {', '.join(expected_splits)} for {dataset.dataset_status}"
        )
    expected_metric_fields = {
        "records",
        "reflectance_rmse",
        "reflectance_mae",
        "reflectance_max_abs",
    }
    for split in expected_splits:
        split_metrics = metrics[split]
        if not isinstance(split_metrics, dict) or set(split_metrics) != expected_metric_fields:
            raise DatasetValidationError(f"Evaluation metrics.{split} has an invalid field set")
        records = split_metrics["records"]
        if (
            isinstance(records, bool)
            or not isinstance(records, int)
            or records != dataset.split_record_counts[split]
        ):
            raise DatasetValidationError(
                f"Evaluation metrics.{split}.records does not match validated split-count authority"
            )
        for field in ("reflectance_rmse", "reflectance_mae", "reflectance_max_abs"):
            value = split_metrics[field]
            try:
                is_finite = not isinstance(value, bool) and isinstance(value, (int, float)) and np.isfinite(float(value))
            except OverflowError:
                is_finite = False
            if not is_finite:
                raise DatasetValidationError(f"Evaluation metrics.{split}.{field} must be finite")
    try:
        model_logical_path = model_file.relative_to(dataset.root).as_posix()
    except ValueError:
        model_logical_path = model_file.name
    try:
        evaluation_logical_path = evaluation_file.relative_to(dataset.root).as_posix()
    except ValueError:
        evaluation_logical_path = evaluation_file.name
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "status": dataset.dataset_status,
        "production_pass": False,
        "bindings": {
            "manifest": {"path": "manifest.json", "sha256": dataset.manifest_sha256},
            "source_files": [dict(source_hash) for source_hash in dataset.source_hashes],
            "model": {"path": model_logical_path, "sha256": model_sha256},
            "evaluation": {"path": evaluation_logical_path, "sha256": evaluation_sha256},
        },
        "promotion": {
            "state": "requires_real_measured_holdout",
            "production_pass": False,
            "requirement": "Promotion requires an independently held-out, real measured-spectrum dataset with physical holdout review; synthetic or research-only evidence cannot enable production ranking.",
        },
    }
    receipt["receipt_payload_sha256"] = sha256_bytes(canonical_json_bytes(receipt))
    receipt_sha256 = write_json_with_sha256(Path(receipt_path), receipt)
    return receipt, receipt_sha256
