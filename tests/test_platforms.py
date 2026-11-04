"""End to end with per-platform buttons: batch -> send -> taps -> post to Bluesky and LinkedIn.

Papers come from the live APIs; Gemini, Telegram, Bluesky and LinkedIn are stand-ins. Runs in a throwaway copy.
"""
import atexit
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="neuropaperbot-platforms-"))  # a throwaway copy of the project
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
for name in ("scripts", "assets"):
    shutil.copytree(PROJECT / name, WORK / name, ignore=shutil.ignore_patterns("__pycache__"))
(WORK / "data").mkdir()
for name, empty in (("drafts", []), ("seen", {}), ("posted", []), ("state", {"telegram_offset": 0, "last_batch_date": None})):
    (WORK / "data" / f"{name}.json").write_text(json.dumps(empty), encoding="utf-8")
cfg = json.loads((PROJECT / "config.json").read_text(encoding="utf-8"))
cfg["posting"]["platforms"] = ["bluesky", "linkedin"]


def save_cfg():
    (WORK / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


save_cfg()
sys.path.insert(0, str(WORK / "scripts"))
os.environ.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="111", BLUESKY_USERNAME="@bot.bsky.social",
                  BLUESKY_PASSWORD="fake", LINKEDIN_ACCESS_TOKEN="fake-li")

import atproto  # noqa: E402
from atproto import models  # noqa: E402

import batch  # noqa: E402
import compose  # noqa: E402
import linkedin_api  # noqa: E402
import linkedin_login  # noqa: E402
import post  # noqa: E402
import review  # noqa: E402
import telegram_api  # noqa: E402
from common import DRAFTS, POSTED, iso, now_utc, read_json, write_json  # noqa: E402

failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


# ---------- stand-ins ----------
class Resp:
    def __init__(self, text):
        self.text = text


class FakeGemini:
    def __init__(self):
        self.models = self

    def generate_content(self, *, model, contents, config=None):
        if config:
            ids = re.findall(r"^ID: (p\d+)$", contents, re.M)
            titles = re.findall(r"^TITLE: (.*)$", contents, re.M)
            return Resp(json.dumps([{"id": i, "score": 1 + int(hashlib.md5(t.encode()).hexdigest(), 16) % 10,
                                     "reason": "fake reason"} for i, t in zip(ids, titles)]))
        return Resp("Neurons in this circuit fire in a sequence that predicts the animal's next choice.")


class FakeTelegram:
    def __init__(self):
        self.calls, self.updates, self.next_id = [], [], 100

    def call(self, method, files=None, **params):
        self.calls.append((method, params, files))
        if method in ("sendPhoto", "sendMessage"):
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "getUpdates":
            return [u for u in self.updates if u["update_id"] >= (params.get("offset") or 0)]
        if method in ("editMessageCaption", "editMessageText"):
            return True
        raise AssertionError(f"unexpected Telegram call {method}")

    def of(self, method):
        return [c for c in self.calls if c[0] == method]


BLOB = models.blob_ref.BlobRef.model_validate(
    {"$type": "blob", "ref": {"$link": "bafkreibme22gw2h7y2h7tg2fhqotaqjucnbc24deqo72b6mkl2egezxhvy"},
     "mimeType": "image/png", "size": 1234}
)


class FakeBluesky:
    posts = []  # (uri, cid, embed), shared like a real account
    fail = False
    last = None

    def __init__(self):
        self.com = SimpleNamespace(atproto=SimpleNamespace(identity=SimpleNamespace(resolve_handle=self.resolve)))

    def login(self, login, password):
        assert not login.startswith("@"), "login must not include @"
        self.me = SimpleNamespace(did="did:plc:bot")

    def resolve(self, params):
        if params["handle"] == "alice.bsky.social":
            return SimpleNamespace(did="did:plc:alice")
        raise ValueError("no such handle")

    def get_author_feed(self, actor, limit=None, **_):
        items = [SimpleNamespace(post=SimpleNamespace(uri=u, cid=c, record=SimpleNamespace(embed=e)))
                 for u, c, e in reversed(FakeBluesky.posts)]
        return SimpleNamespace(feed=items[:limit])

    def upload_blob(self, data):
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        return SimpleNamespace(blob=BLOB)

    def send_post(self, text, facets=None, embed=None, langs=None):
        if FakeBluesky.fail:
            raise RuntimeError("PDS unavailable")
        n = len(FakeBluesky.posts) + 1
        uri, cid = f"at://did:plc:bot/app.bsky.feed.post/3kpost{n}", f"bafycid{n}"
        FakeBluesky.posts.append((uri, cid, embed))
        FakeBluesky.last = SimpleNamespace(text=text, facets=facets or [], embed=embed)
        return SimpleNamespace(uri=uri, cid=cid)


class LIResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeLinkedIn:
    def __init__(self):
        self.calls, self.fail_status, self.n = [], None, 0

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append(SimpleNamespace(method=method, url=url, headers=headers or {}, body=kw.get("json"), data=kw.get("data")))
        if self.fail_status:
            return LIResp(self.fail_status, {"message": "nope"})
        if url.endswith("/v2/userinfo"):
            return LIResp(200, {"sub": "abc123", "name": "Test Person"})
        if "action=initializeUpload" in url:
            return LIResp(200, {"value": {"uploadUrl": "https://www.linkedin.com/dms-uploads/fake", "image": "urn:li:image:FAKE"}})
        if url.startswith("https://www.linkedin.com/dms-uploads/"):
            return LIResp(201)
        if url.endswith("/rest/posts"):
            self.n += 1
            return LIResp(201, None, {"x-restli-id": f"urn:li:share:{7000 + self.n}"})
        raise AssertionError(url)

    def posts(self):
        return [c for c in self.calls if c.url.endswith("/rest/posts")]


tg = FakeTelegram()
li = FakeLinkedIn()
telegram_api.call = tg.call
batch.gemini_client = lambda: FakeGemini()
atproto.Client = FakeBluesky
linkedin_api.session = li


def run_post(*argv):
    sys.argv = ["post.py", *argv]
    try:
        post.main()
        return None
    except SystemExit as exc:
        return str(exc)


def drafts():
    return {d["id"]: d for d in read_json(DRAFTS, [])}


def edits(message_id):
    return [p for _, p, _ in tg.of("editMessageCaption") if p["message_id"] == message_id]


def labels(markup):
    return [[b["text"] for b in row] for row in markup["inline_keyboard"]]


def tap(update_id, data, message_id, chat=111):
    return {"update_id": update_id, "callback_query": {"id": f"cq{update_id}", "data": data,
                                                        "message": {"message_id": message_id, "chat": {"id": chat}}}}


def reply(update_id, text, to=None, chat=111):
    msg = {"message_id": 900 + update_id, "chat": {"id": chat}, "text": text}
    if to:
        msg["reply_to_message"] = {"message_id": to}
    return {"update_id": update_id, "message": msg}


# ---------- 0. little text ----------
print("0. LinkedIn text escaping")
check(linkedin_api.commentary("a (b) [c] @d e_f #neuroscience") == "a \\(b\\) \\[c\\] \\@d e\\_f {hashtag|\\#|neuroscience}",
      "reserved characters escaped, hashtag kept clickable")

# ---------- 1. batch ----------
print("1. batch")
sys.argv = ["batch.py", "--force", "--start", "2026-10-28", "--end", "2026-10-30"]
batch.main()
ds = read_json(DRAFTS, [])
check(len(ds) == 3 and all(d["status"] == "ready" for d in ds), "3 ready drafts")
d1, d2, d3 = (d["id"] for d in ds)

# ---------- 2. send ----------
print("2. send")
review.send(dry_run=False)
photos = tg.of("sendPhoto")
m1, m2, m3 = (drafts()[i]["tg_message_id"] for i in (d1, d2, d3))
check(len(photos) == 3, "3 photos sent")
for _, params, _ in photos:
    check(compose.visible_length(params["caption"]) <= 1024, "caption fits")
    check(all(len(b["callback_data"].encode()) <= 64 for row in params["reply_markup"]["inline_keyboard"] for b in row),
          "button data short enough")
check(labels(photos[0][1]["reply_markup"]) == [["🦋 Bluesky", "💼 LinkedIn"], ["❌ Skip"]],
      f"one button per platform, then Skip: {labels(photos[0][1]['reply_markup'])}")

