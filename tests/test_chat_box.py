"""The chat box and the Custom layout: the chat cut from where it was drawn on the stream and put in its own box
(corners, border, see-through, its background colour taken out, its own picture settings); the Custom layout puts the
gameplay in a box of its own over the blurred gameplay, with the cam and the chat where they were placed; each part
fills its box (the extra is cut, never squashed); old styles are unchanged. Clips are really made by ffmpeg."""
import os, subprocess, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-chat-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIP_CHECKS="0")
sys.path.insert(0, ".")
from pathlib import Path   # noqa: E402

from PIL import Image   # noqa: E402

from studio import render, templates   # noqa: E402

fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


# ---- the look
L = templates.clean_look({"layout": "custom", "chat": {"x": 90, "y": 2, "w": 3, "h": 300}, "chat_src": {"x": 0.7, "y": 0, "w": 0.3, "h": 1},
                          "chat_style": {"op": 5, "ks": 200, "key": 1, "kc": "zz", "r": 80}, "game": {"x": 0, "y": 25, "w": 100, "h": 50}})
check("the Custom layout is a layout", (L["layout"], "custom" in templates.LAYOUT_NAMES, "custom" in render.LAYOUTS), ("custom", True, True))
check("the chat box is held inside the frame, at least a small size", L["chat"], {"x": 90.0, "y": 0.0, "w": 8.0, "h": 100.0})
check("its look is held in range", (L["chat_style"]["op"], L["chat_style"]["ks"], L["chat_style"]["kc"], L["chat_style"]["r"], L["chat_style"]["key"]),
      (10, 60, "000000", 50, True))
old = templates.clean_look({"layout": "blur"})
check("an old style has no chat, no gameplay box", (old["chat"], old["chat_src"], old["game"]), (None, None, None))
check("...and its picture is made exactly as before", render._filter("blur", 608).startswith("[0:v]fps="), True)
check("the chat has its own picture settings", "chat" in templates.ADJUST_PARTS)
check("a vertical video keeps the Custom layout", render.geometry(1080, 1920, "custom")[0], "custom")

# ---- a test video: left half red, right half (the 'chat') black with a white bar in the middle
d = Path(tempfile.mkdtemp())
src = d / "src.mp4"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=1280x720:d=3:r=30", "-f", "lavfi", "-i", "color=c=black:s=640x720:d=3:r=30",
                "-f", "lavfi", "-i", "color=c=white:s=640x120:d=3:r=30", "-f", "lavfi", "-i", "sine=d=3",
                "-filter_complex", "[0:v][1:v]overlay=640:0[a];[a][2:v]overlay=640:300[v]", "-map", "[v]", "-map", "3:a", "-shortest",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
GAME = {"x": 0, "y": 0, "w": 0.5, "h": 1}          # the red half
CHAT = {"x": 0.5, "y": 0, "w": 0.5, "h": 1}        # the black half with its white bar


def frame(look, name, adjust=None):
    clip = {"start": 0.2, "end": 2.5, "look": look, "hook": "", "title": "", "description": ""}
    if adjust is not None:
        clip["adjust"] = adjust
    out = render.preview_frame(src, clip, 1280, 720, {}, 1.0, d / f"{name}.jpg")
    return Image.open(out).convert("RGB")


def px(im, x, y):                                  # a pixel, by shares of the frame
    return im.getpixel((int(x * im.width), int(y * im.height)))


custom = templates.clean_look({"layout": "custom", "game_src": GAME, "game": {"x": 0, "y": 30, "w": 100, "h": 40},
                               "chat": {"x": 60, "y": 2, "w": 36, "h": 24}, "chat_src": CHAT, "hook": {"show": False}, "cap": {"show": False}})
