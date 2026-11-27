"""Downloaded clips in Desktop\\PeakCut Clips\\<streamer>\\<video>\\: copied only on Download, named "clipN - title",
with one clips.txt per video; the folder can be named before clipping and renamed later (on disk too); a taken name
gets " (2)"; a title changed later doesn't rename a saved file; uploads go to Uploads; only on PeakCut's own computer.
Runs with a throwaway folder in place of the desktop."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-folder-")
OUT = tempfile.mkdtemp(prefix="pc-desk-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIPS_DIR=OUT)
sys.path.insert(0, ".")
from pathlib import Path   # noqa: E402

from fastapi.testclient import TestClient   # noqa: E402

from studio import auth, clipfolder, creators, ctx, db as database, jobs, server   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


# ---- names
check("characters Windows won't take are dropped", clipfolder.safe_name('He said: "no*way?" <3 | a/b\\c'), "He said noway 3 abc")
check("trailing dots and spaces go", clipfolder.safe_name("ends here... "), "ends here")
check("device names can't be used", clipfolder.safe_name("CON"), "")
check("at most 60 characters", len(clipfolder.safe_name("x" * 90)), 60)
check("emoji are kept", clipfolder.safe_name("\U0001F7E8LOSING MY MIND\U0001F7E8"), "\U0001F7E8LOSING MY MIND\U0001F7E8")

u = auth.local_user().id


def fake_job(title, creator=None, src="url", clips=(("clip01", "He did it", 4985.0, 5030.0), ("clip02", "Chat lost it: W", 100.0, 140.0)), folder=""):
    with ctx.as_user(u):
        j = jobs.create({"type": src, "url": "https://www.twitch.tv/videos/2940013635", "name": title}, {"folder": folder} if folder else {},
                        enqueue=False, creator=creator)
    j.update(title=title, uploader="caseoh_", status="done")
    d = Path(j["dir"]) / "clips"
    d.mkdir(parents=True, exist_ok=True)
    for cid, t, s, e in clips:
        (d / f"{cid}.mp4").write_bytes(b"video " + cid.encode())
        j["clips"].append({"id": cid, "title": t, "start": s, "end": e, "status": "done", "file": f"{cid}.mp4",
                           "description": f"{t}. #clips", "hashtags": ["clips", "twitch"]})
    j["clips"].append({"id": "clip03", "title": "not made", "start": 1, "end": 2, "status": "error"})
    jobs.save(j)
    return j


with ctx.as_user(u):
    cr = creators.create({"name": "CaseOh", "channel": "https://www.twitch.tv/caseoh_"})
j = fake_job("\U0001F7E8LOSING MY MIND\U0001F7E8", creator=cr["id"])
check("nothing is saved before Download", Path(OUT).exists() and any(Path(OUT).iterdir()), False)

here = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 5000))
away = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("203.0.113.9", 5000))
H = {"Origin": "http://127.0.0.1:8765"}

r = here.post(f"/api/jobs/{j['id']}/save", json={"clips": ["clip01"]}, headers=H)
where = Path(OUT) / "CaseOh" / "\U0001F7E8LOSING MY MIND\U0001F7E8"
check("one clip goes to PeakCut Clips\\streamer\\VOD name", (r.status_code, Path(r.json()["folder"]) == where), (200, True))
check("...named clipN - title", sorted(p.name for p in where.iterdir()), ["clip1 - He did it.mp4", "clips.txt"])
check("...a copy: PeakCut keeps its own", (Path(j["dir"]) / "clips" / "clip01.mp4").is_file())
txt = (where / "clips.txt").read_text(encoding="utf-8")
check("clips.txt lists every finished clip by stream time, with title, caption and hashtags",
      [x in txt for x in ("== clip2 - Chat lost it W", "Stream time: 1:23:05 - 1:23:50", "Title: He did it", "Caption: He did it. #clips",
                          "Hashtags: #clips #twitch")], [True] * 5)
check("...in stream order, without unfinished clips", (txt.index("clip2") < txt.index("clip1"), "not made" in txt), (True, False))
r = here.post(f"/api/jobs/{j['id']}/save", json={}, headers=H)
check("Download all saves the rest", sorted(r.json()["saved"]), ["clip1 - He did it.mp4", "clip2 - Chat lost it W.mp4"])
check("from another computer it is refused", away.post(f"/api/jobs/{j['id']}/save", json={}, headers=H).status_code, lambda s: s in (403, 409))
check("from another website it is refused", here.post(f"/api/jobs/{j['id']}/save", json={}, headers={"Origin": "https://evil.example"}).status_code, 403)

# a title changed later keeps the saved file's name until Download again
j["clips"][0]["title"] = "A new title"
check("a later title change doesn't rename the saved file", (where / "clip1 - He did it.mp4").is_file())
here.post(f"/api/jobs/{j['id']}/save", json={"clips": ["clip01"]}, headers=H)
check("...Download again saves it under the new name", (where / "clip1 - A new title.mp4").is_file())

# renaming moves the folder on the desktop
r = here.put(f"/api/jobs/{j['id']}/folder", json={"name": "Day 3: chaos"}, headers=H)
check("renaming moves the saved folder", (r.status_code, (Path(OUT) / "CaseOh" / "Day 3 chaos").is_dir(), where.exists()), (200, True, False))
check("an empty name is refused", here.put(f"/api/jobs/{j['id']}/folder", json={"name": "???"}, headers=H).status_code, 400)

# a name another video has gets " (2)"; one named before clipping keeps it
j2 = fake_job("Another stream", creator=cr["id"], folder="Day 3 chaos")
r = here.post(f"/api/jobs/{j2['id']}/save", json={}, headers=H)
check("a folder named before clipping is used", Path(r.json()["folder"]).name, "Day 3 chaos (2)")
j3 = fake_job("My upload.mp4", src="file", clips=(("clip01", "", 0, 10),))
r = here.post(f"/api/jobs/{j3['id']}/save", json={}, headers=H)
check("an uploaded file with no streamer goes in Uploads, a clip with no title is just clipN",
      (Path(r.json()["folder"]).parent.name, r.json()["saved"]), ("Uploads", ["clip1.mp4"]))
check("the folder name option is cleaned like a file name", jobs.clean_options({"folder": "a:b*c"}), {"folder": "abc"})

# the worth-a-look moments of a VOD scanned for chat spikes
j4 = fake_job("Spike VOD", creator=cr["id"])
j4.update(vod="2940013635", spikes=[{"t": 5010, "clip": "clip01"}])
from studio import spikes   # noqa: E402
import json   # noqa: E402
with ctx.as_user(u):
    spikes._file("2940013635").write_text(json.dumps({"id": "2940013635", "length": 6000, "step": 10,
                                                      "rates": [2.0] * 300 + [5.4] + [2.0] * 200 + [6.0] + [2.0] * 98}), encoding="utf-8")
    r = here.post(f"/api/jobs/{j4['id']}/save", json={}, headers=H)
txt = (Path(r.json()["folder"]) / "clips.txt").read_text(encoding="utf-8")
check("a spike VOD's clips.txt ends with the moments worth a look", ("Worth a look" in txt, "0:50:00" in txt), (True, True))

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
