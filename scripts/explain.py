"""Plain-language summaries of a paper on request, written by Gemini from the title and abstract.

The prompt is scripts/explain_prompt.txt.
"""
import html
import json
from pathlib import Path

from pydantic import BaseModel

from common import gemini_generate
from compose import attribution

PROMPT = Path(__file__).resolve().parent / "explain_prompt.txt"
PARTS = (
    ("question", "The question"),
    ("approach", "What they did"),
    ("findings", "What they found"),
    ("caveats", "Caveats"),
    ("why_it_matters", "Why it matters"),
)


class Explanation(BaseModel):
    question: str
    approach: str
    findings: str
    caveats: str
    why_it_matters: str


def explain(client, models, paper):
    """The five parts of the summary, as a dict."""
    contents = (
        PROMPT.read_text(encoding="utf-8").strip()
        + f"\n\nSOURCE: {paper['venue']}\nTITLE: {paper['title']}\nABSTRACT: {paper['abstract'][:6000]}"
    )
    config = {"response_mime_type": "application/json", "response_schema": Explanation, "temperature": 0.3}
    data = json.loads(gemini_generate(client, models, contents, config))
    return {key: str(data.get(key) or "").strip() for key, _ in PARTS}


def render(paper, parts):
    """The summary as a Telegram message (HTML)."""

    def e(text):
        return html.escape(text, quote=False)

    head = f"📄 <b>{e(paper['title'])}</b>\n{e(attribution(paper))}"
    body = "\n\n".join(f"<b>{label}:</b> {e(parts[key])}" for key, label in PARTS if parts.get(key))
    return f"{head}\n\n{body}\n\n<i>Summarized from the abstract only.</i>\n{e(paper['link'])}"
