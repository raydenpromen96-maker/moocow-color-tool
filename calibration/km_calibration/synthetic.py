"""Deterministic synthetic-only calibration dataset generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .hashing import canonical_json_bytes, sha256_bytes, sha256_file, write_json_with_sha256
from .km import apply_saunderson, finite_film_reflectance, mix_coefficients
from .schema import DATASET_SCHEMA_VERSION


def _truth_curves(wavelengths: np.ndarray) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    normalized = (wavelengths - wavelengths.min()) / (wavelengths.max() - wavelengths.min())
    base_k = 0.07 + 0.04 * normalized
    base_s = 13.0 + 1.2 * np.sin(normalized * np.pi)
    yellow_k = 0.35 + 11.0 * np.exp(-((wavelengths - 430.0) / 42.0) ** 2)
    yellow_s = 24.0 + 1.5 * np.cos(normalized * np.pi)
    red_k = 0.45 + 10.0 * np.exp(-((wavelengths - 535.0) / 50.0) ** 2)
    red_s = 21.5 + 1.8 * np.sin(normalized * np.pi * 0.7)
    return {
        "base-clear": (base_k, base_s),
        "yellow-oxide": (yellow_k, yellow_s),
        "red-oxide": (red_k, red_s),
    }


def _component_rows(fractions: dict[str, float]) -> list[dict[str, float | str]]:
    return [
        {"component_id": component_id, "nonvolatile_volume_fraction": fraction}
        for component_id, fraction in sorted(fractions.items())
        if fraction > 0
    ]


def generate_synthetic_dataset(
    output_root: Path | str,
    *,
    seed: int = 20260714,
    noise_std: float = 0.0,
) -> dict[str, Any]:
    """Generate a deterministic, synthetic-only, hash-bound training dataset."""
    root = Path(output_root)
    if root.exists():
        if any(root.iterdir()):
            raise ValueError(f"Refusing to overwrite non-empty dataset directory: {root}")
    else:
        root.mkdir(parents=True)
    if noise_std < 0 or not np.isfinite(noise_std):
        raise ValueError("noise_std must be a finite non-negative value")

    wavelengths = np.arange(400.0, 701.0, 10.0)
    locked_conditions = {
        "geometry": "d/8",
        "instrument_id": "synthetic-spectro-v1",
        "illuminant": "D65",
        "observer": "2deg",
        "cure_protocol": "synthetic-7d-23C",
    }
    conditions_sha256 = sha256_bytes(canonical_json_bytes(locked_conditions))
    backings = {
        "white": {"reflectance": (0.93 + 0.015 * np.cos((wavelengths - 400.0) / 300.0 * np.pi)).round(12).tolist()},
        "black": {"reflectance": (0.025 + 0.008 * np.sin((wavelengths - 400.0) / 300.0 * np.pi)).round(12).tolist()},
    }
    saunderson = {"mode": "fixed", "k1": 0.035, "k2": 0.075}
    components = [
        {"component_id": "base-clear", "role": "base", "batch_id": "SYN-BASE-001"},
        {"component_id": "yellow-oxide", "role": "colorant", "batch_id": "SYN-YEL-001"},
        {"component_id": "red-oxide", "role": "colorant", "batch_id": "SYN-RED-001"},
    ]
    formulas = [
        ("family-base", "formula-base", {"base-clear": 1.0}, "train"),
        ("family-yellow-low", "formula-yellow-low", {"base-clear": 0.80, "yellow-oxide": 0.20}, "train"),
        ("family-yellow-high", "formula-yellow-high", {"base-clear": 0.55, "yellow-oxide": 0.45}, "train"),
        ("family-red-low", "formula-red-low", {"base-clear": 0.80, "red-oxide": 0.20}, "train"),
        ("family-red-high", "formula-red-high", {"base-clear": 0.55, "red-oxide": 0.45}, "train"),
        ("family-warm-validation", "formula-warm-validation", {"base-clear": 0.50, "yellow-oxide": 0.25, "red-oxide": 0.25}, "validation"),
        ("family-warm-holdout", "formula-warm-holdout", {"base-clear": 0.35, "yellow-oxide": 0.40, "red-oxide": 0.25}, "holdout"),
    ]
    truth_curves = _truth_curves(wavelengths)
    rng = np.random.default_rng(seed)
    measurements: list[dict[str, Any]] = []
    for family_id, formula_id, fractions, _split in formulas:
        formula_components = _component_rows(fractions)
        k_mix, s_mix = mix_coefficients(formula_components, truth_curves)
        for backing_name in ("black", "white"):
            for dft_um in (40.0, 105.0, 220.0):
                intrinsic = finite_film_reflectance(
                    k_mix,
                    s_mix,
                    dft_um / 1000.0,
                    np.asarray(backings[backing_name]["reflectance"], dtype=float),
                )
                measured = apply_saunderson(intrinsic, saunderson)
                for repeat_index in (1, 2):
                    noisy = np.clip(measured + rng.normal(0.0, noise_std, measured.size), 0.0, 1.0)
                    measurements.append(
                        {
                            "measurement_id": f"{formula_id}-{backing_name}-{int(dft_um)}-r{repeat_index}",
                            "formula_family_id": family_id,
                            "formula_id": formula_id,
                            "formula_batch_id": f"{formula_id}-batch-a",
                            "card_id": f"{formula_id}-card-a",
                            "sample_group_id": f"{formula_id}-{backing_name}-{int(dft_um)}",
                            "repeat_id": f"r{repeat_index}",
                            "backing": backing_name,
                            "dft_um": dft_um,
                            "conditions": locked_conditions,
                            "conditions_sha256": conditions_sha256,
                            "target_kind": "synthetic_spectrum",
                            "components": formula_components,
                            "reflectance": np.round(noisy, 12).tolist(),
                        }
                    )

    source_root = root / "sources"
    source_root.mkdir(exist_ok=False)
    measurement_source = {
        "source_schema_version": "moocow-km-calibration-source-v1",
        "dataset_status": "synthetic_only",
        "wavelength_nm": wavelengths.astype(int).tolist(),
        "locked_conditions": locked_conditions,
        "measurements": measurements,
    }
    truth_source = {
        "source_schema_version": "moocow-km-calibration-source-v1",
        "dataset_status": "synthetic_only",
        "wavelength_nm": wavelengths.astype(int).tolist(),
        "locked_conditions": locked_conditions,
        "synthetic_generator": {"seed": seed, "noise_std": noise_std},
        "component_truth_mm_inv": {
            component_id: {
                "K_mm_inv": np.round(k_curve, 12).tolist(),
                "S_mm_inv": np.round(s_curve, 12).tolist(),
            }
            for component_id, (k_curve, s_curve) in truth_curves.items()
        },
    }
    measurement_path = source_root / "synthetic-measurements.json"
    truth_path = source_root / "synthetic-truth.json"
    write_json_with_sha256(measurement_path, measurement_source)
    write_json_with_sha256(truth_path, truth_source)

    splits = {"train": [], "validation": [], "holdout": []}
    for family_id, _formula_id, _fractions, split in formulas:
        splits[split].append(family_id)
    manifest = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_status": "synthetic_only",
        "physical_ranking_enabled": False,
        "concentration_basis": "nonvolatile_volume_fraction",
        "wavelength_nm": wavelengths.astype(int).tolist(),
        "locked_conditions": locked_conditions,
        "components": components,
        "backings": backings,
        "saunderson": saunderson,
        "splits": splits,
        "source_files": [
            {
                "path": "sources/synthetic-measurements.json",
                "kind": "measurement_records",
                "sha256": sha256_file(measurement_path),
            },
            {
                "path": "sources/synthetic-truth.json",
                "kind": "synthetic_truth",
                "sha256": sha256_file(truth_path),
            },
        ],
    }
    manifest_path = root / "manifest.json"
    manifest_sha256 = write_json_with_sha256(manifest_path, manifest)
    return {
        "status": "synthetic_only",
        "dataset_root": str(root.resolve()),
        "manifest_sha256": manifest_sha256,
        "measurement_count": len(measurements),
        "seed": seed,
        "noise_std": noise_std,
    }
