"""SQLite-очередь постов.

Статусы поста:
  pending     — ждёт своего времени
  processing  — взят в работу (`combo run`); если процесс упал, остаётся здесь
                и НЕ подхватывается автоматически (чтобы не опубликовать дубль) —
                разбирать руками через `combo retry ID`
  published   — опубликован на всех платформах
  failed      — хотя бы одна платформа упала (текст в error)
  dry_run     — прогон без токенов: план напечатан, сетевых вызовов не было

Прогресс по платформам хранится в JSON-колонке results, плюс ig_container_id /
ig_media_id — повторный запуск пропускает уже сделанные шаги.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_DB = Path(os.environ.get(
    "COMBO_DB", Path(__file__).resolve().parent.parent / "data" / "combo.db"))
DEFAULT_TZ = os.environ.get("COMBO_TZ", "Asia/Dubai")

PLATFORM_ALIASES = {"ig": "ig", "instagram": "ig", "threads": "threads", "th": "threads"}
STATUSES = ("pending", "processing", "published", "failed", "dry_run")

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file            TEXT NOT NULL,
    caption         TEXT NOT NULL DEFAULT '',
    platforms       TEXT NOT NULL,
    scheduled_at    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    ig_media_id     TEXT,
    error           TEXT,
    ig_container_id TEXT,
    product         TEXT,
    first_comment   TEXT,
    results         TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_due ON posts(status, scheduled_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_at(value: str, tz: str = DEFAULT_TZ) -> datetime:
    """'2026-09-29 18:00' (локальное время, по умолчанию Дубай) или ISO со смещением."""
    value = value.strip()
    if value.lower() == "now":
        return utcnow()
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(timezone.utc)


def parse_platforms(value: str) -> list[str]:
    out = []
    for raw in value.split(","):
        raw = raw.strip().lower()
        if not raw:
            continue
        if raw not in PLATFORM_ALIASES:
            raise ValueError(f"неизвестная платформа: {raw} (есть: ig, threads)")
        name = PLATFORM_ALIASES[raw]
        if name not in out:
            out.append(name)
    if not out:
        raise ValueError("не указаны платформы")
    return out


class Queue:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- запись -----------------------------------------------------------
    def add(self, file: str, caption: str, platforms: list[str], scheduled_at: datetime,
            product: str | None = None, first_comment: str | None = None) -> int:
        now = to_iso(utcnow())
        cur = self.conn.execute(
            "INSERT INTO posts (file, caption, platforms, scheduled_at, status, product,"
            " first_comment, created_at, updated_at) VALUES (?,?,?,?, 'pending', ?,?,?,?)",
            (file, caption, ",".join(platforms), to_iso(scheduled_at), product,
             first_comment, now, now))
        return cur.lastrowid

    def update(self, post_id: int, **fields) -> None:
        if "results" in fields and not isinstance(fields["results"], str):
            fields["results"] = json.dumps(fields["results"], ensure_ascii=False)
        fields["updated_at"] = to_iso(utcnow())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE posts SET {cols} WHERE id = ?", (*fields.values(), post_id))

    def claim(self, post_id: int, from_status: str = "pending") -> bool:
        """Атомарно pending -> processing. False, если пост уже кто-то взял."""
        cur = self.conn.execute(
            "UPDATE posts SET status = 'processing', updated_at = ? WHERE id = ? AND status = ?",
            (to_iso(utcnow()), post_id, from_status))
        return cur.rowcount == 1

    def reset(self, post_id: int) -> bool:
        """Вернуть failed/processing/dry_run пост в pending (сделанные шаги сохраняются)."""
        cur = self.conn.execute(
            "UPDATE posts SET status = 'pending', error = NULL, updated_at = ?"
            " WHERE id = ? AND status != 'published'", (to_iso(utcnow()), post_id))
        return cur.rowcount == 1

    # --- чтение -----------------------------------------------------------
    def get(self, post_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return _row(row) if row else None

    def due(self, now: datetime | None = None) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM posts WHERE status = 'pending' AND scheduled_at <= ?"
            " ORDER BY scheduled_at, id", (to_iso(now or utcnow()),)).fetchall()
        return [_row(r) for r in rows]

    def list(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM posts WHERE status = ? ORDER BY scheduled_at, id", (status,))
        else:
            rows = self.conn.execute("SELECT * FROM posts ORDER BY scheduled_at, id")
        return [_row(r) for r in rows.fetchall()]


def _row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["platforms"] = [p for p in d["platforms"].split(",") if p]
    d["results"] = json.loads(d["results"] or "{}")
    return d
