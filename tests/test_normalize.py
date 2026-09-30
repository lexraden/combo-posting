import pytest

from combo_posting import normalize as nz
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def codes(path):
    return {i.code for i in nz.check(path)}


def test_spec_compliant_file_left_as_is(video_factory):
    src = video_factory(faststart=True)
    assert codes(src) == set()
    res = nz.normalize(src)
    assert res.action == "none" and res.path == str(src)


def test_moov_at_end_is_detected_and_fixed_by_remux(video_factory):
    src = video_factory(faststart=False)
    assert not nz.moov_at_start(src)
    # ffmpeg по умолчанию ещё и edit list пишет (задержка B-кадров) — тоже чинится ремуксом
    assert "moov_at_end" in codes(src) and codes(src) <= nz.REMUX_FIXABLE

    res = nz.normalize(src)
    assert res.action == "remux"
    assert res.path.endswith("in.ig.mp4")
    assert nz.moov_at_start(res.path)
    atoms = nz.top_level_atoms(res.path)
    assert atoms.index("moov") < atoms.index("mdat")
    assert codes(res.path) == set()
    # ремукс не перекодирует: кодек и размер кадра те же
    v = next(s for s in nz.probe(res.path)["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (320, 568)


def test_edit_list_detected(video_factory):
    assert nz.has_nontrivial_edit_list(video_factory("a.mp4"))
    assert not nz.has_nontrivial_edit_list(video_factory("b.mp4", faststart=True))


def test_no_audio_gets_silence(video_factory):
    src = video_factory(audio=False, faststart=True)
    assert codes(src) == {"no_audio"}

    res = nz.normalize(src)
    assert res.action == "add_silence"
    info = nz.probe(res.path)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1
    assert audio[0]["codec_name"] == "aac"
    assert int(audio[0]["sample_rate"]) == 48000
    assert audio[0]["channels"] == 2
    assert nz.moov_at_start(res.path)
    assert float(info["format"]["duration"]) >= 3.0
    assert codes(res.path) == set()


def test_bad_fps_and_no_audio_triggers_full_reencode(video_factory, tmp_path):
    src = video_factory(audio=False, fps=15)
    assert {"fps", "no_audio", "moov_at_end"} <= codes(src)

    res = nz.normalize(src, tmp_path / "out.mp4")
    assert res.action == "reencode"
    info = nz.probe(res.path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert nz._fps(v) == pytest.approx(30)
    assert a["codec_name"] == "aac" and int(a["sample_rate"]) == 48000
    assert nz.moov_at_start(res.path)


def test_too_short_is_rejected(video_factory):
    src = video_factory(seconds=1, faststart=True)
    assert "duration" in codes(src)
    with pytest.raises(nz.NormalizeError, match="не чинится"):
        nz.normalize(src)


def test_missing_file(tmp_path):
    with pytest.raises(nz.NormalizeError):
        nz.check(tmp_path / "nope.mp4")


@pytest.mark.parametrize("issue_codes, action", [
    (set(), "none"),
    ({"moov_at_end"}, "remux"),
    ({"moov_at_end", "edit_list"}, "remux"),
    ({"no_audio"}, "add_silence"),
    ({"no_audio", "moov_at_end"}, "add_silence"),
    ({"fps"}, "reencode"),
    ({"audio_rate", "moov_at_end"}, "reencode"),
    ({"duration", "fps"}, "reject"),
])
def test_plan(issue_codes, action):
    assert nz.plan([nz.Issue(c, "") for c in issue_codes]) == action
