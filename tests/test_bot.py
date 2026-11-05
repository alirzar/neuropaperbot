"""The public bot end to end, with stand-ins for Telegram, Gemini, LinkedIn, PubMed and the paper sources.

Runs in a throwaway copy with its own database.
"""
import atexit
import hashlib
import json
import os
import re
import shutil
import threading
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="neuropaperbot-bot-"))  # a throwaway copy of the project
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
for name in ("scripts", "assets"):
    shutil.copytree(PROJECT / name, WORK / name, ignore=shutil.ignore_patterns("__pycache__"))
(WORK / "data").mkdir()
shutil.copy(PROJECT / "config.json", WORK / "config.json")
sys.path.insert(0, str(WORK / "scripts"))
ADMIN, ANA, BEN, CARL = 111, 222, 333, 444
os.environ.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID=str(ADMIN), GEMINI_API_KEY="fake", LINKEDIN_ACCESS_TOKEN="fake-li")

import bot as botmod  # noqa: E402
import catalog  # noqa: E402
import digest  # noqa: E402
import explain  # noqa: E402
import linkedin_api  # noqa: E402
import cite  # noqa: E402
import telegram_api  # noqa: E402
from common import load_config, short_id  # noqa: E402
from store import Store  # noqa: E402

failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


# ---------- stand-ins ----------
def fake_vector(text):
    """Bag of words hashed into 768 numbers: texts that share words come out similar."""
    v = np.zeros(768, dtype=np.float32)
    for word in re.findall(r"[a-z]+", text.lower()):
        if len(word) >= 4:
            v[int(hashlib.md5(word[:6].encode()).hexdigest(), 16) % 768] += 1.0
    return (v / (np.linalg.norm(v) or 1)).tolist()


class FakeGemini:
    def __init__(self):
        self.models = self
        self.calls = {"embed": 0, "embedded": 0, "hook": 0, "hooks": 0, "score": 0, "explain": 0}
        self.lock = threading.Lock()  # hooks can be written four at a time

    def embed_content(self, *, model, contents, config=None):
        self.calls["embed"] += 1
        self.calls["embedded"] += len(contents)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=fake_vector(c.parts[0].text)) for c in contents])

    def generate_content(self, *, model, contents, config=None):
        if config and config.get("response_schema") is explain.Explanation:
            self.calls["explain"] += 1
            return SimpleNamespace(text=json.dumps({k: f"{k} part <b>&" for k, _ in explain.PARTS}))
        if config and "Hook" in str(config.get("response_schema")):
            self.calls["hooks"] += 1
            items = re.findall(r"^ID: (p\d+)\nSOURCE: .*\nTITLE: (.*)$", contents, re.M)
            return SimpleNamespace(text=json.dumps([{"id": i, "hook": f"Hook: {t[:60]}"} for i, t in items]))
        if config:
            self.calls["score"] += 1
            self.last_prompt = contents
            items = re.findall(r"^ID: (p\d+)\nSOURCE: .*\nTITLE: (.*)$", contents, re.M)
            return SimpleNamespace(text=json.dumps(
                [{"id": i, "score": 7, "reason": "fake", "neuro": "machine learning" not in t.lower()} for i, t in items]))
        with self.lock:
            self.calls["hook"] += 1
        title = re.search(r"^TITLE: (.*)$", contents, re.M).group(1)
        return SimpleNamespace(text=f"Hook: {title[:60]}")


class FakeTelegram:
    def __init__(self):
        self.calls, self.next_id = [], 1000

    def call(self, method, files=None, **params):
        if files:
            params["_files"] = files
        if method in ("sendMessage", "sendPhoto", "sendDocument"):
            self.next_id += 1
            params["_id"] = self.next_id
            self.calls.append((method, params))
            return {"message_id": self.next_id}
        self.calls.append((method, params))
        return True

    def to(self, chat, method=None):
        return [p for m, p in self.calls if p.get("chat_id") == chat and (method is None or m == method)]

    def texts(self, chat):
        return [p.get("text") or p.get("caption") or "" for m, p in self.calls if p.get("chat_id") == chat and m in ("sendMessage", "sendPhoto")]

    def last(self, chat):
        return self.texts(chat)[-1]

    def edits(self, method="editMessageReplyMarkup"):
        return [p for m, p in self.calls if m == method]


def paper(n, title, abstract, authors, source="biorxiv", venue="bioRxiv preprint", src="biorxiv:neuroscience"):
    return {
        "doi": f"10.1101/2026.11.01.{n:06d}", "source": source, "venue": venue, "src": src, "title": title,
        "authors": [a.split(",")[0] for a in authors], "author_keys": [botmod.name_key(a) for a in authors],
        "date": "2026-11-01", "link": f"https://doi.org/10.1101/2026.11.01.{n:06d}", "abstract": abstract, "license": "cc_by",
    }


PAPERS = [
    paper(1, "Hippocampal replay during sleep consolidates spatial memory",
          "Hippocampus replay of place cell sequences during sleep supports memory consolidation in rats.", ["Smith, J.", "Lee, K."]),
    paper(2, "Dopamine prediction errors drive reinforcement learning in striatum",
          "Dopamine neurons signal reward prediction errors that drive reinforcement learning and motivation.", ["Garcia, M."]),
    paper(3, "Orientation tuning in visual cortex emerges from recurrent circuits",
          "Visual cortex neurons acquire orientation tuning through recurrent excitatory circuits.", ["Chen, L."]),
    paper(4, "Amyloid pathology disrupts synapses in Alzheimer's disease models",
          "Alzheimer's disease amyloid pathology causes synapse loss and neurodegeneration in mice.", ["Patel, R."]),
    paper(5, "A new optogenetic tool for silencing neurons",
          "We present an optogenetic method for silencing neurons with light, a new recording and imaging technique.", ["Novak, P."]),
    paper(6, "Sleep spindles couple with hippocampal ripples in human memory consolidation",
          "Human EEG shows sleep spindles coupled to hippocampal ripples predict memory consolidation overnight.", ["Okafor, A."]),
    paper(7, "Motor cortex population dynamics during reaching",
          "Motor cortex neural population dynamics generate reaching movements in monkeys.", ["Ito, T."]),
    paper(8, "Gene expression programs in the developing cortex <with> neurogenesis & more",
          "Brain development: gene expression programs control neurogenesis in the developing cortex.", ["Weber, S."]),
    # Only fetched once someone turns on Nature (a general journal) or adds J Neurosci Methods:
    paper(20, "Dopamine release encodes reward and motivation in striatum",
          "Dopamine neurons release dopamine in striatum to encode reward prediction and motivation.", ["Kim, H."],
          source="pubmed", venue="Nature", src="pubmed:nature"),
    paper(21, "A machine learning model finds new battery materials",
          "Neural networks predict lithium battery cathode materials from crystal structures.", ["Volta, A."],
          source="pubmed", venue="Nature", src="pubmed:nature"),
    paper(22, "Spike sorting for dense probes with a new method",
          "A spike sorting method for dense silicon probes recording neurons.", ["Buzsaki, G."],
          source="pubmed", venue="J Neurosci Methods", src="pubmed:j neurosci methods"),
]
SMITH_NEW = paper(9, "A second replay study by Smith", "Hippocampal replay and memory consolidation again.", ["Smith, J. A.", "Brown, T."],
                  source="pubmed", venue="Cereb Cortex", src="pubmed:cereb cortex")
