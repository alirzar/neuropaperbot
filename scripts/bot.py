"""The public bot: one program that keeps running, answers people, and sends each a daily digest.

People ask to join with /start, and you (the admin, TELEGRAM_CHAT_ID) approve them with a tap.
Each picks topics, keywords, a time zone and an hour, then gets the new papers that suit them
best every day, with buttons for 👍/👎, saving, a plain-language summary and a ready-to-copy post.
They can also search recent papers, follow authors, and paste a DOI or bioRxiv link for a summary.

  python scripts/bot.py      runs until you stop it with Ctrl+C

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (you), GEMINI_API_KEY; LINKEDIN_ACCESS_TOKEN for your
     💼 LinkedIn button (optional). Everything it remembers is in data/bot.db.
"""
import html
import logging
import os
import re
import threading
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

import digest
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

POLL_SECONDS = 25  # how long each request to Telegram waits for news before the bot checks its schedule

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
}

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

/digest: today's papers, now
/find <i>topic</i>: search recent papers
/saved: papers you saved
/follow <i>name</i>: a message when an author publishes (/following, /unfollow)
/topics, /keywords, /time, /settings: what you get, and when
/privacy: what I keep · /stop: delete it all

Paste a DOI or a bioRxiv link and I'll summarize the paper in plain language."""

PRIVACY = """What I keep about you: your Telegram name and ID; the topics, keywords, time zone and hour you gave me; which papers I sent you; and your 👍/👎, saved papers and followed authors.

It's only used to pick your papers. It stays in the bot's own database and isn't shared. /stop deletes all of it."""


def button(text, data):
    return {"text": text, "callback_data": data}


def in_rows(buttons, per_row):
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


