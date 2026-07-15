"""Test-only open-selection spectra with a finite-film K-M oracle.

This fixture deliberately makes no physical, instrument, or product claim.  It
reuses the receipt-bound temporary acquisition/admission chain and replaces
only its synthetic spectra with deterministic finite-film predictions.  The
three-point admission fixture remains a separate transport-only fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import numpy as np


CALIBRATION_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = CALIBRATION_ROOT / "tests"

import sys

sys.path.insert(0, str(CALIBRATION_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from km_calibration.hashing import write_json_with_sha256
from km_calibration.km import finite_film_reflectance
from km_calibration.acquisition_preflight import COMPONENT_IDS
from open_measurement_admission_fixtures import write_valid_open_measurement_fixture


WAVELENGTH_NM = tuple(float(value) for value in range(400, 701, 20))
COMPONENT_COUNT = 15
TRAIN_CARD_COUNT = 30
VALIDATION_CARD_COUNT = 6
TRAIN_CELL_COUNT = 60
VALIDATION_CELL_COUNT = 12
REPOSITION_COUNT = 3


def _backing_mean(backing: str) -> np.ndarray:
    wavelength = np.asarray(WAVELENGTH_NM, dtype=float)
    if backing == "black":
        return 0.075 + 0.00020 * (wavelength - wavelength[0])
    if backing == "white":
        return 0.700 - 0.00010 * (wavelength - wavelength[0])
    raise AssertionError(f"unexpected fixture backing {backing!r}")


def _truth_component_curves(component_pairs: list[tuple[str, str]]) -> list[dict[str, object]]:
    """Return smooth, positive, nonphysical curves in the fixed receipt order."""

    wavelength = np.asarray(WAVELENGTH_NM, dtype=float)
    normalized = (wavelength - wavelength[0]) / (wavelength[-1] - wavelength[0])
    curves: list[dict[str, object]] = []
    for index, (component_id, physical_lot_id) in enumerate(component_pairs):
        phase = 0.31 * (index + 1)
        k_curve = 9.0 + 1.15 * index + 3.4 * normalized + 0.35 * np.sin(phase + 2.2 * normalized)
        s_curve = 118.0 + 2.6 * index + 15.0 * normalized + 1.1 * np.cos(phase + 1.7 * normalized)
        curves.append(
            {
                "component_id": component_id,
                "physical_lot_id": physical_lot_id,
                "k_mm_inv": [float(value) for value in k_curve],
                "s_mm_inv": [float(value) for value in s_curve],
            }
        )
    return curves


def _mean_dft_mm(card: Mapping[str, object], backing: str) -> float:
    dft_by_backing = card["dft_by_backing"]
    if not isinstance(dft_by_backing, Mapping):  # pragma: no cover - fixture invariant.
        raise AssertionError("fixture card dft_by_backing must be a mapping")
    dft_record = dft_by_backing[backing]
    if not isinstance(dft_record, Mapping):  # pragma: no cover - fixture invariant.
        raise AssertionError("fixture DFT record must be a mapping")
    points = dft_record["dft_points_um"]
    if not isinstance(points, list) or not points:  # pragma: no cover - fixture invariant.
        raise AssertionError("fixture DFT points must be non-empty")
    return float(np.mean(np.asarray(points, dtype=float)) / 1000.0)


def write_fit_ready_open_selection_fixture(root: Path) -> dict[str, object]:
    """Write a 15-component, 400--700 nm, finite-film-consistent input.

    The predecessor chain, component lots, cards, split assignment, and all
    temporary evidence bindings are produced by the established admission
    fixture.  Only the wavelength grid and test-only spectra are replaced.
    """

    fixture = write_valid_open_measurement_fixture(root)
    payload = fixture["payload"]
    receipt = fixture["open_receipt"]
    if not isinstance(payload, dict) or not isinstance(receipt, Mapping):  # pragma: no cover - fixture invariant.
        raise AssertionError("base admission fixture must return mappings")

    batches = receipt["batches"]
    if not isinstance(batches, list):  # pragma: no cover - fixture invariant.
        raise AssertionError("open receipt batches must be a list")
    lots_by_component: dict[str, str] = {}
    for batch in batches:
        if not isinstance(batch, Mapping):  # pragma: no cover - fixture invariant.
            raise AssertionError("base batch must be a mapping")
        batch_components = batch["components"]
        if not isinstance(batch_components, list):  # pragma: no cover - fixture invariant.
            raise AssertionError("base batch components must be a list")
        for component in batch_components:
            if not isinstance(component, Mapping):  # pragma: no cover - fixture invariant.
                raise AssertionError("base component must be a mapping")
            component_id = str(component["component_id"])
            physical_lot_id = str(component["physical_lot_id"])
            prior_lot = lots_by_component.setdefault(component_id, physical_lot_id)
            if prior_lot != physical_lot_id:  # pragma: no cover - fixture invariant.
                raise AssertionError("base receipt must retain one lot per component")
    component_pairs = [(component_id, lots_by_component[component_id]) for component_id in COMPONENT_IDS]
    if len(component_pairs) != COMPONENT_COUNT or set(lots_by_component) != set(COMPONENT_IDS):  # pragma: no cover - fixture invariant.
        raise AssertionError("base receipt must retain all fixed component identities and lots")
    truth_curves = _truth_component_curves(component_pairs)
    k_matrix = np.asarray([curve["k_mm_inv"] for curve in truth_curves], dtype=float)
    s_matrix = np.asarray([curve["s_mm_inv"] for curve in truth_curves], dtype=float)

    fractions_by_family: dict[str, np.ndarray] = {}
    for batch in batches:
        fractions = np.asarray(batch["actual_nv_vector"], dtype=float)
        if fractions.shape != (COMPONENT_COUNT,) or not np.isclose(fractions.sum(), 1.0):  # pragma: no cover - fixture invariant.
            raise AssertionError("base batch must retain a normalized 15-column actual-NV vector")
        fractions_by_family[str(batch["formula_family_id"])] = fractions

    payload["wavelength_nm"] = list(WAVELENGTH_NM)
    backing_means = {backing: _backing_mean(backing) for backing in ("black", "white")}
    backings = payload["backings"]
    if not isinstance(backings, Mapping):  # pragma: no cover - fixture invariant.
        raise AssertionError("base payload backings must be a mapping")
    for backing, mean in backing_means.items():
        backing_payload = backings[backing]
        if not isinstance(backing_payload, Mapping):  # pragma: no cover - fixture invariant.
            raise AssertionError("base backing payload must be a mapping")
        bare_measurements = backing_payload["bare_measurements"]
        if not isinstance(bare_measurements, list) or len(bare_measurements) != REPOSITION_COUNT:  # pragma: no cover - fixture invariant.
            raise AssertionError("base fixture must retain three bare measurements per backing")
        for offset, measurement in zip((-0.001, 0.0, 0.001), bare_measurements, strict=True):
            if not isinstance(measurement, dict):  # pragma: no cover - fixture invariant.
                raise AssertionError("base bare measurement must be mutable")
            measurement["reflectance"] = [float(value + offset) for value in mean]

    cards = payload["cards"]
    readings = payload["readings"]
    if not isinstance(cards, list) or not isinstance(readings, list):  # pragma: no cover - fixture invariant.
        raise AssertionError("base payload cards and readings must be lists")
    cards_by_id = {str(card["card_id"]): card for card in cards if isinstance(card, Mapping)}
    skeleton = receipt["card_skeleton"]
    if not isinstance(skeleton, list) or len(cards_by_id) != TRAIN_CARD_COUNT + VALIDATION_CARD_COUNT:  # pragma: no cover - fixture invariant.
        raise AssertionError("base receipt must retain the 36-card roster")
    family_by_card = {
        str(card["card_id"]): str(card["formula_family_id"])
        for card in skeleton
        if isinstance(card, Mapping)
    }
    if set(family_by_card) != set(cards_by_id):  # pragma: no cover - fixture invariant.
        raise AssertionError("base skeleton and admission cards must agree")

    expected_cell_spectra: dict[str, list[float]] = {}
    for card_id, card in cards_by_id.items():
        fractions = fractions_by_family[family_by_card[card_id]]
        k_mix = fractions @ k_matrix
        s_mix = fractions @ s_matrix
        for backing, mean in backing_means.items():
            spectrum = finite_film_reflectance(k_mix, s_mix, _mean_dft_mm(card, backing), mean)
            expected_cell_spectra[f"{card_id}|{backing}"] = [float(value) for value in spectrum]

    for reading in readings:
        if not isinstance(reading, dict):  # pragma: no cover - fixture invariant.
            raise AssertionError("base reading must be mutable")
        card_id = str(reading["card_id"])
        backing = str(reading["backing"])
        reading["reflectance"] = list(expected_cell_spectra[f"{card_id}|{backing}"])

    input_path = fixture["admission_input_path"]
    if not isinstance(input_path, Path):  # pragma: no cover - fixture invariant.
        raise AssertionError("base fixture admission input path must be a Path")
    write_json_with_sha256(input_path, payload)
    return {
        **fixture,
        "payload": payload,
        "wavelength_nm": list(WAVELENGTH_NM),
        "truth_component_curves": truth_curves,
        "expected_cell_spectra": expected_cell_spectra,
    }
