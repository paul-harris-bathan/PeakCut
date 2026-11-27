"""The streamer's own cam box (their page, Setup, Streamer cam) wins over looking for a cam, for videos and live
recordings alike, so a VTuber's character is used and not the chat a cam finder can mistake for it; "No cam" means
none is looked for; without a box the finder still runs. The finder and the AI are replaced by fakes."""
import os, subprocess, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-cam-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_UPDATE_CHECK="0")
sys.path.insert(0, ".")
from pathlib import Path   # noqa: E402
from types import SimpleNamespace   # noqa: E402

from studio import auth, creators, ctx, db as database, facecam, jobs, live   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


d = Path(os.environ["PEAKCUT_DATA"])
src = d / "stream.mp4"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=1920x1080:d=1:r=30", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)], check=True)
looked = []
facecam.detect = lambda path, ask=None: looked.append(str(path)) or {"x": 1500, "y": 700, "w": 380, "h": 330, "conf": "high"}   # "the chat"

u = auth.local_user().id
with ctx.as_user(u):
    mouse = creators.create({"name": "ironmouse", "channel": "https://www.twitch.tv/ironmouse"})
    plain = creators.create({"name": "someone", "channel": "https://www.twitch.tv/someone"})
    nocam = creators.create({"name": "nocam", "channel": "https://www.twitch.tv/nocam"})
    creators.update(mouse["id"], {"cam": {"x": 0.05, "y": 0.5, "w": 0.25, "h": 0.4}, "cam_auto": False})
    creators.update(nocam["id"], {"cam": None, "cam_none": True})

    check("no streamer: no box of theirs", jobs.streamer_cam(None, src), None)
    check("a streamer with no box set: none", jobs.streamer_cam(plain["id"], src), None)
    check("a streamer set to No cam: False", jobs.streamer_cam(nocam["id"], src), False)
    check("their box, in this video's pixels (from the file)", jobs.streamer_cam(mouse["id"], src), [96, 540, 480, 432])
    check("...or from a size given", jobs.streamer_cam(mouse["id"], None, (1280, 720)), [64, 360, 320, 288])
    creators.update(plain["id"], {"cam": {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}})
    check("a box hanging off the frame is cut to fit", jobs.streamer_cam(plain["id"], None, (1000, 1000)), [900, 900, 100, 100])
    check("a frame size that isn't known: no box", jobs.streamer_cam(mouse["id"], None, (0, 0)), None)
    creators.update(plain["id"], {"cam": None})

    def job_for(cid):
        return {"creator": cid, "source_path": str(src), "width": 1920, "height": 1080}

    j = job_for(mouse["id"])
    check("a video: the streamer's box is used, the finder isn't asked", (jobs._facecam(j), looked, j["facecam"]), ([96, 540, 480, 432], [], [96, 540, 480, 432]))
    check("a video of a No cam streamer: no cam, nothing looked for", (jobs._facecam(job_for(nocam["id"])), looked), (None, []))
    check("a video of a streamer with no box: the finder still runs", (jobs._facecam(job_for(plain["id"])), len(looked)), ([1500, 700, 380, 330], 1))

    rec = SimpleNamespace(job={"creator": mouse["id"]})
    check("a live recording: the streamer's box too", (live.Session._find_facecam(rec, str(src)), len(looked)), ([96, 540, 480, 432], 1))
    check("a live recording of a No cam streamer: none", live.Session._find_facecam(SimpleNamespace(job={"creator": nocam["id"]}), str(src)), None)
    check("a live recording with no box: the finder runs", (live.Session._find_facecam(SimpleNamespace(job={"creator": plain["id"]}), str(src)), len(looked)),
          ([1500, 700, 380, 330], 2))

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
