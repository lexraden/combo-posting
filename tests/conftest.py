import shutil
import subprocess

import pytest

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe не установлены")


def make_video(path, *, seconds=3.5, audio=True, faststart=False, size="320x568", fps=30):
    """Маленький тестовый ролик через lavfi. ffmpeg по умолчанию пишет moov в конец."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-b:a", "64k"]
    if faststart:   # «чистый» по спеке файл: moov в начале, без edit list
        args += ["-use_editlist", "0", "-movflags", "+faststart"]
    args += ["-shortest", str(path)]
    subprocess.run(args, check=True)
    return path


@pytest.fixture
def video_factory(tmp_path):
    def factory(name="in.mp4", **kw):
        return make_video(tmp_path / name, **kw)
    return factory
