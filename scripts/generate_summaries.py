"""Write the hook, the teaser text at the top of each post, with Gemini.

The prompt is scripts/summary_prompt.txt. Hooks are at most posting.hook_max_chars
long; when Gemini writes more, it's asked again rather than the text being cut off.

Try the current prompt on the latest drafts, next to their saved hooks (saves nothing):
  python scripts/generate_summaries.py
  python scripts/generate_summaries.py --last 10
"""
import argparse
import time
from pathlib import Path

from common import DRAFTS, gemini_client, load_config, read_json, shorten

PROMPT = Path(__file__).resolve().parent / "summary_prompt.txt"
ABSTRACT_CHARS = 4000


def load_prompt(max_chars):
    return PROMPT.read_text(encoding="utf-8").strip().replace("{max_chars}", str(max_chars))


def make_hook(client, model, prompt, paper, max_chars):
    contents = f"{prompt}\n\nSOURCE: {paper['venue']}\nTITLE: {paper['title']}\nABSTRACT: {paper['abstract'][:ABSTRACT_CHARS]}"
    hook = ""
    for _ in range(3):
        hook = " ".join(generate_text(client, model, contents).split()).strip('"“”')
        if len(hook) <= max_chars:
            return hook
        contents += f'\n\nYour answer was {len(hook)} characters long: "{hook}"\nRewrite it in at most {max_chars} characters.'
    return shorten(hook, max_chars)  # still too long after three tries


def generate_text(client, model, contents):
    for attempt in range(1, 4):
        try:
            resp = client.models.generate_content(model=model, contents=contents)
            if resp.text:
                return resp.text
            raise ValueError("empty response")
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * attempt)


def main():
    parser = argparse.ArgumentParser(description="Rewrite hooks for the latest drafts with the current prompt (saves nothing).")
    parser.add_argument("--last", type=int, default=5, help="how many recent drafts (default 5)")
    args = parser.parse_args()
    cfg = load_config()
    max_chars = cfg["posting"].get("hook_max_chars", 200)
    drafts = [d for d in read_json(DRAFTS, []) if d.get("abstract")][-args.last :]
    if not drafts:
        raise SystemExit("No drafts yet. To preview a whole batch instead: python scripts/batch.py --dry-run")

    client, model, prompt = gemini_client(), cfg["gemini"]["model"], load_prompt(max_chars)
    for d in drafts:
        print(f"### {d['title']}")
        print(f"  saved: {d.get('hook')}")
        print(f"  new:   {make_hook(client, model, prompt, d, max_chars)}\n")


if __name__ == "__main__":
    main()
