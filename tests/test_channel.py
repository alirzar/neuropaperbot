"""The public channel and the ready-to-copy messages, with stand-ins for Gemini and Telegram.

Papers come from the live APIs; everything else is stubbed. Runs in a throwaway copy.
"""
import atexit
import contextlib
import hashlib
import html
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="neuropaperbot-channel-"))  # a throwaway copy of the project
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
for name in ("scripts", "assets"):
    shutil.copytree(PROJECT / name, WORK / name, ignore=shutil.ignore_patterns("__pycache__"))
(WORK / "data").mkdir()
for name, empty in (("drafts", []), ("seen", {}), ("posted", []), ("state", {"telegram_offset": 0, "last_batch_date": None})):
    (WORK / "data" / f"{name}.json").write_text(json.dumps(empty), encoding="utf-8")
cfg = json.loads((PROJECT / "config.json").read_text(encoding="utf-8"))
cfg["posting"].update(platforms=["channel", "linkedin"], telegram_channel="@TestChannel")


def save_cfg():
    (WORK / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


save_cfg()
sys.path.insert(0, str(WORK / "scripts"))
os.environ.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="111", LINKEDIN_ACCESS_TOKEN="fake-li")

import batch  # noqa: E402
import compose  # noqa: E402
import post  # noqa: E402
import review  # noqa: E402
import telegram_api  # noqa: E402
from common import DRAFTS, read_json, write_json  # noqa: E402

failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


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
        return Resp("Neurons in this circuit fire in a sequence that predicts the animal's next choice <a & b>.")


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


tg = FakeTelegram()
telegram_api.call = tg.call
batch.gemini_client = lambda: FakeGemini()


def run_post(*argv):
    sys.argv = ["post.py", *argv]
    try:
        post.main()
        return None
    except SystemExit as exc:
        return str(exc)


def drafts():
    return {d["id"]: d for d in read_json(DRAFTS, [])}


def tap(update_id, data, message_id):
    return {"update_id": update_id, "callback_query": {"id": f"cq{update_id}", "data": data,
                                                        "message": {"message_id": message_id, "chat": {"id": 111}}}}


def blocks(text):
    return [html.unescape(b) for b in re.findall(r"<pre>(.*?)</pre>", text, re.S)]


print("1. batch")
sys.argv = ["batch.py", "--force", "--start", "2026-10-28", "--end", "2026-10-30"]
batch.main()
d1, d2, d3 = (d["id"] for d in read_json(DRAFTS, []))

print("2. drafts come with a ready-to-copy message")
review.send(dry_run=False)
photos, copies = tg.of("sendPhoto"), tg.of("sendMessage")
check(len(photos) == 3 and len(copies) == 3, "3 drafts, each followed by a copy message")
s = drafts()
for d_id in (d1, d2, d3):
    d = s[d_id]
    copy = next(p for _, p, _ in copies if p["reply_parameters"]["message_id"] == d["tg_message_id"])
    check(d.get("tg_copy_message_id") is not None, f"copy message id saved for {d_id}")
    check(copy["parse_mode"] == "HTML" and copy["link_preview_options"] == {"is_disabled": True}, "HTML, no link preview")
    short, long = blocks(copy["text"])
    check(len(short) <= 280 and d["link"] in short and short.endswith("#neuroscience"), f"short version fits X ({len(short)} chars)")
    check(d["title"] in long and d["link"] in long, "long version has the title and link")
    check("<a & b>" in short, "special characters come through as text")
check(compose.review_buttons({"id": "x", "status": "sent"}, ["channel", "linkedin"])["inline_keyboard"][0][0]["text"] == "📢 Channel",
      "Channel button comes first")

print("3. posting to the channel")
tg.updates = [tap(500, f"to:channel:{d1}", s[d1]["tg_message_id"])]
review.poll()
check(drafts()[d1]["targets"] == ["channel"] and drafts()[d1]["status"] == "approved", "tap queues d1 for the channel")
check(run_post() is None, "post run succeeds")
channel_posts = [p for _, p, _ in tg.of("sendPhoto") if p["chat_id"] == "@TestChannel"]
check(len(channel_posts) == 1, "one photo sent to the channel")
cap = channel_posts[0]["caption"]
check(f"<b>{html.escape(s[d1]['title'], quote=False)}</b>" in cap and s[d1]["link"] in cap and "#neuroscience" in cap,
      "channel post has the title, link and hashtag")
check("&lt;a &amp; b&gt;" in cap, "the hook is escaped for Telegram")
check("reply_markup" not in channel_posts[0] or channel_posts[0]["reply_markup"] is None, "no buttons in the channel")
d = drafts()[d1]
check(d["status"] == "posted" and d["posted"]["channel"]["url"] == f"https://t.me/TestChannel/{d['posted']['channel']['message_id']}",
      "posted, with the channel post's link saved")
review_edit = [p for _, p, _ in tg.of("editMessageCaption") if p["message_id"] == d["tg_message_id"]][-1]
check("Posted to Channel" in review_edit["caption"], "review message says it's in the channel")

print("4. replying to the copy message edits the hook")
new_hook = "A shorter, clearer hook for the second paper."
tg.updates.append({"update_id": 501, "message": {"message_id": 901, "chat": {"id": 111}, "text": new_hook,
                                                  "reply_to_message": {"message_id": s[d2]["tg_copy_message_id"]}}})
review.poll()
check(drafts()[d2]["hook"] == new_hook, "hook replaced from a reply to the copy message")
copy_edits = [p for _, p, _ in tg.of("editMessageText") if p["message_id"] == s[d2]["tg_copy_message_id"]]
check(copy_edits and new_hook in blocks(copy_edits[-1]["text"])[0], "copy message rewritten with the new hook")
check(any(p["message_id"] == s[d2]["tg_message_id"] for _, p, _ in tg.of("editMessageCaption")), "review message redrawn too")

print("5. channel not set")
cfg["posting"]["telegram_channel"] = ""
save_cfg()
tg.updates.append(tap(502, f"to:channel:{d3}", s[d3]["tg_message_id"]))
review.poll()
result = run_post("--now")
check(bool(result) and "telegram_channel" in result, f"clear message: {result}")
check(drafts()[d3]["status"] == "approved" and not drafts()[d3].get("attempts"), "draft stays queued, not counted as a failure")
cfg["posting"]["telegram_channel"] = "@TestChannel"
save_cfg()
check(run_post("--now") is None and drafts()[d3]["status"] == "posted", "posts once the channel is set")

print("6. links and dry run")
check(compose.channel_link("-1001234567890", 5) == "https://t.me/c/1234567890/5", "private channel link")
check(compose.channel_link("@Neuro", 7) == "https://t.me/Neuro/7", "public channel link")
ds = read_json(DRAFTS, [])
write_json(DRAFTS, ds + [dict(ds[0], id="fresh00000", status="ready", tg_message_id=None, tg_copy_message_id=None)])
out = io.StringIO()
with contextlib.redirect_stdout(out):
    review.send(dry_run=True)
check("Ready to copy" in out.getvalue(), "send --dry-run shows the copy message too")

print("\nALL OK" if not failures else f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
