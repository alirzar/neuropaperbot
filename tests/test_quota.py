"""Gemini's free-tier limits: embeddings wait as long as asked, and the daily update saves batch by batch."""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="neuropaperbot-quota-"))  # a throwaway copy of the project
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
for name in ("scripts", "assets"):
    shutil.copytree(PROJECT / name, WORK / name, ignore=shutil.ignore_patterns("__pycache__"))
(WORK / "data").mkdir()
shutil.copy(PROJECT / "config.json", WORK / "config.json")
sys.path.insert(0, str(WORK / "scripts"))
os.environ.update(GEMINI_API_KEY="fake")

import digest  # noqa: E402
import vectors  # noqa: E402
from common import load_config  # noqa: E402
from store import Store  # noqa: E402

failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


class QuotaError(Exception):
    code = 429


class OtherError(Exception):
    code = 500


class FakeClient:
    def __init__(self, script):
        self.models, self.script, self.calls = self, list(script), 0

    def embed_content(self, *, model, contents, config=None):
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if step == "quota":
            raise QuotaError("429 RESOURCE_EXHAUSTED ... Please retry in 3.5s. ... 'retryDelay': '3s'")
        if step == "fail":
            raise OtherError("500 INTERNAL")
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] + [0.0] * 767) for _ in contents])

    def generate_content(self, *, model, contents, config=None):
        raise OtherError("no scoring in this test")


waits = []
vectors.time.sleep = waits.append

print("1. a rate limit is waited out")
client = FakeClient(["quota", "quota", "ok"])
out = vectors.embed(client, "m", ["a", "b"], "RETRIEVAL_DOCUMENT")
check(out.shape == (2, 768) and client.calls == 3, "two waits, then the batch goes through")
check(waits == [5.5, 5.5], f"waits for the time Gemini asked, plus 2 seconds: {waits}")
check(vectors.retry_after(QuotaError("no hint at all")) == 60, "no hint: a minute")
check(vectors.retry_after(QuotaError("retry in 999s")) == 120, "never more than 2 minutes")

print("2. other errors aren't waited on")
try:
    vectors.embed(FakeClient(["fail"]), "m", ["a"], "RETRIEVAL_DOCUMENT")
    check(False, "a server error is raised")
except OtherError:
    check(True, "a server error is raised")

print("3. the daily update saves batch by batch")
papers = [{"doi": f"10.1101/x.{i}", "title": f"Paper {i}", "abstract": "text", "authors": [], "author_keys": [],
           "source": "biorxiv", "venue": "bioRxiv preprint", "src": "biorxiv:neuroscience", "link": "", "date": None}
          for i in range(120)]
digest.fetch_sources = lambda sources, start, end, pubmed=None: ([dict(p) for p in papers], [])
store = Store(WORK / "data" / "db" / "bot.db")
try:
    digest.daily_update(store, load_config(), FakeClient(["ok", "fail"]))
    check(False, "the update stops when Gemini keeps failing")
except OtherError:
    check(True, "the update stops when Gemini keeps failing")
check(len(store.recent(7)) == 50 and store.get("last_update") is None, "the first batch of 50 was kept; the day isn't marked done")
check(len(store.unembedded()) == 70, "the other 70 are saved, waiting for their fingerprints")
client = FakeClient([])
result = digest.daily_update(store, load_config(), client)
check(result["new"] == 0 and client.calls == 2 and len(store.recent(7)) == 120, "the next try only fingerprints those 70")
check(store.get("last_update") is not None, "and marks the day done")

print("4. scoring failing doesn't stop the update")
store = Store(WORK / "data" / "db2" / "bot.db")
result = digest.daily_update(store, load_config(), FakeClient([]))
check(any("scoring" in e for e in result["errors"]) and len(store.recent(7)) == 120, "papers saved and fingerprinted, without scores")

print("\nALL OK" if not failures else f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
