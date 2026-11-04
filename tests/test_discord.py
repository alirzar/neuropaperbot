"""The public bot on Discord, with stand-ins for Discord's API, Telegram, Gemini and the paper sources.

Runs in a throwaway copy with its own database.
"""
import atexit
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="neuropaperbot-discord-"))  # a throwaway copy of the project
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
for name in ("scripts", "assets"):
    shutil.copytree(PROJECT / name, WORK / name, ignore=shutil.ignore_patterns("__pycache__"))
(WORK / "data").mkdir()
shutil.copy(PROJECT / "config.json", WORK / "config.json")
sys.path.insert(0, str(WORK / "scripts"))
ADMIN = 111  # on Telegram
DEE, GONE = 400000000000000001, 400000000000000002  # on Discord
os.environ.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID=str(ADMIN), GEMINI_API_KEY="fake", DISCORD_BOT_TOKEN="fake-discord")
os.environ.pop("LINKEDIN_ACCESS_TOKEN", None)

import bot as botmod  # noqa: E402
import catalog  # noqa: E402
import digest  # noqa: E402
import discord_api as dc  # noqa: E402
import explain  # noqa: E402
import telegram_api  # noqa: E402
from common import load_config  # noqa: E402
from store import Store  # noqa: E402

failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


# ---------- stand-ins ----------
def fake_vector(text):
    v = np.zeros(768, dtype=np.float32)
    for word in re.findall(r"[a-z]+", text.lower()):
        if len(word) >= 4:
            v[int(hashlib.md5(word[:6].encode()).hexdigest(), 16) % 768] += 1.0
    return (v / (np.linalg.norm(v) or 1)).tolist()


class FakeGemini:
    def __init__(self):
        self.models = self

    def embed_content(self, *, model, contents, config=None):
        return SimpleNamespace(embeddings=[SimpleNamespace(values=fake_vector(c.parts[0].text)) for c in contents])

    def generate_content(self, *, model, contents, config=None):
        if config and config.get("response_schema") is explain.Explanation:
            return SimpleNamespace(text=json.dumps({k: f"{k} part" for k, _ in explain.PARTS}))
        if config:
            ids = re.findall(r"^ID: (p\d+)$", contents, re.M)
            return SimpleNamespace(text=json.dumps([{"id": i, "score": 7, "reason": "fake", "neuro": True} for i in ids]))
        title = re.search(r"^TITLE: (.*)$", contents, re.M).group(1)
        return SimpleNamespace(text=f"Hook: {title[:60]}")


class FakeTelegram:
    def __init__(self):
        self.calls, self.next_id = [], 1000

    def call(self, method, files=None, **params):
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            params["_id"] = self.next_id
        self.calls.append((method, params))
        return {"message_id": self.next_id} if method in ("sendMessage", "sendPhoto") else True

    def texts(self, chat):
        return [p.get("text") or p.get("caption") or "" for m, p in self.calls if p.get("chat_id") == chat and m.startswith("send")]


class FakeDiscord:
    """Discord's API as the bot uses it: DM channels, messages, edits, interaction replies, commands."""

    def __init__(self):
        self.calls, self.next_id, self.commands, self.messages, self.posts = [], 5000, [], {}, []

    def call(self, method, path, payload=None, files=None):
        self.calls.append((method, path, payload, files))
        if path == "/oauth2/applications/@me":
            return {"id": "4242"}
        if path == "/users/@me/channels":
            return {"id": f"dm{payload['recipient_id']}"}
        if path == f"/channels/dm{GONE}/messages":
            raise dc.DiscordError("POST /channels/…/messages: 403 Cannot send messages to this user", dc.CANT_MESSAGE)
        if path.endswith("/commands"):
            if method == "PUT":
                self.commands = payload
            return self.commands
        if method == "POST" and path.endswith("/messages"):
            self.next_id += 1
            self.messages[self.next_id] = dict(payload)
            self.posts.append((path, self.next_id, files))
            return {"id": str(self.next_id)}
        if method == "PATCH":
            mid = int(path.rsplit("/", 1)[1])
            self.messages.setdefault(mid, {}).update(payload)
            return {"id": str(mid)}
        return None

    def sent(self, uid):
        """(message id, payload, files) of each message sent to this person, in order."""
        return [(mid, self.messages[mid], files) for path, mid, files in self.posts if path == f"/channels/dm{uid}/messages"]

    def last(self, uid):
        mid, payload, files = self.sent(uid)[-1]
        return mid, self.messages[mid], files

    def contents(self, uid):
        return [p["content"] for _, p, _ in self.sent(uid)]


