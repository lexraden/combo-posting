"""Обработка due-постов: normalize -> публикация по платформам -> статус в SQLite."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable

from . import captions
from .normalize import NormalizeError, normalize
from .platforms import Publisher, get_publisher
from .queue import Queue


def render_caption(post: dict, platform: str) -> str:
    return captions.render(post["caption"], platform, post.get("product"),
                           campaign=f"p{post['id']}", handle=os.environ.get("IG_HANDLE"))


def run_due(queue: Queue, now: datetime | None = None,
            publishers: dict[str, Publisher] | None = None,
            normalizer: Callable | None = None,
            out: Callable[[str], None] = print) -> list[dict]:
    """Публикует все pending-посты с scheduled_at <= now. Возвращает обновлённые посты."""
    publishers = publishers if publishers is not None else {}
    normalizer = normalizer or normalize
    done = []
    for post in queue.due(now):
        pid = post["id"]
        if not queue.claim(pid):            # взял параллельный `combo run`
            continue
        out(f"#{pid}: {post['file']} -> {','.join(post['platforms'])}")
        results = post["results"]
        errors: list[str] = []
        dry = real = 0

        try:
            file = normalizer(post["file"]).path
        except (NormalizeError, OSError) as e:
            queue.update(pid, status="failed", error=f"normalize: {e}")
            out(f"#{pid}: FAILED normalize: {e}")
            done.append(queue.get(pid))
            continue

        for platform in post["platforms"]:
            state = results.setdefault(platform, {})
            state.pop("error", None)
            if state.get("media_id"):
                real += 1
                continue
            pub = publishers.get(platform) or publishers.setdefault(platform, get_publisher(platform))

            def save(s, platform=platform):
                results[platform] = s
                extra = {}
                if platform == "ig":
                    extra = {"ig_container_id": s.get("container_id"),
                             "ig_media_id": s.get("media_id")}
                queue.update(pid, results=results, **extra)

            try:
                caption = render_caption(post, platform)
                res = pub.publish(file, caption, post.get("first_comment"),
                                  state=state, on_progress=save)
            except Exception as e:           # noqa: BLE001 — ошибку пишем в очередь
                state["error"] = str(e)
                save(state)
                errors.append(f"{platform}: {e}")
                out(f"#{pid}: {platform} FAILED: {e}")
                continue
            if res.dry_run:
                dry += 1
            else:
                real += 1
                state["media_id"] = res.media_id
                save(state)
                out(f"#{pid}: {platform} published media_id={res.media_id}")

        if errors:
            status = "failed"
        elif real:
            status = "published"
        else:
            status = "dry_run"
        queue.update(pid, status=status, error="; ".join(errors) or None, results=results)
        out(f"#{pid}: {status}")
        done.append(queue.get(pid))
    return done
