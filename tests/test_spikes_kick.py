"""Chat spikes in a past Kick stream: its chat asked for every 10 seconds; each answer's speed counted exactly when it
spans the step, else its 25 messages over the time up to its newest one (Kick's cursor, to the microsecond), so a very
busy chat is measured rather than capped; Kick turning PeakCut away stops the scan with a plain message (never a
graph with holes); spike clips come from the stream's own file, under the streamer whose Kick channel it is. Kick is
replaced by fakes."""
import os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-spkkick-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIP_CHECKS="0")
sys.path.insert(0, ".")
import urllib.error   # noqa: E402
from datetime import datetime, timedelta, timezone   # noqa: E402

from fastapi.testclient import TestClient   # noqa: E402

from studio import auth, creators, ctx, db as database, jobs, kickchat, server, spikeclip, spikes   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


U = "db689b68-5d13-4eb7-8851-779ff19115ee"
check("a Kick past-stream link's id, either form", [kickchat.video_id(u) for u in (f"https://kick.com/adinross/videos/{U}", f"https://kick.com/video/{U.upper()}",
      "https://kick.com/adinross")], [U, U, ""])
check("scans are kept as kk- and the id", spikes.scan_key(f"https://kick.com/adinross/videos/{U}"), "kk-" + U)

# ---- one answer's speed
check("a quiet answer spanning the step is counted exactly", kickchat.rate([60, 61, 63, 65, 69, 72, 80], 60), 0.5)
check("a busy answer: 25 messages up to the newest one's exact time", kickchat.rate([1800] * 25, 1800, 1800.5), 50.0)
check("...without the exact time, up to the end of its last second", kickchat.rate([600] * 20 + [601] * 5, 600), 12.5)
check("no messages: nobody typed", kickchat.rate([], 100), 0.0)

# ---- the stream: a fake Kick with 5 messages a second, and 60 a second from 1200 to 1260 s
START = datetime(2026, 11, 26, 18, 46, 40, tzinfo=timezone.utc)
calls, refuse = [], {"n": 0}


def fake_get(url):
    calls.append(url)
    if refuse["n"]:
        refuse["n"] -= 1
        raise kickchat.Blocked("403")
    if "/api/v1/video/" in url:
        return {"source": "https://stream.kick.com/x/master.m3u8", "livestream": {"session_title": "Cookies", "start_time": "2026-11-26T18:46:40+00:00",
                "duration": 1800_000, "channel_id": 875396, "is_live": False, "channel": {"slug": "adinross", "user": {"username": "AdinRoss"}}}}
    t = datetime.fromisoformat(url.split("start_time=")[1].replace("%3A", ":").replace("Z", "+00:00"))
    s = (t - START).total_seconds()
    speed = 60 if 1200 <= s < 1260 else 5
    msgs = [{"created_at": (t + timedelta(seconds=i / speed)).strftime("%Y-%m-%dT%H:%M:%SZ")} for i in range(25)]
    return {"data": {"messages": msgs, "cursor": str(int((t + timedelta(seconds=24 / speed)).timestamp() * 1e6))}}


kickchat._get = fake_get
info = kickchat.info(U)
check("a past stream's details", (info["id"], info["platform"], info["url"], info["source"], info["length"], info["login"], info["streamer"]),
      ("kk-" + U, "kick", f"https://kick.com/adinross/videos/{U}", "https://stream.kick.com/x/master.m3u8", 1800, "adinross", "AdinRoss"))
rates = kickchat.sample(info)
check("every 10 seconds is read, no gaps", (len(rates), sum(1 for r in rates if r is None)), (180, 0))
check("the normal speed is measured", round(rates[30]), 5)
check("the busy minute is measured, not capped at 25", round(rates[123]), lambda v: 50 <= v <= 70)
check("...and found as a big spike", [s["t"] for s in spikes.find(rates) if s["level"] == "big"], lambda ts: any(1200 <= t <= 1260 for t in ts))

# ---- what can't be read
refuse["n"] = 1
try:
    kickchat.info(U)
    check("Kick turning PeakCut away at the start says so", "no error", "error")
except ValueError as e:
    check("Kick turning PeakCut away at the start says so", "didn't let PeakCut" in str(e), True)
refuse["n"] = 10 ** 6
try:
    kickchat.sample(info)
    check("Kick turning many asks away stops the scan, no half graph", "no error", "error")
except ValueError as e:
    check("Kick turning many asks away stops the scan, no half graph", "didn't let PeakCut" in str(e), True)
refuse["n"] = 3
check("a few refusals are just tried again", sum(1 for r in kickchat.sample(info) if r is None), 0)
refuse["n"] = 0


def gone(url):
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


kickchat._get = gone
try:
    kickchat.info(U)
except ValueError as e:
    check("a deleted stream says so", "no past stream" in str(e), True)
kickchat._get = lambda url: {"livestream": {"is_live": True}, "source": "x"}
try:
    kickchat.info(U)
except ValueError as e:
    check("a stream still live says so", "still live" in str(e), True)
kickchat._get = fake_get

# ---- from the page, and the clips
spikes.muted = lambda vid, client=None: (_ for _ in ()).throw(AssertionError("Twitch asked"))
c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}
r = c.post("/api/spikes", json={"url": f"https://kick.com/adinross/videos/{U}"}, headers=H)
check("a Kick link starts a scan", (r.status_code, r.json().get("id"), r.json().get("platform")), (200, "kk-" + U, "kick"))
for _ in range(100):
    if c.get(f"/api/spikes/kk-{U}").json().get("status") in ("done", "error"):
        break
    time.sleep(0.1)
d = c.get(f"/api/spikes/kk-{U}").json()
check("the finished scan has its speeds and spikes, and no Twitch muting", (d["status"], len(d["rates"]), d.get("muted")), ("done", 180, None))
u = auth.local_user().id
with ctx.as_user(u):
    who = creators.create({"name": "Adin", "channel": "https://kick.com/adinross"})
    creators.create({"name": "Not him", "channel": "https://kick.com/adinross2"})
    spikeclip.need_keys = lambda: None
    jobs._q.put = lambda item, user=None: None
    job = spikeclip.clip_spikes(spikes.get("kk-" + U), [1210])
check("the spike video is fetched from the stream's own file, the page kept", (job["source"]["url"], job["source"].get("page")),
      ("https://stream.kick.com/x/master.m3u8", f"https://kick.com/adinross/videos/{U}"))
check("...under the streamer with exactly that Kick channel", job["creator"], who["id"])
with ctx.as_user(u):
    job["clips"] = [{"id": "clip01", "start": 1185.0, "end": 1230.0, "status": "done", "title": "Chat explodes"}]
    check("the clip history and clipped mark work for Kick too", (spikeclip.clip_count("kk-" + U), [x["title"] for x in spikeclip.history("kk-" + U)]), (1, ["Chat explodes"]))

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
