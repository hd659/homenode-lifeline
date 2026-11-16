"""配置：通道、开机时长来源、修复预案、现场清单。

通道分三种角色：
- control：观测前提（本机网络、VPS 本身）。它挂了，依赖它的通道只能算「无法判断」。
- path：到家里节点的远控通道（Tailscale、反向隧道）。只要有一条活着，节点就还能远程救。
- service：节点上的服务（SSH、家宽出口）。挂了不代表节点挂了。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROLES = ("control", "path", "service")
KINDS = ("tcp", "cmd", "tailscale")
ANYWHERE = "*"  # 修复预案的 via 写 "*"：不依赖任何通道（比如 VPS 服务商网页控制台）


class ConfigError(ValueError):
    pass


@dataclass
class Channel:
    name: str
    kind: str
    role: str = "path"
    label: str = ""
    host: str = ""
    port: int = 0
    command: str = ""
    expect: str = ""
    requires: list[str] = field(default_factory=list)
    timeout_s: float | None = None

    @property
    def title(self) -> str:
        return self.label or self.name


@dataclass
class UptimeSource:
    via: str
    command: str


@dataclass
class Repair:
    target: str
    via: str
    steps: list[str]
    note: str = ""


@dataclass
class Config:
    node: str
    channels: list[Channel]
    uptime: list[UptimeSource] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    onsite: list[str] = field(default_factory=list)
    timeout_s: float = 8.0
    reboot_tolerance_s: float = 180.0
    escalate_after: int = 3
    tailscale_bin: str = "tailscale"
    notify: str = ""
    notify_command: str = ""

    def channel(self, name: str) -> Channel:
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise KeyError(name)

    def title(self, name: str) -> str:
        if name == ANYWHERE:
            return "无需通道"
        try:
            return self.channel(name).title
        except KeyError:
            return name


def _check_cycles(channels: list[Channel]) -> None:
    deps = {c.name: c.requires for c in channels}
    state: dict[str, int] = {}

    def visit(n: str, trail: list[str]) -> None:
        if state.get(n) == 1:
            raise ConfigError("通道依赖成环：" + " -> ".join(trail + [n]))
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in deps[n]:
            visit(d, trail + [n])
        state[n] = 2

    for name in deps:
        visit(name, [])


def parse_config(data: dict) -> Config:
    try:
        channels = [Channel(**c) for c in data.get("channels", [])]
        uptime = [UptimeSource(**u) for u in data.get("uptime", [])]
        repairs = [Repair(**r) for r in data.get("repairs", [])]
    except TypeError as e:
        raise ConfigError(f"配置字段不认识：{e}") from e
    if not channels:
        raise ConfigError("至少需要一个通道")
    names = [c.name for c in channels]
    if len(set(names)) != len(names):
        raise ConfigError("通道名重复")
    if not any(c.role == "path" for c in channels):
        raise ConfigError("至少需要一条 role=path 的远控通道")
    known = set(names)
    for c in channels:
        if c.kind not in KINDS:
            raise ConfigError(f"{c.name}: kind 只能是 {KINDS}")
        if c.role not in ROLES:
            raise ConfigError(f"{c.name}: role 只能是 {ROLES}")
        if c.kind == "tcp" and not (c.host and c.port):
            raise ConfigError(f"{c.name}: tcp 通道需要 host 和 port")
        if c.kind == "cmd" and not c.command:
            raise ConfigError(f"{c.name}: cmd 通道需要 command")
        if c.kind == "tailscale" and not c.host:
            raise ConfigError(f"{c.name}: tailscale 通道需要 host")
        for r in c.requires:
            if r not in known:
                raise ConfigError(f"{c.name}: requires 引用了不存在的通道 {r}")
    _check_cycles(channels)
    for u in uptime:
        if u.via not in known:
            raise ConfigError(f"uptime.via 引用了不存在的通道 {u.via}")
    for r in repairs:
        if r.target not in known:
            raise ConfigError(f"repairs.target 引用了不存在的通道 {r.target}")
        if r.via != ANYWHERE and r.via not in known:
            raise ConfigError(f"repairs.via 引用了不存在的通道 {r.via}")
        if r.via == r.target:
            raise ConfigError(f"{r.target}: 不能通过自己修自己")
    extra = {k: v for k, v in data.items() if k not in ("channels", "uptime", "repairs")}
    try:
        return Config(channels=channels, uptime=uptime, repairs=repairs, **extra)
    except TypeError as e:
        raise ConfigError(f"配置字段不认识：{e}") from e


def load_config(path: str | Path) -> Config:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"找不到配置文件 {p}，先运行 init 生成一份")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p} 不是合法 JSON：{e}") from e
    return parse_config(data)
