"""On-screen text: a clip's captions are shown as lines to correct (from what was drawn, else what it was made with);
corrected lines become words that share each line's time; saving makes the clip again with exactly those words and
the heading typed, never proofread or translated over by the AI; a clip still being made can't be changed.
The renderer and the AI are replaced by fakes."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-screen-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIP_CHECKS="0")
sys.path.insert(0, ".")
from pathlib import Path   # noqa: E402

from fastapi.testclient import TestClient   # noqa: E402

from studio import auth, brain, ctx, db as database, jobs, render, server   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


u = auth.local_user().id
heard = [{"w": w, "s": 100.0 + i * 0.4, "e": 100.3 + i * 0.4} for i, w in enumerate("he did not see that coming.".split())]
heard += [{"w": w, "s": 104.0 + i * 0.4, "e": 104.3 + i * 0.4} for i, w in enumerate("chat is going crazy right now".split())]
with ctx.as_user(u):
    job = jobs.create({"type": "file", "path": "x.mp4", "name": "x.mp4"}, {}, enqueue=False)
src = Path(job["dir"]) / "src.mp4"
src.write_bytes(b"video")
(Path(job["dir"]) / "clips").mkdir(exist_ok=True)
job.update(status="done", source_path=str(src), width=1280, height=720, duration=200.0)
clip = {"id": "clip01", "start": 99.5, "end": 108.0, "status": "done", "version": 1, "style": "clean", "layout": "blur",
        "captions": True, "hook": "WAIT FOR IT", "file": "clip01.mp4", "title": "He did not see it", "description": "Chat lost it.",
        "hashtags": ["clips"], "score": 80,
        "heard": {"a": 99.5, "b": 108.0, "lang": "en", "words": heard}}
job["clips"] = [clip]
jobs.save(job)

# ---- lines to correct
lines = jobs.caption_lines(job, clip)
check("captions come as lines, split at a sentence end and a pause", [l["text"] for l in lines], ["he did not see that coming.", "chat is going crazy right now"])
check("...with times from the clip's start", [(l["s"], l["e"]) for l in lines], [(0.5, 2.8), (4.5, 6.8)])

# ---- corrected lines become words sharing the line's time
w = jobs.words_from_lines(clip, [{"s": 0.5, "e": 2.5, "text": "he did NOT see it"}, {"s": 4.5, "e": 6.8, "text": ""},
                                 {"s": "x", "e": 1, "text": "junk"}, "not a line"])
check("each word gets an even share of its line", [(x["w"], x["s"], x["e"]) for x in w],
      [("he", 100.0, 100.4), ("did", 100.4, 100.8), ("NOT", 100.8, 101.2), ("see", 101.2, 101.6), ("it", 101.6, 102.0)])
check("an emptied line shows nothing there; nonsense is skipped", all(x["s"] < 103 for x in w))
check("times are held inside the clip", jobs.words_from_lines(clip, [{"s": -5, "e": 999, "text": "a b"}])[-1]["e"], 108.0)

# ---- saving: remembered, made again, only when finished
c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}
r = c.get(f"/api/jobs/{job['id']}/clips/clip01/screen")
check("the page gets the heading and the lines", (r.json()["hook"], len(r.json()["lines"]), r.json()["edited"]), ("WAIT FOR IT", 2, False))
put = []
jobs._q.put = lambda item, user=None: put.append(item)
new_lines = [{"s": 0.5, "e": 2.8, "text": "he did NOT see that coming"}, {"s": 4.5, "e": 6.8, "text": "chat is losing it"}]
r = c.put(f"/api/jobs/{job['id']}/clips/clip01/screen", json={"hook": "NO WAY", "lines": new_lines}, headers=H)
check("saving puts the clip in line to be made again", (r.status_code, clip["status"], put), (200, "queued", [("clip", job["id"], "clip01")]))
check("...with the heading typed", clip["hook"], "NO WAY")
check("...and the words typed, kept on the clip", " ".join(x["w"] for x in clip["own_words"]), "he did NOT see that coming chat is losing it")
check("a clip still being made can't be changed", c.put(f"/api/jobs/{job['id']}/clips/clip01/screen", json={"hook": "x"}, headers=H).status_code, 409)
clip["status"] = "done"
check("changing it from another website is refused", c.put(f"/api/jobs/{job['id']}/clips/clip01/screen", json={"hook": "x"},
                                                            headers={"Origin": "https://evil.example"}).status_code, 403)
check("nothing to change is refused", c.put(f"/api/jobs/{job['id']}/clips/clip01/screen", json={}, headers=H).status_code, 400)
check("the page sees its own corrections next time", (c.get(f"/api/jobs/{job['id']}/clips/clip01/screen").json()["lines"][1]["text"],
                                                       c.get(f"/api/jobs/{job['id']}/clips/clip01/screen").json()["edited"]), ("chat is losing it", True))
check("the word lists stay off the page's regular updates", any(k in jobs.public(job)["clips"][0] for k in ("own_words", "shown_words")), False)

# ---- made again: exactly the typed words, no AI proofreading or translation over them
drawn = {}


def fake_render(src_, out_dir, name, local, near, w_, h_, extras=None):
    drawn.update(words=[x["w"] for x in near], hook=local.get("hook"), first=near[0]["s"] if near else None)
    return {"file": f"{name}.mp4", "cover": f"{name}.jpg"}


def no_ai(*a, **k):
    raise AssertionError("the AI was asked")


render.render_clip = fake_render
brain.fix_heard = brain.translate_captions = brain.english_captions = no_ai
job["options"]["cap_lang"] = "es"            # captions translated to Spanish, normally
with ctx.as_user(u):
    jobs.render_one(job, clip, [])
check("the clip is drawn with exactly the typed words", " ".join(drawn["words"]), "he did NOT see that coming chat is losing it")
check("...at the time it is said (the video's clock here)", drawn["first"], 100.0)
check("...with the typed heading", drawn["hook"].lower(), "no way")
check("...no AI proofread or translated them", (clip["status"], clip.get("error")), ("done", None))
check("what was drawn is remembered for next time", " ".join(x["w"] for x in clip["shown_words"]), "he did NOT see that coming chat is losing it")

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
