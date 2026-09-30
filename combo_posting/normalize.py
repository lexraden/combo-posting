"""Проверка видео по спеке IG Reels (instagram_pipeline.md §1.5) и нормализация ffmpeg.

Стратегия (минимально необходимое действие):
  * всё по спеке                          -> файл как есть
  * проблемы только контейнера            -> ремукс -c copy +faststart (без перекодирования)
    (moov в конце, edit list)
  * нет аудио, видео по спеке             -> копия видео + тишина AAC 48k, +faststart
  * иначе                                 -> полное перекодирование командой из спеки
                                             (+ тишина, если нет аудио)
Неисправимое (длительность вне 3 с–15 мин) -> NormalizeError.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# --- спека IG Reels ---------------------------------------------------------
MAX_WIDTH = 1920
FPS_MIN, FPS_MAX = 23, 60
MAX_VIDEO_BITRATE = 25_000_000
MAX_AUDIO_RATE = 48_000
DURATION_MIN, DURATION_MAX = 3.0, 15 * 60.0
MAX_SIZE = 300 * 1024 * 1024
VIDEO_CODECS = {"h264", "hevc"}
PIX_FMTS = {"yuv420p", "yuvj420p"}
CONTAINERS = ("mp4", "mov")

# коды проблем, которые чинятся ремуксом без перекодирования
REMUX_FIXABLE = {"moov_at_end", "edit_list"}

REENCODE_ARGS = [
    "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", "30",
    "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
           "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
    "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
    "-b:v", "8M", "-maxrate", "10M", "-bufsize", "16M",
]
AUDIO_ARGS = ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k"]
SILENCE_INPUT = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
MUX_ARGS = ["-use_editlist", "0", "-movflags", "+faststart"]


class NormalizeError(Exception):
    pass


@dataclass
class Issue:
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class NormalizeResult:
    path: str
    action: str                      # none | remux | add_silence | reencode
    issues: list[Issue] = field(default_factory=list)


# --- ffprobe ----------------------------------------------------------------
def _bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise NormalizeError(f"{name} не найден в PATH")
    return path


def probe(path: str | os.PathLike) -> dict:
    out = subprocess.run(
        [_bin("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise NormalizeError(f"ffprobe не смог прочитать {path}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = (stream.get(key) or "0/0").partition("/")
        try:
            if float(den or 1):
                return float(num) / float(den or 1)
        except ValueError:
            pass
    return 0.0


# --- разбор MP4-атомов (stdlib) --------------------------------------------
def _boxes(f, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack(">I4s", header)
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            return
        yield kind.decode("latin-1"), pos, pos + hdr, pos + size
        pos += size


def top_level_atoms(path: str | os.PathLike) -> list[str]:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        return [kind for kind, *_ in _boxes(f, 0, size)]


def moov_at_start(path: str | os.PathLike) -> bool:
    atoms = top_level_atoms(path)
    if "moov" not in atoms:
        return False
    return "mdat" not in atoms or atoms.index("moov") < atoms.index("mdat")


def has_nontrivial_edit_list(path: str | os.PathLike) -> bool:
    """True, если в каком-то треке edit list с >1 записью или ненулевым media_time.

    Тривиальный elst (одна запись, media_time=0) безвреден и встречается повсюду.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        def walk(start, end):
            for kind, _, body, stop in _boxes(f, start, end):
                if kind in ("moov", "trak", "edts"):
                    if walk(body, stop):
                        return True
                elif kind == "elst":
                    f.seek(body)
                    version = f.read(1)[0]
                    f.read(3)
                    count = struct.unpack(">I", f.read(4))[0]
                    if count != 1:
                        return True
                    f.read(8 if version == 1 else 4)          # segment_duration
                    media_time = (struct.unpack(">q", f.read(8)) if version == 1
                                  else struct.unpack(">i", f.read(4)))[0]
                    if media_time not in (0, -1):
                        return True
            return False
        return walk(0, size)


