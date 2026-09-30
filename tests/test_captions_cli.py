from unittest import mock

import pytest
import requests

from combo_posting import captions, cli
from combo_posting.normalize import NormalizeResult
from combo_posting.queue import Queue


def test_add_utm_keeps_existing_query():
    url = captions.add_utm("https://x.io/p?ref=a", "instagram", "reel", "p7")
    assert url == "https://x.io/p?ref=a&utm_source=instagram&utm_medium=reel&utm_campaign=p7"


def test_render_ig_uses_bio_cta_and_hashtags():
    text = captions.render("Хук\nценность #dailyq", "ig", "dailyq", handle="dailyq.app", dm_keyword=True)
    assert "ссылка в профиле (@dailyq.app)" in text
    assert "«DAILY»" in text
    assert "http" not in text                       # в IG ссылки в подписи не кликабельны
    assert text.lower().count("#dailyq") == 1       # не дублируем хэштег
    assert "#productivity" in text


def test_render_threads_has_utm_link():
    text = captions.render("Хук", "threads", "dailyq", campaign="p3")
    assert "utm_source=threads" in text and "utm_campaign=p3" in text


def test_botify_deeplink():
    assert captions.product_link("botify", "ig", "p5").endswith("?start=ig_p5")


def test_limits():
    with pytest.raises(captions.CaptionError):
        captions.render("x" * 2201, "ig")
    with pytest.raises(captions.CaptionError):
        captions.render(" ".join(f"#t{i}" for i in range(31)), "ig")
    with pytest.raises(captions.CaptionError):
        captions.render("x" * 501, "threads")
    captions.render("x" * 600, "ig")


def test_cli_add_run_status_dry_run(tmp_path, monkeypatch, capsys):
    for k in ("IG_TOKEN", "IG_USER_ID", "COMBO_PUBLIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("combo_posting.runner.normalize", lambda p: NormalizeResult(p, "none"))
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"\0" * 10)
    db = str(tmp_path / "c.db")

    assert cli.main(["--db", db, "add", str(video), "--caption", "Хук", "--at", "2026-09-29 18:00",
                     "--to", "ig,threads", "--product", "dailyq", "--comment", "#more"]) == 0
    assert "#1 добавлен" in capsys.readouterr().out

    # ещё рано
    assert cli.main(["--db", db, "run", "--now", "2026-09-29 17:59"]) == 0
    assert "нет постов" in capsys.readouterr().out

    with mock.patch.object(requests.Session, "request", side_effect=AssertionError("net")) as net:
        assert cli.main(["--db", db, "run", "--now", "2026-09-29 18:05"]) == 0
    net.assert_not_called()
    out = capsys.readouterr().out
    assert "[DRY_RUN] Instagram" in out and "[DRY_RUN] Threads" in out
    assert "utm_campaign=p1" in out                 # threads-ссылка

    # идемпотентно: второй run ничего не делает
    assert cli.main(["--db", db, "run", "--now", "2026-09-29 18:10"]) == 0
    assert "нет постов" in capsys.readouterr().out

    assert cli.main(["--db", db, "status"]) == 0
    assert "dry_run" in capsys.readouterr().out
    q = Queue(db)
    assert q.get(1)["scheduled_at"] == "2026-09-29T14:00:00Z"
    q.close()


def test_cli_add_rejects_missing_file(tmp_path):
    assert cli.main(["--db", str(tmp_path / "c.db"), "add", str(tmp_path / "no.mp4"),
                     "--caption", "x"]) == 2


def test_cli_add_rejects_unknown_platform(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"0")
    assert cli.main(["--db", str(tmp_path / "c.db"), "add", str(f), "--caption", "x",
                     "--to", "tiktok"]) == 2
