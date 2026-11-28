"""Picture adjustments on a clip: exposure, brightness, contrast, saturation, warm/cool, sharpen, shadows and highlights,
for the main picture and the streamer's cam apart; settings held in range; the live preview is a real frame made the
same way as the clip (cam brighter, main untouched, and the other way round); applying makes the clip again with them,
and "back to the style's" takes them off. Clips are really made by ffmpeg from a plain grey test video."""
import os, subprocess, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-adjust-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIP_CHECKS="0")
sys.path.insert(0, ".")
import io   # noqa: E402
from pathlib import Path   # noqa: E402

from fastapi.testclient import TestClient   # noqa: E402
from PIL import Image, ImageStat   # noqa: E402

from studio import auth, ctx, db as database, jobs, render, server, templates   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


# ---- the settings
a = templates.clean_adjust({"main": {"exposure": 250, "sharpen": -40, "temp": "-30", "junk": 5}, "cam": {"brightness": 20}, "other": {"x": 1}})
check("settings are held in range; unknown ones dropped", (a["main"]["exposure"], a["main"]["sharpen"], a["main"]["temp"], set(a)),
      (100, 0, -30, {"main", "cam"}))
check("every setting is there for each part", sorted(a["cam"]), sorted(templates.ADJUST_KEYS))
check("all zero is nothing to change", (templates.clean_adjust({"main": {"brightness": 0}}), templates.clean_adjust("x")), (None, None))
check("no settings: no filters", render.adjust_filter(None) + render.adjust_filter({"brightness": 0}), "")
f = render.adjust_filter(templates.clean_adjust({"main": {"exposure": 50, "brightness": 10, "temp": 20, "sharpen": 40, "shadows": 30}})["main"])
check("each setting becomes its filter", all(x in f for x in ("curves=", "eq=", "colorbalance=", "unsharp=")) and f.endswith(","))
check("only what changed is filtered", render.adjust_filter({"saturation": 30, "exposure": 0}), "eq=brightness=0.000:saturation=1.300,")
check("contrast is an S-curve: black stays black, white stays white", render.adjust_filter({"contrast": 50}), "curves=all='0/0.000 0.25/0.210 0.5/0.500 0.75/0.790 1/1.000',")

for colour, want in (("black", lambda px: px == (0, 0, 0)), ("0x808080", lambda px: px[0] > px[1] + 15 > px[2] + 30)):
    pic = Path(tempfile.mkdtemp()) / "w.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=64x64:d=0.1", "-vf",
                    render.adjust_filter({"temp": 100}).rstrip(","), "-frames:v", "1", str(pic)], check=True)
    check(f"warm: {'black stays black' if colour == 'black' else 'grey turns warm'}", Image.open(pic).convert("RGB").getpixel((32, 32)), want)

# ---- a real clip, cam on top (a full-width cam box), game below
u = auth.local_user().id
with ctx.as_user(u):
    job = jobs.create({"type": "file", "path": "x.mp4", "name": "x.mp4"}, {}, enqueue=False)
d = Path(job["dir"])
(d / "clips").mkdir(exist_ok=True)
src = d / "src.mp4"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x808080:s=1280x720:d=6:r=30", "-f", "lavfi", "-i", "sine=f=300:d=6",
                "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
look = templates.clean_look({"layout": "blur", "cam": {"x": 0, "y": 0, "w": 100, "h": 30}, "cam_src": {"x": 0, "y": 0, "w": 0.3, "h": 0.3}})
job.update(status="done", source_path=str(src), width=1280, height=720, duration=6.0)
clip = {"id": "clip01", "start": 0.5, "end": 5.0, "status": "done", "version": 1, "style": "clean", "layout": "blur", "look": look,
        "template": "", "captions": True, "hook": "TEST", "file": "clip01.mp4", "title": "t", "description": "d", "hashtags": [], "score": 50}
job["clips"] = [clip]
jobs.save(job)


def luma(jpg: bytes, box):
    im = Image.open(io.BytesIO(jpg)).convert("L")
    w, h = im.size
    return ImageStat.Stat(im.crop((int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)))).mean[0]


