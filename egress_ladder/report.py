"""Render a plan into a Chinese restart card (plain text / markdown-friendly)."""
from __future__ import annotations

import unicodedata
from typing import Dict, List

from .core import Graph, Health, Plan
from .fallback import FallbackReport

ERR = {"refused": "端口没开", "timeout": "超时", "rejected": "被拒绝",
       "unreachable": "网络不可达", "failed": "失败", "": "不通"}


def dwidth(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - dwidth(text))


def _ms(s) -> str:
    return "-" if s is None else f"{s * 1000:.0f}ms"


def is_slow(g: Graph, nid: str, h: Health) -> bool:
    slow = g.nodes[nid].slow_ms
    return bool(slow and h.ok_s is not None and h.ok_s * 1000 > slow)


def status_lines(g: Graph, health: Dict[str, Health]) -> List[str]:
    out = []
    width = max(dwidth(n.name) for n in g.nodes.values())
    for nid, node in g.nodes.items():
        h = health.get(nid, Health())
        if h.state == "up" and h.error == "inferred":
            line = f"  通    {pad(node.name, width)}  （没单独测，下游通了所以它一定通）"
        elif h.state == "up":
            tag = "慢" if is_slow(g, nid, h) else "通"
            rate = "" if h.p is None else f"  成功率 {h.p:.0%}"
            line = f"  {tag}    {pad(node.name, width)}  {_ms(h.ok_s)}{rate}"
            if tag == "慢" and node.slow_hint:
                line += f"  ({node.slow_hint})"
        elif h.state == "down":
            line = f"  不通  {pad(node.name, width)}  {ERR.get(h.error, h.error)} {_ms(h.fail_s)}"
        else:
            line = f"  未测  {node.name}"
        out.append(line)
    return out


def _name(g: Graph, nid: str) -> str:
    return g.nodes[nid].name


def plan_lines(g: Graph, p: Plan) -> List[str]:
    out = []
    if p.deadlocks:
        out.append("必须先手动处理（死循环）")
        for d in p.deadlocks:
            out.append("  环: " + " -> ".join(_name(g, c) for c in d.cycle + d.cycle[:1]))
            for b in d.single_breaks:
                how = g.nodes[b].manual or "手动让它先通"
                out.append(f"    解法: 手动搞定「{_name(g, b)}」: {how}")
            if d.outside_alts:
                names = "、".join(_name(g, a) for a in d.outside_alts)
                out.append(f"    或者: 先修好 {names}（现在不通），环会自动解开")
            if not d.single_breaks and not d.outside_alts:
                out.append("    单独处理任何一个都不够，需要同时处理环里多个节点")
        out.append("  处理完后加 --assume <节点> 重新排")
        out.append("")
    if p.broken:
        out.append("已坏（上游都通但自己不通，先别指望）: "
                   + "、".join(_name(g, b) for b in sorted(p.broken)))
        out.append("")
    out.append("启动顺序")
    if not p.waves:
        out.append("  （没有能启动的节点）")
    for i, wave in enumerate(p.waves, 1):
        for nid in wave:
            node = g.nodes[nid]
            if nid in p.up:
                note = "已通，不用动"
            elif nid in p.manual:
                note = "你已手动处理"
            else:
                note = node.start or node.manual or "启动它"
            via = p.via.get(nid) or []
            dep = f"  [经 {', '.join(_name(g, v) for v in via)}]" if via and nid not in p.up else ""
            out.append(f"  第{i}步  {node.name}: {note}{dep}")
    stuck = {n: r for n, r in p.blocked.items()
             if not any(n in d.cycle for d in p.deadlocks)}
    if stuck:
        out.append("")
        out.append("暂时起不来（等上游）")
        for nid, reqs in stuck.items():
            out.append(f"  {_name(g, nid)}: 缺 " + "；".join(reqs))
    return out


def fallback_lines(g: Graph, reports: List[FallbackReport]) -> List[str]:
    out = []
    for r in reports:
        cur = " > ".join(_name(g, n) for n in r.current)
        best = " > ".join(_name(g, n) for n in r.best) or "（没有能用的）"
        out.append(f"回落顺序: {r.consumer}")
        out.append(f"  现在: {cur}   平均每条新连接等 {r.current_wait_s:.2f}s")
        if not r.usable:
            out.append("  现在一条都不通：先按上面的启动顺序把出口开起来，再重测一次")
            continue
        elif r.best != r.current:
            out.append(f"  建议: {best}   等 {r.best_wait_s:.2f}s，省 {r.saved_s:.2f}s")
        else:
            out.append("  现在的顺序已经最优")
        for d in r.drop:
            out.append(f"  建议去掉 {_name(g, d)}: 在这里不通，每次还要白等"
                       f" {g.nodes[d].timeout_s:g}s")
        for nid, t in r.timeout_hints.items():
            out.append(f"  {_name(g, nid)} 超时可从 {g.nodes[nid].timeout_s:g}s 收到 {t:g}s")
        if r.fail_all_current > 0.2:
            out.append(f"  注意: 全部候选都失败的概率 {r.fail_all_current:.0%}")
    return out


def card(g: Graph, health: Dict[str, Health], p: Plan,
         reports: List[FallbackReport], stamp: str = "") -> str:
    head = "出口冷启动卡" + (f"  {stamp}" if stamp else "")
    parts = [head, "=" * 24, "当前状态", *status_lines(g, health), "",
             *plan_lines(g, p)]
    fb = fallback_lines(g, reports)
    if fb:
        parts += ["", *fb]
    return "\n".join(parts) + "\n"
