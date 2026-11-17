"""Talking to Telegram and bioRxiv: connections kept open, dropped ones tried again, and errors that say
what went wrong without giving away the bot's token.

Runs in a throwaway copy of the project; the connections are stand-ins, so nothing goes online.
"""
import atexit
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import requests
from urllib3.exceptions import MaxRetryError, ProtocolError

PROJECT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="neuropaperbot-network-"))  # a throwaway copy of the project
atexit.register(shutil.rmtree, WORK, ignore_errors=True)
shutil.copytree(PROJECT / "scripts", WORK / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
sys.path.insert(0, str(WORK / "scripts"))
TOKEN = "123456:SECRET-part_of-the-token"
os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN

import fetch_papers  # noqa: E402
import telegram_api  # noqa: E402

failures = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self.body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)

    def json(self):
        return self.body


class FakeConnection:
    """Answers each request with the next outcome: a response, or an error to raise."""

    def __init__(self, outcomes):
        self.outcomes, self.urls = list(outcomes), []

    def next(self, url):
        self.urls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def post(self, url, data=None, files=None, timeout=None):
        return self.next(url)

    def get(self, url, params=None, timeout=None):
        return self.next(url)

    def close(self):
        self.closed = True


def error_from(call):
    try:
        call()
    except Exception as exc:
        return exc
    return None


print("1. Telegram: one connection per thread, kept open")
first, again = telegram_api._session(), telegram_api._session()
elsewhere = []
thread = threading.Thread(target=lambda: elsewhere.append(telegram_api._session()))
thread.start()
thread.join()
check(isinstance(first, requests.Session) and first is again, "every call in a thread uses the same connection")
check(elsewhere and elsewhere[0] is not first, "and each thread has its own")
fake = FakeConnection([FakeResponse(body={"ok": True, "result": {"id": 1}}), FakeResponse(body={"ok": True, "result": True})])
telegram_api._local.session = fake
check(telegram_api.call("getMe") == {"id": 1} and telegram_api.call("sendMessage", chat_id=1, text="hi") is True,
      "calls go through it")
check(len(fake.urls) == 2 and fake.urls[0].endswith("/getMe"), "both on the one connection")

print("2. Telegram: a connection that went stale is replaced, once")
REAL_SESSION, new_ones = requests.Session, []
requests.Session = lambda: new_ones.pop(0)  # the connections telegram_api opens from here on
dropped = requests.ConnectionError(MaxRetryError(None, f"/bot{TOKEN}/getUpdates",
                                                 ProtocolError("Connection aborted.", ConnectionResetError(104, "Connection reset by peer"))))
stale, fresh = FakeConnection([dropped]), FakeConnection([FakeResponse(body={"ok": True, "result": {"id": 2}})])
telegram_api._local.session, new_ones[:] = stale, [fresh]
check(telegram_api.call("getMe") == {"id": 2} and len(stale.urls) == len(fresh.urls) == 1, "dropped: the call goes through on a new connection")
check(getattr(stale, "closed", False) and telegram_api._session() is fresh, "the stale one is closed, and the new one kept")
telegram_api._local.session, new_ones[:] = FakeConnection([dropped]), [FakeConnection([dropped])]
exc = error_from(lambda: telegram_api.call("getUpdates", timeout=25))
message = str(exc)
check(isinstance(exc, telegram_api.TelegramError) and message.startswith("getUpdates: ConnectionError") and not new_ones,
      f"dropped again: no third try, but an error: {message}")

print("3. Telegram: errors say why, without the token")
check("Connection reset by peer" in message and "SECRET" not in message and "123456" not in message, "it names the cause, and not the token")
quoted = requests.ConnectionError(f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: /bot{TOKEN}/getMe")
telegram_api._local.session, new_ones[:] = FakeConnection([quoted]), [FakeConnection([quoted])]
message = str(error_from(lambda: telegram_api.call("getMe")))
check("SECRET" not in message and "/bot…/getMe" in message, f"even when the error quotes the URL: {message}")
slow = FakeConnection([requests.ReadTimeout("Read timed out. (read timeout=60)")])
telegram_api._local.session, new_ones[:] = slow, []
message = str(error_from(lambda: telegram_api.call("sendMessage", chat_id=1, text="hi")))
check(message == "sendMessage: ReadTimeout: Read timed out. (read timeout=60)" and len(slow.urls) == 1,
      f"a timeout isn't tried again, as the message may have gone out: {message}")
telegram_api._local.session = FakeConnection([FakeResponse(body={"ok": False, "description": "Bad Gateway"})])
check(str(error_from(lambda: telegram_api.call("getUpdates"))) == "getUpdates: Bad Gateway", "Telegram's own errors as before")
requests.Session = REAL_SESSION

print("4. bioRxiv: a dropped connection or a server error is tried again")
fetch_papers.RETRY_WAIT = 0
RECORDS = [{"doi": f"10.1101/2026.11.0{i}", "version": "1", "title": f"Paper {i}", "authors": "Smith, J.; Lee, K.",
            "date": "2026-11-10", "category": "neuroscience", "abstract": "Text.", "license": "cc_by"} for i in range(3)]
PAGE = FakeResponse(body={"messages": [{"status": "ok", "total": 3}], "collection": RECORDS})
GONE = requests.ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")
fetch_papers.session = FakeConnection([GONE, PAGE])
papers = fetch_papers.fetch_biorxiv("2026-11-09", "2026-11-13", "neuroscience")
check(len(papers) == 3 and len(fetch_papers.session.urls) == 2, "a dropped connection: the page comes on the second try")
fetch_papers.session = FakeConnection([requests.ReadTimeout("Read timed out."), FakeResponse(503), PAGE])
papers = fetch_papers.fetch_biorxiv("2026-11-09", "2026-11-13", "neuroscience")
check(len(papers) == 3 and len(fetch_papers.session.urls) == 3, "a timeout and a server error, then the third try works")
fetch_papers.session = FakeConnection([GONE, GONE, GONE, PAGE])
exc = error_from(lambda: fetch_papers.fetch_biorxiv("2026-11-09", "2026-11-13", "neuroscience"))
check(isinstance(exc, requests.ConnectionError) and len(fetch_papers.session.urls) == 3, "three failures: it gives up on the source")
fetch_papers.session = FakeConnection([FakeResponse(404), PAGE])
exc = error_from(lambda: fetch_papers.fetch_biorxiv("2026-11-09", "2026-11-13", "neuroscience"))
check(isinstance(exc, requests.HTTPError) and len(fetch_papers.session.urls) == 1, "a mistake on our side (404) isn't tried again")

print("\nALL OK" if not failures else f"\n{len(failures)} FAILED: {failures}")
raise SystemExit(1 if failures else 0)
