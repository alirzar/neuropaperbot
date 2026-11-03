"""The weekday batch: fetch new papers, score them, pick the day's drafts, write their hooks.

The hourly workflow runs this every time. It does nothing unless a batch is due:
a weekday, after schedule.batch_hour_utc, and no batch yet today. New drafts get
status "ready", and review.py sends them to Telegram.

  python scripts/batch.py              make today's batch, if it's due
  python scripts/batch.py --force      make a batch now, whatever the time
  python scripts/batch.py --dry-run    show what a batch would pick, save nothing
  python scripts/batch.py --dry-run --start 2026-10-28 --end 2026-10-30
"""
import argparse
from datetime import timedelta

from common import (
    DRAFTS,
    POSTED,
    SEEN,
    STATE,
    describe,
    gemini_client,
    gemini_models,
    iso,
    load_config,
    now_utc,
    parse_iso,
    read_json,
    short_id,
    shorten,
    title_key,
    write_json,
)
from compose import post_text
from fetch_papers import add_window_args, fetch_papers, window_from_args
from generate_summaries import load_prompt as load_hook_prompt
from generate_summaries import make_hook
from rank_papers import load_prompt as load_ranking_prompt
from rank_papers import pick, score_papers

FINISHED = {"posted", "skipped", "expired", "failed"}
KEEP_SEEN_DAYS = 60  # well past the lookback window, after which a paper can't come back
KEEP_FINISHED_DRAFTS_DAYS = 14  # posted.json keeps the long-term record


def batch_due(cfg, state, now):
    schedule = cfg["schedule"]
    if schedule.get("weekdays_only", True) and now.weekday() >= 5:
        return False
    if now.hour < schedule.get("batch_hour_utc", 13):
        return False
    return state.get("last_batch_date") != now.date().isoformat()


def new_draft(paper, score, now):
    return {
        "id": short_id(paper["doi"]),
        "status": "ready",
        "doi": paper["doi"],
        "source": paper["source"],
        "venue": paper["venue"],
        "title": paper["title"],
        "authors": paper["authors"][:3],  # enough for "A", "A and B" or "A et al."
        "date": paper["date"],
        "link": paper["link"],
        "license": paper.get("license"),
        "abstract": paper["abstract"],
        "snippet": shorten(paper["abstract"], 300),
        "score": score["score"],
        "reason": score["reason"],
        "hook": paper.get("hook"),
        "created_at": iso(now),
    }


def prune(seen, drafts, now):
    seen_cutoff = (now - timedelta(days=KEEP_SEEN_DAYS)).date().isoformat()
    for doi in [doi for doi, day in seen.items() if day < seen_cutoff]:
        del seen[doi]
    drafts_cutoff = now - timedelta(days=KEEP_FINISHED_DRAFTS_DAYS)
    drafts[:] = [d for d in drafts if d["status"] not in FINISHED or parse_iso(d["created_at"]) > drafts_cutoff]


def main():
    parser = argparse.ArgumentParser(description="Make the weekday batch of drafts.")
    parser.add_argument("--force", action="store_true", help="make a batch now, whatever the time")
    parser.add_argument("--dry-run", action="store_true", help="show what would be picked, save nothing")
    add_window_args(parser)
    args = parser.parse_args()

    cfg = load_config()
    state = read_json(STATE, {})
    now = now_utc()
    if not (args.force or args.dry_run or batch_due(cfg, state, now)):
        print("No batch due.")
        return

    seen = read_json(SEEN, {})
    drafts = read_json(DRAFTS, [])
    posted = read_json(POSTED, [])
    start, end = window_from_args(args, cfg)
    papers, errors = fetch_papers(cfg, start, end)
    for err in errors:
        print(f"ERROR {err}")
    if args.dry_run:
        new = papers  # preview everything in the window, including papers already seen
    else:
        known_titles = {title_key(d["title"]) for d in drafts} | {title_key(p["title"]) for p in posted}
        new = [p for p in papers if p["doi"].lower() not in seen and title_key(p["title"]) not in known_titles]
    print(f"{start} to {end}: {len(papers)} papers, {len(new)} new")

    picks, scores = [], {}
    if new:
        sel = cfg["selection"]
        models = gemini_models(cfg)
        max_chars = cfg["posting"].get("hook_max_chars", 200)
        client = gemini_client()
        try:
            scores = score_papers(client, models, load_ranking_prompt(), new)
        except Exception as exc:
            raise SystemExit(f"Gemini didn't score the papers ({describe(exc)}). Nothing was saved; the next run tries again.")
        picks = pick(new, scores, sel["daily_picks"], sel.get("min_score", 1))
        hook_prompt = load_hook_prompt(max_chars)
        for p in picks:
            try:
                p["hook"] = make_hook(client, models, hook_prompt, p, max_chars)
            except Exception as exc:  # the draft still goes out; you can reply in Telegram with a hook
                print(f"ERROR writing the hook for {p['doi']}: {describe(exc)}")
                errors.append(f"hook for {p['doi']}")

    new_drafts = [new_draft(p, scores[p["doi"].lower()], now) for p in picks]
    hashtag = cfg["posting"].get("hashtag")
    for d in new_drafts:
        print(f"\n{d['score']}/10  {d['venue']}  {d['title']}\n  {d['reason']}\n")
        print("  " + post_text(d, hashtag).replace("\n", "\n  "))
    if new and not new_drafts:
        print("No paper reached selection.min_score.")

    if args.dry_run:
        print("\n[dry-run] nothing saved.")
        return

    drafts += new_drafts
    for p in new:
        if p["doi"].lower() in scores:  # a paper Gemini left out gets another chance next batch
            seen[p["doi"].lower()] = now.date().isoformat()
    prune(seen, drafts, now)
    state["last_batch_date"] = now.date().isoformat()
    write_json(DRAFTS, drafts)
    write_json(SEEN, seen)
    write_json(STATE, state)
    print(f"\nSaved {len(new_drafts)} new draft{'' if len(new_drafts) == 1 else 's'}.")
    if errors:
        raise SystemExit("Finished with errors: " + "; ".join(errors))


if __name__ == "__main__":
    main()
