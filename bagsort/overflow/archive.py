"""On-disk snapshots around every transfer and recall.

Each operation writes *before* and *after* pictures to disk
(``NNNNNN-{operation}-{phase}.json``) and links them with a sha256 chain: an
after-file carries the digest of its before-file, and the next before-file
carries the previous after-digest. Editing or deleting a snapshot in the
middle breaks every later link, which :func:`verify_archive` reports.

Snapshots are best-effort forensic evidence, never the system of record -- the
journal is. They are written *before* the journal session is opened (after the
"after" snapshot only once the mutation committed), so a snapshot file can only
ever describe a real committed state.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

GENESIS = "0" * 64


@dataclass(frozen=True)
class SnapshotFile:
    """One archived snapshot as read back from disk."""

    name: str
    sequence: int
    operation: str
    phase: str
    body: dict[str, Any]


def canonical_digest(body: dict[str, Any]) -> str:
    material = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class SnapshotArchive:
    """Writes and verifies the on-disk before/after snapshot chain."""

    def __init__(self, directory: str) -> None:
        self._dir = os.path.abspath(directory)

    @property
    def directory(self) -> str:
        return self._dir

    def ensure_dir(self) -> None:
        os.makedirs(self._dir, exist_ok=True)

    def list_snapshots(self) -> list[SnapshotFile]:
        if not os.path.isdir(self._dir):
            return []
        files: list[SnapshotFile] = []
        for name in sorted(os.listdir(self._dir)):
            if not name.endswith(".json"):
                continue
            parsed = parse_name(name)
            if parsed is None:
                continue
            with open(os.path.join(self._dir, name), "r", encoding="utf-8") as handle:
                try:
                    body = json.load(handle)
                except ValueError:
                    body = {}
            sequence, operation, phase = parsed
            files.append(
                SnapshotFile(
                    name=name,
                    sequence=sequence,
                    operation=operation,
                    phase=phase,
                    body=body if isinstance(body, dict) else {},
                )
            )
        return sorted(files, key=lambda item: (item.sequence, item.name))

    def write(
        self,
        operation: str,
        phase: str,
        picture: dict[str, Any],
        watermark: int,
        at: str,
        prev_digest: str,
    ) -> SnapshotFile:
        """Persist one snapshot; returns its metadata including its digest."""

        self.ensure_dir()
        sequence = self._next_sequence()
        body = {
            "sequence": sequence,
            "operation": operation,
            "phase": phase,
            "at": at,
            "watermark": watermark,
            "prev_digest": prev_digest,
            "state": picture,
        }
        digest = canonical_digest(body)
        body["digest"] = digest
        name = f"{sequence:06d}-{operation}-{phase}.json"
        path = os.path.join(self._dir, name)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)
        return SnapshotFile(
            name=name,
            sequence=sequence,
            operation=operation,
            phase=phase,
            body=body,
        )

    def last_digest(self) -> str:
        files = self.list_snapshots()
        if not files:
            return GENESIS
        return str(files[-1].body.get("digest", ""))

    def verify(self) -> dict[str, Any]:
        """Recompute every digest and every chain link."""

        files = self.list_snapshots()
        errors: list[str] = []
        previous = GENESIS
        last_sequence = 0
        for item in files:
            body = dict(item.body)
            stored = str(body.pop("digest", ""))
            if canonical_digest(body) != stored:
                errors.append(f"{item.name}: digest mismatch")
            if str(body.get("prev_digest", "")) != previous:
                errors.append(f"{item.name}: broken chain link")
            previous = stored
            last_sequence = item.sequence
        return {
            "ok": not errors,
            "errors": errors,
            "count": len(files),
            "last_sequence": last_sequence,
            "directory": self._dir,
        }

    def _next_sequence(self) -> int:
        files = self.list_snapshots()
        if not files:
            return 1
        return max(item.sequence for item in files) + 1


def parse_name(name: str) -> tuple[int, str, str] | None:
    """Parse ``NNNNNN-operation-phase.json`` into its parts."""

    stem = name.removesuffix(".json")
    parts = stem.split("-", 2)
    if len(parts) != 3 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1], parts[2]
