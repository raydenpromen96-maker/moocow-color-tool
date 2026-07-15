"""Immutable whole-file bindings for physical container-label evidence."""

from __future__ import annotations

import datetime as dt
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping, Sequence

from .errors import CalibrationError, DatasetValidationError
from .hashing import read_regular_file_snapshot, sha256_bytes


class EvidenceValidationError(CalibrationError):
    """A machine-readable physical-evidence validation failure."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"[{code}] {path}: {message}")


def _fail(code: str, path: str, message: str) -> None:
    raise EvidenceValidationError(code, path, message)


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("LABEL_TEXT", path, "must be a non-empty string")
    text = value.strip()
    if any(marker in text.casefold() for marker in ("required", "template", "placeholder", "not_yet", "synthetic")):
        _fail("LABEL_PLACEHOLDER", path, "must be a real physical-label value, not a placeholder")
    return text


def _timestamp(value: object, path: str) -> str:
    text = _text(value, path)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail("LABEL_TIMESTAMP", path, "must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("LABEL_TIMESTAMP_TIMEZONE", path, "must include a timezone offset")
    return text


def _relative_path(value: object, path: str) -> str:
    text = _text(value, path)
    windows_path = PureWindowsPath(text)
    posix_path = PurePosixPath(text)
    parts = text.split("/")
    if (
        "\\" in text
        or "\x00" in text
        or any(character in '<>:"|?*' for character in text)
        or windows_path.is_absolute()
        or windows_path.drive
        or posix_path.is_absolute()
        or any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)
    ):
        _fail("LABEL_PATH", path, "must be a portable relative path without traversal")
    return text


def _evidence_root(value: Path | str) -> Path:
    root = Path(value).absolute()
    try:
        root_stat = root.lstat()
    except OSError as error:
        _fail("REGISTRY_EVIDENCE_ROOT", str(root), str(error))
    if not root.is_dir() or root.is_symlink() or bool(getattr(root_stat, "st_file_attributes", 0) & 0x400):
        _fail("REGISTRY_EVIDENCE_ROOT", str(root), "must be an existing non-link directory")
    return root


def bind_physical_label_evidence(
    components: Sequence[Mapping[str, Any]], *, registry_evidence_root: Path | str
) -> list[dict[str, Any]]:
    """Read and bind every required whole-file physical container label.

    File bytes are acquired through the foundation single-open snapshot helper;
    this module never performs a separate hash-then-reopen sequence.
    """
    root = _evidence_root(registry_evidence_root)
    bindings: list[dict[str, Any]] = []
    verification_ids: dict[str, int] = {}
    relative_paths: dict[str, int] = {}
    file_sha256_values: dict[str, int] = {}
    for index, component in enumerate(components):
        prefix = f"registry.components[{index}]"
        component_id = _text(component.get("component_id"), f"{prefix}.component_id")
        if component.get("lot_verification_status") != "verified_physical_label":
            _fail(
                "REGISTRY_LOT_VERIFICATION",
                f"{prefix}.lot_verification_status",
                "must be verified_physical_label",
            )
        verification_id = _text(
            component.get("physical_label_verification_id"),
            f"{prefix}.physical_label_verification_id",
        )
        previous_index = verification_ids.get(verification_id)
        if previous_index is not None:
            _fail(
                "LABEL_VERIFICATION_ID_REUSE",
                f"{prefix}.physical_label_verification_id",
                f"must be unique across components; already bound to registry.components[{previous_index}]",
            )
        verification_ids[verification_id] = index
        verified_at = _timestamp(
            component.get("physical_label_verified_at"),
            f"{prefix}.physical_label_verified_at",
        )
        locator = component.get("physical_label_evidence")
        if not isinstance(locator, Mapping) or set(locator) != {"relative_path", "record_locator"}:
            _fail("LABEL_LOCATOR", f"{prefix}.physical_label_evidence", "must contain only relative_path and record_locator")
        relative_path = _relative_path(
            locator.get("relative_path"), f"{prefix}.physical_label_evidence.relative_path"
        )
        previous_index = relative_paths.get(relative_path)
        if previous_index is not None:
            _fail(
                "LABEL_EVIDENCE_REUSE",
                f"{prefix}.physical_label_evidence.relative_path",
                f"must bind distinct whole-file evidence; already bound to registry.components[{previous_index}]",
            )
        relative_paths[relative_path] = index
        record_locator = locator.get("record_locator")
        if not isinstance(record_locator, Mapping) or record_locator != {"kind": "whole_file"}:
            _fail(
                "LABEL_LOCATOR",
                f"{prefix}.physical_label_evidence.record_locator",
                "must be exactly a whole_file locator",
            )
        candidate = root.joinpath(*PurePosixPath(relative_path).parts)
        try:
            raw_bytes, file_sha256 = read_regular_file_snapshot(candidate, trusted_root=root)
        except DatasetValidationError as error:
            _fail("LABEL_FILE", f"{prefix}.physical_label_evidence.relative_path", str(error))
        previous_index = file_sha256_values.get(file_sha256)
        if previous_index is not None:
            _fail(
                "LABEL_EVIDENCE_REUSE",
                f"{prefix}.physical_label_evidence.relative_path",
                f"must bind distinct whole-file evidence; already bound to registry.components[{previous_index}]",
            )
        file_sha256_values[file_sha256] = index
        record_sha256 = sha256_bytes(raw_bytes)
        bindings.append(
            {
                "component_id": component_id,
                "physical_lot_id": _text(component.get("batch_id"), f"{prefix}.batch_id"),
                "physical_label_verification_id": verification_id,
                "physical_label_verified_at": verified_at,
                "physical_label_evidence": {
                    "relative_path": relative_path,
                    "record_locator": {
                        "kind": "whole_file",
                        "byte_offset": 0,
                        "byte_length": len(raw_bytes),
                        "record_sha256": record_sha256,
                    },
                },
                "size_bytes": len(raw_bytes),
                "file_sha256": file_sha256,
            }
        )
    return bindings
