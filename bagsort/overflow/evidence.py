"""Snapshot evidence written to disk around every overflow movement.

A transfer, a rollback and a return each take two snapshots of the *whole*
materialised state: one immediately before the movement and one immediately
after. Each snapshot is its own checksummed file, so a later disagreement
between the two terminals can be resolved against an artifact neither side can
rewrite in place. The journal keeps an :class:`EvidenceAnchor` per file, which
is what lets a restart notice a snapshot that has gone missing or changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from bagsort.model.state import State

EVIDENCE_DIR = "overflow-evidence"


def digest_of(document: dict[str, Any]) -> str:
    material = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class EvidenceStore:
    """Writes and verifies immutable snapshot files next to the journal."""

    def __init__(self, store_path: str) -> None:
        parent = os.path.dirname(os.path.abspath(store_path)) or "."
        self._dir = os.path.join(parent, EVIDENCE_DIR)

    @property
    def directory(self) -> str:
        return self._dir

    def _path(self, ref: str) -> str:
        return os.path.join(self._dir, f"{ref}.json")

    def write(
        self,
        phase: str,
        watermark: int,
        at: str,
        state: State,
        detail: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Write one snapshot atomically; returns ``(ref, digest)``."""

        os.makedirs(self._dir, exist_ok=True)
        ref = f"{watermark:08d}-{phase}"
        document = {
            "ref": ref,
            "phase": phase,
            "at": at,
            "watermark": watermark,
            "detail": dict(detail or {}),
            "state": state.as_dict(),
        }
        digest = digest_of(document)
        document["digest"] = digest
        temporary = f"{self._path(ref)}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, self._path(ref))
        return ref, digest

    def read(self, ref: str) -> dict[str, Any]:
        with open(self._path(ref), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def verify_ref(self, ref: str, digest: str) -> bool:
        """True when the file exists and its checksum still matches."""

        try:
            document = self.read(ref)
        except (OSError, ValueError):
            return False
        stored = str(document.pop("digest", ""))
        return stored == digest and digest_of(document) == digest

    def verify_all(self, anchors: list) -> int:
        """Verify every journal anchor; returns how many were checked."""

        for anchor in anchors:
            if not self.verify_ref(anchor.ref, anchor.digest):
                raise FileNotFoundError(
                    f"overflow evidence {anchor.ref!r} is missing or altered"
                )
        return len(anchors)
