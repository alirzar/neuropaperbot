"""Post the oldest approved draft to Bluesky (and X, if enabled), then update its Telegram message.

At most one post per run, and only once the last post is posting.min_hours_between_posts
old, so drafts you approve together still go out spaced apart.

  python scripts/post.py              post the next approved draft, if one is due
  python scripts/post.py --now        don't wait for the spacing
  python scripts/post.py --dry-run    show what would be posted

Env: BLUESKY_USERNAME, BLUESKY_PASSWORD (an app password), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID;
     X_API_KEY, X_API_KEY_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET if posting.platforms has "x"
"""
import argparse
import re
from datetime import timedelta

import telegram_api
from card import make_card
from common import DRAFTS, POSTED, iso, load_config, now_utc, parse_iso, read_json, require_env, write_json
from compose import X_LIMIT, post_text, review_buttons, review_caption

MAX_ATTEMPTS = 3  # a draft that fails this many runs in a row is marked "failed"

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
    return {"bluesky_uri": uri, "bluesky_cid": cid, "bluesky_url": f"https://bsky.app/profile/{did}/post/{rkey}"}


def post_bluesky(text, d, png):
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
    resp = client.send_post(text=text, facets=facets(client, text, models), embed=card, langs=["en"])
    return bluesky_result(resp.uri, resp.cid)


def post_x(d, hashtag):
    """Text only, with the link in a reply: X shows posts with links to fewer people."""
    import tweepy

    key, secret, token, token_secret = require_env("X_API_KEY", "X_API_KEY_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    client = tweepy.Client(consumer_key=key, consumer_secret=secret, access_token=token, access_token_secret=token_secret)
    tweet_id = client.create_tweet(text=post_text(d, hashtag, limit=X_LIMIT)).data["id"]
    try:
        client.create_tweet(text=f"Read the paper: {d['link']}", in_reply_to_tweet_id=tweet_id)
    except Exception as exc:
        print(f"X link reply failed: {exc}")
    return {"tweet_id": tweet_id}


def main():
    parser = argparse.ArgumentParser(description="Post the next approved draft.")
    parser.add_argument("--now", action="store_true", help="don't wait for posting.min_hours_between_posts")
    parser.add_argument("--dry-run", action="store_true", help="show what would be posted, post nothing")
    args = parser.parse_args()

    cfg = load_config()
    posting = cfg["posting"]
    hashtag = posting.get("hashtag")
    platforms = posting.get("platforms", ["bluesky"])
    drafts = read_json(DRAFTS, [])
    posted = read_json(POSTED, [])
    now = now_utc()

    approved = sorted((d for d in drafts if d["status"] == "approved"), key=lambda d: d.get("approved_at", ""))
    if not approved:
        print("Nothing approved to post.")
        return
    gap = timedelta(hours=posting.get("min_hours_between_posts", 2))
    if posted and not args.now and now - parse_iso(posted[-1]["posted_at"]) < gap:
        print(f"{len(approved)} approved, but the last post went out less than {gap} ago; trying next run.")
        return

    d = approved[0]
    text = post_text(d, hashtag)
    if args.dry_run:
        print(f"Would post draft {d['id']} to {', '.join(platforms)}:\n\n{text}\n\nLink card: {d['title']}\n  {d['link']}")
        return

    png = make_card(d, cfg.get("name", "Neuro Paper Bot"))
    result, errors = {}, []
    if "bluesky" in platforms:
        try:
            result.update(post_bluesky(text, d, png))
            print(f"On Bluesky: {result['bluesky_url']}")
        except Exception as exc:
            errors.append(f"Bluesky: {exc}")
    if "x" in platforms:
        try:
            result.update(post_x(d, hashtag))
            print(f"Posted to X: {result['tweet_id']}")
        except Exception as exc:
            errors.append(f"X: {exc}")

    if result:
        d.update(status="posted", posted_at=iso(now), **result)
        posted.append(
            {"id": d["id"], "doi": d["doi"], "title": d["title"], "venue": d["venue"], "posted_at": d["posted_at"], **result}
        )
        write_json(POSTED, posted)
    else:
        d["post_attempts"] = d.get("post_attempts", 0) + 1
        if d["post_attempts"] >= MAX_ATTEMPTS:
            d.update(status="failed", error="; ".join(errors)[:200])
    write_json(DRAFTS, drafts)

    if d.get("tg_message_id") and d["status"] in ("posted", "failed"):
        try:
            telegram_api.edit_caption(d["tg_message_id"], review_caption(d, hashtag), review_buttons(d))
        except telegram_api.TelegramError as exc:
            print(f"Couldn't update the Telegram message: {exc}")
    if errors:
        # Fail the run so GitHub shows it (and emails you), even when another platform worked.
        raise SystemExit("Posting failed. " + "; ".join(errors))


if __name__ == "__main__":
    main()
