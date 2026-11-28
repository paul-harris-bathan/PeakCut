"""Chat spikes in a Twitch VOD: links are read, chat speed is worked out from Twitch's answers (which start a little
early, and are empty when Twitch holds back), spikes are judged against the stream's own normal, and scans are kept
per user and need no AI key. Twitch is replaced by a fake; nothing goes online."""
import os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-spikes-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_AI_KEY="",
                  PEAKCUT_AI_KEY_FILE="", PEAKCUT_AI_MODELS="", PEAKCUT_AI_PROVIDER="", PEAKCUT_UPDATE_CHECK="0")
sys.path.insert(0, ".")
from fastapi.testclient import TestClient   # noqa: E402

from studio import db as database, server, spikes   # noqa: E402

database.init()
spikes.muted = lambda vid, client=None: []   # Twitch isn't asked which stretches are muted
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


# ---- links
check("a VOD link gives its number", spikes.vod_id("https://www.twitch.tv/videos/2940013635"), "2940013635")
check("...also with extra bits", spikes.vod_id("https://twitch.tv/videos/2940013635?t=1h2m"), "2940013635")
check("...and the bare number", spikes.vod_id("2940013635"), "2940013635")
check("a channel link is not a VOD", spikes.vod_id("https://www.twitch.tv/caseoh_"), "")
check("other sites are not VODs", spikes.vod_id("https://www.youtube.com/watch?v=abc"), "")

# ---- chat speed from one answer
check("an answer spanning the step is counted exactly", spikes._rate([97, 99, 100, 101, 105, 109, 110, 112], 100, True), 0.4)
check("busy chat: messages over the seconds covered", spikes._rate([97] * 10 + [98] * 10 + [99] * 10 + [100] * 20, 100, True), 12.5)
check("the next message after this step: no chat here", spikes._rate([130, 131], 100, True), 0.0)
check("the last messages of the VOD count exactly", spikes._rate([99, 100, 103], 100, False), 0.2)
check("no messages: no chat", spikes._rate([], 100, False), 0.0)

# ---- spikes, against the stream's own normal
normal = [2.0] * 600                      # 100 minutes of 2 messages a second
r = list(normal)
r[200] = 12.0                             # 6x: big
r[400] = 5.5                              # 2.75x: worth a look (big starts at 3x)
r[402] = 5.0                              # 20 s later: the same moment
r[500] = None                             # a sample Twitch didn't answer
found = spikes.find(r)
check("a big spike and one worth a look are found", [(s["t"], s["level"]) for s in found], [(2000, "big"), (4000, "minor")])
check("...each says how many times the normal it is", [s["times"] for s in found], [6.0, 2.8])
check("3x the normal is big", spikes.find([2.0] * 300 + [6.0] + [2.0] * 300)[0]["level"], "big")
check("a gap never becomes a spike", all(s["t"] != 5000 for s in found))
check("pickier: the small one drops out", [s["t"] for s in spikes.find(r, 1.5)], [2000])
check("looser: the big one stays big, more are found", [s["level"] for s in spikes.find(r, 0.5)][:1], ["big"])
quiet = [0.0] * 300
quiet[100] = 1.0
check("a near-silent chat: one message isn't a big spike", [s["level"] for s in spikes.find(quiet)], ["big"] if 1.0 / spikes.MIN_NORMAL >= spikes.BIG else ["minor"])
busy = [20.0] * 300
busy[150] = 50.0
check("a huge chat is judged against its own normal", [s["level"] for s in spikes.find(busy)], ["minor"])
check("nothing but gaps: no spikes", spikes.find([None] * 50), [])


# ---- sampling: Twitch's empty answers are asked again; only the end of the VOD can be really empty
class FakeTwitch:
    def __init__(self, holdback=1):
        self.asked, self.holdback, self.seen = 0, holdback, {}

    def post(self, url, json):
        o = json["variables"]["o"]
        self.asked += 1
        self.seen[o] = self.seen.get(o, 0) + 1
        empty = self.seen[o] <= self.holdback or o >= 990
        edges = [] if empty else [{"node": {"contentOffsetSeconds": o - 2 + k // 5}} for k in range(50)]
        data = {"data": {"video": {"comments": {"edges": edges, "pageInfo": {"hasNextPage": not empty}}}}}

        class R:
            def raise_for_status(self): pass
            def json(self): return data
        return R()


spikes.time.sleep = lambda s: None        # no real waiting between retries
fake = FakeTwitch(holdback=1)
got = spikes.sample("123456789", 1000, client=fake)
check("one sample every 10 s", len(got), 100)
check("an empty answer is asked again, then counted", got[10] is not None and got[10] > 0)
check("the end of the VOD can be really empty", got[-1], 0.0)
fake = FakeTwitch(holdback=99)
got = spikes.sample("123456789", 100, client=fake)
check("a sample Twitch never answers is a gap, not zero", got[:3], [None, None, None])
check("...after a few tries", fake.seen[0], spikes.RETRIES)

# ---- the pages, as one user, with Twitch replaced
c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}
spikes.vod_info = lambda vid, client=None: {"id": vid, "title": "Big stream", "length": 6000, "created": "", "streamer": "Streamer",
                                            "login": "streamer", "picture": ""}
spikes.sample = lambda vid, length, progress=None, client=None, workers=3: (progress and progress(1.0)) or list(r)

check("a non-VOD link is refused with what to paste", c.post("/api/spikes", json={"url": "https://twitch.tv/caseoh_"}, headers=H).json().get("detail", ""),
      lambda d: "Twitch VOD" in d and "YouTube" in d and "kick.com" in d)
check("starting from another website is refused", c.post("/api/spikes", json={"url": "2940013635"}, headers={"Origin": "https://evil.example"}).status_code, 403)
r1 = c.post("/api/spikes", json={"url": "https://www.twitch.tv/videos/2940013635"}, headers=H)
check("a scan starts with no AI key, and says what video it is", (r1.status_code, r1.json().get("title"), r1.json().get("status")), (200, "Big stream", "scanning"))
for _ in range(50):
    g = c.get("/api/spikes/2940013635")
    if g.status_code == 200 and g.json().get("status") == "done":
        break
    time.sleep(0.1)
g = c.get("/api/spikes/2940013635").json()
check("the finished scan has its chat speeds and spikes", (g["status"], len(g["rates"]), [s["level"] for s in g["spikes"]]), ("done", 600, ["big", "minor"]))
check("pickier from the page", [s["t"] for s in c.get("/api/spikes/2940013635?s=1.5").json()["spikes"]], [2000])
check("the list shows it, without the chat speeds", [(x["id"], "rates" in x) for x in c.get("/api/spikes").json()], [("2940013635", False)])
check("no chat text is kept, only timings", "message" not in (server.config.USERS_DIR / "u_local" / "spikes" / "2940013635.json").read_text())
check("an unknown scan is not found", c.get("/api/spikes/1111111111").status_code, 404)
check("a bad id is refused", c.get("/api/spikes/..%2Fjob").status_code, lambda s: s in (400, 404))
check("removing from another website is refused", c.delete("/api/spikes/2940013635", headers={"Origin": "https://evil.example"}).status_code, 403)
check("it can be removed", (c.delete("/api/spikes/2940013635", headers=H).status_code, c.get("/api/spikes").json()), (200, []))

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
