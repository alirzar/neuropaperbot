"""Papers for the public bot: the daily update, choosing each person's papers, search, and author alerts.

Each paper and each interest gets a topic fingerprint (see vectors.py). A person's papers are the
week's newest ones that best match any of their interests, nudged toward papers like the ones
they gave 👍 and away from the 👎 ones, plus a little for Gemini's quality score.
"""
import logging
import os
from datetime import timedelta

import numpy as np

from common import describe, gemini_models, now_utc
from fetch_papers import fetch_papers, search_pubmed
from rank_papers import load_prompt as load_ranking_prompt
from rank_papers import score_papers
from vectors import BATCH, embed

log = logging.getLogger("bot")

FEEDBACK_WEIGHT = 0.2  # how much 👍 and 👎 move a score (scores are similarities, mostly 0.5 to 0.9)
QUALITY_WEIGHT = 0.02  # per point of Gemini's 1-10 score away from 5


def embedding_model(cfg):
    return cfg["gemini"].get("embedding_model", "gemini-embedding-2")


def paper_text(p):
    return f"{p['title']}\n\n{p.get('abstract') or ''}"


def query_vectors(store, client, model, texts):
    """Fingerprints for interests and searches, cached so each text is only sent to Gemini once."""
    keys = [f"{model}|{t.strip().lower()}" for t in texts]
    missing = [t for t, k in zip(texts, keys) if store.cached_vector(k) is None]
    if missing:
        for text, vector in zip(missing, embed(client, model, missing, "RETRIEVAL_QUERY")):
            store.cache_vector(f"{model}|{text.strip().lower()}", vector)
    return np.vstack([store.cached_vector(k) for k in keys])


def daily_update(store, cfg, client, alert=None):
    """Fetch new papers, fingerprint and score them, and call alert(uid, paper, label) for followed authors."""
    today = now_utc().date()
    start = today - timedelta(days=cfg["selection"].get("lookback_days", 4))
    papers, errors = fetch_papers(cfg, start, today)

    # Followed authors: their new papers anywhere in PubMed, not only in the journals followed.
    follow_keys = store.follow_keys()
    pubmed = cfg["sources"].get("pubmed", {})
    for key in sorted(follow_keys):
        term = f'"{key}"[au] AND {start:%Y/%m/%d}:{today:%Y/%m/%d}[edat]'
        try:
            papers += search_pubmed(
                term, pubmed.get("journals", {}), set(pubmed.get("exclude_types", [])),
                pubmed.get("email") or None, os.getenv("NCBI_API_KEY"),
            )
        except Exception as exc:
            errors.append(f"PubMed search for {key}: {describe(exc)}")

    unique = {}
    for p in papers:
        unique.setdefault(p["doi"].lower(), p)
    new = [p for p in unique.values() if not store.has_paper(p["doi"])]
    for i in range(0, len(new), BATCH):
        # Saved a batch at a time, so if Gemini fails partway, the next try only does the rest.
        batch = new[i : i + BATCH]
        for p, vector in zip(batch, embed(client, embedding_model(cfg), [paper_text(p) for p in batch], "RETRIEVAL_DOCUMENT")):
            p["pid"] = store.add_paper(p, vector)
        if alert:
            for p in batch:
                for key in set(p.get("author_keys") or []) & follow_keys:
                    for uid, label in store.followers(key):
                        alert(uid, p["pid"], label)
    if new:
        try:
            scores = score_papers(client, gemini_models(cfg), load_ranking_prompt(), new)
        except Exception as exc:  # scores only nudge the picks, so carry on without them
            scores = {}
            errors.append(f"scoring: {describe(exc)}")
        for p in new:
            score = scores.get(p["doi"].lower())
            if score:
                store.set_paper(p["pid"], quality=score["score"], reason=score["reason"])
    store.prune(cfg.get("bot", {}).get("keep_days", 60))
    store.put("last_update", today.isoformat())
    log.info("Daily update: %d papers fetched, %d new%s", len(unique), len(new), f"; errors: {errors}" if errors else "")
    return {"fetched": len(unique), "new": len(new), "errors": errors}


def scores_for(store, user, papers):
    """How well each paper suits this person (higher is better)."""
    matrix = np.vstack([p["vector"] for p in papers])
    quality = np.array([p["quality"] or 5 for p in papers], dtype=np.float32)
    if user["profile"] is None:  # no topics or keywords: just the best papers overall
        return quality
    score = (matrix @ user["profile"].T).max(axis=1)
    liked, disliked = store.feedback_vectors(user["id"])
    if liked is not None:
        score += FEEDBACK_WEIGHT * (matrix @ liked.T).max(axis=1)
    if disliked is not None:
        score -= FEEDBACK_WEIGHT * (matrix @ disliked.T).max(axis=1)
    return score + QUALITY_WEIGHT * (quality - 5)


def pick(store, user, n, days):
    """The n best papers from the last `days` days that this person hasn't been sent yet."""
    already = store.sent_pids(user["id"])
    papers = [p for p in store.recent(days) if p["pid"] not in already]
    if not papers:
        return []
    order = np.argsort(-scores_for(store, user, papers), kind="stable")
    return [papers[i] for i in order[:n]]


def search(store, client, model, query, days, n):
    """The n papers from the last `days` days closest to what the query describes."""
    papers = store.recent(days)
    if not papers:
        return []
    query_vector = query_vectors(store, client, model, [query])[0]
    similarity = np.vstack([p["vector"] for p in papers]) @ query_vector
    return [papers[i] for i in np.argsort(-similarity, kind="stable")[:n]]
