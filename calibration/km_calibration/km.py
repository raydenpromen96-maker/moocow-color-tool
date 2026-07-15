"""Finite-film two-constant Kubelka-Munk and fixed Saunderson transforms."""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

from .errors import DatasetValidationError


def validate_saunderson(config: Mapping[str, object]) -> dict[str, float | str]:
    if not isinstance(config, Mapping):
        raise DatasetValidationError("saunderson must be an object")
    mode = config.get("mode")
    if mode == "off":
        if set(config) != {"mode"}:
            raise DatasetValidationError("Saunderson off mode cannot carry fitted parameters")
        return {"mode": "off"}
    if mode != "fixed":
        raise DatasetValidationError("Saunderson mode must be exactly 'off' or 'fixed'")
    if set(config) != {"mode", "k1", "k2"}:
        raise DatasetValidationError("Fixed Saunderson requires exactly mode, k1, and k2")
    k1 = config["k1"]
    k2 = config["k2"]
    if isinstance(k1, bool) or isinstance(k2, bool):
        raise DatasetValidationError("Saunderson constants must be numeric")
    try:
        k1 = float(k1)
        k2 = float(k2)
    except (TypeError, ValueError) as error:
        raise DatasetValidationError("Saunderson constants must be numeric") from error
    if not (np.isfinite(k1) and np.isfinite(k2) and 0 <= k1 < 1 and 0 <= k2 < 1):
        raise DatasetValidationError("Saunderson fixed constants must be finite values in [0, 1)")
    return {"mode": "fixed", "k1": k1, "k2": k2}


def _as_nonnegative_array(value: object, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=float)
    if not np.all(np.isfinite(array)) or np.any(array < 0):
        raise DatasetValidationError(f"{name} must contain finite non-negative values")
    return array


def finite_film_reflectance(
    k_mm_inv: object,
    s_mm_inv: object,
    thickness_mm: object,
    backing_reflectance: object,
) -> np.ndarray:
    """Evaluate the architect-specified finite-film two-constant K-M equation.

    `u = b*coth(b*S*t)` is evaluated with a small-argument series so zero or
    near-zero absorption remains stable rather than producing 0/0 numerics.
    Scattering must remain strictly positive because K/S is otherwise undefined.
    """
    k = _as_nonnegative_array(k_mm_inv, "K")
    s = np.asarray(s_mm_inv, dtype=float)
    t = np.asarray(thickness_mm, dtype=float)
    rg = np.asarray(backing_reflectance, dtype=float)
    if not np.all(np.isfinite(s)) or np.any(s <= 0):
        raise DatasetValidationError("S must contain finite values strictly greater than zero")
    if not np.all(np.isfinite(t)) or np.any(t <= 0):
        raise DatasetValidationError("Film thickness must contain finite values strictly greater than zero")
    if not np.all(np.isfinite(rg)) or np.any((rg < 0) | (rg > 1)):
        raise DatasetValidationError("Backing reflectance must remain in [0, 1]")

    k, s, t, rg = np.broadcast_arrays(k, s, t, rg)
    ratio = k / s
    a = 1.0 + ratio
    b = np.sqrt(ratio * (ratio + 2.0))
    scattering_thickness = s * t
    argument = b * scattering_thickness
    small = np.abs(argument) < 1e-5
    u = np.empty_like(argument, dtype=float)
    if np.any(small):
        q = scattering_thickness[small]
        b_small = b[small]
        # b*coth(b*q) = 1/q + b^2*q/3 - b^4*q^3/45 + O((b*q)^5).
        u[small] = (
            1.0 / q
            + (b_small**2 * q) / 3.0
            - (b_small**4 * q**3) / 45.0
            + (2.0 * b_small**6 * q**5) / 945.0
        )
    if np.any(~small):
        u[~small] = b[~small] / np.tanh(argument[~small])
    denominator = a - rg + u
    if np.any(denominator <= 0) or not np.all(np.isfinite(denominator)):
        raise DatasetValidationError("Finite-film K-M denominator is invalid")
    result = (1.0 - rg * (a - u)) / denominator
    if not np.all(np.isfinite(result)):
        raise DatasetValidationError("Finite-film K-M result is non-finite")
    return np.clip(result, 0.0, 1.0)


def apply_saunderson(intrinsic_reflectance: object, config: Mapping[str, object]) -> np.ndarray:
    """Map intrinsic K-M reflectance to fixed measured reflectance."""
    normalized = validate_saunderson(config)
    reflectance = np.asarray(intrinsic_reflectance, dtype=float)
    if not np.all(np.isfinite(reflectance)) or np.any((reflectance < 0) | (reflectance > 1)):
        raise DatasetValidationError("Intrinsic reflectance must remain in [0, 1]")
    if normalized["mode"] == "off":
        return reflectance
    k1 = float(normalized["k1"])
    k2 = float(normalized["k2"])
    result = k1 + (1.0 - k1) * (1.0 - k2) * reflectance / (1.0 - k2 * reflectance)
    return np.clip(result, 0.0, 1.0)


def remove_saunderson(measured_reflectance: object, config: Mapping[str, object]) -> np.ndarray:
    """Invert `apply_saunderson` for the same fixed constants."""
    normalized = validate_saunderson(config)
    reflectance = np.asarray(measured_reflectance, dtype=float)
    if not np.all(np.isfinite(reflectance)) or np.any((reflectance < 0) | (reflectance > 1)):
        raise DatasetValidationError("Measured reflectance must remain in [0, 1]")
    if normalized["mode"] == "off":
        return reflectance
    k1 = float(normalized["k1"])
    k2 = float(normalized["k2"])
    shifted = reflectance - k1
    denominator = (1.0 - k1) * (1.0 - k2) + k2 * shifted
    if np.any(denominator <= 0):
        raise DatasetValidationError("Saunderson inverse denominator is invalid")
    return np.clip(shifted / denominator, 0.0, 1.0)


def mix_coefficients(
    component_fractions: Sequence[Mapping[str, object]],
    component_curves: Mapping[str, tuple[object, object]],
) -> tuple[np.ndarray, np.ndarray]:
    """Return K_mix=sum(phi*K_j), S_mix=sum(phi*S_j) in mm^-1."""
    k_mix: np.ndarray | None = None
    s_mix: np.ndarray | None = None
    for item in component_fractions:
        component_id = str(item["component_id"])
        fraction = float(item["nonvolatile_volume_fraction"])
        try:
            k_curve, s_curve = component_curves[component_id]
        except KeyError as error:
            raise DatasetValidationError(f"Model lacks component curve for {component_id}") from error
        k = _as_nonnegative_array(k_curve, f"K curve for {component_id}")
        s = np.asarray(s_curve, dtype=float)
        if np.any(s <= 0) or not np.all(np.isfinite(s)):
            raise DatasetValidationError(f"S curve for {component_id} must be strictly positive")
        k_mix = fraction * k if k_mix is None else k_mix + fraction * k
        s_mix = fraction * s if s_mix is None else s_mix + fraction * s
    if k_mix is None or s_mix is None:
        raise DatasetValidationError("Formula must contain at least one component")
    return k_mix, s_mix