# ---------- 3. taps ----------
print("3. taps")
edited = "Edited hook (with brackets) for @alice.bsky.social, see https://example.org/a_b."
tg.updates = [
    tap(500, f"to:bluesky:{d1}", m1),
    tap(501, f"to:linkedin:{d1}", m1),
    tap(502, f"skip:{d2}", m2),
    reply(503, edited, to=m3),
    tap(504, f"to:bluesky:{d3}", m3, chat=999),  # someone else's chat: ignored
    reply(505, "hello"),  # not a reply to a draft: gets the help text
    tap(506, f"to:linkedin:{d3}", m3),
    reply(507, "x " * 200, to=m3),  # too long: refused
]
review.poll()
s = drafts()
check(s[d1]["status"] == "approved" and s[d1]["targets"] == ["bluesky", "linkedin"], "two taps queue d1 for both")
check(s[d2]["status"] == "skipped", "Skip")
check(s[d3]["status"] == "approved" and s[d3]["targets"] == ["linkedin"], "d3 queued for LinkedIn only")
check(s[d3]["hook"] == edited, "reply replaced d3's hook")
check(labels(edits(m1)[-1]["reply_markup"]) == [["↩️ Undo"]], "d1: both platforms picked, only Undo left")
check(labels(edits(m3)[-1]["reply_markup"]) == [["🦋 Bluesky"], ["↩️ Undo"]], "d3: Bluesky still offered, plus Undo")
check("Queued for LinkedIn" in edits(m3)[-1]["caption"], "d3's message says it's queued for LinkedIn")
texts = [p["text"] for _, p, _ in tg.of("sendMessage")]
check(any(t.startswith("Every weekday") for t in texts), "help text for a stray message")
check("Updated the hook." in texts and any("at most" in t for t in texts), "edit confirmed, too-long edit refused")

# ---------- 4. post to both ----------
print("4. post to both")
check(run_post() is None, "post run succeeds")
check(len(FakeBluesky.posts) == 1 and FakeBluesky.last.embed.external.uri == s[d1]["link"], "Bluesky got d1 as a link card")
check(len(li.posts()) == 1, "LinkedIn got one post")
body = li.posts()[0].body
article = body["content"]["article"]
check(body["author"] == "urn:li:person:abc123" and body["visibility"] == "PUBLIC", "posted publicly as the logged-in member")
check(article["source"] == s[d1]["link"] and article["title"] == s[d1]["title"] and article["thumbnail"] == "urn:li:image:FAKE",
      "LinkedIn link card has the link, title and uploaded card image")
check("{hashtag|\\#|neuroscience}" in body["commentary"], "LinkedIn hashtag is clickable")
headers = li.posts()[0].headers
check(headers.get("LinkedIn-Version") == linkedin_api.VERSION and headers.get("X-Restli-Protocol-Version") == "2.0.0",
      "versioned API headers sent")
uploads = [c for c in li.calls if c.method == "PUT"]
check(len(uploads) == 1 and uploads[0].data[:4] == b"\x89PNG" and "LinkedIn-Version" not in uploads[0].headers,
      "card image uploaded as a PNG")
s = drafts()
check(s[d1]["status"] == "posted" and set(s[d1]["posted"]) == {"bluesky", "linkedin"}, "d1 posted to both")
check(s[d1]["posted"]["linkedin"]["url"] == "https://www.linkedin.com/feed/update/urn:li:share:7001/", "LinkedIn post link saved")
cap = edits(m1)[-1]["caption"]
check("Posted to Bluesky" in cap and "Posted to LinkedIn" in cap, "Telegram message links both posts")
check(edits(m1)[-1]["reply_markup"]["inline_keyboard"] == [], "no buttons left on a posted draft")
check(sorted(x["platform"] for x in read_json(POSTED, [])) == ["bluesky", "linkedin"], "posted.json: one entry per platform")

# ---------- 5. spacing ----------
print("5. spacing per platform")
check(run_post() is None and len(li.posts()) == 1, "d3 waits: LinkedIn posted less than 2 hours ago")
check(run_post("--now") is None and len(li.posts()) == 2, "--now posts d3 to LinkedIn")
text = li.posts()[-1].body["commentary"]
check(text.startswith("Edited hook \\(with brackets\\) for \\@alice.bsky.social, see https://example.org/a\\_b."),
      f"edited hook escaped for LinkedIn: {text[:70]!r}")
