"""Spike clips: the ElevenLabs key is kept like the AI key; each spike's stretch is fetched (from 25 s before it), heard by
ElevenLabs, cut by the AI 15 to 25 s after the spike, and made like any clip; big spikes are clipped by themselves; a
failing spike doesn't stop the rest; nothing starts without both keys. Twitch, ElevenLabs and the AI are replaced by
fakes; the clips are really made from a test pattern."""
import os, subprocess, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-spkclip-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_AI_KEY="",
                  PEAKCUT_AI_KEY_FILE="", PEAKCUT_AI_MODELS="", PEAKCUT_AI_PROVIDER="", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_ELEVEN_KEY="", PEAKCUT_CLIP_CHECKS="0")
sys.path.insert(0, ".")
from pathlib import Path   # noqa: E402

from fastapi.testclient import TestClient   # noqa: E402

from studio import ai, auth, brain, ctx, db as database, eleven, jobs, media, server, spikeclip   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}

# ---- the ElevenLabs key
check("no key to begin with", c.get("/api/eleven").json()["has_key"], False)
check("something that isn't a key is refused", c.put("/api/eleven", json={"key": "short"}, headers=H).status_code, 400)
check("saving from another website is refused", c.put("/api/eleven", json={"key": "sk_" + "a" * 30}, headers={"Origin": "https://evil.example"}).status_code, 403)
KEY = "sk_test_elevenlabs_key_abcd1234"
r = c.put("/api/eleven", json={"key": KEY}, headers=H)
check("a key is saved", (r.status_code, r.json()["has_key"], r.json()["key_hint"]), (200, True, "1234"))
check("the key itself is never sent back", KEY in r.text or KEY in c.get("/api/eleven").text, False)
check("it is kept in the data folder", eleven.key(), KEY)


class Reply:
    def __init__(self, status, data, text=""):
        self.status_code, self._d, self.text = status, data, text

    def json(self):
        return self._d


sent = {}


def fake_post(url, headers=None, data=None, files=None, timeout=None):
    sent.update(url=url, key=headers.get("xi-api-key"), model=data.get("model_id"), file=files["file"][0])
    return Reply(200, {"language_code": "eng", "words": [
        {"text": "no", "start": 0.5, "end": 0.7, "type": "word"}, {"text": " ", "start": 0.7, "end": 0.8, "type": "spacing"},
        {"text": "way!", "start": 0.8, "end": 1.2, "type": "word"}, {"text": "(laughs)", "start": 1.3, "end": 2.0, "type": "audio_event"}]})


import httpx   # noqa: E402
real_post = httpx.post
httpx.post = fake_post
wav = Path(os.environ["PEAKCUT_DATA"]) / "a.wav"
wav.write_bytes(b"RIFF")
words, lang = eleven.transcribe(wav)
check("words come back with their times, without spaces or sound tags", words, [{"w": "no", "s": 0.5, "e": 0.7}, {"w": "way!", "s": 0.8, "e": 1.2}])
check("...and the language in two letters", lang, "en")
check("ElevenLabs is asked with the key and the Scribe model", (sent["url"], sent["key"], sent["model"]), (eleven.URL, KEY, "scribe_v2"))
httpx.post = lambda *a, **k: Reply(401, {}, "invalid api key")
try:
    eleven.transcribe(wav)
    check("a refused key says so", False)
except eleven.ElevenError as e:
    check("a refused key says so", "refused the key" in str(e))
httpx.post = real_post

# ---- nothing starts without both keys
scan = {"id": "2940013635", "title": "Big stream", "length": 600, "streamer": "Streamer", "login": "streamer", "status": "done",
        "spikes": [{"t": 100, "times": 4.0, "level": "big"}, {"t": 300, "times": 3.5, "level": "big"}, {"t": 450, "times": 2.6, "level": "minor"}]}
