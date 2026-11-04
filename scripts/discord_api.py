"""Discord for the public bot: sending messages, turning the bot's messages into Discord's format,
and the connection that receives what people do.

The bot writes each message once, as Telegram HTML with rows of buttons. Here that becomes
Discord Markdown and components. Lists people tick (topics, sources) and the time zone list become
dropdown menus, since Discord allows at most 25 buttons on a message. Other buttons stay buttons,
five to a row.

People use the bot in a direct message. Discord only lets a bot message people who share a server
with it, so the bot lives in a server people join first.

API reference: https://discord.com/developers/docs
Env: DISCORD_BOT_TOKEN
"""
import html
import json
import logging
import math
import os
import re
import threading
import time

import requests

API = "https://discord.com/api/v10"
TIMEOUT = 60
LIMIT = 2000  # characters in a message
NO_EMBEDS, EPHEMERAL = 1 << 2, 1 << 6  # message flags: no link previews; only the person sees it
CANT_MESSAGE = 50007  # Discord's error code when the bot can't message someone (they left the server)
TICK = "✅ "
MENUS = {"topic": "Choose your areas", "src": "Choose your sources", "tz": "Choose your time zone"}
SINGLE = {"tz"}  # menus where you pick one; in the others you tick any number

log = logging.getLogger("bot")
session = requests.Session()
session.headers["User-Agent"] = "DiscordBot (https://github.com/alirzar/neuropaperbot, 1.0)"
_channels = {}  # each person's direct-message channel
_app = []  # the application's ID, once known


class DiscordError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def call(method, path, payload=None, files=None):
    """One request to Discord's API, waiting out rate limits."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise DiscordError("DISCORD_BOT_TOKEN is not set (in .env)")
    shown = re.sub(r"/webhooks/(\d+)/[^/]+", r"/webhooks/\1/…", path)  # interaction tokens stay out of errors
    for _ in range(5):
        try:
            if files:
                body = {"payload_json": json.dumps(payload or {})}
                resp = session.request(method, API + path, headers={"Authorization": f"Bot {token}"}, data=body, files=files, timeout=TIMEOUT)
            else:
                resp = session.request(method, API + path, headers={"Authorization": f"Bot {token}"}, json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise DiscordError(f"{method} {shown}: {type(exc).__name__}") from None
        if resp.status_code == 429:  # too many requests: wait as long as Discord asks
            try:
                wait = float(resp.json().get("retry_after", 1))
            except ValueError:
                wait = 1.0
            time.sleep(min(wait, 30) + 0.1)
            continue
        if resp.status_code >= 400:
            try:
                error = resp.json()
            except ValueError:
                error = {}
            raise DiscordError(f"{method} {shown}: {resp.status_code} {error.get('message', '')}".strip(), error.get("code"))
        return resp.json() if resp.content else None
    raise DiscordError(f"{method} {shown}: still rate limited")


# ---------- from Telegram HTML to Discord Markdown ----------
_TAG = re.compile(r'<(/?)(b|i|code|pre|a)(?: href="([^"]*)")?>')
_URL = re.compile(r"https?://\S+")


def _escape(text):
    """Plain text with Discord's formatting characters escaped, except inside web addresses."""
    out, last = [], 0
    for m in _URL.finditer(text):
        out += [_escape_plain(text[last : m.start()]), m.group(0)]
        last = m.end()
    return "".join(out + [_escape_plain(text[last:])])


def _escape_plain(text):
    text = re.sub(r"([\\*_~`|\[\]])", r"\\\1", text)
    return re.sub(r"(?m)^(\s*)([>#-])", r"\1\\\2", text)  # quotes, headings and lists start a line


def markdown(text):
    """The bot's Telegram HTML (b, i, code, pre and links) as Discord Markdown."""
    out, pos, code, links = [], 0, 0, []
    for m in _TAG.finditer(text):
        plain = html.unescape(text[pos : m.start()])
        out.append(plain if code else _escape(plain))
        pos = m.end()
        closing, tag = m.group(1) == "/", m.group(2)
        if tag == "b":
            out.append("**")
        elif tag == "i":
            out.append("_")
        elif tag == "code":
            out.append("`")
            code += -1 if closing else 1
        elif tag == "pre":
            out.append("\n```" if closing else "```\n")
            code += -1 if closing else 1
        elif closing:
            out.append(f"]({links.pop() if links else ''})")
        else:
            links.append(html.unescape(m.group(3) or ""))
            out.append("[")
    rest = html.unescape(text[pos:])
    out.append(rest if code else _escape(rest))
    return "".join(out)