EXTERNAL = paper(10, "An external paper found by DOI", "Hippocampal theta oscillations organize memory.", ["Rossi, F."])

gemini, tg = FakeGemini(), FakeTelegram()
telegram_api.call = tg.call
fetched = []  # the source keys each fetch asked for


def fake_fetch_sources(sources, start, end, pubmed=None):
    keys = {s["key"] for s in sources}
    general = {s["key"] for s in sources if s["broad"]}
    fetched.append(keys)
    found = [dict(p, broad=p["src"] in general) for p in PAPERS if p["src"] in keys]
    return found, []


digest.fetch_sources = fake_fetch_sources
pubmed_terms = []
digest.search_pubmed = lambda term, *a, **k: (pubmed_terms.append(term) or ([dict(SMITH_NEW)] if '"smith j"' in term else []))
looked_up = []
botmod.lookup = lambda doi, journals: (looked_up.append(doi) or (dict(EXTERNAL) if doi == EXTERNAL["doi"] else None))
JOURNALS = {
    "neuroscience methods": [
        {"abbr": "J Neurosci Methods", "title": "Journal of neuroscience methods", "per_month": 18.2},
        {"abbr": "Neurosci Methods Rep", "title": "Neuroscience methods reports", "per_month": 0.4},
    ],
    "plos one": [{"abbr": "PLoS One", "title": "PloS one", "per_month": 1352.0}],
    "huge journal": [{"abbr": "Huge J", "title": "Huge journal", "per_month": 5000.0}],
}
DETAILS = {
    "J Neurosci Methods": {"per_month": 18.2, "brain_per_month": 17.0, "broad": False},
    "Neurosci Methods Rep": {"per_month": 0.4, "brain_per_month": 0.4, "broad": False},
    "PLoS One": {"per_month": 1352.0, "brain_per_month": 279.0, "broad": True},
    "Huge J": {"per_month": 5000.0, "brain_per_month": 4900.0, "broad": False},
}
journal_searches = []
catalog.find_journals = lambda text: (journal_searches.append(text) or JOURNALS.get(text.lower(), []))
catalog.journal_details = lambda abbr: DETAILS.get(abbr)

cfg = load_config()
store = Store(WORK / "data" / "bot.db")
bot = botmod.Bot(cfg, store, gemini)
BASE_KEYS = catalog.base_keys(cfg)


def message(uid, text, first="Test", username=None, chat_type="private"):
    sender = {"id": uid, "first_name": first, **({"username": username} if username else {})}
    bot.handle({"update_id": 1, "message": {"message_id": 1, "chat": {"id": uid, "type": chat_type}, "from": sender, "text": text}})


answers = []
telegram_api.answer = lambda callback_id, text=None: answers.append(text)


def tap(uid, data, message_id=None):
    bot.handle({"update_id": 1, "callback_query": {"id": "cq", "from": {"id": uid}, "data": data,
                                                    "message": {"message_id": message_id or 5, "chat": {"id": uid}}}})


def buttons(params):
    return [[b["text"] for b in row] for row in (params.get("reply_markup") or {}).get("inline_keyboard", [])]


def flat(params):
    return [b for row in buttons(params) for b in row]


def sid(key):
    return short_id(key)


def setup(uid, topics, keywords, tz_tap=None, tz_cmd=None, hour=8, sources=()):
    for t in topics:
        tap(uid, f"topic:{t}")
    tap(uid, "topics_done")
    message(uid, keywords) if keywords else tap(uid, "keep_keywords")
    for key in sources:
        tap(uid, f"src:{sid(key)}")
    tap(uid, "src_done")
    tap(uid, f"tz:{tz_tap}") if tz_cmd is None else message(uid, f"/tz {tz_cmd}")
    tap(uid, f"hour:{hour}")
    tap(uid, "days_done")
    tap(uid, "count:3")
    tap(uid, "go")


def run_pending_update():
    bot.updating = None
    bot.maybe_update(datetime.now(timezone.utc).replace(hour=12))
    if bot.updating:
        bot.updating.join(10)


print("1. the admin sets up before any papers exist")
message(ADMIN, "/start", first="Ali")
user = store.user(ADMIN)
check(user["status"] == "active" and user["is_admin"] and user["step"] == "topics", "admin is active and starts setup")
picker = tg.to(ADMIN, "sendMessage")[-1]
check(len(buttons(picker)) == 7 and buttons(picker)[-1] == ["Done"], "topic picker: 12 topics in pairs, then Done")
tap(ADMIN, "topic:memory")
check(any("✅ Learning & memory" in b for b in flat(tg.edits()[-1])), "tapped topic gets a ✅")
tap(ADMIN, "topics_done")
message(ADMIN, "hippocampal replay, sleep")
check(store.user(ADMIN)["step"] == "sources", "after keywords: the sources")
lst = tg.to(ADMIN, "sendMessage")[-1]
names = flat(lst)
check("Where your papers come from" in lst["text"], "sources list explained")
check(not any(b.startswith("✅") for b in names), f"nothing is ticked to start with: {[b for b in names if b.startswith('✅')]}")
check({"bioRxiv · Neuroscience", "Neuron", "Nature", "eLife", "medRxiv · Neurology"} <= set(names), "every source offered")
check(buttons(lst)[-1] == ["➕ Add a journal", "Done"], "add and done buttons last")
check(all(len(row) == 1 or all(len(b.replace("✅ ", "")) <= 19 for b in row) for row in buttons(lst)), "long names get a row of their own")
tap(ADMIN, "src_done")
check("No sources picked, so your papers come from everything I follow" in tg.texts(ADMIN)[-2], "picking none is fine, and explained")
tap(ADMIN, "tz:0")
tap(ADMIN, "hour:8")
days = tg.to(ADMIN, "sendMessage")[-1]
check(store.user(ADMIN)["step"] == "days" and "Which days" in days["text"], "after the hour: which days")
check(buttons(days) == [["✅ Mon", "✅ Tue", "✅ Wed", "✅ Thu"], ["✅ Fri", "✅ Sat", "✅ Sun"], ["Every day", "Weekdays"], ["Done"]],
      "every day ticked to start with, and two shortcuts")
