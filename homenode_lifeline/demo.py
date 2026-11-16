"""离线演示：用模板配置 + 假探针，回放一段典型的三个月里会遇到的故事。"""
from __future__ import annotations

import asyncio
import copy

from .config import parse_config
from .defaults import TEMPLATE
from .engine import run_round
from .probes import ProbeResult
from .report import render, render_history
from .store import Store

T0 = 1_800_000_000.0  # 固定起点，方便复现
HOUR = 3600.0

# (距起点小时数, 场景说明, 挂掉的通道, 开机时刻相对起点的小时数)
SCENES = [
    (0, "一切正常", set(), -240),
    (1, "Tailscale 掉线（比如密钥过期），反向隧道还活着", {"tailscale"}, -240),
    (2, "家里停电，所有通道都断", {"tailscale", "tunnel"}, -240),
    (3, "来电后节点自启，Tailscale 还没登录回来", {"tailscale"}, 2.8),
    (4, "Tailscale 修好，全部恢复", set(), 2.8),
    (5, "你这边断网（比如在高铁上）", {"local"}, 2.8),
]

def _scene_prober(down: set[str]):
    async def prober(ch, cfg):
        if ch.name in down:
            return ProbeResult(ch.name, False, detail="模拟：连接超时")
        lat = {"tailscale": 38.0, "tunnel": 95.0}.get(ch.name, 60.0)
        detail = "直连" if ch.kind == "tailscale" else "ok"
        return ProbeResult(ch.name, True, lat, detail)
    return prober


def _scene_boot(boot_h: float):
    async def reader(sources, alive, cfg, now):
        for s in sources:
            if s.via in alive:
                return T0 + boot_h * HOUR, s.via
        return None, ""
    return reader


async def _play(show_all: bool) -> str:
    cfg = parse_config(copy.deepcopy(TEMPLATE))
    cfg.escalate_after = 2
    store = Store(":memory:")
    out: list[str] = []
    for h, desc, down, boot_h in SCENES:
        now = T0 + h * HOUR
        rep = await run_round(cfg, store, _scene_prober(down), _scene_boot(boot_h), now=now)
        out.append(f"=========== 场景：{desc} ===========")
        out.append(render(cfg, rep) if show_all or rep.verdict != "ok" or rep.events else "（正常，略）")
        out.append("")
    out.append("=========== history 命令的效果 ===========")
    out.append(render_history(cfg, store, hours=24, now=T0 + 5 * HOUR))
    store.close()
    return "\n".join(out)


def run_demo(show_all: bool = True) -> str:
    return asyncio.run(_play(show_all))
