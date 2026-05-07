from __future__ import annotations

import html
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import bibtexparser
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
ASSET_EXTENSIONS = {".png", ".webp", ".gif", ".svg"}
WEEKDAYS_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def load_yaml(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or default


def load_bibtex(path: Path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return bibtexparser.load(f).entries


def load_data():
    config = load_yaml(DATA_DIR / "config.yaml", {})
    rankings = load_yaml(DATA_DIR / "rankings.yaml", {})
    published = load_bibtex(DATA_DIR / "papers.bib")
    preprints = load_bibtex(DATA_DIR / "preprints.bib")
    return config, rankings, published, preprints


def clean_tex(value: str | None) -> str:
    value = value or ""
    value = value.replace("\n", " ")
    value = re.sub(r"[{}]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_abstract(value: str | None) -> str:
    text = html.unescape(clean_tex(value))
    text = text.replace("\\mathrmmAP \\text@ .5. .95", "mAP@.5:.95")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def year_key(value: str | None) -> int:
    match = re.search(r"\d{4}", str(value or ""))
    return int(match.group(0)) if match else 0


def paper_rank_priority(tags: list[str]) -> int:
    tag_text = " ".join(tags).upper()
    priority = [
        ("CCF A", 0),
        ("JCR Q1", 1),
        ("JCR 1", 1),
        ("CCF B", 2),
        ("JCR Q2", 3),
        ("JCR 2", 3),
        ("CCF C", 4),
        ("JCR Q3", 5),
        ("JCR 3", 5),
        ("JCR Q4", 6),
        ("JCR 4", 6),
    ]
    for marker, rank in priority:
        if marker in tag_text:
            return rank
    return 99


def normalize_person(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower())


def self_name_variants(info: dict) -> set[str]:
    english_name = clean_tex(info.get("name", {}).get("en", ""))
    variants = {normalize_person(english_name)}
    parts = english_name.split()
    if len(parts) >= 2:
        variants.add(normalize_person(f"{parts[-1]} {' '.join(parts[:-1])}"))
    return {v for v in variants if v}


def format_author_name(raw_name: str) -> str:
    name = clean_tex(raw_name)
    if "," in name:
        last, first, *_ = [part.strip() for part in name.split(",")]
        if first:
            return f"{first} {last}"
    return name


def format_authors(author_field: str | None, info: dict) -> list[dict]:
    variants = self_name_variants(info)
    authors = []
    for raw_name in re.split(r"\s+and\s+", clean_tex(author_field)):
        if not raw_name:
            continue
        name = format_author_name(raw_name)
        authors.append({"name": name, "is_self": normalize_person(name) in variants})
    return authors


def match_ranking_meta(venue_name: str, rankings: dict) -> dict:
    for key, meta in rankings.items():
        if key.lower() in venue_name.lower() and isinstance(meta, dict):
            return meta
    return {}


def match_rankings(venue_name: str, rankings: dict) -> list[str]:
    return list(match_ranking_meta(venue_name, rankings).get("tags", []))


def paper_links(entry: dict) -> list[dict]:
    links = []
    doi = clean_tex(entry.get("doi"))
    if doi:
        links.append({"label": "DOI", "url": f"https://doi.org/{doi}"})
    url = clean_tex(entry.get("url"))
    if url:
        links.append({"label": "Link", "url": url})
    return links


# 论文结构化字段（与 scripts/profile_wizard.py 中的 META_FIELD_* 对应）
PAPER_META_AUTHORROLE = "authorrole"      # first/cofirst/corresponding/co-corresponding/coauthor
PAPER_META_VENUETIER = "venuetier"        # 自由文本，逗号/分号分隔；优先于 rankings.yaml
PAPER_META_VENUEINDEX = "venueindex"      # 自由文本，逗号分隔（SCI / EI / Scopus / ...）
PAPER_META_ACCEPTSTATUS = "acceptstatus"  # published/accepted/under-review
PAPER_META_IMPACTFACTOR = "impactfactor"  # Journal impact factor for cumulative IF stats
IMPACT_FACTOR_KEYS = (PAPER_META_IMPACTFACTOR, "impact_factor", "impact-factor")

ROLE_LABELS = {
    "first": {"zh": "第一作者", "en": "First Author"},
    "cofirst": {"zh": "共同一作", "en": "Co-first Author"},
    "corresponding": {"zh": "通讯作者", "en": "Corresponding"},
    "co-corresponding": {"zh": "共同通讯", "en": "Co-corresponding"},
    "coauthor": None,
}
STATUS_LABELS = {
    "published": {"zh": "已出版", "en": "Published"},
    "accepted": {"zh": "已录用", "en": "Accepted"},
    "under-review": {"zh": "在投审稿", "en": "Under Review"},
}


def split_meta_list(value: str | None) -> list[str]:
    if not value:
        return []
    raw = clean_tex(value)
    return [seg.strip() for seg in re.split(r"[,;]", raw) if seg.strip()]


def parse_impact_factor(value: object) -> float | None:
    raw = clean_tex(str(value or "")).replace(",", ".")
    matches = re.findall(r"\d+(?:\.\d+)?", raw)
    if not matches:
        return None
    decimals = [match for match in matches if "." in match]
    number = float((decimals or matches)[0])
    return number if number > 0 else None


def format_metric_number(value: float | int | None) -> str:
    if value is None:
        return ""
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    if not text:
        return "0.0"
    return text if "." in text else f"{text}.0"


def paper_impact_factor(entry: dict, ranking_meta: dict) -> float | None:
    for key in IMPACT_FACTOR_KEYS:
        parsed = parse_impact_factor(entry.get(key))
        if parsed is not None:
            return parsed
    for key in IMPACT_FACTOR_KEYS:
        parsed = parse_impact_factor(ranking_meta.get(key))
        if parsed is not None:
            return parsed
    return None


def categorize_tag(tag: str) -> tuple[str, str, str]:
    """Return (category, css_class, display_label). category ∈ {tier, index, other}."""
    upper = tag.upper()
    # Tier
    if "CCF A" in upper:
        return ("tier", "ccf-a", "CCF A")
    if "CCF B" in upper:
        return ("tier", "ccf-b", "CCF B")
    if "CCF C" in upper:
        return ("tier", "ccf-c", "CCF C")
    if "Q1" in upper:
        return ("tier", "jcr-q1", "JCR Q1")
    if "Q2" in upper:
        return ("tier", "jcr-q2", "JCR Q2")
    if "Q3" in upper:
        return ("tier", "jcr-q3", "JCR Q3")
    if "Q4" in upper:
        return ("tier", "jcr-q4", "JCR Q4")
    # Index databases
    if upper in {"SCI", "SCI-E", "EI", "CPCI-S", "CPCI", "SCOPUS", "CSCD"}:
        return ("index", "", upper)
    # Awards / preprints / other
    return ("other", "", tag)


def split_tags_by_category(tags: list[str]) -> dict[str, list]:
    tier: list[dict] = []
    index: list[dict] = []
    other: list[dict] = []
    seen = set()
    for tag in tags:
        key = tag.strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        category, css_class, display = categorize_tag(tag)
        item = {"label": display, "css_class": css_class, "raw": tag}
        if category == "tier":
            tier.append(item)
        elif category == "index":
            index.append(item)
        else:
            other.append(item)
    return {"tier": tier, "index": index, "other": other}


def clean_and_tag_paper(entry: dict, rankings: dict, info: dict, preprint: bool = False) -> dict:
    venue_name = clean_tex(entry.get("journal") or entry.get("booktitle") or "Preprint / Under Review")
    ranking_meta = match_ranking_meta(venue_name, rankings)

    # 等级：优先使用 paper 自带 venuetier，否则查 rankings.yaml
    explicit_tier = split_meta_list(entry.get(PAPER_META_VENUETIER))
    if explicit_tier:
        tier_tags = explicit_tier
    else:
        tier_tags = list(ranking_meta.get("tags", []))

    # 检索：paper 自带 venueindex 全部加进去
    index_tags = split_meta_list(entry.get(PAPER_META_VENUEINDEX))

    tags = list(tier_tags) + list(index_tags)
    if preprint and not tags:
        tags = ["Preprint"]

    # 录用状态
    status = clean_tex(entry.get(PAPER_META_ACCEPTSTATUS)) or (
        "under-review" if preprint else "published"
    )
    status_label = STATUS_LABELS.get(status)

    # 作者身份（即使 author 字段缺失也可独立展示）
    role = clean_tex(entry.get(PAPER_META_AUTHORROLE))
    role_label = ROLE_LABELS.get(role)

    authors = format_authors(entry.get("author"), info)
    is_first = role == "first" or bool(authors and authors[0]["is_self"])
    impact_factor = paper_impact_factor(entry, ranking_meta)

    categorized = split_tags_by_category(tags)
    return {
        "title": clean_tex(entry.get("title")),
        "authors": authors,
        "venue": venue_name,
        "year": clean_tex(entry.get("year")),
        "abstract": clean_abstract(entry.get("abstract")),
        "tags": tags,
        "tier_tags": categorized["tier"],
        "index_tags": categorized["index"],
        "other_tags": categorized["other"],
        "links": paper_links(entry),
        "is_first_author": is_first,
        "role": role or "",
        "role_label": role_label,
        "status": status,
        "status_label": status_label,
        "impact_factor": impact_factor,
        "impact_factor_display": format_metric_number(impact_factor),
    }


def process_all_papers(published_raw, preprints_raw, rankings, info):
    stats = defaultdict(int)
    published_papers = []

    for entry in published_raw:
        paper = clean_and_tag_paper(entry, rankings, info)
        published_papers.append(paper)
        stats["total"] += 1
        if paper["is_first_author"]:
            stats["first_author"] += 1
        # 按 authorrole 字段细分（与 wizard 中 META_FIELD_AUTHORROLE 对齐）
        role = paper.get("role") or ""
        if role == "first":
            stats["first_only"] += 1
        elif role == "cofirst":
            stats["cofirst"] += 1
        elif role == "corresponding":
            stats["corresponding"] += 1
        elif role == "co-corresponding":
            stats["co_corresponding"] += 1
        for tag in paper["tags"]:
            if "CCF" in tag:
                stats["ccf_total"] += 1
            if "CCF A" in tag:
                stats["ccf_a"] += 1
            if "CCF B" in tag:
                stats["ccf_b"] += 1
            if "CCF C" in tag:
                stats["ccf_c"] += 1
            if "JCR Q1" in tag:
                stats["jcr_q1"] += 1
            if "JCR Q2" in tag:
                stats["jcr_q2"] += 1
            if "JCR Q3" in tag:
                stats["jcr_q3"] += 1
            if "JCR Q4" in tag:
                stats["jcr_q4"] += 1
        if paper["impact_factor"] is not None:
            stats["impact_factor_count"] += 1
            stats["impact_factor_total"] += paper["impact_factor"]
    # 聚合：含共一/共通讯
    stats["first_total"] = stats["first_only"] + stats["cofirst"]
    stats["corresponding_total"] = stats["corresponding"] + stats["co_corresponding"]

    preprint_papers = [
        clean_and_tag_paper(entry, rankings, info, preprint=True) for entry in preprints_raw
    ]

    published_papers.sort(
        key=lambda x: (paper_rank_priority(x["tags"]), -year_key(x["year"]), x["title"].lower())
    )
    preprint_papers.sort(key=lambda x: (-year_key(x["year"]), x["title"].lower()))

    stats["q1_q2"] = stats["jcr_q1"] + stats["jcr_q2"]
    stats["impact_factor_total"] = round(float(stats["impact_factor_total"]), 3)
    stats["impact_factor_total_display"] = format_metric_number(stats["impact_factor_total"])
    stats["preprints"] = len(preprint_papers)
    for key in [
        "total",
        "first_author",
        "first_only",
        "cofirst",
        "corresponding",
        "co_corresponding",
        "first_total",
        "corresponding_total",
        "ccf_total",
        "ccf_a",
        "ccf_b",
        "ccf_c",
        "jcr_q1",
        "jcr_q2",
        "jcr_q3",
        "jcr_q4",
        "q1_q2",
        "impact_factor_count",
        "preprints",
    ]:
        stats[key] = int(stats[key])

    return published_papers, preprint_papers, dict(stats)


def linebreaks(value: str | None) -> Markup:
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return Markup("<br>".join(escape(line) for line in lines))


def resolve_output_asset(path_value: str | None) -> Path | None:
    value = str(path_value or "").strip().replace("\\", "/")
    if not value:
        return None
    asset_path = (OUTPUT_DIR / value).resolve()
    try:
        asset_path.relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        return None
    return asset_path


def collect_gallery_assets(decorations: dict) -> list[str]:
    gallery_dir = resolve_output_asset(decorations.get("gallery_dir"))
    if not gallery_dir or not gallery_dir.exists() or not gallery_dir.is_dir():
        return []

    assets = []
    for path in sorted(gallery_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in ASSET_EXTENSIONS:
            assets.append(path.relative_to(OUTPUT_DIR).as_posix())
    return assets


def build():
    config, rankings, papers_raw, preprints_raw = load_data()
    info = config.get("info", {})
    papers, preprints, stats = process_all_papers(papers_raw, preprints_raw, rankings, info)
    publication_years = sorted({paper["year"] for paper in papers if paper["year"]}, reverse=True)
    # Recent Outputs: 4 篇最高等级（先按 tier 排序，再按时间降序）
    recent_work = sorted(
        preprints + papers,
        key=lambda x: (paper_rank_priority(x["tags"]), -year_key(x["year"]), x["title"].lower()),
    )[:4]
    decorations = config.get("decorations") or {}
    decoration_gallery = collect_gallery_assets(decorations)

    env = Environment(
        loader=FileSystemLoader(str(ROOT)),
        autoescape=select_autoescape(enabled_extensions=("html", "xml")),
    )
    env.filters["linebreaks"] = linebreaks
    template = env.get_template("template.html")
    now = datetime.now()

    output_html = template.render(
        info=info,
        bio=config.get("bio", {}),
        research_profile=config.get("research_profile", {}),
        research_directions=config.get("research_directions") or config.get("interests", []),
        interests=config.get("interests", []),
        education=config.get("education", []),
        activities=config.get("activities", []),
        reviewing=config.get("reviewing", {}),
        achievement_summary=config.get("achievement_summary", {}),
        decorations=decorations,
        decoration_gallery=decoration_gallery,
        papers=papers,
        preprints=preprints,
        recent_work=recent_work,
        publication_years=publication_years,
        stats=stats,
        build_year=now.year,
        build_date={
            "zh": f"{now:%Y.%m.%d} {WEEKDAYS_ZH[now.weekday()]}",
            "en": f"{now:%b} {now.day}, {now:%Y}",
        },
    )
    output_html = "\n".join(line.rstrip() for line in output_html.splitlines()) + "\n"

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / "index.html").write_text(output_html, encoding="utf-8")

    impact_note = (
        f", cumulative IF {stats['impact_factor_total_display']}"
        if stats["impact_factor_count"]
        else ""
    )
    print(
        f"Build success: {stats['total']} published papers, "
        f"{stats['preprints']} preprints, {len(publication_years)} publication years"
        f"{impact_note}."
    )


if __name__ == "__main__":
    build()
