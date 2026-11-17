# egress-ladder

A cold-start planner for a personal setup with several proxy exits. You list your exits and what each one depends on. It probes which ones work and how fast, then prints a Chinese "restart card":

- **Current status**: whether each exit works, its latency and success rate. It also flags exits that work but are too slow, such as a Tailscale exit going through a DERP relay.
- **Manual first (deadlocks)**: chicken-and-egg loops like "the client can't log in without a proxy, and the proxy doesn't work until you log in". For each loop it lists which single node you can fix by hand to unlock it, or which broken exit would unlock it once repaired.
- **Bring-up order**: a topological order in waves, showing which upstream each step goes through.
- **Fallback order**: the expected wait per new connection for the current candidate order, the order that minimises it, which candidates to drop, and how far each timeout can be cut.

It uses only the standard library. SOCKS5 and HTTP CONNECT are probed over raw sockets.

## Quick start

```bash
python3 -m egress_ladder demo                    # offline demo: the Misty login loop on the day you get back
python3 -m egress_ladder demo --scenario warm    # after the loop is fixed: suggests a new fallback order
python3 -m egress_ladder init my-egress.json     # copy the example config and edit it
python3 -m egress_ladder plan -c my-egress.json  # live probes + restart card
python3 -m egress_ladder plan -c my-egress.json --assume misty-login   # after a manual fix, plan again
python3 -m egress_ladder plan -c my-egress.json --save ~/egress-card.txt --json
```

Exit codes: `0` means you can start things in the printed order. `2` means there is a loop you have to handle by hand. `1` means the config is wrong.

## Config

```json
{
  "nodes": [
    {"id": "misty-client", "label": "Misty:1084",
     "needs": ["misty-login"],
     "probe": {"type": "socks5", "host": "127.0.0.1", "port": 1084, "target": "example.com:443"},
     "timeout_s": 5, "slow_ms": 800,
     "start": "Open the client and connect",
     "manual": "Import an offline node config first"},
    {"id": "misty-login", "needs": ["local-net", ["misty-client", "sg-ssh", "ts-exit"]]}
  ],
  "fallbacks": {"isolated-browser-bridge": ["sg-ssh", "misty-client", "ts-exit"]}
}
```

- `needs` is a list of requirements that must all hold (AND). Inside a requirement, a list is a set of alternatives (OR), and any one of them is enough.
- `probe.type`: `tcp` / `socks5` / `http` (CONNECT) / `cmd` (an exit code of 0 counts as working).
- `sample`: offline samples for demos and tests. Use `--offline --scenario <name>` to select a group.
- A node with no probe is inferred to work when a downstream node that hard-depends on it works.

## How it decides

- **Health classification**: a refused connection means the port isn't listening, so the node just hasn't been started and can still be planned. If all upstreams work but the node times out or is rejected, it is marked broken and left out of planning.
- **Deadlocks**: nodes that still can't start after the fixpoint are put into a graph of unmet dependencies, and Tarjan's algorithm finds the strongly connected components. For each loop it tries assuming each node was fixed by hand. If that is enough for the whole loop to start, the node is listed as a single-point fix.
- **Fallback cost**: each attempt costs `p*ok + (1-p)*fail`. The expected total is the sum of each attempt's cost times the probability that every earlier candidate failed. Sorting by `attempt/p` in ascending order gives the optimal order (proved by an adjacent-swap argument). Candidates with `p=0` that still cost time are recommended for removal.
- **Timeout suggestion**: three times the observed normal latency, never below 1 second and never below `slow_ms`, rounded up to the nearest 0.5 second.

## Scope

It only plans connectivity for your own network: whether your own exits work and what order to start them in. It does not do multi-account isolation and does not try to get around any platform's checks.

## Tests

```bash
python3 -m pytest -q
```

The tests are fully offline. SOCKS5 probes run against a local fake server.
