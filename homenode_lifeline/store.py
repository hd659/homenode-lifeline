"""SQLite 持久化：每轮探测、每个通道结果、开机时刻、事件流。"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    verdict TEXT NOT NULL,
    boot_ts REAL,
    boot_via TEXT
);
CREATE TABLE IF NOT EXISTS results (
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    channel TEXT NOT NULL,
    state TEXT NOT NULL,
    latency_ms REAL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    channel TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_channel ON results(channel, round_id);
"""


@dataclass
class Event:
    ts: float
    kind: str
    channel: str | None
    message: str


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def save_round(
        self,
        ts: float,
        verdict: str,
        states: dict[str, tuple[str, float | None, str]],
        boot_ts: float | None,
        boot_via: str,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO rounds(ts, verdict, boot_ts, boot_via) VALUES (?,?,?,?)",
            (ts, verdict, boot_ts, boot_via or None),
        )
        rid = cur.lastrowid
        self.db.executemany(
            "INSERT INTO results(round_id, channel, state, latency_ms, detail) VALUES (?,?,?,?,?)",
            [(rid, name, st, lat, det) for name, (st, lat, det) in states.items()],
        )
        self.db.commit()
        return rid

    def add_event(self, ts: float, kind: str, channel: str | None, message: str) -> None:
        self.db.execute(
            "INSERT INTO events(ts, kind, channel, message) VALUES (?,?,?,?)",
            (ts, kind, channel, message),
        )
        self.db.commit()

    def last_states(self) -> dict[str, str]:
        row = self.db.execute("SELECT id FROM rounds ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return {}
        return dict(
            self.db.execute("SELECT channel, state FROM results WHERE round_id=?", (row[0],))
        )

    def last_verdict(self) -> str | None:
        row = self.db.execute("SELECT verdict FROM rounds ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def last_boot(self) -> float | None:
        row = self.db.execute(
            "SELECT boot_ts FROM rounds WHERE boot_ts IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def last_seen_alive(self) -> float | None:
        """最近一次至少有一条远控通道活着的时间。"""
        row = self.db.execute(
            "SELECT ts FROM rounds WHERE verdict IN ('ok','degraded') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def down_streak(self, channel: str) -> int:
        """该通道从最近一轮往回数，连续 down 了几轮。"""
        rows = self.db.execute(
            "SELECT state FROM results WHERE channel=? ORDER BY round_id DESC LIMIT 500",
            (channel,),
        ).fetchall()
        n = 0
        for (st,) in rows:
            if st != "down":
                break
            n += 1
        return n

    def down_since(self, channel: str) -> float | None:
        """该通道这一段连续断开是从哪一轮开始的。"""
        row = self.db.execute(
            "SELECT MAX(r.id) FROM results x JOIN rounds r ON r.id=x.round_id "
            "WHERE x.channel=? AND x.state='up'",
            (channel,),
        ).fetchone()
        last_up = row[0] or 0
        row = self.db.execute(
            "SELECT MIN(r.ts) FROM results x JOIN rounds r ON r.id=x.round_id "
            "WHERE x.channel=? AND x.state='down' AND r.id>?",
            (channel, last_up),
        ).fetchone()
        return row[0]

    def availability(self, since_ts: float) -> dict[str, tuple[int, int]]:
        """每个通道在时间窗口内的 (up 轮数, 有效探测轮数)，跳过的轮次不计。"""
        rows = self.db.execute(
            "SELECT x.channel, SUM(x.state='up'), SUM(x.state IN ('up','down')) "
            "FROM results x JOIN rounds r ON r.id=x.round_id WHERE r.ts>=? GROUP BY x.channel",
            (since_ts,),
        ).fetchall()
        return {ch: (int(up or 0), int(total or 0)) for ch, up, total in rows}

    def events(self, limit: int = 30, kinds: tuple[str, ...] = ()) -> list[Event]:
        sql = "SELECT ts, kind, channel, message FROM events"
        args: list = []
        if kinds:
            sql += " WHERE kind IN (%s)" % ",".join("?" * len(kinds))
            args.extend(kinds)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [Event(*r) for r in self.db.execute(sql, args)]

    def round_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
