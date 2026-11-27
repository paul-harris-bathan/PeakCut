"""Quick edit, Add image: a picture is kept once per account (only real PNG, JPG, WebP or GIF pictures); put on chosen
clips it is remembered on each and they are made again; it is drawn where asked, at the size asked, on top of the clip
and next to any campaign logo; it can be taken off again; clips still being made are skipped. Clips are really made
from a test pattern and their pixels looked at."""
import os, subprocess, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-overlay-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIP_CHECKS="0", PEAKCUT_AI_KEY="", PEAKCUT_AI_PROVIDER="", PEAKCUT_AI_MODELS="")
sys.path.insert(0, ".")
from pathlib import Path   # noqa: E402

from fastapi.testclient import TestClient   # noqa: E402
from PIL import Image   # noqa: E402

from studio import auth, ctx, db as database, jobs, overlays, render, server   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


d = Path(os.environ["PEAKCUT_DATA"])
u = auth.local_user().id
c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}

# ---- pictures: only real ones are kept, each once
red = d / "red.png"
Image.new("RGBA", (200, 200), (255, 0, 0, 255)).save(red)
r = c.post("/api/overlays", files={"file": ("sticker.png", red.read_bytes(), "image/png")}, headers=H)
check("a PNG is kept, under a name made from its contents", (r.status_code, r.json().get("image", "").startswith("img-")), (200, True))
name = r.json()["image"]
check("...the same picture again is kept once", c.post("/api/overlays", files={"file": ("again.png", red.read_bytes(), "image/png")}, headers=H).json()["image"], name)
check("something that isn't a picture is refused", c.post("/api/overlays", files={"file": ("fake.png", b"not a picture", "image/png")}, headers=H).status_code, 400)
check("other file types are refused", c.post("/api/overlays", files={"file": ("run.exe", b"MZ", "application/octet-stream")}, headers=H).status_code, 400)
check("uploading from another website is refused", c.post("/api/overlays", files={"file": ("a.png", red.read_bytes(), "image/png")}, headers={"Origin": "https://evil.example"}).status_code, 403)
noisy = d / "big.png"            # a real picture of several MB (random pixels don't squeeze), far over the 256 KB of a form
Image.frombytes("RGB", (1200, 1000), os.urandom(1200 * 1000 * 3)).save(noisy)
r = c.post("/api/overlays", files={"file": ("big.png", noisy.read_bytes(), "image/png")}, headers=H)
check(f"a big picture ({noisy.stat().st_size / 1e6:.1f} MB) is taken", (r.status_code, r.json().get("width")), (200, 1200))
r = c.post("/api/overlays", files={"file": ("huge.png", b"\0" * (16 * 1024 * 1024), "image/png")}, headers=H)
check("over 15 MB is refused with the limit", (r.status_code, "15 MB" in r.json().get("detail", "")), (413, True))
gif = d / "blink.gif"
frames = [Image.new("RGB", (120, 120), col) for col in ((0, 0, 255), (0, 255, 0))]
frames[0].save(gif, save_all=True, append_images=frames[1:], duration=200, loop=0)
check("a moving GIF is kept", c.post("/api/overlays", files={"file": ("blink.gif", gif.read_bytes(), "image/gif")}, headers=H).status_code, 200)
with ctx.as_user(u):
    check("a name that isn't one of ours is never a path", (overlays.path_of("../../secret.png"), overlays.path_of("img-0000000000000000.png")), (None, None))
    check("a spot or size that doesn't exist falls back", overlays.clean({"file": name, "pos": "nowhere", "size": "huge"}), {"file": name, "pos": "tr", "size": "m"})
    try:
        overlays.clean({"file": "img-ffffffffffffffff.png"})
        check("a picture that isn't here is refused", False)
    except ValueError:
        check("a picture that isn't here is refused", True)
    check("placed by hand: where and how wide are kept", overlays.clean({"file": name, "x": 0.25, "y": 0.5, "w": 0.2}),
          {"file": name, "x": 0.25, "y": 0.5, "w": 0.2})
    check("...too small or too big is held to 5%-100% of the width, and it can't be dropped far off the frame",
          [overlays.clean({"file": name, "x": 9, "y": -9, "w": w})["w"] for w in (0.001, 7)] + [overlays.clean({"file": name, "x": 9, "y": -9, "w": 0.5})["x"]],
          [0.05, 1.0, 1.2])
    check("...nonsense numbers fall back", overlays.clean({"file": name, "x": "abc", "y": None, "w": "nan"}), {"file": name, "x": 0.5, "y": 0.5, "w": 0.3})
    check("whole frame wins", overlays.clean({"file": name, "pos": "full", "w": 0.2}), {"file": name, "pos": "full"})

# ---- drawn where asked, on top, next to a logo
src = d / "src.mp4"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=1280x720:d=4:r=30", "-f", "lavfi", "-i", "sine=d=4",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
logo = d / "logo.png"
Image.new("RGBA", (300, 100), (0, 0, 255, 255)).save(logo)


def frame(mp4: Path) -> Image.Image:
    png = mp4.with_suffix(".png")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.5", "-i", str(mp4), "-frames:v", "1", str(png)], check=True)
    return Image.open(png).convert("RGB")


def near(px, want, tol=60):
    return all(abs(a - b) <= tol for a, b in zip(px, want))


clip = {"start": 0.0, "end": 3.0, "style": "clean", "layout": "blur", "hook": "", "captions": False}
with ctx.as_user(u):
    pics = overlays.for_render({"image": {"file": name, "pos": "tr", "size": "s"}})