u = auth.local_user().id
with ctx.as_user(u):
    try:
        spikeclip.clip_spikes(scan, [100])
        check("no AI key: no spike clips", False)
    except spikeclip.NotReady as e:
        check("no AI key: no spike clips, and it says which key", "AI key" in str(e))
    check("no keys: big spikes are not clipped by themselves", spikeclip.auto(scan), None)

os.environ.update(PEAKCUT_AI_PROVIDER="gemini", PEAKCUT_AI_MODELS="gemini-test", PEAKCUT_AI_KEY="test-ai-key-123456")
from studio import config   # noqa: E402
config.AI_PROVIDER, config.AI_MODELS = "gemini", ["gemini-test"]
config.ai_key = lambda: "test-ai-key-123456"
check("with the AI key set, the AI is ready", ai.ready(), True)

# ---- the stretch, the words, the cut and the text
fetched = []


def fake_section(url, out_dir, stem, start, end, height=720, exact=False):
    fetched.append((url, round(start, 2), round(end, 2), exact))
    f = Path(out_dir) / f"{stem}.mp4"
    if round(start) == 425:            # the spike at 450: Twitch doesn't send this stretch
        raise RuntimeError("HTTP Error 404")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=1280x720:d={end - start:.2f}:r=30", "-f", "lavfi",
                    "-i", f"sine=f=300:d={end - start:.2f}", "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(f)], check=True)
    return f


media.download_section = fake_section
eleven.transcribe = lambda path, lang="": ([{"w": w, "s": 20.0 + i * 0.5, "e": 20.4 + i * 0.5} for i, w in enumerate(
    "oh my god he actually did it no way chat look at this".split())], "en")
asked = []


def fake_ai(prompt, purpose="", **k):
    asked.append(prompt)
    return ({"end": 999, "score": 88, "hook": "HE DID IT", "title": "He actually did it", "description": "Chat lost it.",
             "hashtags": ["clips", "twitch"], "highlight_words": ["no", "way"], "reason": "a big reaction"}, "gemini-test", 10)


brain.ask_ai = fake_ai
brain.fix_heard = lambda words, talk="", title="": [dict(w, w=w["w"].upper()) for w in words]    # the AI's proofread, visible

