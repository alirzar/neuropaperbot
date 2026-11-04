"""The sources people can choose in the public bot: preprint servers and journals.

Each source has a key like "biorxiv:neuroscience" or "pubmed:j neurosci". Everyone starts with none
ticked and picks their own. The ones under "sources" in config.json are the base: always fetched,
ticked or not, so there are always papers to choose from. "more_sources" lists the others /sources
offers, and anyone can add a journal PubMed indexes: it's checked here, then offered to everyone.

Try the journal search on its own:
  python scripts/catalog.py "journal of cognitive neuroscience"
"""
import difflib
import os
import re
import sys
from datetime import timedelta

from common import clean_text, now_utc, short_id
from fetch_papers import NEURO_FILTER, PREPRINTS, eutils, pubmed_count

GENERAL_SHARE = 0.6  # a journal is "general" when fewer than this share of its papers are about the brain
MAX_PER_MONTH = 1000  # brain papers a month: more than this is too many to follow
SHORT_NAME = 34  # journal titles longer than this show as their abbreviation
SMALL_WORDS = {"of", "and", "in", "the", "for", "on", "at", "to", "a", "an", "&"}


def key_for(kind, query):
    return f"{kind}:{query.strip().lower()}"


def make(kind, query, name, broad=False, base=False, added=False):
    key = key_for(kind, query)
    return {
        "key": key,
        "id": short_id(key),  # for Telegram buttons
        "kind": kind,  # biorxiv, medrxiv or pubmed
        "query": query,  # the preprint category, or the journal's PubMed abbreviation
        "name": name,
        "broad": broad,  # a general journal: only its brain papers
        "base": base,  # always fetched
        "added": added,  # added by someone with /sources
    }


def sources(cfg, store=None):
    """Every source people can choose, in the order /sources lists them."""
    base, more = cfg["sources"], cfg.get("more_sources", {})
    preprints, journals = [], []
    biorxiv = base.get("biorxiv", {})
    if biorxiv.get("enabled", True):
        category = biorxiv.get("category", "neuroscience")
        preprints.append(make("biorxiv", category, f"bioRxiv · {category.capitalize()}", base=True))
    for key, name in more.get("preprints", {}).items():
        server, _, category = key.partition(":")
        if server in PREPRINTS and category:
            preprints.append(make(server, category, name))
    pubmed = base.get("pubmed", {})
    if pubmed.get("enabled", True):
        journals += [make("pubmed", abbr, name, base=True) for abbr, name in pubmed.get("journals", {}).items()]
    journals += [make("pubmed", abbr, name) for abbr, name in more.get("journals", {}).items()]
    journals += [make("pubmed", abbr, name, broad=True) for abbr, name in more.get("general_journals", {}).items()]
    added = [make(r["kind"], r["query"], r["name"], bool(r["broad"]), added=True) for r in store.added_sources()] if store else []
    unique = {}
    for s in preprints + journals + added:
        unique.setdefault(s["key"], s)
    return list(unique.values())


def base_keys(cfg):
    """The sources the bot always fetches, whether or not anyone has them ticked."""
    return [s["key"] for s in sources(cfg) if s["base"]]


# ---------- adding a journal ----------
def past_year():
    today = now_utc().date()
    return f"{today - timedelta(days=365):%Y/%m/%d}:{today:%Y/%m/%d}[edat]"


def plain(text):
    """'The Journal of neuroscience : the official journal...' -> 'journal of neuroscience'"""
    text = re.sub(r"^the\s+", "", re.split(r"\s[:=]\s", text.lower())[0])
    return " ".join(re.findall(r"[a-z0-9]+", text))


def display_name(title, abbr):
    """A journal's title, tidied and capitalized, or its abbreviation when the title is long."""
    title = re.sub(r"^the\s+", "", re.split(r"\s[:=]\s", clean_text(title))[0].strip(" ."), flags=re.I)
    words = [
        w if w != w.lower() or (i and w in SMALL_WORDS) else w[:1].upper() + w[1:]  # leaves "eLife" and "PloS" alone
        for i, w in enumerate(title.split())
    ]
    name = " ".join(words)
    return name if name and len(name) <= SHORT_NAME else abbr


