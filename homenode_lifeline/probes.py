"""异步探针：tcp 连通、外部命令、tailscale ping；以及开机时长输出解析。

所有探针都有硬超时，超时会杀掉子进程，避免 ssh 卡住把整轮探测拖死。
"""
from __future__ import annotations

import asyncio
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Channel, Config, UptimeSource


@dataclass
class ProbeResult:
    name: str
    ok: bool
    latency_ms: float | None = None
    detail: str = ""
    skipped: bool = False  # 前置通道挂了，没去探


async def run_command(command: str, timeout_s: float) -> tuple[int | None, str]:
    """跑 shell 命令，返回 (退出码, 合并输出)。超时返回 (None, 'timeout')。"""
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return None, f"超时 {timeout_s:.0f}s"
    return proc.returncode, out.decode("utf-8", "replace").strip()


def _tail(text: str, n: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else "..." + text[-n:]


async def probe_tcp(ch: Channel, timeout_s: float) -> ProbeResult:
    t0 = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ch.host, ch.port), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        return ProbeResult(ch.name, False, detail=f"连接 {ch.host}:{ch.port} 超时")
    except OSError as e:
        return ProbeResult(ch.name, False, detail=f"连接 {ch.host}:{ch.port} 失败：{e.strerror or e}")
    ms = (time.perf_counter() - t0) * 1000
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return ProbeResult(ch.name, True, round(ms, 1), f"{ch.host}:{ch.port} 可连")


async def probe_cmd(ch: Channel, timeout_s: float) -> ProbeResult:
    t0 = time.perf_counter()
    code, out = await run_command(ch.command, timeout_s)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    if code is None:
        return ProbeResult(ch.name, False, detail=out)
    if code != 0:
        return ProbeResult(ch.name, False, ms, f"退出码 {code}：{_tail(out)}")
    if ch.expect and ch.expect not in out:
        return ProbeResult(ch.name, False, ms, f"输出里没有「{ch.expect}」：{_tail(out)}")
    return ProbeResult(ch.name, True, ms, _tail(out, 60) or "ok")


async def probe_tailscale(ch: Channel, timeout_s: float, binary: str) -> ProbeResult:
    """tailscale ping 走的是 Tailscale 自己的通道，能区分直连还是走 DERP 中继。"""
    secs = max(1, int(timeout_s) - 1)
    cmd = f"{shlex.quote(binary)} ping -c 1 --timeout {secs}s {shlex.quote(ch.host)}"
    t0 = time.perf_counter()
    code, out = await run_command(cmd, timeout_s)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    if code is None:
        return ProbeResult(ch.name, False, detail=out)
    pong = re.search(r"^pong from .*$", out, re.MULTILINE)
    if code != 0 or not pong:
        return ProbeResult(ch.name, False, ms, _tail(out) or f"退出码 {code}")
    out = pong.group(0)
    m = re.search(r"in\s+([\d.]+)\s*ms", out)
    lat = float(m.group(1)) if m else ms
    route = "DERP 中继" if "via DERP" in out else "直连"
    return ProbeResult(ch.name, True, lat, route)


async def probe(ch: Channel, cfg: Config) -> ProbeResult:
    timeout_s = ch.timeout_s or cfg.timeout_s
    try:
        if ch.kind == "tcp":
            return await probe_tcp(ch, timeout_s)
        if ch.kind == "tailscale":
            return await probe_tailscale(ch, timeout_s, cfg.tailscale_bin)
        return await probe_cmd(ch, timeout_s)
    except Exception as e:  # 探针自身出错也算挂，不能让整轮崩掉
        return ProbeResult(ch.name, False, detail=f"探针异常：{type(e).__name__}: {e}")


_NUM = re.compile(r"^\s*(\d+(?:\.\d+)?)")
_DHMS = re.compile(r"(?:(\d+)\s*d\w*[ ,]*)?(\d+):(\d{2})(?::(\d{2}))?")


def parse_uptime(text: str, now: float) -> float | None:
    """把开机时长命令的输出换算成开机时刻（epoch 秒）。

    支持三种输出：
    - 纯数字秒数（Linux /proc/uptime 第一列、PowerShell 算出来的 TotalSeconds）
    - ISO 时间戳，表示开机时刻（PowerShell LastBootUpTime 转 ISO、uptime -s）
    - 「3 days, 04:05:06」这类天数加时分秒
    """
    text = (text or "").strip()
    if not text:
        return None
    first = text.splitlines()[0].strip()
    m = _NUM.match(first)
    if m and re.fullmatch(r"\d+(?:\.\d+)?(\s+\d+(?:\.\d+)?)?", first):
        return now - float(m.group(1))
    try:
        dt = datetime.fromisoformat(first.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    m = _DHMS.search(first)
    if m:
        d, h, mi, s = (int(x) if x else 0 for x in m.groups())
        return now - (d * 86400 + h * 3600 + mi * 60 + s)
    return None


async def read_boot_time(
    sources: list[UptimeSource], alive: set[str], cfg: Config, now: float
) -> tuple[float | None, str]:
    """按顺序从还活着的通道读开机时长，返回 (开机时刻, 来源通道)。"""
    for src in sources:
        if src.via not in alive:
            continue
        code, out = await run_command(src.command, cfg.timeout_s)
        if code == 0:
            boot = parse_uptime(out, now)
            if boot is not None:
                return boot, src.via
    return None, ""
