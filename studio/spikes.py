"""Spike mode, part 1: where chat went wild in a Twitch VOD (or a past YouTube live stream, studio/ytchat.py, or a
past Kick stream, studio/kickchat.py: they read the chat, and everything else here is the same).

Twitch keeps a past broadcast's chat with the video. Reading every message needs a check only Twitch's own site can
pass, but asking for the chat at any moment does not: each answer is the first ~50 messages from that second on. So
the VOD is sampled every STEP seconds (a 10-hour VOD is 3,600 small requests, three at a time, in about five
minutes: Twitch answers about 12 a second and quietly sends empty answers above that), and each sample tells how
fast chat was going there: exact when its messages span the whole step, else estimated from how many seconds they
cover.

A spike is a moment where chat runs much faster than this stream's own normal: the normal is the median speed over
the surrounding minutes, so a huge streamer's busy chat and a small streamer's quiet one are judged the same way.
Spikes at BIG times the normal or more are the ones worth clipping straight away; from MINOR up they are worth a look.

Twitch mutes stretches of a VOD that play copyrighted music (usually in 3-minute blocks), and a clip from one has no
sound at all. The scan asks Twitch which stretches are muted (asked again now and then: Twitch can mute later), and a
spike whose clip would be mostly muted is marked so, and is not clipped.

Each scan is kept as one JSON file per user and VOD; nothing is downloaded but chat timings (no text is kept)."""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import config, ctx, profiles

GQL = "https://gql.twitch.tv/gql"
STEP = 10                     # seconds between chat samples (the graph's finest point)
WORKERS = 3                   # samples asked for at once: at 4 or more Twitch starts sending empty answers
RETRIES = 4                   # an empty answer is Twitch holding back: wait a little longer each time and ask again
NORMAL_WINDOW = 600           # seconds around a moment whose median chat speed is "normal" there
MIN_NORMAL = 0.2              # messages a second: a near-silent stretch can't make every message a spike
BIG, MINOR = 3.0, 2.5         # times the normal: clip straight away / worth a look (the slider moves both). A big
                              # streamer's busiest moments measured about 3x (CaseOh: ~6.6 messages a second, peaks ~13)
MERGE = 30                    # seconds: spikes closer than this are one moment
MAX_HOURS = 72
CLIP_BEFORE, CLIP_AFTER = 25, 20  # seconds around a spike its clip covers (studio/spikeclip.py)
MUTED_SHARE = 0.5             # a spike whose clip would be this much muted by Twitch or more is marked muted
MUTED_FRESH = 6 * 3600        # seconds before the muted stretches are asked for again
MUTED_RETRY = 600             # after Twitch didn't answer, seconds before asking again

_VOD = re.compile(r"(?:twitch\.tv/(?:[\w-]+/)?videos?/|^)(\d{6,14})(?:[/?#]|$)")
_Q_VIDEO = ("query($id:ID!){video(id:$id){id title lengthSeconds createdAt broadcastType "
            "owner{login displayName profileImageURL(width:70)}}}")
_Q_MUTED = "query($id:ID!){video(id:$id){muteInfo{mutedSegmentConnection{nodes{duration offset}}}}}"
_Q_CHAT = "query($id:ID!,$o:Int){video(id:$id){comments(contentOffsetSeconds:$o){edges{node{contentOffsetSeconds}}pageInfo{hasNextPage}}}}"
_scans: dict = {}              # (user, vod) -> live progress of a scan that is running
_lock = threading.Lock()


def vod_id(url: str) -> str:
    """The VOD's number from a link like twitch.tv/videos/123456789 (or the number itself); '' if it isn't one."""
    m = _VOD.search((url or "").strip())
    return m.group(1) if m else ""


def _gql(client, query: str, variables: dict) -> dict:
    r = client.post(GQL, json={"query": query, "variables": variables})
    r.raise_for_status()
    d = r.json() or {}
    if d.get("errors") and not d.get("data"):
        raise ValueError(str(d["errors"])[:200])
    return d.get("data") or {}