tap(ADMIN, "days:weekdays", message_id=days["_id"])
check(store.user(ADMIN)["days"] == [0, 1, 2, 3, 4] and buttons(tg.edits()[-1])[1] == ["✅ Fri", "Sat", "Sun"], "Weekdays: Monday to Friday")
for d in range(5):
    tap(ADMIN, f"day:{d}", message_id=days["_id"])
check(store.user(ADMIN)["days"] == [], "untick them all")
tap(ADMIN, "days_done", message_id=days["_id"])
check(answers[-1] == "Pick at least one day" and store.user(ADMIN)["step"] == "days", "no days: asked to pick one")
tap(ADMIN, "days:all", message_id=days["_id"])
tap(ADMIN, "days_done", message_id=days["_id"])
count = tg.to(ADMIN, "sendMessage")[-1]
check(store.user(ADMIN)["step"] == "count" and "How many papers" in count["text"], "then: how many papers each time")
check(buttons(count) == [["1", "2", "✅ 3", "4", "5"], ["6", "7", "8", "9", "10"]], "1 to 10, with the default ticked")
tap(ADMIN, "count:3", message_id=count["_id"])
check(store.user(ADMIN)["step"] == "confirm", "then a summary to confirm")
summary = tg.to(ADMIN, "sendMessage")[-1]
for line in ("<b>Areas:</b> Learning &amp; memory", "<b>In your words:</b> hippocampal replay, sleep",
             "<b>Sources:</b> none picked, so papers come from everything I follow",
             "<b>When:</b> every day at 8:00, Eastern (Toronto, New York) time", "<b>Papers each time:</b> 3"):
    check(line in summary["text"], f"summary shows {line!r}")
check(buttons(summary) == [["✅ Start my digest"], ["Areas", "Keywords", "Sources"], ["Time", "Days", "Papers each time"]], "start, or change any part")
store.update_user(ADMIN, last_digest=None)
bot.updating = SimpleNamespace(is_alive=lambda: True)
store.put("last_update", "2026-11-04")
bot.tick(now=datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc))
check(store.user(ADMIN)["last_digest"] is None, "no digest before Start")
store.run("DELETE FROM meta WHERE key='last_update'")
bot.updating = None
tap(ADMIN, "change:keywords", message_id=summary["_id"])
check(store.user(ADMIN)["step"] == "keywords_review" and "Now: <i>hippocampal replay, sleep</i>" in tg.last(ADMIN), "change keywords: asks again")
check(tg.edits()[-1]["message_id"] == summary["_id"] and tg.edits()[-1]["reply_markup"]["inline_keyboard"] == [], "the old summary's buttons go")
message(ADMIN, "hippocampal replay, sleep, place cells")
check(store.user(ADMIN)["step"] == "confirm" and "place cells" in tg.last(ADMIN) and "Your digest" in tg.last(ADMIN), "and comes back to the summary")
tap(ADMIN, "change:time")
tap(ADMIN, "tz:0")
tap(ADMIN, "hour:7")
check(store.user(ADMIN)["hour"] == 7 and "every day at 7:00" in tg.last(ADMIN), "change the time: back to the summary")
tap(ADMIN, "change:count")
tap(ADMIN, "count:5")
check("<b>Papers each time:</b> 5" in tg.last(ADMIN), "change papers per digest: back to the summary")
tap(ADMIN, "change:days")
tap(ADMIN, "day:5")
tap(ADMIN, "day:6")
tap(ADMIN, "days_done")
check("<b>When:</b> on weekdays at 7:00" in tg.last(ADMIN), "change the days: back to the summary")
tap(ADMIN, "day:2")
tap(ADMIN, "day:4")
check("on Mon, Tue, Thu" in bot.summary(store.user(ADMIN)), "other days are listed by name")
store.update_user(ADMIN, days=None)
tap(ADMIN, "change:sources")
tap(ADMIN, "src_add")
check(store.user(ADMIN)["step"] == "journal_review", "adding a journal from the summary")
tap(ADMIN, "src_cancel")
tap(ADMIN, "src_done")
check(store.user(ADMIN)["step"] == "confirm" and "Your digest" in tg.last(ADMIN), "sources: back to the summary")
message(ADMIN, "/start")
check("Does this look right?" in tg.last(ADMIN), "/start during the summary shows it again")
store.update_user(ADMIN, hour=8, per_digest=None)
tap(ADMIN, "go", message_id=4321)
user = store.user(ADMIN)
check(user["step"] is None and user["tz"] == "America/Toronto" and user["hour"] == 8, "Start: setup finished, Toronto, 8:00")
check(tap(ADMIN, "go") is None and answers[-1] == "Already started", "a second Start does nothing")
check(bot.my_sources(user) == [], "no sources ticked")
check(user["profile"] is not None and user["profile"].shape == (4, 768), "interests: 1 topic + 3 keyword phrases")
check("still collecting" in " ".join(tg.texts(ADMIN)), "no papers yet: says the first digest is coming")

print("2. the daily update")
result = digest.daily_update(store, cfg, gemini, alert=bot.send_alert)
check(fetched[-1] == set(BASE_KEYS), f"nobody ticked anything: fetches the base sources: {sorted(fetched[-1])}")
check(result["new"] == 8 and not result["errors"], f"8 papers added ({result})")
check(all((p["hook"] or "").startswith("Hook: ") for p in store.recent(7)), "every new paper has its teaser already")
check(gemini.calls["hooks"] == 1 and gemini.calls["hook"] == 0, "all 8 written in one request, not one by one")
check(all(p["vector"] is not None and p["quality"] == 7 for p in store.recent(7)), "each has a fingerprint and a score")
check('"neuro"' in gemini.last_prompt, "Gemini is asked whether each paper is neuroscience")
check(store.get("last_update") is not None, "update recorded")
result = digest.daily_update(store, cfg, gemini, alert=bot.send_alert)
check(result["new"] == 0, "running it again adds nothing")

