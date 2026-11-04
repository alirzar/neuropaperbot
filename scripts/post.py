"""Post approved drafts to the platforms you picked for them, then update their Telegram messages.

Each run posts at most one draft per platform, and only once that platform's last post is
posting.min_hours_between_posts old, so drafts you approve together still go out spaced apart.

  python scripts/post.py              post what's due
  python scripts/post.py --now        don't wait for the spacing
  python scripts/post.py --dry-run    show what would be posted

Env: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, to update the messages (and, with
     posting.telegram_channel in config.json, to post to your channel); BLUESKY_USERNAME and
     BLUESKY_PASSWORD (an app password) for Bluesky; LINKEDIN_ACCESS_TOKEN for LinkedIn (from
     linkedin_login.py); X_API_KEY, X_API_KEY_SECRET, X_ACCESS_TOKEN and X_ACCESS_TOKEN_SECRET for X.
"""
import argparse
import re
from datetime import timedelta

import linkedin_api
import telegram_api
from card import make_card
from common import DRAFTS, POSTED, iso, load_config, now_utc, parse_iso, read_json, require_env, write_json
from compose import (
    LINKEDIN_LIMIT,
    PLATFORMS,
    X_LIMIT,
    channel_link,
    channel_post,
    enabled_platforms,
    pending,
    post_text,
    review_buttons,
    review_caption,
    settle,
)

MAX_ATTEMPTS = 3  # a platform that fails this many runs in a row is given up on for that draft

LINK = re.compile(r"https?://[^\s<>\"]+")
TAG = re.compile(r"(?<![\w#])#([^\W\d_][\w-]*)")
MENTION = re.compile(r"(?<![\w@])@([a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+)")


def facets(client, text, models):
    """Bluesky only makes links, hashtags and @mentions clickable when the post lists them with byte offsets."""
    F = models.AppBskyRichtextFacet

    def where(start, end):
        return F.ByteSlice(byte_start=len(text[:start].encode()), byte_end=len(text[:end].encode()))

    found, links = [], []
    for m in LINK.finditer(text):
        url = m.group().rstrip(".,;:!?)")
        links.append(range(m.start(), m.start() + len(url)))
        found.append(F.Main(features=[F.Link(uri=url)], index=where(m.start(), m.start() + len(url))))
    in_link = lambda pos: any(pos in r for r in links)  # noqa: E731
    for m in TAG.finditer(text):
        if not in_link(m.start()):
            found.append(F.Main(features=[F.Tag(tag=m.group(1))], index=where(m.start(), m.end())))
    for m in MENTION.finditer(text):
        if in_link(m.start()):
            continue
        try:
            did = client.com.atproto.identity.resolve_handle({"handle": m.group(1)}).did
        except Exception:
            continue  # not a real handle: leave it as plain text
        found.append(F.Main(features=[F.Mention(did=did)], index=where(m.start(), m.end())))
    return found or None


def bluesky_result(uri, cid):
    did, rkey = uri.split("/")[2], uri.split("/")[-1]  # at://did:plc:…/app.bsky.feed.post/<rkey>
    return {"uri": uri, "cid": cid, "url": f"https://bsky.app/profile/{did}/post/{rkey}"}


def post_channel(d, cfg, png):
    """The card and the post, in your public Telegram channel."""
    channel = cfg["posting"].get("telegram_channel")
    if not channel:
        raise SystemExit("Set posting.telegram_channel in config.json to your channel's @username")
    msg = telegram_api.send_photo(png, channel_post(d, cfg["posting"].get("hashtag")), chat=channel)
    return {"message_id": msg["message_id"], "url": channel_link(channel, msg["message_id"])}


def post_bluesky(d, cfg, png):
    from atproto import Client, models

    username, password = require_env("BLUESKY_USERNAME", "BLUESKY_PASSWORD")
    client = Client()
    client.login(username.lstrip("@"), password)

    # If an earlier run posted this but couldn't save that it did, don't post it twice.
    for item in client.get_author_feed(actor=client.me.did, limit=20).feed:
        external = getattr(getattr(item.post.record, "embed", None), "external", None)
        if external is not None and external.uri == d["link"]:
            print("Already on Bluesky (an earlier run must have posted it); recording that post.")
            return bluesky_result(item.post.uri, item.post.cid)

    thumb = None
    try:
        thumb = client.upload_blob(png).blob
    except Exception as exc:  # a link card without a thumbnail beats no post
        print(f"Thumbnail upload failed, posting without it: {exc}")
    card = models.AppBskyEmbedExternal.Main(
        external=models.AppBskyEmbedExternal.External(
            uri=d["link"], title=d["title"], description=d.get("snippet") or "", thumb=thumb
        )
    )
    text = post_text(d, cfg["posting"].get("hashtag"))
    resp = client.send_post(text=text, facets=facets(client, text, models), embed=card, langs=["en"])
    return bluesky_result(resp.uri, resp.cid)


