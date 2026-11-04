"""The public bot: one program that keeps running, answers people on Telegram and Discord, and
sends each a daily digest.

People ask to join with /start, and you (the admin, TELEGRAM_CHAT_ID) approve them with a tap.
Each picks topics, keywords, sources, a time zone and an hour, then gets the new papers that suit
them best every day, with buttons for 👍/👎, saving, a plain-language summary and a ready-to-copy
post, and a ➡️ button for the next few. They can also search recent papers, follow authors, add
journals, and paste a DOI or bioRxiv link for a summary.

  python scripts/bot.py      runs until you stop it with Ctrl+C

Everything is written once, for Telegram; discord_api.py turns it into Discord's format. Both apps
share the database and the daily paper update.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (you), GEMINI_API_KEY; DISCORD_BOT_TOKEN to be on Discord
     too, and LINKEDIN_ACCESS_TOKEN for your 💼 LinkedIn button (both optional). Everything it
     remembers is in data/db/bot.db.
"""
import html
import logging
import os
import queue
import re
import threading
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

import catalog
import digest
import discord_api
import explain
import telegram_api as tg
from card import make_card
from common import describe, gemini_client, gemini_models, load_config, name_key, now_utc, require_env
from compose import attribution, channel_post, copy_message
from generate_summaries import load_prompt as load_hook_prompt
from generate_summaries import make_hook
from lookup import find_doi, lookup
from store import Store

log = logging.getLogger("bot")

POLL_SECONDS = 25  # how long each request to Telegram waits for news
TICK_SECONDS = 20  # how often the bot checks its schedule

DEFAULTS = {  # overridden by the "bot" section of config.json
    "approval": True,
    "update_hour_utc": 6,
    "papers_per_digest": 3,
    "recent_days": 7,
    "search_days": 60,
    "keep_days": 60,
    "summaries_per_day": 10,
    "searches_per_day": 30,
    "follows_per_person": 20,
    "more_per_day": 5,
}
SETUP_STEPS = ("topics", "keywords", "sources", "journal", "tz", "hour", "confirm", "keywords_review", "journal_review")
REVIEW_STEPS = ("keywords_review", "journal_review")  # answering a question asked from the summary, in setup

ZONES = [
    ("Eastern (Toronto, New York)", "America/Toronto"),
    ("Central (Chicago)", "America/Chicago"),
    ("Mountain (Denver)", "America/Denver"),
    ("Pacific (Vancouver, LA)", "America/Vancouver"),
    ("São Paulo", "America/Sao_Paulo"),
    ("UK and Ireland", "Europe/London"),
    ("Central Europe", "Europe/Berlin"),
    ("Eastern Europe", "Europe/Athens"),
    ("India", "Asia/Kolkata"),
    ("China", "Asia/Shanghai"),
    ("Japan and Korea", "Asia/Tokyo"),
    ("Sydney", "Australia/Sydney"),
    ("UTC", "UTC"),
]
HOURS = [6, 7, 8, 9, 10, 12, 14, 17, 20]
COUNTS = [1, 3, 5, 8]

HELP = """I send you a daily digest of new neuroscience papers picked for your interests.

/digest: today's papers, now (tap ➡️ under them for more)
/find <i>topic</i>: search recent papers
/saved: papers you saved
/follow <i>name</i>: a message when an author publishes (/following, /unfollow)
/settings: what you get, and when (or straight to /topics, /keywords, /sources, /time)
/privacy: what I keep · /stop: delete it all

Paste a DOI or a bioRxiv link and I'll summarize the paper in plain language."""

PRIVACY = """What I keep about you: your Telegram or Discord name and ID; the topics, keywords, sources, time zone and hour you gave me; which papers I sent you; and your 👍/👎, saved papers and followed authors.

It's only used to pick your papers. It stays in the bot's own database and isn't shared. Journals you add are offered to everyone, without your name. /stop deletes all of it."""

SOURCES = """📚 <b>Where your papers come from</b>

Tap the sources you want (tap again to remove). Your digest starts with their papers, best match first; when those run out, the best matches from everywhere else follow. From general journals like Nature or eLife, you only get the brain papers.

Missing a journal? Tap <b>➕ Add a journal</b>."""

DISCORD_COMMANDS = [  # Discord's slash commands: (name, description, option or None); option is (name, description, required)
    ("start", "Set up your digest", None),
    ("digest", "Today's papers, now", None),
    ("find", "Search recent papers", ("topic", "What to look for, like hippocampal replay", True)),
    ("saved", "Papers you saved", None),
    ("follow", "Get a message when an author publishes", ("name", "Their first and last name", True)),
    ("following", "Authors you follow", None),
    ("unfollow", "Stop following an author", ("name", "Their name", False)),
    ("topics", "The areas you follow", None),
    ("keywords", "Your interests in your own words", ("text", "Like: hippocampal replay, sleep", False)),
    ("sources", "The journals and preprints you get", None),
    ("time", "When your digest arrives", None),
    ("tz", "Set your time zone by name", ("name", "Like Europe/Madrid", True)),
    ("settings", "Your choices, and changing them", None),
    ("summary", "A plain-language summary of a paper", ("doi", "A DOI, or a bioRxiv or medRxiv link", True)),
    ("privacy", "What the bot keeps", None),
    ("stop", "Delete your data", None),
    ("help", "What the bot can do", None),
]


class Alert(str):
    """A note on a tap that matters: Discord shows these, and skips the other notes."""


def button(text, data):
    return {"text": text, "callback_data": data}


def in_rows(buttons, per_row):
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def pair_up(items, short=19):
    """(label, data, name) items as buttons: two to a row when both names are short, else one.
    The name leaves out the ✅, so a row doesn't change shape when one is ticked."""
    rows, i = [], 0
    while i < len(items):
        if i + 1 < len(items) and len(items[i][2]) <= short and len(items[i + 1][2]) <= short:
            rows.append([button(*items[i][:2]), button(*items[i + 1][:2])])
            i += 2
        else:
            rows.append([button(*items[i][:2])])
            i += 1
    return rows


