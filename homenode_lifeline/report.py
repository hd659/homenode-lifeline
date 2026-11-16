"""把一轮结果渲染成人能直接照做的中文结论。"""
from __future__ import annotations

import json
import time
import unicodedata

from .config import ANYWHERE, Config
from .engine import RoundReport
from .store import Store

HEADLINE = {
    "ok": "正常：所有通道都通",
    "degraded": "部分异常：还有活着的通道，可以远程修",
    "isolated": "失联：所有远控通道都断了",
    "blind": "无法判断：观测端自己的网络有问题，先查本机",
}
MARK = {"up": "通", "down": "断", "skipped": "--"}


def fmt_duration(sec: float) -> str:
    sec = int(max(sec, 0))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分"
    return f"{m}分钟"


def fmt_ts(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def dwidth(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(width - dwidth(s), 0)


def render(cfg: Config, rep: RoundReport) -> str:
    out = [f"[{cfg.node}] {fmt_ts(rep.ts)}  {HEADLINE[rep.verdict]}", ""]
    width = max(dwidth(c.title) for c in cfg.channels)
    for ch in cfg.channels:
        r = rep.results[ch.name]
        st = rep.state(ch.name)
        lat = f"{r.latency_ms:.0f}ms" if r.ok and r.latency_ms is not None else ""
        extra = ""
        since = rep.down_since.get(ch.name)
        if st == "down" and since is not None and rep.ts - since >= 60:
            extra = f"（已断 {fmt_duration(rep.ts - since)}）"
        out.append(f"  {MARK[st]}  {pad(ch.title, width)}  {lat:>6}  {r.detail}{extra}")

    out.append("")
    if rep.boot_ts is not None:
        line = f"开机时长：{fmt_duration(rep.ts - rep.boot_ts)}（{fmt_ts(rep.boot_ts)} 开机，经 {cfg.title(rep.boot_via)} 读取）"
        out.append(line + ("，本轮发现重启过" if rep.rebooted else ""))
    elif rep.verdict in ("ok", "degraded"):
        out.append("开机时长：读不到（能读开机时长的通道都不通，或命令输出不认识）")
    elif rep.last_seen_alive is not None:
        out.append(f"最后一次确认在线：{fmt_ts(rep.last_seen_alive)}（{fmt_duration(rep.ts - rep.last_seen_alive)}前）")

    for kind, _, msg in rep.events:
        if kind in ("reboot", "netonly"):
            out.append(f"注意：{msg}")

    if rep.routes:
        if out[-1]:
            out.append("")
        out.append("修复路线：")
        out.extend(render_routes(cfg, rep))
    if rep.verdict == "isolated":
        out.append("")
        out.extend(render_isolated(cfg, rep))
    return "\n".join(out)


def render_routes(cfg: Config, rep: RoundReport) -> list[str]:
    lines: list[str] = []
    cascaded: list[str] = []
    for route in rep.routes:
        title = cfg.title(route.target)
        if route.root_cause:
            cascaded.append(title)
            continue
        if route.available:
            for rp in route.available:
                via = "不需要任何通道" if rp.via == ANYWHERE else f"走 {cfg.title(rp.via)}"
                lines.append(f"- {title}：{via}")
                lines.extend(f"    {i}. {s}" for i, s in enumerate(rp.steps, 1))
                if rp.note:
                    lines.append(f"    备注：{rp.note}")
            continue
        if route.blocked:
            vias = "、".join(cfg.title(b.via) for b in route.blocked)
            lines.append(f"- {title}：能修它的 {vias} 现在也不通，无法远程修")
        else:
            lines.append(f"- {title}：配置里没有远程修复预案")
    if cascaded:
        lines.append(f"- 被上游连累、本轮没探：{'、'.join(cascaded)}（上游修好后会自动重新探）")
    return lines


def render_isolated(cfg: Config, rep: RoundReport) -> list[str]:
    unknown = [c.title for c in cfg.channels if c.role == "path" and rep.results[c.name].skipped]
    if unknown:
        return [f"{'、'.join(unknown)} 是因为上游通道断了才没探，节点本身可能没事。"
                "先按上面的路线把上游修好，再判断要不要找人。"]
    lines = ["节点失联时的判断顺序："]
    lines.append("  1. 先等。家宽断网、路由器重启、断电来电后自启一般 10~30 分钟内会自己回来")
    lines.append("  2. 回来后看开机时长：变短了就是断过电，没变就是只断了网")
    if cfg.onsite:
        lines.append("  3. 超过预期还没回来，只能找人到现场：")
        lines.extend(f"     - {s}" for s in cfg.onsite)
    return lines


def render_history(cfg: Config, store: Store, hours: float, now: float | None = None) -> str:
    now = time.time() if now is None else now
    since = now - hours * 3600
    out = [f"[{cfg.node}] 最近 {fmt_duration(hours * 3600)} 可用率（共 {store.round_count()} 轮记录）"]
    avail = store.availability(since)
    for ch in cfg.channels:
        up, total = avail.get(ch.name, (0, 0))
        pct = f"{up / total * 100:5.1f}%" if total else "  无数据"
        out.append(f"  {pad(ch.title, 16)} {pct}  ({up}/{total})")
    reboots = store.events(limit=10, kinds=("reboot",))
    out.append("")
    out.append("重启记录：" if reboots else "重启记录：无")
    out.extend(f"  {fmt_ts(e.ts)}  {e.message}" for e in reboots)
    evs = store.events(limit=20)
    if evs:
        out.append("")
        out.append("最近事件：")
        out.extend(f"  {fmt_ts(e.ts)}  {e.message}" for e in evs)
    return "\n".join(out)


def render_onsite(cfg: Config) -> str:
    out = [f"[{cfg.node}] 远程修不了、必须有人到现场的事："]
    out.extend(f"  - {s}" for s in cfg.onsite or ["（配置里还没写）"])
    out.append("")
    out.append("每条通道断了能从哪里修：")
    for ch in cfg.channels:
        reps = [r for r in cfg.repairs if r.target == ch.name]
        vias = "、".join(cfg.title(r.via) for r in reps) or "无（只能现场）"
        out.append(f"  {pad(ch.title, 16)} <- {vias}")
    return "\n".join(out)


def to_json(cfg: Config, rep: RoundReport) -> str:
    return json.dumps({
        "node": cfg.node, "ts": rep.ts, "verdict": rep.verdict,
        "boot_ts": rep.boot_ts, "rebooted": rep.rebooted,
        "channels": {n: {"state": rep.state(n), "latency_ms": r.latency_ms, "detail": r.detail}
                     for n, r in rep.results.items()},
        "events": [{"kind": k, "channel": c, "message": m} for k, c, m in rep.events],
        "repairs": [{"target": rt.target, "via": [r.via for r in rt.available],
                     "root_cause": rt.root_cause, "onsite": rt.needs_onsite} for rt in rep.routes],
    }, ensure_ascii=False, indent=2)