print("3. the admin's digest, and more")
message(ADMIN, "/digest")
cards = tg.to(ADMIN, "sendPhoto")
check(len(cards) == 3, "3 cards")
captions = [c["caption"] for c in cards]
check("replay during sleep" in captions[0] or "Sleep spindles" in captions[0], f"best match first: {captions[0][:60]!r}")
check(all("Hook:" in c for c in captions), "each card has its hook")
check(all("💡 Matches <i>" in c for c in captions), f"each card says which interest it matched: {captions[0][-60:]!r}")
check(buttons(cards[0])[1:] == [["📃 Abstract", "📄 Summary"], ["📋 Copy", "🔗 Similar", "💼 LinkedIn"]], "admin gets the LinkedIn button")
footer = tg.to(ADMIN, "sendMessage")[-1]
check(footer["text"] == "Want more?" and buttons(footer) == [["➡️ 3 more"]], "a ➡️ 3 more button after the cards")
tap(ADMIN, "more", message_id=footer["_id"])
edit = tg.edits("editMessageText")[-1]
check(edit["message_id"] == footer["_id"] and edit["text"] == "➡️ 3 more:" and edit["reply_markup"]["inline_keyboard"] == [],
      "tapping it turns the button into a heading")
check(len(tg.to(ADMIN, "sendPhoto")) == 6 and len({c["caption"] for c in tg.to(ADMIN, "sendPhoto")}) == 6, "3 different papers")
footer = tg.to(ADMIN, "sendMessage")[-1]
check(buttons(footer) == [["➡️ 3 more"]], "and another ➡️ button")
tap(ADMIN, "more", message_id=footer["_id"])
check(len(tg.to(ADMIN, "sendPhoto")) == 8 and tg.to(ADMIN)[-1].get("caption"), "the last 2, with no button after them")
tap(ADMIN, "more", message_id=footer["_id"])
check("every new paper from the past week" in tg.last(ADMIN), "nothing left: says so")

message(ADMIN, "/settings")
settings = tg.to(ADMIN, "sendMessage")[-1]
check("Tap one to change it." in settings["text"] and buttons(settings) == [["Areas", "Keywords", "Sources"], ["Time", "Days", "Papers each time"]],
      "/settings: the same summary, without Start")
tap(ADMIN, "change:count", message_id=settings["_id"])
tap(ADMIN, "count:3")
check(tg.last(ADMIN) == "Done: 3 papers per digest.", "a change outside setup just confirms it")

print("4. joining needs approval")
message(ANA, "/start", first="Ana", username="ana_lab")
check(store.user(ANA)["status"] == "pending" and "asked the admin" in tg.last(ANA), "Ana waits")
request = tg.to(ADMIN, "sendMessage")[-1]
check("Ana (@ana_lab) wants to use the bot" in request["text"] and buttons(request) == [["✅ Approve", "❌ Deny"]], "admin is asked")
message(ANA, "hello")
check("on the list" in tg.last(ANA), "pending people are told to wait")
tap(ANA, f"ok:{ANA}")
check(store.user(ANA)["status"] == "pending", "only the admin can approve")
tap(ADMIN, f"ok:{ANA}", message_id=77)
check(store.user(ANA)["status"] == "active" and "You're in" in " ".join(tg.texts(ANA)), "approved and told")
check(any(m == "editMessageText" and p["message_id"] == 77 and "Approved: Ana" in p["text"] for m, p in tg.calls), "request message updated")
message(BEN, "/start", first="Ben")
tap(ADMIN, f"no:{BEN}")
check(store.user(BEN)["status"] == "denied" and "didn't approve" in tg.last(BEN), "Ben denied and told")
before = len(tg.calls)
message(BEN, "please?")
check(len(tg.calls) == before, "denied people get no replies")
message(CARL, "hi from a group", chat_type="group")
check(store.user(CARL) is None, "group chats are ignored")

print("5. Ana sets up, turns on Nature, and gets a first digest at once")
setup(ANA, ["affect"], None, tz_cmd="Europe/Madrid", hour=9, sources=["pubmed:nature", "pubmed:j neurosci"])
ana = store.user(ANA)
check(ana["tz"] == "Europe/Madrid" and ana["step"] is None, "custom time zone with /tz")
check(ana["sources"] == ["pubmed:nature", "pubmed:j neurosci"], f"Ana picked Nature and J Neurosci: {ana['sources']}")
check(answers[-8:].count("On: Nature") == 1 and "On: Journal of Neuroscience" in answers[-8:], "taps answered with what changed")
check(bot.pending_sources == {"pubmed:nature"}, "nobody had Nature: it's fetched now, not tomorrow")
first = tg.to(ANA, "sendPhoto")
check(len(first) == 3 and "Dopamine" in first[0]["caption"], f"reward topic -> dopamine paper first: {first[0]['caption'][:50]!r}")
check(buttons(first[0])[1:] == [["📃 Abstract", "📄 Summary"], ["📋 Copy", "🔗 Similar"]], "no LinkedIn button for others")
message(ANA, "/tz Mars/Olympus")
check("don't know that time zone" in tg.last(ANA), "bad time zone refused")

print("6. a source someone just turned on")
embedded = gemini.calls["embedded"]
run_pending_update()
check(fetched[-1] == {"pubmed:nature"} and not bot.pending_sources, "only Nature fetched")
brain = store.paper_by_doi(PAPERS[8]["doi"])
other = store.paper_by_doi(PAPERS[9]["doi"])
check(brain["vector"] is not None and not brain["hidden"] and brain["src"] == "pubmed:nature", "its brain paper is in")
check(other["hidden"] and other["vector"] is None, "its machine learning paper is kept aside, unfingerprinted")
check(gemini.calls["embedded"] - embedded == 1, "only the brain paper was sent for a fingerprint")
check(all(p["pid"] != other["pid"] for p in store.recent(7)), "and never offered")
store.run("UPDATE papers SET hook=NULL")  # as if these papers had come in after the daily update
written = gemini.calls["hook"]
message(ANA, "/digest")
check(gemini.calls["hook"] - written == 3 and all("Hook:" in c["caption"] for c in tg.to(ANA, "sendPhoto")[-3:]),
      "missing teasers are written on the spot, all three together")
