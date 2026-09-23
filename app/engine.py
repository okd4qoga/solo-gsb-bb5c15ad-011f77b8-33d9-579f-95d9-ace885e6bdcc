"""Half-open interval / sweep-line rule engine.

Each sensor channel is a sequence of timestamped samples.  A sample's value is
held from its own timestamp up to (but excluding) the timestamp of the next
sample on the same channel — i.e. every piece of the derived truth model is a
half-open interval ``[start, end)``.

Touching half-open intervals (``[a, b)`` followed by ``[b, c)``) are
consecutive: a sweep-line that emits a segment only when the truth value
actually changes merges them automatically, never emitting a zero-length
"gap" or "reset" segment at ``b``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

OPERATORS = {">", ">=", "<", "<=", "==", "!="}
LOGIC = {"AND", "OR"}
_EPS = 1e-7  # seconds of slack for duration comparisons (float boundaries)


def _normalize_epoch_number(n: float) -> float:
    # Heuristic for epoch milliseconds: numeric values past ~year 10000 in
    # seconds are treated as milliseconds (applied to numbers and numeric
    # strings so that JSONL payloads and query params behave identically).
    return n / 1000.0 if n > 1e11 else n


def to_epoch(value: Any) -> float:
    """Convert an epoch number (s/ms) / ISO-8601 string to epoch seconds (float)."""
    if value is None:
        raise ValueError("timestamp is null")
    if isinstance(value, bool):
        raise ValueError(f"unsupported timestamp: {value!r}")
    if isinstance(value, (int, float)):
        return _normalize_epoch_number(float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty timestamp")
        # Numeric strings are epoch seconds/milliseconds, same as JSON numbers.
        try:
            return _normalize_epoch_number(float(text))
        except ValueError:
            pass
        normalized = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            raise ValueError(f"unparsable timestamp: {value!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError(f"unsupported timestamp type: {type(value).__name__}")


def to_number(value: Any) -> float | None:
    """Best-effort numeric conversion; returns None when not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def compare(value: float, op: str, threshold: float) -> bool:
    if op == ">":
        return value > threshold
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    if op == "!=":
        return value != threshold
    raise ValueError(f"unknown operator: {op}")


@dataclass(frozen=True)
class Interval:
    """Half-open interval [start, end)."""

    start: float
    end: float

    def nonempty(self) -> bool:
        return self.end > self.start + _EPS


def union(intervals: Iterable[Interval]) -> list[Interval]:
    """Merge overlapping or *touching* intervals.

    ``[a,b)`` and ``[b,c)`` touch and are treated as one ``[a,c)``.
    """
    ordered = sorted(
        (iv for iv in intervals if iv.end > iv.start), key=lambda iv: iv.start
    )
    merged: list[Interval] = []
    for iv in ordered:
        if merged and iv.start <= merged[-1].end:
            if iv.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, iv.end)
        else:
            merged.append(iv)
    return merged


def cluster_intervals(intervals: Iterable[Interval]) -> list[list[int]]:
    """Cluster interval indices by overlap-or-touch, *transitively*.

    Half-open intervals that overlap (``[a,b)`` vs ``[a',b')`` with
    ``a' < b``) or merely touch (``[a,b)`` followed by ``[b,c)``) belong to
    the same cluster, and the relation chains: A~B and B~C implies
    ``{A, B, C}`` even when A and C never intersect.

    Returns clusters as lists of original input indices.  Empty/zero-length
    inputs are dropped; clusters are ordered by their earliest ``start``.
    """
    ordered = sorted(
        ((i, iv) for i, iv in enumerate(intervals) if iv.end > iv.start),
        key=lambda item: (item[1].start, item[1].end),
    )
    clusters: list[list[int]] = []
    current: list[int] = []
    cur_end: float = -math.inf
    for index, iv in ordered:
        if current and iv.start <= cur_end + _EPS:
            current.append(index)
            if iv.end > cur_end:
                cur_end = iv.end
        else:
            if current:
                clusters.append(current)
            current = [index]
            cur_end = iv.end
    if current:
        clusters.append(current)
    return clusters


def intersect(intervals: Iterable[Interval]) -> list[Interval]:
    """Intersection of interval lists, each pre-merged (touching = continuous).

    Boundary events at the same timestamp are applied as one atomic group so
    that ``[a,b)`` ending and ``[b,c)`` starting on two different operands do
    not spuriously break the result at ``b``.
    """
    lists = [ivs for ivs in (sorted(set(ivs), key=lambda iv: iv.start) for ivs in intervals) if ivs]
    if not lists:
        return []

    groups: dict[float, int] = defaultdict(int)
    n = len(lists)
    for ivs in lists:
        for iv in ivs:
            groups[iv.start] += 1
            groups[iv.end] -= 1

    # A segment is emitted only when the truth value actually changes into or
    # out of "all n active"; boundaries whose deltas cancel at the same instant
    # (one interval ends there, another starts there) leave truth unchanged and
    # must not split the result.
    result: list[Interval] = []
    active = 0
    seg_start: float | None = None
    for t in sorted(groups):
        before = active
        active += groups[t]
        if before < n <= active:
            seg_start = t
        elif before >= n > active:
            if seg_start is not None and t > seg_start:
                result.append(Interval(seg_start, t))
            seg_start = None
    return result


