"""CLI: combo add | run | status | retry | check.

  combo add reel.mp4 --caption "Хук" --at "2026-09-29 18:00" --to ig,threads --product dailyq
  combo run            # крон раз в 5 мин: */5 * * * * combo run
  combo status [ID]
  combo retry ID       # failed/processing/dry_run -> pending (сделанные шаги сохраняются)
  combo check reel.mp4 # ffprobe-проверка по спеке IG
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import captions
from .normalize import NormalizeError, check, plan
from .queue import DEFAULT_DB, DEFAULT_TZ, STATUSES, Queue, parse_at, parse_platforms
from .runner import run_due


def cmd_add(q: Queue, a) -> int:
    file = Path(a.file).expanduser().resolve()
    if not file.is_file():
        print(f"нет файла: {file}", file=sys.stderr)
        return 2
    platforms = parse_platforms(a.to)
    at = parse_at(a.at, a.tz)
    for p in platforms:                      # ошибки подписи — сразу, а не в кроне
        captions.render(a.caption, p, a.product)
    pid = q.add(str(file), a.caption, platforms, at, product=a.product, first_comment=a.comment)
    print(f"#{pid} добавлен: {file.name} -> {','.join(platforms)} в {at:%Y-%m-%d %H:%M} UTC")
    return 0


def cmd_run(q: Queue, a) -> int:
    now = parse_at(a.now, a.tz) if a.now else None
    posts = run_due(q, now)
    if not posts:
        print("нет постов к публикации")
    return 1 if any(p["status"] == "failed" for p in posts) else 0


def cmd_status(q: Queue, a) -> int:
    if a.id:
        post = q.get(a.id)
        if not post:
            print(f"нет поста #{a.id}", file=sys.stderr)
            return 2
        print(json.dumps(post, ensure_ascii=False, indent=2))
        return 0
    posts = q.list(a.status)
    if not posts:
        print("очередь пуста")
    for p in posts:
        line = (f"#{p['id']:<4} {p['status']:<10} {p['scheduled_at']}  "
                f"{','.join(p['platforms']):<10} {Path(p['file']).name}")
        if p["ig_media_id"]:
            line += f"  ig={p['ig_media_id']}"
        if p["error"]:
            line += f"  ERR: {p['error'][:120]}"
        print(line)
    return 0


def cmd_retry(q: Queue, a) -> int:
    if q.reset(a.id):
        print(f"#{a.id} -> pending")
        return 0
    print(f"#{a.id}: нет такого или уже published", file=sys.stderr)
    return 2


def cmd_check(q: Queue | None, a) -> int:
    try:
        issues = check(a.file)
    except NormalizeError as e:
        print(e, file=sys.stderr)
        return 2
    for i in issues:
        print(f"  ✗ {i}")
    print(f"{a.file}: {'OK по спеке IG' if not issues else 'действие: ' + plan(issues)}")
    return 0 if not issues else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="combo", description="combo-posting: IG Reels + Threads")
    ap.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite (по умолч. {DEFAULT_DB})")
    ap.add_argument("--tz", default=DEFAULT_TZ, help="таймзона для --at/--now (Asia/Dubai)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="поставить пост в очередь")
    p.add_argument("file")
    p.add_argument("--caption", required=True)
    p.add_argument("--at", default="now", help="'2026-09-29 18:00' (локальное) | ISO | now")
    p.add_argument("--to", default="ig", help="ig,threads")
    p.add_argument("--product", choices=sorted(captions.PRODUCTS), help="CTA + UTM на продукт")
    p.add_argument("--comment", help="первый комментарий")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("run", help="опубликовать due-посты")
    p.add_argument("--now", help="переопределить текущее время (для тестов)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("status", help="состояние очереди")
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--status", choices=STATUSES)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("retry", help="вернуть пост в pending")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_retry)

    p = sub.add_parser("check", help="проверить файл по спеке IG")
    p.add_argument("file")
    p.set_defaults(fn=cmd_check, no_db=True)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if getattr(a, "no_db", False):
        return a.fn(None, a)
    q = Queue(a.db)
    try:
        return a.fn(q, a)
    except (ValueError, captions.CaptionError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2
    finally:
        q.close()


if __name__ == "__main__":
    sys.exit(main())