class Bot:
    def __init__(self, cfg, store, client):
        self.cfg, self.store, self.client = cfg, store, client
        self.settings = {**DEFAULTS, **cfg.get("bot", {})}
        self.admin = int(tg.chat_id())
        self.models = gemini_models(cfg)
        self.embedding_model = digest.embedding_model(cfg)
        self.updating = None  # the daily update's thread, while it runs
        self.update_tried_at = None  # kept in memory, so a restart tries again straight away

    def say(self, uid, text, buttons=None):
        return tg.send(uid, text, buttons)

    def local_now(self, user, now=None):
        return (now or now_utc()).astimezone(ZoneInfo(user["tz"] or "UTC"))

    def allowed(self, user, kind, limit):
        """Count a use of a limited feature; the admin has no limits."""
        return bool(user["is_admin"]) or self.store.use(user["id"], kind, now_utc().date().isoformat()) <= limit

    # ---------- incoming messages ----------
    def handle(self, update):
        if "callback_query" in update:
            self.on_button(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])

    def on_message(self, msg):
        if msg.get("chat", {}).get("type") != "private":
            return  # not for groups or channels
        sender = msg.get("from") or {}
        uid, text = sender.get("id"), (msg.get("text") or "").strip()
        if uid is None:
            return
        user = self.store.user(uid)
        if user is None:
            return self.join(uid, sender)
        if user["status"] == "pending":
            return self.say(uid, "You're on the list. I'll message you as soon as the admin lets you in.")
        if user["status"] != "active":
            return
        if text.startswith("/"):
            command, _, arg = text.partition(" ")
            return self.command(user, command.split("@")[0].lower(), arg.strip())
        if user["step"] in ("keywords", "keywords_edit"):
            return self.set_keywords(user, text)
        doi = find_doi(text)
        if doi:
            return self.summarize_doi(user, doi)
        self.say(uid, HELP)

    def join(self, uid, sender):
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or "Someone"
        username = sender.get("username")
        if uid == self.admin or not self.settings["approval"]:
            self.store.add_user(uid, name, username, "active", is_admin=uid == self.admin)
            return self.start_setup(self.store.user(uid))
        self.store.add_user(uid, name, username, "pending")
        self.say(
            uid,
            "Hi! I send a daily digest of new neuroscience papers picked for your interests. "
            "I've asked the admin to let you in, and I'll message you when they do.",
        )
        self.ask_admin(self.store.user(uid))

    def ask_admin(self, user):
        who = html.escape(user["name"] or str(user["id"])) + (f" (@{html.escape(user['username'])})" if user["username"] else "")
        self.say(
            self.admin,
            f"🆕 {who} wants to use the bot.",
            [[button("✅ Approve", f"ok:{user['id']}"), button("❌ Deny", f"no:{user['id']}")]],
        )

    def command(self, user, cmd, arg):
        uid = user["id"]
        if cmd in ("/start", "/help"):
            return self.resume_setup(user) if user["step"] else self.say(uid, HELP)
        commands = {
            "/digest": lambda: self.send_digest(user, manual=True),
            "/topics": lambda: self.ask_topics(user),
            "/keywords": lambda: self.set_keywords(user, arg) if arg else self.ask_keywords(user, editing=True),
            "/time": lambda: self.ask_tz(user),
            "/tz": lambda: self.set_tz(user, arg),
            "/settings": lambda: self.ask_count(user),
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
    def on_button(self, cq):
        uid = cq["from"]["id"]
        action, _, value = (cq.get("data") or "").partition(":")
        message_id = (cq.get("message") or {}).get("message_id")
        user = self.store.user(uid)
        note = None
        try:
            if action in ("ok", "no"):
                note = self.decide(user, int(value), action == "ok", message_id)
            elif not user or user["status"] != "active":
                note = "Send /start first."
            else:
                handler = {
                    "topic": lambda: self.toggle_topic(user, value, message_id),
                    "topics_done": lambda: self.topics_done(user, message_id),
                    "keep_keywords": lambda: self.set_keywords(user, ""),
                    "tz": lambda: self.set_tz(user, ZONES[int(value)][1]),
                    "hour": lambda: self.set_hour(user, int(value)),
                    "count": lambda: self.set_count(user, int(value)),
                    "like": lambda: self.react(user, value, 1, message_id),
                    "dislike": lambda: self.react(user, value, -1, message_id),
                    "save": lambda: self.save(user, value, message_id),
                    "sum": lambda: self.summarize_pid(user, value),
                    "copy": lambda: self.copy(user, value),
                    "li": lambda: self.linkedin(user, value),
                }.get(action)
                note = handler() if handler else None
        finally:
            tg.answer(cq["id"], note if isinstance(note, str) else None)

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
            tg.edit(self.admin, message_id, f"{'✅ Approved' if approve else '❌ Denied'}: {html.escape(user['name'] or str(uid))}")
        return "Approved" if approve else "Denied"

    # ---------- setup: topics, keywords, time zone, hour ----------
    def start_setup(self, user):
        self.store.update_user(user["id"], step="topics")
        self.say(user["id"], "Let's set up your digest. First, the areas you follow:")
        self.ask_topics(self.store.user(user["id"]))

    def resume_setup(self, user):
        ask = {"topics": self.ask_topics, "keywords": self.ask_keywords, "tz": self.ask_tz, "hour": self.ask_hour}
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
            tg.edit_markup(user["id"], message_id, self.topic_buttons(topics))
        return None

    def topics_done(self, user, message_id):
        if message_id:
            tg.edit_markup(user["id"], message_id, [])
        if user["step"] == "topics":
            self.store.update_user(user["id"], step="keywords")
            self.ask_keywords(self.store.user(user["id"]))
        else:
            self.refresh_profile(user)
            self.say(user["id"], "Topics saved. They count from your next digest.")
        return "Saved"

    def ask_keywords(self, user, editing=False):
        if editing:
            self.store.update_user(user["id"], step="keywords_edit")
        now = f"\n\nNow: <i>{html.escape(user['keywords'])}</i>" if user["keywords"] else ""
        self.say(
            user["id"],
            "Now tell me in your own words what you work on or like to follow, for example "
            "<i>hippocampal replay, sleep, memory consolidation</i>." + now,
            [[button("Keep as is" if editing else "Skip", "keep_keywords")]],
        )

    def set_keywords(self, user, text):
        uid, step = user["id"], user["step"]
        if text:
            self.store.update_user(uid, keywords=text[:500])
        if step == "keywords":  # part of setup: on to the time zone
            self.store.update_user(uid, step="tz")
            self.ask_tz(self.store.user(uid))
        elif step == "keywords_edit" or text:
            if step == "keywords_edit":
                self.store.update_user(uid, step=None)
            if text:
                self.refresh_profile(self.store.user(uid))
                self.say(uid, "Keywords saved.")
        return None

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
        if user["step"] == "hour":  # the end of setup
            self.store.update_user(uid, step=None)
            self.refresh_profile(self.store.user(uid))
            self.say(uid, f"All set! Your digest comes every day at {hour}:00. Here are today's papers:")
            self.send_digest(self.store.user(uid), manual=True)
            self.say(uid, "Tip: /help lists everything I can do.")
        else:
            self.say(uid, f"Done: your digest now comes at {hour}:00 ({html.escape(user['tz'] or 'UTC')} time).")
        return "Saved"

    def ask_count(self, user):
        now = user["per_digest"] or self.settings["papers_per_digest"]
        self.say(user["id"], f"How many papers per digest? (now {now})", [[button(str(n), f"count:{n}") for n in COUNTS]])

    def set_count(self, user, n):
        self.store.update_user(user["id"], per_digest=n)
        self.say(user["id"], f"Done: {n} paper{'' if n == 1 else 's'} per digest.")
        return "Saved"

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
        n = user["per_digest"] or self.settings["papers_per_digest"]
        papers = digest.pick(self.store, user, n, self.settings["recent_days"])
        if not papers:
            self.say(uid, "No new papers for you today. Try /find, or add /topics.")
            return
        self.say(uid, f"☀️ <b>Your papers for {local:%A}, {local.day} {local:%B}</b>")
        for p in papers:
            self.send_card(user, p, "digest")

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
        msg = tg.photo(user["id"], png, channel_post(p, None), self.paper_buttons(user, p["pid"]))
        self.store.record_sent(user["id"], p["pid"], kind, msg["message_id"])

    def send_compact(self, user, p, kind):
        """A paper as a short text message with the same buttons, for search results and alerts."""
        date = f" · {p['date']}" if p.get("date") else ""
        text = f"<b>{html.escape(p['title'])}</b>\n{html.escape(attribution(p))}{date}\n{html.escape(p.get('link') or '')}"
        msg = self.say(user["id"], text, self.paper_buttons(user, p["pid"]))
        self.store.record_sent(user["id"], p["pid"], kind, msg["message_id"])

    def react(self, user, pid, value, message_id):
        uid = user["id"]
        if not self.store.paper(pid):
            return "That paper is gone"
        self.store.record_sent(uid, pid, "other", message_id)
        value = 0 if self.store.sent_row(uid, pid)["feedback"] == value else value  # a second tap takes it back
        self.store.set_feedback(uid, pid, value)
        if message_id:
            tg.edit_markup(uid, message_id, self.paper_buttons(user, pid))
        return {1: "More like this", -1: "Fewer like this", 0: "Taken back"}[value]

    def save(self, user, pid, message_id):
        uid = user["id"]
        if not self.store.paper(pid):
            return "That paper is gone"
        self.store.record_sent(uid, pid, "other", message_id)
        saved = self.store.toggle_saved(uid, pid)
        if message_id:
            tg.edit_markup(uid, message_id, self.paper_buttons(user, pid))
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
            return "That paper is gone"
        self.say(user["id"], copy_message(self.with_hook(p), self.cfg["posting"].get("hashtag")))
        return None

    def linkedin(self, user, pid):
        if not user["is_admin"]:
            return None
        p = self.store.paper(pid)
        if not p:
            return "That paper is gone"
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
            tg.typing(uid)
            found = lookup(doi, self.cfg["sources"].get("pubmed", {}).get("journals", {}))
            if not found:
                return self.say(uid, f"I couldn't find a paper with the DOI <code>{html.escape(doi)}</code>.")
            self.store.add_paper(found)
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
            tg.typing(uid)
            try:
                text = explain.render(p, explain.explain(self.client, self.models, p))
            except Exception as exc:
                log.warning("Summary of %s failed: %s", p["doi"], describe(exc))
                self.say(uid, "Sorry, I couldn't write that summary just now. Please try again in a few minutes.")
                return "Try again later"
            self.store.set_paper(p["pid"], summary=text)
        msg = self.say(uid, text, self.paper_buttons(user, p["pid"]))
        self.store.record_sent(uid, p["pid"], "summary", msg["message_id"])
        return None

    # ---------- search ----------
    def find(self, user, query):
        uid = user["id"]
        if not query:
            return self.say(uid, "Send /find and what you're looking for, like <code>/find hippocampal replay</code>.")
        if not self.allowed(user, "search", self.settings["searches_per_day"]):
            return self.say(uid, "That's all the searches for today. More tomorrow!")
        tg.typing(uid)
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
        self.say(uid, f"👤 A new paper by someone you follow ({html.escape(label)}):")
        self.send_compact(user, p, "alert")

    # ---------- account ----------
    def stop(self, user):
        self.store.delete_user(user["id"])
        self.say(user["id"], "Done: I've deleted everything I had about you. If you want to come back, send /start.")

    def show_users(self, user):
        if not user["is_admin"]:
            return self.say(user["id"], HELP)
        people = self.store.users()
        active = [u for u in people if u["status"] == "active"]
        pending = [u for u in people if u["status"] == "pending"]
        names = [
            f"• {html.escape(u['name'] or str(u['id']))}" + (f" (@{html.escape(u['username'])})" if u["username"] else "")
            for u in active[:50]
        ]
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
            if user["step"] or user["tz"] is None or user["hour"] is None:
                continue  # still setting up
            local = self.local_now(user, now)
            if local.hour >= user["hour"] and user["last_digest"] != local.date().isoformat():
                try:
                    self.send_digest(user, now=now)
                except Exception:
                    log.exception("The digest for %s failed", user["id"])

    def maybe_update(self, now):
        if self.updating and self.updating.is_alive():
            return
        last = self.store.get("last_update")
        if last == now.date().isoformat() or (last and now.hour < self.settings["update_hour_utc"]):
            return  # done today, or not time yet (the very first update runs right away)
        if self.update_tried_at and now - self.update_tried_at < timedelta(minutes=30):
            return  # the last try failed recently: wait a little
        self.update_tried_at = now
        self.updating = threading.Thread(target=self.run_update, name="daily-update", daemon=True)
        self.updating.start()

    def run_update(self):
        try:
            digest.daily_update(self.store, self.cfg, self.client, alert=self.send_alert)
        except Exception:
            log.exception("The daily update failed; it'll try again in 30 minutes")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for chatty in ("httpx", "google_genai"):  # they log every request
        logging.getLogger(chatty).setLevel(logging.WARNING)
    require_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GEMINI_API_KEY")
    bot = Bot(load_config(), Store(), gemini_client())
    offset = int(bot.store.get("telegram_offset", 0))
    log.info("The bot is running. Press Ctrl+C to stop it.")
    try:
        while True:
            try:
                updates = tg.call("getUpdates", offset=offset, timeout=POLL_SECONDS, allowed_updates=["message", "callback_query"])
            except tg.TelegramError as exc:
                log.warning("Telegram: %s", exc)  # "Conflict" means another copy of the bot is running
                time.sleep(5)
                updates = []
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    bot.handle(update)
                except (Exception, SystemExit):
                    log.exception("Couldn't handle update %s", update.get("update_id"))
                bot.store.put("telegram_offset", offset)
            try:
                bot.tick()
            except (Exception, SystemExit):
                log.exception("Scheduled work failed")
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
