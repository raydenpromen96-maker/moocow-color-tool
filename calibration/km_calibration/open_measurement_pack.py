"""Receipt-derived open-measurement templates and neutral CSV assembler.

This module only prepares incomplete operator templates and assembles completed
open measurements into the existing admission-input transport.  It has no
fitting, promotion, release, or activation authority.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from stat import S_ISDIR, S_ISLNK, S_ISREG
from typing import Any, Iterable, Mapping

from .acquisition_preflight import PERMISSIONS, load_verified_open_acquisition_context
from .errors import CalibrationError
from .hashing import canonical_json_bytes, read_regular_file_snapshot, read_verified_json, sha256_bytes, write_json_with_sha256
from .open_measurement_admission import INPUT_SCHEMA


PACK_SCHEMA = "moocow-open-measurement-pack-v1"
PROFILE_SCHEMA = "moocow-open-measurement-operator-profile-v1"
_BACKINGS = ("black", "white")
_POSITIONS = ("POS01", "POS02", "POS03")
_PLACEHOLDERS = ("required", "template", "placeholder", "synthetic", "inferred", "not_yet")
_OPEN_SCOPE_MARKERS = ("holdout", "sealed", "custody", "release")
_FAM_HO_MARKER = "fam-ho-"
_REPARSE = 0x400
_MAX_JSON_BYTES = 64 * 1024
_MAX_CSV_BYTES = 16 * 1024 * 1024
_MAX_FIELD_BYTES = 4 * 1024
_MAX_SPECTRA_ROWS = 222 * 2_000
_TEMPLATES = (
    "operator-input/measurement-profile.template.json",
    "operator-input/backings.template.csv",
    "operator-input/bare-readings.template.csv",
    "operator-input/dft-readings.template.csv",
    "operator-input/coated-readings.template.csv",
    "operator-input/spectra-long.template.csv",
)
_COMPLETED = tuple(item.replace(".template", "") for item in _TEMPLATES)
_COMPLETED_NAMES = tuple(PurePosixPath(item).name for item in _COMPLETED)


class OpenMeasurementPackError(CalibrationError):
    """Stable non-secret-bearing operator-pack failure."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"[{code}] {path}: {message}")


def _fail(code: str, path: str, message: str) -> None:
    raise OpenMeasurementPackError(code, path, message)


def _permissions() -> dict[str, bool]:
    return {name: False for name in PERMISSIONS}


def _link_or_reparse(stat: os.stat_result) -> bool:
    return S_ISLNK(stat.st_mode) or bool(getattr(stat, "st_file_attributes", 0) & _REPARSE)


def _contains_non_open_scope(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in _OPEN_SCOPE_MARKERS) or _FAM_HO_MARKER in lowered


