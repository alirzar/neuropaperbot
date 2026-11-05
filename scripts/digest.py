"""Papers for the public bot: the daily update, choosing each person's papers, search, and author alerts.

Each paper and each interest gets a topic fingerprint (see vectors.py). A person's papers are the
week's new ones from the sources they ticked, best match first, and then, once those run out, the
best matches from everywhere else. A match is how close a paper is to any of their interests, nudged
toward papers like the ones they gave 👍 and away from the 👎 ones, and a little toward Gemini's
quality score.
"""
import logging
import os
import re
from datetime import timedelta

import numpy as np

import catalog
from common import describe, gemini_models, now_utc
from fetch_papers import fetch_sources, search_pubmed
from rank_papers import TopicScore, score_papers
from rank_papers import load_prompt as load_ranking_prompt
from vectors import BATCH, embed

log = logging.getLogger("bot")

FEEDBACK_WEIGHT = 0.2  # how much 👍 and 👎 move a score (scores are similarities, mostly 0.5 to 0.9)
QUALITY_WEIGHT = 0.02  # per point of Gemini's 1-10 score away from 5
# A watched topic matches a new paper that is among the week's closest 1% to it, or that mentions
# its words and is closer than three quarters of the week's papers. (Short phrases score low on
# every paper, and each on its own scale, so a fixed cutoff wouldn't do.)
WATCH_CLOSE, WATCH_MENTIONED = 99, 75  # percentiles
WATCH_MAX = 3  # messages per watched topic per update

TOPIC_CHECK = (
    '\n\nALSO: give each object a fourth field, "neuro": true if the paper is neuroscience, meaning it studies the '
    "brain or nervous system, or the mind and behavior they produce; false if it belongs to another field (such as "
    'machine learning, immunology or materials science), even when it uses words like "neural" or "memory".'
)


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


def wanted_sources(store, cfg):
    """The keys of every source to fetch: the base ones, and all that anyone has ticked."""
    return set(catalog.base_keys(cfg)) | store.chosen_sources()


def daily_update(store, cfg, client, alert=None, only=None, watched=None):
    """Fetch new papers, score and fingerprint them, call alert(uid, pid, label) for followed authors
    and watched(uid, pid, phrase) for watched topics.

    It fetches the base sources and every one someone has ticked, over the last bot.recent_days days,
    so every paper that can go in a digest is there. With `only` (a set of source keys) it fetches
    just those: for a source someone just ticked.
    """
    today = now_utc().date()
    settings = cfg.get("bot", {})
    everything = catalog.sources(cfg, store)
    wanted = set(only) if only else wanted_sources(store, cfg)
    start = today - timedelta(days=settings.get("recent_days", 7))
    pubmed = cfg["sources"].get("pubmed", {})
    papers, errors = fetch_sources([s for s in everything if s["key"] in wanted], start, today, pubmed)

    # Followed authors: their new papers anywhere in PubMed, not only in the journals followed.
    follow_keys = store.follow_keys()
    if not only:
        names = {s["query"]: s["name"] for s in everything if s["kind"] == "pubmed"}
        for key in sorted(follow_keys):
            term = f'"{key}"[au] AND {start:%Y/%m/%d}:{today:%Y/%m/%d}[edat]'
            try:
                papers += search_pubmed(
                    term, names, set(pubmed.get("exclude_types", [])), pubmed.get("email") or None, os.getenv("NCBI_API_KEY")
                )
            except Exception as exc:
                errors.append(f"PubMed search for {key}: {describe(exc)}")

    unique = {}
    for p in papers:
        unique.setdefault(p["doi"].lower(), p)
    new = [p for p in unique.values() if not store.has_paper(p["doi"])]
    off_topic = 0
    if new:
        try:
            scores = score_papers(client, gemini_models(cfg), load_ranking_prompt() + TOPIC_CHECK, new, TopicScore)
        except Exception as exc:  # scores only nudge the picks, so carry on without them
            scores = {}
            errors.append(f"scoring: {describe(exc)}")
        for p in new:
            score = scores.get(p["doi"].lower(), {})
            followed = set(p.get("author_keys") or []) & follow_keys
            # From a general journal, and Gemini says it isn't neuroscience: keep it aside, unfingerprinted.
            hidden = bool(p.get("broad") and score.get("neuro") is False and not followed)
            off_topic += hidden
            p["pid"] = store.add_paper(p, quality=score.get("score"), reason=score.get("reason"), hidden=hidden)

    # Fingerprints, a batch at a time, for every new paper and any a failed run left without one.
    waiting = store.unembedded()
    for i in range(0, len(waiting), BATCH):
        batch = waiting[i : i + BATCH]
        for p, vector in zip(batch, embed(client, embedding_model(cfg), [paper_text(p) for p in batch], "RETRIEVAL_DOCUMENT")):
            store.set_vector(p["pid"], vector)
        if alert:
            for p in batch:
                for key in set(p.get("author_keys") or []) & follow_keys:
                    for uid, label in store.followers(key):
                        alert(uid, p["pid"], label)
        if watched:
            for uid, pid, phrase in watch_matches(store, client, embedding_model(cfg), batch, settings.get("recent_days", 7)):
                watched(uid, pid, phrase)
    if not only:
        store.drop_sources(wanted)
        store.prune(settings.get("keep_days", 60))
        store.put("last_update", today.isoformat())
    log.info(
        "%s: %d papers fetched, %d new, %d off topic%s",
        "Update for " + ", ".join(sorted(wanted)) if only else "Daily update",
        len(unique), len(new), off_topic, f"; errors: {errors}" if errors else "",
    )
    return {"fetched": len(unique), "new": len(new), "off_topic": off_topic, "errors": errors}