def find_journals(query, limit=5, api_key=None):
    """Journals in PubMed that match what someone typed, closest first:
    [{"abbr", "title", "per_month"}], counting only journals with papers in the past year.

    An exact match (full title, abbreviation or ISSN) wins; otherwise it searches the NLM Catalog,
    PubMed's list of journals, by the words of the title.
    """
    api_key = api_key or os.getenv("NCBI_API_KEY")
    query = " ".join(query.replace('"', " ").split())[:120]
    if not re.search(r"[A-Za-z0-9]", query):
        return []
    window = past_year()
    exact = eutils(
        "esearch.fcgi", api_key, db="pubmed", term=f'"{query}"[ta] AND hasabstract AND {window}', retmode="json", retmax=1
    ).json()["esearchresult"]
    if int(exact["count"]):
        uid = exact["idlist"][0]
        doc = eutils("esummary.fcgi", api_key, db="pubmed", id=uid, retmode="json").json()["result"][uid]
        return [{"abbr": doc["source"], "title": doc.get("fulljournalname") or doc["source"], "per_month": int(exact["count"]) / 12}]

    words = [w for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 2 or w.isdigit()]
    if not words:
        return []
    term = " AND ".join(f"{w}[Title]" for w in words) + " AND ncbijournals[filter]"
    ids = eutils("esearch.fcgi", api_key, db="nlmcatalog", term=term, retmode="json", retmax=100).json()["esearchresult"]["idlist"]
    if not ids:
        return []
    docs = eutils("esummary.fcgi", api_key, db="nlmcatalog", id=",".join(ids), retmode="json").json()["result"]
    titles = {}
    for uid in docs.get("uids", []):
        d = docs[uid]
        if d.get("medlineta") and d.get("titlemainlist") and d.get("endyear") == "9999":  # still publishing
            titles.setdefault(d["medlineta"], d["titlemainlist"][0]["title"])
    wanted = plain(query)

    def closeness(item):
        abbr, title = item
        return max(difflib.SequenceMatcher(None, wanted, plain(t)).ratio() for t in (title, abbr))

    found = []
    for abbr, title in sorted(titles.items(), key=closeness, reverse=True)[: limit + 3]:  # spares, for quiet journals
        n = pubmed_count(f'"{abbr}"[ta] AND hasabstract AND {window}', api_key)
        if n:
            found.append({"abbr": abbr, "title": clean_text(title).rstrip("."), "per_month": n / 12})
        if len(found) == limit:
            break
    return found


def journal_details(abbr, api_key=None):
    """How many papers a journal published a month over the past year, how many were about the brain,
    and whether that makes it a general journal; None if it published nothing."""
    api_key = api_key or os.getenv("NCBI_API_KEY")
    window = past_year()
    total = pubmed_count(f'"{abbr}"[ta] AND hasabstract AND {window}', api_key)
    if not total:
        return None
    brain = pubmed_count(f'"{abbr}"[ta] AND hasabstract AND ({NEURO_FILTER}) AND {window}', api_key)
    return {"per_month": total / 12, "brain_per_month": brain / 12, "broad": brain < GENERAL_SHARE * total}


def main():
    query = " ".join(sys.argv[1:]) or "journal of cognitive neuroscience"
    found = find_journals(query)
    if not found:
        print(f"Nothing in PubMed for {query!r}")
    for j in found:
        details = journal_details(j["abbr"])
        kind = f"general: {details['brain_per_month']:.0f} brain papers a month" if details["broad"] else "neuroscience"
        print(f"{display_name(j['title'], j['abbr']):26} {j['abbr']:24} {j['per_month']:5.0f} a month ({kind})")


if __name__ == "__main__":
    main()
