"""Telegram review: send new drafts to you, and act on your taps and replies.

  python scripts/review.py send             send drafts with status "ready", and expire old ones
  python scripts/review.py send --dry-run   save the cards in previews/ and print the captions instead
  python scripts/review.py poll             apply your taps (Post, Skip, Undo) and reply edits
  python scripts/review.py chat-id          show the chat ID of whoever messaged the bot (for setup)

Telegram holds taps and messages for 24 hours until the bot asks for them, so the
hourly poll doesn't miss any. Only the chat in TELEGRAM_CHAT_ID can act on drafts.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
from datetime import timedelta

import telegram_api
from card import PREVIEWS, make_card
from common import DRAFTS, STATE, iso, load_config, now_utc, parse_iso, read_json, write_json
from compose import hook_room, review_buttons, review_caption

HELP = (
    "Every weekday I send you a few drafts here. Tap ✅ Post or ❌ Skip under each one, "
    "or reply to a draft with new wording to replace its hook. I check for taps once an hour."
)


def refresh(d, hashtag):
    """Redraw a draft's Telegram message to match its status."""
    if not d.get("tg_message_id"):
        return
    try:
        telegram_api.edit_caption(d["tg_message_id"], review_caption(d, hashtag), review_buttons(d))
    except telegram_api.TelegramError as exc:
        print(f"Couldn't update the message for draft {d['id']}: {exc}")


def send(dry_run):
    cfg = load_config()
    hashtag = cfg["posting"].get("hashtag")
    name = cfg.get("name", "Neuro Paper Bot")
    drafts = read_json(DRAFTS, [])
    now = now_utc()

    expire_after = timedelta(days=cfg["schedule"].get("draft_expire_days", 3))
    for d in drafts:
        if d["status"] == "sent" and now - parse_iso(d["sent_at"]) > expire_after:
            print(f"Expiring draft {d['id']}: {d['title'][:70]}")
            if not dry_run:
                d["status"] = "expired"
                refresh(d, hashtag)
                write_json(DRAFTS, drafts)

    ready = [d for d in drafts if d["status"] == "ready"]
    if not ready:
        print("No new drafts to send.")
    for d in ready:
        as_sent = {**d, "status": "sent"}
        png = make_card(d, name)
        caption = review_caption(as_sent, hashtag)
        if dry_run:
            PREVIEWS.mkdir(exist_ok=True)
            (PREVIEWS / f"{d['id']}.png").write_bytes(png)
            print(f"--- draft {d['id']} (card: previews/{d['id']}.png)\n{caption}\n")
            continue
        msg = telegram_api.send_photo(png, caption, review_buttons(as_sent))
        d.update(status="sent", tg_message_id=msg["message_id"], sent_at=iso())
        write_json(DRAFTS, drafts)  # saved after each one, so a later failure can't make it send twice
        print(f"Sent draft {d['id']}: {d['title'][:70]}")


def poll():
    cfg = load_config()
    hashtag = cfg["posting"].get("hashtag")
    me = str(telegram_api.chat_id())
    drafts = read_json(DRAFTS, [])
    state = read_json(STATE, {})
    by_id = {d["id"]: d for d in drafts}
    by_message = {d["tg_message_id"]: d for d in drafts if d.get("tg_message_id")}

    updates = telegram_api.call(
        "getUpdates", offset=state.get("telegram_offset", 0), timeout=0, allowed_updates=["callback_query", "message"]
    )
    for update in updates:
        state["telegram_offset"] = update["update_id"] + 1  # asking with this offset next time confirms it
        try:
            if "callback_query" in update:
                on_tap(update["callback_query"], by_id, me, hashtag)
            elif "message" in update:
                on_message(update["message"], by_message, me, hashtag)
        except telegram_api.TelegramError as exc:
            print(f"Telegram error on update {update['update_id']}: {exc}")
    write_json(DRAFTS, drafts)
    write_json(STATE, state)
    print(f"Handled {len(updates)} Telegram update{'' if len(updates) == 1 else 's'}.")


def on_tap(tap, by_id, me, hashtag):
    if str(tap.get("message", {}).get("chat", {}).get("id")) != me:
        return  # a button in someone else's chat
    action, _, draft_id = (tap.get("data") or "").partition(":")
    d = by_id.get(draft_id)
    if not d:
        return
    if action == "post" and d["status"] == "sent":
        d.update(status="approved", approved_at=iso())
    elif action == "skip" and d["status"] == "sent":
        d["status"] = "skipped"
    elif action == "undo" and d["status"] in ("approved", "skipped"):
        d["status"] = "sent"
        d.pop("approved_at", None)
    else:
        return  # e.g. a second tap on a draft that has already been posted
    print(f"Draft {d['id']} is now {d['status']}")
    refresh(d, hashtag)


def on_message(msg, by_message, me, hashtag):
    if str(msg.get("chat", {}).get("id")) != me:
        return
    text = " ".join((msg.get("text") or "").split())
    d = by_message.get((msg.get("reply_to_message") or {}).get("message_id"))
    if not d:
        telegram_api.send_message(HELP, reply_to=msg["message_id"])
        return
    if d["status"] not in ("sent", "approved"):
        telegram_api.send_message(f"That draft is already {d['status']}, so nothing changed.", reply_to=msg["message_id"])
        return
    room = hook_room(d, hashtag)
    if not text or len(text) > room:
        telegram_api.send_message(
            f"A hook can be at most {room} characters, and yours is {len(text)}, so nothing changed.",
            reply_to=msg["message_id"],
        )
        return
    d.setdefault("original_hook", d.get("hook"))
    d.update(hook=text, edited_at=iso())
    print(f"Draft {d['id']} has a new hook")
    refresh(d, hashtag)
    telegram_api.send_message("Updated the hook.", reply_to=msg["message_id"])


def show_chat_ids():
    chats = {}
    for update in telegram_api.call("getUpdates", timeout=0):  # no offset: just looks, confirms nothing
        msg = update.get("message") or update.get("callback_query", {}).get("message") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
    if not chats:
        print("No messages yet. Send your bot a message in Telegram (for example /start), then run this again.")
    for cid, name in chats.items():
        print(f"{cid}  {name}")


def main():
    parser = argparse.ArgumentParser(description="Telegram review of drafts.")
    commands = parser.add_subparsers(dest="command", required=True)
    send_cmd = commands.add_parser("send", help="send new drafts, and expire old ones")
    send_cmd.add_argument("--dry-run", action="store_true", help="save cards to previews/ and print captions instead")
    commands.add_parser("poll", help="apply your taps and replies")
    commands.add_parser("chat-id", help="show the chat ID of whoever messaged the bot")
    args = parser.parse_args()
    try:
        if args.command == "send":
            send(args.dry_run)
        elif args.command == "poll":
            poll()
        else:
            show_chat_ids()
    except telegram_api.TelegramError as exc:
        raise SystemExit(f"Telegram: {exc}")


if __name__ == "__main__":
    main()