check("Dopamine release encodes reward" in tg.to(ANA, "sendPhoto")[-3]["caption"], "Ana's next paper is Nature's, from a source Ana chose")
check("💡 Matches <i>Reward, emotion &amp; stress</i> · from your source Nature" in tg.to(ANA, "sendPhoto")[-3]["caption"],
      "its card says why: the area it matched, and Ana's source")
memory_fan = {"id": 999, "profile": store.user(ADMIN)["profile"]}  # into hippocampal replay and sleep; nothing sent yet
picks = digest.pick(store, memory_fan, 3, 7, ["pubmed:nature"])
check(picks[0]["pid"] == brain["pid"], "a paper from your own sources comes first, even when others match better")
check("Hippocampal replay" in picks[1]["title"] or "Sleep spindles" in picks[1]["title"], "then the best matches from elsewhere")
check(digest.pick(store, memory_fan, 1, 7, [])[0]["pid"] != brain["pid"], "without Nature ticked, the better match leads")
everyone = digest.pick(store, store.user(ADMIN), 100, 7, bot.my_sources(store.user(ADMIN)))
check(brain["pid"] in [p["pid"] for p in everyone], "people without Nature can still get its paper")

print("7. adding a journal")
message(ANA, "/sources")
lst = tg.to(ANA, "sendMessage")[-1]
check("✅ Nature" in flat(lst) and "✅ Journal of Neuroscience" in flat(lst) and "Neuron" in flat(lst), "/sources shows Ana's choices")
tap(ANA, "src_add", message_id=lst["_id"])
check(store.user(ANA)["step"] == "journal_edit" and "Which journal?" in tg.last(ANA), "asks which journal")
check(tg.edits()[-1]["message_id"] == lst["_id"] and tg.edits()[-1]["reply_markup"]["inline_keyboard"] == [], "the old list's buttons go")
message(ANA, "zzz nothing")
check("couldn't find" in tg.last(ANA) and store.user(ANA)["step"] == "journal_edit", "nothing found: says so and keeps listening")
message(ANA, "neuroscience methods")
found = tg.to(ANA, "sendMessage")[-1]
check("Which one?" in found["text"] and "Journal of neuroscience methods</b> (J Neurosci Methods), about 18 papers a month" in found["text"],
      "lists what PubMed has, with how much each publishes")
check(buttons(found) == [["1. J Neurosci Methods"], ["2. Neurosci Methods Rep"], ["Cancel"]], "one button each, and Cancel")
tap(ANA, f"src_pick:{sid('pubmed:j neurosci methods')}", message_id=found["_id"])
added = store.added_sources()
check(len(added) == 1 and added[0]["name"] == "Journal of Neuroscience Methods" and not added[0]["broad"], f"added for everyone: {added}")
check("pubmed:j neurosci methods" in store.user(ANA)["sources"], "and on for Ana")
check(any("Added <b>Journal of Neuroscience Methods</b>" in t for t in tg.texts(ANA)), "Ana is told")
check(store.user(ANA)["step"] is None, "done listening")
check("✅ Journal of Neuroscience Methods" in flat(tg.to(ANA, "sendMessage")[-1]), "the list comes back with it on")
check("pubmed:j neurosci methods" in bot.pending_sources, "its papers are fetched now")
run_pending_update()
check(store.paper_by_doi(PAPERS[10]["doi"])["vector"] is not None, "its recent paper is in")
message(ADMIN, "/sources")
check("Journal of Neuroscience Methods" in flat(tg.to(ADMIN, "sendMessage")[-1]), "the admin's list offers it too")
tap(ANA, "src_add")
message(ANA, "plos one")
single = tg.to(ANA, "sendMessage")[-1]
check("Is this the one?" in single["text"] and buttons(single)[0] == ["Add PLoS One"], "one match: asks to confirm")
tap(ANA, f"src_pick:{sid('pubmed:plos one')}")
check("only get its brain papers (about 279 papers a month)" in " ".join(tg.texts(ANA)), "a general journal: only its brain papers")
check([s for s in catalog.sources(cfg, store) if s["key"] == "pubmed:plos one"][0]["broad"], "and it's filtered when fetched")
tap(ANA, "src_add")
message(ANA, "huge journal")
tap(ANA, f"src_pick:{sid('pubmed:huge j')}")
check("publishes too much" in tg.last(ANA) and "pubmed:huge j" not in {s["key"] for s in catalog.sources(cfg, store)}, "too big: refused")
tap(ANA, "src_cancel")
check(store.user(ANA)["step"] is None and "Where your papers come from" in tg.last(ANA), "Cancel goes back to the list")
tap(ANA, f"src_pick:{sid('pubmed:j neurosci methods')}")
check(answers[-1] == "That list is old: send the journal's name again", "an old list's buttons are refused")

print("8. a question doesn't hold up the digest")
tap(ANA, "src_add")
check(store.user(ANA)["step"] == "journal_edit", "Ana is asked for a journal")
store.update_user(ANA, last_digest="2000-01-01")
bot.updating = SimpleNamespace(is_alive=lambda: True)
bot.tick(now=datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc))
check(store.user(ANA)["last_digest"] == "2026-11-04", "Ana's digest still goes out on time")
message(ANA, "/saved")
check(store.user(ANA)["step"] is None, "a command cancels the question")

print("9. turning a journal off, then the daily update")
tap(ANA, f"src:{sid('pubmed:j neurosci methods')}")
check("pubmed:j neurosci methods" not in store.user(ANA)["sources"] and answers[-1] == "Off: Journal of Neuroscience Methods", "turned off")
result = digest.daily_update(store, cfg, gemini, alert=bot.send_alert)
check({"pubmed:nature", "pubmed:plos one"} <= fetched[-1] and set(BASE_KEYS) <= fetched[-1], f"fetches the base and everyone's: {sorted(fetched[-1])}")
tap(ANA, "src_done")
check(tg.last(ANA) == "Saved: 3 sources. They count from your next digest.", "Done outside setup says how many")
check("pubmed:j neurosci methods" not in fetched[-1], "not one nobody has on")
check({s["key"] for s in store.added_sources()} == {"pubmed:plos one"}, "an added journal nobody has on is dropped")

