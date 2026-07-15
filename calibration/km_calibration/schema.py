"""Strict v1 dataset validation and formula-family split auditing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping
from types import MappingProxyType

import numpy as np

from .errors import DatasetValidationError
from .hashing import canonical_json_bytes, read_verified_json, sha256_bytes
from .km import validate_saunderson


DATASET_SCHEMA_VERSION = "moocow-km-calibration-dataset-v1"
# Split-leakage signatures use every declared component, sorted by ID, with
# fractions rounded to 12 decimal places. This absorbs trivial float spelling
# differences while keeping the comparison substantially tighter than the
# 1e-9 concentration-sum tolerance.
FORMULA_COMPOSITION_DECIMAL_PLACES = 12
_WINDOWS_RESERVED_PATH_ALIASES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _deep_freeze(value: Any) -> Any:
    """Copy JSON-shaped validation state into immutable mappings and tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


@dataclass
class ValidatedDataset:
    root: Path
    manifest: Mapping[str, Any]
    dataset_status: str
    records: tuple[Mapping[str, Any], ...]
    manifest_sha256: str
    source_hashes: tuple[Mapping[str, str], ...]
    family_splits: Mapping[str, str]
    split_record_counts: Mapping[str, int]
    split_audit_snapshot: Mapping[str, Any]
    _sealed: bool = field(init=False, repr=False, compare=False, default=False)

    def __post_init__(self) -> None:
        for name in (
            "manifest",
            "records",
            "source_hashes",
            "family_splits",
            "split_record_counts",
            "split_audit_snapshot",
        ):
            object.__setattr__(self, name, _deep_freeze(getattr(self, name)))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise TypeError("ValidatedDataset state is immutable")
        object.__setattr__(self, name, value)


def _fail(message: str) -> None:
    raise DatasetValidationError(message)


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and np.isfinite(value)


def _expect_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{label} must be an object")
    return value


def _expect_nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a non-empty string")
    return value


def _is_windows_reserved_path_alias(part: str) -> bool:
    return part.split(".", 1)[0].rstrip(" .").upper() in _WINDOWS_RESERVED_PATH_ALIASES


def _validate_wavelengths(value: object, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) < 3 or not all(_is_number(item) for item in value):
        _fail(f"{label} must contain at least three finite numeric wavelengths")
    wavelengths = [float(item) for item in value]
    if wavelengths[0] < 360 or wavelengths[-1] > 830:
        _fail(f"{label} must stay within the supported 360-830 nm range")
    differences = np.diff(wavelengths)
    if np.any(differences <= 0) or not np.allclose(differences, differences[0], rtol=0, atol=1e-9):
        _fail(f"{label} must be strictly increasing and uniformly sampled")
    return wavelengths


def _validate_reflectance(value: object, wavelengths: list[float], label: str) -> None:
    if not isinstance(value, list) or len(value) != len(wavelengths):
        _fail(f"{label} length must match wavelength_nm")
    if not all(_is_number(item) and 0 <= float(item) <= 1 for item in value):
        _fail(f"{label} must contain finite reflectance values in [0, 1]")