def labels(payload):
    """The button labels and dropdown options of a message, row by row."""
    out = []
    for row in payload.get("components", []):
        items = []
        for c in row["components"]:
            if c["type"] == 2:
                items.append(c["label"])
            else:
                items.append({o["label"]: o.get("default", False) for o in c["options"]})
        out.append(items)
    return out


def paper(n, title, abstract, authors):
    return {"doi": f"10.1101/2026.11.01.{n:06d}", "source": "biorxiv", "venue": "bioRxiv preprint", "src": "biorxiv:neuroscience",
            "title": title, "authors": [a.split(",")[0] for a in authors], "author_keys": [botmod.name_key(a) for a in authors],
            "date": "2026-11-01", "link": f"https://doi.org/10.1101/2026.11.01.{n:06d}", "abstract": abstract, "license": "cc_by"}


PAPERS = [
    paper(1, "Spinal cord circuits for locomotion after injury", "Spinal cord interneurons rebuild locomotion circuits after injury.", ["Rossignol, S."]),
    paper(2, "Motor cortex population dynamics during reaching", "Motor cortex neural population dynamics generate reaching movements.", ["Ito, T."]),
    paper(3, "Hippocampal replay during sleep", "Hippocampus replay of place cells during sleep supports memory.", ["Smith, J."]),
    paper(4, "Cerebellar learning of *timing* in eyeblink conditioning", "Cerebellum learns motor timing with Purkinje cells.", ["Medina, J."]),
    paper(5, "Dopamine and reward_prediction in striatum", "Dopamine neurons signal reward prediction errors.", ["Garcia, M."]),
    paper(6, "Basal ganglia control of movement vigor", "Basal ganglia circuits set movement vigor in mice.", ["Yttri, E."]),
    paper(7, "Visual cortex orientation tuning", "Visual cortex neurons and orientation tuning.", ["Chen, L."]),
    paper(8, "Locomotion speed coding in the brainstem", "Brainstem neurons encode locomotion speed and gait.", ["Kiehn, O."]),
]

gemini, tg, dsc = FakeGemini(), FakeTelegram(), FakeDiscord()
telegram_api.call = tg.call
telegram_api.answer = lambda callback_id, text=None: None
dc.call = dsc.call
digest.fetch_sources = lambda sources, start, end, pubmed=None: ([dict(p, broad=False) for p in PAPERS], [])
digest.search_pubmed = lambda *a, **k: []
catalog.find_journals = lambda text: [{"abbr": f"J {i}", "title": f"Journal {i} of {text}", "per_month": 10.0} for i in range(1, 6)]
catalog.journal_details = lambda abbr: {"per_month": 10.0, "brain_per_month": 9.0, "broad": False}

cfg = load_config()
store = Store(WORK / "data" / "bot.db")
bot = botmod.Bot(cfg, store, gemini, {"telegram": telegram_api.App(), "discord": dc.App()})


def dm(uid, text, name="Dee", username="dee_lab"):
    bot.handle({"kind": "text", "uid": uid, "name": name, "username": username, "text": text}, "discord")


def command(uid, text, token="tok-cmd"):
    bot.handle({"kind": "command", "uid": uid, "name": "Dee", "username": "dee_lab", "text": text, "token": token}, "discord")


def press(uid, custom_id, message_id, token="tok-tap"):
    """A button press, as discord_api.connect passes it on."""
    for event in dc.component_events({"custom_id": custom_id, "component_type": 2}, [], message_id):
        bot.handle({**event, "uid": uid, "name": "Dee", "username": "dee_lab", "token": token}, "discord")


def choose(uid, message_id, custom_id, values, token="tok-menu"):
    """A dropdown choice: the taps it stands for, computed from the message as last drawn."""
    rows = dsc.messages[message_id].get("components", [])
    for event in dc.component_events({"custom_id": custom_id, "component_type": 3, "values": values}, rows, message_id):
        bot.handle({**event, "uid": uid, "name": "Dee", "username": "dee_lab", "token": token}, "discord")


def tg_tap(uid, data, message_id=5):
    bot.handle({"update_id": 1, "callback_query": {"id": "cq", "from": {"id": uid}, "data": data, "message": {"message_id": message_id}}})


