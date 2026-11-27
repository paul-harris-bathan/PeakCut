"""Spike mode, part 1: where chat went wild in a Twitch VOD.

Twitch keeps a past broadcast's chat with the video. Reading every message needs a check only Twitch's own site can
pass, but asking for the chat at any moment does not: each answer is the first ~50 messages from that second on. So
the VOD is sampled every STEP seconds (a 10-hour VOD is 3,600 small requests, three at a time, in about five
minutes: Twitch answers about 12 a second and quietly sends empty answers above that), and each sample tells how
fast chat was going there: exact when its messages span the whole step, else estimated from how many seconds they
cover.

A spike is a moment where chat runs much faster than this stream's own normal: the normal is the median speed over
the surrounding minutes, so a huge streamer's busy chat and a small streamer's quiet one are judged the same way.
Spikes at BIG times the normal or more are the ones worth clipping straight away; from MINOR up they are worth a look.

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

_VOD = re.compile(r"(?:twitch\.tv/(?:[\w-]+/)?videos?/|^)(\d{6,14})(?:[/?#]|$)")
_Q_VIDEO = ("query($id:ID!){video(id:$id){id title lengthSeconds createdAt broadcastType "
            "owner{login displayName profileImageURL(width:70)}}}")
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


def find(rates: list, sensitivity: float = 1.0) -> list:
    """Spikes in a list of chat speeds (one per STEP seconds). `sensitivity` scales both levels: 2 = twice as picky.
    Each spike: {"t": seconds into the VOD, "rate": messages a second, "times": how many times the normal, "level"}."""
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
    return sorted(found, key=lambda s: s["t"])


# ---------------------------------------------------------------- scans kept per user
def _dir() -> Path:
    d = config.user_dir(ctx.uid()) / "spikes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file(vid: str) -> Path:
    if not re.fullmatch(r"\d{6,14}", vid or ""):
        raise ValueError("bad video id")
    return _dir() / f"{vid}.json"


def get(vid: str, sensitivity: float = 1.0) -> dict | None:
    """A scan: the VOD's details, its chat speeds, and the spikes at this sensitivity. Running scans show progress."""
    live = _scans.get((ctx.uid(), vid))
    f = _file(vid)
    if not f.is_file():
        return dict(live) if live else None
    data = json.loads(f.read_text(encoding="utf-8"))
    data["spikes"] = find(data.get("rates") or [], sensitivity)
    data["sensitivity"] = sensitivity
    if live:
        data.update({k: live[k] for k in ("status", "progress", "error") if k in live})
    return data


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


def start(url: str) -> dict:
    """Begin scanning a Twitch VOD's chat in the background. Returns the VOD's details straight away."""
    vid = vod_id(url)
    if not vid:
        raise ValueError("Paste a Twitch VOD link, like https://www.twitch.tv/videos/1234567890")
    u = ctx.uid()
    with _lock:
        if (u, vid) in _scans and _scans[(u, vid)].get("status") == "scanning":
            return dict(_scans[(u, vid)])
    info = vod_info(vid)
    live = dict(info, status="scanning", progress=0.0, error="", started=time.time())
    _scans[(u, vid)] = live

    def run():
        with ctx.as_user(u):
            try:
                def moved(p):
                    live["progress"] = round(p, 3)
                    ctx.bump(u)
                rates = sample(vid, info["length"], progress=moved)
                if not any(x for x in rates if x):
                    raise ValueError("Twitch has no chat for that video.")
                data = dict(info, rates=rates, step=STEP, scanned=time.time(), status="done")
                tmp = _file(vid).with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                tmp.replace(_file(vid))
                _scans.pop((u, vid), None)
            except Exception as e:  # noqa: BLE001
                live.update(status="error", error=str(e)[:300])
            ctx.bump(u)

    threading.Thread(target=run, daemon=True).start()
    return dict(live)


def forget(vid: str):
    _file(vid).unlink(missing_ok=True)
    _scans.pop((ctx.uid(), vid), None)