check(drafts()[d3]["status"] == "posted", "d3 posted")

# ---------- 6. expired LinkedIn login ----------
print("6. expired LinkedIn login")
tg.updates += [tap(508, f"undo:{d2}", m2), tap(509, f"to:linkedin:{d2}", m2)]
review.poll()
check(drafts()[d2]["status"] == "approved" and drafts()[d2]["targets"] == ["linkedin"], "Undo the Skip, then queue for LinkedIn")
li.fail_status = 401
result = run_post("--now")
check(bool(result) and "linkedin_login.py" in result, f"run fails and says to log in again: {result}")
s = drafts()
check(s[d2]["status"] == "approved" and not s[d2].get("attempts") and "linkedin" in s[d2].get("notes", {}),
      "draft stays queued; an expired login isn't counted as a failed try")
check("login has expired" in edits(m2)[-1]["caption"], "Telegram message says to log in again")
li.fail_status = None
check(run_post("--now") is None and drafts()[d2]["status"] == "posted" and not drafts()[d2].get("notes"),
      "after logging in again it posts, and the warning goes away")

# ---------- 7. old button, failures, partial undo ----------
print("7. old Post button, repeated failures, partial Undo")
ds = read_json(DRAFTS, [])
base = {k: v for k, v in ds[0].items() if k not in ("targets", "queued_at", "posted", "failed", "notes", "attempts")}
old = dict(base, id="old0000000", status="sent", tg_message_id=150, link="https://doi.org/10.1/old")
part = dict(base, id="part000000", status="approved", tg_message_id=151, link="https://doi.org/10.1/part",
            targets=["bluesky", "linkedin"], queued_at={"bluesky": "2026-11-03T10:00:00+00:00", "linkedin": "2026-11-03T10:00:00+00:00"},
            posted={"bluesky": {"url": "https://bsky.app/x", "posted_at": "2026-11-03T10:00:00+00:00"}})
write_json(DRAFTS, ds + [old, part])
tg.updates += [tap(510, "post:old0000000", 150), tap(511, "undo:part000000", 151)]
review.poll()
s = drafts()
check(s["old0000000"]["targets"] == ["bluesky"] and s["old0000000"]["status"] == "approved",
      "the first version's Post button still queues for Bluesky")
check(s["part000000"]["status"] == "posted" and s["part000000"]["targets"] == ["bluesky"],
      "Undo after the Bluesky post drops only the LinkedIn part")
FakeBluesky.fail = True
results = [run_post("--now") for _ in range(3)]
check(all(r and "PDS unavailable" in r for r in results), "each failing run exits with an error")
s = drafts()
check(s["old0000000"]["status"] == "failed" and s["old0000000"]["failed"].get("bluesky"), "marked failed after 3 tries")
check("Bluesky failed" in edits(150)[-1]["caption"], "Telegram message shows the failure")
FakeBluesky.fail = False

# ---------- 8. duplicate guard ----------
print("8. Bluesky duplicate guard")
ds = read_json(DRAFTS, [])
for d in ds:
    if d["id"] == d1:
        d["posted"].pop("bluesky")  # as if the run that posted it never saved
        d["status"] = "approved"
write_json(DRAFTS, ds)
n = len(FakeBluesky.posts)
check(run_post("--now") is None and len(FakeBluesky.posts) == n, "already on Bluesky: not posted again")
check(drafts()[d1]["status"] == "posted" and "bluesky" in drafts()[d1]["posted"], "...and recorded as posted")

# ---------- 9. expiry and dry runs ----------
print("9. expiry and dry runs")
ds = read_json(DRAFTS, [])
stale = dict(base, id="stale00000", status="sent", tg_message_id=160, sent_at=iso(now_utc() - timedelta(days=4)),
             link="https://doi.org/10.1/stale")
fresh = dict(base, id="fresh00000", status="ready", tg_message_id=None, link="https://doi.org/10.1/fresh")
write_json(DRAFTS, ds + [stale, fresh])
sent_before = len(tg.of("sendPhoto"))
review.send(dry_run=True)
check(len(tg.of("sendPhoto")) == sent_before and (WORK / "previews" / "fresh00000.png").exists(),
      "send --dry-run saves the card and sends nothing")
