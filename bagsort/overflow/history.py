"""Overflow history, both as materialised records and as a time replay.

Two views are offered on purpose. :meth:`batches` reads the folded state and is
what operators look at day to day; :meth:`recompute` folds the journal commands
themselves up to a given instant and rebuilds every batch, record and balance
from scratch. If the two ever disagree, the journal replay is the one the
accounts must be settled from.
"""

from __future__ import annotations

from bagsort.model.entities import ExternalRecord, OverflowBatch
from bagsort.model.state import State, fold
from bagsort.store.store import Store


def _at_or_before(at: str, cutoff: str) -> bool:
    return not cutoff or at <= cutoff


class OverflowHistory:
    """Read side for outbound batches and external records."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def batches(self, status: str = "") -> list[OverflowBatch]:
        batches = list(self._store.state().overflow_batches)
        if status:
            batches = [batch for batch in batches if batch.status == status]
        return sorted(batches, key=lambda batch: batch.created_at + batch.id)

    def batch(self, batch_id: str) -> OverflowBatch | None:
        return next(
            (item for item in self._store.state().overflow_batches if item.id == batch_id),
            None,
        )

    def records(self, state_value: str = "") -> list[ExternalRecord]:
        records = list(self._store.state().external_records)
        if state_value:
            records = [record for record in records if record.state == state_value]
        return records

    def record_for(self, bag_id: str) -> ExternalRecord | None:
        return next(
            (
                record
                for record in self._store.state().external_records
                if record.bag_id == bag_id
            ),
            None,
        )

    def outbound_bag_ids(self) -> set[str]:
        return {
            record.bag_id
            for record in self._store.state().external_records
            if record.state == "outbound"
        }

    def recompute(self, cutoff: str = "") -> dict[str, object]:
        """Rebuild the overflow books from the journal as of ``cutoff``.

        Commands are filtered by their commit timestamp, then folded from an
        empty state exactly the way restart replay does. The returned
        ``matches_live`` flag compares that fold against the live materialised
        state (full fold when no cutoff is given), which is how a tampered or
        truncated journal surfaces during an audit.
        """

        entries = [
            (entry.kind, entry.payload)
            for entry in self._store.entries()
            if _at_or_before(entry.at, cutoff)
        ]
        rebuilt: State = fold(entries)
        batches = [batch.as_dict() for batch in rebuilt.overflow_batches]
        records = [record.as_dict() for record in rebuilt.external_records]
        result: dict[str, object] = {
            "cutoff": cutoff,
            "watermark": len(entries),
            "batches": batches,
            "records": records,
            "granted": dict(sorted(rebuilt.overflow_granted.items())),
            "outbound_count": sum(1 for record in records if record["state"] == "outbound"),
        }
        if not cutoff:
            live = self._store.state()
            result["matches_live"] = (
                rebuilt.overflow_batches == live.overflow_batches
                and rebuilt.external_records == live.external_records
                and replay_check(self._store)
            )
        return result


def replay_check(store: Store) -> bool:
    """Whole-journal replay agreeing with the materialised state."""

    from bagsort.store.replay import replay

    rebuilt = replay(store.entries())
    return rebuilt.as_dict() == store.state().as_dict()
