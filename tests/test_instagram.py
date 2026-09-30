"""Instagram publisher: мок-ответы Graph API (requests замокан), dry-run."""
from unittest import mock

import pytest
import requests

from combo_posting.platforms.base import PublishError
from combo_posting.platforms.instagram import GraphAPIError, InstagramPublisher, QuotaExceeded

BASE = "https://graph.instagram.com/v23.0"


def resp(status=200, payload=None):
    r = mock.Mock(spec=requests.Response)
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = str(payload)
    return r


class FakeGraph:
    """Маршрутизатор мок-ответов: (METHOD, url-suffix) -> очередь ответов."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append((method, url, dict(params or {}), headers))
        for (m, suffix), queue in self.routes.items():
            if m == method and url.endswith(suffix):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"неожиданный вызов {method} {url}")

    def urls(self, method=None):
        return [u for m, u, *_ in self.calls if method in (None, m)]


def ok_routes(over=None):
    routes = {
        ("GET", "/17841/content_publishing_limit"):
            [resp(payload={"data": [{"quota_usage": 2, "config": {"quota_total": 100}}]})],
        ("POST", "/17841/media"): [resp(payload={"id": "C1", "uri": "https://rupload/..."})],
        ("POST", "/ig-api-upload/v23.0/C1"): [resp(payload={"success": True})],
        ("GET", "/C1"): [resp(payload={"status_code": "IN_PROGRESS"}),
                         resp(payload={"status_code": "IN_PROGRESS"}),
                         resp(payload={"status_code": "FINISHED"})],
        ("POST", "/17841/media_publish"): [resp(payload={"id": "M1"})],
        ("POST", "/M1/comments"): [resp(payload={"id": "CM1"})],
    }
    routes.update(over or {})
    return routes


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "reel.mp4"
    p.write_bytes(b"\x00" * 1024)
    return str(p)


def make(graph, **kw):
    kw.setdefault("public_base_url", "")
    return InstagramPublisher("TOKEN", "17841", session=graph, sleep=lambda s: None,
                              out=lambda s: None, **kw)


def test_full_flow_resumable(video):
    g = FakeGraph(ok_routes())
    saved = []
    state = {}
    res = make(g).publish(video, "caption", "first!", state=state,
                          on_progress=lambda s: saved.append(dict(s)))

    assert (res.media_id, res.container_id, res.comment_id) == ("M1", "C1", "CM1")
    assert not res.dry_run
    assert [(m, u.rsplit("/", 2)[-2:]) for m, u, *_ in g.calls] == [
        ("GET", ["17841", "content_publishing_limit"]),
        ("POST", ["17841", "media"]),
        ("POST", ["v23.0", "C1"]),
        ("GET", ["v23.0", "C1"]), ("GET", ["v23.0", "C1"]), ("GET", ["v23.0", "C1"]),
        ("POST", ["17841", "media_publish"]),
        ("POST", ["M1", "comments"]),
    ]
    create = g.calls[1][2]
    assert create["media_type"] == "REELS" and create["upload_type"] == "resumable"
    assert create["caption"] == "caption" and create["access_token"] == "TOKEN"
    upload_headers = g.calls[2][3]
    assert upload_headers == {"Authorization": "OAuth TOKEN", "offset": "0", "file_size": "1024"}
    assert "access_token" not in g.calls[2][2]           # токен в rupload только в заголовке
    assert g.calls[6][2]["creation_id"] == "C1"
    assert g.calls[7][2]["message"] == "first!"
    # прогресс сохранялся по шагам: контейнер -> загрузка -> media_id -> коммент
    assert saved[0]["container_id"] == "C1" and not saved[0]["uploaded"]
    assert saved[-1] == {"container_id": "C1", "uploaded": True, "media_id": "M1", "comment_id": "CM1"}


def test_video_url_mode_skips_upload(video):
    g = FakeGraph(ok_routes())
    make(g, public_base_url="https://cdn.example.com/reels/").publish(video, "c")
    create = g.calls[1][2]
    assert create["video_url"] == "https://cdn.example.com/reels/reel.mp4"
    assert "upload_type" not in create
    assert not any("rupload" in u for u in g.urls())


def test_resume_from_saved_container(video):
    g = FakeGraph(ok_routes())
    state = {"container_id": "C1", "uploaded": True}
    res = make(g).publish(video, "c", state=state)
    assert res.media_id == "M1"
    assert not any(u.endswith("/17841/media") for u in g.urls("POST"))


def test_already_published_makes_no_calls(video):
    g = FakeGraph({})
    res = make(g).publish(video, "c", state={"container_id": "C1", "media_id": "M0"})
    assert res.media_id == "M0" and g.calls == []


def test_container_error_is_not_retried(video):
    g = FakeGraph(ok_routes({("GET", "/C1"): [resp(payload={"status_code": "ERROR",
                                                               "status": "Error: 2207026"})]}))
    with pytest.raises(PublishError, match="ERROR"):
        make(g).publish(video, "c")
    assert not any("media_publish" in u for u in g.urls())


def test_poll_timeout(video):
    g = FakeGraph(ok_routes({("GET", "/C1"): [resp(payload={"status_code": "IN_PROGRESS"})]}))
    with pytest.raises(PublishError, match="таймаут"):
        make(g, poll_timeout=0).publish(video, "c")


def test_quota_exceeded(video):
    g = FakeGraph(ok_routes({("GET", "/17841/content_publishing_limit"): [
        resp(payload={"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]})]}))
    with pytest.raises(QuotaExceeded):
        make(g).publish(video, "c")
    assert len(g.calls) == 1


def test_graph_error_payload(video):
    g = FakeGraph(ok_routes({("POST", "/17841/media"): [
        resp(400, {"error": {"message": "Invalid OAuth access token", "code": 190}})]}))
    with pytest.raises(GraphAPIError, match="Invalid OAuth") as ei:
        make(g).publish(video, "c")
    assert ei.value.status == 400


def test_5xx_is_retried_on_publish(video):
    g = FakeGraph(ok_routes({("POST", "/17841/media_publish"): [
        resp(503, {}), resp(payload={"id": "M9"})]}))
    assert make(g).publish(video, "c").media_id == "M9"
    assert len([u for u in g.urls("POST") if u.endswith("media_publish")]) == 2


def test_comment_failure_does_not_fail_post(video):
    g = FakeGraph(ok_routes({("POST", "/M1/comments"): [
        resp(400, {"error": {"message": "comments disabled"}})]}))
    state = {}
    res = make(g).publish(video, "c", "first", state=state)
    assert res.media_id == "M1" and res.comment_id is None
    assert "comments disabled" in state["comment_error"]


# --- dry run ------------------------------------------------------------------
def test_dry_run_without_token_no_network(video, monkeypatch):
    monkeypatch.delenv("IG_TOKEN", raising=False)
    monkeypatch.delenv("IG_USER_ID", raising=False)
    monkeypatch.delenv("COMBO_PUBLIC_BASE_URL", raising=False)
    lines = []
    with mock.patch.object(requests.Session, "request",
                           side_effect=AssertionError("network!")) as net:
        pub = InstagramPublisher(out=lines.append)
        assert pub.dry_run is True
        res = pub.publish(video, "Хук\nстрока 2", "первый коммент")
    net.assert_not_called()
    assert res.dry_run and res.media_id is None
    text = "\n".join(lines)
    assert "[DRY_RUN] Instagram" in text
    assert "media_type=REELS" in text and "upload_type=resumable" in text
    assert "media_publish" in text and "comments" in text and "первый коммент" in text
    assert "<IG_USER_ID>" in text
    assert "Хук" in text


def test_dry_run_forced_by_env(video, monkeypatch):
    monkeypatch.setenv("IG_TOKEN", "t")
    monkeypatch.setenv("IG_USER_ID", "1")
    assert InstagramPublisher().dry_run is False
    monkeypatch.setenv("COMBO_DRY_RUN", "1")
    assert InstagramPublisher().dry_run is True


def test_dry_run_guard_blocks_calls():
    pub = InstagramPublisher(token="", user_id="", out=lambda s: None)
    with pytest.raises(AssertionError):
        pub.check_quota()
