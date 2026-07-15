"""Receipt-gated open-selection finite-film K-M fitting and export.

This module is deliberately separate from the synthetic-only pipeline.  It
accepts only a reverified open-measurement admission, fits grouped train cell
means, selects a frozen train-only candidate with validation data, and writes a
non-promotable package whose verifier reconstructs the complete calculation.
"""

from __future__ import annotations

import copy
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from stat import S_ISDIR, S_ISLNK, S_ISREG
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import coo_matrix, csr_matrix, vstack

from .acquisition_preflight import COMPONENT_IDS, PERMISSIONS
from .errors import CalibrationError
from .hashing import canonical_json_bytes, read_verified_json, sha256_bytes, write_json_with_sha256
from .open_measurement_admission import (
    ValidatedOpenSelectionDataset,
    load_and_validate_open_selection_dataset,
    verify_open_measurement_admission,
)


FIT_MODEL_SCHEMA = "moocow-open-selection-km-fit-model-v1"
SELECTION_EVALUATION_SCHEMA = "moocow-open-selection-km-selection-evaluation-v1"
FIT_EXPORT_RECEIPT_SCHEMA = "moocow-open-selection-km-fit-export-receipt-v1"
FIT_SPEC_SCHEMA = "moocow-open-selection-km-fit-spec-v1"
_FIT_ALGORITHM = "joint-all-wavelength-transformed-two-constant-finite-film-km-v1"
_BACKINGS = ("black", "white")
_REPOSITIONS = ("POS01", "POS02", "POS03")
_SPLITS = ("train", "validation")
_REGULARIZATION_GRID = (0.0, 1e-6, 1e-4, 1e-2, 1.0)
_ETA_LOWER = math.log(1e-6)
_ETA_UPPER = math.log(1e6)
_RHO_LOWER = 0.0
_RHO_UPPER = math.log1p(1e6)
_OPTIMIZER_TOLERANCE = 1e-10
_MAX_NFEV = 3000
_PREDICTION_ROUNDOFF = 32.0 * np.finfo(float).eps
_BOUND_TOLERANCE = 1e-8
_SEMANTIC_ATOL = 1e-8
_SEMANTIC_RTOL = 1e-7
_MAX_SCALED_DESIGN_CONDITION = 1.0 / math.sqrt(np.finfo(float).eps)
_OUTPUT_FILES = {
    "fit-model.json",
    "fit-model.json.sha256",
    "selection-evaluation.json",
    "selection-evaluation.json.sha256",
    "fit-export-receipt.json",
    "fit-export-receipt.json.sha256",
}
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class OpenSelectionFitExportError(CalibrationError):
    """Stable, non-secret-bearing failure for the open-selection fit boundary."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"[{code}] {path}: {message}")


@dataclass(frozen=True)
class _Cell:
    card_id: str
    formula_family_id: str
    formula_id: str
    formula_batch_id: str
    split: str
    dft_band: str
    backing: str
    thickness_mm: float
    concentrations: np.ndarray
    observed: np.ndarray
    raw_replicates: np.ndarray
    reposition_ids: tuple[str, ...]


@dataclass(frozen=True)
class _FitData:
    component_pairs: tuple[tuple[str, str], ...]
    wavelengths: np.ndarray
    backing_means: Mapping[str, np.ndarray]
    train_cells: tuple[_Cell, ...]
    validation_cells: tuple[_Cell, ...]
    t_ref_mm: float
    design_diagnostics: Mapping[str, Any]
    train_projection: Mapping[str, Any]
    validation_projection: Mapping[str, Any]


def _fail(code: str, path: str, message: str) -> None:
    raise OpenSelectionFitExportError(code, path, message)


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("TYPE", path, "must be an object")
    return value


def _list(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("TYPE", path, "must be an array")
    return value


def _exact(value: Mapping[str, Any], path: str, fields: Sequence[str]) -> None:
    expected = set(fields)
    actual = set(value)
    if actual != expected:
        _fail("FIELDS", path, f"must contain exactly {sorted(expected)}; missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}")


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("TYPE", path, "must be a non-empty string")
    return value.strip()


def _sha256(value: object, path: str) -> str:
    if not isinstance(value, str):
        _fail("SHA256", path, "must be a SHA-256 hex digest")
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        _fail("SHA256", path, "must be a SHA-256 hex digest")
    return normalized


def _finite_number(value: object, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        _fail("NUMBER", path, "must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        _fail("NUMBER", path, "must be a finite number")
        raise AssertionError("unreachable") from error
    if not np.isfinite(number) or (positive and number <= 0.0):
        _fail("NUMBER", path, "must be finite" + (" and positive" if positive else ""))
    return number


def _permissions() -> dict[str, bool]:
    return {permission: False for permission in PERMISSIONS}


def _assert_permissions(value: Mapping[str, Any], path: str) -> None:
    for permission in PERMISSIONS:
        if value.get(permission) is not False:
            _fail("PERMISSIONS", f"{path}.{permission}", "must remain false")


def _portable_candidate_path(value: object, path: str) -> str:
    text = _text(value, path)
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or "\\" in text or any(part in {"", ".", ".."} for part in candidate.parts):
        _fail("PATH", path, "must be a portable candidate-local POSIX path")
    if len(candidate.parts) != 1 or candidate.name not in {"fit-model.json", "selection-evaluation.json", "fit-export-receipt.json"}:
        _fail("PATH", path, "must name one candidate-local artifact")
    return candidate.as_posix()


def _is_link_or_reparse(path_stat: os.stat_result) -> bool:
    return S_ISLNK(path_stat.st_mode) or bool(
        getattr(path_stat, "st_file_attributes", 0) & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    )


def _assert_no_link_components(path: Path, path_name: str) -> None:
    """Reject any existing parent component that would redirect package I/O."""

    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            _fail("PATH", path_name, f"cannot inspect path component {current}: {error}")
        if _is_link_or_reparse(component_stat) or not S_ISDIR(component_stat.st_mode):
            _fail("PATH", path_name, f"must not traverse a link, reparse point, or non-directory component: {current}")


def _validate_candidate_root(value: Path | str, path: str) -> Path:
    root = Path(value)
    _assert_no_link_components(root.parent, path)
    try:
        stat_result = root.lstat()
    except OSError as error:
        _fail("PACKAGE", path, f"cannot inspect candidate package: {error}")
        raise AssertionError("unreachable") from error
    if _is_link_or_reparse(stat_result) or not S_ISDIR(stat_result.st_mode):
        _fail("PACKAGE", path, "must be a non-link directory")
    return root.resolve(strict=True)


def _validate_candidate_tree(root: Path) -> None:
    actual: set[str] = set()
    for member in root.rglob("*"):
        relative = member.relative_to(root).as_posix()
        try:
            member_stat = member.lstat()
        except OSError as error:
            _fail("PACKAGE", relative, f"cannot inspect package member: {error}")
        if _is_link_or_reparse(member_stat) or not S_ISREG(member_stat.st_mode) or member_stat.st_nlink > 1:
            _fail("PACKAGE", relative, "must be a regular non-link non-hard-linked file")
        actual.add(relative)
    if actual != _OUTPUT_FILES:
        _fail("PACKAGE", "candidate_root", f"must contain exactly {sorted(_OUTPUT_FILES)}; found {sorted(actual)}")


def _prepare_output(output_dir: Path | str) -> tuple[Path, Path, bool]:
    output = Path(output_dir)
    _assert_no_link_components(output.parent, "output_dir")
    if output.exists() or output.is_symlink():
        try:
            output_stat = output.lstat()
        except OSError as error:
            _fail("OUTPUT", "output_dir", f"cannot inspect output: {error}")
        if _is_link_or_reparse(output_stat) or not S_ISDIR(output_stat.st_mode):
            _fail("OUTPUT", "output_dir", "must be a new or empty non-link directory")
        try:
            if any(output.iterdir()):
                _fail("OUTPUT", "output_dir", "must be empty when it already exists")
        except OSError as error:
            _fail("OUTPUT", "output_dir", f"cannot inspect output contents: {error}")
        existed_empty = True
    else:
        existed_empty = False
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        _fail("OUTPUT", "output_dir", f"cannot create output parent: {error}")
    _assert_no_link_components(output.parent, "output_dir")
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        if staging.exists() or staging.is_symlink():
            _fail("OUTPUT", "output_dir", "staging path unexpectedly exists")
        staging.mkdir()
    except OSError as error:
        _fail("OUTPUT", "output_dir", f"cannot create staging directory: {error}")
    return output, staging, existed_empty


def _publish(output: Path, staging: Path, existed_empty: bool) -> None:
    try:
        if existed_empty:
            output.rmdir()
        os.replace(staging, output)
    except OSError as error:
        if existed_empty and not output.exists():
            try:
                output.mkdir(parents=True, exist_ok=False)
            except OSError:
                pass
        _fail("OUTPUT", "output_dir", f"cannot publish atomic package: {error}")


def _artifact_payload_sha256(value: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _receipt_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("receipt_payload_sha256", None)
    return payload


def _reject_forbidden_scope(value: object, path: str = "$") -> None:
    """Keep the package strictly outside custody, holdout, and independent evaluation."""

    prohibited = ("holdout", "sealed", "custody", "independent")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail("SCOPE", path, "object keys must be strings")
            if key == "holdout_release_permitted" and child is False:
                continue
            if any(marker in key.casefold() for marker in prohibited):
                _fail("SCOPE", f"{path}.{key}", "is outside the open-selection boundary")
            _reject_forbidden_scope(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_scope(child, f"{path}[{index}]")
    elif isinstance(value, str) and any(marker in value.casefold() for marker in prohibited):
        _fail("SCOPE", path, "contains a prohibited scope marker")


def _strict_reflectance_and_partials(
    s_mix: np.ndarray,
    k_mix: np.ndarray,
    thickness_mm: np.ndarray,
    backing: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return un-clipped finite-film reflectance and partials for one wavelength.

    The two derivatives are with respect to the K/S ratio and the optical
    thickness ``S*t``.  The small-argument branch differentiates the same
    stable series used by the core finite-film implementation.
    """

    if (
        not np.all(np.isfinite(s_mix))
        or not np.all(np.isfinite(k_mix))
        or not np.all(np.isfinite(thickness_mm))
        or not np.all(np.isfinite(backing))
        or np.any(s_mix <= 0.0)
        or np.any(k_mix < 0.0)
        or np.any(thickness_mm <= 0.0)
        or np.any((backing < 0.0) | (backing > 1.0))
    ):
        _fail("PREDICTION", "finite_film", "received invalid finite-film inputs")

    ratio = k_mix / s_mix
    optical_thickness = s_mix * thickness_mm
    a = 1.0 + ratio
    b_squared = ratio * (ratio + 2.0)
    b = np.sqrt(b_squared)
    z = b * optical_thickness
    small = np.abs(z) < 1e-5
    u = np.empty_like(z)
    du_dratio = np.empty_like(z)
    du_doptical_thickness = np.empty_like(z)

    if np.any(small):
        q = optical_thickness[small]
        b2 = b_squared[small]
        a_small = a[small]
        b4 = b2**2
        b6 = b2**3
        u[small] = 1.0 / q + b2 * q / 3.0 - b4 * q**3 / 45.0 + 2.0 * b6 * q**5 / 945.0
        du_dratio[small] = (
            2.0 * a_small * q / 3.0
            - 4.0 * a_small * b2 * q**3 / 45.0
            + 4.0 * a_small * b4 * q**5 / 315.0
        )
        du_doptical_thickness[small] = (
            -1.0 / q**2 + b2 / 3.0 - b4 * q**2 / 15.0 + 2.0 * b6 * q**4 / 189.0
        )
    if np.any(~small):
        z_large = z[~small]
        b_large = b[~small]
        a_large = a[~small]
        large = z_large > 20.0
        coth = np.empty_like(z_large)
        csch_squared = np.empty_like(z_large)
        if np.any(large):
            coth[large] = 1.0
            csch_squared[large] = 0.0
        if np.any(~large):
            z_regular = z_large[~large]
            coth[~large] = 1.0 / np.tanh(z_regular)
            csch_squared[~large] = np.maximum(0.0, coth[~large] * coth[~large] - 1.0)
        u[~small] = b_large * coth
        du_dratio[~small] = (a_large / b_large) * (coth - z_large * csch_squared)
        du_doptical_thickness[~small] = -(b_large**2) * csch_squared

    numerator = 1.0 - backing * (a - u)
    denominator = a - backing + u
    if np.any(denominator <= 0.0) or not np.all(np.isfinite(denominator)):
        _fail("PREDICTION", "finite_film", "produced an invalid denominator")
    reflectance = numerator / denominator
    if not np.all(np.isfinite(reflectance)) or np.any(
        (reflectance < -_PREDICTION_ROUNDOFF) | (reflectance > 1.0 + _PREDICTION_ROUNDOFF)
    ):
        _fail("PREDICTION", "finite_film", "produced reflectance outside machine-roundoff of [0, 1]")
    d_reflectance_da = (-backing * denominator - numerator) / (denominator**2)
    d_reflectance_du = (backing * denominator - numerator) / (denominator**2)
    return np.clip(reflectance, 0.0, 1.0), d_reflectance_da, d_reflectance_du, np.vstack(
        (du_dratio, du_doptical_thickness)
    )