def per_month(n):
    n = round(n)
    return "fewer than one paper a month" if n < 1 else f"about {n} paper{'' if n == 1 else 's'} a month"


class Bot:
    def __init__(self, cfg, store, client, apps=None):
        self.cfg, self.store, self.client = cfg, store, client
        self.apps = apps or {"telegram": tg.App()}  # each app's way to send, by platform name
        self.platforms = {}  # each person's app, remembered
        self.settings = {**DEFAULTS, **cfg.get("bot", {})}
        self.admin = int(tg.chat_id())
        self.models = gemini_models(cfg)
        self.embedding_model = digest.embedding_model(cfg)
        self.updating = None  # the update's thread, while it runs
        self.update_tried_at = None  # kept in memory, so a restart tries again straight away
        self.pending_sources = set()  # sources someone just turned on that nobody had: fetched at the next tick
        self.found = {}  # each person's latest journal search, by button ID

    # ---------- talking to people, on their own app ----------
    def app(self, uid):
        if uid not in self.platforms:
            user = self.store.user(uid)
            if not user:
                return self.apps["telegram"]
            self.platforms[uid] = user["platform"]
        platform = self.platforms[uid]
        if platform not in self.apps:
            raise RuntimeError(f"{platform.title()} isn't connected: set its token in .env")
        return self.apps[platform]

    def say(self, uid, text, buttons=None):
        """Send a message (Telegram HTML) and return its message ID."""
        return self.app(uid).send(uid, text, buttons)

    def photo(self, uid, png, caption, buttons=None):
        return self.app(uid).photo(uid, png, caption, buttons)

    def edit(self, uid, message_id, text, buttons=None):
        self.app(uid).edit(uid, message_id, text, buttons)

    def edit_buttons(self, uid, message_id, buttons):
        self.app(uid).edit_buttons(uid, message_id, buttons)

    def typing(self, uid):
        self.app(uid).typing(uid)

    def local_now(self, user, now=None):
        return (now or now_utc()).astimezone(ZoneInfo(user["tz"] or "UTC"))

    def allowed(self, user, kind, limit):
        """Count a use of a limited feature; the admin has no limits."""
        return bool(user["is_admin"]) or self.store.use(user["id"], kind, now_utc().date().isoformat()) <= limit

    # ---------- incoming messages ----------
    def handle(self, update, platform="telegram"):
        """One thing someone did: a Telegram update, or an event from discord_api.connect."""
        if platform == "discord":
            return self.handle_discord(update)
        if "callback_query" in update:
            cq, note = update["callback_query"], None
            try:
                note = self.on_tap(cq["from"]["id"], cq.get("data") or "", (cq.get("message") or {}).get("message_id"))
            finally:
                tg.answer(cq["id"], note if isinstance(note, str) else None)
        elif "message" in update:
            msg = update["message"]
            sender = msg.get("from") or {}
            if msg.get("chat", {}).get("type") != "private" or sender.get("id") is None:
                return  # not for groups or channels
            name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or "Someone"
            self.on_text("telegram", sender["id"], (msg.get("text") or "").strip(), name, sender.get("username"))

    def handle_discord(self, event):
        uid, kind = event["uid"], event["kind"]
        if kind == "tap":
            note = None
            try:
                note = self.on_tap(uid, event["data"], event.get("message_id"))
            finally:
                if isinstance(note, Alert) and not event.get("quiet"):
                    discord_api.note(event["token"], note)
            return
        try:
            self.on_text("discord", uid, (event.get("text") or "").strip(), event.get("name"), event.get("username"))
        finally:
            if kind == "command":
                discord_api.finish(event["token"])

    def on_text(self, platform, uid, text, name, username):
        """A message, or a command, from someone in a private chat."""
        user = self.store.user(uid)
        if user is None:
            return self.join(platform, uid, name or "Someone", username)
        if user["status"] == "pending":
            return self.say(uid, "You're on the list. I'll message you as soon as the admin lets you in.")
        if user["status"] != "active":
            return
        if text.startswith("/"):
            command, _, arg = text.partition(" ")
            return self.command(user, command.split("@")[0].lower(), arg.strip())
        if user["step"] in ("keywords", "keywords_edit", "keywords_review"):
            return self.set_keywords(user, text)
        if user["step"] in ("journal", "journal_edit", "journal_review"):
            return self.find_journal(user, text)
        doi = find_doi(text)
        if doi:
            return self.summarize_doi(user, doi)
        self.say(uid, HELP)

    def join(self, platform, uid, name, username):
        is_admin = platform == "telegram" and uid == self.admin
        if is_admin or not self.settings["approval"]:
            self.store.add_user(uid, name, username, "active", is_admin=is_admin, platform=platform)
            return self.start_setup(self.store.user(uid))
        self.store.add_user(uid, name, username, "pending", platform=platform)
        self.say(
            uid,
            "Hi! I send a daily digest of new neuroscience papers picked for your interests. "
            "I've asked the admin to let you in, and I'll message you when they do.",
        )
        self.ask_admin(self.store.user(uid))

    def who(self, user):
        """'Ana (@ana_lab)', with ' on Discord' for people there."""
        handle = f" (@{html.escape(user['username'])})" if user["username"] else ""
        where = " on Discord" if user["platform"] == "discord" else ""
        return html.escape(user["name"] or str(user["id"])) + handle + where

    def ask_admin(self, user):
        who = self.who(user)
        self.say(
            self.admin,
            f"🆕 {who} wants to use the bot.",
            [[button("✅ Approve", f"ok:{user['id']}"), button("❌ Deny", f"no:{user['id']}")]],
        )

    def command(self, user, cmd, arg):
        uid = user["id"]
        if user["step"] in ("keywords_edit", "journal_edit", *REVIEW_STEPS):  # a command means they're not answering my question
            self.store.update_user(uid, step="confirm" if user["step"] in REVIEW_STEPS else None)
            user = self.store.user(uid)
        if cmd in ("/start", "/help"):
            return self.resume_setup(user) if user["step"] else self.say(uid, HELP)
        commands = {
            "/digest": lambda: self.send_digest(user, manual=True),
            "/topics": lambda: self.ask_topics(user),
            "/keywords": lambda: self.set_keywords(user, arg) if arg else self.ask_keywords(user, "keywords_edit"),
            "/sources": lambda: self.ask_sources(user),
            "/time": lambda: self.ask_tz(user),
            "/tz": lambda: self.set_tz(user, arg),
            "/settings": lambda: self.show_summary(user),
            "/find": lambda: self.find(user, arg),
            "/saved": lambda: self.show_saved(user),
            "/follow": lambda: self.follow(user, arg),
            "/unfollow": lambda: self.unfollow(user, arg),
            "/following": lambda: self.show_follows(user),
            "/summary": lambda: self.summarize_doi(user, find_doi(arg)),
            "/privacy": lambda: self.say(uid, PRIVACY),
            "/stop": lambda: self.stop(user),
            "/users": lambda: self.show_users(user),
        }
        commands.get(cmd, lambda: self.say(uid, HELP))()

    # ---------- buttons ----------
    def on_tap(self, uid, data, message_id):
        """A tapped button (or a dropdown choice on Discord); returns a short note to show, if any."""
        action, _, value = data.partition(":")
        user = self.store.user(uid)
        if action in ("ok", "no"):
            return self.decide(user, int(value), action == "ok", message_id)
        if not user or user["status"] != "active":
            return Alert("Send /start first.")
        handler = {
            "topic": lambda: self.toggle_topic(user, value, message_id),
            "topics_done": lambda: self.topics_done(user, message_id),
            "keep_keywords": lambda: self.set_keywords(user, ""),
            "src": lambda: self.toggle_source(user, value, message_id),
            "src_add": lambda: self.ask_journal(user, message_id),
            "src_pick": lambda: self.add_journal(user, value, message_id),
            "src_cancel": lambda: self.cancel_journal(user, message_id),
            "src_done": lambda: self.sources_done(user, message_id),
            "tz": lambda: self.set_tz(user, ZONES[int(value)][1]),
            "hour": lambda: self.set_hour(user, int(value)),
            "count": lambda: self.set_count(user, int(value)),
            "change": lambda: self.change(user, value, message_id),
            "go": lambda: self.start_digest(user, message_id),
            "more": lambda: self.more(user, message_id),
            "like": lambda: self.react(user, value, 1, message_id),
            "dislike": lambda: self.react(user, value, -1, message_id),
            "save": lambda: self.save(user, value, message_id),
            "sum": lambda: self.summarize_pid(user, value),
            "copy": lambda: self.copy(user, value),
            "li": lambda: self.linkedin(user, value),
        }.get(action)
        return handler() if handler else None

    def decide(self, admin, uid, approve, message_id):
        if not admin or not admin["is_admin"]:
            return None
        user = self.store.user(uid)
        if not user or user["status"] != "pending":
            return "Already decided"
        if approve:
            self.store.update_user(uid, status="active")
            self.say(uid, "You're in! 🎉")
            self.start_setup(self.store.user(uid))
        else:
            self.store.update_user(uid, status="denied")
            self.say(uid, "Sorry, the admin didn't approve your request.")
        if message_id:
            self.edit(self.admin, message_id, f"{'✅ Approved' if approve else '❌ Denied'}: {self.who(user)}")
        return "Approved" if approve else "Denied"

    # ---------- setup: topics, keywords, sources, time zone, hour, then a summary to confirm ----------
    def start_setup(self, user):
        self.store.update_user(user["id"], step="topics")
        self.say(user["id"], "Let's set up your digest. First, the areas you follow:")
        self.ask_topics(self.store.user(user["id"]))

    def resume_setup(self, user):
        back = {"journal": "sources", "keywords_review": "confirm", "journal_review": "confirm"}.get(user["step"])
        if back:
            self.store.update_user(user["id"], step=back)
            user = self.store.user(user["id"])
        ask = {
            "topics": self.ask_topics,
            "keywords": self.ask_keywords,
            "sources": self.ask_sources,
            "tz": self.ask_tz,
            "hour": self.ask_hour,
            "confirm": self.show_summary,
        }
        ask.get(user["step"], lambda u: self.say(u["id"], HELP))(user)

    def topic_buttons(self, chosen):
        buttons = [
            button(("✅ " if key in chosen else "") + label, f"topic:{key}") for key, (label, _) in self.cfg["topics"].items()
        ]
        return in_rows(buttons, 2) + [[button("Done", "topics_done")]]

    def ask_topics(self, user):
        self.say(user["id"], "Tap the areas you want (tap again to remove), then <b>Done</b>.", self.topic_buttons(user["topics"]))

    def toggle_topic(self, user, key, message_id):
        if key not in self.cfg["topics"]:
            return None
        topics = [t for t in user["topics"] if t != key] if key in user["topics"] else user["topics"] + [key]
        self.store.update_user(user["id"], topics=topics)
        if message_id:
            self.edit_buttons(user["id"], message_id, self.topic_buttons(topics))
        return None

    def topics_done(self, user, message_id):
        if message_id:
            self.edit_buttons(user["id"], message_id, [])
        if user["step"] == "topics":
            self.store.update_user(user["id"], step="keywords")
            self.ask_keywords(self.store.user(user["id"]))
        elif user["step"] == "confirm":
            self.refresh_profile(user)
            self.show_summary(self.store.user(user["id"]))
        else:
            self.refresh_profile(user)
            self.say(user["id"], "Topics saved. They count from your next digest.")
        return "Saved"

    def ask_keywords(self, user, step=None):
        """Ask for keywords; step is keywords_edit (or keywords_review) when changing them later."""
        if step:
            self.store.update_user(user["id"], step=step)
        now = f"\n\nNow: <i>{html.escape(user['keywords'])}</i>" if user["keywords"] else ""
        self.say(
            user["id"],
            "Now tell me in your own words what you work on or like to follow, for example "
            "<i>hippocampal replay, sleep, memory consolidation</i>." + now,
            [[button("Keep as is" if step else "Skip", "keep_keywords")]],
        )

    def set_keywords(self, user, text):
        uid, step = user["id"], user["step"]
        if text:
            self.store.update_user(uid, keywords=text[:500])
        if step == "keywords":  # part of setup: on to the sources
            self.store.update_user(uid, step="sources")
            self.ask_sources(self.store.user(uid))
        elif step == "keywords_review":  # changed from the summary: back to it
            self.store.update_user(uid, step="confirm")
            if text:
                self.refresh_profile(self.store.user(uid))
            self.show_summary(self.store.user(uid))
        elif step == "keywords_edit" or text:
            if step == "keywords_edit":
                self.store.update_user(uid, step=None)
            if text:
                self.refresh_profile(self.store.user(uid))
                self.say(uid, "Keywords saved.")
        return None

    def my_sources(self, user):
        """The keys of the sources this person ticked (none to start with)."""
        return list(user["sources"] or [])

    def source_buttons(self, user):
        mine = set(self.my_sources(user))
        items = [
            (("✅ " if s["key"] in mine else "") + s["name"], f"src:{s['id']}", s["name"])
            for s in catalog.sources(self.cfg, self.store)
        ]
        return pair_up(items) + [[button("➕ Add a journal", "src_add"), button("Done", "src_done")]]

    def ask_sources(self, user):
        self.say(user["id"], SOURCES, self.source_buttons(user))

    def toggle_source(self, user, sid, message_id):
        source = next((s for s in catalog.sources(self.cfg, self.store) if s["id"] == sid), None)
        if not source:
            return Alert("That source is gone")
        mine = self.my_sources(user)
        if source["key"] in mine:
            mine.remove(source["key"])
        else:
            self.fetch_soon(source["key"])
            mine.append(source["key"])
        self.store.update_user(user["id"], sources=mine)
        if message_id:
            self.edit_buttons(user["id"], message_id, self.source_buttons(self.store.user(user["id"])))
        return ("On: " if source["key"] in mine else "Off: ") + source["name"]

    def fetch_soon(self, key):
        """A source nobody had on gets its recent papers fetched now, not at the next daily update."""
        if key not in digest.wanted_sources(self.store, self.cfg):
            self.pending_sources.add(key)

    def sources_done(self, user, message_id):
        uid, n = user["id"], len(self.my_sources(user))
        if message_id:
            self.edit_buttons(uid, message_id, [])
        if not n:
            self.say(uid, "No sources picked, so your papers come from everything I follow. Pick some anytime with /sources.")
        if user["step"] == "sources":  # part of setup: on to the time zone
            self.store.update_user(uid, step="tz")
            self.ask_tz(self.store.user(uid))
        elif user["step"] == "confirm":
            self.show_summary(self.store.user(uid))
        elif n:
            self.say(uid, f"Saved: {n} source{'' if n == 1 else 's'}. They count from your next digest.")
        return "Saved"

    def ask_journal(self, user, message_id):
        if user["step"] in ("sources", "journal"):
            step = "journal"
        elif user["step"] in ("confirm", "journal_review"):
            step = "journal_review"
        elif user["step"] in (None, "keywords_edit", "journal_edit"):
            step = "journal_edit"
        else:
            return Alert("Finish setting up first")
        if message_id:
            self.edit_buttons(user["id"], message_id, [])  # the list comes back afterwards
        self.store.update_user(user["id"], step=step)
        self.say(
            user["id"],
            "Which journal? Send its name, its abbreviation (like <i>J Neurosci</i>) or its ISSN.",
            [[button("Cancel", "src_cancel")]],
        )
        return None

    def find_journal(self, user, text):
        uid = user["id"]
        cancel = [[button("Cancel", "src_cancel")]]
        if not self.allowed(user, "journal", self.settings["searches_per_day"]):
            return self.say(uid, "That's all the journal searches for today. More tomorrow!", cancel)
        self.typing(uid)
        try:
            found = catalog.find_journals(text)
        except Exception as exc:
            log.warning("Journal search for %r failed: %s", text, describe(exc))
            return self.say(uid, "PubMed didn't answer just now. Please try again in a minute.", cancel)
        if not found:
            return self.say(
                uid,
                f"I couldn't find <i>{html.escape(text[:100])}</i> in PubMed. Try its full name, its abbreviation "
                "as PubMed writes it (like <i>J Neurosci</i>) or its ISSN.",
                cancel,
            )
        self.found[uid] = {catalog.make("pubmed", j["abbr"], "")["id"]: j for j in found}
        lines = [
            f"{i}. <b>{html.escape(j['title'])}</b> ({html.escape(j['abbr'])}), {per_month(j['per_month'])}"
            for i, j in enumerate(found, 1)
        ]
        if len(found) == 1:
            buttons = [[button(f"Add {found[0]['abbr']}", f"src_pick:{next(iter(self.found[uid]))}")]]
        else:
            buttons = [[button(f"{i}. {j['abbr']}", f"src_pick:{sid}")] for i, (sid, j) in enumerate(self.found[uid].items(), 1)]
        question = "Is this the one?" if len(found) == 1 else "Which one?"
        self.say(uid, question + "\n\n" + "\n".join(lines), buttons + cancel)

    def add_journal(self, user, sid, message_id):
        uid = user["id"]
        found = self.found.get(uid, {}).get(sid)
        if not found:
            return Alert("That list is old: send the journal's name again")
        key = catalog.key_for("pubmed", found["abbr"])
        source = next((s for s in catalog.sources(self.cfg, self.store) if s["key"] == key), None)
        note = ""
        if source is None:  # new to the bot: check it, then offer it to everyone
            self.typing(uid)
            try:
                details = catalog.journal_details(found["abbr"])
            except Exception as exc:
                log.warning("Couldn't check %s: %s", found["abbr"], describe(exc))
                return Alert("PubMed didn't answer: try again in a minute")
            name = html.escape(found["title"])
            if not details:
                self.say(uid, f"PubMed has nothing from <b>{name}</b> in the past year, so I can't follow it.")
                return None
            volume = details["brain_per_month"] if details["broad"] else details["per_month"]
            if volume > catalog.MAX_PER_MONTH:
                self.say(uid, f"<b>{name}</b> publishes too much for me to follow ({per_month(volume)}).")
                return None
            name = catalog.display_name(found["title"], found["abbr"])
            source = catalog.make("pubmed", found["abbr"], name, broad=details["broad"], added=True)
            self.store.add_source(source)
            if details["broad"]:
                note = f" It covers more than neuroscience, so you'll only get its brain papers ({per_month(volume)})."
        if message_id:
            self.edit_buttons(uid, message_id, [])
        mine = self.my_sources(user)
        if key in mine:
            text = f"You already get <b>{html.escape(source['name'])}</b>."
        else:
            self.fetch_soon(key)
            self.store.update_user(uid, sources=mine + [key])
            text = f"Added <b>{html.escape(source['name'])}</b>.{note} Its papers count from your next digest."
        self.found.pop(uid, None)
        self.leave_journal_step(user)
        self.say(uid, text)
        self.ask_sources(self.store.user(uid))
        return "Added"

    def cancel_journal(self, user, message_id):
        if message_id:
            self.edit_buttons(user["id"], message_id, [])
        self.found.pop(user["id"], None)
        self.leave_journal_step(user)
        self.ask_sources(self.store.user(user["id"]))
        return "Cancelled"

    def leave_journal_step(self, user):
        back = {"journal": "sources", "journal_edit": None, "journal_review": "confirm"}
        if user["step"] in back:
            self.store.update_user(user["id"], step=back[user["step"]])

    def ask_tz(self, user):
        buttons = [button(label, f"tz:{i}") for i, (label, _) in enumerate(ZONES)]
        self.say(
            user["id"],
            "Which time zone are you in? If yours isn't listed, send <code>/tz</code> and its name, "
            "like <code>/tz Europe/Madrid</code>.",
            in_rows(buttons, 2),
        )

    def set_tz(self, user, name):
        uid = user["id"]
        try:
            ZoneInfo(name)
        except Exception:
            self.say(uid, "I don't know that time zone. Use a name like <code>Europe/Madrid</code> or <code>America/Toronto</code>.")
            return "Unknown time zone"
        self.store.update_user(uid, tz=name)
        if user["step"] == "tz":
            self.store.update_user(uid, step="hour")
        self.ask_hour(self.store.user(uid))
        return None

    def ask_hour(self, user):
        buttons = [button(f"{h}:00", f"hour:{h}") for h in HOURS]
        self.say(user["id"], f"What time should your digest arrive? ({html.escape(user['tz'] or 'UTC')} time)", in_rows(buttons, 3))

    def set_hour(self, user, hour):
        uid = user["id"]
        self.store.update_user(uid, hour=hour)
        if user["step"] == "hour":  # the last question: now the summary, to confirm
            self.store.update_user(uid, step="confirm")
            self.refresh_profile(self.store.user(uid))
            self.show_summary(self.store.user(uid))
        elif user["step"] == "confirm":
            self.show_summary(self.store.user(uid))
        else:
            self.say(uid, f"Done: your digest now comes at {hour}:00 ({html.escape(user['tz'] or 'UTC')} time).")
        return "Saved"

    def ask_count(self, user):
        now = user["per_digest"] or self.settings["papers_per_digest"]
        self.say(user["id"], f"How many papers per digest? (now {now})", [[button(str(n), f"count:{n}") for n in COUNTS]])

    def set_count(self, user, n):
        self.store.update_user(user["id"], per_digest=n)
        if user["step"] == "confirm":
            self.show_summary(self.store.user(user["id"]))
        else:
            self.say(user["id"], f"Done: {n} paper{'' if n == 1 else 's'} per digest.")
        return "Saved"

    # ---------- the summary of someone's choices ----------
    def summary(self, user):
        """What this person chose, as lines of a message."""
        topics = [self.cfg["topics"][key][0] for key in user["topics"] if key in self.cfg["topics"]]
        names = {s["key"]: s["name"] for s in catalog.sources(self.cfg, self.store)}
        sources = [names.get(key, key) for key in self.my_sources(user)]
        zone = next((label for label, name in ZONES if name == user["tz"]), user["tz"] or "UTC")
        when = f"every day at {user['hour']}:00, {zone} time" if user["hour"] is not None else "not set yet"
        n = user["per_digest"] or self.settings["papers_per_digest"]
        return "\n".join([
            "📋 <b>Your digest</b>",
            "",
            "<b>Areas:</b> " + (html.escape(", ".join(topics)) if topics else "none"),
            "<b>In your words:</b> " + (html.escape(user["keywords"]) if user["keywords"] else "nothing yet"),
            "<b>Sources:</b> " + (html.escape(", ".join(sources)) + " (these come first)" if sources
                                  else "none picked, so papers come from everything I follow"),
            f"<b>When:</b> {html.escape(when)}",
            f"<b>Papers each time:</b> {n}",
        ])

    def show_summary(self, user):
        """The summary with a button to change each part; at the end of setup, also to start."""
        changes = [
            [button("Areas", "change:topics"), button("Keywords", "change:keywords"), button("Sources", "change:sources")],
            [button("Time", "change:time"), button("Papers each time", "change:count")],
        ]
        if user["step"] == "confirm":
            text = self.summary(user) + "\n\nDoes this look right? Start your digest, or change something first."
            self.say(user["id"], text, [[button("✅ Start my digest", "go")]] + changes)
        else:
            self.say(user["id"], self.summary(user) + "\n\nTap one to change it.", changes)

    def change(self, user, what, message_id):
        """A change button under the summary."""
        if user["step"] not in (None, "confirm", "keywords_edit", "journal_edit"):
            return Alert("Finish setting up first")
        if message_id:
            self.edit_buttons(user["id"], message_id, [])  # a fresh summary comes after the change
        if what == "topics":
            self.ask_topics(user)
        elif what == "keywords":
            self.ask_keywords(user, "keywords_review" if user["step"] == "confirm" else "keywords_edit")
        elif what == "sources":
            self.ask_sources(user)
        elif what == "time":
            self.ask_tz(user)
        elif what == "count":
            self.ask_count(user)
        return None

    def start_digest(self, user, message_id):
        """✅ Start my digest: the end of setup, and the first papers."""
        uid = user["id"]
        if user["step"] != "confirm":
            return "Already started"
        if message_id:
            self.edit_buttons(uid, message_id, [])
        self.store.update_user(uid, step=None)
        self.say(uid, f"All set! Your digest comes every day at {user['hour']}:00. Here are today's papers:")
        self.send_digest(self.store.user(uid), manual=True)
        self.say(uid, "Tip: /settings shows your choices, and /help lists everything I can do.")
        return "Started"

    def refresh_profile(self, user):
        """Fingerprints for each chosen topic and keyword phrase; a paper matching any of them scores well."""
        texts = [self.cfg["topics"][key][1] for key in user["topics"] if key in self.cfg["topics"]]
        texts += [part.strip() for part in re.split(r"[,;\n]", user["keywords"] or "") if part.strip()][:10]
        profile = digest.query_vectors(self.store, self.client, self.embedding_model, texts) if texts else None
        self.store.update_user(user["id"], profile=profile)

    # ---------- papers ----------
    def send_digest(self, user, manual=False, now=None):
        uid = user["id"]
        if not self.store.get("last_update"):
            if manual:
                self.say(uid, "I'm still collecting the latest papers. Your first digest is on its way soon.")
            return
        local = self.local_now(user, now)
        self.store.update_user(uid, last_digest=local.date().isoformat())
        if not self.send_papers(user, "digest", f"☀️ <b>Your papers for {local:%A}, {local.day} {local:%B}</b>"):
            self.say(uid, "No new papers for you today. Try /find, or add /topics or /sources.")

    def send_papers(self, user, kind, header=None):
        """This person's next best papers, then a ➡️ button if more are waiting. Returns how many it sent."""
        n = user["per_digest"] or self.settings["papers_per_digest"]
        papers = digest.pick(self.store, user, n + 1, self.settings["recent_days"], self.my_sources(user))
        if papers and header:
            self.say(user["id"], header)
        for p in papers[:n]:
            self.send_card(user, p, kind)
        if len(papers) > n:
            self.say(user["id"], "Want more?", [[button(f"➡️ {n} more", "more")]])
        return len(papers[:n])

    def more(self, user, message_id):
        uid = user["id"]
        if not self.allowed(user, "more", self.settings["more_per_day"]):
            if message_id:
                self.edit(uid, message_id, "That's all for today. More tomorrow!")
            return "That's all for today"
        n = user["per_digest"] or self.settings["papers_per_digest"]
        if message_id:
            self.edit(uid, message_id, f"➡️ {n} more:")  # the button goes, and the message heads the next papers
        if not self.send_papers(user, "more"):
            self.say(uid, "That's every new paper from the past week. More tomorrow!")
        return None

    def with_hook(self, p):
        """The paper, with its hook written (and saved) if it doesn't have one yet."""
        if not p.get("hook") and p.get("abstract"):
            max_chars = self.cfg["posting"].get("hook_max_chars", 200)
            try:
                p["hook"] = make_hook(self.client, self.models, load_hook_prompt(max_chars), p, max_chars)
                self.store.set_paper(p["pid"], hook=p["hook"])
            except Exception as exc:
                log.warning("No hook for %s: %s", p["doi"], describe(exc))
        return p

    def paper_buttons(self, user, pid):
        row = self.store.sent_row(user["id"], pid) or {"feedback": 0, "saved": 0}
        buttons = [
            [
                button("👍" + (" ✓" if row["feedback"] > 0 else ""), f"like:{pid}"),
                button("👎" + (" ✓" if row["feedback"] < 0 else ""), f"dislike:{pid}"),
                button("💾 Saved" if row["saved"] else "💾 Save", f"save:{pid}"),
            ],
            [button("📄 Summary", f"sum:{pid}"), button("📋 Copy", f"copy:{pid}")],
        ]
        if user["is_admin"] and os.getenv("LINKEDIN_ACCESS_TOKEN"):
            buttons[1].append(button("💼 LinkedIn", f"li:{pid}"))
        return buttons

    def send_card(self, user, p, kind):
        """A paper as its card image, with the hook, title, authors and link, and the buttons."""
        p = self.with_hook(p)
        png = make_card(p, self.cfg.get("name", "Neuro Paper Bot"))
        message_id = self.photo(user["id"], png, channel_post(p, None), self.paper_buttons(user, p["pid"]))
        self.store.record_sent(user["id"], p["pid"], kind, message_id)

    def send_compact(self, user, p, kind):
        """A paper as a short text message with the same buttons, for search results and alerts."""
        date = f" · {p['date']}" if p.get("date") else ""
        text = f"<b>{html.escape(p['title'])}</b>\n{html.escape(attribution(p))}{date}\n{html.escape(p.get('link') or '')}"
        message_id = self.say(user["id"], text, self.paper_buttons(user, p["pid"]))
        self.store.record_sent(user["id"], p["pid"], kind, message_id)

    def react(self, user, pid, value, message_id):
        uid = user["id"]
        if not self.store.paper(pid):
            return Alert("That paper is gone")
        self.store.record_sent(uid, pid, "other", message_id)
        value = 0 if self.store.sent_row(uid, pid)["feedback"] == value else value  # a second tap takes it back
        self.store.set_feedback(uid, pid, value)
        if message_id:
            self.edit_buttons(uid, message_id, self.paper_buttons(user, pid))
        return {1: "More like this", -1: "Fewer like this", 0: "Taken back"}[value]

    def save(self, user, pid, message_id):
        uid = user["id"]
        if not self.store.paper(pid):
            return Alert("That paper is gone")
        self.store.record_sent(uid, pid, "other", message_id)
        saved = self.store.toggle_saved(uid, pid)
        if message_id:
            self.edit_buttons(uid, message_id, self.paper_buttons(user, pid))
        return "Saved: see /saved" if saved else "Removed from saved"

    def show_saved(self, user):
        papers = self.store.saved_papers(user["id"], 15)
        if not papers:
            return self.say(user["id"], "Nothing saved yet. Tap 💾 under any paper to keep it here.")
        lines = [
            f"{i}. <a href=\"{html.escape(p['link'] or '')}\">{html.escape(p['title'][:110])}</a>\n    {html.escape(attribution(p))}"
            for i, p in enumerate(papers, 1)
        ]
        self.say(user["id"], "💾 <b>Your saved papers</b>, newest first:\n\n" + "\n\n".join(lines))

    def copy(self, user, pid):
        p = self.store.paper(pid)
        if not p:
            return Alert("That paper is gone")
        self.say(user["id"], copy_message(self.with_hook(p), self.cfg["posting"].get("hashtag")))
        return None

    def linkedin(self, user, pid):
        if not user["is_admin"]:
            return None
        p = self.store.paper(pid)
        if not p:
            return Alert("That paper is gone")
        from post import post_linkedin  # only the admin's button needs it

        try:
            p = self.with_hook(p)
            result = post_linkedin(p, self.cfg, make_card(p, self.cfg.get("name", "Neuro Paper Bot")))
        except (Exception, SystemExit) as exc:
            self.say(user["id"], f"LinkedIn didn't take it: {html.escape(str(exc)[:200])}")
            return "Failed"
        self.say(user["id"], f"💼 Posted to LinkedIn: {html.escape(result.get('url') or '')}")
        return "Posted"

    # ---------- summaries ----------
    def summarize_doi(self, user, doi):
        uid = user["id"]
        if not doi:
            return self.say(uid, "Send /summary with a DOI or a bioRxiv link, or just paste one.")
        paper = self.store.paper_by_doi(doi)
        if not paper:
            self.typing(uid)
            journals = {s["query"]: s["name"] for s in catalog.sources(self.cfg, self.store) if s["kind"] == "pubmed"}
            found = lookup(doi, journals)
            if not found:
                return self.say(uid, f"I couldn't find a paper with the DOI <code>{html.escape(doi)}</code>.")
            self.store.add_paper(found, hidden=True)  # summarized on request: not one of the week's new papers
            paper = self.store.paper_by_doi(found["doi"])
        return self.summarize(user, paper)

    def summarize_pid(self, user, pid):
        paper = self.store.paper(pid)
        return self.summarize(user, paper) if paper else "That paper is gone"

    def summarize(self, user, p):
        uid = user["id"]
        if not p.get("abstract"):
            self.say(uid, f"I found <b>{html.escape(p['title'])}</b>, but not its abstract, so I can't summarize it.\n{html.escape(p.get('link') or '')}")
            return None
        text = p.get("summary")
        if not text:
            if not self.allowed(user, "summary", self.settings["summaries_per_day"]):
                self.say(uid, "That's all the summaries for today. More tomorrow!")
                return "Daily limit reached"
            self.typing(uid)
            try:
                text = explain.render(p, explain.explain(self.client, self.models, p))
            except Exception as exc:
                log.warning("Summary of %s failed: %s", p["doi"], describe(exc))
                self.say(uid, "Sorry, I couldn't write that summary just now. Please try again in a few minutes.")
                return "Try again later"
            self.store.set_paper(p["pid"], summary=text)
        message_id = self.say(uid, text, self.paper_buttons(user, p["pid"]))
        self.store.record_sent(uid, p["pid"], "summary", message_id)
        return None

    # ---------- search ----------
    def find(self, user, query):
        uid = user["id"]
        if not query:
            return self.say(uid, "Send /find and what you're looking for, like <code>/find hippocampal replay</code>.")
        if not self.allowed(user, "search", self.settings["searches_per_day"]):
            return self.say(uid, "That's all the searches for today. More tomorrow!")
        self.typing(uid)
        days = self.settings["search_days"]
        results = digest.search(self.store, self.client, self.embedding_model, query, days, 5)
        if not results:
            return self.say(uid, "I don't have any papers yet. Try again after the next daily update.")
        self.say(uid, f"🔎 The closest matches to <i>{html.escape(query)}</i> from the last {days} days:")
        for p in results:
            self.send_compact(user, p, "search")

    # ---------- author alerts ----------
    def follow(self, user, name):
        uid, key = user["id"], name_key(name)
        if " " not in key:
            return self.say(uid, "Send /follow with a first and last name, like <code>/follow Jane Smith</code>.")
        limit = self.settings["follows_per_person"]
        if key not in {f["author_key"] for f in self.store.follows(uid)} and len(self.store.follows(uid)) >= limit:
            return self.say(uid, f"You can follow up to {limit} authors. /unfollow someone first.")
        self.store.follow(uid, key, name.strip())
        last, initial = key.rsplit(" ", 1)
        self.say(
            uid,
            f"Following {html.escape(name.strip())}. I'll message you when a new paper by <b>{html.escape(last.title())} "
            f"{initial.upper()}.</b> appears. That's anyone with that last name and first initial, so the odd one may "
            "be someone else's.",
        )

    def unfollow(self, user, name):
        if not name:
            return self.show_follows(user)
        if self.store.unfollow(user["id"], name_key(name)):
            self.say(user["id"], f"Stopped following {html.escape(name.strip())}.")
        else:
            self.say(user["id"], f"You weren't following {html.escape(name.strip())}. /following lists who you follow.")

    def show_follows(self, user):
        follows = self.store.follows(user["id"])
        if not follows:
            return self.say(user["id"], "You're not following anyone yet. Try <code>/follow Jane Smith</code>.")
        names = "\n".join(f"• {html.escape(f['label'])}" for f in follows)
        self.say(user["id"], f"You follow:\n{names}\n\n/unfollow <i>name</i> to stop.")

    def send_alert(self, uid, pid, label):
        """Called by the daily update for each new paper by someone this person follows."""
        user, p = self.store.user(uid), self.store.paper(pid)
        if not user or user["status"] != "active" or not p or pid in self.store.sent_pids(uid):
            return
        try:
            self.say(uid, f"👤 A new paper by someone you follow ({html.escape(label)}):")
            self.send_compact(user, p, "alert")
        except Exception as exc:  # one person who can't be reached mustn't stop the update
            unreachable(user, exc, "alert")

    # ---------- account ----------
    def stop(self, user):
        self.say(user["id"], "Done: I've deleted everything I had about you. If you want to come back, send /start.")
        self.store.delete_user(user["id"])

    def show_users(self, user):
        if not user["is_admin"]:
            return self.say(user["id"], HELP)
        people = self.store.users()
        active = [u for u in people if u["status"] == "active"]
        pending = [u for u in people if u["status"] == "pending"]
        names = [f"• {self.who(u)}" for u in active[:50]]
        self.say(user["id"], "\n".join([f"<b>{len(active)} active</b>, {len(pending)} waiting for you."] + names))
        for u in pending:
            self.ask_admin(u)

    # ---------- the schedule ----------
    def tick(self, now=None):
        """Start the daily update when it's due, and send each person's digest at their hour."""
        now = now or now_utc()
        self.maybe_update(now)
        if not self.store.get("last_update"):
            return
        for user in self.store.users("active"):
            if user["step"] in SETUP_STEPS or user["tz"] is None or user["hour"] is None:
                continue  # still setting up
            local = self.local_now(user, now)
            if local.hour >= user["hour"] and user["last_digest"] != local.date().isoformat():
                try:
                    self.send_digest(user, now=now)
                except Exception as exc:
                    unreachable(user, exc, "digest")

    def maybe_update(self, now):
        if self.updating and self.updating.is_alive():
            return
        last = self.store.get("last_update")
        if last == now.date().isoformat() or (last and now.hour < self.settings["update_hour_utc"]):
            # Done today, or not time yet (the very first update runs right away). Meanwhile, a source
            # someone just turned on gets its papers now.
            if self.pending_sources and last:
                only, self.pending_sources = set(self.pending_sources), set()
                self.start_update(only)
            return
        if self.update_tried_at and now - self.update_tried_at < timedelta(minutes=30):
            return  # the last try failed recently: wait a little
        self.update_tried_at = now
        self.pending_sources.clear()  # the daily update fetches them too
        self.start_update(None)

    def start_update(self, only):
        self.updating = threading.Thread(target=self.run_update, args=(only,), name="update", daemon=True)
        self.updating.start()

    def run_update(self, only=None):
        try:
            digest.daily_update(self.store, self.cfg, self.client, alert=self.send_alert, only=only)
        except Exception:
            log.exception("The update failed%s", "" if only else "; it'll try again in 30 minutes")


