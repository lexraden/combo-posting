from datetime import datetime, timezone

import pytest

from combo_posting.normalize import NormalizeResult
from combo_posting.platforms.base import PublishResult, Publisher
from combo_posting.queue import Queue, parse_at, parse_platforms
from combo_posting.runner import run_due

T0 = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)      # = 18:00 Дубай


class FakePublisher(Publisher):
    """Публикует 'по-настоящему', считает вызовы; может упасть после создания контейнера."""

    def __init__(self, name="ig", fail_after_container=False):
        self.name = name
        self.dry_run = False
        self.calls = 0
        self.containers = 0
        self.fail_after_container = fail_after_container

    def publish(self, file, caption, first_comment=None, state=None, on_progress=None):
        self.calls += 1
        if not state.get("container_id"):
            self.containers += 1
            state["container_id"] = f"c{self.containers}"
            on_progress(state)
        if self.fail_after_container:
            raise RuntimeError("boom")
        return PublishResult(self.name, media_id=f"m-{state['container_id']}",
                             container_id=state["container_id"])


def passthrough(path):
    return NormalizeResult(path, "none")


@pytest.fixture
def q(tmp_path):
    queue = Queue(tmp_path / "combo.db")
    yield queue
    queue.close()


def test_parse_at_local_dubai():
    assert parse_at("2026-09-29 18:00") == T0
    assert parse_at("2026-09-29T14:00:00Z") == T0


def test_parse_platforms():
    assert parse_platforms("ig, threads,instagram") == ["ig", "threads"]
    with pytest.raises(ValueError):
        parse_platforms("tiktok")


def test_due_respects_schedule(q):
    q.add("/a.mp4", "a", ["ig"], T0)
    q.add("/b.mp4", "b", ["ig"], T0.replace(hour=20))
    assert [p["file"] for p in q.due(T0)] == ["/a.mp4"]
    assert q.due(T0.replace(hour=13)) == []


def test_claim_is_atomic(q):
    pid = q.add("/a.mp4", "a", ["ig"], T0)
    assert q.claim(pid) is True
    assert q.claim(pid) is False            # второй воркер не возьмёт
    assert q.get(pid)["status"] == "processing"
    assert q.due(T0) == []


def test_run_is_idempotent(q):
    pid = q.add("/a.mp4", "hello", ["ig"], T0)
    ig = FakePublisher()
    run_due(q, T0, {"ig": ig}, normalizer=passthrough, out=lambda s: None)
    post = q.get(pid)
    assert post["status"] == "published"
    assert post["ig_media_id"] == "m-c1"
    assert post["ig_container_id"] == "c1"

    # повторные запуски ничего не публикуют
    assert run_due(q, T0, {"ig": ig}, normalizer=passthrough, out=lambda s: None) == []
    run_due(q, T0.replace(hour=23), {"ig": ig}, normalizer=passthrough, out=lambda s: None)
    assert ig.calls == 1
    # и retry опубликованного невозможен
    assert q.reset(pid) is False


def test_failure_keeps_container_and_retry_resumes(q):
    pid = q.add("/a.mp4", "hello", ["ig"], T0)
    bad = FakePublisher(fail_after_container=True)
    run_due(q, T0, {"ig": bad}, normalizer=passthrough, out=lambda s: None)
    post = q.get(pid)
    assert post["status"] == "failed"
    assert "boom" in post["error"]
    assert post["ig_container_id"] == "c1"

    # failed не подхватывается кроном сам
    assert run_due(q, T0, {"ig": bad}, normalizer=passthrough, out=lambda s: None) == []

    assert q.reset(pid)
    good = FakePublisher()
    run_due(q, T0, {"ig": good}, normalizer=passthrough, out=lambda s: None)
    post = q.get(pid)
    assert post["status"] == "published"
    assert good.containers == 0                  # контейнер переиспользован, не создан заново
    assert post["ig_media_id"] == "m-c1"
    assert post["error"] is None


def test_partial_publish_skips_done_platform_on_retry(q):
    pid = q.add("/a.mp4", "hello", ["ig", "threads"], T0)
    ig, th_bad = FakePublisher("ig"), FakePublisher("threads", fail_after_container=True)
    run_due(q, T0, {"ig": ig, "threads": th_bad}, normalizer=passthrough, out=lambda s: None)
    assert q.get(pid)["status"] == "failed"

    q.reset(pid)
    th_ok = FakePublisher("threads")
    run_due(q, T0, {"ig": ig, "threads": th_ok}, normalizer=passthrough, out=lambda s: None)
    post = q.get(pid)
    assert post["status"] == "published"
    assert ig.calls == 1                         # IG второй раз не публиковали
    assert post["results"]["threads"]["media_id"] == "m-c1"


def test_normalize_failure_marks_failed(q):
    from combo_posting.normalize import NormalizeError

    def broken(path):
        raise NormalizeError("длительность 1.0 с")

    pid = q.add("/a.mp4", "x", ["ig"], T0)
    ig = FakePublisher()
    run_due(q, T0, {"ig": ig}, normalizer=broken, out=lambda s: None)
    assert q.get(pid)["status"] == "failed"
    assert "normalize" in q.get(pid)["error"]
    assert ig.calls == 0