print("10. saving, summaries, copy and LinkedIn")
first = tg.to(ANA, "sendPhoto")
pid = re.search(r"like:(\w+)", json.dumps(first[0]["reply_markup"])).group(1)
tap(ANA, f"like:{pid}", message_id=4242)
check(store.sent_row(ANA, pid)["feedback"] == 1, "👍 recorded")
check(buttons(tg.edits()[-1])[0][0] == "👍 ✓", "button shows it")
tap(ANA, f"like:{pid}")
check(store.sent_row(ANA, pid)["feedback"] == 0, "second tap takes it back")
tap(ANA, f"dislike:{pid}")
check(store.sent_row(ANA, pid)["feedback"] == -1, "👎 recorded")
liked, disliked = store.feedback_vectors(ANA)
check(liked is None and disliked.shape == (1, 768), "feedback fingerprints for scoring")
tap(ANA, f"save:{pid}")
message(ANA, "/saved")
check("Your saved papers" in tg.last(ANA) and "Dopamine" in tg.last(ANA), "/saved lists it")
bot.settings["summaries_per_day"] = 2
tap(ANA, f"sum:{pid}")
summary = tg.last(ANA)
check("<b>The question:</b>" in summary and "&lt;b&gt;&amp;" in summary and "abstract only" in summary, "summary sent, escaped")
calls = gemini.calls["explain"]
tap(ANA, f"sum:{pid}")
check(gemini.calls["explain"] == calls, "the same paper's summary is reused, not rewritten")
message(ANA, f"what about https://doi.org/{PAPERS[0]['doi']} ?")
check("The question" in tg.last(ANA), "pasted DOI of a known paper -> summary")
message(ANA, f"and {EXTERNAL['doi']}")
check("all the summaries" in tg.last(ANA), "third new summary today: daily limit")
bot.settings["summaries_per_day"] = 10
message(ANA, f"and {EXTERNAL['doi']}")
check(looked_up[-1] == EXTERNAL["doi"] and "The question" in tg.last(ANA), "unknown DOI looked up, stored and summarized")
external = store.paper_by_doi(EXTERNAL["doi"])
check(external["hidden"], "a looked-up paper isn't one of the week's new papers")
message(ANA, "/summary 10.9999/nothing.here")
check("couldn't find" in tg.last(ANA), "missing DOI reported")
message(ADMIN, f"/summary {PAPERS[1]['doi']}")
check("The question" in tg.last(ADMIN), "admin: no daily limit")
tap(ANA, f"copy:{pid}")
check("<pre>" in tg.last(ANA) and "Ready to copy" in tg.last(ANA), "copy-ready text")
tap(ANA, f"abs:{pid}")
dopamine = store.paper(pid)
check(tg.last(ANA).startswith(f"📃 <b>{dopamine['title']}</b>") and dopamine["abstract"] in tg.last(ANA), "📃 Abstract: the whole abstract")
check(not tg.to(ANA, "sendMessage")[-1].get("reply_markup"), "as plain text, no buttons")
store.add_paper(dict(PAPERS[0], doi="10.1101/long.abstract", abstract="word " * 1200), hidden=True)
tap(ANA, f"abs:{store.paper_by_doi('10.1101/long.abstract')['pid']}")
check(len(tg.last(ANA)) < 4000 and "…" in tg.last(ANA), "a very long one is cut to fit a message")
store.add_paper(dict(PAPERS[0], doi="10.1101/no.abstract", abstract=None), hidden=True)
tap(ANA, f"abs:{store.paper_by_doi('10.1101/no.abstract')['pid']}")
check("I don't have the abstract" in tg.last(ANA), "none: says so, with the link")
tap(ANA, "abs:nothing123")
check(answers[-1] == "That paper is gone", "an unknown paper")


class LIResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, ""

    def json(self):
        return self._body


def li_request(method, url, headers=None, timeout=None, **kw):
    if url.endswith("/v2/userinfo"):
        return LIResp(200, {"sub": "abc", "name": "Ali"})
    if "initializeUpload" in url:
        return LIResp(200, {"value": {"uploadUrl": "https://www.linkedin.com/dms-uploads/x", "image": "urn:li:image:X"}})
    if "dms-uploads" in url:
        return LIResp(201)
    return LIResp(201, None, {"x-restli-id": "urn:li:share:42"})


linkedin_api.session = SimpleNamespace(request=li_request)
admin_pid = re.search(r"like:(\w+)", json.dumps(tg.to(ADMIN, "sendPhoto")[0]["reply_markup"])).group(1)
tap(ADMIN, f"li:{admin_pid}")
check("Posted to LinkedIn: https://www.linkedin.com/feed/update/urn:li:share:42/" in tg.last(ADMIN), "admin posts to LinkedIn")
before = len(tg.to(ANA))
tap(ANA, f"li:{pid}")
check(len(tg.to(ANA)) == before, "others can't use the LinkedIn button")

print("11. the daily limit on ➡️ more")
bot.settings["more_per_day"] = 1
store.run("DELETE FROM sent WHERE user_id=?", ANA)  # so there are plenty left
message(ANA, "/digest")
footer = tg.to(ANA, "sendMessage")[-1]
tap(ANA, "more", message_id=footer["_id"])
footer = tg.to(ANA, "sendMessage")[-1]
check(buttons(footer) == [["➡️ 3 more"]], "the first ➡️ works")
cards = len(tg.to(ANA, "sendPhoto"))
tap(ANA, "more", message_id=footer["_id"])
check(len(tg.to(ANA, "sendPhoto")) == cards and answers[-1] == "That's all for today", "the second is over the limit")
check(tg.edits("editMessageText")[-1]["text"] == "That's all for today. More tomorrow!", "and the button says so")

print("12. search")
message(ANA, "/find sleep spindles memory")
results = tg.to(ANA, "sendMessage")[-5:]
check("closest matches" in tg.to(ANA, "sendMessage")[-6]["text"], "search header")
check("Sleep spindles" in results[0]["text"], f"best result first: {results[0]['text'][:50]!r}")
check(not any("battery" in r["text"] or "external paper" in r["text"] for r in results), "never the hidden papers")
message(ANA, "/find")
check("like <code>/find" in tg.last(ANA), "empty search explained")

