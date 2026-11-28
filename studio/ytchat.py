"""Chat spikes in a past YouTube live stream: how fast its chat replay went, across the whole stream.

YouTube keeps the chat of a past live stream as a "chat replay". Reading it from the start, the way yt-dlp does, means
thousands of small pages one after another (more than ten minutes for a big streamer). But the replay can be asked
for at any moment of the stream, the way YouTube's own player does when someone skips ahead: each answer is a
stretch of messages around that moment, every message in it (a busy chat: a few seconds of it; a quiet one: minutes).
So the stream is walked in a few strands at once, each answer counted second by second and the next asked for just
after it ends; a 10-hour stream takes a few minutes, a quiet one much less. Only message times are kept, never text.

Only past live streams with their chat replay kept have one: anything else is turned away with a plain message."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import media

STEP = 10                  # seconds per point of the graph (as for Twitch)
WORKERS = 3                # strands walked at once
RETRIES = 4                # an answer that fails is asked again, waiting a little longer each time
_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|live/|shorts/|embed/)|youtu\.be/)([\w-]{11})")
NO_REPLAY = "This video has no chat replay (it wasn't a live stream, or its chat was turned off), so there are no chat spikes to find."


def video_id(url: str) -> str:
    """The 11-letter id of a YouTube video link, or '' if it isn't one."""
    m = _ID.search((url or "").strip())
    return m.group(1) if m else ""


def _friendly(e) -> str:
    s = str(e)
    if re.search(r"confirm your age|age.restricted|inappropriate for some users", s, re.I):
        return "YouTube asks for a sign-in to watch this video (it's age-restricted), so PeakCut can't read its chat."
    if re.search(r"private video|members.only|join this channel", s, re.I):
        return "This video is private or for channel members only, so PeakCut can't read its chat."
    if re.search(r"video unavailable|has been removed|does not exist", s, re.I):
        return "YouTube has no video with that link. It may have been deleted or made private."
    return ""


def info(vid: str) -> dict:
    """Title, length and channel of a past YouTube live stream. Raises ValueError with a plain message if its chat
    can't be read (not a past live stream, no chat replay, age-restricted, private...)."""
    url = f"https://www.youtube.com/watch?v={vid}"

    def work(extra):
        with media._ydl(extra) as ydl:
            return ydl.extract_info(url, download=False)
    try:
        i = media._yt(url, work)
    except media.YouTubeBlocked:
        raise
    except Exception as e:  # noqa: BLE001
        msg = _friendly(e)
        if msg:
            raise ValueError(msg)
        raise
    status = i.get("live_status")
    if status in ("is_live", "is_upcoming"):
        raise ValueError("That stream is still live (or hasn't started). Try again when it has ended.")
    if status != "was_live" or "live_chat" not in (i.get("subtitles") or {}):
        raise ValueError(NO_REPLAY)
    length = int(i.get("duration") or 0)
    if length <= 0:
        raise ValueError("That video has no length yet. Try again in a few minutes.")
    return {"id": "yt-" + vid, "platform": "youtube", "url": url, "title": i.get("title") or "", "length": length,
            "created": str(i.get("release_timestamp") or i.get("timestamp") or ""),
            "streamer": i.get("channel") or i.get("uploader") or "", "login": (i.get("uploader_id") or "").lstrip("@"),
            "channel_url": i.get("channel_url") or "", "channel_id": i.get("channel_id") or "",
            "picture": i.get("thumbnail") or ""}