def _client():
    import httpx
    return httpx.Client(headers={"Client-ID": profiles._TWITCH_WEB, **profiles.UA}, timeout=15)


def vod_info(vid: str, client=None) -> dict:
    """Title, length and streamer of a Twitch VOD. Raises ValueError with a plain message if it can't be used."""
    c = client or _client()
    v = (_gql(c, _Q_VIDEO, {"id": vid}) or {}).get("video")
    if not v:
        raise ValueError("Twitch has no video with that link. It may have been deleted (Twitch keeps VODs 7 to 60 days).")
    owner = v.get("owner") or {}
    length = int(v.get("lengthSeconds") or 0)
    if length <= 0:
        raise ValueError("That video has no length yet. If the stream is still live, try again when it has ended.")
    if length > MAX_HOURS * 3600:
        raise ValueError(f"That video is longer than {MAX_HOURS} hours.")
    return {"id": str(v["id"]), "title": v.get("title") or "", "length": length, "created": v.get("createdAt") or "",
            "streamer": owner.get("displayName") or owner.get("login") or "", "login": owner.get("login") or "",
            "picture": owner.get("profileImageURL") or ""}


def _rate(times: list, start: int, more: bool) -> float:
    """Messages a second around `start`, from one answer's message times (whole seconds, in order). Twitch's answer
    usually begins a few seconds before `start`."""
    if not times:
        return 0.0
    if times[0] <= start and (times[-1] >= start + STEP or not more):   # it spans the whole step: count exactly
        return sum(1 for t in times if start <= t < start + STEP) / STEP
    if times[0] >= start + STEP:                                         # the next message is after this step
        return 0.0
    return len(times) / max(1, times[-1] - times[0] + 1)                 # busy chat: messages over the seconds covered


def sample(vid: str, length: int, progress=None, client=None, workers: int = WORKERS) -> list:
    """Chat speed (messages a second) every STEP seconds of the VOD. An empty answer or an error is asked again,
    waiting longer each time; a sample still unanswered is left as None (a gap in the graph, never a fake spike).
    Only in the last minute of the VOD can an empty answer be real (no more messages): there it counts as 0."""
    c = client or _client()
    starts = list(range(0, max(STEP, length), STEP))
    out = [None] * len(starts)
    done = [0]

    def one(i):
        for attempt in range(RETRIES):
            try:
                com = ((_gql(c, _Q_CHAT, {"id": vid, "o": starts[i]}).get("video") or {}).get("comments") or {})
                times = [int(e["node"]["contentOffsetSeconds"]) for e in com.get("edges") or []]
                if times or starts[i] >= length - 60:
                    out[i] = _rate(times, starts[i], bool((com.get("pageInfo") or {}).get("hasNextPage")))
                    break
            except Exception:  # noqa: BLE001 - asked again, then a gap
                pass
            time.sleep(0.5 * (attempt + 1))
        done[0] += 1
        if progress and done[0] % 50 == 0:
            progress(done[0] / len(starts))

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, range(len(starts))))
    if progress:
        progress(1.0)
    return out


def muted(vid: str, client=None) -> list:
    """The stretches of a VOD Twitch muted (copyrighted music), as [start, end] seconds, joined where they touch."""
    v = (_gql(client or _client(), _Q_MUTED, {"id": vid}) or {}).get("video") or {}
    nodes = (((v.get("muteInfo") or {}).get("mutedSegmentConnection") or {}).get("nodes")) or []
    out = []
    for a, b in sorted((float(n.get("offset") or 0), float(n.get("offset") or 0) + float(n.get("duration") or 0)) for n in nodes):
        if b <= a:
            continue
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def muted_share(ranges, a: float, b: float) -> float:
    """How much of a..b (0 to 1) falls in muted stretches."""
    if b <= a:
        return 0.0
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in ranges or []) / (b - a)


def is_muted(ranges, t: float) -> bool:
    """Whether the clip of a spike at `t` would be mostly without sound."""
    return muted_share(ranges, t - CLIP_BEFORE, t + CLIP_AFTER) >= MUTED_SHARE


