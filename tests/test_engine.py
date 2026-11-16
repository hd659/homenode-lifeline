import asyncio
import copy
import json

import pytest

from homenode_lifeline.config import ConfigError, parse_config
from homenode_lifeline.defaults import TEMPLATE
from homenode_lifeline.engine import judge, probe_all, run_round, should_notify
from homenode_lifeline.notify import summarize
from homenode_lifeline.probes import ProbeResult, parse_uptime
from homenode_lifeline.report import render, render_history, render_onsite, to_json
from homenode_lifeline.store import Store

T0 = 1_800_000_000.0
H = 3600.0


def cfg():
    return parse_config(copy.deepcopy(TEMPLATE))


def fake(down=(), calls=None):
    async def prober(ch, c):
        if calls is not None:
            calls.append(ch.name)
        return ProbeResult(ch.name, ch.name not in down, 10.0, "x")
    return prober


def boot_at(ts):
    async def reader(sources, alive, c, now):
        for s in sources:
            if s.via in alive:
                return ts, s.via
        return None, ""
    return reader


def rnd(c, store, down=(), boot=T0 - 10 * 86400, h=0.0):
    return asyncio.run(run_round(c, store, fake(down), boot_at(boot), now=T0 + h * H))


# ---------- 配置 ----------

def test_template_is_valid():
    c = cfg()
    assert [ch.name for ch in c.channels if ch.role == "path"] == ["tailscale", "tunnel"]


@pytest.mark.parametrize("mutate, msg", [
    (lambda d: d["channels"].append(dict(d["channels"][0])), "重复"),
    (lambda d: d["channels"][1].update(requires=["nope"]), "不存在"),
    (lambda d: d["channels"][0].update(requires=["vps"]), "成环"),
    (lambda d: d["repairs"].append({"target": "tunnel", "via": "tunnel", "steps": []}), "自己"),
    (lambda d: d["channels"][0].update(port=0), "host 和 port"),
    (lambda d: d.update(bogus=1), "不认识"),
])
def test_config_rejects_bad(mutate, msg):
    d = copy.deepcopy(TEMPLATE)
    mutate(d)
    with pytest.raises(ConfigError, match=msg):
        parse_config(d)


# ---------- 开机时长解析 ----------

@pytest.mark.parametrize("text, expect", [
    ("3600", T0 - 3600),
    ("3600.52 7100.00", T0 - 3600.52),
    ("2027-01-15T07:00:00+00:00", T0 - 3600),
    ("2027-01-15T07:00:00Z", T0 - 3600),
    ("2 days, 01:00:00", T0 - (2 * 86400 + 3600)),
    ("01:30", T0 - 5400),
    ("", None),
    ("garbage", None),
])
def test_parse_uptime(text, expect):
    got = parse_uptime(text, T0)
    assert got == pytest.approx(expect) if expect is not None else got is None


# ---------- 依赖与判定 ----------

def test_dependency_skips_and_does_not_probe():
    c, calls = cfg(), []
    res = asyncio.run(probe_all(c, fake(down={"vps"}, calls=calls)))
    assert res["tunnel"].skipped and not res["tunnel"].ok
    assert "tunnel" not in calls and "tailscale" in calls


@pytest.mark.parametrize("down, verdict", [
    (set(), "ok"),
    ({"tailscale"}, "degraded"),
    ({"egress"}, "degraded"),
    ({"tailscale", "tunnel"}, "isolated"),
    ({"tailscale", "vps"}, "isolated"),
    ({"local"}, "blind"),
])
def test_judge(down, verdict):
    c = cfg()
    assert judge(c, asyncio.run(probe_all(c, fake(down)))) == verdict


def test_repair_routes_through_surviving_path():
    store = Store(":memory:")
    rep = rnd(cfg(), store, down={"tailscale"})
    route = {r.target: r for r in rep.routes}
    assert [x.via for x in route["tailscale"].available] == ["tunnel"]
    assert route["ssh"].root_cause == ["tailscale"]
    assert "走 反向隧道" in render(cfg(), rep)


def test_both_paths_down_is_onsite():
    rep = rnd(cfg(), Store(":memory:"), down={"tailscale", "tunnel"})
    route = {r.target: r for r in rep.routes}
    assert route["tailscale"].needs_onsite and route["tunnel"].needs_onsite
    text = render(cfg(), rep)
    assert "只能找人到现场" in text and "按电源键" in text


def test_vps_down_repair_needs_no_channel():
    rep = rnd(cfg(), Store(":memory:"), down={"vps"})
    route = {r.target: r for r in rep.routes}
    assert route["vps"].available[0].via == "*"
    assert route["tunnel"].root_cause == ["vps"]


def test_isolated_by_upstream_does_not_send_people_onsite():
    rep = rnd(cfg(), Store(":memory:"), down={"tailscale", "vps"})
    assert rep.verdict == "isolated"
    text = render(cfg(), rep)
    assert "先按上面的路线把上游修好" in text and "按电源键" not in text


# ---------- 断电 / 仅断网 / 事件 ----------

def test_power_loss_detected_after_isolation():
    c, s = cfg(), Store(":memory:")
    rnd(c, s, h=0)
    rnd(c, s, down={"tailscale", "tunnel"}, h=1)
    rep = rnd(c, s, boot=T0 + 1.8 * H, h=2)
    assert rep.rebooted
    assert any(k == "reboot" and "断电" in m for k, _, m in rep.events)
    assert "重启记录" in render_history(c, s, 24, now=T0 + 2 * H)


def test_network_only_outage_no_reboot():
    c, s = cfg(), Store(":memory:")
    rnd(c, s, h=0)
    rnd(c, s, down={"tailscale", "tunnel"}, h=1)
    rep = rnd(c, s, h=2)
    assert not rep.rebooted
    assert any(k == "netonly" for k, _, _ in rep.events)


def test_small_boot_jitter_is_not_reboot():
    c, s = cfg(), Store(":memory:")
    rnd(c, s, boot=T0 - 1000, h=0)
    assert not rnd(c, s, boot=T0 - 1000 + 5, h=1).rebooted


def test_events_only_on_change_and_escalation():
    c, s = cfg(), Store(":memory:")
    c.escalate_after = 3
    assert not should_notify(rnd(c, s, h=0))
    r1 = rnd(c, s, down={"egress"}, h=1)
    assert should_notify(r1) and r1.events[0][0] == "down"
    r2 = rnd(c, s, down={"egress"}, h=2)
    assert not should_notify(r2) and r2.streaks["egress"] == 2
    r3 = rnd(c, s, down={"egress"}, h=3)
    assert r3.escalated == ["egress"] and r3.down_since["egress"] == T0 + H
    title, body = summarize(c, r3)
    assert "连续断 3 轮" in body and "走 Tailscale SSH" in body
    r4 = rnd(c, s, h=4)
    assert any(k == "recovered" for k, _, _ in r4.events)
    up, total = s.availability(T0)["egress"]
    assert (up, total) == (2, 5)


def test_json_and_onsite_render():
    c = cfg()
    rep = rnd(c, Store(":memory:"), down={"tunnel"})
    data = json.loads(to_json(c, rep))
    assert data["verdict"] == "degraded" and data["channels"]["tunnel"]["state"] == "down"
    assert "Tailscale" in render_onsite(c)