print("13. author alerts")
message(ANA, "/follow Jane Smith")
check(store.follows(ANA)[0]["author_key"] == "smith j" and "Smith J." in tg.last(ANA), "following Smith J")
message(ANA, "/follow Smith")
check("first and last name" in tg.last(ANA), "a last name alone isn't enough")
message(ANA, "/following")
check("Jane Smith" in tg.last(ANA), "/following lists them")
result = digest.daily_update(store, cfg, gemini, alert=bot.send_alert)
check(any('"smith j"[au]' in t for t in pubmed_terms), "PubMed searched for the followed author")
check(result["new"] == 1, "Smith's new paper added")
texts = tg.texts(ANA)
check("someone you follow (Jane Smith)" in texts[-2] and "second replay study" in texts[-1], "Ana alerted")
check("someone you follow" not in " ".join(tg.texts(ADMIN)), "the admin doesn't follow Smith: no alert")
check(store.paper_by_doi(EXTERNAL["doi"])["vector"] is None, "the looked-up paper still has no fingerprint")
message(ANA, "/unfollow Jane Smith")
check(store.follows(ANA) == [] and "Stopped following" in tg.last(ANA), "unfollow")

print("13b. similar papers, favourites, citation files")
tap(ANA, f"near:{pid}")
near = tg.to(ANA, "sendMessage")[-6:]
check("🔗 The closest papers to" in near[0]["text"] and len(near) == 6, "🔗 Similar: a heading and 5 papers")
check(all(f"like:{pid}" not in json.dumps(m["reply_markup"]) for m in near[1:]), "not the paper itself")
tap(ANA, f"near:{store.paper_by_doi(EXTERNAL['doi'])['pid']}")
check(answers[-1] == "I can only compare papers from the last few weeks", "a looked-up paper can't be compared")
message(ANA, "/popular")
check("Nothing yet" in tg.last(ANA), "/popular: nothing until two people agree")
store.record_sent(ANA, pid, "test", None)
store.run("UPDATE sent SET saved=1 WHERE user_id=? AND pid=?", ANA, pid)  # Ana saved it (section 11 cleared Ana's papers)
tap(ADMIN, f"like:{pid}")
message(ANA, "/popular")
popular = tg.to(ANA, "sendMessage")[-2:]
check("This week's favourites" in popular[0]["text"] and "👍 1 · 💾 1" in popular[1]["text"], "Ana saved it and the admin liked it: it shows, with counts")
check("Ana" not in popular[1]["text"] and "Ali" not in popular[1]["text"], "and no names")
crossref = []


def fake_crossref(url, timeout=None):
    crossref.append(url)
    if PAPERS[1]["doi"] in url:
        body = "TY  - JOUR\nTI  - From Crossref\nAU  - Garcia, Maria\nER  - " if "research-info" in url else "@article{Garcia_2026, title={From Crossref}}"
        return SimpleNamespace(status_code=200, content=body.encode("utf-8"))
    return SimpleNamespace(status_code=404, content=b"")


cite.session = SimpleNamespace(get=fake_crossref)
tap(ANA, f"save:{store.paper_by_doi(PAPERS[0]['doi'])['pid']}")
message(ANA, "/export")
doc = [p for m, p in tg.calls if m == "sendDocument" and p["chat_id"] == ANA][-1]
name, data = doc["_files"]["document"]
text = data.decode("utf-8")
check(name == "saved-papers.ris" and "Zotero, EndNote or Mendeley" in doc["caption"] and "2 saved papers" in doc["caption"], "/export: a .ris file")
check("TI  - From Crossref" in text and "AU  - Garcia, Maria" in text, "with Crossref's full entry when it has one")
check("TY  - GENERIC" in text and "AU  - Smith" in text and text.count("ER  - ") == 2, "and the bot's own entry when it doesn't")
message(ANA, "/export bibtex")
doc = [p for m, p in tg.calls if m == "sendDocument" and p["chat_id"] == ANA][-1]
name, data = doc["_files"]["document"]
check(name == "saved-papers.bib" and data.decode().count("@article{") == 2, "/export bibtex: a .bib file")
message(BEN, "/export")
check(not [p for m, p in tg.calls if m == "sendDocument" and p["chat_id"] == BEN], "denied people get nothing")
store.add_user(555, "Eve", None, "active")
message(555, "/export")
check("Nothing saved yet" in tg.last(555), "nothing saved: says so")
store.delete_user(555)

print("13c. watched topics")
message(ANA, "/watch dopamine release")
check(store.watches(ANA) == [(ANA, "dopamine release")] and "Watching <i>dopamine release</i>" in tg.last(ANA), "/watch")
bot.settings["watches_per_person"] = 1
message(ANA, "/watch grid cells")
check("up to 1 topics" in tg.last(ANA) and len(store.watches(ANA)) == 1, "a limit on topics")
bot.settings["watches_per_person"] = 10
message(ANA, "/watching")
check("dopamine release" in tg.last(ANA), "/watching lists them")
PAPERS.append(paper(30, "Dopamine release in striatum tracks reward", "Striatal dopamine release encodes reward and motivation in mice.",
                    ["Lopez, C."]))
PAPERS.append(paper(31, "Cortical layer 6 projections to thalamus", "Layer 6 corticothalamic neurons gate thalamic relay.", ["Usrey, W."]))
result = digest.daily_update(store, cfg, gemini, alert=bot.send_alert, watched=bot.send_watch)
watch_texts = [t for t in tg.texts(ANA) if "A new paper on" in t]
check(result["new"] == 2 and len(watch_texts) == 1 and "<i>dopamine release</i>" in watch_texts[0], "a new paper on it: Ana hears that day")
check("Dopamine release in striatum tracks reward" in tg.texts(ANA)[tg.texts(ANA).index(watch_texts[0]) + 1], "with the paper")
check("Cortical layer 6" not in " ".join(tg.texts(ANA)), "not the unrelated one")
message(ANA, "/unwatch dopamine release")
check(store.watches(ANA) == [] and "Stopped watching" in tg.last(ANA), "/unwatch")

print("13d. numbers for the admin")
message(ADMIN, "/stats")
stats = tg.to(ADMIN, "sendMessage")[-1]["text"]
check("<b>The last 7 days</b>" in stats and "People: 2 active (2 on Telegram, 0 on Discord)" in stats, f"/stats: {stats[:120]!r}")
check("Reactions: 👍" in stats and "Gemini: about" in stats, "with reactions and Gemini use")
message(ANA, "/stats")
check("I send you a daily digest" in tg.last(ANA), "only for the admin")