CAM, MAIN = (0.1, 0.05, 0.9, 0.25), (0.1, 0.55, 0.9, 0.9)       # well inside the cam box (top 30%) and the game below
with ctx.as_user(u):
    info = jobs.adjust_info(job, clip)
    check("the panel starts at zero, from the style, with a cam to adjust apart", (info["own"], info["cam"], info["adjust"]["main"]["exposure"]), (False, True, 0))
    plain = jobs.preview_frame(job, clip, None)
    lit_cam = jobs.preview_frame(job, clip, templates.clean_adjust({"cam": {"brightness": 80}}))
    lit_main = jobs.preview_frame(job, clip, templates.clean_adjust({"main": {"exposure": 80}}))
check("the preview is a small picture of the clip", Image.open(io.BytesIO(plain)).size, (render.PREVIEW_W, render.PREVIEW_H))
check("a brighter cam: the cam is brighter...", luma(lit_cam, CAM) - luma(plain, CAM), lambda x: x > 25)
check("...and the main picture is untouched", abs(luma(lit_cam, MAIN) - luma(plain, MAIN)), lambda x: x < 3)
check("more exposure on the main picture: it is brighter...", luma(lit_main, MAIN) - luma(plain, MAIN), lambda x: x > 25)
check("...and the cam is untouched", abs(luma(lit_main, CAM) - luma(plain, CAM)), lambda x: x < 3)

# every setting at once, on both parts, really made into a clip by ffmpeg (captions and all)
every = templates.clean_adjust({p: {"exposure": 20, "brightness": -10, "contrast": 30, "saturation": 40, "temp": -50, "sharpen": 60,
                                    "shadows": 40, "highlights": -40} for p in ("main", "cam")})
out = render.render_clip(src, d / "clips", "every", dict(clip, adjust=every), [{"w": "hello", "s": 1.0, "e": 1.5}], 1280, 720)
check("a clip with every setting is made", (d / "clips" / out["file"]).stat().st_size, lambda x: x > 1000)
game = dict(clip, look=templates.clean_look({"layout": "gameplay"}), layout="gameplay", facecam=[0, 0, 400, 240], adjust=every)
out = render.render_clip(src, d / "clips", "game", game, [], 1280, 720)
check("...and in the gameplay layout with a found webcam", (out["layout"], (d / "clips" / out["file"]).is_file()), ("gameplay", True))
check("a found webcam in the gameplay layout also counts as a cam", jobs.adjust_info(job, dict(game, adjust={}))["cam"], True)

# ---- the page
c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}
B = f"/api/jobs/{job['id']}/clips/clip01/adjust"
r = c.get(B)
check("the page gets the settings and the list of them", (r.status_code, r.json()["keys"], r.json()["cam"]), (200, list(templates.ADJUST_KEYS), True))
r = c.post(B + "/preview", json={"adjust": {"main": {"brightness": 50}}, "at": 1.0}, headers=H)
check("the preview route sends a picture", (r.status_code, r.headers["content-type"], r.content[:2]), (200, "image/jpeg", b"\xff\xd8"))
check("previews from another website are refused", c.post(B + "/preview", json={}, headers={"Origin": "https://evil.example"}).status_code, 403)
put = []
jobs._q.put = lambda item, user=None: put.append(item)
check("applying needs settings", c.put(B, json={}, headers=H).status_code, 400)
r = c.put(B, json={"adjust": {"main": {"contrast": 25}, "cam": {"temp": 400}}}, headers=H)
check("applying makes the clip again with the settings, held in range", (r.status_code, clip["status"], clip["adjust"]["main"]["contrast"], clip["adjust"]["cam"]["temp"], put),
      (200, "queued", 25, 100, [("clip", job["id"], "clip01")]))
check("a clip still being made can't be changed", c.put(B, json={"adjust": None}, headers=H).status_code, 409)
clip["status"] = "done"
check("the panel then shows the clip's own settings", (c.get(B).json()["own"], c.get(B).json()["adjust"]["main"]["contrast"]), (True, 25))
c.put(B, json={"adjust": {"main": {"contrast": 0}}}, headers=H)
check("all zero is kept as the clip's own (nothing changed, even if its style has settings)", (clip.get("adjust"), c.get(B).json()["own"]), ({}, True))
clip["status"] = "done"
c.put(B, json={"adjust": None}, headers=H)
check("back to the style's: the clip's own settings are taken off", ("adjust" in clip, clip["status"]), (False, "queued"))
check("the clip's settings show on the page's updates", "adjust" in jobs.public(dict(job, clips=[dict(clip, adjust={"main": {}})]))["clips"][0], True)

