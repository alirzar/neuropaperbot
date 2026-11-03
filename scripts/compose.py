"""Post text and Telegram review messages, shared by batch.py, review.py and post.py."""
import html
import re

from common import shorten

BLUESKY_LIMIT = 300
X_LIMIT = 280
CAPTION_LIMIT = 1000  # Telegram allows 1024 characters in a photo caption; keep some slack

STATUS = {
    "sent": "🕓 Waiting for you (taps are picked up within the hour)",
    "approved": "✅ Approved and queued for posting",
    "posted": "📣 Posted",
    "skipped": "❌ Skipped",
    "expired": "⌛ Expired",
    "failed": "⚠️ Posting failed",
}


def byline(authors):
    if not authors:
        return None
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]} and {authors[1]}"
    return f"{authors[0]} et al."


def attribution(d):
    """'Smith et al. · Neuron'"""
    return " · ".join(x for x in (byline(d.get("authors")), d.get("venue")) if x)


def tail(d, hashtag):
    return f"\n\n{attribution(d)}" + (f"\n{hashtag}" if hashtag else "")


def hook_room(d, hashtag, limit=BLUESKY_LIMIT):
    """How many characters the hook can have."""
    return limit - len(tail(d, hashtag))


def post_text(d, hashtag, limit=BLUESKY_LIMIT):
    """The hook, then who and where. The title and link go in the post's link card."""
    hook = (d.get("hook") or "").strip() or d["title"]
    return shorten(hook, hook_room(d, hashtag, limit)) + tail(d, hashtag)


def review_caption(d, hashtag):
    """Telegram caption (HTML) for a draft: its status, the post as it will look, and the paper."""
    def e(text):
        return html.escape(text, quote=False)  # quotes only need escaping inside attributes

    status = STATUS.get(d["status"], d["status"])
    if d["status"] == "posted" and d.get("bluesky_url"):
        status += f': <a href="{html.escape(d["bluesky_url"])}">see it on Bluesky</a>'
    if d["status"] == "failed" and d.get("error"):
        status += f": {e(d['error'])}"
    parts = [
        f"{status}\n<b>{e(d['venue'])}</b> · score {d.get('score', '?')}/10 · {e(d.get('reason') or '')}",
        f"<blockquote>{e(post_text(d, hashtag))}</blockquote>",
        f"<b>{e(d['title'])}</b>",
        "",  # as much of the abstract as fits, filled in below
        e(d["link"]),
    ]
    if d["status"] in ("sent", "approved"):
        parts.append("<i>Reply to this message with new wording to replace the hook.</i>")
    room = CAPTION_LIMIT - visible_length("\n\n".join(p for p in parts if p)) - 2
    abstract = d.get("abstract") or d.get("snippet") or ""
    parts[3] = e(shorten(abstract, room)) if room > 60 else ""
    return "\n\n".join(p for p in parts if p)


def review_buttons(d):
    if d["status"] == "sent":
        row = [
            {"text": "✅ Post", "callback_data": f"post:{d['id']}"},
            {"text": "❌ Skip", "callback_data": f"skip:{d['id']}"},
        ]
    elif d["status"] in ("approved", "skipped"):
        row = [{"text": "↩️ Undo", "callback_data": f"undo:{d['id']}"}]
    else:
        row = []
    return {"inline_keyboard": [row] if row else []}


def visible_length(markup):
    return len(html.unescape(re.sub(r"<[^>]+>", "", markup)))
