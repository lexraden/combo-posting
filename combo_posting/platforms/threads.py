"""Threads — заглушка с интерфейсом InstagramPublisher.

Реальный flow (TODO): POST graph.threads.net/v1.0/<THREADS_USER_ID>/threads
media_type=VIDEO video_url=... text=... -> poll status -> POST .../threads_publish.
Пока всегда DRY_RUN: печатает план, сетевых вызовов нет.
"""
from __future__ import annotations

import os
from typing import Callable

from .base import PublishResult, Publisher


class ThreadsPublisher(Publisher):
    name = "threads"
    base_url = "https://graph.threads.net/v1.0"

    def __init__(self, token: str | None = None, user_id: str | None = None, *,
                 out: Callable[[str], None] = print, **_ignored):
        self.token = token if token is not None else os.environ.get("THREADS_TOKEN")
        self.user_id = user_id if user_id is not None else os.environ.get("THREADS_USER_ID")
        self.dry_run = True                     # реализации нет — сеть не трогаем
        self.out = out

    def plan(self, file: str, caption: str, first_comment: str | None = None) -> list[str]:
        uid = self.user_id or "<THREADS_USER_ID>"
        steps = [
            f"POST {self.base_url}/{uid}/threads media_type=VIDEO video_url=<public url of {file}>",
            f"GET  {self.base_url}/<container_id>?fields=status — поллинг до FINISHED",
            f"POST {self.base_url}/{uid}/threads_publish creation_id=<container_id>",
        ]
        if first_comment:
            steps.append(f"POST {self.base_url}/{uid}/threads reply_to_id=<media_id> "
                         f"text={first_comment!r}")
        return steps

    def publish(self, file: str, caption: str, first_comment: str | None = None,
                state: dict | None = None,
                on_progress: Callable[[dict], None] | None = None) -> PublishResult:
        steps = self.plan(file, caption, first_comment)
        self.out(f"[DRY_RUN] Threads (заглушка): {file}")
        self.out("  text: " + caption.replace("\n", "\n        "))
        for s in steps:
            self.out(f"  - {s}")
        return PublishResult(self.name, dry_run=True, plan=steps)
