"""Instagram Reels через Instagram Graph API (instagram_pipeline.md §1.6).

create container -> (resumable upload) -> poll status_code -> media_publish -> первый коммент.

Env: IG_TOKEN, IG_USER_ID (обязательны для реальной публикации),
     IG_API_VERSION (v23.0), IG_GRAPH_BASE (https://graph.instagram.com),
     COMBO_PUBLIC_BASE_URL — если задан, видео отдаётся по video_url=<base>/<имя файла>;
     иначе resumable upload в rupload.facebook.com (публичный хостинг не нужен),
     COMBO_DRY_RUN=1 — принудительный dry-run.
Без IG_TOKEN/IG_USER_ID -> DRY_RUN: печатает план, сетевых вызовов нет.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import requests

from .base import PublishError, PublishResult, Publisher

TERMINAL_BAD = {"ERROR", "EXPIRED"}
RETRY_STATUS = {500, 502, 503, 504}


class GraphAPIError(PublishError):
    def __init__(self, message: str, status: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class QuotaExceeded(PublishError):
    pass


class InstagramPublisher(Publisher):
    name = "ig"

    def __init__(self, token: str | None = None, user_id: str | None = None, *,
                 api_version: str | None = None, base_url: str | None = None,
                 public_base_url: str | None = None, dry_run: bool | None = None,
                 session: requests.Session | None = None,
                 poll_interval: float = 15, poll_timeout: float = 600,
                 retries: int = 3, backoff: float = 2,
                 sleep: Callable[[float], None] = time.sleep,
                 out: Callable[[str], None] = print):
        env = os.environ
        self.token = token if token is not None else env.get("IG_TOKEN")
        self.user_id = user_id if user_id is not None else env.get("IG_USER_ID")
        self.api_version = api_version or env.get("IG_API_VERSION", "v23.0")
        self.base_url = (base_url or env.get("IG_GRAPH_BASE", "https://graph.instagram.com")).rstrip("/")
        self.rupload_url = "https://rupload.facebook.com/ig-api-upload"
        self.public_base_url = public_base_url if public_base_url is not None \
            else env.get("COMBO_PUBLIC_BASE_URL")
        if dry_run is None:
            dry_run = not (self.token and self.user_id) or env.get("COMBO_DRY_RUN") == "1"
        self.dry_run = dry_run
        self.session = session or requests.Session()
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self.out = out

    # --- HTTP ---------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}/{self.api_version}/{path.lstrip('/')}"

    def _call(self, method: str, url: str, *, params: dict | None = None,
              data=None, headers: dict | None = None, retry: bool = True) -> dict:
        if self.dry_run:
            raise AssertionError("сетевой вызов в DRY_RUN")
        params = dict(params or {})
        if "rupload" not in url:
            params["access_token"] = self.token
        attempts = self.retries if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(method, url, params=params, data=data,
                                            headers=headers, timeout=60)
            except requests.RequestException as e:
                if attempt == attempts:
                    raise GraphAPIError(f"{method} {url}: {e}") from e
                self.sleep(self.backoff ** attempt)
                continue
            if resp.status_code in RETRY_STATUS and attempt < attempts:
                self.sleep(self.backoff ** attempt)
                continue
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            if resp.status_code >= 400 or "error" in payload:
                err = payload.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise GraphAPIError(f"{method} {url.split('?')[0]}: HTTP {resp.status_code}: "
                                    f"{msg or resp.text[:300]}", resp.status_code, payload)
            return payload
        raise GraphAPIError(f"{method} {url}: исчерпаны ретраи")  # pragma: no cover

    # --- шаги ---------------------------------------------------------------
    def check_quota(self) -> dict:
        data = self._call("GET", self._url(f"{self.user_id}/content_publishing_limit"),
                          params={"fields": "quota_usage,config"})
        item = (data.get("data") or [{}])[0]
        usage = item.get("quota_usage", 0)
        total = (item.get("config") or {}).get("quota_total")
        if total is not None and usage >= total:
            raise QuotaExceeded(f"лимит публикаций исчерпан: {usage}/{total} за 24 ч")
        return item

    def create_container(self, file: str, caption: str) -> tuple[str, str | None]:
        """-> (container_id, upload_uri|None). Без public_base_url — resumable."""
        params = {"media_type": "REELS", "caption": caption, "share_to_feed": "true"}
        if self.public_base_url:
            params["video_url"] = f"{self.public_base_url.rstrip('/')}/{Path(file).name}"
        else:
            params["upload_type"] = "resumable"
        # не ретраим слепо: лишний контейнер безвреден, но плодить их не нужно
        data = self._call("POST", self._url(f"{self.user_id}/media"), params=params, retry=False)
        if "id" not in data:
            raise GraphAPIError(f"нет id контейнера в ответе: {data}")
        return data["id"], data.get("uri")

    def upload(self, container_id: str, file: str) -> None:
        size = os.path.getsize(file)
        with open(file, "rb") as f:
            body = f.read()
        data = self._call(
            "POST", f"{self.rupload_url}/{self.api_version}/{container_id}", data=body,
            headers={"Authorization": f"OAuth {self.token}", "offset": "0",
                     "file_size": str(size)})
        if data.get("success") is False:
            raise GraphAPIError(f"rupload не принял файл: {data}")

    def wait_ready(self, container_id: str) -> str:
        deadline = time.monotonic() + self.poll_timeout
        while True:
            data = self._call("GET", self._url(container_id), params={"fields": "status_code,status"})
            code = data.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return code
            if code in TERMINAL_BAD:
                # на ERROR не повторяем вслепую — пишем ошибку
                raise PublishError(f"контейнер {container_id}: {code} {data.get('status', '')}".strip())
            if time.monotonic() >= deadline:
                raise PublishError(f"контейнер {container_id}: таймаут ожидания ({code})")
            self.sleep(self.poll_interval)

    def publish_container(self, container_id: str) -> str:
        data = self._call("POST", self._url(f"{self.user_id}/media_publish"),
                          params={"creation_id": container_id})
        if "id" not in data:
            raise GraphAPIError(f"нет media_id в ответе: {data}")
        return data["id"]

    def comment(self, media_id: str, text: str) -> str:
        data = self._call("POST", self._url(f"{media_id}/comments"), params={"message": text})
        return data.get("id", "")

    # --- план / публикация ---------------------------------------------------
    def plan(self, file: str, caption: str, first_comment: str | None = None) -> list[str]:
        uid = self.user_id or "<IG_USER_ID>"
        src = (f"video_url={self.public_base_url.rstrip('/')}/{Path(file).name}"
               if self.public_base_url else "upload_type=resumable")
        steps = [
            f"GET  {self._url(f'{uid}/content_publishing_limit')} — проверка квоты",
            f"POST {self._url(f'{uid}/media')} media_type=REELS share_to_feed=true {src}",
        ]
        if not self.public_base_url:
            steps.append(f"POST {self.rupload_url}/{self.api_version}/<container_id> "
                         f"<- {file} ({_size(file)})")
        steps += [
            f"GET  {self._url('<container_id>')}?fields=status_code — поллинг до FINISHED",
            f"POST {self._url(f'{uid}/media_publish')} creation_id=<container_id>",
        ]
        if first_comment:
            steps.append(f"POST {self._url('<media_id>/comments')} message={first_comment!r}")
        return steps

    def publish(self, file: str, caption: str, first_comment: str | None = None,
                state: dict | None = None,
                on_progress: Callable[[dict], None] | None = None) -> PublishResult:
        state = state if state is not None else {}
        save = on_progress or (lambda s: None)

        if self.dry_run:
            steps = self.plan(file, caption, first_comment)
            self.out(f"[DRY_RUN] Instagram: {file}")
            self.out("  caption: " + caption.replace("\n", "\n           "))
            for s in steps:
                self.out(f"  - {s}")
            return PublishResult(self.name, dry_run=True, plan=steps)

        if state.get("media_id"):              # уже опубликовано — идемпотентно
            return PublishResult(self.name, media_id=state["media_id"],
                                 container_id=state.get("container_id"),
                                 comment_id=state.get("comment_id"))

        self.check_quota()
        if not state.get("container_id"):
            cid, _ = self.create_container(file, caption)
            state.update(container_id=cid, uploaded=bool(self.public_base_url))
            save(state)
        cid = state["container_id"]
        if not state.get("uploaded"):
            self.upload(cid, file)
            state["uploaded"] = True
            save(state)

        if self.wait_ready(cid) == "PUBLISHED":
            raise PublishError(f"контейнер {cid} уже PUBLISHED, но media_id неизвестен — "
                               "проверь профиль вручную, повторно не публикую")
        media_id = self.publish_container(cid)
        state["media_id"] = media_id
        save(state)

        comment_id = None
        if first_comment and not state.get("comment_id"):
            try:
                comment_id = self.comment(media_id, first_comment)
                state["comment_id"] = comment_id
            except PublishError as e:          # коммент не критичен: пост уже вышел
                state["comment_error"] = str(e)
            save(state)
        return PublishResult(self.name, media_id=media_id, container_id=cid,
                             comment_id=comment_id or state.get("comment_id"))


def _size(file: str) -> str:
    try:
        return f"{os.path.getsize(file) / 1e6:.1f} MB"
    except OSError:
        return "файл не найден"
