"""Synchronize the publication BibTeX files from trusted scholarly sources.

The discovery path deliberately combines identity-oriented sources instead of
scraping Google Scholar: Crossref ORCID, the curated DBLP profile, OpenAlex with
coauthor/affiliation guards, and arXiv with a trusted-coauthor guard. Existing
manual fields are preserved, while authoritative DOI metadata fills authors,
volume, issue, pages, publisher, and publication status.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import bibtexparser
import yaml
from bibtexparser.bwriter import BibTexWriter


ROOT = Path(__file__).resolve().parents[1]
PAPERS_PATH = ROOT / "data" / "papers.bib"
PREPRINTS_PATH = ROOT / "data" / "preprints.bib"
SYNC_CONFIG_PATH = ROOT / "data" / "publication_sync.yaml"
ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
CROSSREF_CACHE: dict[str, dict[str, str]] = {}


def log(message: str) -> None:
    print(f"[publication-sync] {message}")


def fetch(url: str, accept: str = "application/json", retries: int = 3) -> bytes:
    config = load_sync_config()
    email = config.get("profile", {}).get("contact_email", "")
    headers = {
        "Accept": accept,
        "User-Agent": f"zexingzhang.github.io-publication-sync/1.0 (mailto:{email})",
    }
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            if attempt + 1 == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("unreachable")


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch(url).decode("utf-8"))


def load_sync_config() -> dict[str, Any]:
    with SYNC_CONFIG_PATH.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def load_bib(path: Path) -> bibtexparser.bibdatabase.BibDatabase:
    with path.open("r", encoding="utf-8") as stream:
        return bibtexparser.load(stream)


def normalize_doi(value: str | None) -> str:
    doi = html.unescape(str(value or "")).strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    return doi.rstrip(". ")


def normalize_person(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", html.unescape(str(value or "")))
    return re.sub(r"[^a-z]", "", text.lower())


def normalize_title(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", html.unescape(str(value or "")))
    text = re.sub(r"[{}]", "", text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def strip_markup(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\\(?:rev|textit|textbf|emph)\b", "", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("\\pm", "±").replace("\\%", "%").replace("$", "")
    return re.sub(r"\s+", " ", text).strip()


def page_range(value: str | None) -> str:
    pages = str(value or "").strip()
    if re.fullmatch(r"\d+\s*-\s*\d+", pages):
        return re.sub(r"\s*-\s*", "--", pages)
    return pages


def crossref_author_name(author: dict[str, Any]) -> str:
    family = str(author.get("family") or "").strip()
    given = str(author.get("given") or "").strip()
    return f"{family}, {given}" if family and given else family or given


def authors_to_bib(names: list[str]) -> str:
    return " and ".join(name.strip() for name in names if name and name.strip())


def publication_year(record: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = (record.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            return str(parts[0][0])
    return ""


def crossref_entry(record: dict[str, Any], profile_name: str) -> dict[str, str]:
    record_type = str(record.get("type") or "")
    entry_type = "article" if record_type == "journal-article" else "inproceedings"
    titles = record.get("title") or []
    containers = record.get("container-title") or []
    names = [crossref_author_name(author) for author in record.get("author") or []]
    entry: dict[str, str] = {
        "ENTRYTYPE": entry_type,
        "title": strip_markup(titles[0] if titles else ""),
        "author": authors_to_bib(names),
        "year": publication_year(record),
        "doi": normalize_doi(record.get("DOI")),
        "acceptstatus": "published",
    }
    if containers:
        entry["journal" if entry_type == "article" else "booktitle"] = strip_markup(containers[0])
    for remote_key, local_key in (
        ("volume", "volume"),
        ("issue", "number"),
        ("page", "pages"),
        ("publisher", "publisher"),
    ):
        value = record.get(remote_key)
        if value:
            entry[local_key] = page_range(value) if local_key == "pages" else str(value).strip()
    abstract = strip_markup(record.get("abstract"))
    if abstract:
        entry["abstract"] = abstract
    if names:
        entry["authorrole"] = (
            "first" if normalize_person(names[0]) == normalize_person(profile_name) else "coauthor"
        )
    return {key: value for key, value in entry.items() if value}


def crossref_by_doi(doi: str, profile_name: str) -> dict[str, str]:
    normalized = normalize_doi(doi)
    if normalized in CROSSREF_CACHE:
        return CROSSREF_CACHE[normalized]
    encoded = urllib.parse.quote(normalized, safe="")
    payload = fetch_json(f"https://api.crossref.org/works/{encoded}")
    CROSSREF_CACHE[normalized] = crossref_entry(payload["message"], profile_name)
    return CROSSREF_CACHE[normalized]


def crossref_orcid(orcid: str, profile_name: str) -> list[dict[str, str]]:
    query = urllib.parse.urlencode({"filter": f"orcid:{orcid}", "rows": 100})
    payload = fetch_json(f"https://api.crossref.org/works?{query}")
    return [crossref_entry(item, profile_name) for item in payload["message"].get("items", [])]


def inverted_abstract(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    ordered = sorted((position, word) for word, positions in index.items() for position in positions)
    return " ".join(word for _, word in ordered)


def openalex_is_trusted(work: dict[str, Any], config: dict[str, Any]) -> bool:
    target_id = str(config["profile"].get("openalex_author_id") or "").split("/")[-1]
    trusted_people = {normalize_person(name) for name in config.get("trusted_coauthors", [])}
    trusted_affiliations = [name.lower() for name in config.get("trusted_affiliations", [])]
    target_authorship = None
    coauthors: set[str] = set()
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        author_id = str(author.get("id") or "").split("/")[-1]
        if author_id == target_id:
            target_authorship = authorship
        else:
            coauthors.add(normalize_person(author.get("display_name")))
    if not target_authorship:
        return False
    if trusted_people.intersection(coauthors):
        return True
    target_institutions = [
        str(institution.get("display_name") or "").lower()
        for institution in target_authorship.get("institutions") or []
    ]
    return any(
        trusted in institution
        for trusted in trusted_affiliations
        for institution in target_institutions
    )


def openalex_entry(work: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    profile_name = config["profile"]["name"]
    work_type = str(work.get("type") or "")
    entry_type = "article" if work_type in {"article", "preprint"} else "inproceedings"
    authors = [
        str((authorship.get("author") or {}).get("display_name") or "").strip()
        for authorship in work.get("authorships") or []
    ]
    doi = normalize_doi(work.get("doi"))
    source = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
    entry: dict[str, str] = {
        "ENTRYTYPE": entry_type,
        "title": strip_markup(work.get("title")),
        "author": authors_to_bib(authors),
        "year": str(work.get("publication_year") or ""),
        "acceptstatus": "under-review" if work_type == "preprint" else "published",
        "openalex": str(work.get("id") or "").split("/")[-1],
    }
    if doi:
        entry["doi"] = doi
    if source:
        entry["journal" if entry_type == "article" else "booktitle"] = str(source)
    biblio = work.get("biblio") or {}
    for remote_key, local_key in (
        ("volume", "volume"),
        ("issue", "number"),
        ("first_page", "pages"),
    ):
        value = biblio.get(remote_key)
        if value:
            entry[local_key] = str(value)
    if biblio.get("first_page") and biblio.get("last_page"):
        entry["pages"] = f"{biblio['first_page']}--{biblio['last_page']}"
    abstract = inverted_abstract(work.get("abstract_inverted_index"))
    if abstract:
        entry["abstract"] = abstract
    if not doi:
        landing_page = (work.get("primary_location") or {}).get("landing_page_url")
        if landing_page:
            entry["url"] = str(landing_page)
    if authors:
        entry["authorrole"] = (
            "first" if normalize_person(authors[0]) == normalize_person(profile_name) else "coauthor"
        )
    if doi:
        try:
            authoritative = crossref_by_doi(doi, profile_name)
        except (KeyError, ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            authoritative = {}
        for key in ("author", "volume", "number", "pages", "publisher"):
            if authoritative.get(key):
                entry[key] = authoritative[key]
        if not entry.get("journal") and not entry.get("booktitle"):
            venue_key = "journal" if authoritative.get("journal") else "booktitle"
            if authoritative.get(venue_key):
                entry[venue_key] = authoritative[venue_key]
    return {key: value for key, value in entry.items() if value}


def discover_openalex(config: dict[str, Any]) -> list[dict[str, str]]:
    author_id = str(config["profile"]["openalex_author_id"]).split("/")[-1]
    email = config["profile"].get("contact_email", "")
    query = urllib.parse.urlencode(
        {
            "filter": f"author.id:{author_id}",
            "per-page": 200,
            "sort": "publication_date:desc",
            "mailto": email,
        }
    )
    payload = fetch_json(f"https://api.openalex.org/works?{query}")
    return [
        openalex_entry(work, config)
        for work in payload.get("results", [])
        if openalex_is_trusted(work, config)
    ]


def xml_text(element: ET.Element | None) -> str:
    return "" if element is None else strip_markup("".join(element.itertext()))


def discover_dblp(config: dict[str, Any]) -> list[dict[str, str]]:
    pid = config["profile"].get("dblp_pid", "")
    root = ET.fromstring(fetch(f"https://dblp.org/pid/{pid}.xml", accept="application/xml"))
    entries: list[dict[str, str]] = []
    for wrapper in root.findall("r"):
        record = next(iter(wrapper), None)
        if record is None or record.tag not in {"article", "inproceedings", "incollection"}:
            continue
        entry_type = "article" if record.tag == "article" else "inproceedings"
        authors = [xml_text(author) for author in record.findall("author")]
        entry: dict[str, str] = {
            "ENTRYTYPE": entry_type,
            "title": xml_text(record.find("title")),
            "author": authors_to_bib(authors),
            "year": xml_text(record.find("year")),
            "acceptstatus": "published",
            "authorrole": (
                "first"
                if authors and normalize_person(authors[0]) == normalize_person(config["profile"]["name"])
                else "coauthor"
            ),
        }
        venue = xml_text(record.find("journal" if entry_type == "article" else "booktitle"))
        if venue:
            entry["journal" if entry_type == "article" else "booktitle"] = venue
        for source_key, target_key in (
            ("volume", "volume"),
            ("number", "number"),
            ("pages", "pages"),
        ):
            value = xml_text(record.find(source_key))
            if value:
                entry[target_key] = page_range(value)
        editions = [xml_text(ee) for ee in record.findall("ee")]
        doi_url = next((url for url in editions if "doi.org/" in url.lower()), "")
        if doi_url:
            entry["doi"] = normalize_doi(doi_url)
        elif editions:
            entry["url"] = editions[0]
        entries.append({key: value for key, value in entry.items() if value})
    return entries


def discover_arxiv(config: dict[str, Any]) -> list[dict[str, str]]:
    profile_name = config["profile"]["name"]
    trusted_people = {normalize_person(name) for name in config.get("trusted_coauthors", [])}
    query = urllib.parse.urlencode(
        {
            "search_query": f'au:"{profile_name}"',
            "start": 0,
            "max_results": 100,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )
    root = ET.fromstring(fetch(f"https://export.arxiv.org/api/query?{query}", accept="application/atom+xml"))
    entries: list[dict[str, str]] = []
    for item in root.findall("atom:entry", ARXIV_NS):
        authors = [xml_text(author.find("atom:name", ARXIV_NS)) for author in item.findall("atom:author", ARXIV_NS)]
        if normalize_person(profile_name) not in {normalize_person(name) for name in authors}:
            continue
        coauthors = {normalize_person(name) for name in authors if normalize_person(name) != normalize_person(profile_name)}
        if not trusted_people.intersection(coauthors):
            continue
        identifier = xml_text(item.find("atom:id", ARXIV_NS))
        arxiv_id = re.sub(r"v\d+$", "", identifier.rstrip("/").split("/")[-1])
        published = xml_text(item.find("atom:published", ARXIV_NS))
        entry: dict[str, str] = {
            "ENTRYTYPE": "article",
            "title": xml_text(item.find("atom:title", ARXIV_NS)),
            "author": authors_to_bib(authors),
            "year": published[:4],
            "journal": "arXiv preprint",
            "doi": f"10.48550/arXiv.{arxiv_id}",
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "eprint": arxiv_id,
            "archiveprefix": "arXiv",
            "abstract": xml_text(item.find("atom:summary", ARXIV_NS)),
            "acceptstatus": "under-review",
            "authorrole": (
                "first" if authors and normalize_person(authors[0]) == normalize_person(profile_name) else "coauthor"
            ),
        }
        entries.append(entry)
    return entries


def title_match(left: str | None, right: str | None) -> bool:
    left_norm = normalize_title(left)
    right_norm = normalize_title(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    return SequenceMatcher(None, left_norm, right_norm).ratio() >= 0.94


def find_existing(
    candidate: dict[str, str], papers: list[dict[str, str]], preprints: list[dict[str, str]]
) -> tuple[dict[str, str] | None, list[dict[str, str]] | None]:
    candidate_doi = normalize_doi(candidate.get("doi"))
    for collection in (papers, preprints):
        for entry in collection:
            if candidate_doi and candidate_doi == normalize_doi(entry.get("doi")):
                return entry, collection
            if title_match(candidate.get("title"), entry.get("title")):
                return entry, collection
    return None, None


def make_citekey(candidate: dict[str, str], used: set[str]) -> str:
    authors = re.split(r"\s+and\s+", candidate.get("author", ""))
    first_author = authors[0].strip() if authors else "zhang"
    surname = first_author.split(",", 1)[0] if "," in first_author else first_author.split()[-1]
    surname = re.sub(r"[^a-z0-9]", "", surname.lower()) or "zhang"
    words = re.findall(r"[a-z0-9]+", candidate.get("title", "").lower())
    stopwords = {"a", "an", "the", "from", "for", "of", "on", "to", "via", "with"}
    keyword = next((word for word in words if word not in stopwords), "work")[:18]
    base = f"{surname}{candidate.get('year', '')}{keyword}"
    key = base
    suffix = 2
    while key in used:
        key = f"{base}{suffix}"
        suffix += 1
    used.add(key)
    return key


def merge_missing(target: dict[str, str], source: dict[str, str]) -> bool:
    changed = False
    if source.get("acceptstatus") == "published" and target.get("acceptstatus") == "accepted":
        target["acceptstatus"] = "published"
        changed = True
    for key, value in source.items():
        if key in {"ENTRYTYPE", "ID"} or not value:
            continue
        if not target.get(key):
            target[key] = value
            changed = True
    return changed


def add_candidate(
    candidate: dict[str, str],
    papers: list[dict[str, str]],
    preprints: list[dict[str, str]],
    used_keys: set[str],
) -> str:
    if not candidate.get("title") or not candidate.get("year"):
        return "ignored"
    existing, collection = find_existing(candidate, papers, preprints)
    is_preprint = candidate.get("acceptstatus") == "under-review" or candidate.get("journal") == "arXiv preprint"
    if existing is not None and collection is not None:
        if collection is preprints and not is_preprint:
            promoted = dict(candidate)
            merge_missing(promoted, existing)
            promoted["ID"] = existing.get("ID") or make_citekey(promoted, used_keys)
            preprints.remove(existing)
            papers.append(promoted)
            return "promoted"
        return "enriched" if merge_missing(existing, candidate) else "duplicate"
    candidate = dict(candidate)
    candidate["ID"] = make_citekey(candidate, used_keys)
    (preprints if is_preprint else papers).append(candidate)
    return "added-preprint" if is_preprint else "added-paper"


def is_excluded(candidate: dict[str, str], config: dict[str, Any]) -> bool:
    doi = normalize_doi(candidate.get("doi"))
    excluded = {normalize_doi(value) for value in config.get("excluded_dois", [])}
    prefixes = [normalize_doi(value) for value in config.get("ignored_doi_prefixes", [])]
    return doi in excluded or any(doi.startswith(prefix) for prefix in prefixes)


def enrich_existing_dois(entries: list[dict[str, str]], profile_name: str) -> int:
    changes = 0
    for entry in entries:
        doi = normalize_doi(entry.get("doi"))
        if not doi:
            continue
        try:
            remote = crossref_by_doi(doi, profile_name)
        except (KeyError, ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        changed = False
        if remote.get("author") and entry.get("author") != remote["author"]:
            entry["author"] = remote["author"]
            changed = True
        for key in ("volume", "number", "pages"):
            if remote.get(key) and entry.get(key) != remote[key]:
                entry[key] = remote[key]
                changed = True
        for key in ("publisher", "abstract"):
            if remote.get(key) and not entry.get(key):
                entry[key] = remote[key]
                changed = True
        if not entry.get("journal") and not entry.get("booktitle"):
            venue_key = "journal" if remote.get("journal") else "booktitle"
            if remote.get(venue_key):
                entry[venue_key] = remote[venue_key]
                changed = True
        if remote.get("acceptstatus") == "published" and entry.get("acceptstatus") == "accepted":
            entry["acceptstatus"] = "published"
            changed = True
        if changed:
            changes += 1
    return changes


def serialized(database: bibtexparser.bibdatabase.BibDatabase) -> str:
    writer = BibTexWriter()
    return writer.write(database)


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize publication metadata and discover new works.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing BibTeX files.")
    parser.add_argument("--no-discovery", action="store_true", help="Only enrich DOI metadata already in BibTeX.")
    args = parser.parse_args()

    config = load_sync_config()
    profile = config["profile"]
    papers_db = load_bib(PAPERS_PATH)
    preprints_db = load_bib(PREPRINTS_PATH)
    papers = papers_db.entries
    preprints = preprints_db.entries
    original_papers = serialized(papers_db)
    original_preprints = serialized(preprints_db)
    used_keys = {entry["ID"] for entry in papers + preprints}

    enriched = enrich_existing_dois(papers + preprints, profile["name"])
    results: dict[str, int] = {}

    if not args.no_discovery:
        sources = (
            ("Crossref ORCID", lambda: crossref_orcid(profile["orcid"], profile["name"])),
            ("OpenAlex", lambda: discover_openalex(config)),
            ("DBLP", lambda: discover_dblp(config)),
            ("arXiv", lambda: discover_arxiv(config)),
        )
        for source_name, discover in sources:
            try:
                candidates = discover()
            except (ET.ParseError, KeyError, ValueError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                log(f"{source_name} unavailable: {exc}")
                continue
            accepted = 0
            for candidate in candidates:
                if is_excluded(candidate, config):
                    continue
                result = add_candidate(candidate, papers, preprints, used_keys)
                results[result] = results.get(result, 0) + 1
                if result in {"added-paper", "added-preprint", "promoted", "enriched"}:
                    accepted += 1
            log(f"{source_name}: checked {len(candidates)} records, applied {accepted}")

    new_papers = serialized(papers_db)
    new_preprints = serialized(preprints_db)
    changed = new_papers != original_papers or new_preprints != original_preprints
    log(
        f"DOI-enriched entries: {enriched}; papers: {len(papers)}; "
        f"preprints: {len(preprints)}; changed: {'yes' if changed else 'no'}"
    )
    if results:
        log("discovery results: " + ", ".join(f"{key}={value}" for key, value in sorted(results.items())))
    if changed and not args.dry_run:
        PAPERS_PATH.write_text(new_papers, encoding="utf-8")
        PREPRINTS_PATH.write_text(new_preprints, encoding="utf-8")
    if args.dry_run and changed:
        log("dry run: no files written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
