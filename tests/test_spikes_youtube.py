"""Chat spikes in a past YouTube live stream: the chat replay is asked for moment by moment, each answer counted second
by second (a stray old message at its start dropped), the stream walked in strands; anything without a chat replay is
turned away in plain words; the scan is kept under "yt-" and the video's id, never asks Twitch about muting; spike
clips come from the YouTube link and go under the streamer whose YouTube channel it is. YouTube is replaced by fakes."""
import os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-spkyt-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIP_CHECKS="0")
sys.path.insert(0, ".")
from fastapi.testclient import TestClient   # noqa: E402

from studio import auth, creators, ctx, db as database, jobs, media, server, spikeclip, spikes, ytchat   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


# ---- links
check("a YouTube link's id, any form", [ytchat.video_id(u) for u in ("https://www.youtube.com/watch?v=91c96JtcIFg&t=10", "https://youtu.be/91c96JtcIFg",
      "https://www.youtube.com/live/91c96JtcIFg?si=x", "https://www.twitch.tv/videos/123456789")], ["91c96JtcIFg"] * 3 + [""])
check("scans are kept by site", (spikes.scan_key("https://youtu.be/91c96JtcIFg"), spikes.scan_key("https://www.twitch.tv/videos/2932628190"),
                                 spikes.scan_key("https://example.com/x")), ("yt-91c96JtcIFg", "2932628190", ""))

# ---- one answer: counted second by second; a stray old message dropped
stray = [3002.3] + [3016 + i * 0.07 for i in range(100)]
check("a stray old message at the start of an answer is dropped", ytchat.trim(stray)[0], 3016)
quiet = [60, 200, 340, 500, 640, 800, 950]
check("...but a quiet chat's long gaps are kept", ytchat.trim(quiet), quiet)
out = [None] * 10
end = ytchat.fill(out, [10 + i * 0.1 for i in range(100)], 0, 10)        # 10 messages a second from 10 s to 19.9 s
check("a busy answer: its point gets its speed", (abs(out[1] - 10) < 0.2, round(end)), (True, 20))
out = [None] * 20
ytchat.fill(out, [5, 55, 105, 155], 0, 20)                               # one message every 50 s, from 5 s to 155 s
check("a quiet answer fills every point it covers (0 where nobody typed)", (out[0], out[1], out[5], out[15], out[18]), (0.1, 0.0, 0.1, 0.1, None))


# ---- the walk: a fake replay of 1 message a second, with a burst of 20 a second from 600 to 660 s
def chat_at(t):
    msgs = [s + 0.5 for s in range(0, 1800)] + [600 + i / 20 for i in range(1200)]
    msgs = sorted(m for m in msgs if m < 1800)
    i = min(range(len(msgs)), key=lambda k: abs(msgs[k] - t))
    return [3.0] + msgs[max(0, i - 50):i + 50]                            # a stray old message first, as YouTube does


class Fake:
    calls = 0

    def at(self, t):
        Fake.calls += 1
        return ytchat.trim(chat_at(t))


rates = ytchat.sample("x", 1800, chat=Fake())
check("the whole stream is read, with no gaps", (len(rates), sum(1 for r in rates if r is None)), (180, 0))
check("...the normal speed is right", round(rates[20], 1), 1.0)
check("...and the burst stands out", round(rates[62]), 21)
check("...in far fewer asks than points when chat is quiet enough", Fake.calls, lambda n: n < 180)
check("the burst is found as a big spike", [s["t"] for s in spikes.find(rates) if s["level"] == "big"], lambda ts: any(600 <= t <= 660 for t in ts))


# ---- what can't be read is said plainly
def fake_yt(answer):
    def run(url, work):
        if isinstance(answer, Exception):
            raise answer
        return answer
    return run


real_yt = media._yt
for got, want in [(RuntimeError("ERROR: Sign in to confirm your age. This video may be inappropriate"), "age-restricted"),
                  ({"live_status": "not_live", "duration": 600, "subtitles": {}}, "no chat replay"),
                  ({"live_status": "was_live", "duration": 600, "subtitles": {"en": []}}, "no chat replay"),
                  ({"live_status": "is_live", "duration": 0, "subtitles": {}}, "still live"),
                  (RuntimeError("ERROR: Private video. Sign in if you've been granted access"), "private")]:
    media._yt = fake_yt(got)
    try:
        ytchat.info("91c96JtcIFg")
        check(f"turned away: {want}", "no error", want)
    except ValueError as e:
        check(f"turned away: {want}", want in str(e), True)