with ctx.as_user(u):
    job = spikeclip.clip_spikes(scan, [100, 110, 450])
    check("one video for the VOD, kind spikes, with the VOD's title", (job["kind"], job["vod"], job["title"]), ("spikes", "2940013635", "Big stream"))
    check("spikes within 30 s are one clip", [s["t"] for s in job["spikes"]], [100, 450])
    check("again with the same spike: nothing added twice", [s["t"] for s in spikeclip.clip_spikes(scan, [100])["spikes"]], [100, 450])
    spikeclip.run(job)
    made = [cl for cl in job["clips"] if cl["status"] == "done"]
    check("the stretch fetched is 25 s before to 30 s after, cut exactly", fetched[0], ("https://www.twitch.tv/videos/2940013635", 75.0, 130.0, True))
    check("a clip is made for the spike that could be fetched", len(made), 1)
    cl = made[0]
    check("it starts 25 s before the spike", cl["start"], 75.0)
    check("the AI's end is kept inside 15-25 s after the spike", cl["end"], 125.0)
    check("the AI only read that minute's words, and the spike time", ("[95.0] oh my god" in asked[0], "at 100s" in asked[0]), (True, True))
    check("its heading (tidied like any heading), title and hashtags are the AI's", (cl["hook"].lower(), cl["title"], "#clips" in cl["description"] or "clips" in cl["hashtags"]), ("he did it", "He actually did it", True))
    check("the captions are ElevenLabs' words, proofread by the AI", [w["w"] for w in cl["heard"]["words"]][:3], ["OH", "MY", "GOD"])
    check("the clip file is made", (Path(job["dir"]) / "clips" / cl["file"]).is_file())
    bad = next(s for s in job["spikes"] if s["t"] == 450)
    check("a spike that can't be fetched says why, and the rest are still made", (bad["clip"], "404" in bad["error"] or "fetch" in bad["error"].lower()), ("error", True))
    check("the video finishes", (job["status"], job["stage"].startswith("Done")), ("done", True))
    s = spikeclip.summary("2940013635")
    check("the spike page sees each spike's clip", [(x["t"], x["clip"]) for x in s["spikes"]], [(100, "done"), (450, "error")])
    check("the clip shows in the videos list like any other", any(j["id"] == job["id"] for j in jobs.list_jobs()))

    # the style picked on the spike page: these spikes' clips are made in it
    from studio import templates
    other = next(t for t in templates.all_templates()["builtin"] if t["id"] != templates.default_id())
    scan4 = dict(scan, id="3333333333")
    j4 = spikeclip.clip_spikes(scan4, [100], template=other["id"])
    check("the picked style goes with the ticked spike and the new video", (j4["spikes"][0].get("template"), j4["options"]["template"]), (other["id"], other["id"]))
    spikeclip.run(j4)
    c4 = j4["clips"][0]
    check("its clip is made in the picked style", (c4["status"], c4["template"], c4["look"] == templates.clean_look(other["look"]), c4["layout"]),
          ("done", other["id"], True, templates.clean_look(other["look"])["layout"]))
    third = next(t for t in templates.all_templates()["builtin"] if t["id"] not in (other["id"], templates.default_id()))
    spikeclip.clip_spikes(scan4, [300], template=third["id"])
    spikeclip.run(j4)
    check("a later tick in another style: that clip in that style, the first one left as it was",
          [c_["template"] for c_ in j4["clips"]], [other["id"], third["id"]])
    j5 = spikeclip.clip_spikes(dict(scan, id="3333333334"), [100], template="t-nothere")
    check("a style that doesn't exist is ignored (the usual style is used)", (j5["spikes"][0].get("template"), j5["options"]["template"]), (None, templates.default_id()))

    # ElevenLabs refusing stops the rest the same way, with its message
    def refuse(path, lang=""):
        raise eleven.ElevenError("ElevenLabs refused the key. Paste a working key on the AI page.")
    eleven.transcribe = refuse
    scan2 = dict(scan, id="1234567890")
    job2 = spikeclip.clip_spikes(scan2, [100, 300])
    spikeclip.run(job2)
    check("a refused ElevenLabs key: every spike says so, nothing is made", [(x["clip"], "refused" in x["error"]) for x in job2["spikes"]], [("error", True), ("error", True)])

    # big spikes wait for a tick like any other, unless PEAKCUT_SPIKE_AUTO_MAX is set
    check("big spikes are not clipped by themselves", (spikeclip.AUTO_MAX, spikeclip.auto(dict(scan, id="4444444444")), spikeclip.job_for("4444444444")), (0, None, None))
    # with the setting: biggest first, at most AUTO_MAX
    spikeclip.AUTO_MAX = 1
    scan3 = dict(scan, id="5555555555")
    j3 = spikeclip.auto(scan3)
    check("big spikes are clipped by themselves, biggest first, up to the limit", [s["t"] for s in j3["spikes"]], [100])
    check("...marked as automatic", j3["spikes"][0]["why"], "auto")
    check("...and only once per VOD", spikeclip.auto(scan3), None)

# ---- the page
import json   # noqa: E402

from studio import spikes   # noqa: E402
spikes.muted = lambda vid, client=None: []   # Twitch isn't asked which stretches are muted
with ctx.as_user(u):
    (spikes._file("7777777777")).write_text(json.dumps(dict(scan, id="7777777777", rates=[1.0] * 60, step=10)), encoding="utf-8")
