"""Topic fingerprints ("embeddings") from Gemini, and the maths for comparing them.

Every paper, interest and search becomes a list of 768 numbers. Texts about similar things get
similar numbers, so matching papers to someone's interests is just multiplying arrays.
"""
import logging
import re
import time

import numpy as np

DIM = 768
BATCH = 50  # texts per Gemini request
QUOTA_WAITS = 5  # how many times one batch waits out Gemini's per-minute limit before giving up

log = logging.getLogger("bot")


def retry_after(exc):
    """Seconds Gemini asks us to wait after a rate-limit error (at most 2 minutes)."""
    found = re.search(r"retry in ([\d.]+)s", str(exc)) or re.search(r"retryDelay'?:\s*'(\d+)s", str(exc))
    return min(float(found.group(1)) + 2 if found else 60, 120)


def embed(client, model, texts, task):
    """One unit-length fingerprint per text, as rows of an array.

    task is RETRIEVAL_DOCUMENT for papers, and RETRIEVAL_QUERY for interests and searches.
    """
    from google.genai import types

    rows = []
    for i in range(0, len(texts), BATCH):
        # Each text as its own Content: a plain list of strings is read as one text by newer models.
        contents = [types.Content(parts=[types.Part(text=t[:6000])]) for t in texts[i : i + BATCH]]
        for attempt in range(QUOTA_WAITS + 1):
            try:
                resp = client.models.embed_content(
                    model=model, contents=contents, config={"task_type": task, "output_dimensionality": DIM}
                )
                break
            except Exception as exc:
                # Free-tier keys allow 100 texts a minute, and every text in a batch counts.
                if getattr(exc, "code", None) != 429 or attempt == QUOTA_WAITS:
                    raise
                wait = retry_after(exc)
                log.info("Gemini's per-minute limit: waiting %.0f seconds", wait)
                time.sleep(wait)
        rows += [e.values for e in resp.embeddings]
    if len(rows) != len(texts):
        raise ValueError(f"asked {model} for {len(texts)} fingerprints and got {len(rows)}")
    vectors = np.asarray(rows, dtype=np.float32).reshape(-1, DIM)
    return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)


def pack(vectors):
    return np.asarray(vectors, dtype=np.float32).tobytes()


def unpack(blob):
    """The rows of fingerprints stored in blob, or None."""
    return None if blob is None else np.frombuffer(blob, dtype=np.float32).reshape(-1, DIM)