media._yt = fake_yt({"live_status": "was_live", "duration": 1800, "subtitles": {"live_chat": [{}]}, "title": "BIG STREAM",
                     "channel": "Speedy", "uploader_id": "@SpeedyLive", "channel_id": "UCabc123", "channel_url": "https://www.youtube.com/channel/UCabc123"})
i = ytchat.info("91c96JtcIFg")
check("a past live stream with chat replay is read", (i["id"], i["platform"], i["url"], i["login"], i["length"]),
      ("yt-91c96JtcIFg", "youtube", "https://www.youtube.com/watch?v=91c96JtcIFg", "SpeedyLive", 1800))
media._yt = real_yt

# ---- the scan, from the page
ytchat.info = lambda vid: dict(i, id="yt-" + vid)
ytchat.sample = lambda vid, length, progress=None, **k: (progress and progress(1.0)) or rates
twitch_asked = []
spikes.muted = lambda vid, client=None: twitch_asked.append(vid) or []
spikes.vod_info = lambda vid, client=None: twitch_asked.append(vid) or {}
c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}
r = c.post("/api/spikes", json={"url": "https://www.youtube.com/watch?v=91c96JtcIFg", "name": "Speedy night"}, headers=H)
check("a YouTube link starts a scan", (r.status_code, r.json().get("id"), r.json().get("name")), (200, "yt-91c96JtcIFg", "Speedy night"))
for _ in range(50):
    if c.get("/api/spikes/yt-91c96JtcIFg").json().get("status") == "done":
        break
    time.sleep(0.1)
d = c.get("/api/spikes/yt-91c96JtcIFg").json()
check("the finished scan has its speeds and spikes", (d["status"], len(d["rates"]), any(s["level"] == "big" for s in d["spikes"])), ("done", 180, True))
check("Twitch is never asked about a YouTube stream (no muting there)", (twitch_asked, d.get("muted")), ([], None))
check("it's in the list of scans", any(x["id"] == "yt-91c96JtcIFg" for x in c.get("/api/spikes").json()))
check("a link that's neither Twitch nor YouTube is turned away", c.post("/api/spikes", json={"url": "https://example.com/v/1"}, headers=H).status_code, 400)
ytchat.info = lambda vid: (_ for _ in ()).throw(media.YouTubeBlocked(media.YT_BLOCKED))
check("YouTube refusing the server says so", c.post("/api/spikes", json={"url": "https://youtu.be/AAAAAAAAAAA"}, headers=H).status_code, 503)

# ---- spike clips: from the YouTube link, under the streamer whose YouTube channel it is
u = auth.local_user().id
with ctx.as_user(u):
    who = creators.create({"name": "Speedy", "channel": "https://www.youtube.com/@SpeedyLive"})
    other = creators.create({"name": "Someone", "channel": "https://www.youtube.com/@Speedy"})
    spikeclip.need_keys = lambda: None
    put = []
    jobs._q.put = lambda item, user=None: put.append(item)
    job = spikeclip.clip_spikes(spikes.get("yt-91c96JtcIFg"), [620])
check("the spike video comes from the YouTube link", (job["source"]["url"], job["vod"], job["title"]),
      ("https://www.youtube.com/watch?v=91c96JtcIFg", "yt-91c96JtcIFg", "Speedy night"))
check("...filed under the streamer with that exact YouTube channel (not a similar name)", job["creator"], who["id"])
with ctx.as_user(u):
    whole = jobs.create({"type": "url", "url": "https://youtu.be/91c96JtcIFg", "name": "w"}, {}, enqueue=False)
    whole.update(status="done", clips=[{"id": "clip01", "start": 100.0, "end": 130.0, "status": "done", "title": "Whole"}])
    job["clips"] = [{"id": "clip01", "start": 595.0, "end": 640.0, "status": "done", "title": "Burst"}]
    check("the clip history has the spike clips and the stream clipped whole", sorted(x["title"] for x in spikeclip.history("yt-91c96JtcIFg")), ["Burst", "Whole"])
    check("...and marks it clipped", spikeclip.clip_count("yt-91c96JtcIFg"), 2)

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
