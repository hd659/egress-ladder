"""Fail-fast probes. No third-party deps: raw sockets for SOCKS5 / HTTP CONNECT.

Probe spec (per node, JSON):
  {"type": "tcp",    "host": "127.0.0.1", "port": 1084}
  {"type": "socks5", "host": "127.0.0.1", "port": 1084, "target": "example.com:443"}
  {"type": "http",   "host": "127.0.0.1", "port": 8080, "target": "example.com:443"}
  {"type": "cmd",    "argv": ["tailscale", "status"]}
Error kinds: refused | timeout | rejected | unreachable | failed
"""
from __future__ import annotations

import socket
import statistics
import struct
import subprocess
import time
from typing import Callable, Dict, Optional, Tuple

from .core import Graph, Health, Node

Result = Tuple[bool, float, str]   # ok, seconds, error kind


def _split_target(target: str) -> Tuple[str, int]:
    host, _, port = target.rpartition(":")
    return host, int(port)


def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("short read")
        buf += chunk
    return buf


def socks5_connect(s: socket.socket, target: str) -> None:
    host, port = _split_target(target)
    s.sendall(b"\x05\x01\x00")
    ver, method = _recv_exact(s, 2)
    if ver != 5 or method != 0:
        raise PermissionError(f"socks method {method}")
    name = host.encode("idna")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
    head = _recv_exact(s, 4)
    if head[1] != 0:
        raise PermissionError(f"socks reply {head[1]}")
    atyp = head[3]
    size = {1: 4, 4: 16}.get(atyp)
    if size is None:
        size = _recv_exact(s, 1)[0]
    _recv_exact(s, size + 2)


def http_connect(s: socket.socket, target: str) -> None:
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    line = s.recv(256).split(b"\r\n", 1)[0]
    parts = line.split()
    if len(parts) < 2 or parts[1] != b"200":
        raise PermissionError(line.decode(errors="replace"))


def _kind(exc: BaseException) -> str:
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, (socket.timeout, TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "rejected"
    if isinstance(exc, OSError):
        return "unreachable"
    return "failed"


def probe_once(spec: dict, timeout_s: float) -> Result:
    t0 = time.monotonic()
    try:
        kind = spec.get("type", "tcp")
        if kind == "cmd":
            r = subprocess.run(spec["argv"], capture_output=True, timeout=timeout_s)
            ok = r.returncode == 0
            return ok, time.monotonic() - t0, "" if ok else "failed"
        deadline = t0 + timeout_s
        with socket.create_connection((spec["host"], int(spec["port"])), timeout=timeout_s) as s:
            s.settimeout(max(0.05, deadline - time.monotonic()))
            if kind == "socks5":
                socks5_connect(s, spec.get("target", "example.com:443"))
            elif kind == "http":
                http_connect(s, spec.get("target", "example.com:443"))
        return True, time.monotonic() - t0, ""
    except Exception as exc:  # noqa: BLE001 - classify everything
        return False, time.monotonic() - t0, _kind(exc)


def summarise(results: list) -> Health:
    oks = [t for ok, t, _ in results if ok]
    fails = [(t, e) for ok, t, e in results if not ok]
    p = len(oks) / len(results) if results else None
    err = ""
    if fails:
        kinds = [e for _, e in fails]
        err = max(set(kinds), key=kinds.count)
    return Health(
        state="up" if oks else "down", error="" if oks else err, p=p,
        ok_s=statistics.median(oks) if oks else None,
        fail_s=statistics.mean(t for t, _ in fails) if fails else None)


def health_from_sample(sample: Optional[dict], scenario: str = "") -> Health:
    """Sample is either a flat health dict or {scenario: health dict}."""
    if sample and "state" not in sample:
        sample = sample.get(scenario) if scenario else next(iter(sample.values()), None)
    if not sample:
        return Health()
    ms = lambda k: sample[k] / 1000.0 if sample.get(k) is not None else None  # noqa: E731
    return Health(state=sample.get("state", "unknown"), error=sample.get("error", ""),
                  p=sample.get("p"), ok_s=ms("ok_ms"), fail_s=ms("fail_ms"))


def probe_node(node: Node, samples: int = 3,
               runner: Callable[[dict, float], Result] = probe_once) -> Health:
    if not node.probe:
        return Health()
    return summarise([runner(node.probe, node.timeout_s) for _ in range(samples)])


def collect(g: Graph, offline: bool = False, samples: int = 3,
            runner: Optional[Callable[[dict, float], Result]] = None,
            scenario: str = "") -> Dict[str, Health]:
    out: Dict[str, Health] = {}
    for nid, node in g.nodes.items():
        if offline:
            out[nid] = health_from_sample(node.sample, scenario)
        else:
            out[nid] = probe_node(node, samples, runner or probe_once)
    return out