def post_linkedin(d, cfg, png):
    """A link card posted as you, from your own profile."""
    (token,) = require_env("LINKEDIN_ACCESS_TOKEN")
    author = linkedin_api.person_urn(token)
    thumbnail = None
    try:
        thumbnail = linkedin_api.upload_image(token, author, png)
    except linkedin_api.LinkedInError as exc:
        if exc.status == 401:
            raise
        print(f"LinkedIn thumbnail upload failed, posting without it: {exc}")
    urn = linkedin_api.create_post(
        token,
        author,
        post_text(d, cfg["posting"].get("hashtag"), limit=LINKEDIN_LIMIT),
        d["link"],
        d["title"],
        d.get("snippet") or "",
        thumbnail,
    )
    return {"urn": urn, "url": linkedin_api.post_url(urn) if urn else None}


def post_x(d, cfg, png):
    """Text only, with the link in a reply: X shows posts with links to fewer people."""
    import tweepy

    key, secret, token, token_secret = require_env("X_API_KEY", "X_API_KEY_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    client = tweepy.Client(consumer_key=key, consumer_secret=secret, access_token=token, access_token_secret=token_secret)
    tweet_id = client.create_tweet(text=post_text(d, cfg["posting"].get("hashtag"), limit=X_LIMIT)).data["id"]
    try:
        client.create_tweet(text=f"Read the paper: {d['link']}", in_reply_to_tweet_id=tweet_id)
    except Exception as exc:
        print(f"X link reply failed: {exc}")
    return {"tweet_id": tweet_id, "url": f"https://x.com/i/web/status/{tweet_id}"}


POSTERS = {"channel": post_channel, "bluesky": post_bluesky, "linkedin": post_linkedin, "x": post_x}


def last_post(posted, platform):
    times = [p["posted_at"] for p in posted if p.get("platform") == platform]
    return parse_iso(max(times)) if times else None


def main():
    parser = argparse.ArgumentParser(description="Post approved drafts.")
    parser.add_argument("--now", action="store_true", help="don't wait for posting.min_hours_between_posts")
    parser.add_argument("--dry-run", action="store_true", help="show what would be posted, post nothing")
    args = parser.parse_args()

    cfg = load_config()
    hashtag = cfg["posting"].get("hashtag")
    platforms = enabled_platforms(cfg)
    gap = timedelta(hours=cfg["posting"].get("min_hours_between_posts", 2))
    drafts = read_json(DRAFTS, [])
    posted = read_json(POSTED, [])
    now = now_utc()

    queued = [d for d in drafts if d["status"] == "approved"]
    if not queued:
        print("Nothing approved to post.")
        return
    if not platforms:
        print(f"{len(queued)} approved, but posting.platforms in config.json is empty, so nothing is posted.")
        return

    errors, touched = [], []
    for platform in platforms:
        name = PLATFORMS[platform]
        due = [d for d in queued if platform in pending(d)]
        if not due:
            continue
        last = last_post(posted, platform)
        if last and not args.now and now - last < gap:
            print(f"{name}: {len(due)} queued, but the last post went out less than {gap} ago; trying next run.")
            continue
        d = min(due, key=lambda d: d.get("queued_at", {}).get(platform, ""))  # oldest tap first
        if args.dry_run:
            print(f"Would post draft {d['id']} to {name}:\n\n{post_text(d, hashtag)}\n\nLink card: {d['title']}\n  {d['link']}\n")
            continue

        try:
            result = POSTERS[platform](d, cfg, make_card(d, cfg.get("name", "Neuro Paper Bot")))
        except Exception as exc:
            message = str(exc)[:200]
            errors.append(f"{name}: {message}")
            if getattr(exc, "status", None) == 401:  # the login, not this post: keep it queued until you log in again
                d.setdefault("notes", {})[platform] = f"the {name} login has expired; run scripts/linkedin_login.py"
            else:
                attempts = d.setdefault("attempts", {})
                attempts[platform] = attempts.get(platform, 0) + 1
                if attempts[platform] >= MAX_ATTEMPTS:
                    d.setdefault("failed", {})[platform] = message
        else:
            result["posted_at"] = iso(now)
            d.setdefault("posted", {})[platform] = result
            d.get("notes", {}).pop(platform, None)
            posted.append(
                {"id": d["id"], "doi": d["doi"], "title": d["title"], "venue": d["venue"], "platform": platform, **result}
            )
            print(f"Posted to {name}: {result.get('url') or '(no link returned)'}")
        settle(d)
        write_json(DRAFTS, drafts)  # saved after each post, so a later failure can't make it post twice
        write_json(POSTED, posted)
        if d not in touched:
            touched.append(d)

    for d in touched:
        if d.get("tg_message_id"):
            try:
                telegram_api.edit_caption(d["tg_message_id"], review_caption(d, hashtag, platforms), review_buttons(d, platforms))
            except telegram_api.TelegramError as exc:
                print(f"Couldn't update the Telegram message for draft {d['id']}: {exc}")
    if errors:
        # Fail the run so GitHub shows it (and emails you), even when another platform worked.
        raise SystemExit("Posting failed. " + "; ".join(errors))


if __name__ == "__main__":
    main()
