"""Citation files for saved papers: RIS (Zotero, EndNote, Mendeley) or BibTeX (LaTeX).

Each entry comes from Crossref, which has the full author names, journal and pages. When Crossref
doesn't know a paper or doesn't answer, the entry is built from what the bot has (last names only).
"""
import re
from concurrent.futures import ThreadPoolExecutor

import requests

CROSSREF = "https://api.crossref.org/works/{doi}/transform/{kind}"
KINDS = {"ris": "application/x-research-info-systems", "bib": "application/x-bibtex"}
TIMEOUT = 20
LINE_ENDS = "\r\n"

session = requests.Session()
session.headers["User-Agent"] = "neuropaperbot/0.1 (https://github.com/alirzar/neuropaperbot)"


def citations(papers, kind="ris"):
    """The text of a citation file for these papers; kind is "ris" or "bib"."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(lambda p: citation(p, kind), papers))
    return "\n\n".join(entries) + "\n"


def citation(p, kind):
    try:
        resp = session.get(CROSSREF.format(doi=p["doi"], kind=KINDS[kind]), timeout=TIMEOUT)
        if resp.status_code == 200:
            # Crossref doesn't say it's UTF-8; and RIS needs the space at the end of "ER  - ".
            text = resp.content.decode("utf-8", errors="replace").lstrip().rstrip(LINE_ENDS)
            if text.startswith("@" if kind == "bib" else "TY  -"):
                return text
    except requests.RequestException:
        pass
    return own_ris(p) if kind == "ris" else own_bibtex(p)


def year(p):
    found = re.match(r"\d{4}", p.get("date") or "")
    return found.group(0) if found else ""


def own_ris(p):
    preprint = p.get("source") in ("biorxiv", "medrxiv")
    lines = [f"TY  - {'GENERIC' if preprint else 'JOUR'}", f"TI  - {p['title']}"]
    lines += [f"AU  - {name}" for name in p.get("authors") or []]
    lines += [f"T2  - {p['venue']}" if p.get("venue") else "", f"PY  - {year(p)}" if year(p) else ""]
    lines += [f"DO  - {p['doi']}", f"UR  - {p.get('link') or 'https://doi.org/' + p['doi']}", "ER  - "]
    return "\n".join(line for line in lines if line)


def own_bibtex(p):
    def braces(text):
        return (text or "").replace("{", "(").replace("}", ")")

    first = re.sub(r"[^A-Za-z]", "", (p.get("authors") or ["paper"])[0]) or "paper"
    fields = {
        "title": braces(p["title"]),
        "author": " and ".join(braces(a) for a in p.get("authors") or []),
        "journal": braces(p.get("venue")),
        "year": year(p),
        "doi": p["doi"],
        "url": p.get("link") or f"https://doi.org/{p['doi']}",
    }
    body = ",\n".join(f"  {key}={{{value}}}" for key, value in fields.items() if value)
    return f"@article{{{first}{year(p)}_{p['pid']},\n{body}\n}}"
