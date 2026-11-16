"""一轮探测的完整流程：探测 -> 判定 -> 开机时长 -> 事件 -> 修复路线。

整体判定（verdict）：
- ok        全部通道正常
- degraded  有通道挂了，但至少还有一条远控通道活着，可以远程修
- isolated  所有远控通道都探过且都挂了：节点失联，只能等它自己恢复或找人到现场
- blind     观测端自己的前提挂了（本机断网等），无法判断节点状态，不算节点故障
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .config import ANYWHERE, Config, Repair
from .probes import ProbeResult, probe, read_boot_time
from .store import Store

Prober = Callable[..., Awaitable[ProbeResult]]
BootReader = Callable[..., Awaitable[tuple[float | None, str]]]


@dataclass
class RepairRoute:
    target: str
    available: list[Repair] = field(default_factory=list)
    blocked: list[Repair] = field(default_factory=list)
    root_cause: list[str] = field(default_factory=list)  # 被这些前置通道连累

    @property
    def needs_onsite(self) -> bool:
        return not self.available and not self.root_cause


@dataclass
class RoundReport:
    ts: float
    verdict: str
    results: dict[str, ProbeResult]
    boot_ts: float | None = None
    boot_via: str = ""
    rebooted: bool = False
    events: list[tuple[str, str | None, str]] = field(default_factory=list)
    routes: list[RepairRoute] = field(default_factory=list)
    streaks: dict[str, int] = field(default_factory=dict)
    escalated: list[str] = field(default_factory=list)
    down_since: dict[str, float] = field(default_factory=dict)
    last_seen_alive: float | None = None

    def state(self, name: str) -> str:
        r = self.results[name]
        return "skipped" if r.skipped else ("up" if r.ok else "down")

    @property
    def alive(self) -> set[str]:
        return {n for n, r in self.results.items() if r.ok}


async def probe_all(cfg: Config, prober: Prober = probe) -> dict[str, ProbeResult]:
    """按 requires 依赖并发探测：前置通道活着才探，否则标记跳过。"""
    tasks: dict[str, asyncio.Task] = {}

    async def run(name: str) -> ProbeResult:
        ch = cfg.channel(name)
        deps = [await tasks[d] for d in ch.requires]
        bad = [d.name for d in deps if not d.ok]
        if bad:
            names = "、".join(cfg.title(b) for b in bad)
            return ProbeResult(name, False, detail=f"前置通道 {names} 不通，未探测", skipped=True)
        return await prober(ch, cfg)

    order = [c.name for c in cfg.channels]
    for name in order:  # 先全部建 task，依赖关系在 run 内部 await
        tasks[name] = asyncio.ensure_future(run(name))
    done = await asyncio.gather(*(tasks[n] for n in order))
    return dict(zip(order, done))


def judge(cfg: Config, results: dict[str, ProbeResult]) -> str:
    paths = [c.name for c in cfg.channels if c.role == "path"]
    if any(results[p].ok for p in paths):
        return "ok" if all(r.ok for r in results.values()) else "degraded"
    if all(results[p].skipped for p in paths):
        return "blind"
    roots = [c.name for c in cfg.channels if c.role == "control" and not c.requires]
    if roots and any(not results[r].ok for r in roots):
        return "blind"
    return "isolated"


def plan_repairs(cfg: Config, results: dict[str, ProbeResult]) -> list[RepairRoute]:
    routes: list[RepairRoute] = []
    alive = {n for n, r in results.items() if r.ok}
    for ch in cfg.channels:
        r = results[ch.name]
        if r.ok:
            continue
        route = RepairRoute(ch.name)
        if r.skipped:
            route.root_cause = [d for d in ch.requires if d not in alive]
        else:
            for rep in cfg.repairs:
                if rep.target != ch.name:
                    continue
                ok = rep.via == ANYWHERE or rep.via in alive
                (route.available if ok else route.blocked).append(rep)
        routes.append(route)
    return routes


VERDICT_MSG = {
    "ok": "全部通道恢复正常",
    "degraded": "部分通道异常，仍可远程处理",
    "isolated": "所有远控通道都断了，节点失联",
    "blind": "观测端自身网络异常，暂时无法判断节点状态",
}


async def run_round(
    cfg: Config,
    store: Store,
    prober: Prober = probe,
    boot_reader: BootReader = read_boot_time,
    now: float | None = None,
) -> RoundReport:
    now = time.time() if now is None else now
    prev_states = store.last_states()
    prev_verdict = store.last_verdict()
    prev_boot = store.last_boot()
    last_alive = store.last_seen_alive()

    results = await probe_all(cfg, prober)
    verdict = judge(cfg, results)
    rep = RoundReport(now, verdict, results, last_seen_alive=last_alive)
    rep.boot_ts, rep.boot_via = await boot_reader(cfg.uptime, rep.alive, cfg, now)

    for name, r in results.items():
        st = rep.state(name)
        before = prev_states.get(name)
        if st == "down" and before != "down":
            rep.events.append(("down", name, f"{cfg.title(name)} 断开：{r.detail}"))
        elif st == "up" and before == "down":
            rep.events.append(("recovered", name, f"{cfg.title(name)} 恢复"))

    if prev_verdict and verdict != prev_verdict:
        rep.events.append(("verdict", None, VERDICT_MSG[verdict]))

    if rep.boot_ts is not None and prev_boot is not None:
        if rep.boot_ts - prev_boot > cfg.reboot_tolerance_s:
            rep.rebooted = True
            when = time.strftime("%m-%d %H:%M", time.localtime(rep.boot_ts))
            cause = "期间失联过，很可能是断电后来电自启" if (
                last_alive is not None and rep.boot_ts > last_alive and prev_verdict == "isolated"
            ) else "可能是断电、系统更新或人为重启"
            rep.events.append(("reboot", None, f"节点在 {when} 左右重新开机，{cause}"))
    if prev_verdict == "isolated" and verdict in ("ok", "degraded") and not rep.rebooted:
        if rep.boot_ts is not None and prev_boot is not None:
            rep.events.append(("netonly", None, "失联期间节点没有重启，判断只是网络中断，没断电"))

    rid_states = {n: (rep.state(n), r.latency_ms, r.detail) for n, r in results.items()}
    store.save_round(now, verdict, rid_states, rep.boot_ts, rep.boot_via)
    for kind, ch, msg in rep.events:
        store.add_event(now, kind, ch, msg)

    for name in results:
        if rep.state(name) == "down":
            rep.streaks[name] = store.down_streak(name)
            since = store.down_since(name)
            if since is not None:
                rep.down_since[name] = since
            if rep.streaks[name] == cfg.escalate_after:
                rep.escalated.append(name)
    rep.routes = plan_repairs(cfg, results)
    return rep


def should_notify(rep: RoundReport) -> bool:
    """只在状态变化、重启、或某通道刚好连续断满阈值时通知，避免刷屏。"""
    return bool(rep.events or rep.escalated)
