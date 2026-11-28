"""Chat spikes in a past Kick stream: how fast its chat went, across the whole stream.

Kick keeps a past stream's chat and hands out the messages around any moment of it (its own player does this when
someone skips ahead): the 25 messages from that moment on, timed to the second, and a "cursor" that is the exact
time (to the microsecond) of the newest of them. So the stream is sampled every STEP seconds, a few asks at once,
and each answer gives how fast chat was going there: exact when its messages span the whole step, else 25 messages
over the time from the moment asked to the cursor, which measures even a chat of 80 messages a second.

Kick sits behind bot protection. If it turns PeakCut away for more than a few asks, the scan stops and says so,
rather than show a graph with holes in it. Only message times are kept, never text."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import profiles

STEP = 10                      # seconds per point of the graph (as for Twitch)
WORKERS = 4                    # asks at once: about 4 answers a second, a 10-hour stream in about 15 minutes
RETRIES = 3
BLOCKED_SHARE = 0.05           # more asks than this turned away: the scan stops
_UUID = re.compile(r"kick\.com/(?:video/|[\w-]+/videos?/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
BLOCKED = "Kick didn't let PeakCut read this stream's chat. Try again later."


class Blocked(Exception):
    pass


def video_id(url: str) -> str:
    """The id of a Kick past-stream link (kick.com/<channel>/videos/<id> or kick.com/video/<id>), or ''."""
    m = _UUID.search((url or "").strip())
    return m.group(1).lower() if m else ""


def _get(url: str) -> dict:
    try:
        _, raw = profiles._get(url, "application/json")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            raise Blocked(str(e.code))
        raise
    try:
        return json.loads(raw)
    except ValueError:                 # a challenge page instead of an answer
        raise Blocked("not json")


def _when(s: str) -> datetime:
    s = str(s or "").replace("Z", "+00:00").replace(" ", "T")
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def info(vid: str) -> dict:
    """Title, length, channel and stream file of a past Kick stream. Raises ValueError with a plain message."""
    try:
        v = _get(f"https://kick.com/api/v1/video/{vid}")
    except Blocked:
        raise ValueError(BLOCKED)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError("Kick has no past stream with that link. It may have been deleted.")
        raise
    ls = v.get("livestream") or {}
    ch = ls.get("channel") or {}
    if ls.get("is_live"):
        raise ValueError("That stream is still live. Try again when it has ended.")
    length = int((ls.get("duration") or 0) / 1000)
    if length <= 0 or not ls.get("start_time") or not ls.get("channel_id"):
        raise ValueError("Kick has no length or chat for that video yet. Try again in a few minutes.")
    if not v.get("source"):
        raise ValueError("Kick has no video file for that stream.")
    slug = ch.get("slug") or ""
    return {"id": "kk-" + vid, "platform": "kick", "url": f"https://kick.com/{slug}/videos/{vid}" if slug else f"https://kick.com/video/{vid}",
            "source": v["source"], "title": ls.get("session_title") or "", "length": length,
            "created": _when(ls["start_time"]).isoformat(), "streamer": (ch.get("user") or {}).get("username") or slug,
            "login": slug, "channel_id": int(ls["channel_id"]), "picture": ls["thumbnail"] if isinstance(ls.get("thumbnail"), str) else (ls.get("thumbnail") or {}).get("src") or ""}


def at(channel_id: int, start: datetime, second: int) -> tuple:
    """(message times in whole seconds into the stream, in order; the exact time of the newest of them, or None) for
    chat from this moment on."""
    t = (start + timedelta(seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    d = (_get(f"https://kick.com/api/v2/channels/{channel_id}/messages?start_time={urllib.parse.quote(t)}").get("data") or {})
    out = []
    for m in d.get("messages") or []:
        try:
            out.append(int((_when(m["created_at"]) - start).total_seconds()))
        except (KeyError, TypeError, ValueError):
            continue
    end = None
    try:
        end = (datetime.fromtimestamp(int(d["cursor"]) / 1e6, timezone.utc) - start).total_seconds() if d.get("cursor") else None
    except (TypeError, ValueError, OverflowError):
        pass
    return sorted(out), end


def rate(times: list, start: int, end=None) -> float:
    """Messages a second from `start`, from one answer: counted over the step when the answer spans all of it, else
    the answer's messages over the time from `start` to its newest message (`end`: exact; else the last whole second)."""
    near = [t for t in times if t >= start]
    if not near:
        return 0.0
    if near[-1] >= start + STEP:                                     # it spans the whole step: count exactly
        return sum(1 for t in near if t < start + STEP) / STEP
    upto = end if end is not None and end >= near[-1] else near[-1] + 1
    return len(near) / max(0.05, upto - start)


def sample(meta: dict, progress=None, workers: int = WORKERS) -> list:
    """Chat speed (messages a second) every STEP seconds of the stream. Raises ValueError(BLOCKED) if Kick turns
    away more than a few asks; an ask that just fails is tried again, then left as a gap."""
    start, cid, length = _when(meta["created"]), int(meta["channel_id"]), int(meta["length"])
    starts = list(range(0, max(STEP, length), STEP))
    out = [None] * len(starts)
    done, blocked, lock = [0], [0], threading.Lock()
    stop = threading.Event()

    def one(i):
        if stop.is_set():
            return
        for attempt in range(RETRIES):
            try:
                times, end = at(cid, start, starts[i])
                out[i] = round(rate(times, starts[i], end), 3)
                break
            except Blocked:
                with lock:
                    blocked[0] += 1
                    if blocked[0] > max(10, BLOCKED_SHARE * len(starts)):
                        stop.set()
                        return
                time.sleep(2 * (attempt + 1))
            except Exception:  # noqa: BLE001 - tried again, then a gap
                time.sleep(0.5 * (attempt + 1))
        with lock:
            done[0] += 1
            if progress and done[0] % 20 == 0:
                progress(min(0.99, done[0] / len(starts)))

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(one, range(len(starts))))
    if stop.is_set():
        raise ValueError(BLOCKED)
    if progress:
        progress(1.0)
    return out
