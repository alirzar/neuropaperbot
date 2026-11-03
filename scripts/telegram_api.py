"""A minimal Telegram Bot API client: only the calls review.py and post.py need.

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


def chat_id():
    cid = os.getenv("TELEGRAM_CHAT_ID")
    if not cid:
        raise TelegramError("TELEGRAM_CHAT_ID is not set; run `python scripts/review.py chat-id` to find it")
    return cid


def send_photo(png, caption, buttons):
    return call(
        "sendPhoto",
        files={"photo": ("card.png", png, "image/png")},
        chat_id=chat_id(),
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


def send_message(text, reply_to=None):
    reply = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
    return call("sendMessage", chat_id=chat_id(), text=text, reply_parameters=reply)
