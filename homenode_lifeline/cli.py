"""命令行入口。

  python3 -m homenode_lifeline demo                 离线演示，不碰网络
  python3 -m homenode_lifeline init                 生成配置模板
  python3 -m homenode_lifeline check [--json]       探一轮，打印结论和修复路线
  python3 -m homenode_lifeline watch --interval 300 常驻，状态变化时通知
  python3 -m homenode_lifeline history --hours 72   可用率、重启记录、事件
  python3 -m homenode_lifeline onsite               只能现场处理的事和修复矩阵
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .defaults import TEMPLATE
from .engine import run_round, should_notify
from .notify import send, summarize
from .report import render, render_history, render_onsite, to_json
from .store import Store

HOME = Path(os.environ.get("LIFELINE_HOME", "~/.homenode-lifeline")).expanduser()
EXIT = {"ok": 0, "degraded": 1, "isolated": 2, "blind": 3}


def _paths(args) -> tuple[Path, Path]:
    cfg = Path(args.config).expanduser() if args.config else HOME / "config.json"
    db = Path(args.db).expanduser() if args.db else HOME / "lifeline.db"
    return cfg, db


def cmd_init(args) -> int:
    cfg_path, _ = _paths(args)
    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} 已存在，加 --force 覆盖")
        return 1
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(TEMPLATE, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成 {cfg_path}")
    print("把里面的 vps.example.com、minipc、minipc-ts、minipc-vps 改成你自己的主机和 ssh 别名，然后运行 check")
    return 0


async def _check_once(cfg, store, as_json: bool, notify: bool) -> int:
    rep = await run_round(cfg, store)
    print(to_json(cfg, rep) if as_json else render(cfg, rep))
    if notify and should_notify(rep):
        send(cfg, *summarize(cfg, rep))
    return EXIT[rep.verdict]


def cmd_check(args) -> int:
    cfg_path, db_path = _paths(args)
    cfg = load_config(cfg_path)
    store = Store(db_path)
    try:
        return asyncio.run(_check_once(cfg, store, args.json, args.notify))
    finally:
        store.close()


async def _watch(cfg, store, interval: float, max_rounds: int) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    n = 0
    while not stop.is_set():
        rep = await run_round(cfg, store)
        n += 1
        if should_notify(rep):
            print(render(cfg, rep), flush=True)
            print("-" * 40, flush=True)
            send(cfg, *summarize(cfg, rep))
        else:
            print(f"[{rep.verdict}] 第 {n} 轮，无变化", flush=True)
        if max_rounds and n >= max_rounds:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def cmd_watch(args) -> int:
    cfg_path, db_path = _paths(args)
    cfg = load_config(cfg_path)
    store = Store(db_path)
    print(f"开始守望 {cfg.node}，每 {args.interval:.0f} 秒一轮，Ctrl+C 退出", flush=True)
    try:
        asyncio.run(_watch(cfg, store, args.interval, args.rounds))
    finally:
        store.close()
    return 0


def cmd_history(args) -> int:
    cfg_path, db_path = _paths(args)
    cfg = load_config(cfg_path)
    store = Store(db_path)
    try:
        print(render_history(cfg, store, args.hours))
    finally:
        store.close()
    return 0


def cmd_onsite(args) -> int:
    print(render_onsite(load_config(_paths(args)[0])))
    return 0


def cmd_demo(args) -> int:
    from .demo import run_demo
    print(run_demo(show_all=args.all))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="homenode-lifeline", description="远程家用节点生命线监控")
    p.add_argument("--config", help="配置文件路径，默认 ~/.homenode-lifeline/config.json")
    p.add_argument("--db", help="SQLite 路径，默认 ~/.homenode-lifeline/lifeline.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="生成配置模板")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)
    s = sub.add_parser("check", help="探测一轮")
    s.add_argument("--json", action="store_true")
    s.add_argument("--notify", action="store_true", help="有变化时发通知（适合 cron/launchd）")
    s.set_defaults(func=cmd_check)
    s = sub.add_parser("watch", help="常驻定时探测")
    s.add_argument("--interval", type=float, default=300)
    s.add_argument("--rounds", type=int, default=0, help="跑几轮就退出，0 表示一直跑")
    s.set_defaults(func=cmd_watch)
    s = sub.add_parser("history", help="可用率与事件")
    s.add_argument("--hours", type=float, default=72)
    s.set_defaults(func=cmd_history)
    sub.add_parser("onsite", help="现场清单与修复矩阵").set_defaults(func=cmd_onsite)
    s = sub.add_parser("demo", help="离线演示")
    s.add_argument("--all", action="store_true", help="正常轮次也完整打印")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
