"""Checks on a finished clip, before anyone posts it: sound that is missing, too quiet or distorted, and a picture
that is mostly black or frozen. Each problem found comes back as {"code", "text"}, written for the person posting;
nothing found is [].

One ffmpeg run reads the finished clip (ebur128 for the sound, blackdetect for the picture) and, when it is given,
the same stretch of the source video for freezedetect: captions keep moving over a frozen source, so the finished
clip itself never looks frozen. A check never stops a clip: if ffmpeg can't read it, there are simply no warnings."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config
from .media import NO_WINDOW

QUIET_LUFS = float(config.env("PEAKCUT_CHECK_QUIET_LUFS", "-30"))    # clips are made at about -14; below this is hard to hear
PEAK_DB = float(config.env("PEAKCUT_CHECK_PEAK_DB", "0.5"))          # true peak above this (dBTP): the sound clips
SILENT_PEAK_DB = -90.0                                                # nothing louder than this: no sound at all
BAD_SHARE = 0.5                                                       # black or frozen for more than half the clip
CHECK_SECONDS = 120

_I = re.compile(r"^\s*I:\s*(-?[\d.]+|-inf)\s*LUFS", re.M)
_PEAK = re.compile(r"^\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", re.M)
_BLACK = re.compile(r"black_duration:\s*([\d.]+)")
_FROZEN = re.compile(r"freeze_duration:\s*([\d.]+)")
_FROZEN_START = re.compile(r"freeze_start:\s*([\d.]+)")
_FROZEN_END = re.compile(r"freeze_end:\s*([\d.]+)")

TEXT = {
    "silent": "The clip has no sound.",
    "quiet": "The sound is very quiet. Check the clip has the voice in it.",
    "loud": "The sound is so loud it may crackle.",
    "black": "Most of the picture is black.",
    "frozen": "The picture is frozen for most of the clip.",
}


def _num(s: str) -> float:
    return float("-inf") if s == "-inf" else float(s)


def measure(path: str | Path, duration: float, src: str | Path = None, start: float = 0.0) -> dict:
    """What ffmpeg hears and sees: loudness (LUFS), true peak (dBTP), and the seconds that are black (in the clip)
    and frozen (in the source stretch, or the clip when no source is given). Pictures are read small, at 10 frames
    a second, which is plenty for these and quick."""
    small = "fps=10,scale=270:-2,format=gray"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", str(path)]
    if src:
        cmd += ["-ss", f"{max(0.0, start):.2f}", "-t", f"{duration:.2f}", "-i", str(src)]
        graph = (f"[0:v]{small},blackdetect=d=0.5:pic_th=0.95:pix_th=0.08[v];"
                 f"[1:v]{small},freezedetect=n=-60dB:d=1[f];[0:a]ebur128=peak=true[a]")
        maps = ["-map", "[v]", "-map", "[f]", "-map", "[a]"]
    else:
        graph = (f"[0:v]{small},blackdetect=d=0.5:pic_th=0.95:pix_th=0.08,freezedetect=n=-60dB:d=1[v];"
                 f"[0:a]ebur128=peak=true[a]")
        maps = ["-map", "[v]", "-map", "[a]"]
    p = subprocess.run(cmd + ["-filter_complex", graph, *maps, "-f", "null", "-"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=CHECK_SECONDS)
    if p.returncode != 0:
        raise ValueError(p.stderr[-300:])
    err = p.stderr
    i, pk = _I.findall(err), _PEAK.findall(err)
    frozen = sum(float(x) for x in _FROZEN.findall(err))
    starts, ends = _FROZEN_START.findall(err), _FROZEN_END.findall(err)
    if len(starts) > len(ends):             # still frozen when the stretch ends: freezedetect gives no duration for it
        frozen += max(0.0, duration - float(starts[-1]))      # (times count from the start of the stretch)
    return {"lufs": _num(i[-1]) if i else None, "peak": _num(pk[-1]) if pk else None,
            "black": min(duration, sum(float(x) for x in _BLACK.findall(err))), "frozen": min(duration, frozen)}


def judge(m: dict, duration: float) -> list[dict]:
    """The problems in a measurement worth telling the person about, most serious first."""
    found = []
    if m["peak"] is None or m["peak"] <= SILENT_PEAK_DB:
        found.append("silent")
    elif m["lufs"] is not None and m["lufs"] < QUIET_LUFS:
        found.append("quiet")
    elif m["peak"] > PEAK_DB:
        found.append("loud")
    if duration > 0 and m["black"] / duration > BAD_SHARE:
        found.append("black")
    elif duration > 0 and m["frozen"] / duration > BAD_SHARE:
        found.append("frozen")
    return [{"code": c, "text": TEXT[c]} for c in found]


def check(path: str | Path, duration: float, src: str | Path = None, start: float = 0.0) -> list[dict]:
    """[] when the clip looks and sounds fine, or when it could not be checked at all."""
    try:
        return judge(measure(path, duration, src, start), duration)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