print("1. Discord's format")
md = dc.markdown('<b>Bold &amp; title</b> by <i>Smith</i>: a*b_c ~x~ `y` | https://doi.org/10.1101/a_b and <a href="https://x.org/a_b">T*t [1]</a>')
check(md == r"**Bold & title** by _Smith_: a\*b\_c \~x\~ \`y\` \| https://doi.org/10.1101/a_b and [T\*t \[1\]](https://x.org/a_b)", f"HTML to Markdown, escaped, links intact: {md}")
check(dc.markdown("<pre>a*b\n_c</pre>") == "```\na*b\n_c\n```", "code blocks are left as they are")
check(dc.markdown("<code>/tz Europe/Madrid</code> &lt;with&gt;") == "`/tz Europe/Madrid` <with>", "inline code, and entities decoded")
check(dc.markdown("- item\n# not a heading\n> nor a quote") == "\\- item\n\\# not a heading\n\\> nor a quote", "line starts escaped")
long = "\n\n".join("x" * 900 for _ in range(5))
parts = dc.pieces(long)
check(len(parts) == 3 and all(len(p) <= 2000 for p in parts) and "\n\n".join(parts) == long, "long text split at paragraphs into 3 messages")

print("2. buttons and dropdowns")
topics = bot.topic_buttons(["memory"])
rows = dc.components(topics)
menu = rows[0]["components"][0]
check(menu["type"] == 3 and len(menu["options"]) == 12 and menu["max_values"] == 12 and menu["min_values"] == 0, "12 topics: one dropdown, pick any")
check([o["label"] for o in menu["options"] if o["default"]] == ["Learning & memory"], "ticked ones are preselected, without the ✅")
check(labels({"components": rows})[1] == ["Done"] and rows[1]["components"][0]["style"] == 1, "Done stays a button, highlighted")
store.add_user(999, "X", None, "active", platform="discord")
sources = dc.components(bot.source_buttons(store.user(999)))
menus = [r["components"][0] for r in sources if r["components"][0]["type"] == 3]
check(len(menus) == 2 and [len(m["options"]) for m in menus] == [14, 14], f"28 sources: two dropdowns of 14 ({[len(m['options']) for m in menus]})")
check(menus[0]["placeholder"] == "bioRxiv · Neuroscience … Biological Psychiatry", f"named by first and last: {menus[0]['placeholder']}")
check(labels({"components": sources})[-1] == ["➕ Add a journal", "Done"], "then the add and done buttons")
zones = dc.components([[botmod.button(label, f"tz:{i}") for i, (label, _) in enumerate(botmod.ZONES)]])[0]["components"][0]
check(zones["max_values"] == 1 and zones["min_values"] == 1 and not any(o["default"] for o in zones["options"]), "time zones: pick one")
six = [[botmod.button(f"{i}. J {i}", f"src_pick:{i}")] for i in range(1, 6)] + [[botmod.button("Cancel", "src_cancel")]]
check(labels({"components": dc.components(six)}) == [["1. J 1", "2. J 2", "3. J 3", "4. J 4", "5. J 5"], ["Cancel"]], "six rows packed into two")
check(dc.menu_taps("pick:src:0", ["b", "c"], [{"components": [{"custom_id": "pick:src:0", "options": [
    {"value": "a", "default": True}, {"value": "b", "default": True}, {"value": "c", "default": False}]}]}]) == ["a", "c"], "a dropdown change is the taps that differ")
store.run("DELETE FROM users WHERE id=999")

print("3. slash commands")
check(dc.register_commands(botmod.DISCORD_COMMANDS) and len(dsc.commands) == len(botmod.DISCORD_COMMANDS), "registered")
find = next(c for c in dsc.commands if c["name"] == "find")
check(find["contexts"] == [1] and find["options"][0]["name"] == "topic" and find["options"][0]["required"], "for direct messages, with their options")
puts = sum(1 for m, p, *_ in dsc.calls if m == "PUT")
check(not dc.register_commands(botmod.DISCORD_COMMANDS) and sum(1 for m, p, *_ in dsc.calls if m == "PUT") == puts, "unchanged: not written again")
check("client_id=4242" in dc.invite_link() and "permissions=0" in dc.invite_link(), "invite link needs no permissions")

print("4. papers for everyone")
message_admin = lambda text: bot.handle({"update_id": 1, "message": {"message_id": 1, "chat": {"id": ADMIN, "type": "private"},
                                                                      "from": {"id": ADMIN, "first_name": "Ali"}, "text": text}})
message_admin("/start")
digest.daily_update(store, cfg, gemini)
check(len(store.recent(7)) == 8, "8 papers in")

