"""A minimal Telegram Bot API client: only the calls review.py, post.py and the public bot need.

API reference: https://core.telegram.org/bots/api
"""
import json
import os

import requests

TIMEOUT = 60


class TelegramError(Exception):
    pass


def call(method, files=None, **params):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set (in .env locally, or as a GitHub secret)")
    data = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items() if v is not None}
    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=TIMEOUT)
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        # The request URL contains the token, so keep it out of the error message.
        raise TelegramError(f"{method}: {type(exc).__name__}") from None
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