def _prediction_and_data_jacobian(
    data: _FitData,
    parameters: np.ndarray,
    cells: Sequence[_Cell],
) -> tuple[np.ndarray, csr_matrix]:
    component_count = len(data.component_pairs)
    wavelength_count = len(data.wavelengths)
    parameter_count = component_count * wavelength_count * 2
    expected_shape = (parameter_count,)
    if parameters.shape != expected_shape:
        _fail("PARAMETERS", "parameters", f"must have shape {expected_shape}")
    eta = parameters[: component_count * wavelength_count].reshape(component_count, wavelength_count)
    rho = parameters[component_count * wavelength_count :].reshape(component_count, wavelength_count)
    scattering = np.exp(eta) / data.t_ref_mm
    ratio_by_component = np.expm1(rho)
    absorption = scattering * ratio_by_component
    concentrations = np.vstack([cell.concentrations for cell in cells])
    thicknesses = np.asarray([cell.thickness_mm for cell in cells], dtype=float)
    prediction = np.empty((len(cells), wavelength_count), dtype=float)
    rows: list[np.ndarray] = []
    columns: list[np.ndarray] = []
    values: list[np.ndarray] = []
    cell_indexes = np.arange(len(cells), dtype=int)

    for wavelength_index in range(wavelength_count):
        s_component = scattering[:, wavelength_index]
        k_component = absorption[:, wavelength_index]
        ratio_component = ratio_by_component[:, wavelength_index]
        s_mix = concentrations @ s_component
        k_mix = concentrations @ k_component
        backing = np.asarray(
            [data.backing_means[cell.backing][wavelength_index] for cell in cells], dtype=float
        )
        reflected, d_reflectance_da, d_reflectance_du, du = _strict_reflectance_and_partials(
            s_mix, k_mix, thicknesses, backing
        )
        prediction[:, wavelength_index] = reflected
        ratio_mix = k_mix / s_mix
        d_ratio = d_reflectance_da + d_reflectance_du * du[0]
        d_optical = d_reflectance_du * du[1]
        eta_derivative = (
            d_ratio[:, None]
            * concentrations
            * s_component[None, :]
            * (ratio_component[None, :] - ratio_mix[:, None])
            / s_mix[:, None]
            + d_optical[:, None] * thicknesses[:, None] * concentrations * s_component[None, :]
        )
        rho_derivative = (
            d_ratio[:, None]
            * concentrations
            * s_component[None, :]
            * (1.0 + ratio_component[None, :])
            / s_mix[:, None]
        )
        row = cell_indexes * wavelength_count + wavelength_index
        for component_index in range(component_count):
            rows.extend((row, row))
            columns.extend(
                (
                    np.full(len(cells), component_index * wavelength_count + wavelength_index, dtype=int),
                    np.full(
                        len(cells),
                        component_count * wavelength_count + component_index * wavelength_count + wavelength_index,
                        dtype=int,
                    ),
                )
            )
            values.extend((eta_derivative[:, component_index], rho_derivative[:, component_index]))

    return prediction, coo_matrix(
        (np.concatenate(values), (np.concatenate(rows), np.concatenate(columns))),
        shape=(len(cells) * wavelength_count, parameter_count),
    ).tocsr()


def _second_difference(values: np.ndarray) -> np.ndarray:
    return values[:, 2:] - 2.0 * values[:, 1:-1] + values[:, :-2]


def _residual_and_jacobian(
    data: _FitData, parameters: np.ndarray, regularization: float
) -> tuple[np.ndarray, csr_matrix, np.ndarray, csr_matrix]:
    component_count = len(data.component_pairs)
    wavelength_count = len(data.wavelengths)
    train_count = len(data.train_cells)
    prediction, data_jacobian = _prediction_and_data_jacobian(data, parameters, data.train_cells)
    observed = np.vstack([cell.observed for cell in data.train_cells])
    data_scale = 1.0 / math.sqrt(train_count * wavelength_count)
    residual = (prediction - observed).reshape(-1) * data_scale
    jacobian = data_jacobian * data_scale
    if regularization == 0.0:
        return residual, jacobian, prediction, data_jacobian

    eta = parameters[: component_count * wavelength_count].reshape(component_count, wavelength_count)
    rho = parameters[component_count * wavelength_count :].reshape(component_count, wavelength_count)
    regularization_scale = math.sqrt(regularization / (component_count * (wavelength_count - 2)))
    regularization_residual = np.concatenate(
        (
            (_second_difference(eta) * regularization_scale).reshape(-1),
            (_second_difference(rho) * regularization_scale).reshape(-1),
        )
    )
    regularization_rows: list[int] = []
    regularization_columns: list[int] = []
    regularization_values: list[float] = []
    per_curve = wavelength_count - 2
    for block in range(2):
        offset = block * component_count * wavelength_count
        row_offset = block * component_count * per_curve
        for component_index in range(component_count):
            for center in range(1, wavelength_count - 1):
                row = row_offset + component_index * per_curve + center - 1
                start = offset + component_index * wavelength_count
                regularization_rows.extend((row, row, row))
                regularization_columns.extend((start + center - 1, start + center, start + center + 1))
                regularization_values.extend(
                    (regularization_scale, -2.0 * regularization_scale, regularization_scale)
                )
    regularization_jacobian = coo_matrix(
        (regularization_values, (regularization_rows, regularization_columns)),
        shape=(len(regularization_residual), component_count * wavelength_count * 2),
    ).tocsr()
    return np.concatenate((residual, regularization_residual)), vstack((jacobian, regularization_jacobian), format="csr"), prediction, data_jacobian


def _analytic_jacobian_finite_difference_check() -> dict[str, float]:
    """Deterministically compare the internal sparse analytic Jacobian to finite differences.

    This private helper is intentionally verification-only: it does not alter
    fit settings or expose any production/tuning surface.
    """

    wavelengths = np.asarray((400.0, 420.0, 440.0, 460.0), dtype=float)
    pairs = (("component-a", "lot-a"), ("component-b", "lot-b"))
    backing = {
        "black": np.asarray((0.08, 0.09, 0.10, 0.11), dtype=float),
        "white": np.asarray((0.78, 0.77, 0.76, 0.75), dtype=float),
    }
    cells = tuple(
        _Cell(
            card_id=f"card-{index}",
            formula_family_id=f"family-{index}",
            formula_id=f"formula-{index}",
            formula_batch_id=f"batch-{index}",
            split="train",
            dft_band="DFT-L" if index % 2 == 0 else "DFT-H",
            backing="black" if index < 2 else "white",
            thickness_mm=100.0 if index == 3 else 0.030 + 0.010 * (index % 2),
            concentrations=np.asarray(concentrations, dtype=float),
            observed=np.asarray((0.30, 0.31, 0.32, 0.33), dtype=float),
            raw_replicates=np.tile(np.asarray((0.30, 0.31, 0.32, 0.33), dtype=float), (3, 1)),
            reposition_ids=("POS01", "POS02", "POS03"),
        )
        for index, concentrations in enumerate(((1.0, 0.0), (0.4, 0.6), (1.0, 0.0), (0.4, 0.6)))
    )
    data = _FitData(
        component_pairs=pairs,
        wavelengths=wavelengths,
        backing_means=backing,
        train_cells=cells,
        validation_cells=(),
        t_ref_mm=0.035,
        design_diagnostics={},
        train_projection={},
        validation_projection={},
    )
    parameters = np.linspace(-0.4, 0.55, len(pairs) * len(wavelengths) * 2, dtype=float)
    analytic_residual, analytic_jacobian, _prediction, _data_jacobian = _residual_and_jacobian(data, parameters, 1e-4)
    step = 1e-6
    numerical = np.empty((len(analytic_residual), len(parameters)), dtype=float)
    for index in range(len(parameters)):
        shifted_plus = parameters.copy()
        shifted_minus = parameters.copy()
        shifted_plus[index] += step
        shifted_minus[index] -= step
        plus, _jacobian, _prediction, _data_jacobian = _residual_and_jacobian(data, shifted_plus, 1e-4)
        minus, _jacobian, _prediction, _data_jacobian = _residual_and_jacobian(data, shifted_minus, 1e-4)
        numerical[:, index] = (plus - minus) / (2.0 * step)
    difference = analytic_jacobian.toarray() - numerical
    denominator = np.maximum(1e-9, np.abs(numerical))
    return {
        "max_abs_error": float(np.max(np.abs(difference))),
        "max_relative_error": float(np.max(np.abs(difference) / denominator)),
    }