def find(rates: list, sensitivity: float = 1.0, mute: list | None = None) -> list:
    """Spikes in a list of chat speeds (one per STEP seconds). `sensitivity` scales both levels: 2 = twice as picky.
    Each spike: {"t": seconds into the VOD, "rate": messages a second, "times": how many times the normal, "level"},
    and "muted": True when Twitch muted most of its clip (`mute`: the muted stretches)."""
    r = np.array([np.nan if x is None else float(x) for x in rates], dtype=float)
    if not len(r) or np.all(np.isnan(r)):
        return []
    half = max(1, NORMAL_WINDOW // STEP // 2)
    normal = np.array([np.nanmedian(r[max(0, i - half):i + half + 1]) if not np.all(np.isnan(r[max(0, i - half):i + half + 1]))
                       else np.nan for i in range(len(r))])
    normal = np.where(np.isnan(normal), MIN_NORMAL, np.maximum(normal, MIN_NORMAL))
    times = np.where(np.isnan(r), 0.0, r / normal)
    big, minor = BIG * sensitivity, MINOR * sensitivity
    found = []
    for i in np.argsort(-times):                       # strongest first, so a moment keeps its highest point
        if times[i] < minor:
            break
        t = int(i * STEP)
        if any(abs(t - s["t"]) < MERGE for s in found):
            continue
        found.append({"t": t, "rate": round(float(r[i]), 2), "times": round(float(times[i]), 1),
                      "level": "big" if times[i] >= big else "minor"})
        if mute and is_muted(mute, t):
            found[-1]["muted"] = True
    return sorted(found, key=lambda s: s["t"])


# ---------------------------------------------------------------- scans kept per user
def _dir() -> Path:
    d = config.user_dir(ctx.uid()) / "spikes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scan_key(url: str) -> str:
    """What a scan is kept under: a Twitch VOD's number, "yt-" and a YouTube video's id, or "kk-" and a Kick past
    stream's id; '' for any other link."""
    from . import kickchat, ytchat
    if ytchat.video_id(url):
        return "yt-" + ytchat.video_id(url)
    if kickchat.video_id(url):
        return "kk-" + kickchat.video_id(url)
    return vod_id(url)


def _file(vid: str) -> Path:
    if not re.fullmatch(r"\d{6,14}|yt-[\w-]{11}|kk-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", vid or ""):
        raise ValueError("bad video id")
    return _dir() / f"{vid}.json"


def get(vid: str, sensitivity: float = 1.0) -> dict | None:
    """A scan: the VOD's details, its chat speeds, and the spikes at this sensitivity. Running scans show progress."""
    live = _scans.get((ctx.uid(), vid))
    f = _file(vid)
    if not f.is_file():
        return dict(live) if live else None
    data = _read(f)
    _fresh_muted(vid, data)
    data["spikes"] = find(data.get("rates") or [], sensitivity, data.get("muted"))
    data["sensitivity"] = sensitivity
    if live:
        data.update({k: live[k] for k in ("status", "progress", "error") if k in live})
    return data


_muted_failed: dict = {}


def _read(f: Path) -> dict:
    """A scan file. Windows briefly refuses to open a file while it is being swapped for its new version: tried again."""
    for i in range(6):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except PermissionError:
            if i == 5:
                raise
            time.sleep(0.05 * (i + 1))


def _save(vid: str, data: dict):
    tmp = _file(vid).with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(_file(vid))


def _fresh_muted(vid: str, data: dict, save: bool = True):
    """Ask Twitch which stretches are muted if the scan never did (made before this) or did a while ago. Kept with the
    scan. If Twitch doesn't answer, what was known is used, and it is asked again in a few minutes."""
    now = time.time()
    if not vid.isdigit():                     # muting like Twitch's is a Twitch thing
        return
    if now - float(data.get("muted_at") or 0) < MUTED_FRESH or now - _muted_failed.get(vid, 0) < MUTED_RETRY:
        return
    try:
        data["muted"], data["muted_at"] = muted(vid), now
    except Exception:  # noqa: BLE001 - the spikes still show, just without knowing what is muted
        _muted_failed[vid] = now
        return
    if not save:
        return
    keep = dict(data)
    for k in ("spikes", "sensitivity", "status", "progress", "error"):
        keep.pop(k, None)
    keep["status"] = "done"
    try:
        _save(vid, keep)
    except OSError:
        pass


def all_scans() -> list:
    """Every scan of this user, newest first, without the chat speeds (they are only needed for one graph)."""
    out = []
    for f in _dir().glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        d.pop("rates", None)
        out.append(d)
    for (u, vid), live in list(_scans.items()):
        if u == ctx.uid() and not any(s.get("id") == vid for s in out):
            out.append(dict(live))
    return sorted(out, key=lambda d: -float(d.get("scanned") or d.get("started") or 0))


def clean_name(name) -> str:
    """A name the person gave the VOD (shown instead of Twitch's title): one line, at most 100 characters."""
    return re.sub(r"\s+", " ", str(name or "")).strip()[:100]


def start(url: str, name: str = "") -> dict:
    """Begin scanning a Twitch VOD's (or a past YouTube live stream's) chat in the background. Returns the video's
    details straight away. `name`: what to call it in PeakCut (its spike clips' video too) instead of its title."""
    from . import kickchat, ytchat
    vid = scan_key(url)
    if not vid:
        raise ValueError("Paste a Twitch VOD, a past YouTube live stream or a past Kick stream link "
                         "(twitch.tv/videos/..., youtube.com/watch?v=..., kick.com/<channel>/videos/...)")
    u = ctx.uid()
    with _lock:
        if (u, vid) in _scans and _scans[(u, vid)].get("status") == "scanning":
            return dict(_scans[(u, vid)])
    if vid.startswith("yt-"):
        info = ytchat.info(vid[3:])
        if info["length"] > MAX_HOURS * 3600:
            raise ValueError(f"That video is longer than {MAX_HOURS} hours.")
        read = lambda moved: ytchat.sample(vid[3:], info["length"], progress=moved)   # noqa: E731
    elif vid.startswith("kk-"):
        info = kickchat.info(vid[3:])
        if info["length"] > MAX_HOURS * 3600:
            raise ValueError(f"That video is longer than {MAX_HOURS} hours.")
        read = lambda moved: kickchat.sample(info, progress=moved)   # noqa: E731
    else:
        info = dict(vod_info(vid), platform="twitch", url=f"https://www.twitch.tv/videos/{vid}")
        read = lambda moved: sample(vid, info["length"], progress=moved)   # noqa: E731
    if clean_name(name):
        info["name"] = clean_name(name)
    live = dict(info, status="scanning", progress=0.0, error="", started=time.time())
    _scans[(u, vid)] = live

    def run():
        with ctx.as_user(u):
            try:
                def moved(p):
                    live["progress"] = round(p, 3)
                    ctx.bump(u)
                rates = read(moved)
                if not any(x for x in rates if x):
                    raise ValueError("There's no chat in that video.")
                data = dict(info, rates=rates, step=STEP, scanned=time.time(), status="done")
                _fresh_muted(vid, data, save=False)   # which stretches Twitch muted (saved with the scan just below)
                _save(vid, data)
                _scans.pop((u, vid), None)
                try:                               # only if PEAKCUT_SPIKE_AUTO_MAX is set: big spikes clipped by themselves
                    from . import spikeclip
                    spikeclip.auto(dict(data, spikes=find(rates, mute=data.get("muted"))))
                except Exception:  # noqa: BLE001 - the scan is kept either way; spikes can still be clipped by hand
                    pass
            except Exception as e:  # noqa: BLE001
                live.update(status="error", error=str(e)[:300])
            ctx.bump(u)

    threading.Thread(target=run, daemon=True).start()
    return dict(live)


def forget(vid: str):
    _file(vid).unlink(missing_ok=True)
    _scans.pop((ctx.uid(), vid), None)