def pieces(text, limit=LIMIT):
    """Text split at paragraph breaks into messages Discord accepts."""
    parts, current = [], ""
    for para in text.split("\n\n"):
        joined = f"{current}\n\n{para}" if current else para
        if len(joined) <= limit:
            current = joined
            continue
        if current:
            parts.append(current)
        while len(para) > limit:
            parts.append(para[:limit])
            para = para[limit:]
        current = para
    return parts + [current] if current or not parts else parts


# ---------- from rows of buttons to components ----------
def components(rows):
    """Rows of Telegram-style buttons as Discord components, at most five rows of five."""
    menus, plain = {}, []
    for row in rows or []:
        kept = []
        for b in row:
            kind = b["callback_data"].partition(":")[0]
            if kind in MENUS:
                menus.setdefault(kind, []).append(b)
            else:
                kept.append(b)
        if kept:
            plain.append(kept)
    out = []
    for kind, items in menus.items():
        n = math.ceil(len(items) / 25)
        size = math.ceil(len(items) / n)
        for i in range(n):
            out.append({"type": 1, "components": [menu(kind, i, items[i * size : (i + 1) * size], n)]})
    if len(out) + len(plain) > 5 or any(len(row) > 5 for row in plain):
        flat = [b for row in plain for b in row]
        plain = [flat[i : i + 5] for i in range(0, len(flat), 5)]
    out += [{"type": 1, "components": [as_button(b) for b in row]} for row in plain]
    return out[:5]


def menu(kind, index, items, count):
    options = [
        {"label": b["text"].removeprefix(TICK)[:100], "value": b["callback_data"],
         "default": kind not in SINGLE and b["text"].startswith(TICK)}
        for b in items
    ]
    placeholder = MENUS[kind] if count == 1 else f"{options[0]['label']} … {options[-1]['label']}"
    one = kind in SINGLE
    return {
        "type": 3, "custom_id": f"pick:{kind}:{index}", "options": options, "placeholder": placeholder[:150],
        "min_values": 1 if one else 0, "max_values": 1 if one else len(options),
    }


def as_button(b):
    main = b["text"] == "Done" or b["callback_data"] in ("more", "go")
    return {"type": 2, "style": 1 if main else 2, "label": b["text"][:80], "custom_id": b["callback_data"][:100]}


def menu_taps(custom_id, values, rows):
    """What a dropdown choice means as taps: the one picked, or each option ticked or unticked."""
    kind = (custom_id.split(":") + [""])[1]  # pick:<kind>:<index>
    if kind in SINGLE:
        return list(values[:1])
    for row in rows:
        for c in row.get("components", []):
            if c.get("custom_id") == custom_id:
                return [o["value"] for o in c.get("options", []) if (o["value"] in values) != bool(o.get("default"))]
    return []


def component_events(data, rows, message_id):
    """The tap events for a pressed button or a dropdown choice. data is the interaction's data,
    rows the message's components; only the last tap redraws the message."""
    custom_id = data.get("custom_id", "")
    taps = menu_taps(custom_id, data.get("values", []), rows) if data.get("component_type") == 3 else [custom_id]
    return [
        {"kind": "tap", "data": tap, "message_id": message_id if i == len(taps) - 1 else None, "quiet": len(taps) > 1}
        for i, tap in enumerate(taps)
    ]


# ---------- sending ----------
def channel(uid):
    """The direct-message channel with this person."""
    if uid not in _channels:
        _channels[uid] = call("POST", "/users/@me/channels", {"recipient_id": str(uid)})["id"]
    return _channels[uid]


def send(uid, text, buttons=None):
    parts = pieces(markdown(text))
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        payload = {"content": part, "flags": NO_EMBEDS, "allowed_mentions": {"parse": []}}
        msg = call("POST", f"/channels/{channel(uid)}/messages", {**payload, "components": components(buttons) if last else []})
    return int(msg["id"])


def photo(uid, png, caption, buttons=None):
    payload = {
        "content": pieces(markdown(caption))[0], "flags": NO_EMBEDS, "allowed_mentions": {"parse": []},
        "components": components(buttons), "attachments": [{"id": 0, "filename": "card.png"}],
    }
    msg = call("POST", f"/channels/{channel(uid)}/messages", payload, files={"files[0]": ("card.png", png, "image/png")})
    return int(msg["id"])


def edit(uid, message_id, text, buttons=None):
    payload = {"content": pieces(markdown(text))[0], "components": components(buttons)}
    call("PATCH", f"/channels/{channel(uid)}/messages/{message_id}", payload)


