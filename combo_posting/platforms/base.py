"""Общий интерфейс публикаторов.

publish(file, caption, first_comment=None, state=None, on_progress=None) -> PublishResult

state — dict прогресса по этой платформе (хранится в posts.results[<platform>]).
Публикатор дописывает в него шаги (container_id, media_id, ...) и вызывает
on_progress(state) после каждого шага, чтобы очередь сохранила его в SQLite:
при падении повторный запуск продолжит с места, а не опубликует дубль.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


class PublishError(Exception):
    pass


@dataclass
class PublishResult:
    platform: str
    dry_run: bool = False
    media_id: str | None = None
    container_id: str | None = None
    comment_id: str | None = None
    plan: list[str] = field(default_factory=list)


class Publisher:
    name = "base"
    dry_run = True

    def publish(self, file: str, caption: str, first_comment: str | None = None,
                state: dict | None = None,
                on_progress: Callable[[dict], None] | None = None) -> PublishResult:
        raise NotImplementedError