# --- проверка ---------------------------------------------------------------
def check(path: str | os.PathLike, info: dict | None = None) -> list[Issue]:
    path = Path(path)
    if not path.is_file():
        raise NormalizeError(f"файл не найден: {path}")
    info = info or probe(path)
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    issues: list[Issue] = []

    def add(code, msg):
        issues.append(Issue(code, msg))

    if not any(c in fmt.get("format_name", "") for c in CONTAINERS):
        add("container", f"контейнер {fmt.get('format_name')} (нужен MP4/MOV)")
    size = path.stat().st_size
    if size > MAX_SIZE:
        add("size", f"{size / 1e6:.0f} MB > 300 MB")
    duration = float(fmt.get("duration") or 0)
    if not DURATION_MIN <= duration <= DURATION_MAX:
        add("duration", f"длительность {duration:.1f} с (нужно 3 с – 15 мин)")

    if video is None:
        add("no_video", "нет видеодорожки")
    else:
        if video.get("codec_name") not in VIDEO_CODECS:
            add("video_codec", f"кодек {video.get('codec_name')} (нужен H.264/HEVC)")
        if video.get("pix_fmt") not in PIX_FMTS:
            add("pix_fmt", f"pix_fmt {video.get('pix_fmt')} (нужен yuv420p)")
        if video.get("field_order") not in (None, "progressive", "unknown"):
            add("interlaced", f"field_order {video.get('field_order')} (нужен progressive)")
        fps = _fps(video)
        if not FPS_MIN <= fps <= FPS_MAX:
            add("fps", f"{fps:.2f} fps (нужно 23–60)")
        if int(video.get("width") or 0) > MAX_WIDTH:
            add("width", f"ширина {video.get('width')} > {MAX_WIDTH}")
        vbr = int(video.get("bit_rate") or 0)
        if vbr > MAX_VIDEO_BITRATE:
            add("video_bitrate", f"{vbr / 1e6:.1f} Mbps > 25 Mbps")

    if audio is None:
        add("no_audio", "нет аудио (рилс без звука хуже ранжируется)")
    else:
        if audio.get("codec_name") != "aac":
            add("audio_codec", f"аудио {audio.get('codec_name')} (нужен AAC)")
        if int(audio.get("sample_rate") or 0) > MAX_AUDIO_RATE:
            add("audio_rate", f"{audio.get('sample_rate')} Hz > 48 kHz")
        if int(audio.get("channels") or 0) > 2:
            add("audio_channels", f"{audio.get('channels')} каналов (нужно моно/стерео)")

    if "mp4" in fmt.get("format_name", "") or "mov" in fmt.get("format_name", ""):
        if not moov_at_start(path):
            add("moov_at_end", "moov atom не в начале (нужен faststart)")
        if has_nontrivial_edit_list(path):
            add("edit_list", "есть edit list")
    return issues


# --- нормализация -----------------------------------------------------------
def _run_ffmpeg(args: list[str]) -> None:
    cmd = [_bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise NormalizeError(f"ffmpeg упал: {out.stderr.strip()[-500:]}")


def default_output(src: str | os.PathLike) -> Path:
    src = Path(src)
    return src.with_name(f"{src.stem}.ig.mp4")


def plan(issues: list[Issue]) -> str:
    codes = {i.code for i in issues}
    if not codes:
        return "none"
    if codes & {"duration", "no_video"}:
        return "reject"
    if codes <= REMUX_FIXABLE:
        return "remux"
    if codes <= REMUX_FIXABLE | {"no_audio"}:
        return "add_silence"
    return "reencode"


def normalize(src: str | os.PathLike, dst: str | os.PathLike | None = None) -> NormalizeResult:
    src = Path(src)
    issues = check(src)
    action = plan(issues)
    if action == "none":
        return NormalizeResult(str(src), action, issues)
    if action == "reject":
        raise NormalizeError("не чинится нормализацией: "
                             + "; ".join(str(i) for i in issues if i.code in ("duration", "no_video")))
    dst = Path(dst) if dst else default_output(src)
    if dst.resolve() == src.resolve():
        raise NormalizeError("dst совпадает с src")
    no_audio = any(i.code == "no_audio" for i in issues)

    if action == "remux":
        args = ["-i", str(src), "-map", "0", "-c", "copy", *MUX_ARGS, str(dst)]
    elif action == "add_silence":
        args = ["-i", str(src), *SILENCE_INPUT, "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", *AUDIO_ARGS, "-shortest", *MUX_ARGS, str(dst)]
    else:
        inputs = ["-i", str(src)] + (SILENCE_INPUT if no_audio else [])
        amap = ["-map", "0:v:0", "-map", "1:a:0" if no_audio else "0:a:0"]
        args = [*inputs, *amap, *REENCODE_ARGS, *AUDIO_ARGS,
                *(["-shortest"] if no_audio else []), *MUX_ARGS, str(dst)]
    _run_ffmpeg(args)

    left = check(dst)
    if left:
        raise NormalizeError(f"после {action} остались проблемы: " + "; ".join(map(str, left)))
    return NormalizeResult(str(dst), action, issues)