@dataclass
class Condition:
    device: str
    metric: str
    op: str
    threshold: float

    def __post_init__(self) -> None:
        if not str(self.device).strip():
            raise ValueError("condition device is required")
        if not str(self.metric).strip():
            raise ValueError("condition metric is required")
        if self.op not in OPERATORS:
            raise ValueError(f"operator must be one of {sorted(OPERATORS)}")
        self.threshold = float(self.threshold)

    @property
    def channel(self) -> tuple[str, str]:
        return (str(self.device), str(self.metric))

    @classmethod
    def from_dict(cls, raw: dict) -> "Condition":
        return cls(
            device=raw["device"],
            metric=raw["metric"],
            op=raw["op"],
            threshold=raw["threshold"],
        )

    def to_dict(self) -> dict:
        return {"device": self.device, "metric": self.metric, "op": self.op, "threshold": self.threshold}


@dataclass
class Event:
    id: int
    device: str
    metric: str
    ts: float
    value: float


@dataclass
class EvidenceItem:
    """Samples that witness one condition inside a fault interval."""

    condition: Condition
    events: list[Event] = field(default_factory=list)
    # maximal [start,end) sub-intervals in which this condition held
    held_intervals: list[Interval] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "condition": self.condition.to_dict(),
            "held_intervals": [[iv.start, iv.end] for iv in self.held_intervals],
            "events": [
                {"event_id": e.id, "ts": e.ts, "value": e.value} for e in self.events
            ],
        }


@dataclass
class FaultInterval:
    start: float
    end: float
    rule_id: int
    duration_required: float
    confirmed_at: float
    evidence: list[EvidenceItem]

    @property
    def length(self) -> float:
        return self.end - self.start


def channel_held_intervals(
    events: list[Event], condition: Condition, horizon: float
) -> tuple[list[Interval], list[Event]]:
    """Return ([start,end) intervals where the predicate held], witnessing events).

    A sample's value is held on ``[sample.ts, next_sample.ts)``; the last sample
    is held through ``horizon`` (the timestamp of the newest event in the whole
    dataset). Equal timestamps keep the last supplied value.
    """
    ordered: list[Event] = []
    for ev in sorted(events, key=lambda e: (e.ts, e.id)):
        if ordered and ordered[-1].ts == ev.ts:
            ordered[-1] = ev  # last write at identical timestamp
        else:
            ordered.append(ev)

    held: list[Interval] = []
    witnesses: list[Event] = []
    for i, ev in enumerate(ordered):
        seg_end = ordered[i + 1].ts if i + 1 < len(ordered) else horizon
        if seg_end <= ev.ts:
            continue
        if compare(ev.value, condition.op, condition.threshold):
            held.append(Interval(ev.ts, seg_end))
            witnesses.append(ev)
    return union(held), witnesses


def _merge_or(per_condition: list[list[Interval]]) -> list[Interval]:
    """Union via sweep-line; touching segments are continuous."""
    return union(iv for ivs in per_condition for iv in ivs)


def evaluate_rule(
    rule_id: int,
    logic: str,
    duration_required: float,
    conditions: list[Condition],
    events_by_channel: dict[tuple[str, str], list[Event]],
    horizon: float,
) -> list[FaultInterval]:
    """Evaluate one rule against indexed channel data.

    Returns the merged fault intervals whose length is >= ``duration_required``.
    """
    logic = logic.upper()
    if logic not in LOGIC:
        raise ValueError("logic must be AND or OR")
    duration_required = float(duration_required)
    if duration_required < 0:
        raise ValueError("duration must be non-negative")
    if not conditions:
        return []

    held_per: list[list[Interval]] = []
    witnesses_per: list[list[Event]] = []
    for cond in conditions:
        ivs, witnesses = channel_held_intervals(
            events_by_channel.get(cond.channel, []), cond, horizon
        )
        held_per.append(ivs)
        witnesses_per.append(witnesses)

    if logic == "OR":
        truth = _merge_or(held_per)
    else:
        # AND over channels that actually exist; a condition with no channel
        # data can never be satisfied.
        if any(cond.channel not in events_by_channel for cond in conditions):
            return []
        truth = intersect(held_per)

    faults: list[FaultInterval] = []
    for iv in truth:
        if iv.end - iv.start + _EPS >= duration_required:
            evidence: list[EvidenceItem] = []
            for cond, held, witnesses in zip(conditions, held_per, witnesses_per):
                inside = [
                    h
                    for h in held
                    if h.start < iv.end and h.end > iv.start
                ]
                inside_events = [
                    ev
                    for ev in witnesses
                    if iv.start <= ev.ts < iv.end
                ]
                evidence.append(
                    EvidenceItem(condition=cond, events=inside_events, held_intervals=inside)
                )
            faults.append(
                FaultInterval(
                    start=iv.start,
                    end=iv.end,
                    rule_id=rule_id,
                    duration_required=duration_required,
                    confirmed_at=iv.start + duration_required,
                    evidence=evidence,
                )
            )
    return faults
