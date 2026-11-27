"""Pictures put on chosen clips (Quick edit, Add image): a PNG, JPG, WebP or GIF, kept once per account in its
"overlays" folder under a name made from its contents (the same picture uploaded twice is kept once).

A clip that has one remembers it in clip["image"]: {"file", "x", "y", "w"} (where its centre is, as shares of the
frame's width and height, and how wide it is, as a share of the frame's width; it keeps its own shape), or
{"file", "pos": "full"} to cover the whole frame. Clips from before free placement keep {"file", "pos", "size"}
(a spot and a small, medium or large box). studio/jobs.render_one hands it to the renderer, which draws it over the
clip after any campaign logo (both show)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import config, ctx

TYPES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
SPOTS = ("tl", "tc", "tr", "mc", "bl", "bc", "br", "full")
SIZES = ("s", "m", "l")
MAX_MB = 15
_NAME = re.compile(r"img-[0-9a-f]{16}\.(png|jpg|jpeg|webp|gif)")


def folder(uid: str = None) -> Path:
    d = config.user_dir(uid or ctx.uid()) / "overlays"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path_of(name: str, uid: str = None):
    """Where a kept picture is, or None if the name isn't one of ours or the file is gone."""
    if not _NAME.fullmatch(str(name or "")):
        return None
    p = folder(uid) / name
    return p if p.is_file() else None


def keep(tmp: Path, ext: str) -> str:
    """Keep an uploaded picture (already checked to be one) and return its name."""
    ext = ext.lower()
    if ext not in TYPES:
        raise ValueError("Upload a PNG, JPG, WebP or GIF picture.")
    h = hashlib.sha256()
    with tmp.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    name = f"img-{h.hexdigest()[:16]}{ext}"
    dest = folder() / name
    if dest.is_file():
        tmp.unlink(missing_ok=True)
    else:
        tmp.replace(dest)
    return name


W_MIN, W_MAX = 0.05, 1.0       # the narrowest and widest a placed picture can be, as a share of the frame's width


def _share(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return round(min(hi, max(lo, f)), 4) if f == f else default      # f != f: not a number


def clean(v) -> dict:
    """The picture and where it goes, from the page, checked: the picture must be one of this account's. Free
    placement ({"x", "y", "w"}) wins; "pos": "full" covers the frame; an old spot and size still work."""
    if not isinstance(v, dict):
        raise ValueError("Choose a picture first.")
    name = str(v.get("file") or "")
    if not path_of(name):
        raise ValueError("That picture isn't here any more. Upload it again.")
    if v.get("pos") == "full":
        return {"file": name, "pos": "full"}
    if "w" in v or "x" in v or "y" in v:
        # the centre may sit a little outside the frame, so a picture can hang off an edge, never vanish
        return {"file": name, "x": _share(v.get("x"), -0.2, 1.2, 0.5), "y": _share(v.get("y"), -0.2, 1.2, 0.5),
                "w": _share(v.get("w"), W_MIN, W_MAX, 0.3)}
    pos = v.get("pos") if v.get("pos") in SPOTS else "tr"
    size = v.get("size") if v.get("size") in SIZES else "m"
    return {"file": name, "pos": pos, "size": size}


def for_render(clip: dict, uid: str = None) -> list:
    """The renderer's view of a clip's picture: [{"path", and "x", "y", "w" or "pos" (and "size")}], or [] if it has
    none (or the file is gone)."""
    img = clip.get("image")
    if not isinstance(img, dict):
        return []
    p = path_of(img.get("file"), uid)
    if not p:
        return []
    if "w" in img:
        return [{"path": str(p), "x": img.get("x", 0.5), "y": img.get("y", 0.5), "w": img["w"]}]
    return [{"path": str(p), "pos": img.get("pos", "tr"), "size": img.get("size", "m")}]