def _validate_components(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = manifest.get("components")
    if not isinstance(raw, list) or not raw:
        _fail("manifest.components must be a non-empty array")
    components: dict[str, Mapping[str, Any]] = {}
    base_count = 0
    for index, item in enumerate(raw):
        component = _expect_mapping(item, f"components[{index}]")
        component_id = _expect_nonempty_string(component.get("component_id"), f"components[{index}].component_id")
        _expect_nonempty_string(component.get("batch_id"), f"components[{index}].batch_id")
        role = component.get("role")
        if role not in {"base", "colorant"}:
            _fail(f"components[{index}].role must be base or colorant")
        if component_id in components:
            _fail(f"Duplicate component_id {component_id}")
        components[component_id] = component
        base_count += int(role == "base")
    if base_count != 1:
        _fail("Exactly one explicit base component is required")
    return components


def _validate_backings(manifest: Mapping[str, Any], wavelengths: list[float]) -> None:
    backings = _expect_mapping(manifest.get("backings"), "manifest.backings")
    if set(backings) != {"black", "white"}:
        _fail("manifest.backings must contain exactly black and white backing spectra")
    for name in ("black", "white"):
        backing = _expect_mapping(backings[name], f"backings.{name}")
        _validate_reflectance(backing.get("reflectance"), wavelengths, f"backings.{name}.reflectance")
    if np.allclose(backings["black"]["reflectance"], backings["white"]["reflectance"], rtol=0, atol=1e-12):
        _fail("Black and white backing spectra must differ")


def _validate_formula_components(
    value: object,
    components: Mapping[str, Mapping[str, Any]],
    label: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not value:
        _fail(f"{label} must be a non-empty component array")
    seen: set[str] = set()
    fractions: dict[str, float] = {}
    for index, item in enumerate(value):
        component = _expect_mapping(item, f"{label}[{index}]")
        component_id = _expect_nonempty_string(component.get("component_id"), f"{label}[{index}].component_id")
        fraction = component.get("nonvolatile_volume_fraction")
        if component_id not in components:
            _fail(f"{label}[{index}] uses undeclared component {component_id}")
        if component_id in seen:
            _fail(f"{label} cannot repeat component {component_id}")
        if not _is_number(fraction) or float(fraction) < 0:
            _fail(f"{label}[{index}].nonvolatile_volume_fraction must be non-negative")
        seen.add(component_id)
        fractions[component_id] = float(fraction)
    if not any(components[component_id]["role"] == "base" and fraction > 0 for component_id, fraction in fractions.items()):
        _fail(f"{label} must explicitly include a positive base fraction")
    if not np.isclose(sum(fractions.values()), 1.0, rtol=0, atol=1e-9):
        _fail(f"{label} nonvolatile_volume_fraction values must sum to 1")
    canonical: list[tuple[str, str]] = []
    for component_id in sorted(components):
        rounded = round(fractions.get(component_id, 0.0), FORMULA_COMPOSITION_DECIMAL_PLACES)
        if rounded == 0:
            rounded = 0.0
        canonical.append(
            (component_id, f"{rounded:.{FORMULA_COMPOSITION_DECIMAL_PLACES}f}")
        )
    return tuple(canonical)


def _validate_splits(manifest: Mapping[str, Any], records: list[dict[str, Any]]) -> dict[str, str]:
    raw_splits = _expect_mapping(manifest.get("splits"), "manifest.splits")
    if set(raw_splits) != {"train", "validation", "holdout"}:
        _fail("manifest.splits must contain exactly train, validation, and holdout")
    family_splits: dict[str, str] = {}
    for split, families in raw_splits.items():
        if not isinstance(families, list) or not families:
            _fail(f"splits.{split} must be a non-empty array")
        for family in families:
            family_id = _expect_nonempty_string(family, f"splits.{split}")
            if family_id in family_splits:
                _fail(f"formula_family_id {family_id} leaks across {family_splits[family_id]} and {split}")
            family_splits[family_id] = split
    observed = {record["formula_family_id"] for record in records}
    if observed != set(family_splits):
        missing = sorted(observed - set(family_splits))
        stale = sorted(set(family_splits) - observed)
        _fail(f"Split assignment mismatch; unassigned={missing}, no-record families={stale}")

    family_by_artifact: dict[tuple[str, str], str] = {}
    for record in records:
        family = record["formula_family_id"]
        for field in ("formula_id", "formula_batch_id", "card_id", "sample_group_id"):
            key = (field, record[field])
            prior = family_by_artifact.setdefault(key, family)
            if prior != family:
                _fail(f"{field} {record[field]} crosses formula families {prior} and {family}")
    return family_splits


def load_and_validate_dataset(dataset_root: Path | str) -> ValidatedDataset:
    root = Path(dataset_root).resolve()
    manifest_path = root / "manifest.json"
    manifest_raw, manifest_sha256 = read_verified_json(
        manifest_path,
        require_sidecar=True,
        trusted_root=root,
    )
    manifest = _expect_mapping(manifest_raw, "manifest")
    if manifest.get("schema_version") != DATASET_SCHEMA_VERSION:
        _fail(f"Unsupported dataset schema_version {manifest.get('schema_version')!r}")
    dataset_status = manifest.get("dataset_status")
    if dataset_status not in {"synthetic_only", "research_only"}:
        _fail("dataset_status must be synthetic_only or research_only")
    if manifest.get("physical_ranking_enabled") is not False:
        _fail("physical_ranking_enabled must be false; physical ranking is never enabled by v1")
    if manifest.get("concentration_basis") != "nonvolatile_volume_fraction":
        _fail("concentration_basis must be nonvolatile_volume_fraction")

    wavelengths = _validate_wavelengths(manifest.get("wavelength_nm"), "manifest.wavelength_nm")
    locked_conditions = _expect_mapping(manifest.get("locked_conditions"), "manifest.locked_conditions")
    if not locked_conditions:
        _fail("locked_conditions must not be empty")
    expected_conditions_sha256 = sha256_bytes(canonical_json_bytes(locked_conditions))
    components = _validate_components(manifest)
    _validate_backings(manifest, wavelengths)
    validate_saunderson(_expect_mapping(manifest.get("saunderson"), "manifest.saunderson"))

    source_files = manifest.get("source_files")
    if not isinstance(source_files, list) or not source_files:
        _fail("manifest.source_files must be a non-empty array")
    source_hashes: list[dict[str, str]] = []
    records: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, descriptor in enumerate(source_files):
        descriptor = _expect_mapping(descriptor, f"source_files[{index}]")
        relative_path = _expect_nonempty_string(descriptor.get("path"), f"source_files[{index}].path")
        windows_path = PureWindowsPath(relative_path)
        posix_path = PurePosixPath(relative_path)
        if (
            relative_path in seen_paths
            or "\\" in relative_path
            or "\x00" in relative_path
            or any(character in '<>:"|?*' for character in relative_path)
            or windows_path.is_absolute()
            or windows_path.drive
            or posix_path.is_absolute()
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
            or any(part.endswith((".", " ")) for part in posix_path.parts)
            or any(_is_windows_reserved_path_alias(part) for part in posix_path.parts)
        ):
            _fail(f"source_files[{index}].path must be a unique relative safe path")
        seen_paths.add(relative_path)
        expected_hash = descriptor.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            _fail(f"source_files[{index}].sha256 must be a SHA-256 hex digest")
        source_path = root.joinpath(*posix_path.parts)
        try:
            source_path.relative_to(root)
        except ValueError:
            _fail(f"source_files[{index}].path must resolve within the dataset root")
        source_raw, actual_hash = read_verified_json(
            source_path,
            expected_sha256=expected_hash,
            trusted_root=root,
        )
        source_hashes.append({"path": relative_path, "sha256": actual_hash})
        source = _expect_mapping(source_raw, f"source {relative_path}")
        if source.get("dataset_status") != dataset_status:
            _fail(f"{relative_path} dataset_status does not match manifest")
        if source.get("wavelength_nm") != manifest["wavelength_nm"]:
            _fail(f"{relative_path} wavelength_nm does not match manifest")
        if source.get("locked_conditions") != locked_conditions:
            _fail(f"{relative_path} locked_conditions does not match manifest")
        if descriptor.get("kind") == "measurement_records":
            raw_records = source.get("measurements")
            if not isinstance(raw_records, list) or not raw_records:
                _fail(f"{relative_path} must contain non-empty measurements")
            records.extend(raw_records)

    if not records:
        _fail("No measurement_records were declared")
    seen_measurements: set[str] = set()
    seen_repeats: set[tuple[str, str]] = set()
    formula_signatures: dict[str, tuple[tuple[str, str], ...]] = {}
    family_by_composition: dict[tuple[tuple[str, str], ...], str] = {}
    for index, record in enumerate(records):
        record = _expect_mapping(record, f"measurements[{index}]")
        for unsupported in ("hex", "qtc", "hidingPower", "strength"):
            if unsupported in record:
                _fail(f"measurements[{index}] includes unsupported non-physical field {unsupported}")
        measurement_id = _expect_nonempty_string(record.get("measurement_id"), f"measurements[{index}].measurement_id")
        if measurement_id in seen_measurements:
            _fail(f"Duplicate measurement_id {measurement_id}")
        seen_measurements.add(measurement_id)
        for field in ("formula_family_id", "formula_id", "formula_batch_id", "card_id", "sample_group_id", "repeat_id"):
            _expect_nonempty_string(record.get(field), f"measurements[{index}].{field}")
        repeat_key = (record["sample_group_id"], record["repeat_id"])
        if repeat_key in seen_repeats:
            _fail(f"Duplicate repeat_id {record['repeat_id']} in sample_group_id {record['sample_group_id']}")
        seen_repeats.add(repeat_key)
        if record.get("backing") not in {"black", "white"}:
            _fail(f"measurements[{index}].backing must be black or white")
        dft_um = record.get("dft_um")
        if not _is_number(dft_um) or not 0 < float(dft_um) <= 5000:
            _fail(f"measurements[{index}].dft_um must be a realistic positive micrometre value")
        if record.get("conditions") != locked_conditions or record.get("conditions_sha256") != expected_conditions_sha256:
            _fail(f"measurements[{index}] violates the locked measurement conditions")
        target_kind = record.get("target_kind")
        expected_target_kind = "synthetic_spectrum" if dataset_status == "synthetic_only" else "measured_spectrum"
        if target_kind != expected_target_kind:
            _fail(f"measurements[{index}].target_kind must be {expected_target_kind}")
        _validate_reflectance(record.get("reflectance"), wavelengths, f"measurements[{index}].reflectance")
        signature = _validate_formula_components(record.get("components"), components, f"measurements[{index}].components")
        prior_signature = formula_signatures.setdefault(record["formula_id"], signature)
        if signature != prior_signature:
            _fail(f"formula_id {record['formula_id']} has inconsistent component concentrations")
        family = record["formula_family_id"]
        prior_family = family_by_composition.setdefault(signature, family)
        if prior_family != family:
            _fail(
                "Canonical formula composition crosses formula families "
                f"{prior_family} and {family}"
            )

    family_splits = _validate_splits(manifest, records)
    split_names = ("train", "validation", "holdout")
    split_record_counts = {
        split: sum(family_splits[record["formula_family_id"]] == split for record in records)
        for split in split_names
    }
    split_audit_snapshot = {
        "family_counts": {
            split: sum(assigned_split == split for assigned_split in family_splits.values())
            for split in split_names
        },
        "record_counts": split_record_counts,
        "families": {
            split: tuple(sorted(family for family, assigned_split in family_splits.items() if assigned_split == split))
            for split in split_names
        },
    }
    public_records = (
        records
        if dataset_status == "synthetic_only"
        else [record for record in records if family_splits[record["formula_family_id"]] != "holdout"]
    )
    return ValidatedDataset(
        root=root,
        manifest=dict(manifest),
        dataset_status=dataset_status,
        records=tuple(public_records),
        manifest_sha256=manifest_sha256,
        source_hashes=tuple(source_hashes),
        family_splits=family_splits,
        split_record_counts=split_record_counts,
        split_audit_snapshot=split_audit_snapshot,
    )


def split_audit(dataset: ValidatedDataset) -> dict[str, Any]:
    snapshot = dataset.split_audit_snapshot
    return {
        "status": "pass",
        "split_unit": "formula_family_id",
        "family_counts": dict(snapshot["family_counts"]),
        "record_counts": dict(snapshot["record_counts"]),
        "families": {split: list(families) for split, families in snapshot["families"].items()},
        "leakage": False,
    }
