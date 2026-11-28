"""A clip's length changed by hand: its start moved earlier or later, its end too. Inside the piece of video it has,
it's simply made again; a clip from a video link (a spike clip, a long video) made longer than its piece gets a wider
piece fetched first, and a spike clip is heard again so the new part has captions; if that fetch fails the old piece
is put back; a live clip stays within what was recorded. The fetch, ElevenLabs, the AI and the renderer are fakes."""
import os, subprocess, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-len-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIP_CHECKS="0")
sys.path.insert(0, ".")
from pathlib import Path   # noqa: E402

from fastapi.testclient import TestClient   # noqa: E402

from studio import auth, brain, ctx, db as database, eleven, jobs, media, render, server   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


def make(path, secs):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=640x360:d={secs}:r=30", "-f", "lavfi", "-i", f"sine=d={secs}",
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    return path


u = auth.local_user().id
with ctx.as_user(u):
    job = jobs.create({"type": "url", "url": "https://www.twitch.tv/videos/2932628190", "name": "VOD"}, {}, enqueue=False, kind="spikes")
d = Path(job["dir"]) / "clips"
d.mkdir(parents=True, exist_ok=True)
piece = make(d / "clip01.src.mp4", 55)                        # the piece a spike clip has: 975 s to 1030 s of the VOD
job.update(status="done", width=640, height=360, duration=7200.0, vod="2932628190", spikes=[{"t": 1000, "clip": "clip01"}])
clip = {"id": "clip01", "start": 975.0, "end": 1020.0, "spike": 1000.0, "src": str(piece), "src_offset": 975.0, "src_end": 1030.0,
        "status": "done", "version": 1, "style": "clean", "layout": "blur", "captions": True, "hook": "WOW", "title": "t", "description": "d",
        "hashtags": [], "score": 80, "file": "clip01.mp4", "heard": {"a": 975.0, "b": 1020.0, "lang": "en", "words": [{"w": "hi", "s": 990.0, "e": 990.4}]}}
job["clips"] = [clip]
jobs.save(job)
put = []
jobs._q.put = lambda item, user=None: put.append(item)

# ---- inside the piece: just made again
jobs.request_render(job["id"], "clip01", {"start": 980.0, "end": 1025.0})
check("shorter at the start, longer at the end, inside its piece: made again, nothing fetched", (clip["start"], clip["end"], clip.get("refetch"), len(put)), (980.0, 1025.0, None, 1))
clip["status"] = "done"

# ---- longer than its piece: a wider piece is fetched, and heard again
jobs.request_render(job["id"], "clip01", {"start": 960.0, "end": 1040.0})
check("longer than its piece: marked to fetch more, the times kept", (clip["start"], clip["end"], clip.get("refetch")), (960.0, 1040.0, True))
fetched, heard_calls = [], []


def fake_section(url, out_dir, stem, start, end, height=720, exact=False):
    fetched.append((url, round(start, 1), round(end, 1)))
    return make(Path(out_dir) / f"{stem}.mp4", round(end - start, 1))


media.download_section = fake_section
eleven.transcribe = lambda path, lang="": (heard_calls.append(path) or [{"w": w, "s": 10.0 + i, "e": 10.5 + i} for i, w in enumerate("early words here".split())]
                                          + [{"w": "late", "s": 85.0, "e": 85.4}], "en")
brain.fix_heard = lambda words, talk="", title="": words
drawn = {}
render.render_clip = lambda src, out_dir, name, local, near, w, h, extras=None: drawn.update(words=[x["w"] for x in near], start=local["start"]) or {"file": f"{name}.mp4", "cover": f"{name}.jpg"}
with ctx.as_user(u):
    jobs.render_one(job, clip, [])
check("the wider piece is fetched from the VOD, padded either side", fetched, [("https://www.twitch.tv/videos/2932628190", 952.0, 1048.0)])
check("...the clip now has it", (clip["src_offset"], clip["src_end"], clip.get("refetch"), clip["status"]), (952.0, 1048.0, None, "done"))
check("...ElevenLabs heard the new piece (a spike clip), so the new part has captions", (len(heard_calls), drawn["words"]), (1, ["early", "words", "here", "late"]))
check("...and the old piece is gone", [p.name for p in d.glob("clip01*.old*")], [])

# ---- the fetch fails: the old piece is put back
clip["status"] = "done"
jobs.request_render(job["id"], "clip01", {"start": 900.0, "end": 1000.0})


def broken(*a, **k):
    raise RuntimeError("HTTP Error 404")


media.download_section = broken
before = Path(clip["src"]).read_bytes()[:100]
with ctx.as_user(u):
    jobs.render_one(job, clip, [])
check("a failed fetch says why", (clip["status"], "fetch" in (clip.get("error") or "").lower()), ("error", True))
check("...and the clip keeps its old piece", (clip.get("src_offset"), Path(clip["src"]).is_file(), Path(clip["src"]).read_bytes()[:100] == before), (952.0, True, True))

# ---- a live clip: only within what was recorded
with ctx.as_user(u):
    live = jobs.create({"type": "live", "url": "https://www.twitch.tv/somebody", "name": "live"}, {}, enqueue=False, kind="live")
live.update(status="done", width=640, height=360)
lc = {"id": "clip01", "start": 100.0, "end": 140.0, "src": str(piece), "src_offset": 90.0, "src_end": 150.0, "status": "done", "version": 1,
      "style": "clean", "layout": "blur", "hook": "", "title": "", "description": "", "hashtags": [], "score": 1}
live["clips"] = [lc]
jobs.request_render(live["id"], "clip01", {"start": 60.0, "end": 200.0})
check("a live clip can only grow to what was recorded", (lc["start"], lc["end"], lc.get("refetch")), (90.0, 150.0, None))

# ---- from the page
clip["status"] = "done"
c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}
r = c.post(f"/api/jobs/{job['id']}/clips/clip01/render", json={"start": 970.0, "end": 1030.0}, headers=H)
check("the page's new start and end are taken", (r.status_code, clip["start"], clip["end"]), (200, 970.0, 1030.0))
clip["status"] = "done"
check("more than 3 minutes is refused", c.post(f"/api/jobs/{job['id']}/clips/clip01/render", json={"start": 900.0, "end": 1100.0}, headers=H).status_code, 400)

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