print("5. joining from Discord")
dm(DEE, "hi")
check(store.user(DEE)["platform"] == "discord" and store.user(DEE)["status"] == "pending", "Dee waits, as a Discord person")
check(("POST", "/users/@me/channels", {"recipient_id": str(DEE)}, None) in dsc.calls, "a DM channel is opened")
check("asked the admin" in dsc.contents(DEE)[-1], "and Dee is told, on Discord")
request = [p for m, p in tg.calls if m == "sendMessage" and p["chat_id"] == ADMIN][-1]
check("Dee (@dee_lab) on Discord wants to use the bot" in request["text"], f"the admin is asked on Telegram: {request['text']!r}")
tg_tap(ADMIN, f"ok:{DEE}", message_id=request["_id"])
check(store.user(DEE)["status"] == "active" and "You're in" in " ".join(dsc.contents(DEE)), "approved, and told on Discord")
edit = [p for m, p in tg.calls if m == "editMessageText"][-1]
check("Approved: Dee (@dee_lab) on Discord" in edit["text"], "the admin's request shows the decision")

print("6. setup with dropdowns")
picker_id, picker, _ = dsc.last(DEE)
check(picker["components"][0]["components"][0]["custom_id"] == "pick:topic:0", "topics come as a dropdown")
edits = len([c for c in dsc.calls if c[0] == "PATCH"])
choose(DEE, picker_id, "pick:topic:0", ["topic:motor", "topic:systems"])
check(set(store.user(DEE)["topics"]) == {"motor", "systems"}, "two topics from one choice")
patches = [c for c in dsc.calls if c[0] == "PATCH"]
check(len(patches) == edits + 1 and patches[-1][1].endswith(f"/{picker_id}"), "one redraw, not one per topic")
check({k for k, v in labels(dsc.messages[picker_id])[0][0].items() if v} == {"Motor control", "Circuits & systems"}, "redrawn with them ticked")
choose(DEE, picker_id, "pick:topic:0", ["topic:motor"])
check(store.user(DEE)["topics"] == ["motor"], "unticking one in the dropdown removes it")
press(DEE, "topics_done", picker_id)
check(dsc.messages[picker_id]["components"] == [] and "in your own words" in dsc.contents(DEE)[-1], "Done: on to keywords")
dm(DEE, "spinal cord injury, locomotion")
lst_id, lst, _ = dsc.last(DEE)
check("**Where your papers come from**" in lst["content"] and len(lst["components"]) == 3, "sources: two dropdowns and a row of buttons")
choose(DEE, lst_id, "pick:src:0", ["src:" + catalog.make("pubmed", "Nat Neurosci", "")["id"]])
check(store.user(DEE)["sources"] == ["pubmed:nat neurosci"], "Nature Neuroscience picked")
press(DEE, "src_add", lst_id)
dm(DEE, "spinal")
found_id, found, _ = dsc.last(DEE)
check(labels(found) == [["1. J 1", "2. J 2", "3. J 3", "4. J 4", "5. J 5"], ["Cancel"]], "journal search: five choices and Cancel, in two rows")
press(DEE, "src_cancel", found_id)
lst_id, lst, _ = dsc.last(DEE)
press(DEE, "src_done", lst_id)
tz_id, tz, _ = dsc.last(DEE)
check(tz["components"][0]["components"][0]["custom_id"] == "pick:tz:0", "time zones as a dropdown")
choose(DEE, tz_id, "pick:tz:0", ["tz:5"])
check(store.user(DEE)["tz"] == "Europe/London", "UK picked")
hour_id, hour, _ = dsc.last(DEE)
check(labels(hour) == [["6:00", "7:00", "8:00"], ["9:00", "10:00", "12:00"], ["14:00", "17:00", "20:00"]], "hours stay buttons")
press(DEE, "hour:9", hour_id)
summary_id, summary, _ = dsc.last(DEE)
check("📋 **Your digest**" in summary["content"] and "**Sources:** Nature Neuroscience (these come first)" in summary["content"],
      f"the summary, in Markdown: {summary['content'][:60]!r}")
check(labels(summary)[0] == ["✅ Start my digest"] and summary["components"][0]["components"][0]["style"] == 1, "Start, highlighted")
press(DEE, "go", summary_id)
dee = store.user(DEE)
check(dee["step"] is None and dee["hour"] == 9, "setup done")

