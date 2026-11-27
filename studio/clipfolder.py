"""Downloaded clips, kept in one folder on the desktop:

    Desktop\\PeakCut Clips\\
        <streamer>\\
            <video or VOD name>\\          (named before clipping, or renamed later; " (2)" when the name is taken)
                clip1 - <title>.mp4
                clip2 - <title>.mp4
                clips.txt                  (each clip's time in the stream, title, caption and hashtags; then the
                                            moments still worth a look, for a VOD scanned for chat spikes)
        Uploads\\                           (videos uploaded as files, with no streamer)

Clips are copied there only when the person presses Download (PeakCut keeps its own copy, to post or make again). A
title changed later doesn't rename a file already saved: pressing Download again saves it under the new name.

    PEAKCUT_CLIPS_DIR   another folder instead of Desktop\\PeakCut Clips"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from . import config, creators

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
NAME_MAX = 60


def safe_name(text, limit: int = NAME_MAX) -> str:
    """A name Windows (and macOS and Linux) will take as a file or folder: no \\ / : * ? " < > |, no trailing dots or
    spaces, not a reserved device name, at most `limit` characters. '' if nothing is left."""
    s = _BAD.sub("", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()[:limit].rstrip(" .")
    return "" if s.lower().split(".")[0] in _RESERVED else s


def desktop() -> Path:
    """The desktop folder, where Windows really keeps it (it can be moved, e.g. into OneDrive)."""
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.c_wchar_p()
            guid = ctypes.c_byte * 16
            # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
            fid = guid(*bytes.fromhex("3accbfb42cdb4c42b0297fe99a87c641"))
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                path = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                if path:
                    return Path(path)
        except (OSError, AttributeError, ValueError):
            pass
    return Path.home() / "Desktop"


def root() -> Path:
    return Path(config.env("PEAKCUT_CLIPS_DIR") or desktop() / "PeakCut Clips")


def streamer_name(job: dict) -> str:
    c = creators.get(job["creator"]) if job.get("creator") else None
    name = safe_name((c or {}).get("name") or "")
    if name:
        return name
    if (job.get("source") or {}).get("type") == "file":
        return "Uploads"
    return safe_name(job.get("uploader") or "") or "Other"


def video_name(job: dict) -> str:
    """The video's folder name: the one the person gave it, else its title, else its file name."""
    o = job.get("options") or {}
    return (safe_name(job.get("folder") or o.get("folder") or "") or safe_name(job.get("title") or "")
            or safe_name(Path(str((job.get("source") or {}).get("name") or "")).stem) or job["id"])


def _free(path: Path, mine: str = "") -> Path:
    """`path`, or "path (2)", "path (3)"... when another video already has that folder."""
    if not path.exists() or str(path) == mine:
        return path
    n = 2
    while True:
        p = path.with_name(f"{path.name} ({n})")
        if not p.exists() or str(p) == mine:
            return p
        n += 1


def folder(job: dict, make: bool = False) -> Path:
    """This video's folder: where its clips were saved before (it may have been renamed), else a new free one."""
    was = job.get("saved_dir")
    if was and Path(was).is_dir():
        return Path(was)
    p = _free(root() / streamer_name(job) / video_name(job))
    if make:
        p.mkdir(parents=True, exist_ok=True)
        job["saved_dir"] = str(p)
    return p


def number(clip: dict) -> int:
    m = re.search(r"(\d+)$", str(clip.get("id") or ""))
    return int(m.group(1)) if m else 0


def file_name(clip: dict) -> str:
    title = safe_name(clip.get("title") or clip.get("hook") or "")
    n = number(clip)
    return f"clip{n} - {title}.mp4" if title else f"clip{n}.mp4"


def hms(t) -> str:
    t = max(0, int(round(float(t or 0))))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def _worth_a_look(job: dict) -> list:
    """For a VOD scanned for chat spikes: the moments worth a look that no clip was made from (seconds)."""
    vid = job.get("vod")
    if not vid:
        return []
    try:
        from . import spikes
        scan = spikes.get(vid)
    except Exception:  # noqa: BLE001 - no scan, nothing to list
        return []
    taken = [float(s.get("t", 0)) for s in job.get("spikes") or []]
    return [s["t"] for s in (scan or {}).get("spikes") or [] if s.get("level") == "minor" and not any(abs(s["t"] - t) < 30 for t in taken)]


def write_list(job: dict, where: Path) -> Path:
    """clips.txt: every finished clip of the video, by its place in the stream, ready to copy when posting."""
    done = sorted((c for c in job.get("clips") or [] if c.get("status") == "done"), key=lambda c: float(c.get("start") or 0))
    lines = [job.get("title") or video_name(job)]
    if job.get("uploader"):
        lines.append(f"Streamer: {job['uploader']}")
    url = (job.get("source") or {}).get("url")
    if url:
        lines.append(url)
    lines.append("")
    for c in done:
        tags = " ".join("#" + t for t in c.get("hashtags") or [])
        lines += [f"== {file_name(c)[:-4]}", f"Stream time: {hms(c.get('start'))} - {hms(c.get('end'))}",
                  f"Title: {c.get('title') or ''}", f"Caption: {c.get('description') or ''}"]
        if tags:
            lines.append(f"Hashtags: {tags}")
        lines.append("")
    later = _worth_a_look(job)
    if later:
        lines += ["== Worth a look (chat spiked here, no clip made yet)"] + [hms(t) for t in later] + [""]
    f = where / "clips.txt"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


def save(job: dict, clip_ids=None) -> dict:
    """Copy finished clips (all, or these) into the video's folder and write its clips.txt. Returns where, and which.
    The caller saves the job (its folder is remembered on it)."""
    src_dir = Path(job["dir"]) / "clips"
    want = [c for c in job.get("clips") or [] if c.get("status") == "done" and c.get("file") and (clip_ids is None or c["id"] in clip_ids)]
    if not want:
        raise ValueError("There is no finished clip to download yet.")
    where = folder(job, make=True)
    saved = []
    for c in want:
        f = src_dir / Path(c["file"]).name
        if not f.is_file():
            continue
        out = where / file_name(c)
        if not (out.is_file() and out.stat().st_size == f.stat().st_size):
            tmp = out.with_suffix(".part")
            shutil.copyfile(f, tmp)
            tmp.replace(out)
        saved.append(out.name)
    if not saved:
        raise ValueError("The clip files are no longer here, so nothing could be downloaded.")
    write_list(job, where)
    return {"folder": str(where), "saved": saved}


def rename(job: dict, name: str) -> dict:
    """Give the video's folder a new name. A folder already saved on the desktop is renamed there too."""
    new = safe_name(name)
    if not new:
        raise ValueError("Give the folder a name (letters and numbers; \\ / : * ? \" < > | can't be used).")
    job["folder"] = new
    was = job.get("saved_dir")
    if was and Path(was).is_dir():
        target = _free(Path(was).parent / new, mine=was)
        if str(target) != was:
            Path(was).rename(target)
        job["saved_dir"] = str(target)
    return {"name": new, "folder": str(folder(job))}


def open_folder(job: dict):
    p = folder(job)
    if not p.is_dir():
        raise ValueError("Nothing of this video has been downloaded yet.")
    if sys.platform == "win32":
        os.startfile(str(p))  # noqa: S606 - the owner's own folder, on their own computer
    else:
        import subprocess
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])


def info(job: dict) -> dict:
    p = folder(job)
    return {"name": video_name(job), "folder": str(p), "saved": p.is_dir()}