check(drafts()["stale00000"]["status"] == "sent", "send --dry-run expires nothing")
review.send(dry_run=False)
check(drafts()["stale00000"]["status"] == "expired" and drafts()["fresh00000"]["status"] == "sent", "stale draft expired, new one sent")
tg.updates += [tap(512, "to:linkedin:fresh00000", drafts()["fresh00000"]["tg_message_id"])]
review.poll()
n = len(li.posts())
check(run_post("--now", "--dry-run") is None and len(li.posts()) == n, "post --dry-run posts nothing")

# ---------- 10. posting off ----------
print("10. posting turned off")
cfg["posting"]["platforms"] = []
save_cfg()
check(run_post("--now") is None and len(li.posts()) == n, "empty posting.platforms posts nothing and doesn't fail")
check(drafts()["fresh00000"]["status"] == "approved", "...and the queued draft keeps waiting")

# ---------- 11. LinkedIn login helper ----------
print("11. LinkedIn login helper")


def login_attempt(expected_state, sent_state):
    got = {}

    def run():
        try:
            got["code"] = linkedin_login.wait_for_code(expected_state, timeout=20)
        except SystemExit as exc:
            got["error"] = str(exc)

    thread = threading.Thread(target=run)
    thread.start()
    page = ""
    for _ in range(100):  # until the little server is listening
        try:
            url = f"http://localhost:{linkedin_login.PORT}/callback?code=abc123&state={sent_state}"
            page = urllib.request.urlopen(url, timeout=5).read().decode()
            break
        except OSError:
            time.sleep(0.1)
    thread.join(15)
    return got, page


got, page = login_attempt("s1", "s1")
check(got.get("code") == "abc123" and "Logged in" in page, "catches LinkedIn's redirect and reads the code")
got, page = login_attempt("s2", "wrong")
check("doesn't belong" in got.get("error", "") and "Login failed" in page, "a reply for a different login attempt is refused")


class TokenResp:
    status_code = 200

    def json(self):
        return {"access_token": "tok-123", "expires_in": 5184000}


real_post = linkedin_login.requests.post
linkedin_login.requests.post = lambda *a, **k: TokenResp()
try:
    check(linkedin_login.exchange("abc123", "cid", "secret") == ("tok-123", 5184000), "code traded for a 60-day token")
finally:
    linkedin_login.requests.post = real_post
linkedin_login.save_to_env("tok-123")
check("LINKEDIN_ACCESS_TOKEN=tok-123" in (WORK / ".env").read_text(encoding="utf-8"), "token saved in .env")
(WORK / ".env").write_text("A=1\nLINKEDIN_ACCESS_TOKEN=old\nB=2\n", encoding="utf-8")
linkedin_login.save_to_env("tok-456")
check((WORK / ".env").read_text(encoding="utf-8") == "A=1\nLINKEDIN_ACCESS_TOKEN=tok-456\nB=2\n", "an old token is replaced in place")

# ---------- 12. platform changes redraw open drafts ----------
print("12. turning a platform on or off redraws drafts still waiting")
cfg["posting"]["platforms"] = ["linkedin"]
save_cfg()
open_drafts = [d for d in read_json(DRAFTS, []) if d["status"] in ("sent", "approved") and d.get("tg_message_id")]
before = len(tg.of("editMessageCaption"))
review.send(dry_run=False)
redrawn = [p for _, p, _ in tg.of("editMessageCaption")[before:]]
check(open_drafts and sorted(p["message_id"] for p in redrawn) == sorted(d["tg_message_id"] for d in open_drafts),
      f"every open draft redrawn once ({len(open_drafts)})")
check(all(drafts()[d["id"]]["buttons_for"] == ["linkedin"] for d in open_drafts), "...and remembers which platforms it shows")
before = len(tg.of("editMessageCaption"))
review.send(dry_run=False)
check(len(tg.of("editMessageCaption")) == before, "no redraw when nothing changed")
legacy = dict(base, id="legacy0000", status="sent", tg_message_id=170, link="https://doi.org/10.1/legacy")
legacy.pop("buttons_for", None)
write_json(DRAFTS, read_json(DRAFTS, []) + [legacy])
review.send(dry_run=False)
check(labels(edits(170)[-1]["reply_markup"]) == [["💼 LinkedIn"], ["❌ Skip"]], "a draft sent before this change gets the LinkedIn button")

print("\nALL OK" if not failures else f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