class _Replay:
    """The chat replay of one video, ready to be asked for any moment."""

    def __init__(self, vid: str, extra: dict):
        from yt_dlp.utils import try_get
        self.ydl = media._ydl(extra)
        ie = self.ydl.get_info_extractor("Youtube")
        page = self.ydl.urlopen(f"https://www.youtube.com/watch?v={vid}").read().decode("utf-8", "replace")
        data = ie.extract_yt_initial_data(vid, page)
        self.cont = try_get(data, lambda x: x["contents"]["twoColumnWatchNextResults"]["conversationBar"]["liveChatRenderer"]
                            ["continuations"][0]["reloadContinuationData"]["continuation"])
        if not self.cont:
            raise ValueError(NO_REPLAY)
        # YouTube opens the replay on "Top chat", which leaves most messages out: switch to "Live chat replay" (all of
        # them), as yt-dlp does. If that switch isn't there, Top chat is still a fair measure of how fast chat went.
        try:
            first = self.ydl.urlopen("https://www.youtube.com/live_chat_replay?continuation=" + self.cont).read().decode("utf-8", "replace")
            items = try_get(ie.extract_yt_initial_data(vid, first), lambda x: x["continuationContents"]["liveChatContinuation"]["header"]
                            ["liveChatHeaderRenderer"]["viewSelector"]["sortFilterSubMenuRenderer"]["subMenuItems"], list) or []
            self.cont = items[1]["continuation"]["reloadContinuationData"]["continuation"] if len(items) > 1 else self.cont
        except Exception:  # noqa: BLE001
            pass
        cfg = ie.extract_ytcfg(vid, page)
        self.ctx = cfg["INNERTUBE_CONTEXT"]
        self.url = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat_replay?key=" + cfg["INNERTUBE_API_KEY"]
        self.headers = {**ie.generate_api_headers(ytcfg=cfg, visitor_data=self.ctx["client"].get("visitorData")),
                        "content-type": "application/json"}
        self.lock = threading.Lock()

    def at(self, second: float) -> list:
        """The message times (seconds into the video, in order) of the stretch of chat around this moment."""
        from yt_dlp.networking.common import Request
        body = json.dumps({"context": self.ctx, "continuation": self.cont,
                           "currentPlayerState": {"playerOffsetMs": str(int(max(0, second) * 1000))}}).encode()
        raw = self.ydl.urlopen(Request(self.url, data=body, headers=self.headers)).read()
        acts = ((json.loads(raw).get("continuationContents") or {}).get("liveChatContinuation") or {}).get("actions") or []
        out = []
        for a in acts:
            r = a.get("replayChatItemAction") or {}
            try:
                ms = int(r.get("videoOffsetTimeMsec") or 0)
            except (TypeError, ValueError):
                continue
            if ms > 0 and any("addChatItemAction" in x for x in r.get("actions") or []):
                out.append(ms / 1000)
        return trim(sorted(out))


def trim(times: list) -> list:
    """An answer is a run of messages around the moment asked for, but it can start (or end) with a stray message from
    well before: one whose gap to the rest is far bigger than the gaps between the others. Counting it would stretch
    the run over seconds nobody typed in, so it's dropped."""
    while len(times) >= 6:
        gaps = sorted(b - a for a, b in zip(times, times[1:]))
        typical = max(gaps[len(gaps) // 2], 0.05)
        if times[1] - times[0] > max(5.0, typical * 15):
            times = times[1:]
        elif times[-1] - times[-2] > max(5.0, typical * 15):
            times = times[:-1]
        else:
            break
    return times


def replay(vid: str) -> _Replay:
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        return media._yt(url, lambda extra: _Replay(vid, extra))
    except (ValueError, media.YouTubeBlocked):
        raise
    except Exception as e:  # noqa: BLE001
        msg = _friendly(e)
        if msg:
            raise ValueError(msg)
        raise


def fill(out: list, times: list, lo: int, hi: int):
    """Count one answer into the graph: each point (STEP seconds) inside the stretch the answer covers gets its exact
    speed; a point only partly covered gets its speed over the part covered (2 seconds or more). Only points from
    lo to hi (exclusive) are filled. Returns the second the answer ends at."""
    half = (times[-1] - times[0]) / (len(times) - 1) / 2 if len(times) > 1 else 0    # each message stands for the time
    a, b = max(0.0, times[0] - half), times[-1] + half                                # half way to its neighbours
    for i in range(max(lo, int(a // STEP)), min(hi, int(b // STEP) + 1)):
        s, e = max(a, i * STEP), min(b, (i + 1) * STEP)
        if e - s >= min(2.0, STEP) or (len(times) == 1 and i == int(a // STEP)):
            n = sum(1 for t in times if i * STEP <= t < (i + 1) * STEP)
            out[i] = round(n / max(e - s, 1.0), 3)
    return b


def sample(vid: str, length: int, progress=None, workers: int = WORKERS, chat=None) -> list:
    """Chat speed (messages a second) every STEP seconds of the stream. Points no answer reached stay None (a gap,
    never a fake spike); a stretch with no messages at all counts as 0."""
    chat = chat or replay(vid)
    n = max(1, math.ceil(length / STEP))
    out = [None] * n
    per = math.ceil(n / workers)
    done = [0]
    lock = threading.Lock()

    def walk(lo, hi):
        t = lo * STEP
        while t < hi * STEP:
            times = None
            for attempt in range(RETRIES):
                try:
                    times = chat.at(t)
                    break
                except Exception:  # noqa: BLE001 - asked again, then a gap
                    time.sleep(0.5 * (attempt + 1))
            if times is None:
                nxt = t + STEP
            elif not times:              # nobody typed anywhere near here
                out[int(t // STEP)] = 0.0
                nxt = t + STEP
            else:
                end = fill(out, times, lo, hi)
                nxt = max(t + STEP, end + 1)
            with lock:
                done[0] += int(min(nxt, hi * STEP) // STEP) - int(t // STEP)
                if progress:
                    progress(min(0.99, done[0] / n))
            t = nxt

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda w: walk(w * per, min(n, (w + 1) * per)), range(workers)))
    if progress:
        progress(1.0)
    return out