def _reject_text(value: object, path: str, *, placeholder: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("PLACEHOLDER" if placeholder else "PACK_BINDING", path, "must be a non-empty string")
    text = value.strip()
    if len(text.encode("utf-8")) > _MAX_FIELD_BYTES:
        _fail("INPUT_LIMIT", path, f"must not exceed {_MAX_FIELD_BYTES} UTF-8 bytes")
    if placeholder and any(marker in text.casefold() for marker in _PLACEHOLDERS):
        _fail("PLACEHOLDER", path, "contains a template placeholder")
    if _contains_non_open_scope(text):
        _fail("PACK_BINDING", path, "contains prohibited non-open vocabulary")
    return text


def _reject_nested_values(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_text(str(key), f"{path}.{key}", placeholder=False)
            _reject_nested_values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_nested_values(item, f"{path}[{index}]")
    elif isinstance(value, str):
        _reject_text(value, path)


def _safe_relative(value: object, path: str) -> str:
    text = _reject_text(value, path)
    pure = PurePosixPath(text)
    if (
        "\\" in text
        or "\x00" in text
        or any(character in '<>:"|?*' for character in text)
        or pure.is_absolute()
        or not pure.parts
        or any(part in ("", ".", "..") or part.endswith((".", " ")) for part in pure.parts)
    ):
        _fail("EVIDENCE_PATH", path, "must be a non-empty portable relative path")
    windows = PureWindowsPath(text)
    if windows.is_absolute() or windows.drive:
        _fail("EVIDENCE_PATH", path, "must not be an absolute Windows path")
    return pure.as_posix()


def _root(value: Path | str, path: str) -> Path:
    _reject_text(str(value), path, placeholder=False)
    candidate = Path(value).absolute()
    return _safe_directory_chain(candidate, code="PACK_TREE", path=path, create=False)


def _open_context(*, acquisition_receipt_path: Path | str, shared_root: Path | str, open_root: Path | str) -> dict[str, Any]:
    try:
        context = load_verified_open_acquisition_context(
            receipt_path=acquisition_receipt_path,
            shared_root=shared_root,
            open_root=open_root,
        )
    except CalibrationError as error:
        _fail("PREDECESSOR", "acquisition_receipt", str(error))
    if len(context.get("card_skeleton", [])) != 36:
        _fail("PREDECESSOR", "acquisition_receipt", "did not yield the fixed 36-card open roster")
    return context


def _csv_bytes(headers: Iterable[str], rows: Iterable[Iterable[object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(headers))
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _bare_slot(backing: str, position: str) -> str:
    return f"bare:{backing}:{position}"


def _coated_slot(card_id: str, backing: str, position: str) -> str:
    return f"coated:{card_id}:{backing}:{position}"


def _slots(context: Mapping[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    cards = context["card_skeleton"]
    if not isinstance(cards, list) or len(cards) != 36:
        _fail("PREDECESSOR", "card_skeleton", "must contain exactly 36 cards")
    bare = [
        {"measurement_slot_id": _bare_slot(backing, position), "backing": backing, "reposition_id": position}
        for backing in _BACKINGS
        for position in _POSITIONS
    ]
    dft: list[dict[str, str]] = []
    coated: list[dict[str, str]] = []
    roster: list[dict[str, str]] = []
    for card in cards:
        if not isinstance(card, Mapping) or not isinstance(card.get("card_id"), str):
            _fail("PREDECESSOR", "card_skeleton", "contains an invalid card")
        card_id = card["card_id"]
        roster.append({"card_id": card_id})
        for backing in _BACKINGS:
            dft.append({"card_id": card_id, "backing": backing})
            for position in _POSITIONS:
                coated.append(
                    {
                        "measurement_slot_id": _coated_slot(card_id, backing, position),
                        "card_id": card_id,
                        "backing": backing,
                        "reposition_id": position,
                    }
                )
    if len(dft) != 72 or len(coated) != 216:
        _fail("PREDECESSOR", "card_skeleton", "does not produce the fixed DFT/coated roster")
    return roster, bare, dft, coated


def _template_payloads(context: Mapping[str, Any]) -> dict[str, bytes]:
    _roster, bare, dft, coated = _slots(context)
    profile = {
        "schema_version": PROFILE_SCHEMA,
        "measurement_session_id": "REQUIRED_MEASUREMENT_SESSION_ID",
        "instrument_id": "REQUIRED_INSTRUMENT_ID",
        "fixture_protocol_id": "REQUIRED_FIXTURE_PROTOCOL_ID",
        "instrument_calibration_evidence_relative_path": "REQUIRED_CALIBRATION_EVIDENCE_RELATIVE_PATH",
        "instrument_run_log_evidence_relative_path": "REQUIRED_RUN_LOG_EVIDENCE_RELATIVE_PATH",
    }
    result = {
        _TEMPLATES[0]: canonical_json_bytes(profile) + b"\n",
        _TEMPLATES[1]: _csv_bytes(
            ("backing", "backing_id", "lot_id"),
            ((backing, "REQUIRED_BACKING_ID", "REQUIRED_LOT_ID") for backing in _BACKINGS),
        ),
        _TEMPLATES[2]: _csv_bytes(
            ("backing", "reposition_id", "instrument_measurement_id", "measured_at_local", "raw_spectrum_evidence_relative_path"),
            ((item["backing"], item["reposition_id"], "REQUIRED_INSTRUMENT_MEASUREMENT_ID", "REQUIRED_ISO8601_TIMESTAMP", "REQUIRED_RAW_SPECTRUM_EVIDENCE_RELATIVE_PATH") for item in bare),
        ),
        _TEMPLATES[3]: _csv_bytes(
            ("card_id", "backing", "dft_measurement_id", "measured_at_local", "dft_evidence_relative_path", "dft_points_um"),
            ((item["card_id"], item["backing"], "REQUIRED_DFT_MEASUREMENT_ID", "REQUIRED_ISO8601_TIMESTAMP", "REQUIRED_DFT_EVIDENCE_RELATIVE_PATH", "REQUIRED_POSITIVE_UM_VALUES_SEMICOLON_SEPARATED") for item in dft),
        ),
        _TEMPLATES[4]: _csv_bytes(
            ("card_id", "backing", "reposition_id", "instrument_measurement_id", "position_note", "orientation", "measured_at_local", "raw_spectrum_evidence_relative_path", "surface_status", "model_applicability_status"),
            ((item["card_id"], item["backing"], item["reposition_id"], "REQUIRED_INSTRUMENT_MEASUREMENT_ID", "REQUIRED_POSITION_NOTE", "REQUIRED_ORIENTATION", "REQUIRED_ISO8601_TIMESTAMP", "REQUIRED_RAW_SPECTRUM_EVIDENCE_RELATIVE_PATH", "accepted_uniform_dry_film", "accepted_for_km_diagnostic") for item in coated),
        ),
        _TEMPLATES[5]: _csv_bytes(
            ("measurement_slot_id", "wavelength_nm", "reflectance"),
            ((item["measurement_slot_id"], "REQUIRED_WAVELENGTH_NM", "REQUIRED_REFLECTANCE_FRACTION") for item in [*bare, *coated]),
        ),
    }
    return result


def _manifest(context: Mapping[str, Any], template_hashes: Mapping[str, str]) -> dict[str, Any]:
    roster, bare, dft, coated = _slots(context)
    return {
        "schema_version": PACK_SCHEMA,
        "status": "template_only_incomplete",
        "state": "OPEN_MEASUREMENT_PACK_INCOMPLETE",
        "evidence_class": "operator_template_only",
        "production_pass": False,
        **_permissions(),
        "predecessor": {
            "acquisition_preflight_receipt_sha256": context["acquisition_preflight_receipt_sha256"],
            "open_source_binding": context["open_source_binding"],
        },
        "counts": {
            "cards": 36,
            "card_backing_dft_records": 72,
            "bare_readings": 6,
            "coated_readings": 216,
            "spectra_identities": 222,
        },
        "card_roster": roster,
        "card_skeleton": context["card_skeleton"],
        "bare_slots": bare,
        "dft_slots": dft,
        "coated_slots": coated,
        "template_sha256": dict(template_hashes),
    }


def _pack_readme() -> bytes:
    return (
        "# Open measurement operator pack\n\n"
        "This receipt-derived pack is deliberately incomplete and is not admission input.\n"
        "Copy every file from `operator-input/`, removing `.template` from each name, then replace every `REQUIRED_*` value.\n"
        "For `spectra-long.csv`, repeat each immutable slot once for every wavelength on one shared, strictly increasing, uniformly spaced grid; reflectance is a 0..1 fraction.\n"
        "Do not add formula, lot, DFT-mean, holdout, promotion, release, or activation fields.\n"
    ).encode("utf-8")


def _assert_tree(root: Path, *, expected_files: set[str], expected_dirs: set[str], code: str) -> None:
    files: set[str] = set()
    directories: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            children = list(directory.iterdir())
        except OSError as error:
            _fail(code, str(directory), str(error))
        for child in children:
            try:
                stat = child.lstat()
            except OSError as error:
                _fail(code, str(child), str(error))
            relative = child.relative_to(root).as_posix()
            if _link_or_reparse(stat):
                _fail(code, relative, "must not be a link or reparse point")
            if S_ISDIR(stat.st_mode):
                directories.add(relative)
                pending.append(child)
            elif S_ISREG(stat.st_mode):
                files.add(relative)
            else:
                _fail(code, relative, "must be a regular file or directory")
    if files != expected_files or directories != expected_dirs:
        _fail(code, str(root), "does not contain the exact expected files")


def _safe_directory_chain(directory: Path, *, code: str, path: str, create: bool) -> Path:
    """Validate every directory component and optionally create missing ones.

    Checks run before and after each mkdir so pre-existing static symlink,
    junction, and other reparse-point escapes are rejected.  Python's standard
    library cannot make this race-free against a hostile concurrent Windows
    rename; callers must keep these operator roots in trusted local storage.
    """

    try:
        absolute = directory.absolute()
        anchor = Path(absolute.anchor)
        relative = absolute.relative_to(anchor)
    except (OSError, ValueError) as error:
        _fail(code, path, str(error))

    current = anchor
    for part in ("", *relative.parts):
        if part:
            current = current / part
        try:
            stat = current.lstat()
        except FileNotFoundError:
            if not create:
                _fail(code, str(current), "directory does not exist")
            try:
                current.mkdir()
                stat = current.lstat()
            except OSError as error:
                _fail(code, str(current), str(error))
        except OSError as error:
            _fail(code, str(current), str(error))
        if _link_or_reparse(stat) or not S_ISDIR(stat.st_mode):
            _fail(code, str(current), "must be a non-link directory")
    return absolute


def _assert_new_regular_file(path: Path, *, code: str) -> None:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        _fail(code, str(path), str(error))
    if _link_or_reparse(stat) or not S_ISREG(stat.st_mode):
        _fail(code, str(path), "must be a non-link regular file")
    _fail(code, str(path), "must not already exist")


def _assert_published_regular_file(path: Path, *, code: str) -> None:
    try:
        stat = path.lstat()
    except OSError as error:
        _fail(code, str(path), str(error))
    if _link_or_reparse(stat) or not S_ISREG(stat.st_mode):
        _fail(code, str(path), "must be a non-link regular file")


def _staging(output_dir: Path | str) -> tuple[Path, Path, bool]:
    _reject_text(str(output_dir), "output_dir", placeholder=False)
    try:
        output = Path(output_dir).absolute()
    except OSError as error:
        _fail("OUTPUT_PATH", "output_dir", str(error))
    _safe_directory_chain(output.parent, code="OUTPUT_PATH", path="output_dir", create=True)
    try:
        stat = output.lstat()
    except FileNotFoundError:
        exists = False
    except OSError as error:
        _fail("OUTPUT_PATH", str(output), str(error))
    else:
        exists = True
        if _link_or_reparse(stat) or not S_ISDIR(stat.st_mode):
            _fail("OUTPUT_PATH", str(output), "must be a new or empty non-link directory")
        _safe_directory_chain(output, code="OUTPUT_PATH", path="output_dir", create=False)
        try:
            nonempty = any(output.iterdir())
        except OSError as error:
            _fail("OUTPUT_PATH", str(output), str(error))
        if nonempty:
            _fail("OUTPUT_PATH", str(output), "must be a new or empty non-link directory")
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        _fail("OUTPUT_WRITE", str(staging), str(error))
    _safe_directory_chain(staging, code="OUTPUT_PATH", path="output_dir", create=False)
    return output, staging, exists


def _publish(output: Path, staging: Path, existed_empty: bool) -> None:
    try:
        _safe_directory_chain(output.parent, code="OUTPUT_PATH", path="output_dir", create=False)
        _safe_directory_chain(staging, code="OUTPUT_PATH", path="output_dir", create=False)
        if existed_empty:
            _safe_directory_chain(output, code="OUTPUT_PATH", path="output_dir", create=False)
            if any(output.iterdir()):
                _fail("OUTPUT_PATH", str(output), "must remain empty before publication")
            output.rmdir()
        else:
            _assert_new_regular_file(output, code="OUTPUT_PATH")
        staging.replace(output)
        _safe_directory_chain(output, code="OUTPUT_PATH", path="output_dir", create=False)
    except OpenMeasurementPackError:
        raise
    except OSError as error:
        if existed_empty and not output.exists():
            _safe_directory_chain(output, code="OUTPUT_PATH", path="output_dir", create=True)
        _fail("OUTPUT_WRITE", str(output), str(error))


def _verify_pack(root: Path, context: Mapping[str, Any]) -> dict[str, Any]:
    _assert_tree(root, expected_files={"README.md", "pack-manifest.json", "pack-manifest.json.sha256", *_TEMPLATES}, expected_dirs={"operator-input"}, code="PACK_TREE")
    try:
        manifest, _digest = read_verified_json(root / "pack-manifest.json", require_sidecar=True, trusted_root=root)
    except CalibrationError as error:
        _fail("PACK_BINDING", "pack-manifest.json", str(error))
    if not isinstance(manifest, Mapping):
        _fail("PACK_BINDING", "pack-manifest.json", "must be an object")
    payloads = _template_payloads(context)
    expected_hashes = {path: sha256_bytes(value) for path, value in payloads.items()}
    if dict(manifest) != _manifest(context, expected_hashes):
        _fail("PACK_BINDING", "pack-manifest.json", "does not match the reverified predecessor context")
    for path, expected_hash in expected_hashes.items():
        try:
            _raw, actual_hash = read_regular_file_snapshot(root / path, trusted_root=root)
        except CalibrationError as error:
            _fail("PACK_BINDING", path, str(error))
        if actual_hash != expected_hash:
            _fail("PACK_BINDING", path, "does not match the receipt-derived template hash")
    return dict(manifest)


def prepare_open_measurement_pack(
    *,
    acquisition_receipt_path: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    output_dir: Path | str,
) -> dict[str, object]:
    """Create an incomplete 36/72/6/216/222 operator pack atomically."""

    context = _open_context(acquisition_receipt_path=acquisition_receipt_path, shared_root=shared_root, open_root=open_root)
    output, staging, existed_empty = _staging(output_dir)
    try:
        payloads = _template_payloads(context)
        for path, content in payloads.items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (staging / "README.md").write_bytes(_pack_readme())
        template_hashes = {path: sha256_bytes(content) for path, content in payloads.items()}
        manifest_hash = write_json_with_sha256(staging / "pack-manifest.json", _manifest(context, template_hashes))
        _verify_pack(staging, context)
        _publish(output, staging, existed_empty)
        return {
            "status": "template_only_incomplete",
            "state": "OPEN_MEASUREMENT_PACK_INCOMPLETE",
            "pack_manifest_sha256": manifest_hash,
            "cards": 36,
            "card_backing_dft_records": 72,
            "bare_readings": 6,
            "coated_readings": 216,
            "spectra_identities": 222,
            "output_dir": str(output),
            "production_pass": False,
            **_permissions(),
        }
    except OpenMeasurementPackError:
        raise
    except (OSError, CalibrationError) as error:
        _fail("OUTPUT_WRITE", str(staging), str(error))
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _read_json(path: Path, root: Path, code: str) -> object:
    try:
        raw, _digest = read_regular_file_snapshot(path, trusted_root=root)
        if len(raw) > _MAX_JSON_BYTES:
            _fail("INPUT_LIMIT", str(path), f"must not exceed {_MAX_JSON_BYTES} bytes")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=lambda pairs: _unique_json(pairs, str(path)), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except OpenMeasurementPackError:
        raise
    except (CalibrationError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        _fail(code, str(path), str(error))


def _unique_json(pairs: list[tuple[str, Any]], path: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("CSV_SCHEMA", path, f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read_csv(
    path: Path,
    root: Path,
    headers: tuple[str, ...],
    *,
    max_rows: int,
) -> list[dict[str, str]]:
    try:
        raw, _digest = read_regular_file_snapshot(path, trusted_root=root)
        if len(raw) > _MAX_CSV_BYTES:
            _fail("INPUT_LIMIT", str(path), f"must not exceed {_MAX_CSV_BYTES} bytes")
        text = raw.decode("utf-8")
        reader = csv.reader(io.StringIO(text, newline=""))
        header = next(reader, None)
    except OpenMeasurementPackError:
        raise
    except (CalibrationError, UnicodeDecodeError, csv.Error) as error:
        _fail("CSV_SCHEMA", str(path), str(error))
    if not header or tuple(header) != headers or len(set(header)) != len(header):
        _fail("CSV_SCHEMA", str(path), "must have the exact non-duplicate header row")
    if any(len(field.encode("utf-8")) > _MAX_FIELD_BYTES for field in header):
        _fail("INPUT_LIMIT", str(path), f"contains a field larger than {_MAX_FIELD_BYTES} UTF-8 bytes")
    result: list[dict[str, str]] = []
    try:
        for index, row in enumerate(reader, start=2):
            if index - 1 > max_rows:
                _fail("INPUT_LIMIT", str(path), f"must not exceed {max_rows} data rows")
            if len(row) != len(headers) or not any(cell.strip() for cell in row):
                _fail("CSV_SCHEMA", f"{path}:{index}", "must be a complete non-empty row")
            if any(len(field.encode("utf-8")) > _MAX_FIELD_BYTES for field in row):
                _fail("INPUT_LIMIT", f"{path}:{index}", f"contains a field larger than {_MAX_FIELD_BYTES} UTF-8 bytes")
            result.append(dict(zip(headers, row, strict=True)))
    except csv.Error as error:
        _fail("CSV_SCHEMA", str(path), str(error))
    return result


def _timestamp(value: object, path: str) -> str:
    text = _reject_text(value, path)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail("TIMESTAMP", path, "must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("TIMESTAMP", path, "must include a timezone offset")
    return text


def _positive_points(value: object, path: str) -> list[float]:
    text = _reject_text(value, path)
    try:
        result = [float(item) for item in text.split(";")]
    except ValueError:
        _fail("DFT_VALUE", path, "must be semicolon-separated positive micrometre values")
    if not result or any(not math.isfinite(item) or item <= 0 for item in result):
        _fail("DFT_VALUE", path, "must contain only positive finite micrometre values")
    return result


def _evidence(value: object, measurement_root: Path, path: str, seen_paths: set[str], seen_digests: set[str]) -> dict[str, object]:
    relative = _safe_relative(value, path)
    try:
        raw, digest = read_regular_file_snapshot(measurement_root.joinpath(*PurePosixPath(relative).parts), trusted_root=measurement_root)
    except CalibrationError as error:
        _fail("EVIDENCE_PATH", relative, str(error))
    if not raw:
        _fail("EVIDENCE_PATH", relative, "must not be empty")
    if relative in seen_paths:
        _fail("EVIDENCE_BINDING", relative, "duplicates an evidence path already bound by this assembly")
    if digest in seen_digests:
        _fail("EVIDENCE_BINDING", relative, "duplicates an evidence SHA-256 already bound by this assembly")
    seen_paths.add(relative)
    seen_digests.add(digest)
    return {"relative_path": relative, "record_locator": {"kind": "whole_file"}}


def _check_expected(rows: list[dict[str, str]], *, expected: set[tuple[str, ...]], keys: tuple[str, ...], path: str) -> dict[tuple[str, ...], dict[str, str]]:
    result: dict[tuple[str, ...], dict[str, str]] = {}
    for index, row in enumerate(rows):
        key = tuple(row[item] for item in keys)
        if key in result:
            _fail("ROSTER", f"{path}[{index}]", "duplicates an immutable roster row")
        if key not in expected:
            _fail("ROSTER", f"{path}[{index}]", "is not part of the receipt-derived roster")
        result[key] = row
    if set(result) != expected:
        _fail("ROSTER", path, "does not contain the complete immutable roster")
    return result


def _measurement_id(value: object, path: str, seen: set[str]) -> str:
    result = _reject_text(value, path)
    if result in seen:
        _fail("DUPLICATE_ID", path, "duplicates another instrument or DFT measurement ID")
    seen.add(result)
    return result


def _validate_grid(spectra: Mapping[str, Mapping[float, float]], expected_slots: set[str]) -> list[float]:
    if set(spectra) != expected_slots:
        _fail("ROSTER", "spectra-long.csv", "does not cover every immutable measurement slot")
    first = next(iter(spectra.values()))
    grid = sorted(first)
    if len(grid) < 3 or grid[0] < 360.0 or grid[-1] > 830.0 or any(right <= left for left, right in zip(grid, grid[1:])):
        _fail("WAVELENGTH_GRID", "spectra-long.csv", "must use at least three increasing wavelengths within 360..830 nm")
    step = grid[1] - grid[0]
    if not all(math.isclose(right - left, step, rel_tol=0.0, abs_tol=1e-9) for left, right in zip(grid, grid[1:])):
        _fail("WAVELENGTH_GRID", "spectra-long.csv", "must use a uniformly spaced wavelength grid")
    for slot, values in spectra.items():
        if sorted(values) != grid:
            _fail("WAVELENGTH_GRID", slot, "does not use the common wavelength grid")
    return grid


def _atomic_json(root: Path, relative: str, value: Mapping[str, Any]) -> str:
    target = root.joinpath(*PurePosixPath(relative).parts)
    parent = target.parent
    published_json = False
    try:
        _safe_directory_chain(parent, code="OUTPUT_PATH", path="output_relative_path", create=True)
        _assert_new_regular_file(target, code="OUTPUT_PATH")
        sidecar = target.with_name(f"{target.name}.sha256")
        _assert_new_regular_file(sidecar, code="OUTPUT_PATH")
        suffix = uuid.uuid4().hex
        temporary = parent / f".{target.name}.{suffix}.tmp"
        temporary_sidecar = parent / f".{target.name}.{suffix}.sha256.tmp"
        raw = canonical_json_bytes(value) + b"\n"
        digest = sha256_bytes(raw)
        temporary.write_bytes(raw)
        temporary_sidecar.write_text(f"{digest}  {target.name}\n", encoding="ascii")
        _safe_directory_chain(parent, code="OUTPUT_PATH", path="output_relative_path", create=False)
        _assert_published_regular_file(temporary, code="OUTPUT_PATH")
        _assert_published_regular_file(temporary_sidecar, code="OUTPUT_PATH")
        temporary.replace(target)
        published_json = True
        _safe_directory_chain(parent, code="OUTPUT_PATH", path="output_relative_path", create=False)
        _assert_published_regular_file(target, code="OUTPUT_PATH")
        temporary_sidecar.replace(sidecar)
        _safe_directory_chain(parent, code="OUTPUT_PATH", path="output_relative_path", create=False)
        _assert_published_regular_file(target, code="OUTPUT_PATH")
        _assert_published_regular_file(sidecar, code="OUTPUT_PATH")
        return digest
    except OpenMeasurementPackError:
        raise
    except OSError as error:
        if published_json:
            target.unlink(missing_ok=True)
            target.with_name(f"{target.name}.sha256").unlink(missing_ok=True)
        _fail("OUTPUT_WRITE", relative, str(error))
    finally:
        for item in (locals().get("temporary"), locals().get("temporary_sidecar")):
            if isinstance(item, Path) and item.exists():
                item.unlink(missing_ok=True)


def _output_relative_path(value: object) -> str:
    try:
        relative = _safe_relative(value, "output_relative_path")
    except OpenMeasurementPackError as error:
        _fail("OUTPUT_PATH", "output_relative_path", error.message)
    if not relative.endswith(".json"):
        _fail("OUTPUT_PATH", "output_relative_path", "must name a .json output file")
    return relative


def assemble_open_measurement_input(
    *,
    acquisition_receipt_path: Path | str,
    shared_root: Path | str,
    open_root: Path | str,
    pack_root: Path | str,
    operator_input_dir: Path | str,
    measurement_root: Path | str,
    output_relative_path: str,
) -> dict[str, object]:
    """Assemble completed neutral operator files into the existing admission v1 JSON."""

    context = _open_context(acquisition_receipt_path=acquisition_receipt_path, shared_root=shared_root, open_root=open_root)
    pack = _verify_pack(_root(pack_root, "pack_root"), context)
    operator = _root(operator_input_dir, "operator_input_dir")
    _assert_tree(operator, expected_files=set(_COMPLETED_NAMES), expected_dirs=set(), code="MISSING_OPERATOR_FILE")
    measurement = _root(measurement_root, "measurement_root")
    relative_output = _output_relative_path(output_relative_path)

    profile = _read_json(operator / "measurement-profile.json", operator, "CSV_SCHEMA")
    if not isinstance(profile, Mapping) or set(profile) != {"schema_version", "measurement_session_id", "instrument_id", "fixture_protocol_id", "instrument_calibration_evidence_relative_path", "instrument_run_log_evidence_relative_path"} or profile.get("schema_version") != PROFILE_SCHEMA:
        _fail("CSV_SCHEMA", "measurement-profile.json", "must match the instrument-neutral profile schema")
    _reject_nested_values(profile)
    evidence_paths: set[str] = set()
    evidence_digests: set[str] = set()
    calibration_evidence = _evidence(profile["instrument_calibration_evidence_relative_path"], measurement, "measurement-profile.instrument_calibration_evidence_relative_path", evidence_paths, evidence_digests)
    run_log_evidence = _evidence(profile["instrument_run_log_evidence_relative_path"], measurement, "measurement-profile.instrument_run_log_evidence_relative_path", evidence_paths, evidence_digests)

    backings = _read_csv(
        operator / "backings.csv",
        operator,
        ("backing", "backing_id", "lot_id"),
        max_rows=2,
    )
    backing_rows = _check_expected(backings, expected={(item,) for item in _BACKINGS}, keys=("backing",), path="backings.csv")
    normalized_backings = {
        backing: {"backing_id": _reject_text(backing_rows[(backing,)]["backing_id"], f"backings.{backing}.backing_id"), "lot_id": _reject_text(backing_rows[(backing,)]["lot_id"], f"backings.{backing}.lot_id"), "bare_measurements": []}
        for backing in _BACKINGS
    }

    _roster, bare_slots, dft_slots, coated_slots = _slots(context)
    ids: set[str] = set()
    bare_rows = _check_expected(
        _read_csv(operator / "bare-readings.csv", operator, ("backing", "reposition_id", "instrument_measurement_id", "measured_at_local", "raw_spectrum_evidence_relative_path"), max_rows=6),
        expected={(item["backing"], item["reposition_id"]) for item in bare_slots},
        keys=("backing", "reposition_id"),
        path="bare-readings.csv",
    )
    bare_by_slot: dict[str, dict[str, object]] = {}
    for item in bare_slots:
        row = bare_rows[(item["backing"], item["reposition_id"])]
        bare_by_slot[item["measurement_slot_id"]] = {
            "instrument_measurement_id": _measurement_id(row["instrument_measurement_id"], f"bare.{item['measurement_slot_id']}.instrument_measurement_id", ids),
            "measured_at_local": _timestamp(row["measured_at_local"], f"bare.{item['measurement_slot_id']}.measured_at_local"),
            "reposition_id": item["reposition_id"],
            "raw_spectrum_evidence": _evidence(row["raw_spectrum_evidence_relative_path"], measurement, f"bare.{item['measurement_slot_id']}.evidence", evidence_paths, evidence_digests),
        }

    dft_rows = _check_expected(
        _read_csv(operator / "dft-readings.csv", operator, ("card_id", "backing", "dft_measurement_id", "measured_at_local", "dft_evidence_relative_path", "dft_points_um"), max_rows=72),
        expected={(item["card_id"], item["backing"]) for item in dft_slots},
        keys=("card_id", "backing"),
        path="dft-readings.csv",
    )
    dft_by_card: dict[tuple[str, str], dict[str, object]] = {}
    for item in dft_slots:
        row = dft_rows[(item["card_id"], item["backing"])]
        dft_by_card[(item["card_id"], item["backing"])] = {
            "dft_measurement_id": _measurement_id(row["dft_measurement_id"], f"dft.{item['card_id']}.{item['backing']}.id", ids),
            "measured_at_local": _timestamp(row["measured_at_local"], f"dft.{item['card_id']}.{item['backing']}.timestamp"),
            "dft_points_um": _positive_points(row["dft_points_um"], f"dft.{item['card_id']}.{item['backing']}.points"),
            "dft_evidence": _evidence(row["dft_evidence_relative_path"], measurement, f"dft.{item['card_id']}.{item['backing']}.evidence", evidence_paths, evidence_digests),
        }
    # The transport deliberately has no caller-supplied DFT mean field.  Compute
    # means here solely to fail before publication when receipt-band ordering is
    # impossible; admission records the same arithmetic mean in its dataset.
    cards_by_family: dict[str, list[Mapping[str, Any]]] = {}
    for card in context["card_skeleton"]:
        cards_by_family.setdefault(card["formula_family_id"], []).append(card)
    for family_cards in cards_by_family.values():
        bands = ("DFT-L", "DFT-H") if family_cards[0]["split"] == "train" else ("DFT-L", "DFT-M", "DFT-H")
        for backing in _BACKINGS:
            means = []
            for band in bands:
                card = next(item for item in family_cards if item["dft_band"] == band)
                points = dft_by_card[(card["card_id"], backing)]["dft_points_um"]
                assert isinstance(points, list)  # Internal normalized invariant.
                means.append(math.fsum(points) / len(points))
            if any(right <= left for left, right in zip(means, means[1:])):
                _fail("DFT_VALUE", f"dft.{family_cards[0]['formula_family_id']}.{backing}", "does not preserve receipt-derived DFT-band ordering")

    coated_rows = _check_expected(
        _read_csv(operator / "coated-readings.csv", operator, ("card_id", "backing", "reposition_id", "instrument_measurement_id", "position_note", "orientation", "measured_at_local", "raw_spectrum_evidence_relative_path", "surface_status", "model_applicability_status"), max_rows=216),
        expected={(item["card_id"], item["backing"], item["reposition_id"]) for item in coated_slots},
        keys=("card_id", "backing", "reposition_id"),
        path="coated-readings.csv",
    )
    coated_by_slot: dict[str, dict[str, object]] = {}
    for item in coated_slots:
        row = coated_rows[(item["card_id"], item["backing"], item["reposition_id"])]
        if row["surface_status"] != "accepted_uniform_dry_film" or row["model_applicability_status"] != "accepted_for_km_diagnostic":
            _fail("ROSTER", f"coated.{item['measurement_slot_id']}", "must retain the two accepted physical status values")
        coated_by_slot[item["measurement_slot_id"]] = {
            "card_id": item["card_id"], "backing": item["backing"], "reposition_id": item["reposition_id"],
            "instrument_measurement_id": _measurement_id(row["instrument_measurement_id"], f"coated.{item['measurement_slot_id']}.id", ids),
            "position_note": _reject_text(row["position_note"], f"coated.{item['measurement_slot_id']}.position_note"),
            "orientation": _reject_text(row["orientation"], f"coated.{item['measurement_slot_id']}.orientation"),
            "measured_at_local": _timestamp(row["measured_at_local"], f"coated.{item['measurement_slot_id']}.timestamp"),
            "raw_spectrum_evidence": _evidence(row["raw_spectrum_evidence_relative_path"], measurement, f"coated.{item['measurement_slot_id']}.evidence", evidence_paths, evidence_digests),
            "surface_status": row["surface_status"], "model_applicability_status": row["model_applicability_status"],
            "backing_id": normalized_backings[item["backing"]]["backing_id"], "backing_lot_id": normalized_backings[item["backing"]]["lot_id"],
        }

    spectra: dict[str, dict[float, float]] = {}
    slots = {item["measurement_slot_id"] for item in [*bare_slots, *coated_slots]}
    for index, row in enumerate(
        _read_csv(
            operator / "spectra-long.csv",
            operator,
            ("measurement_slot_id", "wavelength_nm", "reflectance"),
            max_rows=_MAX_SPECTRA_ROWS,
        )
    ):
        slot = _reject_text(row["measurement_slot_id"], f"spectra[{index}].measurement_slot_id")
        if slot not in slots:
            _fail("ROSTER", f"spectra[{index}]", "uses an unknown measurement slot")
        try:
            wavelength = float(row["wavelength_nm"])
            reflectance = float(row["reflectance"])
        except ValueError:
            _fail("WAVELENGTH_GRID", f"spectra[{index}]", "must contain finite numeric wavelength and reflectance")
        if not math.isfinite(wavelength):
            _fail("WAVELENGTH_GRID", f"spectra[{index}].wavelength_nm", "must be finite")
        if not math.isfinite(reflectance) or not 0.0 <= reflectance <= 1.0:
            _fail("REFLECTANCE", f"spectra[{index}].reflectance", "must be a finite fraction in [0, 1]")
        values = spectra.setdefault(slot, {})
        if wavelength in values:
            _fail("DUPLICATE_ID", f"spectra[{index}]", "duplicates a measurement-slot/wavelength row")
        values[wavelength] = reflectance
    grid = _validate_grid(spectra, slots)
    for item in bare_slots:
        record = bare_by_slot[item["measurement_slot_id"]]
        record["reflectance"] = [spectra[item["measurement_slot_id"]][wavelength] for wavelength in grid]
        normalized_backings[item["backing"]]["bare_measurements"].append(record)
    cards = []
    for card in context["card_skeleton"]:
        card_id = card["card_id"]
        cards.append({"card_id": card_id, "dft_by_backing": {backing: dft_by_card[(card_id, backing)] for backing in _BACKINGS}})
    readings = []
    for item in coated_slots:
        record = coated_by_slot[item["measurement_slot_id"]]
        record["reflectance"] = [spectra[item["measurement_slot_id"]][wavelength] for wavelength in grid]
        readings.append(record)
    admission_input = {
        "schema_version": INPUT_SCHEMA,
        "measurement_session_id": _reject_text(profile["measurement_session_id"], "measurement-profile.measurement_session_id"),
        "wavelength_nm": grid,
        "locked_conditions": {
            "instrument_id": _reject_text(profile["instrument_id"], "measurement-profile.instrument_id"),
            "fixture_protocol_id": _reject_text(profile["fixture_protocol_id"], "measurement-profile.fixture_protocol_id"),
            "instrument_calibration_evidence": calibration_evidence,
            "instrument_run_log_evidence": run_log_evidence,
        },
        "backings": normalized_backings,
        "cards": cards,
        "readings": readings,
    }
    digest = _atomic_json(measurement, relative_output, admission_input)
    return {"status": "open_measurement_input_assembled", "schema_version": INPUT_SCHEMA, "admission_input_sha256": digest, "cards": 36, "readings": 216, "bare_readings": 6, "spectra_identities": 222, "output_relative_path": relative_output, "production_pass": False, **_permissions()}
