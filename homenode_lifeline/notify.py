"""通知：macOS 通知中心，或自定义命令（标题和正文通过环境变量传入）。

notify_command 示例（推到手机）：
  curl -s -d "$LIFELINE_BODY" -H "Title: $LIFELINE_TITLE" https://ntfy.example.com/my-topic
"""
from __future__ import annotations

import os
import subprocess

from .config import Config
from .engine import RoundReport
from .report import HEADLINE


def summarize(cfg: Config, rep: RoundReport) -> tuple[str, str]:
    title = f"{cfg.node}: {HEADLINE[rep.verdict]}"
    parts = [m for _, _, m in rep.events]
    for name in rep.escalated:
        parts.append(f"{cfg.title(name)} 已连续断 {rep.streaks[name]} 轮")
    for route in rep.routes:
        if route.available:
            parts.append(f"修 {cfg.title(route.target)} 走 {cfg.title(route.available[0].via)}")
        elif route.needs_onsite:
            parts.append(f"{cfg.title(route.target)} 无法远程修")
    return title, "；".join(parts)[:400]


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def send(cfg: Config, title: str, body: str) -> bool:
    try:
        if cfg.notify_command:
            env = dict(os.environ, LIFELINE_TITLE=title, LIFELINE_BODY=body)
            subprocess.run(cfg.notify_command, shell=True, env=env, timeout=20, check=False)
            return True
        if cfg.notify == "macos":
            script = f"display notification {_applescript_str(body)} with title {_applescript_str(title)}"
            subprocess.run(["osascript", "-e", script], timeout=10, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    return False
