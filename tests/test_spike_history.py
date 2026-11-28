"""Spike chat, a VOD's history: a VOD can be named before its scan (shown instead of Twitch's title, and given to its
spike clips' video); the scan list marks VODs already clipped, with how many clips; each VOD lists every clip made
from it (spike clips and the VOD clipped whole) by its time in the stream, with a short title; ticked clips are made
again in another style. Twitch and the renderer are replaced by fakes."""
import os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-spkhist-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIP_CHECKS="0")
sys.path.insert(0, ".")
from pathlib import Path   # noqa: E402

from fastapi.testclient import TestClient   # noqa: E402

from studio import auth, ctx, db as database, jobs, server, spikeclip, spikes, templates   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}
u = auth.local_user().id
spikes.muted = lambda vid, client=None: []
spikes.vod_info = lambda vid, client=None: {"id": vid, "title": "BIG STREAM !drops !merch", "length": 3000, "created": "",
                                            "streamer": "Streamer", "login": "streamer", "picture": ""}
rates = [1.0] * 300
rates[30] = rates[150] = 20.0
spikes.sample = lambda vid, length, progress=None, client=None, workers=3: list(rates)

# ---- a name before the scan
check("a name is tidied to one short line", spikes.clean_name("  GTA   night\n with chat  " + "x" * 200)[:22], "GTA night with chat xx")
r = c.post("/api/spikes", json={"url": "https://www.twitch.tv/videos/1111111111", "name": "  GTA night  "}, headers=H)
check("the scan starts with the name given", (r.status_code, r.json().get("name"), r.json().get("title")), (200, "GTA night", "BIG STREAM !drops !merch"))
for _ in range(50):
    if c.get("/api/spikes/1111111111").json().get("status") == "done":
        break
    time.sleep(0.1)
d = c.get("/api/spikes/1111111111").json()
check("...kept with the finished scan (Twitch's title too)", (d["status"], d.get("name"), d["title"]), ("done", "GTA night", "BIG STREAM !drops !merch"))
r = c.post("/api/spikes", json={"url": "https://www.twitch.tv/videos/2222222222"}, headers=H)
check("without a name there is none (Twitch's title is shown)", "name" in r.json(), False)
for _ in range(50):
    if c.get("/api/spikes/2222222222").json().get("status") == "done":
        break
    time.sleep(0.1)

# ---- clips made from the VOD
put = []
jobs._q.put = lambda item, user=None: put.append(item)
spikeclip.need_keys = lambda: None
with ctx.as_user(u):
    scan = spikes.get("1111111111")
    sj = spikeclip.clip_spikes(scan, [300, 1500])
check("the spike clips' video is called by the name given", (sj["title"], sj["source"]["name"]), ("GTA night", "GTA night"))
check("nothing is marked clipped before a clip is made", next(x for x in c.get("/api/spikes").json() if x["id"] == "1111111111")["clipped"], 0)
sj["clips"] = [
    {"id": "clip01", "start": 1475.0, "end": 1515.0, "spike": 1500.0, "status": "done", "title": "Chat goes wild", "hook": "NO WAY",
     "made_at": 1795800000.0, "template": "", "version": 1, "style": "clean", "layout": "blur", "captions": True},
    {"id": "clip02", "start": 275.0, "end": 318.0, "spike": 300.0, "status": "done", "title": "", "hook": "HE DID IT",
     "made_at": 1795800100.0, "template": "", "version": 1, "style": "clean", "layout": "blur", "captions": True},
    {"id": "clip03", "start": 900.0, "end": 940.0, "status": "dropped", "title": "Left out"}]
sj["status"] = "done"
with ctx.as_user(u):
    whole = jobs.create({"type": "url", "url": "https://www.twitch.tv/videos/1111111111?t=1h", "name": "x"}, {}, enqueue=False)
    other = jobs.create({"type": "url", "url": "https://www.twitch.tv/videos/11111111119", "name": "y"}, {}, enqueue=False)
whole.update(status="done", clips=[{"id": "clip01", "start": 2400.0, "end": 2450.0, "status": "done", "title": "The ending",
                                    "made_at": 1795700000.0, "version": 1, "style": "clean", "layout": "blur", "captions": True},
                                   {"id": "clip02", "start": 2000.0, "end": 2030.0, "status": "rendering", "title": "Being made"}])
other.update(status="done", clips=[{"id": "clip01", "start": 10.0, "end": 40.0, "status": "done", "title": "Another VOD"}])

d = c.get("/api/spikes/1111111111").json()
h = d["history"]
check("the history lists every clip from the VOD by its time in the stream", [(x["t"], x["title"]) for x in h],
      [(275, "HE DID IT"), (1475, "Chat goes wild"), (2000, "Being made"), (2400, "The ending")])
check("...spike clips and the VOD clipped whole, never another VOD's, nor clips left out", {x["job"] for x in h}, {sj["id"], whole["id"]})
check("...each with when it was made, how long, and how it is going", (h[0]["made"], h[0]["len"], h[2]["status"]), (1795800100.0, 43, "rendering"))
lst = {x["id"]: x for x in c.get("/api/spikes").json()}
check("the scan list marks the VOD clipped, with how many finished clips", (lst["1111111111"]["clipped"], lst["2222222222"]["clipped"]), (3, 0))
check("...and shows the name given", lst["1111111111"].get("name"), "GTA night")

# ---- a new style for ticked clips
with ctx.as_user(u):
    t = next(x for x in templates.all_templates()["builtin"] if x["id"] != templates.default_id())
pairs = [[sj["id"], "clip01"], [whole["id"], "clip01"], [whole["id"], "clip02"], [sj["id"], "nope"]]
check("a style must be picked", c.post("/api/clips/style", json={"clips": pairs, "template": "t-nothere"}, headers=H).status_code, 400)
check("at least one clip must be ticked", c.post("/api/clips/style", json={"clips": [], "template": t["id"]}, headers=H).status_code, 400)
check("changing from another website is refused", c.post("/api/clips/style", json={"clips": pairs, "template": t["id"]},
                                                         headers={"Origin": "https://evil.example"}).status_code, 403)
put.clear()
r = c.post("/api/clips/style", json={"clips": pairs, "template": t["id"]}, headers=H)
check("ticked clips are made again; one being made and one gone are counted", r.json(), {"made": 2, "busy": 1, "skipped": 1})
check("...each in the new style", [(cl["status"], cl["template"], cl["look"]["layout"] == t["look"]["layout"]) for cl in (sj["clips"][0], whole["clips"][0])],
      [("queued", t["id"], True), ("queued", t["id"], True)])
check("...and put in line to be made", sorted(x[2] for x in put), ["clip01", "clip01"])
check("clips not ticked are left as they were", (sj["clips"][1]["status"], sj["clips"][1]["template"]), ("done", ""))
check("the picture route still works the same way", c.post("/api/clips/image", json={"clips": [[sj["id"], "clip02"]], "image": None}, headers=H).json(),
      {"made": 1, "busy": 0, "skipped": 0})

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
