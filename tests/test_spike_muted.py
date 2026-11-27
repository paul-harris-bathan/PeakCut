"""Twitch-muted stretches: asked of Twitch with the scan (and again later, since Twitch can mute later), joined where
they touch; a spike whose clip would be mostly muted is marked; ticking one doesn't make a clip (it would have no
sound) and says why; one picked before it was known is skipped when its turn comes; once Twitch gives the sound back
it can be clipped. Twitch is replaced by a fake."""
import json, os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-spkmute-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0",
                  PEAKCUT_CLIP_CHECKS="0")
sys.path.insert(0, ".")
from studio import auth, ctx, db as database, jobs, spikeclip, spikes   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


# ---- what Twitch says is muted
asked = []


class Fake:
    def __init__(self, nodes):
        self.nodes = nodes

    def post(self, url, json=None):
        asked.append(json["query"])
        nodes = self.nodes

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"video": {"muteInfo": {"mutedSegmentConnection": {"nodes": nodes}}}}}
        return R()


got = spikes.muted("123456789", Fake([{"offset": 1087, "duration": 180}, {"offset": 4511, "duration": 180}, {"offset": 4331, "duration": 180},
                                      {"offset": 9000, "duration": 0}]))
check("muted stretches come in order, joined where they touch, empty ones dropped", got, [[1087.0, 1267.0], [4331.0, 4691.0]])
check("...asked of Twitch with the video's mute info", "muteInfo" in asked[-1])
check("a VOD with nothing muted has no stretches", spikes.muted("123456789", Fake([])), [])

# ---- which spikes are muted
R = [[1000.0, 1180.0]]
check("a spike in the middle of a muted stretch is muted", spikes.is_muted(R, 1100), True)
check("a spike well after it is not", spikes.is_muted(R, 1300), False)
check("a spike whose clip is only a little muted is not", spikes.is_muted(R, 1195), False)
check("how much is muted is measured", round(spikes.muted_share(R, 1170, 1190), 2), 0.5)
rates = [1.0] * 300
rates[110] = rates[200] = 20.0          # spikes at 1100 s (muted) and 2000 s
found = spikes.find(rates, 1.0, R)
check("find() marks only the muted spike", [(s["t"], s.get("muted", False)) for s in found], [(1100, True), (2000, False)])
check("without muted stretches nothing is marked", any(s.get("muted") for s in spikes.find(rates)), False)

# ---- kept with the scan, and asked again later
u = auth.local_user().id
calls = []
spikes.muted = lambda vid, client=None: (calls.append(vid), [[1000.0, 1180.0]])[1]
with ctx.as_user(u):
    spikes._file("5555555555").write_text(json.dumps({"id": "5555555555", "title": "Old scan", "length": 3000, "rates": rates,
                                                      "step": 10, "status": "done", "login": "", "streamer": "S"}), encoding="utf-8")
    d = spikes.get("5555555555")
    check("a scan made before this asks Twitch once", (calls, d["muted"]), (["5555555555"], [[1000.0, 1180.0]]))
    check("...and its spikes show which are muted", [s.get("muted", False) for s in d["spikes"]], [True, False])
    kept = json.loads(spikes._file("5555555555").read_text(encoding="utf-8"))
    check("...kept with the scan (without the spikes list)", ("muted" in kept, "spikes" in kept, len(kept["rates"])), (True, False, 300))
    spikes.get("5555555555")
    check("it isn't asked again straight away", len(calls), 1)
    kept["muted_at"] = time.time() - spikes.MUTED_FRESH - 5
    spikes._file("5555555555").write_text(json.dumps(kept), encoding="utf-8")
    spikes.get("5555555555")
    check("it is asked again after a while (Twitch can mute later)", len(calls), 2)

    def down(vid, client=None):
        calls.append("down")
        raise OSError("no answer")
    spikes.muted = down
    kept = json.loads(spikes._file("5555555555").read_text(encoding="utf-8"))
    kept["muted_at"] = 0
    spikes._file("5555555555").write_text(json.dumps(kept), encoding="utf-8")
    d = spikes.get("5555555555")
    check("if Twitch doesn't answer, what was known is still used", d["spikes"][0].get("muted"), True)
    spikes.get("5555555555")
    check("...and it isn't asked again for a few minutes", calls.count("down"), 1)

    # ---- ticking a muted spike
    spikeclip.need_keys = lambda: None
    put = []
    jobs._q.put = lambda item, user=None: put.append(item)
    scan = spikes.get("5555555555")
    job = spikeclip.clip_spikes(scan, [1100])
    sp = job["spikes"][0]
    check("a ticked muted spike isn't made: it says Twitch muted it", (sp["clip"], "Twitch muted" in sp["error"]), ("muted", True))
    check("...and nothing goes in line for it", put, [])
    check("the page sees it as Twitch muted", spikeclip.summary("5555555555")["spikes"][0]["clip"], "muted")
    check("ticking it again changes nothing", spikeclip.clip_spikes(scan, [1100])["spikes"][0]["clip"], "muted")
    job = spikeclip.clip_spikes(scan, [2000])
    check("a spike with sound still goes in line", (len(put), [s.get("clip") for s in job["spikes"]]), (1, ["muted", None]))

    # ---- picked before it was known: skipped when its turn comes
    job["spikes"].append({"t": 1150, "level": "big", "why": "picked"})
    job["spikes"] = [s for s in job["spikes"] if s["t"] != 2000]
    made = []
    spikeclip._prepare = lambda j, s, n: made.append(s["t"]) or (_ for _ in ()).throw(AssertionError("made"))
    spikeclip.run(job)
    check("a spike found muted only later is skipped, not fetched", (made, job["spikes"][-1]["clip"]), ([], "muted"))
    check("the video says how many were skipped", "skipped (Twitch muted)" in job["stage"], True)

    # ---- Twitch gives the sound back
    scan["muted"] = []
    put.clear()
    job = spikeclip.clip_spikes(scan, [1100])
    check("once Twitch gives the sound back, the spike can be clipped", [s.get("clip") for s in job["spikes"] if s["t"] == 1100], [None])

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
