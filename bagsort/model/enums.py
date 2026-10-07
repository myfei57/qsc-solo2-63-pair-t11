"""Enumerations for the two state machines the plant tracks."""

from __future__ import annotations

from enum import Enum


class FlightState(str, Enum):
    """Lifecycle of one flight batch."""

    OPEN = "open"
    BOARDING = "boarding"
    CUTOFF = "cutoff"
    CLOSED = "closed"


class BagState(str, Enum):
    """Lifecycle of one piece of baggage."""

    CHECKED_IN = "checked_in"
    INJECTED = "injected"
    SORTED = "sorted"
    RECHECKED = "rechecked"
    LOADED = "loaded"
    DIVERTED = "diverted"

    @property
    def active(self) -> bool:
        """Whether the bag still occupies plant capacity."""

        return self in (
            BagState.CHECKED_IN,
            BagState.INJECTED,
            BagState.SORTED,
            BagState.RECHECKED,
        )

class Verdict(str, Enum):
    """Result of comparing an observed (epoch, sequence) pair with the high-water mark."""

    FRESH = "fresh"
    REPLAYED = "replayed"
    ROLLED_OVER = "rolled_over"


# Where a bag the overflow ledger tracks physically sits. ``home`` never moves
# (ownership stays with the home terminal); only this status changes.
# ``released`` is the terminal state: the bag has left the system, but its
# ownership row is retained so closed outbound records always reconcile.
AT_HOME = "at_home"
IN_FLIGHT = "in_flight"
HELD = "held"
RETURNED = "returned"
RELEASED = "released"

# Outbound record lifecycle.
PREPARED = "prepared"
ACCEPTED = "accepted"
PARTIAL_ACCEPT = "partial"
PARTIAL_RECALL = "partial_recall"
ROLLED_BACK = "rolled_back"
RECALLED = "recalled"