check("clipping needs at least one spike", c.post("/api/spikes/7777777777/clip", json={"times": []}, headers=H).status_code, 400)
put = []
real_put = jobs._q.put
jobs._q.put = lambda item, user=None: put.append(item)          # the work line, watched instead of run
r = c.post("/api/spikes/7777777777/clip", json={"times": [450]}, headers=H)
check("ticked spikes are clipped from the page", (r.status_code, [x["t"] for x in r.json()["clips"]["spikes"]]), (200, [450]))
jid = r.json()["job"]
check("...and the new spike video really goes in the work line", put, [("job", jid, None)])
from studio import templates   # noqa: E402
with ctx.as_user(u):
    pick_t = next(t for t in templates.all_templates()["builtin"] if t["id"] != templates.default_id())["id"]
c.post("/api/spikes/7777777777/clip", json={"times": [200], "template": pick_t}, headers=H)
check("the page sends the picked style with the ticked spikes", next(s_ for s_ in jobs._jobs[jid]["spikes"] if s_["t"] == 200).get("template"), pick_t)
jobs._jobs[jid]["spikes"] = [s_ for s_ in jobs._jobs[jid]["spikes"] if s_["t"] != 200]
c.post("/api/spikes/7777777777/clip", json={"times": [300]}, headers=H)
check("ticking more while it waits doesn't put it in the line twice", (len(put), [s["t"] for s in jobs._jobs[jid]["spikes"]]), (1, [450, 300]))
j7 = jobs._jobs[jid]
j7.update(status="error", error="Cancelled by you.", stage="Cancelled")       # stopped before any spike was made
st = c.get("/api/spikes/7777777777").json()["clips"]
check("a stopped video's spikes show as not made (so the page lets you pick them again)", (st["status"], [x["clip"] for x in st["spikes"]]), ("error", [None, None]))
c.post("/api/spikes/7777777777/clip", json={"times": [450]}, headers=H)
check("picking a stopped spike again puts the video back in the line", (len(put), j7["status"]), (2, "queued"))
j7["spikes"][0].update(clip="error", error="HTTP 404")
j7.update(status="done")
c.post("/api/spikes/7777777777/clip", json={"times": [450]}, headers=H)
check("picking a failed spike again tries it again", (j7["spikes"][0]["clip"], j7["spikes"][0]["error"], len(put)), (None, None, 3))
jobs._q.put = real_put

# a spike ticked while the video is being made is made in the same run
with ctx.as_user(u):
    j8 = spikeclip.clip_spikes(dict(scan, id="8888888888"), [100])
    calls = []

    def quick_prepare(job, spike, n):
        calls.append(spike["t"])
        if len(calls) == 1:                       # ticked while the first spike is being made
            job["spikes"].append({"t": 300, "level": "big", "why": "picked"})
        return {"id": f"clip{n:02d}", "status": "done", "start": spike["t"] - 25, "end": spike["t"] + 20}
    real_prep, real_render = spikeclip._prepare, jobs.render_one
    spikeclip._prepare, jobs.render_one = quick_prepare, (lambda job, clip, words: None)
    spikeclip.run(j8)
    spikeclip._prepare, jobs.render_one = real_prep, real_render
check("a spike ticked during the run is made in that run", (calls, [s["clip"] for s in j8["spikes"]], j8["status"]), ([100, 300], ["clip01", "clip02"], "done"))
check("the scan says which keys are set", c.get("/api/spikes/7777777777").json()["keys"], {"ai": True, "eleven": True})
check("clipping from another website is refused", c.post("/api/spikes/7777777777/clip", json={"times": [100]}, headers={"Origin": "https://evil.example"}).status_code, 403)
check("the key can be removed", (c.delete("/api/eleven/key", headers=H).json()["has_key"], eleven.key()), (False, ""))
r = c.post("/api/spikes/7777777777/clip", json={"times": [100]}, headers=H)
check("without the ElevenLabs key it says what to add", (r.status_code, "ElevenLabs" in r.json().get("detail", "")), (409, True))

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