def mentions(p, phrase):
    """Whether every word of the phrase (three letters or more) is in the paper's title or abstract."""
    text = f"{p['title']} {p.get('abstract') or ''}".lower()
    words = [w for w in re.findall(r"[a-z0-9]+", phrase.lower()) if len(w) >= 3]
    return bool(words) and all(w in text for w in words)


def watch_matches(store, client, model, batch, days):
    """(uid, pid, phrase) for each new paper in `batch` that a watched topic should hear about."""
    watches = store.watches()
    if not watches or not batch:
        return []
    pool = store.recent(days)  # this week's papers, the new ones included, to set each topic's bar
    if not pool:
        return []
    phrases = sorted({phrase for _, phrase in watches})
    topics = query_vectors(store, client, model, phrases)
    week = np.vstack([p["vector"] for p in pool]) @ topics.T
    close, mentioned = np.percentile(week, WATCH_CLOSE, axis=0), np.percentile(week, WATCH_MENTIONED, axis=0)
    fresh = [store.paper(p["pid"]) for p in batch]
    fresh = [p for p in fresh if p and p["vector"] is not None]
    if not fresh:
        return []
    near = np.vstack([p["vector"] for p in fresh]) @ topics.T
    found = {}
    for j, phrase in enumerate(phrases):
        hits = [(near[i, j], p) for i, p in enumerate(fresh)
                if near[i, j] >= close[j] or (near[i, j] >= mentioned[j] and mentions(p, phrase))]
        found[phrase] = [p["pid"] for _, p in sorted(hits, key=lambda h: -h[0])[:WATCH_MAX]]
    return [(uid, pid, phrase) for uid, phrase in watches for pid in found.get(phrase, [])]


def similar(store, paper, days, n):
    """The n papers from the last `days` days closest to this one (which needs a fingerprint)."""
    papers = [p for p in store.recent(days) if p["pid"] != paper["pid"]]
    if not papers or paper.get("vector") is None:
        return []
    closeness = np.vstack([p["vector"] for p in papers]) @ paper["vector"][0]
    return [papers[i] for i in np.argsort(-closeness, kind="stable")[:n]]


def best_interest(user, paper):
    """Which of this person's interests (an index into their fingerprints) the paper matches best."""
    if user["profile"] is None or paper.get("vector") is None:
        return None
    return int(np.argmax(user["profile"] @ paper["vector"][0]))


def scores_for(store, user, papers):
    """How well each paper matches this person (higher is better)."""
    matrix = np.vstack([p["vector"] for p in papers])
    quality = np.array([p["quality"] or 5 for p in papers], dtype=np.float32)
    if user["profile"] is None:  # no topics or keywords: the best papers overall
        score = np.zeros(len(papers), dtype=np.float32)
    else:
        score = (matrix @ user["profile"].T).max(axis=1)
    liked, disliked = store.feedback_vectors(user["id"])
    if liked is not None:
        score += FEEDBACK_WEIGHT * (matrix @ liked.T).max(axis=1)
    if disliked is not None:
        score -= FEEDBACK_WEIGHT * (matrix @ disliked.T).max(axis=1)
    return score + QUALITY_WEIGHT * (quality - 5)


def pick(store, user, n, days, mine=()):
    """The n papers from the last `days` days this person hasn't been sent yet: ones from their own
    sources (mine, a list of source keys) first, best match first, then the best of the rest."""
    already = store.sent_pids(user["id"])
    papers = [p for p in store.recent(days) if p["pid"] not in already]
    if not papers:
        return []
    scores, mine = scores_for(store, user, papers), set(mine)
    order = sorted(range(len(papers)), key=lambda i: (papers[i].get("src") not in mine, -scores[i]))
    return [papers[i] for i in order[:n]]


def search(store, client, model, query, days, n):
    """The n papers from the last `days` days closest to what the query describes."""
    papers = store.recent(days)
    if not papers:
        return []
    query_vector = query_vectors(store, client, model, [query])[0]
    similarity = np.vstack([p["vector"] for p in papers]) @ query_vector
    return [papers[i] for i in np.argsort(-similarity, kind="stable")[:n]]
