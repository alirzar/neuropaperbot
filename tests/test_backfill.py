"""Filling in older papers: a week at a time, within a daily budget, with their own dates.

Runs in a throwaway copy with its own database; the paper sources and Gemini are stand-ins.
"""
import atexit
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="neuropaperbot-backfill-"))  # a throwaway copy of the project
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
for name in ("scripts", "assets"):
    shutil.copytree(PROJECT / name, WORK / name, ignore=shutil.ignore_patterns("__pycache__"))
(WORK / "data").mkdir()
shutil.copy(PROJECT / "config.json", WORK / "config.json")
sys.path.insert(0, str(WORK / "scripts"))
os.environ.update(TELEGRAM_BOT_TOKEN="fake", TELEGRAM_CHAT_ID="111", GEMINI_API_KEY="fake")

import bot as botmod  # noqa: E402
import digest  # noqa: E402
import telegram_api  # noqa: E402
from common import load_config, now_utc  # noqa: E402
from store import Store  # noqa: E402

failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


class FakeGemini:
    def __init__(self):
        self.models, self.embedded, self.texts = self, 0, []

    def embed_content(self, *, model, contents, config=None):
        self.embedded += len(contents)
        self.texts += [c.parts[0].text for c in contents]
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] + [0.0] * 767) for _ in contents])

    def generate_content(self, *, model, contents, config=None):
        items = re.findall(r"^ID: (p\d+)\nSOURCE: .*\nTITLE: (.*)$", contents, re.M)
        if config and "Hook" in str(config.get("response_schema")):
            return SimpleNamespace(text=json.dumps([{"id": i, "hook": "Hook"} for i, _ in items]))
        return SimpleNamespace(text=json.dumps([{"id": i, "score": 6, "reason": "fake", "neuro": "machine" not in t} for i, t in items]))


TODAY = now_utc().date()
windows = []


def fake_fetch(sources, start, end, pubmed=None):
    """Three neuroscience papers a week, dated within the window, and one off-topic paper from a general journal."""
    windows.append((start, end))
    papers = [
        {"doi": f"10.1101/old.{start}.{i}", "source": "biorxiv", "venue": "bioRxiv preprint", "src": "biorxiv:neuroscience",
         "title": f"Paper {i} of the week of {start}", "authors": ["Smith"], "author_keys": ["smith j"],
         "date": str(start + timedelta(days=i)), "link": "", "abstract": "Neurons and memory.", "license": None}
        for i in range(3)
    ]
    papers.append(dict(papers[0], doi=f"10.1038/ml.{start}", src="pubmed:nature", venue="Nature", broad=True,
                       title="A machine learning model of batteries"))
    return papers, []


digest.fetch_sources = fake_fetch
cfg = load_config()
cfg["bot"].update(backfill_days=28, backfill_per_day=5, recent_days=7, keep_days=400)
store = Store(WORK / "data" / "bot.db")
gemini = FakeGemini()
week_ago = TODAY - timedelta(days=7)

print("1. before any filling in")
check(digest.history_start(store, cfg) == week_ago, f"history covers the last week: {digest.history_start(store, cfg)}")

print("2. one step: the week before last")
made, done = digest.backfill_step(store, cfg, gemini, 5)
check(windows == [(TODAY - timedelta(days=14), TODAY - timedelta(days=8))], f"fetched the week before the last 7 days: {windows}")
check(made == 3 and not done, "its 3 papers got fingerprints")
old = store.paper_by_doi(f"10.1101/old.{TODAY - timedelta(days=14)}.1")
check(old["added_at"] == f"{TODAY - timedelta(days=13)}T12:00:00+00:00", f"each counts from its own date: {old['added_at']}")
check(old["quality"] == 6 and not old["hook"], "scored, but no teaser written ahead")
ml = store.paper_by_doi(f"10.1038/ml.{TODAY - timedelta(days=14)}")
check(ml["hidden"] and ml["vector"] is None, "the off-topic paper from a general journal is set aside")
check(not store.recent(7), "none of them count as this week's")
check(len(store.candidates(15)) == 3, "but a range reaching back past them includes them")
check(digest.history_start(store, cfg) == TODAY - timedelta(days=14), "history now covers 2 weeks")

