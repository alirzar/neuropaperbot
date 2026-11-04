"""Find a paper from a DOI or link someone pastes, for summaries on demand.

Tries bioRxiv and medRxiv (for their DOIs), then PubMed, then Crossref, which knows nearly every
DOI but often has no abstract.
"""
import os
import re
import xml.etree.ElementTree as ET

from common import clean_text, name_key
from fetch_papers import PREPRINTS, TIMEOUT, biorxiv_record, eutils, pubmed_paper, session

DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
BIORXIV_PREFIXES = ("10.1101/", "10.64898/")


def find_doi(text):
    """The first DOI in text, which can be a bare DOI, a doi.org link or a bioRxiv or medRxiv link."""
    match = DOI.search(text or "")
    if not match:
        return None
    doi = match.group(0).rstrip(".,;:)]}>'\"")
    if doi.startswith(BIORXIV_PREFIXES):
        doi = re.sub(r"v\d+(\.full(\.pdf)?)?$", "", doi)  # their links end in a version, like v2.full
    return doi


def lookup(doi, journals):
    """The paper with this DOI, in the shape every paper has, or None."""
    for find in (from_biorxiv, from_pubmed, from_crossref):
        try:
            paper = find(doi, journals)
        except Exception:
            paper = None  # one source being down shouldn't stop the others
        if paper:
            return paper
    return None


def from_biorxiv(doi, journals):
    if not doi.startswith(BIORXIV_PREFIXES):
        return None
    for server in PREPRINTS:  # medRxiv DOIs look just like bioRxiv's
        rows = session.get(f"https://api.biorxiv.org/details/{server}/{doi}", timeout=TIMEOUT).json().get("collection") or []
        if rows:
            return biorxiv_record(rows[-1], server)  # the latest version
    return None


def from_pubmed(doi, journals):
    api_key = os.getenv("NCBI_API_KEY")
    found = eutils("esearch.fcgi", api_key, db="pubmed", term=f"{doi}[doi]", retmode="json").json()["esearchresult"]["idlist"]
    if not found:
        return None
    xml = eutils("efetch.fcgi", api_key, db="pubmed", id=found[0], retmode="xml").content
    names = {abbr.lower(): name for abbr, name in journals.items()}
    for article in ET.fromstring(xml).iter("PubmedArticle"):
        return pubmed_paper(article, names, set())
    return None


def from_crossref(doi, journals):
    resp = session.get(f"https://api.crossref.org/works/{doi}", timeout=TIMEOUT)
    if resp.status_code != 200:
        return None
    work = resp.json()["message"]
    people = [a for a in work.get("author", []) if a.get("family")]
    parts = (work.get("issued") or {}).get("date-parts") or [[]]
    title = ((work.get("title") or []) + [doi])[0]
    if work.get("subtitle"):  # Crossref keeps "Title: Subtitle" in two fields
        title = f"{title}: {work['subtitle'][0]}"
    return {
        "doi": doi,
        "source": "crossref",
        "venue": clean_text(((work.get("container-title") or []) + [work.get("publisher") or ""])[0]),
        "title": clean_text(title),
        "authors": [a["family"] for a in people],
        "author_keys": [name_key(f"{a['family']}, {a.get('given', '')}") for a in people],
        "date": "-".join(f"{n:02d}" for n in parts[0]) or None,
        "link": f"https://doi.org/{doi}",
        "abstract": clean_text(work.get("abstract")),
        "license": None,
    }
