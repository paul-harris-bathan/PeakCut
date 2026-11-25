"""Checks on each finished clip: no sound, very quiet, crackling, a black picture, or a frozen one (found in the
source, since captions keep moving over a frozen picture). A normal clip gets no warnings, and a check that can't
run never fails a clip. Clips are made from test patterns; nothing is downloaded."""
import os, subprocess, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-cc-"); os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from pathlib import Path   # noqa: E402
from studio import clipcheck, render   # noqa: E402
fails = []


def check(n, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


d = Path(os.environ["PEAKCUT_DATA"])
V = {"moving": "testsrc2=s=1280x720:d=14:r=30", "black": "color=c=black:s=1280x720:d=14:r=30",
     "frozen": "testsrc2=s=1280x720:d=14:r=30,trim=end_frame=1,loop=-1:1,setpts=N/30/TB,trim=duration=14"}
A = {"speech": "sine=f=300:d=14", "silent": "anullsrc=r=48000:cl=stereo,atrim=duration=14", "quiet": "sine=f=300:d=14,volume=0.002"}
words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.35} for i, w in enumerate(("captions that keep changing on the screen " * 5).split())]


def made(v, a, start=2.0, end=10.0):
    src = d / f"{v}-{a}.mp4"
    if not src.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", V[v], "-f", "lavfi", "-i", A[a], "-shortest",
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    clip = {"start": start, "end": end, "style": "bounce", "layout": "blur", "hook": "WAIT FOR IT", "captions": True}
    out = d / f"o-{v}-{a}-{start}"
    got = render.render_clip(src, out, "c", clip, [dict(w, s=w["s"] + start, e=w["e"] + start) for w in words], 1280, 720)
    return [x["code"] for x in clipcheck.check(out / got["file"], end - start, src, start)]


check("a normal clip gets no warnings", made("moving", "speech"), [])
check("a clip with no sound says so", made("moving", "silent"), ["silent"])
check("a very quiet clip says so", made("moving", "quiet"), ["quiet"])
check("a black picture says so", made("black", "speech"), ["black"])
check("a frozen picture says so, though the captions over it move", made("frozen", "speech"), ["frozen"])
check("...also for a clip that starts later in the video", made("frozen", "speech", start=4.0, end=12.0), ["frozen"])
check("two problems at once are both told", made("black", "silent"), ["silent", "black"])

m = {"lufs": -14.0, "peak": 1.2, "black": 0.0, "frozen": 0.0}
check("sound peaking above 0.5 dB is called crackly", [x["code"] for x in clipcheck.judge(m, 10)], ["loud"])
check("each warning has words for the person posting", all(x["text"] for x in clipcheck.judge(m, 10)), True)
check("black for under half the clip is fine", clipcheck.judge(dict(m, peak=-3.0, black=4.0), 10), [])
check("a clip that can't be read gets no warnings, and no error", clipcheck.check(d / "missing.mp4", 10), [])

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