print("3. the daily budget")
made, done = digest.backfill_step(store, cfg, gemini, 2)
check(made == 2 and len(windows) == 2, "a smaller budget: 2 of the next week's 3 fingerprinted")
check(digest.history_start(store, cfg) > TODAY - timedelta(days=21), "history doesn't claim the half-done week")
made, done = digest.backfill_step(store, cfg, gemini, 0)
check(made == 0 and len(windows) == 2, "no budget left: nothing fetched")
made, done = digest.backfill_step(store, cfg, gemini, 5)
check(made == 1 and len(windows) == 2, "next time, the waiting one first, before fetching more")
check(digest.history_start(store, cfg) == TODAY - timedelta(days=21), "now history covers 3 weeks")

print("4. until a year back (here, 4 weeks)")
while not done:
    made, done = digest.backfill_step(store, cfg, gemini, 5)
check(windows[-1] == (TODAY - timedelta(days=28), TODAY - timedelta(days=22)), f"stops at the goal: {windows[-1]}")
check(digest.history_start(store, cfg) == TODAY - timedelta(days=28), "history covers the whole goal")
check(digest.backfill_step(store, cfg, gemini, 5) == (0, True), "and stays done")

print("5. the daily update leaves old papers to the filling in")
store.add_paper({"doi": "10.1101/waiting.old", "title": "Waiting", "abstract": "Old one.", "date": str(TODAY - timedelta(days=40))},
                added_at=f"{TODAY - timedelta(days=40)}T12:00:00+00:00")
digest.fetch_sources = lambda *a, **k: ([], [])
before = gemini.embedded
digest.daily_update(store, cfg, gemini)
check(gemini.embedded == before and store.paper_by_doi("10.1101/waiting.old")["vector"] is None, "it doesn't spend fingerprints on them")
digest.fetch_sources = fake_fetch

print("6. in the bot")
tg_calls = []
telegram_api.call = lambda method, files=None, **params: (tg_calls.append((method, params)) or {"message_id": 1})
bot = botmod.Bot(cfg, store, gemini)
bot.settings.update(backfill_days=28, backfill_per_day=4, keep_days=400)
store.put("last_update", TODAY.isoformat())
store.put("backfill_next", (TODAY - timedelta(days=7)).isoformat())  # start over, so there is something to fill in
store.run("DELETE FROM papers")
windows.clear()
noon = datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=timezone.utc)
bot.maybe_update(noon)
bot.updating.join(10)
check(windows and int(store.get("backfill_used")) == 3 and store.get("backfill_day") == TODAY.isoformat(), "a step runs once today's update is done")
bot.maybe_update(noon)
bot.updating.join(10)
check(int(store.get("backfill_used")) == 4, "the next step stops at the daily budget of 4")
bot.updating = None
bot.maybe_update(noon)
check(bot.updating is None, "and then nothing more today")
store.put("last_update", (TODAY + timedelta(days=1)).isoformat())  # the next day's update has run
bot.maybe_update(noon + timedelta(days=1))
bot.updating.join(10)
check(store.get("backfill_day") == (TODAY + timedelta(days=1)).isoformat(), "the next day, after its update, a new budget")
bot.backfill_done = False
store.put("backfill_next", (TODAY - timedelta(days=400)).isoformat())
store.run("DELETE FROM papers WHERE vector IS NULL")
check([d for d in bot.ranges()][-2:] == [182, 365], f"with a year of history, /range offers 6 months and a year: {bot.ranges()}")
store.put("backfill_next", (TODAY - timedelta(days=100)).isoformat())
check(bot.ranges()[-1] == 60, "with 100 days, only up to 2 months")
bot.settings["keep_days"] = 200
store.put("backfill_next", (TODAY - timedelta(days=400)).isoformat())
check(bot.ranges()[-1] == 182, "never past the papers kept")
check("History: every paper since" in bot.history_line(), f"/stats says how far back: {bot.history_line()}")

print("\nALL OK" if not failures else f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