def edit_buttons(uid, message_id, buttons):
    call("PATCH", f"/channels/{channel(uid)}/messages/{message_id}", {"components": components(buttons)})


def typing(uid):
    try:
        call("POST", f"/channels/{channel(uid)}/typing")
    except DiscordError:
        pass


def application_id():
    if not _app:
        _app.append(call("GET", "/oauth2/applications/@me")["id"])
    return _app[0]


def note(token, text):
    """A short reply to a tap that only this person sees."""
    try:
        call("POST", f"/webhooks/{application_id()}/{token}", {"content": markdown(text), "flags": EPHEMERAL})
    except DiscordError as exc:
        log.warning("Discord note: %s", exc)


def finish(token):
    """Remove the "thinking…" line Discord shows while a slash command runs."""
    try:
        call("DELETE", f"/webhooks/{application_id()}/{token}/messages/@original")
    except DiscordError:
        pass


class App:
    """The bot's way to talk to people on Discord (telegram_api.App has the same methods)."""

    name = "Discord"

    def send(self, uid, text, buttons=None):
        return send(uid, text, buttons)

    def photo(self, uid, png, caption, buttons=None):
        return photo(uid, png, caption, buttons)

    def edit(self, uid, message_id, text, buttons=None):
        edit(uid, message_id, text, buttons)

    def edit_buttons(self, uid, message_id, buttons):
        edit_buttons(uid, message_id, buttons)

    def typing(self, uid):
        typing(uid)


# ---------- slash commands ----------
def command_list(commands):
    """[(name, description, option)] as Discord commands for direct messages with the bot;
    option is None or (name, description, required)."""
    return [
        {
            "name": name, "description": description, "type": 1, "contexts": [1], "integration_types": [0],
            "options": [{"type": 3, "name": o[0], "description": o[1], "required": o[2]}] if o else [],
        }
        for name, description, o in commands
    ]


def _comparable(cmds):
    return sorted(
        (
            c["name"], c["description"], tuple(c.get("contexts") or []),
            tuple((o["name"], o["description"], bool(o.get("required"))) for o in c.get("options") or []),
        )
        for c in cmds
    )


def register_commands(commands):
    """Make the bot's slash commands match `commands`, only writing them when they changed."""
    wanted = command_list(commands)
    path = f"/applications/{application_id()}/commands"
    if _comparable(call("GET", path) or []) == _comparable(wanted):
        return False
    call("PUT", path, wanted)
    return True


def invite_link():
    """The link that adds the bot to a server (it needs no permissions there)."""
    return f"https://discord.com/oauth2/authorize?client_id={application_id()}&scope=bot%20applications.commands&permissions=0"


# ---------- receiving ----------
def connect(events):
    """Connect to Discord in a thread of its own, and put what people do on the `events` queue as
    ("discord", event) pairs. Taps and commands are acknowledged at once, as Discord requires."""
    import discord  # only needed when the bot runs on Discord

    discord.VoiceClient.warn_nacl = discord.VoiceClient.warn_dave = False  # no voice here: skip the warnings
    intents = discord.Intents.none()
    intents.guilds = intents.dm_messages = True
    client = discord.Client(intents=intents)

    def person(user):
        return {"uid": user.id, "name": user.global_name or user.name, "username": user.name}

    @client.event
    async def on_ready():
        log.info("Discord: connected as %s", client.user)

    @client.event
    async def on_message(message):
        if message.guild is None and not message.author.bot:  # direct messages only
            events.put(("discord", {"kind": "text", "text": message.content, **person(message.author)}))

    @client.event
    async def on_interaction(interaction):
        data, who = interaction.data or {}, person(interaction.user)
        if interaction.type == discord.InteractionType.application_command:
            await interaction.response.defer(ephemeral=True, thinking=True)
            arg = " ".join(str(o.get("value", "")) for o in data.get("options", []))
            events.put(("discord", {"kind": "command", "text": f"/{data.get('name')} {arg}".strip(), "token": interaction.token, **who}))
        elif interaction.type == discord.InteractionType.component:
            await interaction.response.defer()
            message = interaction.message
            rows = [row.to_dict() for row in message.components] if message else []
            for event in component_events(data, rows, message.id if message else None):
                events.put(("discord", {**event, "token": interaction.token, **who}))

    def run():
        try:
            client.run(os.environ["DISCORD_BOT_TOKEN"], log_handler=None)
        except Exception:
            log.exception("Discord: the connection stopped")

    threading.Thread(target=run, name="discord", daemon=True).start()
    return client
