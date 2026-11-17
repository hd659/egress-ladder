"""Egress dependency graph: bring-up waves, chicken-and-egg loop detection.

``needs`` = list of requirements (AND); each requirement = list of alternatives
(OR); ``"a"`` is shorthand for ``["a"]``.  Health: ``up`` counts as satisfied;
``down``/refused = not started (plannable); ``down`` with any other error while
all needs are up = broken (excluded); everything else is plannable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set


class ConfigError(ValueError):
    """Invalid egress graph config."""


@dataclass
class Health:
    state: str = "unknown"          # up | down | unknown
    error: str = ""                 # refused | timeout | rejected | ...
    p: Optional[float] = None       # success ratio over samples
    ok_s: Optional[float] = None    # median latency of successes
    fail_s: Optional[float] = None  # cost of one failed attempt


@dataclass
class Node:
    id: str
    label: str = ""
    needs: List[List[str]] = field(default_factory=list)
    probe: Optional[dict] = None
    timeout_s: float = 5.0
    slow_ms: Optional[float] = None
    slow_hint: str = ""
    start: str = ""
    manual: str = ""
    sample: Optional[dict] = None

    @property
    def name(self) -> str:
        return self.label or self.id


@dataclass
class Graph:
    nodes: Dict[str, Node]
    fallbacks: Dict[str, List[str]] = field(default_factory=dict)


def _norm_needs(raw: Iterable) -> List[List[str]]:
    out = []
    for req in raw or []:
        alts = [req] if isinstance(req, str) else list(req)
        if not alts:
            raise ConfigError("empty requirement")
        out.append(alts)
    return out


def load_graph(data: dict) -> Graph:
    nodes: Dict[str, Node] = {}
    for raw in data.get("nodes", []):
        nid = raw.get("id")
        if not nid or nid in nodes:
            raise ConfigError(f"missing or duplicate node id: {nid!r}")
        nodes[nid] = Node(
            id=nid, label=raw.get("label", ""), needs=_norm_needs(raw.get("needs")),
            probe=raw.get("probe"), timeout_s=float(raw.get("timeout_s", 5.0)),
            slow_ms=raw.get("slow_ms"), slow_hint=raw.get("slow_hint", ""), start=raw.get("start", ""),
            manual=raw.get("manual", ""), sample=raw.get("sample"))
    for n in nodes.values():
        for alt in (a for req in n.needs for a in req):
            if alt not in nodes:
                raise ConfigError(f"{n.id} needs unknown node {alt!r}")
    fallbacks = {k: list(v) for k, v in (data.get("fallbacks") or {}).items()}
    for consumer, order in fallbacks.items():
        for nid in order:
            if nid not in nodes:
                raise ConfigError(f"fallback {consumer} lists unknown node {nid!r}")
    return Graph(nodes, fallbacks)


def load_graph_file(path: str) -> Graph:
    with open(path, encoding="utf-8") as fh:
        return load_graph(json.load(fh))


@dataclass
class Deadlock:
    cycle: List[str]
    single_breaks: List[str]      # manually fixing any one of these unlocks all
    outside_alts: List[str]       # broken alternatives that would also unlock it


@dataclass
class Plan:
    waves: List[List[str]]
    via: Dict[str, List[str]]     # node -> chosen upstream nodes
    up: Set[str]
    broken: Set[str]
    manual: Set[str]
    deadlocks: List[Deadlock]
    blocked: Dict[str, List[str]]  # node -> unmet requirement descriptions

    @property
    def order(self) -> List[str]:
        return [n for w in self.waves for n in w]


def infer(g: Graph, health: Dict[str, Health]) -> Dict[str, Health]:
    """Unprobed hard prerequisites of a working node must be working too."""
    out = dict(health)
    changed = True
    while changed:
        changed = False
        for n in g.nodes.values():
            for req in (n.needs if out.get(n.id, Health()).state == "up" else []):
                if len(req) == 1 and out.get(req[0], Health()).state == "unknown":
                    out[req[0]] = Health("up", error="inferred")
                    changed = True
    return out


def classify(g: Graph, health: Dict[str, Health]) -> Dict[str, str]:
    """Return node -> up | broken | plannable."""
    up = {n for n, h in health.items() if h.state == "up"}
    out = {}
    for n in g.nodes.values():
        h = health.get(n.id, Health())
        if n.id in up:
            out[n.id] = "up"
        elif h.state == "down" and h.error != "refused" and all(
                any(a in up for a in req) for req in n.needs):
            out[n.id] = "broken"
        else:
            out[n.id] = "plannable"
    return out


def _fixpoint(g: Graph, cls: Dict[str, str], assume: Set[str], dead: Set[str]):
    sat = {n for n, c in cls.items() if c == "up"} | (assume - dead)
    waves = [sorted(sat)] if sat else []
    via: Dict[str, List[str]] = {n: [] for n in sat}
    usable = {n for n, c in cls.items() if c == "plannable"} - dead
    while True:
        wave, picks = [], {}
        for nid in sorted(usable - sat):
            chosen = []
            for req in g.nodes[nid].needs:
                hit = next((a for a in req if a in sat), None)
                if hit is None:
                    break
                chosen.append(hit)
            else:
                wave.append(nid)
                picks[nid] = chosen
        if not wave:
            return sat, waves, via
        sat |= set(wave)
        via.update(picks)
        waves.append(wave)


def _sccs(edges: Dict[str, Set[str]]) -> List[List[str]]:
    index, low, stack, on, out, counter = {}, {}, [], set(), [], [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in sorted(edges.get(v, ())):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            out.append(sorted(comp))

    for v in sorted(edges):
        if v not in index:
            visit(v)
    return out


def plan(g: Graph, health: Optional[Dict[str, Health]] = None,
         assume: Iterable[str] = (), dead: Iterable[str] = ()) -> Plan:
    health = health or {}
    assume, dead = set(assume), set(dead)
    cls = classify(g, health)
    broken = {n for n, c in cls.items() if c == "broken"} | dead
    sat, waves, via = _fixpoint(g, cls, assume, dead)
    pending = set(g.nodes) - sat - broken
    edges: Dict[str, Set[str]] = {}
    blocked: Dict[str, List[str]] = {}
    for nid in sorted(pending):
        unmet = [req for req in g.nodes[nid].needs if not any(a in sat for a in req)]
        edges[nid] = {a for req in unmet for a in req if a in pending}
        blocked[nid] = [" / ".join(req) for req in unmet]
    deadlocks = []
    for comp in _sccs(edges):
        if len(comp) == 1 and comp[0] not in edges.get(comp[0], ()):
            continue
        breaks = [c for c in comp
                  if set(comp) <= _fixpoint(g, cls, assume | {c}, dead)[0]]
        outside = sorted({a for c in comp for req in g.nodes[c].needs
                          for a in req if a in broken})
        deadlocks.append(Deadlock(comp, breaks, outside))
    return Plan(waves, via, {n for n, c in cls.items() if c == "up"},
                broken, assume - dead, deadlocks, blocked)
