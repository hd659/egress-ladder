import json
import socket
import threading

import pytest

from egress_ladder import cli
from egress_ladder.core import ConfigError, Health, infer, load_graph, load_graph_file, plan
from egress_ladder.fallback import Leg, analyse, best_order, expected_wait
from egress_ladder.probe import collect, probe_once, summarise
from egress_ladder.report import card

UP, DOWN = Health("up", p=1, ok_s=0.1), Health("down", "timeout", p=0, fail_s=8)


def chicken_egg():
    return load_graph({"nodes": [
        {"id": "net"},
        {"id": "client", "needs": ["login"]},
        {"id": "login", "needs": ["net", ["client", "jump"]]},
        {"id": "jump", "needs": ["net"]},
    ]})


def test_bring_up_waves_follow_dependencies():
    g = load_graph({"nodes": [{"id": "a"}, {"id": "b", "needs": ["a"]},
                              {"id": "c", "needs": ["b", "a"]}]})
    p = plan(g)
    assert p.waves == [["a"], ["b"], ["c"]]
    assert p.via["c"] == ["b", "a"] and not p.deadlocks


def test_or_alternative_avoids_deadlock_when_jump_host_starts():
    p = plan(chicken_egg(), {"net": UP})
    assert not p.deadlocks
    assert p.order.index("jump") < p.order.index("login") < p.order.index("client")
    assert p.via["login"] == ["net", "jump"]


def test_deadlock_when_only_alternative_is_broken():
    p = plan(chicken_egg(), {"net": UP, "jump": DOWN})
    assert p.broken == {"jump"}
    assert len(p.deadlocks) == 1
    d = p.deadlocks[0]
    assert d.cycle == ["client", "login"]
    assert d.single_breaks == ["client", "login"]
    assert d.outside_alts == ["jump"]


def test_assume_breaks_cycle_and_reorders():
    p = plan(chicken_egg(), {"net": UP, "jump": DOWN}, assume=["login"])
    assert not p.deadlocks
    assert p.order.index("login") < p.order.index("client")
    assert "login" in p.manual


def test_refused_port_is_not_broken_but_plannable():
    p = plan(chicken_egg(), {"net": UP, "jump": Health("down", "refused")})
    assert "jump" not in p.broken and not p.deadlocks


def test_dead_flag_excludes_node():
    p = plan(chicken_egg(), {"net": UP}, dead=["jump"])
    assert p.deadlocks and "jump" in p.broken


def test_self_loop_reported():
    g = load_graph({"nodes": [{"id": "x", "needs": ["x"]}]})
    assert plan(g).deadlocks[0].cycle == ["x"]


def test_infer_marks_hard_prereq_of_working_node():
    h = infer(chicken_egg(), {"client": UP})
    assert h["login"].state == "up" and h["login"].error == "inferred"
    assert "jump" not in h  # OR-alternative stays unknown


def test_config_errors():
    with pytest.raises(ConfigError):
        load_graph({"nodes": [{"id": "a", "needs": ["nope"]}]})
    with pytest.raises(ConfigError):
        load_graph({"nodes": [{"id": "a"}, {"id": "a"}]})
    with pytest.raises(ConfigError):
        load_graph({"nodes": [{"id": "a"}], "fallbacks": {"b": ["zzz"]}})


def test_expected_wait_and_optimal_order():
    slow_dead = Leg("ssh", 0.0, 0.0, 8.0)
    misty = Leg("misty", 1.0, 0.2, 5.0)
    assert expected_wait([slow_dead, misty]) == pytest.approx(8.2)
    assert [l.id for l in best_order([slow_dead, misty])] == ["misty", "ssh"]
    a, b = Leg("a", 0.5, 1.0, 4.0), Leg("b", 0.9, 0.5, 4.0)
    best = best_order([a, b])
    assert expected_wait(best) <= expected_wait([a, b])
    assert expected_wait(best) <= expected_wait(list(reversed(best)))


def test_fallback_report_on_example_warm():
    g = load_graph_file(cli.EXAMPLE)
    health = collect(g, offline=True, scenario="warm")
    rep = analyse(g, health)[0]
    assert rep.current[0] == "sg-ssh"
    assert rep.best == ["misty-client", "ts-exit"]
    assert rep.drop == ["sg-ssh"]
    assert rep.saved_s == pytest.approx(8.0, abs=0.01)


def test_example_cold_has_misty_paradox():
    g = load_graph_file(cli.EXAMPLE)
    p = plan(g, collect(g, offline=True, scenario="cold"))
    assert [d.cycle for d in p.deadlocks] == [["misty-client", "misty-login"]]
    assert p.broken == {"sg-ssh", "ts-exit"}


def test_card_text_mentions_manual_step():
    g = load_graph_file(cli.EXAMPLE)
    h = collect(g, offline=True, scenario="cold")
    text = card(g, h, plan(g, h), analyse(g, h))
    assert "死循环" in text and "Misty登录" in text and "一条都不通" in text


def test_summarise_majority_error_and_ratio():
    h = summarise([(True, 0.2, ""), (False, 5.0, "timeout"), (True, 0.4, "")])
    assert h.state == "up" and h.p == pytest.approx(2 / 3)
    assert h.ok_s == pytest.approx(0.3) and h.fail_s == pytest.approx(5.0)


def _fake_socks_server(reply_code=0):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(3)
            conn.sendall(b"\x05\x00")
            conn.recv(262)
            conn.sendall(bytes([5, reply_code, 0, 1]) + b"\x00" * 6)
        srv.close()
    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def test_socks5_probe_against_local_fake():
    port = _fake_socks_server()
    ok, _, err = probe_once({"type": "socks5", "host": "127.0.0.1", "port": port}, 2)
    assert ok and err == ""
    port = _fake_socks_server(reply_code=5)
    ok, _, err = probe_once({"type": "socks5", "host": "127.0.0.1", "port": port}, 2)
    assert not ok and err == "rejected"


def test_refused_probe():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ok, _, err = probe_once({"type": "tcp", "host": "127.0.0.1", "port": port}, 1)
    assert not ok and err == "refused"


def test_cli_demo_exit_codes_and_json(capsys, tmp_path):
    assert cli.main(["demo"]) == 2
    assert cli.main(["demo", "--scenario", "warm", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{"):])
    assert data["fallbacks"][0]["best"][0] == "misty-client"
    target = tmp_path / "mine.json"
    assert cli.main(["init", str(target)]) == 0
    assert cli.main(["init", str(target)]) == 1
    saved = tmp_path / "card.txt"
    assert cli.main(["plan", "-c", str(target), "--offline", "--scenario", "cold",
                     "--assume", "misty-login", "--save", str(saved)]) == 0
    assert "启动顺序" in saved.read_text(encoding="utf-8")