print("7. the digest on Discord")
cards = [(mid, p, f) for mid, p, f in dsc.sent(DEE) if f]
check(len(cards) == 3, "3 cards")
mid, first, files = cards[0]
check(files["files[0]"][0] == "card.png" and files["files[0]"][1][:4] == b"\x89PNG", "each with its card image")
check(first["flags"] & dc.NO_EMBEDS and first["allowed_mentions"] == {"parse": []}, "no link previews, no pings")
check("Hook:" in first["content"] and "**" in first["content"], f"caption in Markdown: {first['content'][:80]!r}")
check(labels(first) == [["👍", "👎", "💾 Save"], ["📄 Summary", "📋 Copy"]], "the same buttons")
footer_id, footer, _ = [m for m in dsc.sent(DEE) if m[1]["content"] == "Want more?"][-1]
check(labels(footer) == [["➡️ 3 more"]] and footer["components"][0]["components"][0]["style"] == 1, "and ➡️ 3 more")
check("/help lists everything" in dsc.contents(DEE)[-1], "then the tip, as on Telegram")
pid = re.search(r"like:(\w+)", json.dumps(first["components"])).group(1)
press(DEE, f"like:{pid}", mid)
check(store.sent_row(DEE, pid)["feedback"] == 1 and labels(dsc.messages[mid])[0][0] == "👍 ✓", "👍 recorded and shown")
notes = [c for c in dsc.calls if c[1].startswith("/webhooks/4242/") and c[0] == "POST"]
check(not notes, "no note for that: the button shows it")
press(DEE, "like:nothing123", mid, token="tok-gone")
note = [c for c in dsc.calls if c[0] == "POST" and c[1] == "/webhooks/4242/tok-gone"]
check(note and note[0][2] == {"content": "That paper is gone", "flags": dc.EPHEMERAL}, "an important note is shown, to Dee only")
press(DEE, "more", footer_id)
check(dsc.messages[footer_id]["content"] == "➡️ 3 more:" and dsc.messages[footer_id]["components"] == [], "➡️ turns into a heading")
check(len([1 for _, _, f in dsc.sent(DEE) if f]) == 6, "and 3 more cards come")
press(DEE, f"copy:{pid}", mid)
check("```" in dsc.contents(DEE)[-1], "copy text in code blocks")

print("8. commands")
command(DEE, "/find locomotion", token="tok-find")
check(any("closest matches" in c for c in dsc.contents(DEE)), "/find answers in the DM")
check(("DELETE", "/webhooks/4242/tok-find/messages/@original", None, None) in dsc.calls, "and the thinking… line is removed")
command(DEE, "/saved", token="tok-saved")
check("Nothing saved yet" in dsc.contents(DEE)[-1], "/saved")
dm(DEE, f"what about {PAPERS[0]['doi']}")
check("**The question:**" in dsc.contents(DEE)[-1], "a pasted DOI gets a summary, in Markdown")
message_admin("/users")
users = [p for m, p in tg.calls if m == "sendMessage" and p["chat_id"] == ADMIN][-1]["text"]
check("Dee (@dee_lab) on Discord" in users, "/users shows where people are")

print("9. the schedule, across apps")
store.add_user(GONE, "Gone", "gone", "active", platform="discord")
store.update_user(GONE, step=None, tz="UTC", hour=6, topics=["motor"])
store.update_user(DEE, last_digest="2000-01-01")
bot.updating = SimpleNamespace(is_alive=lambda: True)
before = len(dsc.sent(DEE))
bot.tick(now=datetime(2026, 11, 4, 9, 30, tzinfo=timezone.utc))
check(len(dsc.sent(DEE)) > before, "Dee's 9:00 digest goes out on Discord")
check(store.user(GONE)["last_digest"] == "2026-11-04", "someone who left the server: skipped without stopping the others")

print("10. an alert for someone who can't be reached")
store.follow(GONE, "rossignol s", "Serge Rossignol")
PAPERS.append(paper(9, "Another locomotion study", "Spinal locomotion circuits again.", ["Rossignol, S."]))
store.put("last_update", "2000-01-01")
result = digest.daily_update(store, cfg, gemini, alert=bot.send_alert)
check(result["new"] == 1 and store.get("last_update") != "2000-01-01", "the daily update finishes anyway")

print("11. leaving")
command(DEE, "/stop", token="tok-stop")
check("deleted everything" in dsc.contents(DEE)[-1] and store.user(DEE) is None, "/stop: told on Discord, then deleted")

print("\nALL OK" if not failures else f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