got = render.render_clip(src, d / "o1", "c", dict(clip), [], 1280, 720, extras={"logo": {"path": str(logo), "pos": "tl", "size": "m"}, "images": pics})
im = frame(d / "o1" / got["file"])
check("the picture is in the top right", near(im.getpixel((1080 - 44 - 100, 56 + 100)), (255, 0, 0)))
check("...and the campaign logo still shows top left", near(im.getpixel((44 + 100, 56 + 30)), (0, 0, 255)))
check("...and the rest of the clip is untouched", near(im.getpixel((540, 1700)), (128, 128, 128), 70))
with ctx.as_user(u):
    big = overlays.for_render({"image": {"file": name, "pos": "mc", "size": "l"}})
got = render.render_clip(src, d / "o2", "c", dict(clip), [], 1280, 720, extras={"images": big})
im = frame(d / "o2" / got["file"])
check("in the middle, large: a 640 px box", (near(im.getpixel((540, 960)), (255, 0, 0)), near(im.getpixel((540 - 330, 960)), (255, 0, 0))), (True, False))
with ctx.as_user(u):
    full = overlays.for_render({"image": {"file": name, "pos": "full", "size": "s"}})
got = render.render_clip(src, d / "o3", "c", dict(clip), [], 1280, 720, extras={"images": full})
check("the whole frame: covers it", near(frame(d / "o3" / got["file"]).getpixel((100, 1800)), (255, 0, 0)))
with ctx.as_user(u):
    free = overlays.for_render({"image": {"file": name, "x": 0.25, "y": 0.5, "w": 0.2}})
got = render.render_clip(src, d / "o5", "c", dict(clip), [], 1280, 720, extras={"images": free})
im = frame(d / "o5" / got["file"])
check("placed by hand: centred where it was dropped", near(im.getpixel((270, 960)), (255, 0, 0)))
check("...20% of the width: 216 px wide, square like the picture", [near(im.getpixel(p), (255, 0, 0)) for p in
      ((270 - 100, 960), (270 + 100, 960), (270 - 120, 960), (270 + 120, 960), (270, 960 - 100), (270, 960 + 120))],
      [True, True, False, False, True, False])
with ctx.as_user(u):
    edge = overlays.for_render({"image": {"file": name, "x": 1.0, "y": 0.0, "w": 0.3}})
got = render.render_clip(src, d / "o6", "c", dict(clip), [], 1280, 720, extras={"images": edge})
check("hanging off a corner: the part inside still shows", near(frame(d / "o6" / got["file"]).getpixel((1079 - 40, 40)), (255, 0, 0)))
gname = next(p.name for p in overlays.folder(u).glob("img-*.gif"))
with ctx.as_user(u):
    g = overlays.for_render({"image": {"file": gname, "pos": "bc", "size": "m"}})
got = render.render_clip(src, d / "o4", "c", dict(clip), [], 1280, 720, extras={"images": g})
check("a moving GIF is drawn and the clip keeps its length", abs(__import__("studio.media", fromlist=["probe"]).probe(d / "o4" / got["file"])["duration"] - 3.0) < 0.3)

# ---- on chosen clips: remembered, made again, taken off again
with ctx.as_user(u):
    job = jobs.create({"type": "file", "path": str(src), "name": "src.mp4"}, {}, enqueue=False)
job.update(status="done", source_path=str(src), width=1280, height=720, duration=4.0)
job["clips"] = [{"id": "clip01", "start": 0.0, "end": 3.0, "status": "done", "version": 1, "style": "clean", "layout": "blur",
                 "captions": False, "hook": "", "title": "one"},
                {"id": "clip02", "start": 0.5, "end": 3.5, "status": "rendering", "version": 1, "style": "clean", "layout": "blur", "hook": ""}]
jobs.save(job)
queued = []
jobs._q.put = lambda item, user=None: queued.append(item)
r = c.post("/api/clips/image", json={"clips": [[job["id"], "clip01"], [job["id"], "clip02"], ["nojob-123456", "clip01"]],
                                     "image": {"file": name, "pos": "br", "size": "l"}}, headers=H)
check("the picture goes on the finished clip; one still being made is skipped", (r.json()["made"], r.json()["busy"], r.json()["skipped"]), (1, 1, 1))
check("...the clip remembers it and is made again", (job["clips"][0]["image"], job["clips"][0]["status"], queued[-1]),
      ({"file": name, "pos": "br", "size": "l"}, "queued", ("clip", job["id"], "clip01")))
check("putting it on from another website is refused", c.post("/api/clips/image", json={"clips": [[job["id"], "clip01"]], "image": None},
                                                             headers={"Origin": "https://evil.example"}).status_code, 403)
with ctx.as_user(u):
    jobs.render_one(job, job["clips"][0], [])
check("making it again draws the picture", near(frame(Path(job["dir"]) / "clips" / job["clips"][0]["file"]).getpixel((1080 - 44 - 150, 1920 - 300 - 150)), (255, 0, 0)))
job["clips"][0]["status"] = "done"
r = c.post("/api/clips/image", json={"clips": [[job["id"], "clip01"]], "image": None}, headers=H)
check("taking it off: the clip forgets it and is made again", (r.json()["made"], "image" in job["clips"][0], job["clips"][0]["status"]), (1, False, "queued"))
with ctx.as_user(u):
    jobs.render_one(job, job["clips"][0], [])
check("...and the picture is gone from the clip", near(frame(Path(job["dir"]) / "clips" / job["clips"][0]["file"]).getpixel((1080 - 44 - 150, 1920 - 300 - 150)), (255, 0, 0)), False)
check("nothing chosen is refused", c.post("/api/clips/image", json={"clips": [], "image": None}, headers=H).status_code, 400)

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
