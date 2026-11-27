"""A streamer's recent Twitch VODs on their page: the login is read from their Twitch link, the list comes from Twitch
newest first (asked once every few minutes), a streamer without Twitch gets no list, and an unknown streamer is not
found. Twitch is replaced by a fake; nothing goes online."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-vods-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0")
sys.path.insert(0, ".")
from fastapi.testclient import TestClient   # noqa: E402

from studio import auth, creators, ctx, db as database, discover, server   # noqa: E402

database.init()
fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


check("the login comes from a Twitch link", discover.twitch_login("https://www.twitch.tv/CaseOh_"), "caseoh_")
check("...also with more after it", discover.twitch_login("https://twitch.tv/caseoh_/videos?filter=archives"), "caseoh_")
check("a VOD link is not a channel", discover.twitch_login("https://www.twitch.tv/videos/2940013635"), "")
check("other sites have no Twitch login", discover.twitch_login("https://www.youtube.com/@caseoh"), "")

asked = []


def fake_gql(query, variables):
    asked.append(variables)
    return {"user": {"videos": {"edges": [
        {"node": {"id": "2940013635", "title": "LOSING MY MIND", "lengthSeconds": 14993, "createdAt": "2026-11-25T03:01:33Z",
                  "previewThumbnailURL": "https://static-cdn.jtvnw.net/cf_vods/abc/thumb0-320x180.jpg"}},
        {"node": {"id": "2938655753", "title": "", "lengthSeconds": 37936, "createdAt": "2026-11-23T20:02:25Z",
                  "previewThumbnailURL": "https://vod-secure.twitch.tv/_404/404_processing_320x180.png"}}]}}}


discover._gql = fake_gql
got = discover.recent_vods("caseoh_")
check("VODs come newest first, with length and link", [(v["id"], v["length"], v["url"]) for v in got],
      [("2940013635", 14993, "https://www.twitch.tv/videos/2940013635"), ("2938655753", 37936, "https://www.twitch.tv/videos/2938655753")])
check("a picture goes through PeakCut's own address", got[0]["pic"].startswith("/api/discover/img?") or got[0]["pic"] == "", True)
check("a VOD still being processed has no picture", got[1]["pic"], "")
discover.recent_vods("caseoh_")
check("Twitch is asked once every few minutes, not on every look", len(asked), 1)
check("a made-up login isn't sent to Twitch", (discover.recent_vods("../etc"), len(asked)), ([], 1))

c = TestClient(server.app, base_url="http://127.0.0.1:8765")
u = auth.local_user().id
with ctx.as_user(u):
    tw = creators.create({"name": "CaseOh", "channel": "https://www.twitch.tv/caseoh_"})
    yt = creators.create({"name": "Someone", "channel": "https://www.youtube.com/@someone"})
r = c.get(f"/api/creators/{tw['id']}/vods").json()
check("a Twitch streamer's page lists their VODs", (r["login"], len(r["vods"])), ("caseoh_", 2))
check("a streamer without Twitch gets no list", c.get(f"/api/creators/{yt['id']}/vods").json(), {"login": "", "vods": []})
check("an unknown streamer is not found", c.get("/api/creators/nobody123/vods").status_code, 404)


def down(query, variables):
    raise OSError("no network")


discover._gql = down
discover._cache.clear()
check("Twitch not answering says so", c.get(f"/api/creators/{tw['id']}/vods").status_code, 502)

print(f"{len(fails)} failed" if fails else "ALL PASSED")
sys.exit(1 if fails else 0)