print("14. the schedule")
store.update_user(ADMIN, last_digest="2000-01-01")
store.update_user(ANA, last_digest="2000-01-01")
morning = datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)  # 07:30 Toronto, 13:30 Madrid
cards_before = {u: len(tg.to(u, "sendPhoto")) for u in (ADMIN, ANA)}
bot.updating = SimpleNamespace(is_alive=lambda: True)  # pretend an update is running, so tick doesn't start one
bot.tick(now=morning)
check(len(tg.to(ADMIN, "sendPhoto")) == cards_before[ADMIN], "07:30 in Toronto: before the admin's 8:00, nothing")
check(len(tg.to(ANA, "sendPhoto")) > cards_before[ANA] or "No new papers" in tg.last(ANA), "13:30 in Madrid: Ana's 9:00 digest went out")
check(store.user(ANA)["last_digest"] == "2026-11-04", "Ana's digest recorded for Ana's own date")
sent = len(tg.calls)
bot.tick(now=morning + timedelta(minutes=5))
check(len([c for c in tg.calls[sent:] if c[1].get("chat_id") == ANA]) == 0, "not sent twice the same day")
bot.tick(now=morning + timedelta(hours=1))
check(store.user(ADMIN)["last_digest"] == "2026-11-04", "08:30 in Toronto: the admin's digest went out")
message(ANA, "/days")
tap(ANA, "days:weekdays")
tap(ANA, "day:2")
tap(ANA, "days_done")
check(tg.last(ANA) == "Done: your digest now comes on Mon, Tue, Thu, Fri.", "/days outside setup")
store.update_user(ANA, last_digest="2000-01-01")
bot.tick(now=morning)
check(store.user(ANA)["last_digest"] == "2000-01-01", "Wednesday isn't one of Ana's days: no digest")
bot.tick(now=morning + timedelta(days=1))
check(store.user(ANA)["last_digest"] == "2026-11-05", "Thursday is")
store.update_user(ANA, days=None)

print("15. when updates run")
started = []
bot.updating = None
bot.run_update = lambda only=None: started.append(only)
store.put("last_update", "2026-11-03")
bot.update_tried_at = None
bot.pending_sources = {"medrxiv:neurology"}
bot.maybe_update(datetime(2026, 11, 4, 5, 0, tzinfo=timezone.utc))
bot.updating.join(2)
check(started == [{"medrxiv:neurology"}], "before 06:00 UTC: only a just-turned-on source")
bot.updating = None
bot.maybe_update(datetime(2026, 11, 4, 5, 10, tzinfo=timezone.utc))
check(len(started) == 1, "and only once")
bot.pending_sources = {"medrxiv:psychiatry and clinical psychology"}
bot.maybe_update(datetime(2026, 11, 4, 6, 5, tzinfo=timezone.utc))
bot.updating.join(2)
check(started[-1] is None and not bot.pending_sources, "after 06:00 UTC: the daily update, which covers it")
bot.updating = None
bot.maybe_update(datetime(2026, 11, 4, 6, 20, tzinfo=timezone.utc))
check(len(started) == 2, "a failed try waits 30 minutes before the next")

print("16. admin view and leaving")
message(ADMIN, "/users")
check("2 active" in tg.to(ADMIN, "sendMessage")[-1]["text"], "/users counts people")
message(ANA, "/stop")
check(store.user(ANA) is None and not store.sent_pids(ANA) and not store.follows(ANA), "/stop deletes Ana's data")
check("deleted everything" in tg.last(ANA), "and tells them")
message(ANA, "/start", first="Ana")
check(store.user(ANA)["status"] == "pending", "coming back means asking again")

print("17. escaping")
dev = next(p for p in store.recent(7) if "developing cortex" in p["title"])
bot.send_card(store.user(ADMIN), dev, "test")
check("&lt;with&gt; neurogenesis &amp; more" in tg.to(ADMIN, "sendPhoto")[-1]["caption"], "titles are escaped in captions")

print("18. journal names")
check(catalog.display_name("The Journal of neuroscience : the official journal of the Society for Neuroscience", "J Neurosci")
      == "Journal of Neuroscience", "subtitles and 'The' dropped, words capitalized")
check(catalog.display_name("eLife", "Elife") == "eLife" and catalog.display_name("PloS one", "PLoS One") == "PloS One", "mixed case kept")
check(catalog.display_name("Biological psychiatry. Cognitive neuroscience and neuroimaging", "Biol Psychiatry Cogn Neurosci Neuroimaging")
      == "Biol Psychiatry Cogn Neurosci Neuroimaging", "long titles become the abbreviation")

print("19. the admin, after /stop")
message(ADMIN, "/stop")
check(store.user(ADMIN) is None, "the admin's own data is gone")
message(CARL, "/start", first="Carl")
check("Carl wants to use the bot" in tg.to(ADMIN, "sendMessage")[-1]["text"], "requests still reach the admin")
tap(ADMIN, f"ok:{CARL}", message_id=88)
check(store.user(CARL)["status"] == "active", "and Approve still works")
check(store.user(ADMIN) is None, "without setting up a digest for the admin")
message(ADMIN, "/start", first="Ali")
check(store.user(ADMIN)["is_admin"] and store.user(ADMIN)["status"] == "active", "/start brings the admin back at once")

print("20. buttons only do what they offer")
carl = store.user(CARL)
for data in ("count:100000", "count:-3", "count:0", "count:11", "hour:99", "hour:x", "tz:99", "tz:-1", "tz:abc", "day:9", "days:bogus"):
    tap(CARL, data)
after = store.user(CARL)
check((after["per_digest"], after["hour"], after["tz"], after["days"]) == (carl["per_digest"], carl["hour"], carl["tz"], carl["days"]),
      "made-up numbers change nothing")
tap(CARL, "count:5")
check(store.user(CARL)["per_digest"] == 5, "while real ones still work")
tap(CARL, "count:10")
check(store.user(CARL)["per_digest"] == 10, "up to 10 papers")

print("21. the log of slow actions says what, but not what people typed")
happened = botmod.what_happened
check(happened({"message": {"text": "/find grid cells"}}, "telegram") == "telegram /find", "a command, without its words")
check(happened({"message": {"text": "my secret project"}}, "telegram") == "telegram message", "a message, without its words")
check(happened({"callback_query": {"data": f"like:{pid}"}}, "telegram") == "telegram button like", "a button")
check(happened({"kind": "tap", "data": "more"}, "discord") == "discord button more", "on Discord too")

print("\nALL OK" if not failures else f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
