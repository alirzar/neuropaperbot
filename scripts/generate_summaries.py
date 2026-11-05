"""Write the hook, the teaser text at the top of each post, with Gemini.

The prompt is scripts/summary_prompt.txt. Hooks are at most posting.hook_max_chars
long; when Gemini writes more, it's asked again rather than the text being cut off.

Try the current prompt on the latest drafts, next to their saved hooks (saves nothing):
  python scripts/generate_summaries.py
  python scripts/generate_summaries.py --last 10
"""
import argparse
import json
from pathlib import Path

from pydantic import BaseModel

from common import DRAFTS, gemini_client, gemini_generate, gemini_models, load_config, read_json, shorten

PROMPT = Path(__file__).resolve().parent / "summary_prompt.txt"
ABSTRACT_CHARS = 4000
BATCH_SIZE = 20  # papers per request when writing many hooks at once
BATCH_ABSTRACT_CHARS = 2500
ONE_ONLY = "Return only the teaser text, nothing else."
FOR_EACH = (
    "Write one teaser for each paper below, following the rules above for each. Return a JSON list with "
    'exactly one {"id", "hook"} object per paper, using the IDs given.'
)


class Hook(BaseModel):
    id: str
    hook: str


def load_prompt(max_chars):
    return PROMPT.read_text(encoding="utf-8").strip().replace("{max_chars}", str(max_chars))


def make_hook(client, models, prompt, paper, max_chars):
    contents = f"{prompt}\n\nSOURCE: {paper['venue']}\nTITLE: {paper['title']}\nABSTRACT: {paper['abstract'][:ABSTRACT_CHARS]}"
    hook = ""
    for _ in range(3):
        hook = " ".join(gemini_generate(client, models, contents).split()).strip('"“”')
        if len(hook) <= max_chars:
            return hook
        contents += f'\n\nYour answer was {len(hook)} characters long: "{hook}"\nRewrite it in at most {max_chars} characters.'
    return shorten(hook, max_chars)  # still too long after three tries


def make_hooks(client, models, prompt, papers, max_chars):
    """Hooks for many papers, BATCH_SIZE per Gemini request: {pid: hook}. A paper Gemini leaves out,
    or whose hook is too long, is missing from the result, and gets its own make_hook later."""
    batch_prompt = prompt.replace(ONE_ONLY, "").strip() + "\n\n" + FOR_EACH
    config = {"response_mime_type": "application/json", "response_schema": list[Hook]}
    hooks = {}
    for i in range(0, len(papers), BATCH_SIZE):
        batch = {f"p{n}": p for n, p in enumerate(papers[i : i + BATCH_SIZE], 1)}
        listing = "\n\n".join(
            f"ID: {key}\nSOURCE: {p['venue']}\nTITLE: {p['title']}\nABSTRACT: {p['abstract'][:BATCH_ABSTRACT_CHARS]}"
            for key, p in batch.items()
        )
        try:
            items = json.loads(gemini_generate(client, models, f"{batch_prompt}\n\nPAPERS\n\n{listing}", config))
        except ValueError:  # a garbled reply: these papers get their hooks one by one later
            continue
        for item in items if isinstance(items, list) else []:
            p = batch.get(str(item.get("id"))) if isinstance(item, dict) else None
            hook = " ".join(str(item.get("hook", "")).split()).strip('"“”') if p else ""
            if hook and len(hook) <= max_chars:
                hooks[p["pid"]] = hook
    return hooks


def main():
    parser = argparse.ArgumentParser(description="Rewrite hooks for the latest drafts with the current prompt (saves nothing).")
    parser.add_argument("--last", type=int, default=5, help="how many recent drafts (default 5)")
    args = parser.parse_args()
    cfg = load_config()
    max_chars = cfg["posting"].get("hook_max_chars", 200)
    drafts = [d for d in read_json(DRAFTS, []) if d.get("abstract")][-args.last :]
    if not drafts:
        raise SystemExit("No drafts yet. To preview a whole batch instead: python scripts/batch.py --dry-run")

    client, models, prompt = gemini_client(), gemini_models(cfg), load_prompt(max_chars)
    for d in drafts:
        print(f"### {d['title']}")
        print(f"  saved: {d.get('hook')}")
        print(f"  new:   {make_hook(client, models, prompt, d, max_chars)}\n")


if __name__ == "__main__":
    main()
