"""Post text, Telegram review messages and draft bookkeeping, shared by batch.py, review.py and post.py.

A draft is queued for the platforms whose buttons you tap (its "targets"), and its status follows
from that: sent (nothing picked yet), approved (still queued somewhere), posted or failed; or
skipped or expired.
"""
import html
import re

from common import shorten

BLUESKY_LIMIT = 300
X_LIMIT = 280
LINKEDIN_LIMIT = 3000
CAPTION_LIMIT = 1000  # Telegram allows 1024 characters in a photo caption; keep some slack

PLATFORMS = {"channel": "Channel", "bluesky": "Bluesky", "linkedin": "LinkedIn", "x": "X"}
BUTTONS = {"channel": "📢 Channel", "bluesky": "🦋 Bluesky", "linkedin": "💼 LinkedIn", "x": "𝕏 X"}


def enabled_platforms(cfg):
    """The platforms in posting.platforms that the bot knows how to post to."""
    return [p for p in cfg["posting"].get("platforms", []) if p in PLATFORMS]


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


def copy_versions(d, hashtag):
    """Posts to paste by hand, link included: one short enough for X and Bluesky, a longer one for anywhere else."""
    hook = (d.get("hook") or "").strip() or d["title"]
    short_tail = f"\n\n{attribution(d)}\n{d['link']}" + (f"\n{hashtag}" if hashtag else "")
    short = shorten(hook, X_LIMIT - len(short_tail)) + short_tail
    long = f"{hook}\n\n📄 {d['title']}\n{attribution(d)}\n{d['link']}" + (f"\n\n{hashtag}" if hashtag else "")
    return short, long


def copy_message(d, hashtag):
    """Telegram message (HTML) with both versions in code blocks, which Telegram copies with a tap."""

    def e(text):
        return html.escape(text, quote=False)

    short, long = copy_versions(d, hashtag)
    return (
        "📋 <b>Ready to copy</b>\n\n"
        f"<b>X and Bluesky</b>\n<pre>{e(short)}</pre>\n\n"
        f"<b>LinkedIn and others</b>\n<pre>{e(long)}</pre>"
    )


def channel_post(d, hashtag):
    """Caption (HTML) for the public channel: the hook, then the paper."""

    def e(text):
        return html.escape(text, quote=False)

    hook = shorten((d.get("hook") or "").strip(), 400)
    parts = [e(hook)] if hook else []
    parts.append(f"<b>{e(d['title'])}</b>\n{e(attribution(d))}\n{e(d['link'])}")
    if hashtag:
        parts.append(e(hashtag))
    return "\n\n".join(parts)


def channel_link(channel, message_id):
    """Link to a channel post, for a channel given as @name (public) or as its -100… ID (private)."""
    channel = str(channel)
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}/{message_id}"
    if channel.startswith("-100"):
        return f"https://t.me/c/{channel[4:]}/{message_id}"
    return None


def pending(d):
    """Platforms the draft is queued for that it hasn't been posted to, or given up on, yet."""
    done = set(d.get("posted", {})) | set(d.get("failed", {}))
    return [p for p in d.get("targets", []) if p not in done]


def settle(d):
    """Set the status of a draft you've acted on, from where it's queued and where it went out."""
    if pending(d):
        d["status"] = "approved"
    elif d.get("posted"):
        d["status"] = "posted"
    elif d.get("targets"):
        d["status"] = "failed"
    else:
        d["status"] = "sent"


def status_line(d, platforms):
    if d["status"] in ("ready", "sent"):
        return "🕓 Waiting for you (taps are picked up within the hour)"
    if d["status"] == "skipped":
        return "❌ Skipped"
    if d["status"] == "expired":
        return "⌛ Expired"
    parts = []
    for p in d.get("targets", []):
        name = PLATFORMS.get(p, p)
        if p in d.get("posted", {}):
            url = d["posted"][p].get("url")
            parts.append(f'📣 <a href="{html.escape(url)}">Posted to {name}</a>' if url else f"📣 Posted to {name}")
        elif p in d.get("failed", {}):
            parts.append(f"⚠️ {name} failed: {html.escape(d['failed'][p], quote=False)}")
        elif p in d.get("notes", {}):
            parts.append(f"⚠️ Queued for {name}, but {html.escape(d['notes'][p], quote=False)}")
        elif p in platforms:
            parts.append(f"✅ Queued for {name}")
        else:
            parts.append(f"⏸ Queued for {name}, which isn't turned on in config.json")
    return " · ".join(parts)


def review_caption(d, hashtag, platforms):
    """Telegram caption (HTML) for a draft: its status, the post as it will look, and the paper."""

    def e(text):
        return html.escape(text, quote=False)  # quotes only need escaping inside attributes

    parts = [
        f"{status_line(d, platforms)}\n<b>{e(d['venue'])}</b> · score {d.get('score', '?')}/10 · {e(d.get('reason') or '')}",
        f"<blockquote>{e(post_text(d, hashtag))}</blockquote>",
        f"<b>{e(d['title'])}</b>",
        "",  # as much of the abstract as fits, filled in below
        e(d["link"]),
    ]
    if d["status"] in ("sent", "approved"):
        hint = "Reply to this message with new wording to replace the hook."
        if not platforms:
            hint += " No platform is turned on yet (posting.platforms in config.json), so there's nothing to post to."
        parts.append(f"<i>{hint}</i>")
    room = CAPTION_LIMIT - visible_length("\n\n".join(p for p in parts if p)) - 2
    abstract = d.get("abstract") or d.get("snippet") or ""
    parts[3] = e(shorten(abstract, room)) if room > 60 else ""
    return "\n\n".join(p for p in parts if p)


def review_buttons(d, platforms):
    """A button for each platform the draft can still be queued for, then Skip or Undo."""
    rows = []
    if d["status"] in ("sent", "approved"):
        row = [
            {"text": BUTTONS.get(p, p), "callback_data": f"to:{p}:{d['id']}"}
            for p in platforms
            if p not in d.get("targets", [])
        ]
        if row:
            rows.append(row)
    if d["status"] == "sent":
        rows.append([{"text": "❌ Skip", "callback_data": f"skip:{d['id']}"}])
    elif d["status"] in ("approved", "skipped"):
        rows.append([{"text": "↩️ Undo", "callback_data": f"undo:{d['id']}"}])
    return {"inline_keyboard": rows}


def visible_length(markup):
    return len(html.unescape(re.sub(r"<[^>]+>", "", markup)))
