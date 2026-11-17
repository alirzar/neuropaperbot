"""A minimal Telegram Bot API client: only the calls review.py, post.py and the public bot need.

API reference: https://core.telegram.org/bots/api
"""
import json
import os
import re
import threading

import requests

TIMEOUT = 60
_local = threading.local()  # each thread's own connection to Telegram, kept open between calls


class TelegramError(Exception):
    pass


def _session():
    """This thread's connection to Telegram. Reusing it saves finding and connecting to Telegram's servers
    on every call, which can take seconds; each thread keeps its own, as a session isn't meant to be shared."""
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
    return _local.session


def _drop_session():
    """Close and forget this thread's connection, after it failed: the next call opens a new one."""
    old = _local.__dict__.pop("session", None)
    if old is not None:
        old.close()


def _reason(exc, token):
    """What went wrong, briefly and without the token: requests' errors quote the URL, which holds it."""
    inner = exc.args[0] if exc.args else exc
    inner = getattr(inner, "reason", None) or inner  # urllib3's MaxRetryError wraps the actual cause
    text = re.sub(r"<[\w.]+ object at 0x[0-9a-fA-F]+>:?\s*", "", str(inner))
    for secret in (token, token.partition(":")[2]):
        if secret:
            text = text.replace(secret, "…")
    return f"{type(exc).__name__}: {text[:200]}" if text.strip() else type(exc).__name__


def call(method, files=None, **params):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set (in .env locally, or as a GitHub secret)")
    data = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items() if v is not None}
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        for attempt in range(2):
            try:
                resp = _session().post(url, data=data, files=files, timeout=TIMEOUT)
                break
            except requests.ConnectionError:
                # A connection kept open can go stale while it sits idle, if the other end drops it: open a
                # new one and try once more. (Not after a timeout, when the message may have gone out.)
                _drop_session()
                if attempt:
                    raise
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise TelegramError(f"{method}: {_reason(exc, token)}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description', 'failed')}")
    return body["result"]


# ---------- for the public bot, which talks to many chats ----------
def keyboard(rows):
    return {"inline_keyboard": rows} if rows else None


def send(chat, text, buttons=None, reply_to=None):
    """An HTML message to any chat, without link previews."""
    return call(
        "sendMessage",
        chat_id=chat,
        text=text,
        parse_mode="HTML",
        link_preview_options={"is_disabled": True},
        reply_markup=keyboard(buttons),
        reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
    )


def photo(chat, png, caption, buttons=None):
    return call(
        "sendPhoto",
        files={"photo": ("card.png", png, "image/png")},
        chat_id=chat,
        caption=caption,
        parse_mode="HTML",
        reply_markup=keyboard(buttons),
    )


def document(chat, data, filename, caption):
    """A file, such as a citation file, with an HTML caption."""
    return call("sendDocument", files={"document": (filename, data)}, chat_id=chat, caption=caption, parse_mode="HTML")


def edit(chat, message_id, text, buttons=None):
    """Replace a text message's text, and its buttons (none if not given)."""
    try:
        call(
            "editMessageText",
            chat_id=chat,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            reply_markup={"inline_keyboard": buttons or []},
        )
    except TelegramError as exc:
        if "message is not modified" not in str(exc):
            raise


def edit_markup(chat, message_id, buttons):
    try:
        call("editMessageReplyMarkup", chat_id=chat, message_id=message_id, reply_markup={"inline_keyboard": buttons or []})
    except TelegramError as exc:
        if "message is not modified" not in str(exc):
            raise


def answer(callback_id, text=None):
    """Stop a tapped button's spinner, optionally with a short note."""
    try:
        call("answerCallbackQuery", callback_query_id=callback_id, text=text)
    except TelegramError:
        pass  # taps can't be answered once they're a few minutes old; nothing to do about that


def typing(chat):
    try:
        call("sendChatAction", chat_id=chat, action="typing")
    except TelegramError:
        pass


class App:
    """The public bot's way to talk to people on Telegram (discord_api.App has the same methods)."""

    name = "Telegram"

    def send(self, chat, text, buttons=None):
        return send(chat, text, buttons)["message_id"]

    def photo(self, chat, png, caption, buttons=None):
        return photo(chat, png, caption, buttons)["message_id"]

    def document(self, chat, data, filename, caption):
        return document(chat, data, filename, caption)["message_id"]

    def edit(self, chat, message_id, text, buttons=None):
        edit(chat, message_id, text, buttons)

    def edit_buttons(self, chat, message_id, buttons):
        edit_markup(chat, message_id, buttons)

    def typing(self, chat):
        typing(chat)


def chat_id():
    cid = os.getenv("TELEGRAM_CHAT_ID")
    if not cid:
        raise TelegramError("TELEGRAM_CHAT_ID is not set; run `python scripts/review.py chat-id` to find it")
    return cid


def send_photo(png, caption, buttons=None, chat=None):
    """Send a card with an HTML caption to your chat, or to `chat` (such as a channel's @username)."""
    return call(
        "sendPhoto",
        files={"photo": ("card.png", png, "image/png")},
        chat_id=chat or chat_id(),
        caption=caption,
        parse_mode="HTML",
        reply_markup=buttons,
    )


def edit_caption(message_id, caption, buttons):
    try:
        call(
            "editMessageCaption",
            chat_id=chat_id(),
            message_id=message_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=buttons,
        )
    except TelegramError as exc:
        if "message is not modified" not in str(exc):
            raise


def send_message(text, reply_to=None, html=False):
    reply = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
    formatting = {"parse_mode": "HTML", "link_preview_options": {"is_disabled": True}} if html else {}
    return call("sendMessage", chat_id=chat_id(), text=text, reply_parameters=reply, **formatting)


def edit_text(message_id, text):
    """Replace the text of an HTML message sent with send_message(html=True)."""
    try:
        call(
            "editMessageText",
            chat_id=chat_id(),
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )
    except TelegramError as exc:
        if "message is not modified" not in str(exc):
            raise