def _array(value: object, path: str, *, length: int, unit_interval: bool = False) -> np.ndarray:
    items = _list(value, path)
    if len(items) != length:
        _fail("SPECTRUM", path, f"must contain exactly {length} values")
    result = np.asarray([_finite_number(item, f"{path}[{index}]") for index, item in enumerate(items)], dtype=float)
    if unit_interval and np.any((result < 0.0) | (result > 1.0)):
        _fail("SPECTRUM", path, "must remain in [0, 1]")
    return result


def _validate_component_pairs(manifest: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    components = _list(manifest.get("components"), "manifest.components")
    if len(components) != 15:
        _fail("COMPONENTS", "manifest.components", "must contain exactly 15 components")
    pairs: list[tuple[str, str]] = []
    for index, component in enumerate(components):
        item = _mapping(component, f"manifest.components[{index}]")
        component_id = _text(item.get("component_id"), f"manifest.components[{index}].component_id")
        lot_id = _text(item.get("physical_lot_id"), f"manifest.components[{index}].physical_lot_id")
        pairs.append((component_id, lot_id))
    if tuple(component_id for component_id, _lot_id in pairs) != tuple(COMPONENT_IDS):
        _fail("COMPONENTS", "manifest.components", "must retain the fixed component order")
    if len(set(pairs)) != len(pairs):
        _fail("COMPONENTS", "manifest.components", "component/lot pairs must be unique")
    return tuple(pairs)


def _validate_wavelength_grid(value: object) -> np.ndarray:
    wavelengths = np.asarray(
        [_finite_number(item, f"manifest.wavelength_nm[{index}]") for index, item in enumerate(_list(value, "manifest.wavelength_nm"))],
        dtype=float,
    )
    if len(wavelengths) < 3 or not np.all(np.diff(wavelengths) > 0.0):
        _fail("WAVELENGTHS", "manifest.wavelength_nm", "must be strictly increasing with at least three values")
    deltas = np.diff(wavelengths)
    if not np.allclose(deltas, deltas[0], rtol=0.0, atol=1e-9):
        _fail("WAVELENGTHS", "manifest.wavelength_nm", "must be uniform")
    if wavelengths[0] > 400.0 or wavelengths[-1] < 700.0 or deltas[0] > 20.0:
        _fail("WAVELENGTHS", "manifest.wavelength_nm", "must uniformly cover 400-700 nm with step no greater than 20 nm")
    return wavelengths


def _design_diagnostics(cells: Sequence[_Cell]) -> dict[str, Any]:
    by_family: dict[str, np.ndarray] = {}
    for cell in cells:
        existing = by_family.setdefault(cell.formula_family_id, cell.concentrations)
        if not np.array_equal(existing, cell.concentrations):
            _fail("DESIGN", cell.formula_family_id, "must have one stable actual-NV vector")
    if len(by_family) != 15:
        _fail("DESIGN", "train", "must contain exactly 15 formula-family design rows")
    design = np.vstack([by_family[family] for family in sorted(by_family)])
    column_norms = np.linalg.norm(design, axis=0)
    if np.any(column_norms <= 0.0) or not np.all(np.isfinite(column_norms)):
        _fail("DESIGN", "train", "all actual-NV columns must have positive finite norm")
    scaled = design / column_norms
    singular_values = np.linalg.svd(scaled, compute_uv=False)
    tolerance = max(scaled.shape) * np.finfo(float).eps * singular_values[0]
    rank = int(np.count_nonzero(singular_values > tolerance))
    condition = float(singular_values[0] / singular_values[-1]) if singular_values[-1] > 0.0 else float("inf")
    if rank != 15 or not np.isfinite(condition) or condition > _MAX_SCALED_DESIGN_CONDITION:
        _fail(
            "DESIGN",
            "train",
            "actual-NV design must have rank 15 and condition at or below the fixed numerical guardrail",
        )
    return {
        "formula_family_order": sorted(by_family),
        "rank": rank,
        "required_rank": 15,
        "condition_number": condition,
        "maximum_condition_number": _MAX_SCALED_DESIGN_CONDITION,
        "singular_values": singular_values.tolist(),
        "column_norms": column_norms.tolist(),
        "rank_tolerance": float(tolerance),
    }


def _projection_payload(cells: Sequence[_Cell], wavelengths: np.ndarray, split: str) -> dict[str, Any]:
    return {
        "schema_version": "moocow-open-selection-projection-v1",
        "split": split,
        "wavelength_nm": wavelengths.tolist(),
        "cells": [
            {
                "card_id": cell.card_id,
                "formula_family_id": cell.formula_family_id,
                "formula_id": cell.formula_id,
                "formula_batch_id": cell.formula_batch_id,
                "backing": cell.backing,
                "dft_band": cell.dft_band,
                "dft_mm": cell.thickness_mm,
                "actual_nv": cell.concentrations.tolist(),
                "mean_reflectance": cell.observed.tolist(),
                "reposition_ids": list(cell.reposition_ids),
                "reposition_reflectance": cell.raw_replicates.tolist(),
            }
            for cell in cells
        ],
    }


def _build_fit_data(dataset: ValidatedOpenSelectionDataset) -> _FitData:
    manifest = copy.deepcopy(dict(dataset.manifest))
    source = copy.deepcopy(dict(dataset.source))
    if manifest.get("dataset_status") != "open_selection_only" or source.get("dataset_status") != "open_selection_only":
        _fail("DATASET", "dataset_status", "must be exactly open_selection_only")
    if manifest.get("production_pass") is not False:
        _fail("PERMISSIONS", "manifest.production_pass", "must remain false")
    _assert_permissions(manifest, "manifest")
    if manifest.get("saunderson") != {"mode": "off"}:
        _fail("SAUNDERSON", "manifest.saunderson", "must be exactly {'mode': 'off'}")
    if set(_mapping(manifest.get("splits"), "manifest.splits")) != set(_SPLITS):
        _fail("SPLITS", "manifest.splits", "must contain exactly train and validation")
    component_pairs = _validate_component_pairs(manifest)
    wavelengths = _validate_wavelength_grid(manifest.get("wavelength_nm"))
    backings = _mapping(manifest.get("backings"), "manifest.backings")
    if set(backings) != set(_BACKINGS):
        _fail("BACKINGS", "manifest.backings", "must contain exactly black and white")
    backing_means = {
        backing: _array(
            _mapping(backings[backing], f"manifest.backings.{backing}").get("mean_reflectance"),
            f"manifest.backings.{backing}.mean_reflectance",
            length=len(wavelengths),
            unit_interval=True,
        )
        for backing in _BACKINGS
    }
    if np.allclose(backing_means["black"], backing_means["white"], rtol=0.0, atol=1e-12):
        _fail("BACKINGS", "manifest.backings", "black and white admitted backing means must differ")

    cards = _list(source.get("cards"), "source.cards")
    if len(cards) != 36:
        _fail("CARDS", "source.cards", "must contain exactly 36 cards")
    card_by_id: dict[str, Mapping[str, Any]] = {}
    for index, card_value in enumerate(cards):
        card = _mapping(card_value, f"source.cards[{index}]")
        card_id = _text(card.get("card_id"), f"source.cards[{index}].card_id")
        split = _text(card.get("split"), f"source.cards[{index}].split")
        if split not in _SPLITS or card_id in card_by_id:
            _fail("CARDS", f"source.cards[{index}]", "must contain a unique train or validation card")
        card_by_id[card_id] = card
    split_cards = {split: [card for card in card_by_id.values() if card["split"] == split] for split in _SPLITS}
    if len(split_cards["train"]) != 30 or len(split_cards["validation"]) != 6:
        _fail("CARDS", "source.cards", "must contain exactly 30 train and 6 validation cards")
    manifest_splits = _mapping(manifest.get("splits"), "manifest.splits")
    if len(_list(manifest_splits.get("train"), "manifest.splits.train")) != 30 or len(
        _list(manifest_splits.get("validation"), "manifest.splits.validation")
    ) != 6:
        _fail("SPLITS", "manifest.splits", "must retain exactly 30 train and 6 validation cards")

    measurements = _list(source.get("measurements"), "source.measurements")
    if len(measurements) != 216:
        _fail("MEASUREMENTS", "source.measurements", "must contain exactly 216 coated readings")
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for index, measurement_value in enumerate(measurements):
        measurement = _mapping(measurement_value, f"source.measurements[{index}]")
        card_id = _text(measurement.get("card_id"), f"source.measurements[{index}].card_id")
        backing = _text(measurement.get("backing"), f"source.measurements[{index}].backing")
        if card_id not in card_by_id or backing not in _BACKINGS:
            _fail("MEASUREMENTS", f"source.measurements[{index}]", "must bind a declared card and backing")
        if measurement.get("split") != card_by_id[card_id]["split"] or measurement.get("target_kind") != "measured_spectrum":
            _fail("MEASUREMENTS", f"source.measurements[{index}]", "does not retain admitted split/target kind")
        grouped.setdefault((card_id, backing), []).append(measurement)
    expected_cells = {(card_id, backing) for card_id in card_by_id for backing in _BACKINGS}
    if set(grouped) != expected_cells:
        _fail("MEASUREMENTS", "source.measurements", "must contain one complete black/white cell roster")

    component_index = {pair: index for index, pair in enumerate(component_pairs)}
    cells_by_split: dict[str, list[_Cell]] = {split: [] for split in _SPLITS}
    for card_id, backing in sorted(expected_cells):
        records = grouped[(card_id, backing)]
        if len(records) != 3:
            _fail("REPLICATES", f"{card_id}.{backing}", "must contain exactly three reposition spectra")
        paired_records = sorted(
            (
                _text(record.get("reposition_id"), f"{card_id}.{backing}.reposition_id"),
                record,
            )
            for record in records
        )
        reposition_ids = tuple(reposition_id for reposition_id, _record in paired_records)
        if reposition_ids != _REPOSITIONS:
            _fail(
                "REPLICATES",
                f"{card_id}.{backing}",
                "must contain exactly POS01, POS02, and POS03",
            )
        records = [record for _reposition_id, record in paired_records]
        first = records[0]
        card = card_by_id[card_id]
        for field in ("formula_family_id", "formula_id", "formula_batch_id", "split", "dft_band"):
            if first.get(field) != card.get(field):
                _fail("MEASUREMENTS", f"{card_id}.{backing}.{field}", "must match its admitted card record")
        if first.get("backing") != backing:
            _fail("MEASUREMENTS", f"{card_id}.{backing}.backing", "must match its grouped backing")
        raw = np.vstack(
            [
                _array(record.get("reflectance"), f"{card_id}.{backing}.reflectance", length=len(wavelengths), unit_interval=True)
                for record in records
            ]
        )
        concentrations = np.zeros(len(component_pairs), dtype=float)
        components = _list(first.get("components"), f"{card_id}.{backing}.components")
        if len(components) != len(component_pairs):
            _fail("COMPONENTS", f"{card_id}.{backing}.components", "must retain all 15 components")
        for component_position, component_value in enumerate(components):
            component = _mapping(component_value, f"{card_id}.{backing}.components[{component_position}]")
            pair = (
                _text(component.get("component_id"), f"{card_id}.{backing}.components[{component_position}].component_id"),
                _text(component.get("physical_lot_id"), f"{card_id}.{backing}.components[{component_position}].physical_lot_id"),
            )
            if pair != component_pairs[component_position]:
                _fail("COMPONENTS", f"{card_id}.{backing}.components", "must retain fixed component/lot order")
            concentrations[component_index[pair]] = _finite_number(
                component.get("nonvolatile_volume_fraction"),
                f"{card_id}.{backing}.components[{component_position}].nonvolatile_volume_fraction",
            )
        if np.any(concentrations < 0.0) or not np.isclose(float(np.sum(concentrations)), 1.0, rtol=0.0, atol=1e-12):
            _fail("COMPONENTS", f"{card_id}.{backing}.components", "actual-NV fractions must be non-negative and sum to one")
        for record in records[1:]:
            if _list(record.get("components"), f"{card_id}.{backing}.components") != components:
                _fail("COMPONENTS", f"{card_id}.{backing}", "replicates must retain the same component projection")
            if _finite_number(record.get("dft_um"), f"{card_id}.{backing}.dft_um", positive=True) != _finite_number(
                first.get("dft_um"), f"{card_id}.{backing}.dft_um", positive=True
            ):
                _fail("DFT", f"{card_id}.{backing}", "replicates must retain one measured DFT")
        thickness_mm = _finite_number(first.get("dft_um"), f"{card_id}.{backing}.dft_um", positive=True) / 1000.0
        cells_by_split[card["split"]].append(
            _Cell(
                card_id=card_id,
                formula_family_id=_text(first.get("formula_family_id"), f"{card_id}.{backing}.formula_family_id"),
                formula_id=_text(first.get("formula_id"), f"{card_id}.{backing}.formula_id"),
                formula_batch_id=_text(first.get("formula_batch_id"), f"{card_id}.{backing}.formula_batch_id"),
                split=card["split"],
                dft_band=_text(first.get("dft_band"), f"{card_id}.{backing}.dft_band"),
                backing=backing,
                thickness_mm=thickness_mm,
                concentrations=concentrations,
                observed=np.mean(raw, axis=0),
                raw_replicates=raw,
                reposition_ids=reposition_ids,
            )
        )

    train_cells = tuple(sorted(cells_by_split["train"], key=lambda cell: (cell.formula_family_id, cell.formula_batch_id, cell.card_id, cell.backing)))
    validation_cells = tuple(sorted(cells_by_split["validation"], key=lambda cell: (cell.formula_family_id, cell.formula_batch_id, cell.card_id, cell.backing)))
    if len(train_cells) != 60 or len(validation_cells) != 12:
        _fail("CELLS", "source.measurements", "must group to exactly 60 train and 12 validation cells")

    train_by_family: dict[str, dict[str, dict[str, _Cell]]] = {}
    for cell in train_cells:
        train_by_family.setdefault(cell.formula_family_id, {}).setdefault(cell.dft_band, {})[cell.backing] = cell
    if len(train_by_family) != 15:
        _fail("DFT", "train", "must retain exactly 15 training formula families")
    for family, by_band in train_by_family.items():
        if set(by_band) != {"DFT-L", "DFT-H"} or any(set(by_backing) != set(_BACKINGS) for by_backing in by_band.values()):
            _fail("DFT", family, "must retain paired DFT-L/DFT-H black/white cells")
        for backing in _BACKINGS:
            if by_band["DFT-L"][backing].thickness_mm >= by_band["DFT-H"][backing].thickness_mm:
                _fail("DFT", f"{family}.{backing}", "must retain positive ordered DFT-L/DFT-H measurements")

    design_diagnostics = _design_diagnostics(train_cells)
    t_ref_mm = float(np.exp(np.mean(np.log([cell.thickness_mm for cell in train_cells]))))
    if not np.isfinite(t_ref_mm) or t_ref_mm <= 0.0:
        _fail("DFT", "train", "cannot form a positive finite geometric-mean reference thickness")
    return _FitData(
        component_pairs=component_pairs,
        wavelengths=wavelengths,
        backing_means=backing_means,
        train_cells=train_cells,
        validation_cells=validation_cells,
        t_ref_mm=t_ref_mm,
        design_diagnostics=design_diagnostics,
        train_projection=_projection_payload(train_cells, wavelengths, "train"),
        validation_projection=_projection_payload(validation_cells, wavelengths, "validation"),
    )


def _fit_spec(data: _FitData) -> dict[str, Any]:
    return {
        "schema_version": FIT_SPEC_SCHEMA,
        "algorithm": _FIT_ALGORITHM,
        "concentration_basis": "nonvolatile_volume_fraction",
        "saunderson": {"mode": "off"},
        "fit_splits": ["train"],
        "selection_splits": ["train", "validation"],
        "wavelength_nm": data.wavelengths.tolist(),
        "wavelength_policy": {"minimum_nm": 400.0, "maximum_nm": 700.0, "maximum_step_nm": 20.0, "uniform": True},
        "grouping": {"train_cells": 60, "validation_cells": 12, "repositions_per_cell": 3, "cell_value": "arithmetic_mean"},
        "t_ref_mm": data.t_ref_mm,
        "parameterization": {
            "eta": "log(S_mm_inv*t_ref_mm)",
            "rho": "log1p(K_mm_inv/S_mm_inv)",
            "S_mm_inv": "exp(eta)/t_ref_mm",
            "K_mm_inv": "S_mm_inv*expm1(rho)",
        },
        "bounds": {"eta": [_ETA_LOWER, _ETA_UPPER], "rho": [_RHO_LOWER, _RHO_UPPER]},
        "regularization_grid": list(_REGULARIZATION_GRID),
        "regularization": {"operator": "second_difference", "data_weighting": "equal_cell_wavelength", "cross_component_shrinkage": False},
        "starts": {
            "count": 4,
            "fixed": {"eta": 0.0, "rho": math.log(2.0)},
            "seeded": {"count": 3, "eta_uniform": [-4.0, 4.0], "rho_uniform": [0.0, math.log1p(100.0)]},
            "seed_binding": "sha256(fit_spec_schema,train_projection_payload_sha256,train,regularization,start_index)",
        },
        "optimizer": {"method": "trf", "tr_solver": "lsmr", "x_scale": "jac", "jacobian": "analytic_sparse", "ftol": _OPTIMIZER_TOLERANCE, "xtol": _OPTIMIZER_TOLERANCE, "gtol": _OPTIMIZER_TOLERANCE, "max_nfev": _MAX_NFEV},
        "prediction_roundoff_tolerance": _PREDICTION_ROUNDOFF,
        "reconstruction_tolerance": {"atol": _SEMANTIC_ATOL, "rtol": _SEMANTIC_RTOL},
    }


def _seed(train_projection_sha256: str, regularization: float, start_index: int) -> int:
    payload = {
        "fit_spec_schema": FIT_SPEC_SCHEMA,
        "train_projection_payload_sha256": train_projection_sha256,
        "split": "train",
        "regularization": regularization,
        "start_index": start_index,
    }
    return int(_artifact_payload_sha256(payload)[:16], 16)


def _bounds(data: _FitData) -> tuple[np.ndarray, np.ndarray]:
    count = len(data.component_pairs) * len(data.wavelengths)
    return (
        np.concatenate((np.full(count, _ETA_LOWER), np.full(count, _RHO_LOWER))),
        np.concatenate((np.full(count, _ETA_UPPER), np.full(count, _RHO_UPPER))),
    )


def _start(data: _FitData, train_projection_sha256: str, regularization: float, start_index: int) -> tuple[np.ndarray, int | None]:
    count = len(data.component_pairs) * len(data.wavelengths)
    if start_index == 0:
        return np.concatenate((np.zeros(count), np.full(count, math.log(2.0)))), None
    seed = _seed(train_projection_sha256, regularization, start_index)
    generator = np.random.default_rng(seed)
    return np.concatenate(
        (
            generator.uniform(-4.0, 4.0, size=count),
            generator.uniform(0.0, math.log1p(100.0), size=count),
        )
    ), seed


def _bound_counts(parameters: np.ndarray, data: _FitData) -> dict[str, int]:
    count = len(data.component_pairs) * len(data.wavelengths)
    eta = parameters[:count]
    rho = parameters[count:]
    return {
        "eta_lower": int(np.count_nonzero(np.isclose(eta, _ETA_LOWER, rtol=0.0, atol=_BOUND_TOLERANCE))),
        "eta_upper": int(np.count_nonzero(np.isclose(eta, _ETA_UPPER, rtol=0.0, atol=_BOUND_TOLERANCE))),
        "rho_lower": int(np.count_nonzero(np.isclose(rho, _RHO_LOWER, rtol=0.0, atol=_BOUND_TOLERANCE))),
        "rho_upper": int(np.count_nonzero(np.isclose(rho, _RHO_UPPER, rtol=0.0, atol=_BOUND_TOLERANCE))),
    }


def _jacobian_diagnostics(data_jacobian: csr_matrix, data: _FitData) -> list[dict[str, Any]]:
    component_count = len(data.component_pairs)
    wavelength_count = len(data.wavelengths)
    rows_per_wavelength = len(data.train_cells)
    result: list[dict[str, Any]] = []
    for wavelength_index, wavelength in enumerate(data.wavelengths):
        row_indexes = np.arange(wavelength_index, rows_per_wavelength * wavelength_count, wavelength_count)
        columns = [
            component_index * wavelength_count + wavelength_index
            for component_index in range(component_count)
        ] + [
            component_count * wavelength_count + component_index * wavelength_count + wavelength_index
            for component_index in range(component_count)
        ]
        matrix = data_jacobian[row_indexes, :][:, columns].toarray()
        singular_values = np.linalg.svd(matrix, compute_uv=False)
        tolerance = max(matrix.shape) * np.finfo(float).eps * singular_values[0]
        rank = int(np.count_nonzero(singular_values > tolerance))
        condition = float(singular_values[0] / singular_values[-1]) if singular_values[-1] > 0.0 else float("inf")
        if (
            rank != 2 * component_count
            or not np.isfinite(condition)
            or condition > _MAX_SCALED_DESIGN_CONDITION
        ):
            _fail(
                "JACOBIAN",
                f"wavelength_nm[{wavelength_index}]",
                "data-only Jacobian must have full rank and condition at or below the fixed numerical guardrail",
            )
        result.append(
            {
                "wavelength_nm": float(wavelength),
                "rank": rank,
                "required_rank": 2 * component_count,
                "condition_number": condition,
                "maximum_condition_number": _MAX_SCALED_DESIGN_CONDITION,
                "singular_values": singular_values.tolist(),
                "rank_tolerance": float(tolerance),
            }
        )
    return result


def _invalid_candidate(
    regularization: float,
    starts: Sequence[Mapping[str, Any]],
    reason: str,
    *,
    objective: float | None = None,
    selected_start_index: int | None = None,
) -> dict[str, Any]:
    return {
        "regularization": regularization,
        "valid": False,
        "invalid_reason": reason,
        "objective": objective,
        "selected_start_index": selected_start_index,
        "starts": copy.deepcopy(list(starts)),
        "converged": False,
    }


def _fit_candidate(data: _FitData, train_projection_sha256: str, regularization: float) -> dict[str, Any]:
    lower, upper = _bounds(data)
    starts: list[dict[str, Any]] = []
    valid_results: list[tuple[float, int, Any, np.ndarray, csr_matrix]] = []
    for start_index in range(4):
        initial, seed = _start(data, train_projection_sha256, regularization, start_index)
        entry: dict[str, Any] = {"start_index": start_index, "kind": "fixed" if seed is None else "sha256_seeded", "seed": seed}
        last_parameters: np.ndarray | None = None
        last_evaluation: tuple[np.ndarray, csr_matrix, np.ndarray, csr_matrix] | None = None

        def evaluate(parameters: np.ndarray) -> tuple[np.ndarray, csr_matrix, np.ndarray, csr_matrix]:
            nonlocal last_parameters, last_evaluation
            if last_parameters is not None and np.array_equal(parameters, last_parameters):
                assert last_evaluation is not None
                return last_evaluation
            last_parameters = parameters.copy()
            last_evaluation = _residual_and_jacobian(data, parameters, regularization)
            return last_evaluation

        try:
            result = least_squares(
                lambda parameters: evaluate(parameters)[0],
                initial,
                jac=lambda parameters: evaluate(parameters)[1],
                bounds=(lower, upper),
                method="trf",
                tr_solver="lsmr",
                x_scale="jac",
                ftol=_OPTIMIZER_TOLERANCE,
                xtol=_OPTIMIZER_TOLERANCE,
                gtol=_OPTIMIZER_TOLERANCE,
                max_nfev=_MAX_NFEV,
            )
            objective = float(np.sum(result.fun**2))
            entry.update(
                {
                    "converged": bool(result.success),
                    "objective": objective,
                    "nfev": int(result.nfev),
                    "njev": int(result.njev) if result.njev is not None else None,
                    "optimality": float(result.optimality),
                    "status": int(result.status),
                }
            )
            if result.success and np.isfinite(objective) and np.all(np.isfinite(result.x)):
                _residual, _jacobian, prediction, data_jacobian = _residual_and_jacobian(data, result.x, regularization)
                bounds_used = _bound_counts(result.x, data)
                if bounds_used["eta_upper"] or bounds_used["rho_upper"]:
                    entry["numerically_valid"] = False
                    entry["invalid_reason"] = "forbidden_upper_guardrail"
                elif not np.all(np.isfinite(prediction)):
                    entry["numerically_valid"] = False
                    entry["invalid_reason"] = "non_finite_prediction"
                else:
                    entry["numerically_valid"] = True
                    valid_results.append((objective, start_index, result, prediction, data_jacobian))
            else:
                entry["numerically_valid"] = False
        except (CalibrationError, FloatingPointError, ValueError, OverflowError):
            entry.update({"converged": False, "numerically_valid": False, "objective": None, "nfev": None, "njev": None, "optimality": None, "status": None})
        starts.append(entry)
    if not valid_results:
        return _invalid_candidate(regularization, starts, "no_converged_numerically_valid_start")
    objective, start_index, result, train_prediction, data_jacobian = min(valid_results, key=lambda item: (item[0], item[1]))
    try:
        validation_prediction, _validation_jacobian = _prediction_and_data_jacobian(data, result.x, data.validation_cells)
        jacobian_diagnostics = _jacobian_diagnostics(data_jacobian, data)
    except OpenSelectionFitExportError as error:
        return _invalid_candidate(
            regularization,
            starts,
            error.code.casefold(),
            objective=objective,
            selected_start_index=start_index,
        )
    bounds_used = _bound_counts(result.x, data)
    component_count = len(data.component_pairs)
    wavelength_count = len(data.wavelengths)
    eta = result.x[: component_count * wavelength_count].reshape(component_count, wavelength_count)
    rho = result.x[component_count * wavelength_count :].reshape(component_count, wavelength_count)
    scattering = np.exp(eta) / data.t_ref_mm
    absorption = scattering * np.expm1(rho)
    return {
        "regularization": regularization,
        "valid": True,
        "objective": objective,
        "selected_start_index": start_index,
        "starts": starts,
        "converged": True,
        "optimizer_status": int(result.status),
        "optimizer_nfev": int(result.nfev),
        "optimizer_njev": int(result.njev) if result.njev is not None else None,
        "optimizer_optimality": float(result.optimality),
        "bound_counts": bounds_used,
        "jacobian_by_wavelength": jacobian_diagnostics,
        "eta": eta,
        "rho": rho,
        "S_mm_inv": scattering,
        "K_mm_inv": absorption,
        "train_prediction": train_prediction,
        "validation_prediction": validation_prediction,
    }


def _metric_values(errors: np.ndarray) -> dict[str, float]:
    absolute = np.abs(np.asarray(errors, dtype=float)).reshape(-1)
    if absolute.size == 0 or not np.all(np.isfinite(absolute)):
        _fail("METRICS", "errors", "must be non-empty and finite")
    p95_index = max(0, math.ceil(0.95 * len(absolute)) - 1)
    return {
        "rmse": float(np.sqrt(np.mean(np.square(absolute)))),
        "mae": float(np.mean(absolute)),
        "p95_abs": float(np.partition(absolute, p95_index)[p95_index]),
        "max_abs": float(np.max(absolute)),
    }


def _stratified_metrics(cells: Sequence[_Cell], errors: np.ndarray, attribute: str) -> dict[str, dict[str, float]]:
    values: dict[str, dict[str, float]] = {}
    for label in sorted({str(getattr(cell, attribute)) for cell in cells}):
        indexes = [index for index, cell in enumerate(cells) if str(getattr(cell, attribute)) == label]
        values[label] = _metric_values(errors[indexes, :])
    return values


def _contrast_metrics(cells: Sequence[_Cell], prediction: np.ndarray) -> dict[str, float]:
    by_card: dict[str, dict[str, tuple[_Cell, np.ndarray]]] = {}
    for cell, predicted in zip(cells, prediction):
        by_card.setdefault(cell.card_id, {})[cell.backing] = (cell, predicted)
    errors: list[np.ndarray] = []
    for card_id, pair in sorted(by_card.items()):
        if set(pair) != set(_BACKINGS):
            _fail("METRICS", card_id, "must retain matched black/white cells")
        black_cell, black_prediction = pair["black"]
        white_cell, white_prediction = pair["white"]
        errors.append((black_prediction - white_prediction) - (black_cell.observed - white_cell.observed))
    return _metric_values(np.vstack(errors))


def _repeatability_metrics(cells: Sequence[_Cell]) -> dict[str, float | int]:
    standard_deviations = np.concatenate([np.std(cell.raw_replicates, axis=0, ddof=1) for cell in cells])
    p95_index = max(0, math.ceil(0.95 * len(standard_deviations)) - 1)
    return {
        "median_spectral_std": float(np.median(standard_deviations)),
        "p95_spectral_std": float(np.partition(standard_deviations, p95_index)[p95_index]),
        "values": int(len(standard_deviations)),
    }


def _split_diagnostics(cells: Sequence[_Cell], prediction: np.ndarray) -> dict[str, Any]:
    observed = np.vstack([cell.observed for cell in cells])
    errors = prediction - observed
    return {
        "cell_count": len(cells),
        "global": _metric_values(errors),
        "by_backing": _stratified_metrics(cells, errors, "backing"),
        "by_dft_band": _stratified_metrics(cells, errors, "dft_band"),
        "by_formula_family": _stratified_metrics(cells, errors, "formula_family_id"),
        "black_white_contrast_error": _contrast_metrics(cells, prediction),
        "within_cell_repeatability": _repeatability_metrics(cells),
    }


def _metric_gaps(train: Mapping[str, Any], validation: Mapping[str, Any]) -> dict[str, float]:
    return {
        key: float(validation["global"][key] - train["global"][key])
        for key in ("rmse", "mae", "p95_abs", "max_abs")
    }


def _projection_summary(cells: Sequence[_Cell], payload_sha256: str) -> dict[str, Any]:
    return {
        "cell_count": len(cells),
        "payload_sha256": payload_sha256,
        "members": [
            {
                "card_id": cell.card_id,
                "formula_family_id": cell.formula_family_id,
                "formula_id": cell.formula_id,
                "formula_batch_id": cell.formula_batch_id,
                "backing": cell.backing,
                "dft_band": cell.dft_band,
            }
            for cell in cells
        ],
    }


def _candidate_model_payload(
    data: _FitData,
    candidate: Mapping[str, Any],
    fit_spec: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    train_projection_sha256: str,
    validation_projection_sha256: str,
) -> dict[str, Any]:
    components = []
    for index, (component_id, lot_id) in enumerate(data.component_pairs):
        rho = np.asarray(candidate["rho"], dtype=float)[index]
        components.append(
            {
                "component_id": component_id,
                "physical_lot_id": lot_id,
                "eta": np.asarray(candidate["eta"], dtype=float)[index].tolist(),
                "rho": rho.tolist(),
                "rho_lower_bound_estimate": bool(np.any(np.isclose(rho, _RHO_LOWER, rtol=0.0, atol=_BOUND_TOLERANCE))),
                "S_mm_inv": np.asarray(candidate["S_mm_inv"], dtype=float)[index].tolist(),
                "K_mm_inv": np.asarray(candidate["K_mm_inv"], dtype=float)[index].tolist(),
            }
        )
    fit_spec_sha256 = _artifact_payload_sha256(fit_spec)
    return {
        "schema_version": FIT_MODEL_SCHEMA,
        "dataset_status": "open_selection_only",
        "status": "open_selection_fit_candidate",
        "production_pass": False,
        **_permissions(),
        "runtime_compatible": False,
        "concentration_basis": "nonvolatile_volume_fraction",
        "wavelength_nm": data.wavelengths.tolist(),
        "saunderson": {"mode": "off"},
        "component_order": [
            {"component_id": component_id, "physical_lot_id": lot_id}
            for component_id, lot_id in data.component_pairs
        ],
        "components": components,
        "fit_spec": dict(fit_spec),
        "fit_spec_payload_sha256": fit_spec_sha256,
        "projection_bindings": {
            "train": _projection_summary(data.train_cells, train_projection_sha256),
            "validation": _projection_summary(data.validation_cells, validation_projection_sha256),
        },
        "predecessor_bindings": dict(predecessor),
        "fit_diagnostics": {
            "grouping_counts": {
                "train_cell_count": 60,
                "train_raw_reading_count": 180,
                "validation_cell_count": 12,
                "validation_raw_reading_count": 36,
            },
            "regularization": float(candidate["regularization"]),
            "objective": float(candidate["objective"]),
            "selected_start_index": int(candidate["selected_start_index"]),
            "converged": bool(candidate["converged"]),
            "optimizer_status": int(candidate["optimizer_status"]),
            "optimizer_nfev": int(candidate["optimizer_nfev"]),
            "optimizer_njev": candidate["optimizer_njev"],
            "optimizer_optimality": float(candidate["optimizer_optimality"]),
            "starts": copy.deepcopy(list(candidate["starts"])),
            "bound_counts": dict(candidate["bound_counts"]),
            "design": copy.deepcopy(dict(data.design_diagnostics)),
            "jacobian_by_wavelength": copy.deepcopy(list(candidate["jacobian_by_wavelength"])),
        },
    }


def _fit_all_candidates(data: _FitData, train_projection_sha256: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for regularization in _REGULARIZATION_GRID:
        try:
            candidates.append(_fit_candidate(data, train_projection_sha256, regularization))
        except OpenSelectionFitExportError as error:
            candidates.append(_invalid_candidate(regularization, (), error.code.casefold()))
    return candidates


def _evaluation_payload(
    model_sha256: str,
    fit_spec: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    train_projection_sha256: str,
    validation_projection_sha256: str,
    selected_candidate: Mapping[str, Any],
    metrics: Mapping[str, Any],
    candidate_scores: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SELECTION_EVALUATION_SCHEMA,
        "dataset_status": "open_selection_only",
        "status": "open_selection_fit_candidate",
        "production_pass": False,
        **_permissions(),
        "runtime_compatible": False,
        "model": {"path": "fit-model.json", "sha256": model_sha256},
        "fit_spec_payload_sha256": _artifact_payload_sha256(fit_spec),
        "projection_bindings": {
            "train_payload_sha256": train_projection_sha256,
            "validation_payload_sha256": validation_projection_sha256,
        },
        "predecessor_bindings": dict(predecessor),
        "selection": {
            "criterion": ["validation_rmse", "validation_mae", "validation_max_abs", "regularization", "model_payload_sha256"],
            "selected_regularization": float(selected_candidate["regularization"]),
            "selected_start_index": int(selected_candidate["selected_start_index"]),
            "candidates": [copy.deepcopy(dict(item)) for item in candidate_scores],
            "train_validation_refit": False,
        },
        "metrics": {"train": copy.deepcopy(metrics["train"]), "validation": copy.deepcopy(metrics["validation"]), "validation_minus_train_global": _metric_gaps(metrics["train"], metrics["validation"])},
        "acceptance_thresholds_configured": False,
    }


def _receipt_payload_object(
    model_sha256: str,
    evaluation_sha256: str,
    fit_spec: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    train_projection_sha256: str,
    validation_projection_sha256: str,
) -> dict[str, Any]:
    receipt = {
        "schema_version": FIT_EXPORT_RECEIPT_SCHEMA,
        "dataset_status": "open_selection_only",
        "status": "open_selection_fit_exported",
        "state": "OPEN_SELECTION_FIT_EXPORTED",
        "production_pass": False,
        **_permissions(),
        "runtime_compatible": False,
        "bindings": {
            **dict(predecessor),
            "fit_spec_payload_sha256": _artifact_payload_sha256(fit_spec),
            "train_projection_payload_sha256": train_projection_sha256,
            "validation_projection_payload_sha256": validation_projection_sha256,
            "fit_model": {"path": "fit-model.json", "sha256": model_sha256},
            "selection_evaluation": {"path": "selection-evaluation.json", "sha256": evaluation_sha256},
        },
        "receipt_payload_sha256": "",
    }
    receipt["receipt_payload_sha256"] = _artifact_payload_sha256(_receipt_payload(receipt))
    return receipt


def _admission_context(
    *,
    acquisition_receipt_path: Path | str,
    admission_receipt_path: Path | str,
    dataset_root: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
) -> tuple[ValidatedOpenSelectionDataset, _FitData, dict[str, Any]]:
    verification = verify_open_measurement_admission(
        acquisition_receipt_path=acquisition_receipt_path,
        admission_receipt_path=admission_receipt_path,
        dataset_root=dataset_root,
        shared_root=shared_root,
        open_root=open_root,
        measurement_root=measurement_root,
    )
    if verification.get("status") != "open_measurement_admission_verified" or verification.get("state") != "OPEN_SELECTION_DATASET_ADMITTED":
        _fail("ADMISSION", "admission_receipt_path", "did not pass open-measurement admission verification")
    _assert_permissions(_mapping(verification, "admission_verification"), "admission_verification")
    if verification.get("cards") != 36 or verification.get("readings") != 216:
        _fail("ADMISSION", "admission_receipt_path", "did not reverify the fixed 36-card/216-reading roster")
    dataset = load_and_validate_open_selection_dataset(dataset_root)
    if (
        verification.get("dataset_manifest_sha256") != dataset.manifest_sha256
        or verification.get("open_measurements_sha256") != dataset.open_measurements_sha256
        or verification.get("bare_backing_measurements") != dataset.manifest.get("counts", {}).get("bare_backing_measurements")
    ):
        _fail("ADMISSION", "dataset_root", "reloaded dataset digests differ from admission verification")
    try:
        admission_receipt, admission_sha256 = read_verified_json(admission_receipt_path, require_sidecar=True)
    except CalibrationError as error:
        _fail("ADMISSION", "admission_receipt_path", str(error))
        raise AssertionError("unreachable") from error
    if verification.get("admission_receipt_sha256") != admission_sha256:
        _fail("ADMISSION", "admission_receipt_path", "digest differs from admission verification")
    receipt = _mapping(admission_receipt, "admission_receipt")
    bindings = _mapping(receipt.get("bindings"), "admission_receipt.bindings")
    predecessor = _mapping(dataset.manifest.get("predecessor"), "manifest.predecessor")
    open_source_binding = _mapping(predecessor.get("open_source_binding"), "manifest.predecessor.open_source_binding")
    acquisition_sha = _sha256(
        predecessor.get("acquisition_preflight_receipt_sha256"),
        "manifest.predecessor.acquisition_preflight_receipt_sha256",
    )
    if bindings.get("acquisition_preflight_receipt_sha256") != acquisition_sha:
        _fail("ADMISSION", "admission_receipt.bindings", "does not retain the revalidated acquisition binding")
    data = _build_fit_data(dataset)
    return dataset, data, {
        "acquisition_preflight_receipt_sha256": acquisition_sha,
        "admission_receipt_sha256": admission_sha256,
        "dataset_manifest_sha256": dataset.manifest_sha256,
        "open_measurements_sha256": dataset.open_measurements_sha256,
        "open_source_binding": copy.deepcopy(dict(open_source_binding)),
    }


def _fit_export_objects(
    dataset: ValidatedOpenSelectionDataset, data: _FitData, predecessor: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    fit_spec = _fit_spec(data)
    train_projection_sha256 = _artifact_payload_sha256(data.train_projection)
    validation_projection_sha256 = _artifact_payload_sha256(data.validation_projection)
    candidates = _fit_all_candidates(data, train_projection_sha256)
    scored: list[tuple[tuple[float, float, float, float, str], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    scores: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.get("valid") is not True:
            scores.append(
                {
                    "regularization": float(candidate["regularization"]),
                    "valid": False,
                    "invalid_reason": _text(candidate.get("invalid_reason"), "candidate.invalid_reason"),
                    "objective": candidate.get("objective"),
                    "selected_start_index": candidate.get("selected_start_index"),
                    "converged": False,
                    "starts": copy.deepcopy(list(candidate.get("starts", []))),
                }
            )
            continue
        metrics = {
            "train": _split_diagnostics(data.train_cells, np.asarray(candidate["train_prediction"], dtype=float)),
            "validation": _split_diagnostics(data.validation_cells, np.asarray(candidate["validation_prediction"], dtype=float)),
        }
        model = _candidate_model_payload(data, candidate, fit_spec, predecessor, train_projection_sha256, validation_projection_sha256)
        model_payload_sha256 = _artifact_payload_sha256(model)
        score = (
            metrics["validation"]["global"]["rmse"],
            metrics["validation"]["global"]["mae"],
            metrics["validation"]["global"]["max_abs"],
            float(candidate["regularization"]),
            model_payload_sha256,
        )
        score_record = {
            "regularization": float(candidate["regularization"]),
            "valid": True,
            "model_payload_sha256": model_payload_sha256,
            "validation_global": copy.deepcopy(metrics["validation"]["global"]),
            "objective": float(candidate["objective"]),
            "selected_start_index": int(candidate["selected_start_index"]),
            "converged": True,
            "optimizer_status": int(candidate["optimizer_status"]),
            "optimizer_nfev": int(candidate["optimizer_nfev"]),
            "optimizer_njev": candidate["optimizer_njev"],
            "optimizer_optimality": float(candidate["optimizer_optimality"]),
            "starts": copy.deepcopy(list(candidate["starts"])),
            "bound_counts": dict(candidate["bound_counts"]),
        }
        scores.append(score_record)
        scored.append((score, candidate, model, metrics, score_record))
    if not scored:
        _fail("FIT", "regularization_grid", "no numerically valid fixed-grid candidate is available for validation selection")
    _score, selected_candidate, model, metrics, _score_record = min(scored, key=lambda item: item[0])
    model_sha256 = sha256_bytes(canonical_json_bytes(model) + b"\n")
    evaluation = _evaluation_payload(
        model_sha256,
        fit_spec,
        predecessor,
        train_projection_sha256,
        validation_projection_sha256,
        selected_candidate,
        metrics,
        scores,
    )
    evaluation_sha256 = sha256_bytes(canonical_json_bytes(evaluation) + b"\n")
    receipt = _receipt_payload_object(
        model_sha256,
        evaluation_sha256,
        fit_spec,
        predecessor,
        train_projection_sha256,
        validation_projection_sha256,
    )
    return model, evaluation, receipt


def run_open_selection_fit_export(
    *,
    acquisition_receipt_path: Path | str,
    admission_receipt_path: Path | str,
    dataset_root: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
    output_dir: Path | str,
) -> dict[str, object]:
    """Fit train-only open cells, select with validation, and atomically export one package."""

    dataset, data, predecessor = _admission_context(
        acquisition_receipt_path=acquisition_receipt_path,
        admission_receipt_path=admission_receipt_path,
        dataset_root=dataset_root,
        shared_root=shared_root,
        open_root=open_root,
        measurement_root=measurement_root,
    )
    model, evaluation, receipt = _fit_export_objects(dataset, data, predecessor)
    _reject_forbidden_scope(model, "fit-model.json")
    _reject_forbidden_scope(evaluation, "selection-evaluation.json")
    _reject_forbidden_scope(receipt, "fit-export-receipt.json")
    output, staging, existed_empty = _prepare_output(output_dir)
    try:
        model_sha256 = write_json_with_sha256(staging / "fit-model.json", model)
        evaluation_sha256 = write_json_with_sha256(staging / "selection-evaluation.json", evaluation)
        if receipt["bindings"]["fit_model"]["sha256"] != model_sha256 or receipt["bindings"]["selection_evaluation"]["sha256"] != evaluation_sha256:
            _fail("OUTPUT", "staging", "artifact hashes changed during package construction")
        receipt_sha256 = write_json_with_sha256(staging / "fit-export-receipt.json", receipt)
        staged_model, staged_model_sha256 = read_verified_json(
            staging / "fit-model.json", require_sidecar=True, trusted_root=staging
        )
        staged_evaluation, staged_evaluation_sha256 = read_verified_json(
            staging / "selection-evaluation.json", require_sidecar=True, trusted_root=staging
        )
        staged_receipt, staged_receipt_sha256 = read_verified_json(
            staging / "fit-export-receipt.json", require_sidecar=True, trusted_root=staging
        )
        if (
            staged_model != model
            or staged_evaluation != evaluation
            or staged_receipt != receipt
            or staged_model_sha256 != model_sha256
            or staged_evaluation_sha256 != evaluation_sha256
            or staged_receipt_sha256 != receipt_sha256
        ):
            _fail("OUTPUT", "staging", "JSON or SHA-256 sidecar readback differs from constructed artifact")
        _reject_forbidden_scope(staged_model, "staging.fit-model.json")
        _reject_forbidden_scope(staged_evaluation, "staging.selection-evaluation.json")
        _reject_forbidden_scope(staged_receipt, "staging.fit-export-receipt.json")
        _validate_candidate_tree(staging)
        _publish(output, staging, existed_empty)
    except OSError as error:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        _fail("OUTPUT", "staging", f"cannot write or verify staged package: {error}")
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "status": "open_selection_fit_exported",
        "state": "OPEN_SELECTION_FIT_EXPORTED",
        "dataset_status": "open_selection_only",
        "production_pass": False,
        "runtime_compatible": False,
        "fit_model_sha256": model_sha256,
        "selection_evaluation_sha256": evaluation_sha256,
        "fit_export_receipt_sha256": receipt_sha256,
        **_permissions(),
    }


def _stored_component_curves(model: Mapping[str, Any], data: _FitData) -> tuple[np.ndarray, np.ndarray]:
    components = _list(model.get("components"), "fit-model.json.components")
    if len(components) != len(data.component_pairs):
        _fail("MODEL", "fit-model.json.components", "must retain the fixed component count")
    absorption: list[np.ndarray] = []
    scattering: list[np.ndarray] = []
    for index, (component_id, lot_id) in enumerate(data.component_pairs):
        component = _mapping(components[index], f"fit-model.json.components[{index}]")
        if component.get("component_id") != component_id or component.get("physical_lot_id") != lot_id:
            _fail("MODEL", f"fit-model.json.components[{index}]", "must retain fixed component/lot order")
        absorption.append(
            _array(
                component.get("K_mm_inv"),
                f"fit-model.json.components[{index}].K_mm_inv",
                length=len(data.wavelengths),
            )
        )
        scattering_curve = _array(
            component.get("S_mm_inv"),
            f"fit-model.json.components[{index}].S_mm_inv",
            length=len(data.wavelengths),
        )
        if np.any(absorption[-1] < 0.0) or np.any(scattering_curve <= 0.0):
            _fail("MODEL", f"fit-model.json.components[{index}]", "must retain non-negative K and positive S")
        scattering.append(scattering_curve)
    return np.vstack(absorption), np.vstack(scattering)


def _predict_from_stored_curves(
    data: _FitData, cells: Sequence[_Cell], absorption: np.ndarray, scattering: np.ndarray
) -> np.ndarray:
    concentrations = np.vstack([cell.concentrations for cell in cells])
    thicknesses = np.asarray([cell.thickness_mm for cell in cells], dtype=float)
    prediction = np.empty((len(cells), len(data.wavelengths)), dtype=float)
    for wavelength_index in range(len(data.wavelengths)):
        backing = np.asarray(
            [data.backing_means[cell.backing][wavelength_index] for cell in cells], dtype=float
        )
        prediction[:, wavelength_index] = _strict_reflectance_and_partials(
            concentrations @ scattering[:, wavelength_index],
            concentrations @ absorption[:, wavelength_index],
            thicknesses,
            backing,
        )[0]
    return prediction


def _verify_stored_metrics(model: Mapping[str, Any], evaluation: Mapping[str, Any], data: _FitData) -> None:
    absorption, scattering = _stored_component_curves(model, data)
    train_metrics = _split_diagnostics(
        data.train_cells, _predict_from_stored_curves(data, data.train_cells, absorption, scattering)
    )
    validation_metrics = _split_diagnostics(
        data.validation_cells,
        _predict_from_stored_curves(data, data.validation_cells, absorption, scattering),
    )
    metrics = _mapping(evaluation.get("metrics"), "selection-evaluation.json.metrics")
    _assert_close(metrics.get("train"), train_metrics, "selection-evaluation.json.metrics.train")
    _assert_close(metrics.get("validation"), validation_metrics, "selection-evaluation.json.metrics.validation")
    _assert_close(
        metrics.get("validation_minus_train_global"),
        _metric_gaps(train_metrics, validation_metrics),
        "selection-evaluation.json.metrics.validation_minus_train_global",
    )


def _read_candidate_objects(candidate_root: Path | str) -> tuple[Path, dict[str, Any], str, dict[str, Any], str, dict[str, Any], str]:
    root = _validate_candidate_root(candidate_root, "candidate_root")
    _validate_candidate_tree(root)
    try:
        model, model_sha256 = read_verified_json(root / "fit-model.json", require_sidecar=True, trusted_root=root)
        evaluation, evaluation_sha256 = read_verified_json(root / "selection-evaluation.json", require_sidecar=True, trusted_root=root)
        receipt, receipt_sha256 = read_verified_json(root / "fit-export-receipt.json", require_sidecar=True, trusted_root=root)
    except CalibrationError as error:
        _fail("PACKAGE", "candidate_root", str(error))
        raise AssertionError("unreachable") from error
    return root, dict(_mapping(model, "fit-model.json")), model_sha256, dict(_mapping(evaluation, "selection-evaluation.json")), evaluation_sha256, dict(_mapping(receipt, "fit-export-receipt.json")), receipt_sha256


def _verify_artifact_reference(
    value: object,
    path: str,
    *,
    expected_path: str,
    expected_sha256: str,
) -> None:
    reference = _mapping(value, path)
    _exact(reference, path, ("path", "sha256"))
    actual_path = _portable_candidate_path(reference.get("path"), f"{path}.path")
    actual_sha256 = _sha256(reference.get("sha256"), f"{path}.sha256")
    if actual_path != expected_path or actual_sha256 != expected_sha256:
        _fail("BINDING", path, "does not reference the current candidate artifact")


def _verify_current_artifact_bindings(
    model: Mapping[str, Any],
    model_sha256: str,
    evaluation: Mapping[str, Any],
    evaluation_sha256: str,
    receipt: Mapping[str, Any],
    receipt_sha256: str,
) -> None:
    artifacts = (
        ("fit-model.json", model, model_sha256),
        ("selection-evaluation.json", evaluation, evaluation_sha256),
        ("fit-export-receipt.json", receipt, receipt_sha256),
    )
    for name, artifact, digest in artifacts:
        if digest != sha256_bytes(canonical_json_bytes(artifact) + b"\n"):
            _fail("PACKAGE", name, "artifact bytes are not the canonical JSON payload")

    _verify_artifact_reference(
        evaluation.get("model"),
        "selection-evaluation.json.model",
        expected_path="fit-model.json",
        expected_sha256=model_sha256,
    )
    receipt_bindings = _mapping(receipt.get("bindings"), "fit-export-receipt.json.bindings")
    predecessor_fields = (
        "acquisition_preflight_receipt_sha256",
        "admission_receipt_sha256",
        "dataset_manifest_sha256",
        "open_measurements_sha256",
        "open_source_binding",
    )
    _exact(
        receipt_bindings,
        "fit-export-receipt.json.bindings",
        (
            *predecessor_fields,
            "fit_spec_payload_sha256",
            "train_projection_payload_sha256",
            "validation_projection_payload_sha256",
            "fit_model",
            "selection_evaluation",
        ),
    )
    _verify_artifact_reference(
        receipt_bindings.get("fit_model"),
        "fit-export-receipt.json.bindings.fit_model",
        expected_path="fit-model.json",
        expected_sha256=model_sha256,
    )
    _verify_artifact_reference(
        receipt_bindings.get("selection_evaluation"),
        "fit-export-receipt.json.bindings.selection_evaluation",
        expected_path="selection-evaluation.json",
        expected_sha256=evaluation_sha256,
    )

    fit_spec = _mapping(model.get("fit_spec"), "fit-model.json.fit_spec")
    fit_spec_sha256 = _artifact_payload_sha256(fit_spec)
    fit_spec_bindings = (
        (model.get("fit_spec_payload_sha256"), "fit-model.json.fit_spec_payload_sha256"),
        (evaluation.get("fit_spec_payload_sha256"), "selection-evaluation.json.fit_spec_payload_sha256"),
        (receipt_bindings.get("fit_spec_payload_sha256"), "fit-export-receipt.json.bindings.fit_spec_payload_sha256"),
    )
    for value, path in fit_spec_bindings:
        if _sha256(value, path) != fit_spec_sha256:
            _fail("BINDING", path, "does not bind the current fit specification")

    model_projections = _mapping(model.get("projection_bindings"), "fit-model.json.projection_bindings")
    _exact(model_projections, "fit-model.json.projection_bindings", _SPLITS)
    evaluation_projections = _mapping(
        evaluation.get("projection_bindings"), "selection-evaluation.json.projection_bindings"
    )
    _exact(
        evaluation_projections,
        "selection-evaluation.json.projection_bindings",
        ("train_payload_sha256", "validation_payload_sha256"),
    )
    for split in _SPLITS:
        model_projection = _mapping(
            model_projections.get(split), f"fit-model.json.projection_bindings.{split}"
        )
        model_projection_sha256 = _sha256(
            model_projection.get("payload_sha256"),
            f"fit-model.json.projection_bindings.{split}.payload_sha256",
        )
        evaluation_projection_sha256 = _sha256(
            evaluation_projections.get(f"{split}_payload_sha256"),
            f"selection-evaluation.json.projection_bindings.{split}_payload_sha256",
        )
        receipt_projection_sha256 = _sha256(
            receipt_bindings.get(f"{split}_projection_payload_sha256"),
            f"fit-export-receipt.json.bindings.{split}_projection_payload_sha256",
        )
        if len({model_projection_sha256, evaluation_projection_sha256, receipt_projection_sha256}) != 1:
            _fail("BINDING", f"projection_bindings.{split}", "candidate artifacts bind different projections")

    model_predecessor = _mapping(model.get("predecessor_bindings"), "fit-model.json.predecessor_bindings")
    evaluation_predecessor = _mapping(
        evaluation.get("predecessor_bindings"), "selection-evaluation.json.predecessor_bindings"
    )
    _exact(model_predecessor, "fit-model.json.predecessor_bindings", predecessor_fields)
    _exact(evaluation_predecessor, "selection-evaluation.json.predecessor_bindings", predecessor_fields)
    if model_predecessor != evaluation_predecessor:
        _fail("BINDING", "predecessor_bindings", "model and evaluation predecessor bindings differ")
    for field in predecessor_fields:
        if receipt_bindings.get(field) != model_predecessor.get(field):
            _fail("BINDING", f"fit-export-receipt.json.bindings.{field}", "does not retain the model predecessor binding")

    selection = _mapping(evaluation.get("selection"), "selection-evaluation.json.selection")
    selected_regularization = _finite_number(
        selection.get("selected_regularization"), "selection-evaluation.json.selection.selected_regularization"
    )
    selected_start_index = selection.get("selected_start_index")
    if isinstance(selected_start_index, bool) or not isinstance(selected_start_index, int):
        _fail("TYPE", "selection-evaluation.json.selection.selected_start_index", "must be an integer")
    selected_candidates = []
    for index, candidate_value in enumerate(_list(selection.get("candidates"), "selection-evaluation.json.selection.candidates")):
        candidate = _mapping(candidate_value, f"selection-evaluation.json.selection.candidates[{index}]")
        if (
            candidate.get("valid") is True
            and candidate.get("regularization") == selected_regularization
            and candidate.get("selected_start_index") == selected_start_index
        ):
            selected_candidates.append((index, candidate))
    if len(selected_candidates) != 1:
        _fail("BINDING", "selection-evaluation.json.selection", "must identify exactly one selected valid candidate")
    selected_index, selected_candidate = selected_candidates[0]
    if _sha256(
        selected_candidate.get("model_payload_sha256"),
        f"selection-evaluation.json.selection.candidates[{selected_index}].model_payload_sha256",
    ) != _artifact_payload_sha256(model):
        _fail("BINDING", "selection-evaluation.json.selection", "selected candidate does not bind the current model payload")

    expected_receipt_payload_sha256 = _artifact_payload_sha256(_receipt_payload(receipt))
    if _sha256(receipt.get("receipt_payload_sha256"), "fit-export-receipt.json.receipt_payload_sha256") != expected_receipt_payload_sha256:
        _fail("RECEIPT", "fit-export-receipt.json.receipt_payload_sha256", "does not self-bind receipt payload")


def _verify_current_authority_bindings(
    model: Mapping[str, Any],
    data: _FitData,
    predecessor: Mapping[str, Any],
) -> None:
    expected_fit_spec = _fit_spec(data)
    actual_fit_spec = _mapping(model.get("fit_spec"), "fit-model.json.fit_spec")
    if canonical_json_bytes(actual_fit_spec) != canonical_json_bytes(expected_fit_spec):
        _fail("BINDING", "fit-model.json.fit_spec", "does not match the current admitted fit authority")

    expected_projection_sha256 = {
        "train": _artifact_payload_sha256(data.train_projection),
        "validation": _artifact_payload_sha256(data.validation_projection),
    }
    model_projections = _mapping(model.get("projection_bindings"), "fit-model.json.projection_bindings")
    for split in _SPLITS:
        projection = _mapping(model_projections.get(split), f"fit-model.json.projection_bindings.{split}")
        actual_sha256 = _sha256(
            projection.get("payload_sha256"),
            f"fit-model.json.projection_bindings.{split}.payload_sha256",
        )
        if actual_sha256 != expected_projection_sha256[split]:
            _fail("BINDING", f"fit-model.json.projection_bindings.{split}", "does not match the current admitted projection")

    actual_predecessor = _mapping(model.get("predecessor_bindings"), "fit-model.json.predecessor_bindings")
    if canonical_json_bytes(actual_predecessor) != canonical_json_bytes(predecessor):
        _fail("BINDING", "fit-model.json.predecessor_bindings", "does not match the current reverified authorities")

    expected_component_order = [
        {"component_id": component_id, "physical_lot_id": lot_id}
        for component_id, lot_id in data.component_pairs
    ]
    if model.get("component_order") != expected_component_order:
        _fail("BINDING", "fit-model.json.component_order", "does not match the current admitted component/lot order")


def _assert_close(actual: object, expected: object, path: str) -> None:
    if isinstance(expected, Mapping):
        actual_mapping = _mapping(actual, path)
        if set(actual_mapping) != set(expected):
            _fail("RECONSTRUCTION", path, "has a different object shape")
        for key in expected:
            _assert_close(actual_mapping[key], expected[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        actual_list = _list(actual, path)
        if len(actual_list) != len(expected):
            _fail("RECONSTRUCTION", path, "has a different array length")
        for index, (actual_item, expected_item) in enumerate(zip(actual_list, expected)):
            _assert_close(actual_item, expected_item, f"{path}[{index}]")
        return
    if isinstance(expected, float):
        actual_number = _finite_number(actual, path)
        if not math.isclose(actual_number, expected, rel_tol=_SEMANTIC_RTOL, abs_tol=_SEMANTIC_ATOL):
            _fail("RECONSTRUCTION", path, "does not match deterministic numerical reconstruction")
        return
    if actual != expected:
        _fail("RECONSTRUCTION", path, "does not match deterministic reconstruction")


def verify_open_selection_fit_export(
    *,
    acquisition_receipt_path: Path | str,
    admission_receipt_path: Path | str,
    dataset_root: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    measurement_root: Path | str,
    export_root: Path | str,
) -> dict[str, object]:
    """Fully reverify an open-selection candidate from current permitted authorities."""

    _root, model, model_sha256, evaluation, evaluation_sha256, receipt, receipt_sha256 = _read_candidate_objects(export_root)
    _reject_forbidden_scope(model, "fit-model.json")
    _reject_forbidden_scope(evaluation, "selection-evaluation.json")
    _reject_forbidden_scope(receipt, "fit-export-receipt.json")
    _verify_current_artifact_bindings(
        model,
        model_sha256,
        evaluation,
        evaluation_sha256,
        receipt,
        receipt_sha256,
    )
    dataset, data, predecessor = _admission_context(
        acquisition_receipt_path=acquisition_receipt_path,
        admission_receipt_path=admission_receipt_path,
        dataset_root=dataset_root,
        shared_root=shared_root,
        open_root=open_root,
        measurement_root=measurement_root,
    )
    _verify_current_authority_bindings(model, data, predecessor)
    _verify_stored_metrics(model, evaluation, data)
    expected_model, expected_evaluation, expected_receipt = _fit_export_objects(dataset, data, predecessor)
    _assert_close(model, expected_model, "fit-model.json")
    _assert_close(evaluation, expected_evaluation, "selection-evaluation.json")
    _assert_close(receipt, expected_receipt, "fit-export-receipt.json")
    bindings = _mapping(receipt.get("bindings"), "fit-export-receipt.json.bindings")
    return {
        "status": "open_selection_fit_export_verified",
        "state": "OPEN_SELECTION_FIT_EXPORTED",
        "dataset_status": "open_selection_only",
        "production_pass": False,
        "runtime_compatible": False,
        "acquisition_preflight_receipt_sha256": _sha256(
            bindings.get("acquisition_preflight_receipt_sha256"),
            "fit-export-receipt.json.bindings.acquisition_preflight_receipt_sha256",
        ),
        "admission_receipt_sha256": _sha256(
            bindings.get("admission_receipt_sha256"),
            "fit-export-receipt.json.bindings.admission_receipt_sha256",
        ),
        "dataset_manifest_sha256": _sha256(
            bindings.get("dataset_manifest_sha256"),
            "fit-export-receipt.json.bindings.dataset_manifest_sha256",
        ),
        "open_measurements_sha256": _sha256(
            bindings.get("open_measurements_sha256"),
            "fit-export-receipt.json.bindings.open_measurements_sha256",
        ),
        "fit_model_sha256": model_sha256,
        "selection_evaluation_sha256": evaluation_sha256,
        "fit_export_receipt_sha256": receipt_sha256,
        **_permissions(),
    }


# Backward-compatible same-contract names for callers that adopted an early
# integration draft.  They intentionally expose no extra authority or options.
fit_and_export_open_selection = run_open_selection_fit_export
fit_open_selection = run_open_selection_fit_export
