"""Immutable-by-convention domain records and their JSON codecs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .enums import BagState, FlightState


def _clean(raw: Any) -> Any:
    """Normalise nested containers so codecs round-trip deterministically."""

    if isinstance(raw, dict):
        return {str(key): _clean(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_clean(item) for item in raw]
    return raw


@dataclass(frozen=True)
class Flight:
    id: str
    code: str
    terminal: str = "T1"
    carousel: str = ""
    chute: str = ""
    state: FlightState = FlightState.OPEN
    batch: int = 0
    schedule_generation: int = 1
    cutoff_at: str = ""
    closed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "terminal": self.terminal,
            "carousel": self.carousel,
            "chute": self.chute,
            "state": self.state.value,
            "batch": self.batch,
            "schedule_generation": self.schedule_generation,
            "cutoff_at": self.cutoff_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Flight":
        return cls(
            id=str(raw["id"]),
            code=str(raw.get("code", "")),
            terminal=str(raw.get("terminal", "T1")),
            carousel=str(raw.get("carousel", "")),
            chute=str(raw.get("chute", "")),
            state=FlightState(raw.get("state", FlightState.OPEN.value)),
            batch=int(raw.get("batch", 0)),
            schedule_generation=int(raw.get("schedule_generation", 1)),
            cutoff_at=str(raw.get("cutoff_at", "")),
            closed_at=str(raw.get("closed_at", "")),
        )

    def evolved(self, **changes: Any) -> "Flight":
        return replace(self, **changes)


@dataclass(frozen=True)
class Bag:
    id: str
    barcode: str
    sequence: int
    epoch: int = 0
    flight_id: str = ""
    chute_id: str = ""
    state: BagState = BagState.CHECKED_IN
    home_terminal: str = ""
    terminal: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "barcode": self.barcode,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "state": self.state.value,
            "home_terminal": self.home_terminal,
            "terminal": self.terminal,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Bag":
        home = str(raw.get("home_terminal", ""))
        current = str(raw.get("terminal", ""))
        return cls(
            id=str(raw["id"]),
            barcode=str(raw.get("barcode", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            state=BagState(raw.get("state", BagState.CHECKED_IN.value)),
            home_terminal=home,
            terminal=current or home,
        )

    def evolved(self, **changes: Any) -> "Bag":
        return replace(self, **changes)


@dataclass(frozen=True)
class Reading:
    barcode: str
    bag_id: str
    sequence: int
    epoch: int
    committed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "bag_id": self.bag_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reading":
        return cls(
            barcode=str(raw.get("barcode", "")),
            bag_id=str(raw.get("bag_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            committed=bool(raw.get("committed", False)),
        )


@dataclass(frozen=True)
class ChuteAssignment:
    flight_id: str
    chute_id: str
    generation: int = 1
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "generation": self.generation,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChuteAssignment":
        return cls(
            flight_id=str(raw["flight_id"]),
            chute_id=str(raw.get("chute_id", "")),
            generation=int(raw.get("generation", 1)),
            updated_at=str(raw.get("updated_at", "")),
        )


@dataclass(frozen=True)
class SortRecord:
    bag_id: str
    flight_id: str
    chute_id: str
    sequence: int
    epoch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SortRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class TransferCommand:
    id: str
    flight_id: str
    carousel: str
    batch: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flight_id": self.flight_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TransferCommand":
        return cls(
            id=str(raw["id"]),
            flight_id=str(raw.get("flight_id", "")),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            created_at=str(raw.get("created_at", "")),
        )


@dataclass(frozen=True)
class LoadRecord:
    bag_id: str
    carousel: str
    batch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class Sequence:
    epoch: int = 0
    next_value: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "next": self.next_value}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Sequence":
        return cls(epoch=int(raw.get("epoch", 0)), next_value=int(raw.get("next", 0)))


@dataclass(frozen=True)
class OverflowBatch:
    """One shipment of overflow bags from the home terminal to a backup.

    The status moves ``outbound`` -> ``returned`` (every bag came home and the
    accounts were settled) or ``outbound`` -> ``rolled_back`` (an interrupted
    outbound restored ownership without ever touching the backup accounts).
    ``bag_ids`` is frozen at creation; an account that ever lists a bag not in
    its batch, or a bag whose terminal disagrees with its batch status, fails
    reconciliation.
    """

    id: str
    backup_terminal: str
    home_terminal: str
    bag_ids: tuple[str, ...]
    status: str = "outbound"
    reason: str = ""
    trigger: str = ""
    created_at: str = ""
    finished_at: str = ""
    pre_evidence: str = ""
    post_evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "backup_terminal": self.backup_terminal,
            "home_terminal": self.home_terminal,
            "bag_ids": list(self.bag_ids),
            "status": self.status,
            "reason": self.reason,
            "trigger": self.trigger,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "pre_evidence": self.pre_evidence,
            "post_evidence": self.post_evidence,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OverflowBatch":
        return cls(
            id=str(raw["id"]),
            backup_terminal=str(raw.get("backup_terminal", "")),
            home_terminal=str(raw.get("home_terminal", "")),
            bag_ids=tuple(str(item) for item in raw.get("bag_ids", [])),
            status=str(raw.get("status", "outbound")),
            reason=str(raw.get("reason", "")),
            trigger=str(raw.get("trigger", "")),
            created_at=str(raw.get("created_at", "")),
            finished_at=str(raw.get("finished_at", "")),
            pre_evidence=str(raw.get("pre_evidence", "")),
            post_evidence=str(raw.get("post_evidence", "")),
        )

    def evolved(self, **changes: Any) -> "OverflowBatch":
        return replace(self, **changes)


@dataclass(frozen=True)
class ExternalRecord:
    """One bag's entry in the external (backup terminal) ledger.

    The snapshot digests are the evidence taken when the bag left and when it
    came home, so the backup can prove it accepted exactly this bag and handed
    exactly this bag back.
    """

    bag_id: str
    batch_id: str
    backup_terminal: str
    home_terminal: str
    state: str
    out_at: str
    back_at: str = ""
    out_evidence: str = ""
    back_evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "batch_id": self.batch_id,
            "backup_terminal": self.backup_terminal,
            "home_terminal": self.home_terminal,
            "state": self.state,
            "out_at": self.out_at,
            "back_at": self.back_at,
            "out_evidence": self.out_evidence,
            "back_evidence": self.back_evidence,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExternalRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            batch_id=str(raw.get("batch_id", "")),
            backup_terminal=str(raw.get("backup_terminal", "")),
            home_terminal=str(raw.get("home_terminal", "")),
            state=str(raw.get("state", "outbound")),
            out_at=str(raw.get("out_at", "")),
            back_at=str(raw.get("back_at", "")),
            out_evidence=str(raw.get("out_evidence", "")),
            back_evidence=str(raw.get("back_evidence", "")),
        )

    def evolved(self, **changes: Any) -> "ExternalRecord":
        return replace(self, **changes)


@dataclass(frozen=True)
class ArrivalSample:
    """One observed arrival count feeding the arrival predictor."""

    at: str
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at, "count": int(self.count)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ArrivalSample":
        return cls(at=str(raw.get("at", "")), count=int(raw.get("count", 0)))


@dataclass(frozen=True)
class EvidenceAnchor:
    """A reference to a snapshot file, kept in the journal that snapshotted it."""

    phase: str
    ref: str
    digest: str
    watermark: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "ref": self.ref,
            "digest": self.digest,
            "watermark": self.watermark,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvidenceAnchor":
        return cls(
            phase=str(raw.get("phase", "")),
            ref=str(raw.get("ref", "")),
            digest=str(raw.get("digest", "")),
            watermark=int(raw.get("watermark", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    kind: str
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)
    prev_digest: str = ""
    digest: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "subject": self.subject,
            "detail": _clean(self.detail),
            "prev_digest": self.prev_digest,
            "digest": self.digest,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        return cls(
            sequence=int(raw.get("sequence", 0)),
            kind=str(raw.get("kind", "")),
            subject=str(raw.get("subject", "")),
            detail=_clean(raw.get("detail", {})),
            prev_digest=str(raw.get("prev_digest", "")),
            digest=str(raw.get("digest", "")),
            at=str(raw.get("at", "")),
        )
