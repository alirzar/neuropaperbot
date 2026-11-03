"""Shared helpers: paths, config, the JSON files in data/, and small text utilities."""
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DRAFTS = DATA / "drafts.json"  # every draft and its status
SEEN = DATA / "seen.json"  # DOIs already scored, so each paper is considered once
STATE = DATA / "state.json"  # Telegram update offset and the date of the last batch
POSTED = DATA / "posted.json"  # log of everything posted

# Local runs read secrets from .env; on GitHub they come from repository secrets.
load_dotenv(ROOT / ".env")

# Windows consoles and redirected output default to a legacy code page, and the
# text printed here has "…", "·" and emoji in it.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now_utc()).isoformat(timespec="seconds")


def parse_iso(text):
    return datetime.fromisoformat(text)


def require_env(*names):
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)}: set it in .env locally, or as a GitHub secret")
    return [os.environ[n] for n in names]


def short_id(doi):
    """Stable 10-character ID for a paper, short enough for Telegram button data."""
    return hashlib.sha1(doi.lower().encode("utf-8")).hexdigest()[:10]


_TAGS = re.compile(r"<[^>]+>")


def clean_text(text):
    """Drop HTML tags and entities, and collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub("", text or ""))).strip()


def title_key(title):
    """Lowercase letters and digits only: spots one paper under two DOIs (preprint, then journal)."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def shorten(text, limit):
    """Cut text to at most `limit` characters at a word boundary, ending in an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def gemini_client():
    from google import genai  # imported here so scripts that don't use Gemini don't load it
    from google.genai import types

    (key,) = require_env("GEMINI_API_KEY")
    # Busy or rate-limited requests (HTTP 429 and 5xx) are retried with backoff, about 30 seconds in all.
    retry = types.HttpRetryOptions(attempts=5, initial_delay=2, max_delay=16)
    return genai.Client(api_key=key, http_options=types.HttpOptions(retry_options=retry))


def gemini_models(cfg):
    """The configured model, then the fallback for when it's overloaded."""
    gemini = cfg["gemini"]
    return [m for m in (gemini.get("model"), gemini.get("fallback_model")) if m]


def gemini_generate(client, models, contents, config=None):
    """Gemini's reply text, from the first of `models` that answers."""
    error = None
    for model in models:
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            if resp.text:
                return resp.text
            error = ValueError(f"empty reply from {model}")
        except Exception as exc:
            error = exc
        print(f"  {model} didn't answer: {describe(error)}")
    raise error or ValueError("no Gemini model set in config.json")


def describe(exc):
    """One line for an error. (Gemini's errors otherwise include the whole JSON response.)"""
    code, status = getattr(exc, "code", None), getattr(exc, "status", None)
    return f"{code} {status}" if code else str(exc)[:200]
