"""Score papers with Gemini and pick the day's drafts.

The prompt is scripts/ranking_prompt.txt; edit its "MY INTERESTS" part to steer
the picks. Each paper gets a score from 1 to 10 and a short reason. The picks are
the best few from each source (selection.daily_picks) that reach selection.min_score.

Preview the scores for a date window without saving anything:
  python scripts/rank_papers.py
  python scripts/rank_papers.py --start 2026-10-28 --end 2026-10-30 --top 30
"""
import argparse
import json
from pathlib import Path

from pydantic import BaseModel

from common import gemini_client, gemini_generate, gemini_models, load_config, shorten
from fetch_papers import add_window_args, fetch_papers, window_from_args

PROMPT = Path(__file__).resolve().parent / "ranking_prompt.txt"
BATCH_SIZE = 20  # papers per Gemini request
ABSTRACT_CHARS = 1500  # enough to judge a paper, and keeps requests small


class Score(BaseModel):
    id: str
    score: int
    reason: str


def load_prompt():
    return PROMPT.read_text(encoding="utf-8").strip()


def score_papers(client, models, prompt, papers):
    """Returns {doi (lowercase): {"score": int, "reason": str}}. Papers Gemini leaves out are missing."""
    scores = {}
    for i in range(0, len(papers), BATCH_SIZE):
        # Short IDs rather than DOIs, which a model can garble.
        batch = {f"p{n}": p for n, p in enumerate(papers[i : i + BATCH_SIZE], 1)}
        listing = "\n\n".join(
            f"ID: {pid}\nSOURCE: {p['venue']}\nTITLE: {p['title']}\nABSTRACT: {p['abstract'][:ABSTRACT_CHARS]}"
            for pid, p in batch.items()
        )
        for item in generate_scores(client, models, f"{prompt}\n\nPAPERS\n\n{listing}"):
            paper = batch.get(str(item.get("id")))
            if paper and isinstance(item.get("score"), int):
                scores[paper["doi"].lower()] = {
                    "score": max(1, min(10, item["score"])),
                    "reason": str(item.get("reason", "")).strip(),
                }
        missed = sum(1 for p in batch.values() if p["doi"].lower() not in scores)
        if missed:
            print(f"  Gemini left out {missed} of {len(batch)} papers in one request; they'll be retried next batch")
    return scores


def generate_scores(client, models, contents):
    config = {"response_mime_type": "application/json", "response_schema": list[Score], "temperature": 0.2}
    for _ in range(2):  # with a schema a garbled reply is rare, but worth one more try
        try:
            items = json.loads(gemini_generate(client, models, contents, config))
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        except ValueError:  # includes json.JSONDecodeError
            pass
    raise ValueError("Gemini's scores weren't a JSON list")


def pick(papers, scores, quotas, min_score):
    """The best `quotas[source]` papers from each source that score at least min_score."""
    picks = []
    for source, count in quotas.items():
        pool = [
            p for p in papers if p["source"] == source and scores.get(p["doi"].lower(), {}).get("score", 0) >= min_score
        ]
        pool.sort(key=lambda p: (scores[p["doi"].lower()]["score"], p.get("date") or ""), reverse=True)
        picks += pool[:count]
    return picks


def main():
    parser = argparse.ArgumentParser(description="Score a window of papers and list the best (saves nothing).")
    add_window_args(parser)
    parser.add_argument("--top", type=int, default=20, help="how many papers to list (default 20)")
    args = parser.parse_args()
    cfg = load_config()
    start, end = window_from_args(args, cfg)

    papers, errors = fetch_papers(cfg, start, end)
    for err in errors:
        print(f"ERROR {err}")
    print(f"Scoring {len(papers)} papers from {start} to {end}...")
    sel = cfg["selection"]
    scores = score_papers(gemini_client(), gemini_models(cfg), load_prompt(), papers)
    chosen = {p["doi"].lower() for p in pick(papers, scores, sel["daily_picks"], sel.get("min_score", 1))}
    ranked = sorted(
        (p for p in papers if p["doi"].lower() in scores), key=lambda p: scores[p["doi"].lower()]["score"], reverse=True
    )
    for p in ranked[: args.top]:
        s = scores[p["doi"].lower()]
        mark = "PICK" if p["doi"].lower() in chosen else "    "
        print(f"{mark} {s['score']:2d}  {p['venue']:<24}  {shorten(p['title'], 90)}\n               {s['reason']}")


if __name__ == "__main__":
    main()
