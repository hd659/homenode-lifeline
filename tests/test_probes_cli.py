"""真实探针（本地 socket、子进程、超时）和 CLI 端到端。"""
import asyncio
import json
import sys

from homenode_lifeline.cli import main
from homenode_lifeline.config import parse_config
from homenode_lifeline.engine import probe_all
from homenode_lifeline.probes import probe

PY = sys.executable


def _real_cfg(channels):
    return parse_config({"node": "t", "timeout_s": 2, "channels": channels})


def test_tcp_probe_real_socket():
    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        c = _real_cfg([{"name": "a", "kind": "tcp", "host": "127.0.0.1", "port": port}])
        up = await probe(c.channels[0], c)
        server.close()
        await server.wait_closed()
        down = await probe(c.channels[0], c)
        return up, down
    up, down = asyncio.run(go())
    assert up.ok and up.latency_ms is not None
    assert not down.ok and "失败" in down.detail


def test_cmd_probe_expect_exit_and_timeout():
    c = _real_cfg([
        {"name": "ok", "kind": "cmd", "command": f'{PY} -c "print(\'pong\')"', "expect": "pong"},
        {"name": "miss", "kind": "cmd", "command": f'{PY} -c "print(1)"', "expect": "pong"},
        {"name": "fail", "kind": "cmd", "command": f'{PY} -c "import sys; sys.exit(3)"'},
        {"name": "slow", "kind": "cmd", "command": f'{PY} -c "import time; time.sleep(5)"',
         "timeout_s": 0.5},
    ])
    res = asyncio.run(probe_all(c))
    assert res["ok"].ok
    assert not res["miss"].ok and "没有" in res["miss"].detail
    assert not res["fail"].ok and "退出码 3" in res["fail"].detail
    assert not res["slow"].ok and "超时" in res["slow"].detail


def test_tailscale_probe_parses_direct_and_derp(tmp_path):
    fake = tmp_path / "ts"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$6" in\n'
        '  direct) echo "pong from minipc (192.0.2.9) via 192.0.2.10:41641 in 41ms";;\n'
        '  relay) echo "pong from minipc (192.0.2.9) via DERP(sin) in 120ms";;\n'
        '  *) echo "timeout waiting for pong"; exit 1;;\n'
        "esac\n"
    )
    fake.chmod(0o755)
    c = parse_config({"node": "t", "timeout_s": 3, "tailscale_bin": str(fake), "channels": [
        {"name": n, "kind": "tailscale", "host": n} for n in ("direct", "relay", "gone")]})
    res = asyncio.run(probe_all(c))
    assert res["direct"].ok and res["direct"].detail == "直连" and res["direct"].latency_ms == 41
    assert res["relay"].ok and res["relay"].detail == "DERP 中继"
    assert not res["gone"].ok and "timeout" in res["gone"].detail


def test_cli_init_check_history(tmp_path, capsys):
    conf, db = tmp_path / "c.json", tmp_path / "l.db"
    assert main(["--config", str(conf), "init"]) == 0
    assert main(["--config", str(conf), "init"]) == 1  # 不覆盖已有配置
    d = {"node": "lab", "timeout_s": 2, "channels": [
        {"name": "p", "kind": "cmd", "command": f'{PY} -c "print(\'lifeline-ok\')"',
         "expect": "lifeline-ok"},
        {"name": "q", "kind": "cmd", "command": f'{PY} -c "import sys; sys.exit(1)"'}],
        "uptime": [{"via": "p", "command": f'{PY} -c "print(7200)"'}],
        "repairs": [{"target": "q", "via": "p", "steps": ["restart q"]}]}
    conf.write_text(json.dumps(d))
    base = ["--config", str(conf), "--db", str(db)]
    assert main(base + ["check"]) == 1
    out = capsys.readouterr().out
    assert "2小时0分" in out and "restart q" in out
    assert main(base + ["history"]) == 0
    hist = capsys.readouterr().out
    assert "100.0%  (1/1)" in hist and "0.0%  (0/1)" in hist
    conf.write_text("{bad")
    assert main(base + ["check"]) == 4


def test_demo_runs():
    from homenode_lifeline.demo import run_demo
    text = run_demo()
    assert "断电后来电自启" in text and "无法判断" in text