im = frame(custom, "custom")
r, g, b = px(im, 0.5, 0.5)
check("Custom: the gameplay (the part shown) fills its box", r > 200 and g < 60 and b < 60, True)
r2, g2, b2 = px(im, 0.3, 0.9)
check("...over the same gameplay, blurred and darker, below it", 90 < r2 < 230 and g2 < 60, True)
check("the chat box shows the chat where it was placed (the dark panel)", sum(px(im, 0.65, 0.05)) < 90, True)
check("...with its white line in the middle", min(px(im, 0.78, 0.14)) > 180, True)
check("...and the gameplay is not under the chat box there", px(im, 0.65, 0.05)[0] < 60, True)

keyed = frame(dict(custom, chat_style={"key": True, "kc": "000000", "ks": 30, "op": 100, "r": 0, "bw": 0, "bc": "FFFFFF"}), "keyed")
check("its background taken out: the blurred gameplay shows through the dark panel", px(keyed, 0.65, 0.05)[0] > 90, True)
check("...and the words (the white line) stay", min(px(keyed, 0.78, 0.14)) > 180, True)
half = frame(dict(custom, chat_style={"key": False, "op": 50, "r": 0, "bw": 0, "bc": "FFFFFF", "kc": "000000", "ks": 25}), "half")
check("see-through: half the gameplay shows through it", 30 < px(half, 0.65, 0.05)[0] < 140, True)
bright = frame(custom, "bright", {"chat": {"brightness": 100}})
check("the chat's own picture settings change the chat...", sum(px(bright, 0.65, 0.05)) - sum(px(im, 0.65, 0.05)) > 60, True)
check("...and not the gameplay", abs(px(bright, 0.5, 0.5)[0] - px(im, 0.5, 0.5)[0]) < 8, True)
styled = frame(dict(custom, chat_style={"key": False, "op": 100, "r": 40, "bw": 12, "bc": "00FF00", "kc": "000000", "ks": 25}), "styled")
check("rounded corners: the corner of the box shows what's behind", px(styled, 0.601, 0.021)[0] > 60, True)
gx, gy = 0.6 + 0.36 / 2, 0.02 + 0.003                 # the middle of its top edge: the border
check("a border round it", px(styled, gx, gy)[1] > 180 and px(styled, gx, gy)[0] < 120, True)

# the chat on an ordinary layout, and with a stacked cam
blur = templates.clean_look({"layout": "blur", "chat": {"x": 60, "y": 2, "w": 36, "h": 24}, "chat_src": CHAT})
check("the chat box works on any layout", sum(px(frame(blur, "blur"), 0.65, 0.05)) < 90, True)
stack = templates.clean_look({"layout": "crop", "cam": {"x": 0, "y": 0, "w": 100, "h": 30}, "cam_src": GAME,
                              "chat": {"x": 60, "y": 75, "w": 36, "h": 20}, "chat_src": CHAT})
check("...with a cam stacked on top too", sum(px(frame(stack, "stack"), 0.65, 0.78)) < 90, True)

# a whole clip, every part at once
full = templates.clean_look({**custom, "cam": {"x": 4, "y": 2, "w": 40, "h": 22}, "cam_src": GAME, "cam_style": {"r": 20, "bw": 6},
                             "chat_style": {"key": True, "op": 80, "r": 20, "bw": 4}, "hook": {"show": True}, "cap": {"show": True},
                             "adjust": {"main": {"contrast": 20}, "cam": {"temp": 30}, "chat": {"brightness": 20}}})
out = render.render_clip(src, d / "o", "full", {"start": 0.2, "end": 2.5, "look": full, "hook": "TEST", "title": "t", "description": "d"},
                         [{"w": "hello", "s": 0.5, "e": 1.0}], 1280, 720)
check("a whole clip with the Custom layout, cam, chat and captions is made", (out["layout"], (d / "o" / out["file"]).stat().st_size > 1000), ("custom", True))
check("a small chat box from a sharp corner asks for a sharper video",
      render.needed_height(templates.clean_look({"layout": "blur", "chat": {"x": 0, "y": 0, "w": 100, "h": 40}, "chat_src": {"x": 0.8, "y": 0, "w": 0.2, "h": 0.3}})),
      lambda q: q >= 1440)

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