def unreachable(user, exc, what):
    """Log a message that couldn't be delivered: briefly when the person blocked the bot or left its
    Discord server, in full otherwise."""
    gone = getattr(exc, "code", None) == discord_api.CANT_MESSAGE or "blocked by the user" in str(exc)
    if gone:
        log.warning("Couldn't send the %s to %s: they blocked the bot or left its server", what, user["id"])
    else:
        log.exception("The %s for %s failed", what, user["id"])


def poll_telegram(store, events):
    """Fetch what people do on Telegram, for ever, and put it on the `events` queue."""
    offset = int(store.get("telegram_offset", 0))
    while True:
        try:
            updates = tg.call("getUpdates", offset=offset, timeout=POLL_SECONDS, allowed_updates=["message", "callback_query"])
        except Exception as exc:
            log.warning("Telegram: %s", exc)  # "Conflict" means another copy of the bot is running
            time.sleep(5)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            events.put(("telegram", update))
        if updates:
            store.put("telegram_offset", offset)


def start_discord(events):
    """Connect to Discord, if DISCORD_BOT_TOKEN is set; returns whether it is."""
    if not os.getenv("DISCORD_BOT_TOKEN"):
        return False
    logging.getLogger("discord").setLevel(logging.WARNING)
    try:
        if discord_api.register_commands(DISCORD_COMMANDS):
            log.info("Discord: slash commands updated")
        log.info("Discord: add the bot to a server with %s", discord_api.invite_link())
    except discord_api.DiscordError as exc:
        log.warning("Discord: %s", exc)
    discord_api.connect(events)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for chatty in ("httpx", "google_genai"):  # they log every request
        logging.getLogger(chatty).setLevel(logging.WARNING)
    require_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY")
    events = queue.Queue()  # (platform, event) from both apps, handled one at a time
    apps = {"telegram": tg.App()}
    if start_discord(events):
        apps["discord"] = discord_api.App()
    bot = Bot(load_config(), Store(), gemini_client(), apps)
    threading.Thread(target=poll_telegram, args=(bot.store, events), name="telegram", daemon=True).start()
    log.info("The bot is running on %s. Press Ctrl+C to stop it.", " and ".join(a.name for a in apps.values()))
    last_tick = 0.0
    try:
        while True:
            try:
                platform, event = events.get(timeout=TICK_SECONDS)
            except queue.Empty:
                platform = None
            if platform:
                try:
                    bot.handle(event, platform)
                except (Exception, SystemExit):
                    log.exception("Couldn't handle a %s event", platform)
            if time.monotonic() - last_tick >= TICK_SECONDS:
                last_tick = time.monotonic()
                try:
                    bot.tick()
                except (Exception, SystemExit):
                    log.exception("Scheduled work failed")
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