# ---- many clips at once: exactly the same settings for all, replacing what each had
clip["status"] = "done"
two = dict(clip, id="clip02", adjust={"main": {"exposure": 60}}, status="done")
busy = dict(clip, id="clip03", status="rendering")
job["clips"] = [clip, two, busy]
put.clear()
pairs = [[job["id"], "clip01"], [job["id"], "clip02"], [job["id"], "clip03"], [job["id"], "nope"]]
check("many: settings are needed", c.post("/api/clips/adjust", json={"clips": pairs}, headers=H).status_code, 400)
check("many: at least one clip", c.post("/api/clips/adjust", json={"clips": [], "adjust": {}}, headers=H).status_code, 400)
check("many: from another website is refused", c.post("/api/clips/adjust", json={"clips": pairs, "adjust": {}}, headers={"Origin": "https://evil.example"}).status_code, 403)
r = c.post("/api/clips/adjust", json={"clips": pairs, "adjust": {"main": {"brightness": 15}, "cam": {"saturation": 30}}}, headers=H)
check("many: the finished ones are made again; one being made and one gone are counted", r.json(), {"made": 2, "busy": 1, "skipped": 1})
check("many: each gets exactly these settings (the old exposure is replaced, not kept)",
      [(x["adjust"]["main"]["brightness"], x["adjust"]["main"]["exposure"], x["adjust"]["cam"]["saturation"]) for x in (clip, two)], [(15, 0, 30), (15, 0, 30)])
check("many: the clip being made is left as it was", "adjust" in busy, False)
for x in (clip, two):
    x["status"] = "done"
c.post("/api/clips/adjust", json={"clips": pairs[:2], "adjust": None}, headers=H)
check("many: back to the style's takes each clip's own settings off", ("adjust" in clip, "adjust" in two), (False, False))

# ---- picture settings in a style: new clips start with them, a clip's own win
check("a look keeps its picture settings (held in range)", templates.clean_look({"adjust": {"main": {"exposure": 300}}})["adjust"]["main"]["exposure"], 100)
check("a look without any has none", templates.clean_look({})["adjust"], None)
r = c.post("/api/templates", json={"name": "Bright", "look": dict(look, adjust={"main": {"exposure": 40}, "cam": {"temp": 30}})}, headers=H)
tid = r.json()["saved"]
with ctx.as_user(u):
    saved = templates.get(tid)
check("a saved style keeps its picture settings", (saved["look"]["adjust"]["main"]["exposure"], saved["look"]["adjust"]["cam"]["temp"]), (40, 30))
clip.update(status="done")
clip.pop("adjust", None)
job["clips"] = [clip]
c.post("/api/clips/style", json={"clips": [[job["id"], "clip01"]], "template": tid}, headers=H)
check("a clip made in the style is made with its picture settings", render.plan(clip, 1280, 720)["adjust"]["main"]["exposure"], 40)
clip["status"] = "done"
check("the clip's Adjust panel starts from the style's", (c.get(B).json()["adjust"]["main"]["exposure"], c.get(B).json()["style"]["cam"]["temp"], c.get(B).json()["own"]), (40, 30, False))
c.put(B, json={"adjust": {"main": {"exposure": -20}}}, headers=H)
check("the clip's own settings win over the style's", render.plan(clip, 1280, 720)["adjust"]["main"]["exposure"], -20)
clip["status"] = "done"
c.put(B, json={"adjust": {}}, headers=H)
check("a clip set to nothing is made with nothing, whatever its style has", render.plan(clip, 1280, 720)["adjust"], None)
with ctx.as_user(u):
    mp4, _ = templates.sample()
r = c.post("/api/templates/preview", json={"look": dict(look, adjust={"main": {"brightness": 60}})}, headers=H)
check("the style editor's preview is a picture of the preview video", (r.status_code, r.headers.get("content-type"), r.content[:2]), (200, "image/jpeg", bytes([0xFF, 0xD8])))
dark = c.post("/api/templates/preview", json={"look": dict(look, adjust={"main": {"brightness": -60}})}, headers=H)
check("...with the settings on it", luma(r.content, MAIN) - luma(dark.content, MAIN), lambda x: x > 20)
check("previews from another website are refused", c.post("/api/templates/preview", json={}, headers={"Origin": "https://evil.example"}).status_code, 403)

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
