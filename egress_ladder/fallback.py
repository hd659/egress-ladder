"""Fallback-order cost model: expected wait per new connection.

A consumer (e.g. an isolated browser bridge) tries candidates in order. Each
candidate i has success ratio p_i, latency ok_i when it works and cost fail_i
when it does not (a timeout costs the whole timeout, a refused port ~0).

    attempt_i  = p_i * ok_i + (1 - p_i) * fail_i
    E[wait]    = sum_i  attempt_i * prod_{j<i} (1 - p_j)

Optimal order (adjacent-swap argument): ascending attempt_i / p_i.
Candidates with p_i == 0 always go last; if they cost time, drop them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from .core import Graph, Health


@dataclass
class Leg:
    id: str
    p: float
    ok_s: float
    fail_s: float

    @property
    def attempt_s(self) -> float:
        return self.p * self.ok_s + (1 - self.p) * self.fail_s

    @property
    def rank(self) -> float:
        return math.inf if self.p <= 0 else self.attempt_s / self.p


@dataclass
class FallbackReport:
    consumer: str
    current: List[str]
    best: List[str]
    current_wait_s: float
    best_wait_s: float
    fail_all_current: float     # probability every candidate fails
    drop: List[str]             # never works but costs time
    timeout_hints: Dict[str, float]
    usable: int = 0             # candidates with p > 0

    @property
    def saved_s(self) -> float:
        return self.current_wait_s - self.best_wait_s


def leg_from_health(nid: str, h: Optional[Health], timeout_s: float) -> Leg:
    h = h or Health()
    if h.p is None:
        p = 1.0 if h.state == "up" else 0.0 if h.state == "down" else 0.5
    else:
        p = h.p
    ok = h.ok_s if h.ok_s is not None else 0.0
    fail = h.fail_s if h.fail_s is not None else timeout_s
    return Leg(nid, p, ok, fail)


def expected_wait(legs: List[Leg]) -> float:
    total, reach = 0.0, 1.0
    for leg in legs:
        total += reach * leg.attempt_s
        reach *= 1 - leg.p
    return total


def best_order(legs: List[Leg]) -> List[Leg]:
    return sorted(legs, key=lambda l: (l.rank, l.attempt_s, l.id))


def suggest_timeout(leg: Leg, current_s: float, slow_ms: Optional[float]) -> Optional[float]:
    """Fail-fast budget: 3x the observed good latency (min 1 s), or slow_ms."""
    if leg.p <= 0 or leg.ok_s <= 0:
        return None
    budget = max(1.0, 3 * leg.ok_s)
    if slow_ms:
        budget = max(budget, slow_ms / 1000.0)
    budget = math.ceil(budget * 2) / 2
    return budget if budget < current_s else None


def analyse(g: Graph, health: Dict[str, Health]) -> List[FallbackReport]:
    reports = []
    for consumer, order in g.fallbacks.items():
        legs = [leg_from_health(n, health.get(n), g.nodes[n].timeout_s) for n in order]
        ranked = best_order(legs)
        useful = [l for l in ranked if l.p > 0]
        drop = [l.id for l in ranked if l.p <= 0 and l.fail_s > 0.05]
        best = useful + [l for l in ranked if l.p <= 0 and l.id not in drop]
        reach = 1.0
        for l in legs:
            reach *= 1 - l.p
        hints = {}
        for l in legs:
            t = suggest_timeout(l, g.nodes[l.id].timeout_s, g.nodes[l.id].slow_ms)
            if t is not None:
                hints[l.id] = t
        reports.append(FallbackReport(
            consumer=consumer, current=list(order), best=[l.id for l in best],
            current_wait_s=expected_wait(legs), best_wait_s=expected_wait(best),
            fail_all_current=reach, drop=drop, timeout_hints=hints,
            usable=len(useful)))
    return reports
