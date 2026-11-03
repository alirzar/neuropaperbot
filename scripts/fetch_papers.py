"""Fetch new neuroscience papers from bioRxiv and PubMed.

Both are free public APIs:
  bioRxiv  api.biorxiv.org: one subject category, first versions only (no revisions)
  PubMed   NCBI E-utilities: the journals in config.json, by the date added to PubMed

Every paper comes back in one shape:
  {doi, source, venue, title, authors (last names), date, link, abstract, license}

Run it on its own to see what a batch would find (saves nothing):
  python scripts/fetch_papers.py
  python scripts/fetch_papers.py --start 2026-10-26 --end 2026-11-01
"""
import argparse
import os
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, timedelta

import requests

from common import clean_text, load_config, now_utc, shorten

BIORXIV = "https://api.biorxiv.org/details/biorxiv/{start}/{end}/{cursor}"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TIMEOUT = 60

session = requests.Session()
session.headers["User-Agent"] = "neuropaperbot/0.1"


def fetch_biorxiv(start, end, category):
    papers, cursor, total = [], 0, 1
    while cursor < total:
        resp = session.get(
            BIORXIV.format(start=start, end=end, cursor=cursor), params={"category": category}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        msg = (data.get("messages") or [{}])[0]
        page = data.get("collection") or []
        if msg.get("status") != "ok" or not page:
            break  # "no posts found" for an empty window
        total = int(msg.get("total") or 0)
        cursor += len(page)  # the API returns 30 records per page
        for p in page:
            if str(p.get("version")) != "1":
                continue  # a revision of an older preprint
            papers.append(
                {
                    "doi": p["doi"],
                    "source": "biorxiv",
                    "venue": "bioRxiv preprint",
                    "title": clean_text(p.get("title")),
                    "authors": biorxiv_authors(p.get("authors")),
                    "date": p.get("date"),
                    "link": f"https://doi.org/{p['doi']}",
                    "abstract": clean_text(p.get("abstract")),
                    "license": p.get("license"),
                }
            )
    return papers


def biorxiv_authors(field):
    """'Smith, J. A.; Doe, B.' -> ['Smith', 'Doe']"""
    names = [part.split(",")[0].strip() for part in (field or "").split(";")]
    return [n for n in names if n]


def fetch_pubmed(start, end, journals, exclude_types, email=None, api_key=None):
    """journals maps PubMed's journal abbreviation (its [ta] search field) to the name shown in posts."""
    params = {"db": "pubmed", "tool": "neuropaperbot"}
    if email:
        params["email"] = email
    if api_key:
        params["api_key"] = api_key
    query = " OR ".join(f'"{abbr}"[ta]' for abbr in journals)
    term = f"({query}) AND {start:%Y/%m/%d}:{end:%Y/%m/%d}[edat]"
    resp = session.get(
        f"{EUTILS}/esearch.fcgi", params={**params, "term": term, "retmode": "json", "retmax": 1000}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    ids = resp.json()["esearchresult"]["idlist"]

    names = {abbr.lower(): name for abbr, name in journals.items()}
    papers = []
    for i in range(0, len(ids), 200):
        time.sleep(0.15 if api_key else 0.4)  # NCBI allows 10 requests/second with a key, 3 without
        resp = session.post(
            f"{EUTILS}/efetch.fcgi", data={**params, "id": ",".join(ids[i : i + 200]), "retmode": "xml"}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        for article in ET.fromstring(resp.content).iter("PubmedArticle"):
            paper = pubmed_paper(article, names, exclude_types)
            if paper:
                papers.append(paper)
    return papers


def pubmed_paper(article, names, exclude_types):
    citation = article.find("MedlineCitation")
    art = citation.find("Article")
    types = {t.text for t in art.iterfind("PublicationTypeList/PublicationType")}
    if types & exclude_types:
        return None
    abstract = clean_text(" ".join(text_of(p) for p in art.iterfind("Abstract/AbstractText")))
    doi = pubmed_doi(article)
    if not abstract or not doi:
        return None  # nothing to rank or hook without an abstract, nothing to link without a DOI
    abbr = citation.findtext("MedlineJournalInfo/MedlineTA") or art.findtext("Journal/ISOAbbreviation") or ""
    authors = [clean_text(a.findtext("LastName") or a.findtext("CollectiveName")) for a in art.iterfind("AuthorList/Author")]
    return {
        "doi": doi,
        "source": "pubmed",
        "venue": names.get(abbr.lower(), abbr),
        "title": clean_text(text_of(art.find("ArticleTitle"))).rstrip("."),
        "authors": [a for a in authors if a],
        "date": entrez_date(article),
        "link": f"https://doi.org/{doi}",
        "abstract": abstract,
        "license": None,
        "pmid": citation.findtext("PMID"),
    }


def text_of(el):
    return "".join(el.itertext()) if el is not None else ""


def pubmed_doi(article):
    for el in article.iterfind("PubmedData/ArticleIdList/ArticleId"):
        if el.get("IdType") == "doi" and el.text:
            return el.text.strip()
    for el in article.iterfind("MedlineCitation/Article/ELocationID"):
        if el.get("EIdType") == "doi" and el.text:
            return el.text.strip()
    return None


def entrez_date(article):
    d = article.find("PubmedData/History/PubMedPubDate[@PubStatus='entrez']")
    if d is None:
        return None
    return f"{d.findtext('Year')}-{int(d.findtext('Month')):02d}-{int(d.findtext('Day')):02d}"


def fetch_papers(cfg, start, end):
    """Every enabled source for the window, one entry per DOI.

    Returns (papers, errors): a source that fails is reported, and the others still count.
    """
    sources = cfg["sources"]
    papers, errors = [], []
    biorxiv = sources.get("biorxiv", {})
    if biorxiv.get("enabled", True):
        try:
            papers += fetch_biorxiv(start, end, biorxiv.get("category", "neuroscience"))
        except Exception as exc:
            errors.append(f"bioRxiv: {exc}")
    pubmed = sources.get("pubmed", {})
    if pubmed.get("enabled", True) and pubmed.get("journals"):
        try:
            papers += fetch_pubmed(
                start,
                end,
                pubmed["journals"],
                set(pubmed.get("exclude_types", [])),
                pubmed.get("email") or None,
                os.getenv("NCBI_API_KEY"),
            )
        except Exception as exc:
            errors.append(f"PubMed: {exc}")
    unique = {}
    for p in papers:
        unique.setdefault(p["doi"].lower(), p)
    return list(unique.values()), errors


def add_window_args(parser):
    parser.add_argument("--start", help="first date, YYYY-MM-DD (default: selection.lookback_days before --end)")
    parser.add_argument("--end", help="last date, YYYY-MM-DD (default: today, UTC)")


def window_from_args(args, cfg):
    end = date.fromisoformat(args.end) if args.end else now_utc().date()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=cfg["selection"].get("lookback_days", 4))
    return start, end


def main():
    parser = argparse.ArgumentParser(description="Show what the fetcher finds for a date window (saves nothing).")
    add_window_args(parser)
    args = parser.parse_args()
    cfg = load_config()
    start, end = window_from_args(args, cfg)

    papers, errors = fetch_papers(cfg, start, end)
    print(f"{start} to {end}: {len(papers)} papers")
    for venue, n in Counter(p["venue"] for p in papers).most_common():
        print(f"  {n:4d}  {venue}")
    samples = [p for p in papers if p["source"] == "biorxiv"][:3] + [p for p in papers if p["source"] == "pubmed"][:3]
    for p in samples:
        print(f"\n  {shorten(p['title'], 100)}\n  {', '.join(p['authors'][:3])} | {p['venue']} | {p['date']} | {p['link']}")
    for err in errors:
        print(f"\nERROR {err}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
