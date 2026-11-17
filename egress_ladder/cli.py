"""egress-ladder CLI.

  python3 -m egress_ladder demo [--scenario cold|warm]
  python3 -m egress_ladder init my-egress.json
  python3 -m egress_ladder plan -c my-egress.json [--assume misty-client] [--save card.txt]
  python3 -m egress_ladder plan -c my-egress.json --offline --scenario warm --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from typing import List, Optional

from .core import ConfigError, infer, load_graph_file, plan
from .fallback import analyse
from .probe import collect
from .report import card

EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "example.json")


def build(args) -> dict:
    g = load_graph_file(args.config)
    health = infer(g, collect(g, offline=args.offline, samples=args.samples,
                              scenario=args.scenario))
    p = plan(g, health, assume=args.assume, dead=args.dead)
    reports = analyse(g, health)
    return {"graph": g, "health": health, "plan": p, "reports": reports}


def to_json(res: dict) -> dict:
    p = res["plan"]
    return {
        "health": {k: asdict(v) for k, v in res["health"].items()},
        "waves": p.waves,
        "via": p.via,
        "broken": sorted(p.broken),
        "deadlocks": [asdict(d) for d in p.deadlocks],
        "blocked": p.blocked,
        "fallbacks": [dict(asdict(r), saved_s=r.saved_s) for r in res["reports"]],
    }


def emit(res: dict, args) -> int:
    if args.json:
        text = json.dumps(to_json(res), ensure_ascii=False, indent=2) + "\n"
    else:
        text = card(res["graph"], res["health"], res["plan"], res["reports"],
                    stamp=time.strftime("%Y-%m-%d %H:%M"))
    sys.stdout.write(text)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            fh.write(text)
        sys.stdout.write(f"\n已保存到 {args.save}\n")
    # exit 2 when the user has to step in by hand
    return 2 if res["plan"].deadlocks else 0


def _common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--assume", action="append", default=[], metavar="NODE",
                    help="当作已手动搞定（解环后重排）")
    sp.add_argument("--dead", action="append", default=[], metavar="NODE",
                    help="当作坏掉、不参与规划")
    sp.add_argument("--samples", type=int, default=3, help="每个节点探测次数")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.add_argument("--save", metavar="FILE", help="把冷启动卡另存一份")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="egress-ladder",
                                 description="多级出口冷启动规划：依赖、死循环、启动顺序、回落顺序")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="探测并输出冷启动卡")
    sp.add_argument("-c", "--config", required=True)
    sp.add_argument("--offline", action="store_true", help="不探测，用配置里的 sample")
    sp.add_argument("--scenario", default="", help="离线时选用哪组 sample")
    _common(sp)

    sd = sub.add_parser("demo", help="用内置示例离线演示")
    sd.add_argument("--scenario", default="cold", choices=["cold", "warm"])
    _common(sd)

    si = sub.add_parser("init", help="复制一份示例配置")
    si.add_argument("path")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == "init":
        if os.path.exists(args.path):
            print(f"{args.path} 已存在，不覆盖", file=sys.stderr)
            return 1
        shutil.copyfile(EXAMPLE, args.path)
        print(f"已写入 {args.path}，改好节点后运行: python3 -m egress_ladder plan -c {args.path}")
        return 0
    if args.cmd == "demo":
        args.config, args.offline = EXAMPLE, True
    try:
        res = build(args)
    except (ConfigError, OSError, json.JSONDecodeError) as exc:
        print(f"配置有问题: {exc}", file=sys.stderr)
        return 1
    return emit(res, args)


if __name__ == "__main__":
    raise SystemExit(main())
