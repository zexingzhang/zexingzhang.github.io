from __future__ import annotations

import argparse
import copy
import difflib
import html
import importlib.util
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable
from urllib import error, parse, request

import bibtexparser
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    track,
)
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


# === 常量 ============================================================

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "config.yaml"
PUBLISHED_BIB_PATH = ROOT / "data" / "papers.bib"
PREPRINT_BIB_PATH = ROOT / "data" / "preprints.bib"
OUTPUT_DIR = ROOT / "output"
DEFAULT_GALLERY = "assets/decorations/gallery"
DEFAULT_OPTIMIZED_GALLERY = "assets/decorations/gallery_optimized"
IMAGE_EXTENSIONS = {".png", ".webp", ".jpg", ".jpeg"}
DEFAULT_WEBP_QUALITY = 52
DEFAULT_MAX_IMAGE_SIDE = 750

console = Console()


# === YAML 读写 ========================================================


class LiteralDumper(yaml.SafeDumper):
    pass


def _represent_string(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


LiteralDumper.add_representer(str, _represent_string)


def dump_config(data: dict[str, Any]) -> str:
    return yaml.dump(data, Dumper=LiteralDumper, sort_keys=False, allow_unicode=True)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(dump_config(data))


def get_nested(data: dict[str, Any], path: str, default: Any = "") -> Any:
    current: Any = data
    for key in path.split("."):
        if isinstance(current, list):
            current = current[int(key)]
        elif isinstance(current, dict):
            current = current.get(key, default)
        else:
            return default
    return current


def set_nested(data: dict[str, Any], path: str, value: Any) -> None:
    current: Any = data
    parts = path.split(".")
    for key in parts[:-1]:
        if isinstance(current, list):
            current = current[int(key)]
        else:
            current = current.setdefault(key, {})
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = value
    else:
        current[last] = value


def zh_en(zh: str = "", en: str = "") -> dict[str, str]:
    return {"zh": zh, "en": en}


# === 终端 UI 原语 =====================================================


def notice(message: str, kind: str = "info") -> None:
    style = {
        "info": "cyan",
        "ok": "green",
        "warn": "yellow",
        "error": "bold red",
    }.get(kind, "white")
    console.print(f"[{style}]{message}[/{style}]")


def section_title(text: str) -> None:
    console.print()
    console.rule(f"[bold cyan]{text}[/bold cyan]", style="cyan")


@contextmanager
def with_spinner(label: str):
    with console.status(f"[cyan]{label}[/cyan]", spinner="dots"):
        yield


@dataclass
class MenuItem:
    key: str
    title: str
    detail: str = ""
    disabled: bool = False
    disabled_reason: str = ""


def render_menu(title: str, items: list[MenuItem]) -> None:
    table = Table(
        title=f"[bold]{title}[/bold]",
        title_justify="left",
        box=None,
        show_header=False,
        padding=(0, 1),
    )
    table.add_column("key", style="bold yellow", no_wrap=True, width=4)
    table.add_column("title", no_wrap=False)
    table.add_column("detail", style="dim", no_wrap=False)
    for item in items:
        if item.disabled:
            key_cell = Text(item.key, style="dim")
            title_cell = Text(
                f"{item.title}  (不可用：{item.disabled_reason})",
                style="dim strike",
            )
            detail_cell = Text(item.detail, style="dim")
        else:
            key_cell = Text(item.key, style="bold yellow")
            title_cell = Text(item.title)
            detail_cell = Text(item.detail, style="dim")
        table.add_row(key_cell, title_cell, detail_cell)
    console.print(table)


def choose_menu(title: str, items: list[MenuItem], default: str | None = None) -> str:
    enabled = {item.key for item in items if not item.disabled}
    all_keys = {item.key for item in items}
    render_menu(title, items)
    suffix = f" [{default}]" if default else ""
    while True:
        choice = Prompt.ask(f"[bold]请选择[/bold]{suffix}", default=default or "").strip()
        if not choice and default and default in enabled:
            return default
        if choice in enabled:
            return choice
        if choice in all_keys:
            notice("该选项当前不可用。", "warn")
            continue
        notice("请输入列表里的编号。", "warn")


def prompt_line(
    label: str,
    default: str = "",
    required: bool = False,
    validator: Callable[[str], str | None] | None = None,
) -> str:
    while True:
        suffix = f" [dim]\\[{default}][/dim]" if default else ""
        value = console.input(f"[cyan]{label}[/cyan]{suffix}: ").strip()
        if not value:
            if default:
                value = default
            elif required:
                notice("这个字段不能为空。", "warn")
                continue
            else:
                return ""
        if validator:
            warning = validator(value)
            if warning:
                notice(warning, "warn")
        return value


def prompt_yes_no(label: str, default: bool = True) -> bool:
    return Confirm.ask(f"[cyan]{label}[/cyan]", default=default)


def prompt_multiline(label: str, default: str = "") -> str:
    console.print(
        f"[cyan]{label}[/cyan] [dim]（多行输入；单独输入 . 结束；"
        "直接回车保留旧值；输入 !clear 清空字段）[/dim]"
    )
    if default:
        console.print("[dim]当前内容：[/dim]")
        console.print(Panel(default.strip(), border_style="dim"))
    first = console.input("[dim]> [/dim]")
    if first == "":
        return default
    if first.strip() == "!clear":
        return ""
    if first == ".":
        return ""
    lines: list[str] = [first.rstrip()]
    while True:
        line = console.input("[dim]> [/dim]")
        if line == ".":
            break
        lines.append(line.rstrip())
    return "\n".join(line for line in lines if line.strip())


def pause() -> None:
    console.input("\n[dim]按回车继续...[/dim]")


# === 校验器（仅给 warning，不拦截）===================================


def validate_email(value: str) -> str | None:
    if value and ("@" not in value or "." not in value.split("@")[-1]):
        return "邮箱格式可疑（缺 @ 或域名）。"
    return None


def validate_url(value: str) -> str | None:
    if value and not value.startswith(("http://", "https://")):
        return "URL 应以 http:// 或 https:// 开头。"
    return None


def validate_github(value: str) -> str | None:
    if value and ("/" in value or value.startswith("http")):
        return "只填用户名，不要带 https://github.com/ 前缀。"
    return None


# === 通用列表编辑器 ==================================================


@dataclass
class ListField:
    key: str
    label: str
    bilingual: bool = True
    required: bool = True


def _empty_item(fields: list[ListField]) -> dict[str, Any]:
    item: dict[str, Any] = {}
    for field in fields:
        item[field.key] = zh_en() if field.bilingual else ""
    return item


def _summarize_item(item: dict[str, Any], fields: list[ListField]) -> str:
    parts: list[str] = []
    for field in fields:
        value = item.get(field.key)
        if isinstance(value, dict):
            text = value.get("zh") or value.get("en") or ""
        else:
            text = str(value or "")
        text = text.replace("\n", " ").strip()
        if text:
            parts.append(text if len(text) <= 40 else text[:38] + "…")
    return "  ·  ".join(parts) if parts else "[空]"


def _show_list(items: list[dict[str, Any]], fields: list[ListField]) -> None:
    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column("idx", style="bold yellow", width=4)
    table.add_column("summary")
    if not items:
        console.print("[dim]目前没有任何条目。[/dim]")
        return
    for idx, item in enumerate(items, 1):
        table.add_row(str(idx), _summarize_item(item, fields))
    console.print(table)


def _edit_item(item: dict[str, Any], fields: list[ListField]) -> dict[str, Any]:
    updated = copy.deepcopy(item)
    for field in fields:
        if field.bilingual:
            current_zh = (updated.get(field.key) or {}).get("zh", "")
            current_en = (updated.get(field.key) or {}).get("en", "")
            new_zh = prompt_line(f"{field.label} (中文)", current_zh, required=field.required)
            en_default = current_en
            new_en = prompt_line(
                f"{field.label} (英文，留空保留旧值)",
                en_default,
                required=False,
            )
            updated[field.key] = zh_en(new_zh, new_en)
        else:
            current = str(updated.get(field.key) or "")
            updated[field.key] = prompt_line(field.label, current, required=field.required)
    return updated


def _pick_index(items: list[dict[str, Any]], action: str) -> int | None:
    if not items:
        notice("当前列表为空。", "warn")
        return None
    raw = prompt_line(f"输入要{action}的编号（回车取消）")
    if not raw:
        return None
    try:
        idx = int(raw) - 1
    except ValueError:
        notice("请输入数字。", "warn")
        return None
    if not 0 <= idx < len(items):
        notice("编号超出范围。", "warn")
        return None
    return idx


def list_editor(
    config: dict[str, Any],
    list_path: str,
    fields: list[ListField],
    title: str,
) -> None:
    parent_path, _, leaf = list_path.rpartition(".")
    parent = config
    if parent_path:
        for key in parent_path.split("."):
            parent = parent.setdefault(key, {})
    items: list[dict[str, Any]] = parent.setdefault(leaf, [])

    while True:
        section_title(title)
        _show_list(items, fields)
        choice = choose_menu(
            "操作",
            [
                MenuItem("1", "编辑某条", "选编号 → 字段循环修改，回车保留旧值。"),
                MenuItem("2", "新增一条", "追加到列表末尾。"),
                MenuItem("3", "删除某条", "选编号删除；操作后不可撤销。"),
                MenuItem("4", "上移", "将某条上移一位。"),
                MenuItem("5", "下移", "将某条下移一位。"),
                MenuItem("0", "返回主菜单", ""),
            ],
            default="0",
        )
        if choice == "0":
            return
        if choice == "1":
            idx = _pick_index(items, "编辑")
            if idx is not None:
                items[idx] = _edit_item(items[idx], fields)
        elif choice == "2":
            new_item = _edit_item(_empty_item(fields), fields)
            primary = fields[0]
            primary_value = new_item.get(primary.key)
            if isinstance(primary_value, dict):
                if not primary_value.get("zh") and not primary_value.get("en"):
                    notice("主字段为空，已取消新增。", "warn")
                    continue
            elif not primary_value:
                notice("主字段为空，已取消新增。", "warn")
                continue
            items.append(new_item)
        elif choice == "3":
            idx = _pick_index(items, "删除")
            if idx is not None:
                summary = _summarize_item(items[idx], fields)
                if prompt_yes_no(f"确认删除「{summary}」", False):
                    items.pop(idx)
        elif choice == "4":
            idx = _pick_index(items, "上移")
            if idx is not None and idx > 0:
                items[idx - 1], items[idx] = items[idx], items[idx - 1]
            elif idx == 0:
                notice("已经在最上面了。", "warn")
        elif choice == "5":
            idx = _pick_index(items, "下移")
            if idx is not None and idx < len(items) - 1:
                items[idx], items[idx + 1] = items[idx + 1], items[idx]
            elif idx == len(items) - 1:
                notice("已经在最下面了。", "warn")


EDUCATION_FIELDS = [
    ListField("school", "学校", bilingual=True, required=True),
    ListField("degree", "学位/身份", bilingual=True, required=True),
    ListField("time", "时间（如 2026 - Present）", bilingual=False, required=True),
]

ACTIVITY_FIELDS = [
    ListField("date", "日期（如 2026.01）", bilingual=False, required=True),
    ListField("title", "活动描述（如 参加 AAAI 汇报）", bilingual=True, required=True),
    ListField("role", "地点 / 角色（如 新加坡 或 程序委员）", bilingual=True, required=True),
]


# === 简介 / 基础信息 / 兴趣 / 审稿 ===================================


def collect_interests(current: list[dict[str, str]]) -> list[dict[str, str]]:
    default = "，".join(item.get("zh", "") for item in current if item.get("zh"))
    line = prompt_line("研究兴趣（中文，用逗号分隔）", default)
    items = [
        item.strip()
        for item in line.replace("、", "，").replace(",", "，").split("，")
        if item.strip()
    ]
    old_en = {item.get("zh", ""): item.get("en", "") for item in current}
    return [zh_en(item, old_en.get(item, "")) for item in items]


def edit_basic_info(config: dict[str, Any]) -> None:
    info = config.setdefault("info", {})
    info.setdefault("name", {})
    info.setdefault("title", {})
    info.setdefault("academic_year", {})
    info.setdefault("location", {})
    config.setdefault("bio", {})

    section_title("快速录入基础信息")
    notice("回车保留旧值；只改你想改的字段。", "info")

    set_nested(config, "info.name.zh", prompt_line("中文姓名", get_nested(config, "info.name.zh"), True))
    set_nested(config, "info.name.en", prompt_line("英文姓名", get_nested(config, "info.name.en"), True))
    set_nested(config, "info.title.zh", prompt_line("中文身份/标题", get_nested(config, "info.title.zh"), True))
    set_nested(
        config,
        "info.academic_year.zh",
        prompt_line("博士年级（如 博一）", get_nested(config, "info.academic_year.zh", "博一")),
    )
    set_nested(
        config,
        "info.email",
        prompt_line("邮箱", get_nested(config, "info.email"), validator=validate_email),
    )
    set_nested(
        config,
        "info.github",
        prompt_line("GitHub 用户名", get_nested(config, "info.github"), validator=validate_github),
    )
    set_nested(
        config,
        "info.scholar",
        prompt_line("Google Scholar 链接", get_nested(config, "info.scholar"), validator=validate_url),
    )
    set_nested(config, "info.location.zh", prompt_line("中文所在地", get_nested(config, "info.location.zh")))

    if prompt_yes_no("是否编辑研究简介 (bio.zh)", False):
        set_nested(config, "bio.zh", prompt_multiline("中文简介", get_nested(config, "bio.zh")))
    if prompt_yes_no("是否更新研究兴趣关键词", False):
        config["interests"] = collect_interests(config.get("interests", []))


def edit_reviewing(config: dict[str, Any]) -> None:
    section_title("维护审稿服务")
    reviewing = config.setdefault("reviewing", {})
    reviewing.setdefault("role", {})
    current_venues = "，".join(str(item) for item in reviewing.get("venues", []) if item)
    venues_line = prompt_line("审稿期刊/会议（用逗号分隔）", current_venues)
    reviewing["venues"] = [
        item.strip()
        for item in venues_line.replace("、", "，").replace(",", "，").split("，")
        if item.strip()
    ]
    set_nested(
        config,
        "reviewing.role.zh",
        prompt_line(
            "中文服务角色",
            get_nested(config, "reviewing.role.zh", "审稿服务") or "审稿服务",
        ),
    )


# === 中英文对照编辑 ===================================================


def _enumerate_bilingual_paths(config: dict[str, Any]) -> list[tuple[str, str]]:
    """Return list of (zh_path, en_path) for fields a user may want to override."""
    entries: list[tuple[str, str]] = []

    def add(zh_path: str, en_path: str) -> None:
        zh = get_nested(config, zh_path, "")
        en = get_nested(config, en_path, "")
        if zh or en:
            entries.append((zh_path, en_path))

    add("info.title.zh", "info.title.en")
    add("info.academic_year.zh", "info.academic_year.en")
    add("info.name.zh", "info.name.en")
    add("info.location.zh", "info.location.en")
    add("bio.zh", "bio.en")
    add("research_profile.zh", "research_profile.en")
    add("achievement_summary.zh", "achievement_summary.en")
    for idx, _ in enumerate(config.get("interests", [])):
        add(f"interests.{idx}.zh", f"interests.{idx}.en")
    for idx, _ in enumerate(config.get("research_directions", [])):
        add(f"research_directions.{idx}.zh", f"research_directions.{idx}.en")
    for idx, _ in enumerate(config.get("education", [])):
        add(f"education.{idx}.school.zh", f"education.{idx}.school.en")
        add(f"education.{idx}.degree.zh", f"education.{idx}.degree.en")
    for idx, _ in enumerate(config.get("activities", [])):
        add(f"activities.{idx}.title.zh", f"activities.{idx}.title.en")
        add(f"activities.{idx}.role.zh", f"activities.{idx}.role.en")
    add("reviewing.role.zh", "reviewing.role.en")
    return entries


def _truncate(text: str, width: int = 36) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def edit_bilingual_overrides(config: dict[str, Any]) -> None:
    while True:
        section_title("对照编辑中英文")
        entries = _enumerate_bilingual_paths(config)
        if not entries:
            notice("当前没有可编辑的双语字段。", "warn")
            return
        table = Table(box=None, show_header=True, padding=(0, 1), header_style="bold cyan")
        table.add_column("idx", style="bold yellow", width=4)
        table.add_column("字段", style="cyan")
        table.add_column("中文")
        table.add_column("英文")
        for idx, (zh_path, en_path) in enumerate(entries, 1):
            label = zh_path.removesuffix(".zh")
            zh_value = str(get_nested(config, zh_path, "") or "")
            en_value = str(get_nested(config, en_path, "") or "")
            en_cell = Text(_truncate(en_value)) if en_value else Text("（空）", style="dim")
            table.add_row(str(idx), label, _truncate(zh_value), en_cell)
        console.print(table)
        raw = prompt_line("输入要编辑的编号（回车返回）")
        if not raw:
            return
        try:
            idx = int(raw) - 1
        except ValueError:
            notice("请输入数字。", "warn")
            continue
        if not 0 <= idx < len(entries):
            notice("编号超出范围。", "warn")
            continue
        zh_path, en_path = entries[idx]
        zh_value = str(get_nested(config, zh_path, "") or "")
        en_value = str(get_nested(config, en_path, "") or "")
        is_long = "\n" in zh_value or "\n" in en_value or len(zh_value) > 60 or len(en_value) > 80
        if is_long:
            new_zh = prompt_multiline(f"{zh_path}（中文）", zh_value)
            new_en = prompt_multiline(f"{en_path}（英文）", en_value)
        else:
            new_zh = prompt_line(f"{zh_path}（中文）", zh_value)
            new_en = prompt_line(f"{en_path}（英文）", en_value)
        set_nested(config, zh_path, new_zh)
        set_nested(config, en_path, new_en)


# === 启动预检 + 状态面板 =============================================


def detect_capabilities() -> dict[str, bool]:
    return {
        "codex": shutil.which("codex") is not None,
        "git": shutil.which("git") is not None,
        "pillow": importlib.util.find_spec("PIL") is not None,
    }


def _bib_stats(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    try:
        database = load_bib(path)
    except Exception:
        return 0, 0
    entries = database.entries
    missing = sum(1 for entry in entries if not clean_text(entry.get("abstract")))
    return len(entries), missing


def render_status(config: dict[str, Any], state: dict[str, bool], dirty: bool) -> None:
    name_zh = get_nested(config, "info.name.zh", "(未填)")
    name_en = get_nested(config, "info.name.en", "(no name)")
    title_zh = get_nested(config, "info.title.zh", "(未填身份)")
    academic_year_zh = get_nested(config, "info.academic_year.zh", "")
    email = get_nested(config, "info.email", "")
    github = get_nested(config, "info.github", "")
    edu_count = len(config.get("education", []) or [])
    act_count = len(config.get("activities", []) or [])
    int_count = len(config.get("interests", []) or [])
    venue_count = len(get_nested(config, "reviewing.venues", []) or [])
    pub_total, pub_missing = _bib_stats(PUBLISHED_BIB_PATH)
    pre_total, pre_missing = _bib_stats(PREPRINT_BIB_PATH)
    gallery_dir_rel = gallery_source(config)
    gallery_imgs = len(list_gallery_images(output_relative_path(gallery_dir_rel)))

    def cap(name: str, ok: bool) -> str:
        mark = "[green]✔[/green]" if ok else "[red]✘[/red]"
        return f"{name} {mark}"

    contact_bits = []
    if email:
        contact_bits.append(email)
    if github:
        contact_bits.append(f"GitHub @{github}")
    contact_line = "  ·  ".join(contact_bits) if contact_bits else "[dim](邮箱 / GitHub 未填)[/dim]"

    dirty_mark = (
        "[yellow]●  有未保存改动[/yellow]" if dirty else "[green]已与磁盘一致[/green]"
    )
    pub_missing_str = f" ({pub_missing} 缺摘要)" if pub_missing else ""
    pre_missing_str = f" ({pre_missing} 缺摘要)" if pre_missing else ""
    profile_bits = [str(title_zh)]
    if academic_year_zh:
        profile_bits.append(str(academic_year_zh))
    body = (
        f"[bold cyan]{name_zh}[/bold cyan] / {name_en}  ·  {' · '.join(profile_bits)}\n"
        f"{contact_line}\n\n"
        f"教育 [bold]{edu_count}[/bold] 条 │ "
        f"活动 [bold]{act_count}[/bold] 条 │ "
        f"兴趣 [bold]{int_count}[/bold] 个 │ "
        f"审稿场所 [bold]{venue_count}[/bold] 处\n"
        f"论文 [bold]{pub_total}[/bold] 篇{pub_missing_str} │ "
        f"预印本 [bold]{pre_total}[/bold] 篇{pre_missing_str} │ "
        f"装饰图 [bold]{gallery_imgs}[/bold] 张 ({gallery_dir_rel})\n\n"
        f"环境  {cap('codex', state['codex'])}   "
        f"{cap('git', state['git'])}   "
        f"{cap('Pillow', state['pillow'])}    {dirty_mark}"
    )
    console.print(Panel(body, title="[bold]状态[/bold]", border_style="cyan"))


# === Codex 调用封装 ==================================================


def extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json").strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Codex 没有返回 JSON。")
    return json.loads(stripped[start : end + 1])


def strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Codex CLI uses strict structured outputs; explicit object props must be required.

    Optional fields are represented as empty strings in our schemas. This keeps the
    output predictable and avoids API-side schema rejections in recent Codex CLI builds.
    """
    normalized = json.loads(json.dumps(schema))

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties.keys())
                node.setdefault("additionalProperties", False)
                for child in properties.values():
                    visit(child)
            additional = node.get("additionalProperties")
            if isinstance(additional, dict):
                visit(additional)
        if node.get("type") == "array":
            visit(node.get("items"))
        for key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(key, []) or []:
                visit(child)

    visit(normalized)
    return normalized


def _run_codex_subprocess(
    prompt: dict[str, Any],
    schema: dict[str, Any],
    model: str | None,
    output_name: str,
    timeout: int,
) -> dict[str, Any]:
    # Windows 上 codex 通常是 npm 装的 .CMD 包装器；
    # 直接传 "codex" 给 subprocess 会 WinError 2，必须用 which 拿完整路径。
    codex_exe = shutil.which("codex")
    if codex_exe is None:
        raise RuntimeError("未找到 codex 命令，请先安装并登录 Codex CLI。")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        output_path = temp_path / output_name
        schema_path = temp_path / "schema.json"
        schema_path.write_text(
            json.dumps(strict_output_schema(schema), ensure_ascii=False),
            encoding="utf-8",
        )

        cmd = [
            codex_exe,
            "exec",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--ephemeral",
            "-C",
            str(ROOT),
            "--output-last-message",
            str(output_path),
            "--output-schema",
            str(schema_path),
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append("-")

        result = subprocess.run(
            cmd,
            input=json.dumps(prompt, ensure_ascii=False, indent=2),
            text=True,
            encoding="utf-8",
            errors="replace",  # Codex CLI 在中文 Windows 下会混入 GBK 字节，不替换会让 reader 线程崩
            capture_output=True,
            cwd=ROOT,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(_codex_error_summary(result))

        raw = output_path.read_text(encoding="utf-8") if output_path.exists() else result.stdout
        return extract_json(raw)


def _codex_error_summary(result: subprocess.CompletedProcess[str]) -> str:
    """Extract the most useful diagnostic from a failed codex run.

    Priorities lines containing ERROR / FATAL / quota / usage hints over
    the codex banner (which dominates the first ~12 lines but says nothing).
    Filters out cmd.exe taskkill chatter that npm CLI wrappers emit on Windows.
    """
    def _is_noise(line: str) -> bool:
        # cmd.exe taskkill output (any locale): "SUCCESS:..." or codepage-mojibake
        if line.startswith("SUCCESS:") or line.startswith("ERROR: The process"):
            return True
        # GBK-mojibake replacement chars
        if "�" in line:
            return True
        return False

    def _without_error_prefix(text: str) -> str:
        lines = []
        for line in text.splitlines():
            lines.append(re.sub(r"^(ERROR|FATAL):\s?", "", line).rstrip())
        return "\n".join(lines)

    def _find_error_message(obj: Any) -> list[str]:
        found: list[str] = []
        if isinstance(obj, dict):
            for key in ("message", "code", "type", "param"):
                value = obj.get(key)
                if value:
                    found.append(f"{key}: {value}")
            for value in obj.values():
                found.extend(_find_error_message(value))
        elif isinstance(obj, list):
            for value in obj:
                found.extend(_find_error_message(value))
        return found

    combined = _without_error_prefix("\n".join([result.stderr or "", result.stdout or ""]))
    try:
        parsed_error = extract_json(combined)
    except Exception:  # noqa: BLE001
        parsed_error = None
    if parsed_error:
        details = []
        seen = set()
        for line in _find_error_message(parsed_error):
            if line not in seen:
                details.append(line)
                seen.add(line)
        if details:
            return (
                f"Codex CLI 执行失败 (returncode={result.returncode})\n"
                + "\n".join(details[:12])
            )

    chunks: list[str] = []
    for stream_name, text in (("stderr", result.stderr), ("stdout", result.stdout)):
        if not text:
            continue
        lines = [ln for ln in text.splitlines() if not _is_noise(ln)]
        signal = [
            ln for ln in lines
            if any(tok in ln for tok in ("ERROR", "FATAL", "Error:", "usage limit", "quota", "rate limit"))
            and ln.strip() not in {"ERROR: {", "ERROR: {"}
        ]
        if signal:
            chunks.append(f"[{stream_name}]\n" + "\n".join(signal[:8]))
        else:
            tail = "\n".join(lines[-8:])
            if tail.strip():
                chunks.append(f"[{stream_name} tail]\n{tail}")
    if not chunks:
        chunks.append("(无 stdout/stderr 输出)")
    return f"Codex CLI 执行失败 (returncode={result.returncode})\n" + "\n\n".join(chunks)


def call_codex(
    prompt: dict[str, Any],
    schema: dict[str, Any],
    label: str,
    model: str | None = None,
    output_name: str = "codex_output.json",
    timeout: int = 180,
    show_panel: bool = True,
) -> dict[str, Any]:
    try:
        with with_spinner(f"Codex: {label}"):
            return _run_codex_subprocess(prompt, schema, model, output_name, timeout)
    except subprocess.TimeoutExpired:
        notice(f"Codex 调用超时（{timeout}s）。", "error")
        raise
    except RuntimeError as exc:
        if show_panel:
            console.print(Panel(str(exc), title=f"[red]Codex 失败：{label}[/red]", border_style="red"))
        raise


def translation_targets(config: dict[str, Any]) -> dict[str, str]:
    targets: dict[str, str] = {}

    def add(en_path: str, zh_path: str) -> None:
        value = str(get_nested(config, zh_path, "") or "").strip()
        if value:
            targets[en_path] = value

    add("info.title.en", "info.title.zh")
    add("info.academic_year.en", "info.academic_year.zh")
    add("info.location.en", "info.location.zh")
    add("bio.en", "bio.zh")
    add("research_profile.en", "research_profile.zh")
    add("achievement_summary.en", "achievement_summary.zh")
    for idx, _ in enumerate(config.get("interests", [])):
        add(f"interests.{idx}.en", f"interests.{idx}.zh")
    for idx, _ in enumerate(config.get("research_directions", [])):
        add(f"research_directions.{idx}.en", f"research_directions.{idx}.zh")
    for idx, _ in enumerate(config.get("education", [])):
        add(f"education.{idx}.school.en", f"education.{idx}.school.zh")
        add(f"education.{idx}.degree.en", f"education.{idx}.degree.zh")
    for idx, _ in enumerate(config.get("activities", [])):
        add(f"activities.{idx}.title.en", f"activities.{idx}.title.zh")
        add(f"activities.{idx}.role.en", f"activities.{idx}.role.zh")
    add("reviewing.role.en", "reviewing.role.zh")
    return targets


def translate_with_codex(targets: dict[str, str], model: str | None = None) -> dict[str, str]:
    if not targets:
        return {}
    translation_properties = {key: {"type": "string"} for key in targets}
    prompt = {
        "task": (
            "Translate Chinese academic homepage fields into polished, concise English. "
            "Preserve names, acronyms, paper/conference names, and line breaks. "
            "Return only JSON that matches the requested schema. Keep the same keys."
        ),
        "fields": targets,
    }
    schema = {
        "type": "object",
        "properties": {
            "translations": {
                "type": "object",
                "properties": translation_properties,
                "required": list(translation_properties.keys()),
                "additionalProperties": False,
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    }
    parsed = call_codex(
        prompt,
        schema,
        label=f"翻译 {len(targets)} 个字段",
        model=model,
        output_name="codex_translation.json",
        timeout=180,
    )
    translations = parsed.get("translations", {})
    return {
        key: str(value).strip()
        for key, value in translations.items()
        if key in targets and str(value).strip()
    }


def apply_translations(config: dict[str, Any], translations: dict[str, str]) -> None:
    for path, value in translations.items():
        set_nested(config, path, value)


def translate_config(config: dict[str, Any], model: str | None) -> dict[str, Any]:
    targets = translation_targets(config)
    if not targets:
        notice("没有可翻译的中文字段。", "warn")
        return config
    try:
        translations = translate_with_codex(targets, model)
    except Exception as exc:
        notice(f"翻译未完成：{exc}", "error")
        notice("已保留现有英文内容，可稍后重试。", "info")
    else:
        apply_translations(config, translations)
        notice(f"已写入 {len(translations)} 个英文译文字段。", "ok")
    return config


def _publication_stats_for_codex(config: dict) -> tuple[dict[str, int], list[dict]]:
    """Aggregate publication stats + notable papers, mirroring build.py logic.

    Returns (stats_dict, notable_papers_list) for feeding into Codex prompt.
    """
    stats = {
        "total": 0, "first": 0, "cofirst": 0, "corresponding": 0,
        "co_corresponding": 0, "ccf_a": 0, "ccf_b": 0, "ccf_c": 0,
        "jcr_q1": 0, "jcr_q2": 0, "preprints": 0,
    }
    notable: list[dict] = []
    for bib_path in [PUBLISHED_BIB_PATH, PREPRINT_BIB_PATH]:
        if not bib_path.exists():
            continue
        is_preprint = (bib_path == PREPRINT_BIB_PATH)
        for entry in load_bib(bib_path).entries:
            if is_preprint:
                stats["preprints"] += 1
                continue
            stats["total"] += 1
            role = clean_text(entry.get(META_FIELD_AUTHORROLE) or "")
            if role == "first": stats["first"] += 1
            elif role == "cofirst": stats["cofirst"] += 1
            elif role == "corresponding": stats["corresponding"] += 1
            elif role == "co-corresponding": stats["co_corresponding"] += 1
            tier_upper = clean_text(entry.get(META_FIELD_VENUETIER) or "").upper()
            if "CCF A" in tier_upper: stats["ccf_a"] += 1
            elif "CCF B" in tier_upper: stats["ccf_b"] += 1
            elif "CCF C" in tier_upper: stats["ccf_c"] += 1
            if "Q1" in tier_upper: stats["jcr_q1"] += 1
            elif "Q2" in tier_upper: stats["jcr_q2"] += 1
            # 收集 notable: CCF A / JCR Q1 或 一作/通讯
            if any(k in tier_upper for k in ("CCF A", "JCR Q1")) or role in ("first", "corresponding"):
                notable.append({
                    "title": clean_text(entry.get("title")),
                    "venue": clean_text(entry.get("journal") or entry.get("booktitle") or ""),
                    "year": clean_text(entry.get("year")),
                    "role": role or "coauthor",
                    "tier": clean_text(entry.get(META_FIELD_VENUETIER) or ""),
                })
    return stats, notable[:10]


def auto_classify_research(config: dict[str, Any], model: str | None) -> dict[str, Any]:
    pub_stats, notable_papers = _publication_stats_for_codex(config)
    prompt = {
        "task": (
            "Condense an academic homepage research profile from the given Chinese profile, "
            "keywords, papers, and abstracts. The style must be concise, formal, direct, "
            "frontier-aware, and credible. Avoid marketing slogans, exaggerated claims, and vague filler. "
            "Also produce an achievement_summary paragraph that follows the bio_zh naturally and "
            "summarizes the publication record using the provided publication_stats and notable_papers. "
            "The achievement_summary should mention specific tier counts (e.g. 'CCF A 1 篇 / JCR Q1 N 篇') "
            "and first / corresponding author counts where they apply, in 谦虚客观 tone, no marketing language. "
            "Return Chinese and English fields suitable for a personal academic homepage."
        ),
        "profile": {
            "bio_zh": get_nested(config, "bio.zh", ""),
            "interests": [item.get("zh", "") for item in config.get("interests", [])],
            "papers": paper_context(),
        },
        "publication_stats": pub_stats,
        "notable_papers": notable_papers,
        "requirements": {
            "research_profile_zh": "One sentence, no more than 45 Chinese characters if possible.",
            "research_profile_en": "One concise sentence.",
            "directions": (
                "3 to 5 short direction labels, not full sentences. "
                "Chinese labels should be 6 to 16 Chinese characters where possible, "
                "with no colon and no sentence punctuation. English labels should be 2 to 6 words."
            ),
            "achievement_summary_zh": (
                "One sentence; 50-90 Chinese characters; factual; mention concrete counts "
                "(e.g. 'CCF A 1 篇'); reads like a continuation of bio_zh, not a brag."
            ),
            "achievement_summary_en": "One sentence; 25-45 English words; factual; concrete counts; modest tone.",
        },
    }
    schema = {
        "type": "object",
        "properties": {
            "research_profile": {
                "type": "object",
                "properties": {
                    "zh": {"type": "string"},
                    "en": {"type": "string"},
                },
                "required": ["zh", "en"],
                "additionalProperties": False,
            },
            "research_directions": {
                "type": "array",
                "minItems": 3,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "zh": {"type": "string"},
                        "en": {"type": "string"},
                    },
                    "required": ["zh", "en"],
                    "additionalProperties": False,
                },
            },
            "achievement_summary": {
                "type": "object",
                "properties": {
                    "zh": {"type": "string"},
                    "en": {"type": "string"},
                },
                "required": ["zh", "en"],
                "additionalProperties": False,
            },
        },
        "required": ["research_profile", "research_directions", "achievement_summary"],
        "additionalProperties": False,
    }
    try:
        result = call_codex(
            prompt,
            schema,
            label="凝练研究方向 + 成果摘要",
            model=model,
            output_name="codex_research_classification.json",
            timeout=240,
        )
    except Exception as exc:
        notice(f"自动归类未完成：{exc}", "error")
        return config

    config["research_profile"] = result["research_profile"]
    config["research_directions"] = result["research_directions"]
    config["achievement_summary"] = result["achievement_summary"]
    notice(
        "已写入 research_profile / research_directions / achievement_summary。",
        "ok",
    )
    return config


# === 摘要补全 ========================================================


def clean_text(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = text.replace("\n", " ")
    text = re.sub(r"[{}]", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean_text(value).lower()).strip()


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def user_agent(config: dict[str, Any]) -> str:
    email = str(get_nested(config, "info.email", "") or "").strip()
    if email:
        return f"zexingzhang-homepage-wizard/1.0 (mailto:{email})"
    return "zexingzhang-homepage-wizard/1.0"


def fetch_json(url: str, config: dict[str, Any], timeout: int = 20) -> dict[str, Any]:
    req = request.Request(url, headers={"User-Agent": user_agent(config)})
    try:
        with request.urlopen(req, timeout=timeout) as response:
            data = response.read().decode("utf-8", errors="replace")
    except (error.URLError, TimeoutError, OSError):
        return {}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return {}


def inverted_abstract(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions:
            words.append((int(position), word))
    return clean_text(" ".join(word for _, word in sorted(words)))


def candidate_abstract(candidate: dict[str, Any]) -> str:
    abstract = clean_text(candidate.get("abstract"))
    if abstract:
        return abstract
    return inverted_abstract(candidate.get("abstract_inverted_index"))


def crossref_by_doi(entry: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    doi = clean_text(entry.get("doi"))
    if not doi:
        return {}
    url = f"https://api.crossref.org/works/{parse.quote(doi, safe='')}"
    message = fetch_json(url, config).get("message", {})
    abstract = clean_text(message.get("abstract"))
    return {"abstract": abstract, "source": "Crossref", "doi": doi} if abstract else {}


def crossref_by_title(entry: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    title = clean_text(entry.get("title"))
    if not title:
        return {}
    query = parse.urlencode({"query.title": title, "rows": "5"})
    url = f"https://api.crossref.org/works?{query}"
    items = fetch_json(url, config).get("message", {}).get("items", [])
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in items:
        item_title = " ".join(item.get("title") or [])
        score = title_similarity(title, item_title)
        if score > best_score:
            best = item
            best_score = score
    if not best or best_score < 0.88:
        return {}
    abstract = clean_text(best.get("abstract"))
    if not abstract:
        return {}
    return {"abstract": abstract, "source": "Crossref", "doi": clean_text(best.get("DOI"))}


def openalex_by_title(entry: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    title = clean_text(entry.get("title"))
    if not title:
        return {}
    query = parse.urlencode({"search": title, "per-page": "5"})
    url = f"https://api.openalex.org/works?{query}"
    results = fetch_json(url, config).get("results", [])
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in results:
        score = title_similarity(title, item.get("title", ""))
        if score > best_score:
            best = item
            best_score = score
    if not best or best_score < 0.88:
        return {}
    abstract = candidate_abstract(best)
    if not abstract:
        return {}
    doi = clean_text(str(best.get("doi", "")).removeprefix("https://doi.org/"))
    return {"abstract": abstract, "source": "OpenAlex", "doi": doi}


def semantic_scholar_by_title(entry: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    title = clean_text(entry.get("title"))
    if not title:
        return {}
    query = parse.urlencode(
        {"query": title, "fields": "title,abstract,year,externalIds", "limit": "5"}
    )
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{query}"
    results = fetch_json(url, config).get("data", [])
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in results:
        score = title_similarity(title, item.get("title", ""))
        if score > best_score:
            best = item
            best_score = score
    if not best or best_score < 0.88:
        return {}
    abstract = clean_text(best.get("abstract"))
    if not abstract:
        return {}
    doi = clean_text((best.get("externalIds") or {}).get("DOI"))
    return {"abstract": abstract, "source": "Semantic Scholar", "doi": doi}


def retrieve_abstract(entry: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    providers = [
        crossref_by_doi,
        crossref_by_title,
        openalex_by_title,
        semantic_scholar_by_title,
    ]
    for provider in providers:
        result = provider(entry, config)
        if result.get("abstract"):
            return result
        time.sleep(0.25)
    return {}


def load_bib(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return bibtexparser.load(f)


def save_bib(path: Path, database) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        bibtexparser.dump(database, f)


def fill_abstracts_for_bib(
    bib_path: Path,
    config: dict[str, Any],
    allow_input: bool,
) -> tuple[int, int, int]:
    if not bib_path.exists():
        return 0, 0, 0

    database = load_bib(bib_path)
    entries = database.entries
    missing = [entry for entry in entries if not clean_text(entry.get("abstract"))]
    if not missing:
        notice(f"{bib_path.name}: 所有论文已有摘要。", "ok")
        return 0, 0, 0

    section_title(f"{bib_path.name}: {len(missing)} 篇缺少摘要")
    retrieved = 0
    manual = 0
    skipped = 0
    skip_all_manual = False

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    auto_results: list[tuple[dict[str, Any], dict[str, str]]] = []
    with progress:
        task = progress.add_task("联网检索摘要", total=len(missing))
        for entry in missing:
            result = retrieve_abstract(entry, config)
            auto_results.append((entry, result))
            progress.advance(task)

    for index, (entry, result) in enumerate(auto_results, 1):
        title = clean_text(entry.get("title"))
        year = clean_text(entry.get("year"))
        console.print(f"\n[bold]\\[{index}/{len(missing)}][/bold] {title} [dim]({year})[/dim]")
        if result.get("abstract"):
            entry["abstract"] = result["abstract"]
            if result.get("doi") and not clean_text(entry.get("doi")):
                entry["doi"] = result["doi"]
            notice(f"  ✔ 已从 {result.get('source', 'online')} 检索到摘要。", "ok")
            retrieved += 1
            continue

        if not allow_input or skip_all_manual:
            notice("  · 未检索到，已跳过。", "warn")
            skipped += 1
            continue

        choice = choose_menu(
            "未检索到摘要",
            [
                MenuItem("1", "手动输入", "粘贴论文摘要；单独输入 . 结束。"),
                MenuItem("2", "跳过这篇", "稍后可重新运行脚本补录。"),
                MenuItem("3", "后续全部跳过", "本次只保留已检索到的摘要。"),
            ],
            default="2",
        )
        if choice == "1":
            abstract = prompt_multiline("论文摘要")
            if abstract.strip():
                entry["abstract"] = abstract.strip()
                manual += 1
            else:
                skipped += 1
        elif choice == "3":
            skip_all_manual = True
            skipped += 1
        else:
            skipped += 1

    if retrieved or manual:
        save_bib(bib_path, database)
        notice(f"已写回：{bib_path}", "ok")
    return retrieved, manual, skipped


def fill_missing_abstracts(config: dict[str, Any], allow_input: bool = True) -> None:
    total_retrieved = 0
    total_manual = 0
    total_skipped = 0
    for bib_path in [PUBLISHED_BIB_PATH, PREPRINT_BIB_PATH]:
        retrieved, manual, skipped = fill_abstracts_for_bib(bib_path, config, allow_input)
        total_retrieved += retrieved
        total_manual += manual
        total_skipped += skipped

    section_title("摘要补全完成")
    summary = Table(box=None, show_header=False, padding=(0, 2))
    summary.add_column("kind", style="cyan")
    summary.add_column("count", style="bold")
    summary.add_row("自动检索", str(total_retrieved))
    summary.add_row("手动录入", str(total_manual))
    summary.add_row("暂时跳过", str(total_skipped))
    console.print(summary)


def paper_context(limit: int = 18) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for bib_path in [PUBLISHED_BIB_PATH, PREPRINT_BIB_PATH]:
        if not bib_path.exists():
            continue
        for entry in load_bib(bib_path).entries:
            items.append(
                {
                    "title": clean_text(entry.get("title")),
                    "venue": clean_text(entry.get("journal") or entry.get("booktitle")),
                    "year": clean_text(entry.get("year")),
                    "abstract": clean_text(entry.get("abstract"))[:900],
                }
            )
    return items[:limit]


# === Bib 编辑器（papers.bib / preprints.bib）=========================


BIB_ENTRY_TYPES: list[tuple[str, str]] = [
    ("article", "期刊论文 (article, journal)"),
    ("inproceedings", "会议论文 (inproceedings, booktitle)"),
    ("inbook", "图书章节 (inbook, booktitle)"),
    ("misc", "其它 (misc, howpublished)"),
]

REQUIRED_BIB_FIELDS = ["title", "year"]
# 作者字段在引导式录入里完全靠联网检索；找不到就留空，不渲染到网页。
# 高级编辑中会作为可选字段出现。
TYPE_TO_VENUE_FIELD = {
    "article": "journal",
    "inproceedings": "booktitle",
    "inbook": "booktitle",
    "misc": "howpublished",
}
OPTIONAL_BIB_FIELDS = [
    "author",
    "doi",
    "impactfactor",
    "abstract",
    "pages",
    "volume",
    "number",
    "organization",
    "publisher",
    "url",
]
RESERVED_BIB_KEYS = {"ENTRYTYPE", "ID"}

# === 论文结构化元数据字段约定（Codex / build.py / wizard 三方共用）======
# 这些自定义字段会写到 bib 里，bibtexparser 原样保留，build.py 读出来渲染。
META_FIELD_AUTHORROLE = "authorrole"      # first / cofirst / corresponding / co-corresponding / coauthor
META_FIELD_VENUETIER = "venuetier"        # 文本标签，如 "CCF A" / "JCR Q1"；多个用逗号分隔
META_FIELD_VENUEINDEX = "venueindex"      # 检索情况，逗号分隔，如 "SCI, EI" / "EI" / "Scopus"
META_FIELD_ACCEPTSTATUS = "acceptstatus"  # published / accepted / under-review
META_FIELD_IMPACTFACTOR = "impactfactor"  # 期刊影响因子；build.py 自动累加

# (key, 中文显示, 简短标签)
AUTHOR_ROLES: list[tuple[str, str, str]] = [
    ("first", "第一作者", "一作"),
    ("cofirst", "共同一作", "共一"),
    ("corresponding", "通讯作者", "通讯"),
    ("co-corresponding", "共同通讯", "共通讯"),
    ("coauthor", "合作作者", "合作"),
    ("", "暂不标注", "—"),
]
ROLE_KEY_TO_LABEL = {key: label for key, label, _ in AUTHOR_ROLES}
ROLE_KEY_TO_SHORT = {key: short for key, _, short in AUTHOR_ROLES}

# 录用状态展示（key, 中文, 简短）
ACCEPT_STATUSES: list[tuple[str, str, str]] = [
    ("published", "已正式出版", "已出版"),
    ("accepted", "已录用待出版", "已录用"),
    ("under-review", "在投 / 审稿中", "在投"),
    ("", "暂不标注", "—"),
]
ACCEPT_KEY_TO_LABEL = {k: lab for k, lab, _ in ACCEPT_STATUSES}
ACCEPT_KEY_TO_SHORT = {k: short for k, _, short in ACCEPT_STATUSES}

# 常见的检索系统标签（仅作为 Codex 提示与界面建议；实际值是自由文本）
COMMON_INDEX_TAGS = ["SCI", "SCI-E", "EI", "CPCI-S", "Scopus", "CSCD"]

JOURNAL_HINT_WORDS = {
    "journal", "transactions", "letters", "magazine", "neurocomputing",
    "review", "computing", "research",
}
CONFERENCE_HINT_WORDS = {
    "proceedings", "conference", "workshop", "symposium",
    "icassp", "aaai", "iccv", "cvpr", "neurips", "iclr", "icml",
    "kdd", "sigir", "sigmod", "wsdm", "emnlp", "acl", "naacl",
}


# --- 作者识别与高亮 ----------------------------------------------------


def _normalize_person(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower())


def _self_name_variants(config: dict[str, Any]) -> set[str]:
    english_name = clean_text(get_nested(config, "info.name.en", ""))
    variants = {_normalize_person(english_name)}
    parts = english_name.split()
    if len(parts) >= 2:
        variants.add(_normalize_person(f"{parts[-1]} {' '.join(parts[:-1])}"))
    return {v for v in variants if v}


def _format_one_author(raw: str) -> str:
    name = clean_text(raw)
    if "," in name:
        last, first, *_ = [part.strip() for part in name.split(",")]
        if first:
            return f"{first} {last}"
    return name


def _split_authors(authors_str: str) -> list[str]:
    if not authors_str:
        return []
    return [_format_one_author(a) for a in re.split(r"\s+and\s+", clean_text(authors_str)) if a.strip()]


def _author_position_in_list(authors: list[str], variants: set[str]) -> int:
    for i, name in enumerate(authors):
        if _normalize_person(name) in variants:
            return i
    return -1


def _format_authors_with_highlight(
    authors_str: str, config: dict[str, Any], max_chars: int = 60
) -> Text:
    variants = _self_name_variants(config)
    names = _split_authors(authors_str)
    text = Text()
    used = 0
    for i, name in enumerate(names):
        if i:
            text.append(", ", style="dim")
            used += 2
        is_self = _normalize_person(name) in variants
        chunk = name
        if used + len(chunk) > max_chars and i > 0:
            text.append("…", style="dim")
            break
        text.append(chunk, style="bold cyan" if is_self else "")
        used += len(chunk)
    return text if names else Text("(无作者信息)", style="dim")


def _guess_author_role(authors_str: str, config: dict[str, Any]) -> str:
    variants = _self_name_variants(config)
    names = _split_authors(authors_str)
    if not names:
        return ""
    pos = _author_position_in_list(names, variants)
    if pos == -1:
        return ""
    if pos == 0:
        return "first"
    # In CS, last author is often the corresponding/PI position when there are >2 authors.
    if pos == len(names) - 1 and len(names) > 2:
        return "corresponding"
    return "coauthor"


# --- citekey 与类型推断 ------------------------------------------------


_STOPWORDS = {"a", "an", "the", "of", "for", "and", "with", "on", "in", "to", "from"}


def _generate_citekey(authors_str: str, year: str, title: str, fallback_surname: str = "") -> str:
    names = _split_authors(authors_str)
    if names:
        first = names[0]
        surname = first.split()[-1] if first else ""
    elif fallback_surname:
        surname = fallback_surname.split()[-1]
    else:
        surname = ""
    surname = re.sub(r"[^a-zA-Z]", "", surname).lower() or "anon"
    year_part = re.sub(r"[^0-9]", "", str(year))[:4] or "0000"
    title_clean = re.sub(r"[^a-zA-Z\s]", "", str(title or ""))
    words = [w.lower() for w in title_clean.split() if w.lower() not in _STOPWORDS]
    word = (words[0][:8] if words else "paper")
    return f"{surname}{year_part}{word}"


def _make_unique_citekey(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    for suffix in "abcdefghijk":
        candidate = f"{base}{suffix}"
        if candidate not in existing:
            return candidate
    i = 2
    while f"{base}{i}" in existing:
        i += 1
    return f"{base}{i}"


def _guess_entry_type(venue: str) -> str:
    v = (venue or "").lower()
    if any(kw in v for kw in JOURNAL_HINT_WORDS):
        # Conference if both keywords match (rare), prefer conference cue
        if any(kw in v for kw in CONFERENCE_HINT_WORDS):
            return "inproceedings"
        return "article"
    if any(kw in v for kw in CONFERENCE_HINT_WORDS):
        return "inproceedings"
    return "article"


# --- 联网检索完整元数据 -----------------------------------------------


def _crossref_full(title: str, year: str, config: dict) -> dict:
    if not title:
        return {}
    query = parse.urlencode({"query.title": title, "rows": "5"})
    url = f"https://api.crossref.org/works?{query}"
    items = fetch_json(url, config).get("message", {}).get("items", [])
    best, best_score = None, 0.0
    for item in items:
        item_title = " ".join(item.get("title") or [])
        score = title_similarity(title, item_title)
        if year and clean_text(str(item.get("issued", {}).get("date-parts", [[""]])[0][0])) != str(year):
            score *= 0.95
        if score > best_score:
            best, best_score = item, score
    if not best or best_score < 0.85:
        return {}
    authors_raw = best.get("author") or []
    authors_bib = " and ".join(
        f"{a.get('family','').strip()}, {a.get('given','').strip()}".strip(", ")
        for a in authors_raw if a.get("family")
    )
    container = best.get("container-title") or []
    venue = clean_text(container[0]) if container else ""
    return {
        "source": "Crossref",
        "doi": clean_text(best.get("DOI")),
        "abstract": clean_text(best.get("abstract")),
        "authors": authors_bib,
        "venue": venue,
    }


def _openalex_full(title: str, year: str, config: dict) -> dict:
    if not title:
        return {}
    query_parts = {"search": title, "per-page": "5"}
    if year:
        query_parts["filter"] = f"publication_year:{year}"
    url = f"https://api.openalex.org/works?{parse.urlencode(query_parts)}"
    results = fetch_json(url, config).get("results", [])
    best, best_score = None, 0.0
    for item in results:
        score = title_similarity(title, item.get("title", ""))
        if score > best_score:
            best, best_score = item, score
    if not best or best_score < 0.85:
        return {}
    authorships = best.get("authorships") or []
    names = []
    for ship in authorships:
        author = ship.get("author") or {}
        display = author.get("display_name", "")
        if display:
            parts = display.split()
            if len(parts) >= 2:
                names.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
            else:
                names.append(display)
    authors_bib = " and ".join(names)
    venue = clean_text(
        (best.get("primary_location", {}) or {}).get("source", {}).get("display_name")
        or (best.get("host_venue", {}) or {}).get("display_name")
        or ""
    )
    doi = clean_text(str(best.get("doi", "")).removeprefix("https://doi.org/"))
    abstract = candidate_abstract(best)
    return {
        "source": "OpenAlex",
        "doi": doi,
        "abstract": abstract,
        "authors": authors_bib,
        "venue": venue,
    }


def _semantic_scholar_full(title: str, year: str, config: dict) -> dict:
    if not title:
        return {}
    query_parts = {
        "query": title,
        "fields": "title,abstract,authors,venue,year,externalIds",
        "limit": "5",
    }
    if year:
        query_parts["year"] = str(year)
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{parse.urlencode(query_parts)}"
    results = fetch_json(url, config).get("data", [])
    best, best_score = None, 0.0
    for item in results:
        score = title_similarity(title, item.get("title", ""))
        if score > best_score:
            best, best_score = item, score
    if not best or best_score < 0.85:
        return {}
    names = []
    for a in best.get("authors") or []:
        display = a.get("name", "")
        parts = display.split()
        if len(parts) >= 2:
            names.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
        elif display:
            names.append(display)
    authors_bib = " and ".join(names)
    return {
        "source": "Semantic Scholar",
        "doi": clean_text((best.get("externalIds") or {}).get("DOI")),
        "abstract": clean_text(best.get("abstract")),
        "authors": authors_bib,
        "venue": clean_text(best.get("venue") or ""),
    }


def _retrieve_full_metadata(title: str, year: str, config: dict) -> dict:
    for fn in (_crossref_full, _openalex_full, _semantic_scholar_full):
        try:
            result = fn(title, year, config)
        except Exception:  # noqa: BLE001
            result = {}
        if result.get("authors") or result.get("doi") or result.get("abstract"):
            return result
        time.sleep(0.25)
    return {}


# --- Codex 一键论文元数据 --------------------------------------------


def codex_paper_metadata(
    title: str,
    raw_venue: str,
    year: str,
    config: dict,
    model: str | None,
) -> dict[str, Any]:
    """One Codex call to fill all structured paper metadata.

    Returns a dict matching the schema below. Field naming matches the bib
    custom field names (authorrole / venuetier / venueindex / acceptstatus)
    so the wizard and build.py can speak the same language.
    """
    user_name = clean_text(get_nested(config, "info.name.en", ""))
    prompt = {
        "task": (
            "Given a partial bibliographic entry for an academic paper, fill in the "
            "structured metadata fields described in the schema. Use only your knowledge "
            "of the venue and field — DO NOT invent author lists or paper abstracts (those "
            "come from a separate online retrieval step). When uncertain, leave optional "
            "string fields empty rather than guessing. Always normalize venue names to the "
            "canonical BibTeX form (e.g. 'Proceedings of the AAAI Conference on Artificial "
            "Intelligence' for AAAI). For the user's role, infer based on whether the "
            "user is likely first author / corresponding author for this paper given "
            "the title, year and venue, but only suggest 'first' or 'corresponding' if "
            "you are confident; otherwise leave empty."
        ),
        "input": {
            "title": title,
            "raw_venue": raw_venue,
            "year": year,
            "user_full_name": user_name,
        },
        "field_definitions": {
            "venuetier": "CCF rating (CCF A/B/C) for conferences, JCR quartile (JCR Q1-Q4) for journals. May combine, e.g. 'CCF A; JCR Q1'. Empty if no widely-accepted ranking applies.",
            "venueindex": "Indexing systems for the venue: SCI, SCI-E, EI, CPCI-S, Scopus, CSCD, etc. Comma-separated. Empty if not indexed or unknown.",
            "acceptstatus": "published = formally published, accepted = accepted but not yet in database, under-review = under peer review.",
        },
    }
    schema = {
        "type": "object",
        "properties": {
            "venue_full_name": {"type": "string"},
            "venue_type": {
                "type": "string",
                "enum": ["conference", "journal", "workshop", "preprint", "other"],
            },
            "venuetier": {"type": "string"},
            "venueindex": {"type": "string"},
            "acceptstatus": {
                "type": "string",
                "enum": ["published", "accepted", "under-review", ""],
            },
            "authorrole": {
                "type": "string",
                "enum": ["first", "cofirst", "corresponding", "co-corresponding", "coauthor", ""],
            },
            "doi": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "notes": {"type": "string"},
        },
        "required": ["venue_full_name", "venue_type", "acceptstatus"],
        "additionalProperties": False,
    }
    return call_codex(
        prompt,
        schema,
        label=f"补全元数据: {_truncate(title, 40)}",
        model=model,
        output_name="codex_paper_metadata.json",
        timeout=120,
    )


def _entry_venue_value(entry: dict) -> str:
    venue_field = TYPE_TO_VENUE_FIELD.get(entry.get("ENTRYTYPE", "article"), "journal")
    return clean_text(entry.get(venue_field) or entry.get("journal") or entry.get("booktitle") or "")


def codex_paper_metadata_batch(
    entries: list[dict],
    config: dict,
    model: str | None,
) -> dict[str, dict[str, Any]]:
    """Fill paper metadata for many entries with one Codex call.

    The result is keyed by our temporary numeric id. Missing/uncertain values are
    returned as empty strings, so applying the result never invents author lists or
    abstracts.
    """
    user_name = clean_text(get_nested(config, "info.name.en", ""))
    items = []
    for idx, entry in enumerate(entries, 1):
        items.append(
            {
                "id": str(idx),
                "citekey": clean_text(entry.get("ID") or ""),
                "entry_type": clean_text(entry.get("ENTRYTYPE") or ""),
                "title": clean_text(entry.get("title") or ""),
                "raw_venue": _entry_venue_value(entry),
                "year": clean_text(entry.get("year") or ""),
                "missing_fields": _missing_metadata_keys(entry),
            }
        )
    prompt = {
        "task": (
            "For each bibliographic item, fill structured academic metadata. "
            "Return exactly one result for each input id. Use reliable venue knowledge "
            "and leave uncertain optional fields as empty strings. Do not invent author "
            "lists or abstracts. Normalize venue names to canonical BibTeX-friendly names."
        ),
        "user_full_name": user_name,
        "items": items,
        "field_definitions": {
            "venuetier": "CCF rating (CCF A/B/C) for conferences, JCR quartile (JCR Q1-Q4) for journals. May combine, e.g. 'CCF A; JCR Q1'. Empty if no widely-accepted ranking applies.",
            "venueindex": "Indexing systems for the venue: SCI, SCI-E, EI, CPCI-S, Scopus, CSCD, etc. Comma-separated. Empty if not indexed or unknown.",
            "acceptstatus": "published = formally published, accepted = accepted but not yet in database, under-review = under peer review.",
            "authorrole": "first, cofirst, corresponding, co-corresponding, coauthor, or empty if uncertain.",
        },
    }
    result_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "venue_full_name": {"type": "string"},
            "venue_type": {
                "type": "string",
                "enum": ["conference", "journal", "workshop", "preprint", "other", ""],
            },
            "venuetier": {"type": "string"},
            "venueindex": {"type": "string"},
            "acceptstatus": {
                "type": "string",
                "enum": ["published", "accepted", "under-review", ""],
            },
            "authorrole": {
                "type": "string",
                "enum": ["first", "cofirst", "corresponding", "co-corresponding", "coauthor", ""],
            },
            "doi": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "notes": {"type": "string"},
        },
        "required": [
            "id",
            "venue_full_name",
            "venue_type",
            "venuetier",
            "venueindex",
            "acceptstatus",
            "authorrole",
            "doi",
            "confidence",
            "notes",
        ],
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "minItems": len(items),
                "maxItems": len(items),
                "items": result_schema,
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }
    parsed = call_codex(
        prompt,
        schema,
        label=f"批量补全论文元数据 ({len(items)} 条)",
        model=model,
        output_name="codex_paper_metadata_batch.json",
        timeout=max(180, 30 + 18 * len(items)),
    )
    return {
        clean_text(item.get("id")): item
        for item in parsed.get("results", [])
        if clean_text(item.get("id"))
    }


def _bib_field_value(entry: dict, key: str) -> str:
    return clean_text(entry.get(key) or "")


def _missing_metadata_keys(entry: dict) -> list[str]:
    """Which structured fields are missing — used to decide whether to call Codex."""
    keys = [META_FIELD_VENUETIER, META_FIELD_VENUEINDEX, META_FIELD_ACCEPTSTATUS]
    return [k for k in keys if not _bib_field_value(entry, k)]


def _apply_codex_metadata(
    entry: dict, codex_result: dict, config: dict, interactive: bool = True
) -> bool:
    """Show user the Codex output, ask which fields to apply. Returns True if entry changed."""
    venue_field = TYPE_TO_VENUE_FIELD.get(entry.get("ENTRYTYPE", "article"), "journal")
    proposals: list[tuple[str, str, str]] = []  # (display_label, bib_key, new_value)

    if codex_result.get("venue_full_name"):
        current_venue = _bib_field_value(entry, venue_field)
        new_venue = clean_text(codex_result["venue_full_name"])
        if new_venue and new_venue != current_venue:
            proposals.append(("venue 全称", venue_field, new_venue))

    for codex_key, bib_key, label in [
        ("venuetier", META_FIELD_VENUETIER, "等级 (venuetier)"),
        ("venueindex", META_FIELD_VENUEINDEX, "检索 (venueindex)"),
        ("acceptstatus", META_FIELD_ACCEPTSTATUS, "状态 (acceptstatus)"),
        ("authorrole", META_FIELD_AUTHORROLE, "我的身份 (authorrole)"),
        ("doi", "doi", "DOI"),
    ]:
        new_val = clean_text(codex_result.get(codex_key) or "")
        current = _bib_field_value(entry, bib_key)
        if new_val and new_val != current:
            proposals.append((label, bib_key, new_val))

    if not proposals:
        notice("Codex 没有给出可补充的字段。", "warn")
        if codex_result.get("notes"):
            notice(f"备注：{codex_result['notes']}", "info")
        return False

    section_title(
        f"Codex 处理结果（confidence: {codex_result.get('confidence', '?')}）"
    )
    table = Table(box=None, show_header=True, padding=(0, 2), header_style="bold cyan")
    table.add_column("字段")
    table.add_column("当前", style="dim")
    table.add_column("Codex 建议", style="green")
    for label, bib_key, new_val in proposals:
        table.add_row(label, _truncate(_bib_field_value(entry, bib_key) or "(空)", 36), _truncate(new_val, 50))
    console.print(table)
    if codex_result.get("notes"):
        console.print(f"[dim]备注：{codex_result['notes']}[/dim]")

    if not interactive:
        for _, bib_key, new_val in proposals:
            entry[bib_key] = new_val
        return True

    choice = choose_menu(
        "如何应用",
        [
            MenuItem("1", "全部应用", "把上面所有 Codex 建议写入条目。"),
            MenuItem("2", "选择性应用", "逐项 y/n 确认。"),
            MenuItem("0", "全部取消", ""),
        ],
        default="1",
    )
    if choice == "0":
        return False
    if choice == "1":
        for _, bib_key, new_val in proposals:
            entry[bib_key] = new_val
        return True
    # selective
    changed = False
    for label, bib_key, new_val in proposals:
        if prompt_yes_no(f"应用 {label} → {_truncate(new_val, 50)}", True):
            entry[bib_key] = new_val
            changed = True
    return changed


# --- 一次性清理工具 ---------------------------------------------------


def strip_author_from_bib(bib_path: Path) -> int:
    """Remove `author` field from every entry in a bib file. Returns count modified."""
    if not bib_path.exists():
        return 0
    db = load_bib(bib_path)
    changed = 0
    for entry in db.entries:
        if "author" in entry:
            del entry["author"]
            changed += 1
    if changed:
        save_bib(bib_path, db)
    return changed


# --- venue 缩写展开（Codex）------------------------------------------


def _expand_venue_with_codex(query: str, year_hint: str, model: str | None) -> list[dict]:
    prompt = {
        "task": (
            "Given an academic venue abbreviation, conference acronym, or partial name, "
            "return up to 3 plausible canonical full names suitable for a BibTeX entry. "
            "For conferences, prefer 'Proceedings of the ...' or the official "
            "'IEEE/ACM/Springer ... Conference on ...' form. For journals use the "
            "canonical journal title. Sort by likelihood, most likely first."
        ),
        "input": query,
        "year_hint": year_hint,
    }
    schema = {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "full_name": {"type": "string"},
                        "venue_type": {
                            "type": "string",
                            "enum": ["conference", "journal", "workshop", "preprint", "other"],
                        },
                        "note": {"type": "string"},
                    },
                    "required": ["full_name", "venue_type"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }
    result = call_codex(
        prompt,
        schema,
        label=f"展开 venue '{query}'",
        model=model,
        output_name="codex_venue.json",
        timeout=60,
    )
    return result.get("candidates", []) or []


def _pick_venue(query: str, year_hint: str, state: dict[str, bool], model: str | None) -> str:
    """User entered an abbreviation. Try to expand via Codex, return the chosen venue."""
    if not state.get("codex"):
        return query
    try:
        candidates = _expand_venue_with_codex(query, year_hint, model)
    except Exception as exc:  # noqa: BLE001
        notice(f"Codex 展开 venue 失败：{exc}", "warn")
        return query
    if not candidates:
        return query
    items = []
    for i, c in enumerate(candidates, 1):
        items.append(
            MenuItem(
                str(i),
                _truncate(c.get("full_name", ""), 70),
                f"[{c.get('venue_type', '?')}]" + (f"  · {c.get('note','')}" if c.get("note") else ""),
            )
        )
    items.append(MenuItem("0", f"以「{query}」为准 / 手动输入全名", ""))
    pick = choose_menu("Codex 给出的 venue 候选", items, default="1")
    if pick == "0":
        return prompt_line("venue 全名", query)
    return candidates[int(pick) - 1]["full_name"]


# --- 角色选择 + 摘要表格 ----------------------------------------------


def _choose_author_role(default_key: str = "") -> str:
    items = [MenuItem(str(i + 1), label, key or "(空)") for i, (key, label, _) in enumerate(AUTHOR_ROLES)]
    default_idx = next(
        (i + 1 for i, (k, _, _) in enumerate(AUTHOR_ROLES) if k == default_key),
        len(AUTHOR_ROLES),
    )
    pick = choose_menu("你在这篇论文中的角色", items, default=str(default_idx))
    return AUTHOR_ROLES[int(pick) - 1][0]


def _choose_accept_status(default_key: str = "") -> str:
    items = [MenuItem(str(i + 1), label, key or "(空)") for i, (key, label, _) in enumerate(ACCEPT_STATUSES)]
    default_idx = next(
        (i + 1 for i, (k, _, _) in enumerate(ACCEPT_STATUSES) if k == default_key),
        len(ACCEPT_STATUSES),
    )
    pick = choose_menu("录用状态", items, default=str(default_idx))
    return ACCEPT_STATUSES[int(pick) - 1][0]


def _prompt_index_tags(current: str = "") -> str:
    """Prompt for venueindex (检索情况). Free-form, comma-separated; suggests common values."""
    notice(
        f"常用值：{', '.join(COMMON_INDEX_TAGS)}；用逗号分隔；输 - 清空。",
        "info",
    )
    return _prompt_bib_value("检索情况 (venueindex)", current, required=False)


def _prompt_tier(current: str = "") -> str:
    """Prompt for venuetier. Free-form; suggests common values."""
    notice(
        "常用值：CCF A / CCF B / CCF C / JCR Q1-Q4；可写多个用分号分隔；输 - 清空。",
        "info",
    )
    return _prompt_bib_value("等级 (venuetier)", current, required=False)


def _valid_impact_factor(value: str) -> bool:
    if not clean_text(value):
        return True
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", clean_text(value)))


def _prompt_impact_factor(current: str = "") -> str:
    """Prompt for journal impact factor. Stored as a plain number-like string."""
    notice("填写数字即可，如 5.1；留空保留旧值，输 - 清空。", "info")
    while True:
        value = _prompt_bib_value("影响因子 (impactfactor)", current, required=False)
        if _valid_impact_factor(value):
            return value.replace(",", ".")
        notice("影响因子请填写数字，例如 5.1。", "warn")


def _bib_summary_table(entries: list[dict], config: dict) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1), header_style="bold cyan")
    table.add_column("idx", style="bold yellow", width=4)
    table.add_column("year", width=5)
    table.add_column("身份", width=6, style="magenta")
    table.add_column("等级", width=10, style="yellow")
    table.add_column("检索", width=10, style="cyan")
    table.add_column("IF", width=6, style="bright_magenta")
    table.add_column("状态", width=8, style="green")
    table.add_column("摘要", width=4, justify="center")
    table.add_column("venue", style="dim")
    table.add_column("title")
    dim_dash = Text("—", style="dim")
    for i, entry in enumerate(entries, 1):
        venue = clean_text(
            entry.get("journal") or entry.get("booktitle") or entry.get("howpublished") or ""
        )
        role_key = clean_text(entry.get(META_FIELD_AUTHORROLE) or "")
        if not role_key:
            role_key = _guess_author_role(str(entry.get("author") or ""), config)
        role_text = ROLE_KEY_TO_SHORT.get(role_key, "")
        role_cell = Text(role_text) if role_text and role_text != "—" else dim_dash
        tier_value = clean_text(entry.get(META_FIELD_VENUETIER) or "")
        tier_cell = Text(_truncate(tier_value, 10)) if tier_value else dim_dash
        index_value = clean_text(entry.get(META_FIELD_VENUEINDEX) or "")
        index_cell = Text(_truncate(index_value, 10)) if index_value else dim_dash
        impact_value = clean_text(entry.get(META_FIELD_IMPACTFACTOR) or "")
        impact_cell = Text(_truncate(impact_value, 6)) if impact_value else dim_dash
        accept_key = clean_text(entry.get(META_FIELD_ACCEPTSTATUS) or "")
        accept_text = ACCEPT_KEY_TO_SHORT.get(accept_key, "")
        accept_cell = Text(accept_text) if accept_text and accept_text != "—" else dim_dash
        has_abs = "[green]✔[/green]" if clean_text(entry.get("abstract")) else "[red]✘[/red]"
        table.add_row(
            str(i),
            clean_text(entry.get("year") or "?"),
            role_cell,
            tier_cell,
            index_cell,
            impact_cell,
            accept_cell,
            has_abs,
            _truncate(venue, 28),
            _truncate(clean_text(entry.get("title")), 48),
        )
    return table


def _prompt_bib_value(label: str, current: str = "", required: bool = False) -> str:
    """Bib field input — supports `-` to clear (when not required)."""
    suffix_hint = " [dim](- 清空)[/dim]" if (current and not required) else ""
    raw = console.input(
        f"[cyan]{label}[/cyan]"
        + (f" [dim]\\[{current}][/dim]" if current else "")
        + f"{suffix_hint}: "
    ).strip()
    if not raw:
        if required and not current:
            notice("这个字段不能为空。", "warn")
            return _prompt_bib_value(label, current, required)
        return current
    if raw == "-" and not required:
        return ""
    return raw


# --- 高级（全字段）编辑 -----------------------------------------------


def _edit_bib_entry_advanced(entry: dict, is_new: bool, existing_ids: set[str]) -> dict | None:
    if is_new:
        type_items = [
            MenuItem(str(i + 1), label, f"@{key}{{...}}")
            for i, (key, label) in enumerate(BIB_ENTRY_TYPES)
        ]
        choice = choose_menu("选择条目类型", type_items, default="1")
        entry["ENTRYTYPE"] = BIB_ENTRY_TYPES[int(choice) - 1][0]
        while True:
            new_id = prompt_line("citekey (如 zhang2026foo)", required=True)
            if new_id in existing_ids:
                notice(f"citekey '{new_id}' 已存在，请换一个。", "warn")
                continue
            entry["ID"] = new_id
            break

    venue_field = TYPE_TO_VENUE_FIELD.get(entry["ENTRYTYPE"], "journal")
    section_title(
        f"高级编辑 @{entry['ENTRYTYPE']}{{{entry['ID']}}}  ·  必填 venue：{venue_field}"
    )
    notice("回车保留旧值；可选字段输入 - 清空。", "info")

    for field in REQUIRED_BIB_FIELDS:
        entry[field] = _prompt_bib_value(field, str(entry.get(field, "")), required=True)
    entry[venue_field] = _prompt_bib_value(
        venue_field, str(entry.get(venue_field, "")), required=True
    )

    existing_optional = [
        f
        for f in list(entry.keys())
        if f not in RESERVED_BIB_KEYS
        and f not in REQUIRED_BIB_FIELDS
        and f != venue_field
    ]
    for field in existing_optional:
        new_val = _prompt_bib_value(field, str(entry.get(field, "")), required=False)
        if new_val == "":
            entry.pop(field, None)
        else:
            entry[field] = new_val

    while prompt_yes_no("是否添加新字段", False):
        unused = [f for f in OPTIONAL_BIB_FIELDS if f not in entry]
        if unused:
            options = [MenuItem(str(i + 1), name, "") for i, name in enumerate(unused)]
            options.append(MenuItem("0", "自定义字段名", ""))
            pick = choose_menu("选择字段", options, default="1")
            if pick == "0":
                fname = prompt_line("字段名", required=True)
            else:
                fname = unused[int(pick) - 1]
        else:
            fname = prompt_line("字段名", required=True)
        if not fname:
            break
        fval = _prompt_bib_value(fname, "", required=False)
        if fval:
            entry[fname] = fval

    return entry


# --- 引导式新增 -------------------------------------------------------


def _add_bib_entry_guided(
    config: dict, model: str | None, state: dict[str, bool], existing_ids: set[str]
) -> dict | None:
    section_title("新增论文 · 引导式录入")
    console.print(
        "[dim]步骤：标题 → venue → 年份 → 身份 → "
        "Codex 一键补全（等级/检索/状态/venue 全名）→ 联网检索作者/DOI/摘要。[/dim]"
    )

    # 1. 标题
    title = prompt_line("论文标题", required=True)
    # 2. venue（先存原文，等 Codex 一起处理）
    raw_venue = prompt_line("venue（缩写如 KDD/ICASSP，或直接全名）", required=True)
    # 3. 年份
    year = prompt_line("年份（如 2026）", required=True)
    # 4. 角色
    role = _choose_author_role()

    # 5. Codex 一键补全（等级 / 检索 / 状态 / venue 全名 / DOI / 身份猜测）
    codex_meta: dict = {}
    if state.get("codex"):
        if prompt_yes_no("是否调用 Codex 一键补全 (等级/检索/状态/venue 全名)", True):
            try:
                codex_meta = codex_paper_metadata(title, raw_venue, year, config, model)
            except Exception as exc:  # noqa: BLE001
                notice(f"Codex 补全失败：{exc}", "warn")
                codex_meta = {}
    venue = clean_text(codex_meta.get("venue_full_name") or "") or raw_venue
    if codex_meta.get("authorrole") and not role:
        suggested = codex_meta["authorrole"]
        notice(f"Codex 推断身份：{ROLE_KEY_TO_LABEL.get(suggested, suggested)}", "info")
        if prompt_yes_no("采用此身份", True):
            role = suggested

    # 5. 自动检索（作者列表完全靠这一步；找不到就留空，不强制手输）
    authors_bib = ""
    fetched: dict = {}
    if prompt_yes_no("是否联网检索作者列表 / DOI / 摘要", True):
        with with_spinner("Crossref / OpenAlex / Semantic Scholar"):
            fetched = _retrieve_full_metadata(title, year, config)
        if fetched:
            section_title(f"检索命中（来自 {fetched.get('source','?')}）")
            preview = Table(box=None, show_header=False, padding=(0, 2))
            preview.add_column("k", style="cyan")
            preview.add_column("v")
            if fetched.get("authors"):
                preview.add_row("作者", _format_authors_with_highlight(fetched["authors"], config, 80))
            if fetched.get("venue"):
                preview.add_row("venue", _truncate(fetched["venue"], 70))
            if fetched.get("doi"):
                preview.add_row("DOI", fetched["doi"])
            if fetched.get("abstract"):
                preview.add_row("abstract", _truncate(fetched["abstract"], 90))
            console.print(preview)
            if fetched.get("authors") and prompt_yes_no("使用检索到的作者列表", True):
                authors_bib = fetched["authors"]
                # 如果用户没主动选角色，根据位置推断一次
                if not role:
                    suggested = _guess_author_role(authors_bib, config)
                    if suggested:
                        notice(f"按作者位置推断身份：{ROLE_KEY_TO_LABEL.get(suggested, suggested)}", "info")
                        if prompt_yes_no("采用此身份", True):
                            role = suggested
            if fetched.get("venue") and fetched["venue"] != venue:
                if prompt_yes_no(
                    f"检索到的 venue：「{_truncate(fetched['venue'], 60)}」  覆盖你之前选的", False
                ):
                    venue = fetched["venue"]
        else:
            notice("未检索到匹配条目；作者列表将留空（网页不显示作者）。", "warn")

    # 6. 类型推断 + citekey（作者缺失时用我自己的姓做 fallback）
    entry_type = _guess_entry_type(venue)
    my_name = clean_text(get_nested(config, "info.name.en", ""))
    base_key = _generate_citekey(authors_bib, year, title, fallback_surname=my_name)
    final_key = _make_unique_citekey(base_key, existing_ids)
    notice(f"类型推断：@{entry_type}  ·  生成 citekey：{final_key}", "info")
    if prompt_yes_no("是否修改 citekey", False):
        while True:
            user_key = prompt_line("新 citekey", final_key, required=True)
            if user_key in existing_ids:
                notice("citekey 已存在，请换一个。", "warn")
                continue
            final_key = user_key
            break

    venue_field = TYPE_TO_VENUE_FIELD[entry_type]
    entry: dict = {
        "ENTRYTYPE": entry_type,
        "ID": final_key,
        "title": title,
        "year": year,
        venue_field: venue,
    }
    if authors_bib:
        entry["author"] = authors_bib
    if role:
        entry[META_FIELD_AUTHORROLE] = role
    # 来自 Codex 的结构化字段
    for codex_key, bib_key in [
        ("venuetier", META_FIELD_VENUETIER),
        ("venueindex", META_FIELD_VENUEINDEX),
        ("acceptstatus", META_FIELD_ACCEPTSTATUS),
    ]:
        v = clean_text(codex_meta.get(codex_key) or "")
        if v:
            entry[bib_key] = v
    if entry_type == "article" and prompt_yes_no("是否填写期刊影响因子 (用于累计 IF)", False):
        impact_value = _prompt_impact_factor("")
        if impact_value:
            entry[META_FIELD_IMPACTFACTOR] = impact_value
    # DOI 优先用联网检索的，其次用 Codex 给的
    doi_value = fetched.get("doi") or codex_meta.get("doi")
    if doi_value:
        entry["doi"] = doi_value
    if fetched.get("abstract") and prompt_yes_no("写入检索到的摘要", True):
        entry["abstract"] = fetched["abstract"]

    section_title("即将新增的条目")
    summary = Table(box=None, show_header=False, padding=(0, 2))
    summary.add_column("k", style="cyan")
    summary.add_column("v")
    summary.add_row("citekey", entry["ID"])
    summary.add_row("type", f"@{entry_type}")
    summary.add_row("title", title)
    if authors_bib:
        summary.add_row("author", _format_authors_with_highlight(authors_bib, config, 80))
    else:
        summary.add_row("author", Text("(留空，网页不显示作者)", style="dim"))
    summary.add_row("year", year)
    summary.add_row(venue_field, _truncate(venue, 70))
    if role:
        summary.add_row("我的身份", ROLE_KEY_TO_LABEL.get(role, role))
    summary.add_row("等级", entry.get(META_FIELD_VENUETIER, "[dim](无)[/dim]"))
    summary.add_row("检索", entry.get(META_FIELD_VENUEINDEX, "[dim](无)[/dim]"))
    summary.add_row("影响因子", entry.get(META_FIELD_IMPACTFACTOR, "[dim](无)[/dim]"))
    summary.add_row(
        "状态",
        ACCEPT_KEY_TO_LABEL.get(entry.get(META_FIELD_ACCEPTSTATUS, ""), "[dim](无)[/dim]"),
    )
    summary.add_row("DOI", entry.get("doi", "[dim](无)[/dim]"))
    summary.add_row("摘要", "[green]✔[/green]" if entry.get("abstract") else "[dim]✘[/dim]")
    console.print(summary)
    if not prompt_yes_no("确认新增", True):
        notice("已取消新增。", "warn")
        return None
    return entry


# --- 简化编辑（已有条目）---------------------------------------------


def _authors_markup_with_highlight(authors_str: str, config: dict, max_chars: int = 100) -> str:
    """Return a markup-safe string of authors with self bolded; suitable for Panel."""
    from rich.markup import escape

    variants = _self_name_variants(config)
    names = _split_authors(authors_str)
    if not names:
        return "[dim](无作者信息)[/dim]"
    parts = []
    used = 0
    for i, name in enumerate(names):
        sep = "" if i == 0 else ", "
        chunk = sep + name
        if used + len(chunk) > max_chars and i > 0:
            parts.append("[dim]…[/dim]")
            break
        is_self = _normalize_person(name) in variants
        if sep:
            parts.append(escape(sep))
        if is_self:
            parts.append(f"[bold cyan]{escape(name)}[/bold cyan]")
        else:
            parts.append(escape(name))
        used += len(chunk)
    return "".join(parts)


def _entry_detail_panel(entry: dict, config: dict) -> Panel:
    from rich.markup import escape

    venue_field = TYPE_TO_VENUE_FIELD.get(entry.get("ENTRYTYPE", "article"), "journal")
    venue_value = entry.get(venue_field) or entry.get("journal") or entry.get("booktitle") or ""
    role_key = clean_text(entry.get(META_FIELD_AUTHORROLE) or "") or _guess_author_role(
        str(entry.get("author") or ""), config
    )
    has_abs = "[green]✔ 已有[/green]" if clean_text(entry.get("abstract")) else "[red]✘ 缺失[/red]"
    tier_value = clean_text(entry.get(META_FIELD_VENUETIER) or "") or "[dim]—[/dim]"
    index_value = clean_text(entry.get(META_FIELD_VENUEINDEX) or "") or "[dim]—[/dim]"
    impact_value = clean_text(entry.get(META_FIELD_IMPACTFACTOR) or "") or "[dim]—[/dim]"
    accept_label = ACCEPT_KEY_TO_LABEL.get(
        clean_text(entry.get(META_FIELD_ACCEPTSTATUS) or ""), "[dim]—[/dim]"
    )
    title = escape(clean_text(entry.get("title")))
    venue_disp = escape(_truncate(clean_text(venue_value), 70))
    year_disp = escape(clean_text(entry.get("year") or "?"))
    role_disp = escape(ROLE_KEY_TO_LABEL.get(role_key, "—"))
    doi_disp = escape(str(entry.get("doi") or "")) if entry.get("doi") else "[dim](无)[/dim]"
    authors_disp = _authors_markup_with_highlight(str(entry.get("author") or ""), config, 100)
    body = (
        f"[cyan]title[/cyan]   {title}\n"
        f"[cyan]author[/cyan]  {authors_disp}\n"
        f"[cyan]year[/cyan]    [bold]{year_disp}[/bold]   "
        f"[cyan]venue[/cyan]  [bold]{venue_disp}[/bold]\n"
        f"[cyan]我的身份[/cyan]  [magenta]{role_disp}[/magenta]   "
        f"[cyan]等级[/cyan]  [yellow]{tier_value}[/yellow]   "
        f"[cyan]检索[/cyan]  [bright_cyan]{index_value}[/bright_cyan]   "
        f"[cyan]IF[/cyan]  [bright_magenta]{impact_value}[/bright_magenta]   "
        f"[cyan]状态[/cyan]  [green]{escape(accept_label) if not accept_label.startswith('[') else accept_label}[/green]\n"
        f"[cyan]摘要[/cyan]  {has_abs}   [cyan]DOI[/cyan]  {doi_disp}"
    )
    return Panel(
        body,
        title=f"[bold]@{entry.get('ENTRYTYPE','?')}{{{entry.get('ID','?')}}}[/bold]",
        border_style="cyan",
    )


def _edit_bib_entry_simple(
    entry: dict,
    config: dict,
    model: str | None,
    state: dict[str, bool],
    existing_ids: set[str],
) -> bool:
    """Returns True if the entry was modified."""
    modified = False
    while True:
        section_title("编辑条目")
        console.print(_entry_detail_panel(entry, config))
        choice = choose_menu(
            "改什么",
            [
                MenuItem("1", "改标题", ""),
                MenuItem("2", "改 venue", "支持 Codex 展开缩写。"),
                MenuItem("3", "改年份", ""),
                MenuItem("4", "改我的身份", "第一作者 / 共一 / 通讯 / …"),
                MenuItem("5", "改等级 (venuetier)", "如 CCF A / JCR Q1；可写多个用分号分隔。"),
                MenuItem("6", "改检索 (venueindex)", "如 SCI / EI / Scopus；逗号分隔。"),
                MenuItem("7", "改录用状态 (acceptstatus)", "已出版 / 已录用 / 在投。"),
                MenuItem("i", "改影响因子 (impactfactor)", "用于自动计算累计 IF；可留空。"),
                MenuItem(
                    "8",
                    "Codex 一键补全此条",
                    "等级/检索/状态/身份/venue 全名/DOI 一次到位。",
                    disabled=not state.get("codex"),
                    disabled_reason="未检测到 codex",
                ),
                MenuItem("9", "联网检索作者/DOI/摘要", "Crossref / OpenAlex / Semantic Scholar。"),
                MenuItem("a", "高级编辑", "全字段编辑（作者列表、DOI 等）。"),
                MenuItem("k", "改 citekey", "重命名引用键，自动查重。"),
                MenuItem("0", "完成", ""),
            ],
            default="0",
        )
        if choice == "0":
            return modified
        if choice == "1":
            new = prompt_line("title", str(entry.get("title", "")), required=True)
            if new != entry.get("title"):
                entry["title"] = new
                modified = True
        elif choice == "2":
            current_field = TYPE_TO_VENUE_FIELD.get(entry.get("ENTRYTYPE", "article"), "journal")
            current = str(entry.get(current_field) or entry.get("journal") or entry.get("booktitle") or "")
            raw = prompt_line(f"{current_field}（输缩写或全名）", current, required=True)
            if raw != current:
                year_hint = str(entry.get("year") or "")
                if state.get("codex") and len(raw) <= 40:
                    new_venue = _pick_venue(raw, year_hint, state, model)
                else:
                    new_venue = raw
                # 如果 venue 类型变了（期刊↔会议），调整 ENTRYTYPE
                new_type = _guess_entry_type(new_venue)
                old_type = entry.get("ENTRYTYPE")
                if new_type != old_type:
                    if prompt_yes_no(
                        f"venue 看起来更像 @{new_type}（当前 @{old_type}），是否同步切换", True
                    ):
                        # 把旧 venue 字段搬到新字段
                        old_field = TYPE_TO_VENUE_FIELD.get(old_type, "journal")
                        entry.pop(old_field, None)
                        entry["ENTRYTYPE"] = new_type
                        entry[TYPE_TO_VENUE_FIELD[new_type]] = new_venue
                    else:
                        entry[current_field] = new_venue
                else:
                    entry[current_field] = new_venue
                modified = True
        elif choice == "3":
            new = prompt_line("year", str(entry.get("year", "")), required=True)
            if new != entry.get("year"):
                entry["year"] = new
                modified = True
        elif choice == "4":
            current_role = clean_text(entry.get(META_FIELD_AUTHORROLE) or "")
            new_role = _choose_author_role(current_role)
            if new_role != current_role:
                if new_role:
                    entry[META_FIELD_AUTHORROLE] = new_role
                else:
                    entry.pop(META_FIELD_AUTHORROLE, None)
                modified = True
        elif choice == "5":
            current = clean_text(entry.get(META_FIELD_VENUETIER) or "")
            new_val = _prompt_tier(current)
            if new_val != current:
                if new_val:
                    entry[META_FIELD_VENUETIER] = new_val
                else:
                    entry.pop(META_FIELD_VENUETIER, None)
                modified = True
        elif choice == "6":
            current = clean_text(entry.get(META_FIELD_VENUEINDEX) or "")
            new_val = _prompt_index_tags(current)
            if new_val != current:
                if new_val:
                    entry[META_FIELD_VENUEINDEX] = new_val
                else:
                    entry.pop(META_FIELD_VENUEINDEX, None)
                modified = True
        elif choice == "7":
            current = clean_text(entry.get(META_FIELD_ACCEPTSTATUS) or "")
            new_val = _choose_accept_status(current)
            if new_val != current:
                if new_val:
                    entry[META_FIELD_ACCEPTSTATUS] = new_val
                else:
                    entry.pop(META_FIELD_ACCEPTSTATUS, None)
                modified = True
        elif choice in {"i", "I"}:
            current = clean_text(entry.get(META_FIELD_IMPACTFACTOR) or "")
            new_val = _prompt_impact_factor(current)
            if new_val != current:
                if new_val:
                    entry[META_FIELD_IMPACTFACTOR] = new_val
                else:
                    entry.pop(META_FIELD_IMPACTFACTOR, None)
                modified = True
        elif choice == "8":
            try:
                venue_field = TYPE_TO_VENUE_FIELD.get(entry.get("ENTRYTYPE", "article"), "journal")
                result = codex_paper_metadata(
                    str(entry.get("title") or ""),
                    str(entry.get(venue_field) or entry.get("journal") or entry.get("booktitle") or ""),
                    str(entry.get("year") or ""),
                    config,
                    model,
                )
                if _apply_codex_metadata(entry, result, config, interactive=True):
                    modified = True
            except Exception as exc:  # noqa: BLE001
                notice(f"Codex 调用失败：{exc}", "error")
        elif choice == "9":
            if _retrieve_for_one(entry, config):
                modified = True
        elif choice in {"a", "A"}:
            updated = _edit_bib_entry_advanced(entry, is_new=False, existing_ids=existing_ids)
            if updated is not None:
                modified = True
        elif choice in {"k", "K"}:
            while True:
                new_key = prompt_line("新 citekey", entry.get("ID", ""), required=True)
                if new_key == entry.get("ID"):
                    break
                if new_key in existing_ids:
                    notice("citekey 已存在，请换一个。", "warn")
                    continue
                entry["ID"] = new_key
                modified = True
                break


def _retrieve_for_one(entry: dict, config: dict) -> bool:
    title = clean_text(entry.get("title"))
    notice(f"检索：{title}", "info")
    with with_spinner("Crossref / OpenAlex / Semantic Scholar"):
        result = retrieve_abstract(entry, config)
    if not result.get("abstract"):
        notice("  · 未检索到摘要。", "warn")
        return False
    if entry.get("abstract") and not prompt_yes_no(
        f"  已存在摘要，是否用 {result.get('source')} 的版本覆盖", False
    ):
        return False
    entry["abstract"] = result["abstract"]
    if result.get("doi") and not clean_text(entry.get("doi")):
        entry["doi"] = result["doi"]
    notice(f"  ✔ 已写入摘要 (来自 {result.get('source')})。", "ok")
    return True


# --- 主入口 ----------------------------------------------------------


def bib_editor(
    bib_path: Path,
    title: str,
    config: dict,
    model: str | None,
    state: dict[str, bool],
) -> None:
    if not bib_path.exists():
        bib_path.parent.mkdir(parents=True, exist_ok=True)
        bib_path.write_text("", encoding="utf-8")

    database = load_bib(bib_path)
    entries: list[dict] = database.entries
    dirty = False

    while True:
        section_title(title)
        console.print(f"[dim]文件：{bib_path}[/dim]")
        if entries:
            console.print(_bib_summary_table(entries, config))
        else:
            console.print("[dim]暂无任何条目。[/dim]")

        missing_meta_count = sum(
            1 for e in entries if _missing_metadata_keys(e)
        )
        missing_abs_count = sum(
            1 for e in entries if not clean_text(e.get("abstract"))
        )
        choice = choose_menu(
            "操作",
            [
                MenuItem("1", "编辑某条", "选编号 → 子菜单按字段精修（含 Codex / 联网）。"),
                MenuItem("2", "新增一条 (引导式)", "标题 → venue → 年 → 身份 → Codex + 联网自动补全。"),
                MenuItem("3", "删除某条", ""),
                MenuItem(
                    "4",
                    f"Codex 批量补全等级/检索/状态/身份  ({missing_meta_count} 条缺字段)",
                    "一次 Codex 调用批量处理；失败不会逐条刷屏。",
                    disabled=not state.get("codex"),
                    disabled_reason="未检测到 codex",
                ),
                MenuItem(
                    "5",
                    f"联网批量检索作者/DOI/摘要  ({missing_abs_count} 条缺摘要)",
                    "Crossref → OpenAlex → Semantic Scholar；不会自动手粘。",
                ),
                MenuItem(
                    "6",
                    "清空所有作者列表（一次性整理）",
                    "把所有条目的 author 字段移除，作者完全靠后续联网检索。",
                ),
                MenuItem("0", "返回上层菜单", "如有改动会询问是否写盘。"),
            ],
            default="0",
        )
        if choice == "0":
            if dirty:
                if prompt_yes_no(f"将改动写入 {bib_path.name}", True):
                    save_bib(bib_path, database)
                    notice(f"已保存：{bib_path}", "ok")
                else:
                    notice("已放弃改动。", "warn")
            return

        if choice == "1":
            idx = _pick_index(entries, "编辑")
            if idx is None:
                continue
            existing_ids = {e.get("ID", "") for e in entries if e is not entries[idx]}
            if _edit_bib_entry_simple(entries[idx], config, model, state, existing_ids):
                dirty = True
        elif choice == "2":
            existing_ids = {e.get("ID", "") for e in entries}
            new_entry = _add_bib_entry_guided(config, model, state, existing_ids)
            if new_entry and new_entry.get("ID"):
                entries.append(new_entry)
                dirty = True
        elif choice == "3":
            idx = _pick_index(entries, "删除")
            if idx is None:
                continue
            target = entries[idx]
            label = f"@{target.get('ENTRYTYPE','?')}{{{target.get('ID','?')}}}"
            if prompt_yes_no(f"确认删除 {label}", False):
                entries.pop(idx)
                dirty = True
        elif choice == "4":
            targets = [e for e in entries if _missing_metadata_keys(e)]
            if not targets:
                notice("所有条目都已有完整的等级/检索/状态字段。", "ok")
                continue
            if not prompt_yes_no(
                f"将对 {len(targets)} 条进行一次 Codex 批量补全，确认开始", True
            ):
                continue
            section_title(f"Codex 批量补全 · {len(targets)} 条")
            updated = 0
            failed = 0
            unchanged = 0
            try:
                batch_results = codex_paper_metadata_batch(targets, config, model)
            except Exception as exc:  # noqa: BLE001
                notice(f"Codex 批量补全失败，未写入任何字段：{exc}", "warn")
                notice("建议先运行 5 联网批量检索；等级/检索/状态可稍后单条精修。", "info")
                continue
            for idx, entry in enumerate(targets, 1):
                result = batch_results.get(str(idx))
                if not result:
                    failed += 1
                    continue
                if _apply_codex_metadata(entry, result, config, interactive=False):
                    updated += 1
                else:
                    unchanged += 1
            if updated:
                dirty = True
            notice(
                f"批量补全完成：写入 {updated} 条，未变化 {unchanged} 条，失败 {failed} 条。",
                "ok" if updated else "warn",
            )
        elif choice == "5":
            missing = [e for e in entries if not clean_text(e.get("abstract"))]
            if not missing:
                notice("当前所有条目都已有摘要。", "ok")
                continue
            section_title(f"为 {len(missing)} 条缺摘要的论文检索")
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("({task.completed}/{task.total})"),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            )
            updated = 0
            with progress:
                task_id = progress.add_task("联网检索摘要", total=len(missing))
                for entry in missing:
                    result = retrieve_abstract(entry, config)
                    if result.get("abstract"):
                        entry["abstract"] = result["abstract"]
                        if result.get("doi") and not clean_text(entry.get("doi")):
                            entry["doi"] = result["doi"]
                        updated += 1
                    progress.advance(task_id)
            if updated:
                dirty = True
            notice(f"成功检索 {updated} / {len(missing)} 篇。", "ok" if updated else "warn")
        elif choice == "6":
            with_authors = sum(1 for e in entries if e.get("author"))
            if with_authors == 0:
                notice("当前所有条目都没有作者列表，无需清理。", "info")
                continue
            if not prompt_yes_no(
                f"将清空 {with_authors} 条的 author 字段（不可逆，需要重新联网检索）",
                False,
            ):
                continue
            for entry in entries:
                entry.pop("author", None)
            dirty = True
            notice(f"已清空 {with_authors} 条作者列表。", "ok")


# === Rankings 编辑器 (data/rankings.yaml) ===========================


RANKINGS_PATH = ROOT / "data" / "rankings.yaml"
RANKING_COLOR_HINTS = ["red", "green", "yellow", "blue", "purple"]


def _load_rankings() -> dict[str, Any]:
    if not RANKINGS_PATH.exists():
        return {}
    with RANKINGS_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_rankings(data: dict[str, Any]) -> None:
    RANKINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RANKINGS_PATH.open("w", encoding="utf-8", newline="\n") as f:
        f.write(
            "# 模糊匹配规则：如果 key 出现在论文的 journal/booktitle 字段中，则应用对应 tags。\n"
        )
        yaml.dump(data, f, sort_keys=False, allow_unicode=True)


def _rankings_table(data: dict[str, Any]) -> Table:
    table = Table(box=None, show_header=True, padding=(0, 1), header_style="bold cyan")
    table.add_column("idx", style="bold yellow", width=4)
    table.add_column("venue (substring)", style="cyan")
    table.add_column("tags")
    table.add_column("IF", style="bright_magenta", width=6)
    table.add_column("color", style="dim", width=10)
    for i, (venue, meta) in enumerate(data.items(), 1):
        tags = ", ".join(meta.get("tags", []) if isinstance(meta, dict) else [])
        impact_factor = clean_text(str(meta.get("impact_factor", "") if isinstance(meta, dict) else ""))
        color = (meta.get("color", "") if isinstance(meta, dict) else "") or ""
        table.add_row(str(i), _truncate(venue, 50), tags or "[dim](无)[/dim]", impact_factor or "[dim]—[/dim]", color)
    return table


def _edit_one_ranking(venue: str | None, current: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if venue is None:
        new_venue = prompt_line(
            "venue 子串（会被模糊匹配到 journal/booktitle，如 ICASSP）",
            required=True,
        )
    else:
        new_venue = prompt_line("venue 子串", venue, required=True)

    current_tags = ", ".join(current.get("tags", []))
    tags_line = prompt_line(
        "标签（用逗号分隔，如 CCF A 或 JCR Q1）",
        current_tags,
    )
    tags = [
        t.strip()
        for t in tags_line.replace("，", ",").split(",")
        if t.strip()
    ]

    current_color = current.get("color", "") or ""
    color_default = current_color or "red"
    color = prompt_line(
        f"颜色（建议 {' / '.join(RANKING_COLOR_HINTS)}）",
        color_default,
    )

    current_impact = clean_text(str(current.get("impact_factor") or current.get("impactfactor") or ""))
    impact_factor = _prompt_impact_factor(current_impact)

    new_meta = {"tags": tags, "color": color}
    if impact_factor:
        new_meta["impact_factor"] = impact_factor
    return new_venue, new_meta


def rankings_editor() -> None:
    data = _load_rankings()
    dirty = False
    while True:
        section_title("等级映射 (rankings.yaml)")
        console.print(
            "[dim]venue 子串会被模糊匹配到论文的 journal/booktitle；"
            "命中后挂上 tags + 颜色。[/dim]"
        )
        if data:
            console.print(_rankings_table(data))
        else:
            console.print("[dim]暂无规则。[/dim]")

        choice = choose_menu(
            "操作",
            [
                MenuItem("1", "编辑某条", "可改 venue / tags / color。"),
                MenuItem("2", "新增一条", ""),
                MenuItem("3", "删除某条", ""),
                MenuItem("0", "返回上层菜单", "如有改动会询问是否写盘。"),
            ],
            default="0",
        )
        if choice == "0":
            if dirty:
                if prompt_yes_no("将改动写入 data/rankings.yaml", True):
                    _save_rankings(data)
                    notice(f"已保存：{RANKINGS_PATH}", "ok")
                else:
                    notice("已放弃改动。", "warn")
            return

        keys = list(data.keys())
        if choice == "1":
            if not keys:
                notice("当前列表为空。", "warn")
                continue
            raw = prompt_line("输入要编辑的编号（回车取消）")
            if not raw:
                continue
            try:
                idx = int(raw) - 1
            except ValueError:
                notice("请输入数字。", "warn")
                continue
            if not 0 <= idx < len(keys):
                notice("编号超出范围。", "warn")
                continue
            old_venue = keys[idx]
            result = _edit_one_ranking(old_venue, data[old_venue] or {})
            if result is None:
                continue
            new_venue, new_meta = result
            new_data: dict[str, Any] = {}
            for k in keys:
                if k == old_venue:
                    new_data[new_venue] = new_meta
                else:
                    new_data[k] = data[k]
            data = new_data
            dirty = True
        elif choice == "2":
            result = _edit_one_ranking(None, {})
            if result is None:
                continue
            new_venue, new_meta = result
            if new_venue in data:
                if not prompt_yes_no(f"venue '{new_venue}' 已存在，是否覆盖", False):
                    continue
            data[new_venue] = new_meta
            dirty = True
        elif choice == "3":
            if not keys:
                notice("当前列表为空。", "warn")
                continue
            raw = prompt_line("输入要删除的编号（回车取消）")
            if not raw:
                continue
            try:
                idx = int(raw) - 1
            except ValueError:
                notice("请输入数字。", "warn")
                continue
            if not 0 <= idx < len(keys):
                notice("编号超出范围。", "warn")
                continue
            target = keys[idx]
            if prompt_yes_no(f"确认删除「{target}」", False):
                del data[target]
                dirty = True


# === 装饰图库优化 ====================================================


def output_relative_path(path_value: str | Path) -> Path:
    value = Path(str(path_value).replace("\\", "/"))
    if value.is_absolute():
        return value
    return OUTPUT_DIR / value


def to_output_rel(path: Path) -> str:
    return path.resolve().relative_to(OUTPUT_DIR.resolve()).as_posix()


def gallery_source(config: dict[str, Any]) -> str:
    return str((config.get("decorations") or {}).get("gallery_dir") or DEFAULT_GALLERY)


def list_gallery_images(source_dir: Path) -> list[Path]:
    if not source_dir.exists():
        return []
    return sorted(
        path
        for path in source_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def import_pillow():
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Pillow。请先运行：py -m pip install -r requirements.txt"
        ) from exc
    return Image, ImageOps


def _compress_one_image(args: tuple[str, str, int, int]) -> tuple[str, int, int, str | None]:
    """Worker for parallel WebP compression. Returns (src_str, src_size, dst_size, error_or_None)."""
    src_str, dst_str, quality, max_side = args
    try:
        from PIL import Image, ImageOps

        src = Path(src_str)
        dst = Path(dst_str)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src_size = src.stat().st_size
        with Image.open(src) as image:
            image = ImageOps.exif_transpose(image)
            if max(image.size) > max_side:
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            image = image.convert("RGBA" if has_alpha else "RGB")
            image.save(dst, "WEBP", quality=quality, method=6, exact=has_alpha)
        return src_str, src_size, dst.stat().st_size, None
    except Exception as exc:  # noqa: BLE001
        return src_str, 0, 0, f"{type(exc).__name__}: {exc}"


def _decide_workers(image_count: int, requested: int | None = None) -> int:
    cpu = os.cpu_count() or 4
    if requested and requested > 0:
        return max(1, min(requested, image_count))
    # Cap at 8 to avoid I/O thrashing on HDD; below 4 images, sequential is fine.
    return max(1, min(8, cpu, image_count))


def path_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def optimize_gallery(
    config: dict[str, Any],
    source_rel: str,
    target_rel: str,
    quality: int,
    max_side: int,
    update_config: bool,
    workers: int | None = None,
) -> dict[str, Any]:
    source_dir = output_relative_path(source_rel)
    target_dir = output_relative_path(target_rel)
    if source_dir.resolve() == target_dir.resolve():
        raise RuntimeError("输出目录不能和原始图库目录相同；为保护原图，请使用单独的优化目录。")
    images = list_gallery_images(source_dir)

    if not images:
        notice(f"没有找到图片：{source_dir}", "warn")
        return config

    import_pillow()  # 早失败：缺 Pillow 时立即抛错，不进 worker
    target_dir.mkdir(parents=True, exist_ok=True)

    tasks: list[tuple[str, str, int, int]] = []
    for src in images:
        rel = src.relative_to(source_dir)
        dst = (target_dir / rel).with_suffix(".webp")
        tasks.append((str(src), str(dst), quality, max_side))

    worker_count = _decide_workers(len(images), workers)
    notice(f"将以 {worker_count} 个进程并行压缩 {len(images)} 张图片。", "info")

    original_total = 0
    optimized_total = 0
    converted = 0
    errors: list[tuple[str, str]] = []

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("({task.completed}/{task.total})"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )

    if worker_count == 1:
        with progress:
            task_id = progress.add_task("压缩为 WebP（单进程）", total=len(tasks))
            for args in tasks:
                src_str, src_size, dst_size, err = _compress_one_image(args)
                if err is None:
                    original_total += src_size
                    optimized_total += dst_size
                    converted += 1
                else:
                    errors.append((src_str, err))
                progress.advance(task_id)
    else:
        ctx = multiprocessing.get_context("spawn")
        with progress, ProcessPoolExecutor(max_workers=worker_count, mp_context=ctx) as pool:
            task_id = progress.add_task(
                f"压缩为 WebP（并行 ×{worker_count}）", total=len(tasks)
            )
            futures = [pool.submit(_compress_one_image, args) for args in tasks]
            for fut in as_completed(futures):
                src_str, src_size, dst_size, err = fut.result()
                if err is None:
                    original_total += src_size
                    optimized_total += dst_size
                    converted += 1
                else:
                    errors.append((src_str, err))
                progress.advance(task_id)

    if update_config:
        decorations = config.setdefault("decorations", {})
        decorations["gallery_dir"] = to_output_rel(target_dir)

    saved = original_total - optimized_total
    ratio = (optimized_total / original_total * 100) if original_total else 0
    section_title("图库优化完成")
    summary = Table(box=None, show_header=False, padding=(0, 2))
    summary.add_column("kind", style="cyan")
    summary.add_column("value", style="bold")
    summary.add_row("图片数量", f"{converted} / {len(images)}")
    summary.add_row("并行进程", f"{worker_count}")
    summary.add_row("原始大小", f"{original_total / 1024 / 1024:.2f} MB")
    summary.add_row("优化后", f"{optimized_total / 1024 / 1024:.2f} MB ({ratio:.1f}%)")
    summary.add_row("节省", f"{saved / 1024 / 1024:.2f} MB")
    summary.add_row("输出目录", str(target_dir))
    console.print(summary)
    if errors:
        notice(f"有 {len(errors)} 张图压缩失败：", "warn")
        for src_str, err in errors[:5]:
            console.print(f"  [dim]·[/dim] {src_str}: {err}")
        if len(errors) > 5:
            console.print(f"  [dim]... 其余 {len(errors) - 5} 条已省略[/dim]")
    if update_config:
        notice(f"已切换网页图库到：{to_output_rel(target_dir)}", "ok")

    return config


def optimize_gallery_to_target(
    config: dict[str, Any],
    source_rel: str,
    target_rel: str,
    quality: int,
    max_side: int,
    update_config: bool,
    target_mb: float,
    workers: int | None = None,
    min_quality: int = 36,
    min_side: int = 560,
    max_attempts: int = 4,
) -> dict[str, Any]:
    target_bytes = int(max(0.1, target_mb) * 1024 * 1024)
    target_dir = output_relative_path(target_rel)
    attempt_quality = max(min_quality, min(100, quality))
    attempt_side = max(min_side, max_side)

    for attempt in range(1, max(1, max_attempts) + 1):
        notice(
            f"目标压缩第 {attempt}/{max_attempts} 轮："
            f"quality={attempt_quality}，最长边={attempt_side}，目标≈{target_mb:.1f} MB。",
            "info",
        )
        config = optimize_gallery(
            config,
            source_rel,
            target_rel,
            attempt_quality,
            attempt_side,
            update_config,
            workers=workers,
        )
        current_bytes = path_size_bytes(target_dir)
        current_mb = current_bytes / 1024 / 1024
        if current_bytes <= target_bytes:
            notice(f"已达到目标：当前约 {current_mb:.2f} MB。", "ok")
            return config
        if attempt >= max_attempts:
            notice(
                f"未完全达到目标：当前约 {current_mb:.2f} MB，"
                f"可继续降低 --min-quality / --min-side 或手动设更小 --quality / --max-side。",
                "warn",
            )
            return config

        ratio = target_bytes / max(1, current_bytes)
        next_quality = max(min_quality, int(attempt_quality * max(0.62, ratio ** 0.35)))
        next_side = max(min_side, int(attempt_side * max(0.72, ratio ** 0.5)))
        if next_quality == attempt_quality and next_side == attempt_side:
            next_quality = max(min_quality, attempt_quality - 6)
            if next_quality == attempt_quality:
                next_side = max(min_side, attempt_side - 80)
        if next_quality == attempt_quality and next_side == attempt_side:
            notice("已经到达设置的质量/尺寸下限，停止自动压缩。", "warn")
            return config
        attempt_quality, attempt_side = next_quality, next_side

    return config


def optimize_gallery_wizard(config: dict[str, Any]) -> dict[str, Any]:
    section_title("压缩透明装饰图库")
    current_gallery = gallery_source(config)
    source_rel = prompt_line("原始图库目录", current_gallery) or current_gallery

    preset = choose_menu(
        "图片压缩方案",
        [
            MenuItem("1", "均衡推荐", "WebP quality=52，最长边 750。默认更小，适合网页快速加载。"),
            MenuItem("2", "高清优先", "WebP quality=64，最长边 1000。文件稍大但细节更稳。"),
            MenuItem("3", "速度优先", "WebP quality=48，最长边 700。加载最快。"),
            MenuItem("4", "自定义", "手动输入质量和最长边。"),
            MenuItem("5", "目标体积", "输入目标总 MB，脚本自动多轮调低 quality / 最长边。"),
        ],
        default="1",
    )

    target_mb: float | None = None
    if preset == "1":
        quality, max_side = DEFAULT_WEBP_QUALITY, DEFAULT_MAX_IMAGE_SIDE
    elif preset == "2":
        quality, max_side = 64, 1000
    elif preset == "3":
        quality, max_side = 48, 700
    elif preset == "5":
        quality, max_side = DEFAULT_WEBP_QUALITY, DEFAULT_MAX_IMAGE_SIDE
        try:
            target_mb = float(prompt_line("目标图库总大小 MB", "25", True))
        except ValueError:
            notice("无法解析目标大小，已回退到默认压缩。", "warn")
            target_mb = None
    else:
        quality = int(prompt_line("WebP 质量 1-100", str(DEFAULT_WEBP_QUALITY), True))
        max_side = int(prompt_line("最长边像素", str(DEFAULT_MAX_IMAGE_SIDE), True))
        quality = max(1, min(100, quality))
        max_side = max(320, max_side)

    target_rel = prompt_line("优化输出目录", DEFAULT_OPTIMIZED_GALLERY) or DEFAULT_OPTIMIZED_GALLERY
    update_config = prompt_yes_no("优化后自动让网页使用这个目录", True)
    cpu = os.cpu_count() or 4
    default_workers = max(1, min(8, cpu))
    workers_raw = prompt_line(
        f"并行进程数（1=单线程；建议 {default_workers}）",
        str(default_workers),
    )
    try:
        workers = max(1, int(workers_raw))
    except ValueError:
        workers = default_workers
        notice(f"无法解析进程数，已回退到 {workers}。", "warn")
    if target_mb:
        return optimize_gallery_to_target(
            config,
            source_rel,
            target_rel,
            quality,
            max_side,
            update_config,
            target_mb,
            workers=workers,
        )
    return optimize_gallery(
        config, source_rel, target_rel, quality, max_side, update_config, workers=workers
    )


# === 构建 / Git / 发布 ==============================================


def run_build(open_browser: bool = False) -> None:
    section_title("重新构建主页")
    result = subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT, text=True)
    if result.returncode != 0:
        raise RuntimeError("构建失败，请检查 build.py 输出。")
    notice("构建完成。", "ok")
    if open_browser:
        maybe_open_in_browser(OUTPUT_DIR / "index.html")


def maybe_open_in_browser(path: Path) -> None:
    if not path.exists():
        return
    if not prompt_yes_no(f"是否打开浏览器查看 {path.name}", True):
        return
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        notice(f"打开浏览器失败：{exc}", "warn")


def run_git_command(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    if shutil.which("git") is None:
        raise RuntimeError("未找到 git，请先安装 Git 并完成 GitHub 登录。")

    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Git 命令失败：git {' '.join(args)}\n{detail}")
    return result


def git_text(args: list[str], check: bool = True) -> str:
    return run_git_command(args, check).stdout.strip()


def publish_to_github(commit_message: str) -> None:
    message = commit_message.strip() or "Update academic homepage"
    repo_root = git_text(["rev-parse", "--show-toplevel"])
    if Path(repo_root).resolve() != ROOT.resolve():
        raise RuntimeError(f"当前脚本不在预期 Git 仓库根目录运行：{repo_root}")

    remotes = [line.strip() for line in git_text(["remote"]).splitlines() if line.strip()]
    if "origin" not in remotes:
        raise RuntimeError("未找到 Git remote `origin`，请先配置 GitHub 仓库地址。")

    branch = git_text(["branch", "--show-current"])
    if not branch:
        raise RuntimeError("当前不在普通分支上，无法一键推送。")

    status = git_text(["status", "--short", "--untracked-files=all"])
    if status:
        console.print("\n[bold]准备提交以下变更：[/bold]")
        console.print(Panel(status, border_style="dim"))
        run_git_command(["add", "-A"])
        staged = git_text(["diff", "--cached", "--name-only"])
        if staged:
            run_git_command(["commit", "-m", message])
            notice(f"已创建提交：{message}", "ok")
        else:
            notice("没有检测到需要提交的文件。", "warn")
    else:
        notice("没有本地变更，直接检查远端推送状态。", "info")

    push = run_git_command(["push"], check=False)
    if push.returncode != 0:
        push = run_git_command(["push", "-u", "origin", branch], check=False)
    if push.returncode != 0:
        detail = (push.stderr or push.stdout or "").strip()
        raise RuntimeError(f"推送失败：\n{detail}")

    notice(f"已推送到 GitHub：origin/{branch}", "ok")


def directory_size(paths: list[Path]) -> int:
    return sum(path.stat().st_size for path in paths if path.exists())


def latest_mtime(paths: list[Path]) -> float:
    if not paths:
        return 0.0
    return max(path.stat().st_mtime for path in paths if path.exists())


def prepare_publish_assets(config: dict[str, Any], config_path: Path) -> tuple[dict[str, Any], bool]:
    source_rel = gallery_source(config).replace("\\", "/").rstrip("/")
    source_dir = output_relative_path(source_rel)
    source_images = list_gallery_images(source_dir)
    if source_rel != DEFAULT_GALLERY or not source_images:
        return config, False

    target_dir = output_relative_path(DEFAULT_OPTIMIZED_GALLERY)
    target_images = list_gallery_images(target_dir)
    source_size_mb = directory_size(source_images) / 1024 / 1024
    needs_optimize = (
        not target_images
        or len(target_images) < len(source_images)
        or latest_mtime(source_images) > latest_mtime(target_images)
    )

    notice(
        f"发布前检测到原始装饰图库：{len(source_images)} 张，约 {source_size_mb:.1f} MB。",
        "info",
    )
    if needs_optimize:
        notice("将先生成 WebP 优化图库，并把网页切换到优化目录。", "info")
        config = optimize_gallery(
            config,
            DEFAULT_GALLERY,
            DEFAULT_OPTIMIZED_GALLERY,
            quality=DEFAULT_WEBP_QUALITY,
            max_side=DEFAULT_MAX_IMAGE_SIDE,
            update_config=True,
        )
    else:
        config.setdefault("decorations", {})["gallery_dir"] = to_output_rel(target_dir)
        notice(f"已复用现有优化图库：{target_dir}", "info")

    save_config(config_path, config)
    return config, True


def save_build_publish(
    config: dict[str, Any],
    config_path: Path,
    no_build: bool,
    commit_message: str,
    build_first: bool = True,
) -> dict[str, Any]:
    config, assets_changed = prepare_publish_assets(config, config_path)
    save_config(config_path, config)
    if not no_build and (build_first or assets_changed):
        run_build()
    publish_to_github(commit_message)
    return config


# === 保存 + diff 预览 ===============================================


def compute_yaml_diff(original: dict[str, Any], current: dict[str, Any]) -> str:
    before = dump_config(original).splitlines()
    after = dump_config(current).splitlines()
    diff = difflib.unified_diff(
        before,
        after,
        fromfile="config.yaml (current on disk)",
        tofile="config.yaml (after this session)",
        lineterm="",
    )
    return "\n".join(diff)


def show_diff_preview(original: dict[str, Any], current: dict[str, Any]) -> bool:
    diff = compute_yaml_diff(original, current)
    if not diff.strip():
        notice("没有未保存的改动。", "info")
        return False
    section_title("保存预览（diff）")
    syntax = Syntax(diff, "diff", theme="ansi_dark", line_numbers=False, word_wrap=True)
    console.print(syntax)
    return True


def save_with_preview(
    config: dict[str, Any],
    original: dict[str, Any],
    config_path: Path,
) -> bool:
    has_changes = show_diff_preview(original, config)
    if not has_changes:
        return True
    if not prompt_yes_no("确认写入磁盘", True):
        notice("已取消保存。", "warn")
        return False
    save_config(config_path, config)
    notice(f"已保存：{config_path}", "ok")
    return True


# === Codex 一键智能维护（保留 CLI 行为）============================


def codex_auto_maintain(
    config: dict[str, Any],
    config_path: Path,
    model: str | None,
    no_build: bool,
    allow_abstract_input: bool,
) -> dict[str, Any]:
    fill_missing_abstracts(config, allow_abstract_input)
    config = auto_classify_research(config, model)
    config = translate_config(config, model)
    save_config(config_path, config)
    notice(f"已保存：{config_path}", "ok")
    if not no_build:
        run_build()
    return config


# === 主菜单（分层）==================================================


def _safe_run(action: Callable[[], Any], pause_on_done: bool = True) -> None:
    try:
        action()
    except KeyboardInterrupt:
        notice("\n已中断当前操作。", "warn")
    except Exception as exc:  # noqa: BLE001
        notice(f"操作失败：{exc}", "error")
        pause()
        return
    if pause_on_done:
        pause()


def data_entry_menu(config: dict[str, Any], model: str | None, state: dict[str, bool]) -> None:
    while True:
        section_title("录入与维护")
        items = [
            MenuItem("1", "基础信息", "姓名/身份/链接/简介/兴趣。"),
            MenuItem("2", "教育经历", "列表精修：编辑/新增/删除/排序。"),
            MenuItem("3", "学术活动", "列表精修：编辑/新增/删除/排序。"),
            MenuItem("4", "审稿服务", "审稿期刊会议 + 服务角色。"),
            MenuItem("5", "已发表论文 (papers.bib)", "引导式新增；身份+venue Codex 展开+自动补全。"),
            MenuItem("6", "预印本 (preprints.bib)", "引导式新增；身份+venue Codex 展开+自动补全。"),
            MenuItem("7", "期刊/会议等级 (rankings.yaml)", "维护 venue → tags / color / IF 映射。"),
            MenuItem("8", "对照编辑中英文", "修正 Codex 翻译；按字段编号编辑。"),
            MenuItem("0", "返回主菜单", ""),
        ]
        choice = choose_menu("录入子菜单", items, default="0")
        if choice == "0":
            return
        if choice == "1":
            _safe_run(lambda: edit_basic_info(config))
        elif choice == "2":
            _safe_run(
                lambda: list_editor(config, "education", EDUCATION_FIELDS, "维护教育经历"),
                pause_on_done=False,
            )
        elif choice == "3":
            _safe_run(
                lambda: list_editor(config, "activities", ACTIVITY_FIELDS, "维护学术活动"),
                pause_on_done=False,
            )
        elif choice == "4":
            _safe_run(lambda: edit_reviewing(config))
        elif choice == "5":
            _safe_run(
                lambda: bib_editor(
                    PUBLISHED_BIB_PATH, "维护已发表论文 (papers.bib)", config, model, state
                ),
                pause_on_done=False,
            )
        elif choice == "6":
            _safe_run(
                lambda: bib_editor(
                    PREPRINT_BIB_PATH, "维护预印本 (preprints.bib)", config, model, state
                ),
                pause_on_done=False,
            )
        elif choice == "7":
            _safe_run(rankings_editor, pause_on_done=False)
        elif choice == "8":
            _safe_run(
                lambda: edit_bilingual_overrides(config),
                pause_on_done=False,
            )


def codex_menu(
    config: dict[str, Any],
    config_path: Path,
    model: str | None,
    no_build: bool,
    state: dict[str, bool],
) -> dict[str, Any]:
    while True:
        section_title("Codex 智能任务")
        if not state["codex"]:
            notice("未检测到 codex 命令；请先安装并登录 Codex CLI。", "warn")
        items = [
            MenuItem("1", "补全论文摘要", "优先联网检索；找不到再让你手粘。"),
            MenuItem(
                "2",
                "凝练研究方向",
                "Codex 归类论文与简介，写入 research_profile / research_directions。",
                disabled=not state["codex"],
                disabled_reason="未检测到 codex",
            ),
            MenuItem(
                "3",
                "翻译中文字段",
                "Codex 把所有中文字段翻成英文。",
                disabled=not state["codex"],
                disabled_reason="未检测到 codex",
            ),
            MenuItem(
                "4",
                "一键全套维护",
                "摘要 → 凝练 → 翻译 → 立即写盘 +（可选）构建。",
                disabled=not state["codex"],
                disabled_reason="未检测到 codex",
            ),
            MenuItem("0", "返回主菜单", ""),
        ]
        choice = choose_menu("Codex 子菜单", items, default="0")
        if choice == "0":
            return config
        try:
            if choice == "1":
                fill_missing_abstracts(
                    config, prompt_yes_no("未检索到摘要时是否手动输入", True)
                )
            elif choice == "2":
                config = auto_classify_research(config, model)
            elif choice == "3":
                config = translate_config(config, model)
            elif choice == "4":
                allow_input = prompt_yes_no("摘要检索不到时是否手动输入", True)
                config = codex_auto_maintain(config, config_path, model, no_build, allow_input)
                # codex_auto_maintain 已落盘，让上层重置 original
                return config
        except KeyboardInterrupt:
            notice("\n已中断当前操作。", "warn")
        except Exception as exc:  # noqa: BLE001
            notice(f"操作失败：{exc}", "error")
        pause()
    return config


def gallery_menu(config: dict[str, Any], state: dict[str, bool]) -> dict[str, Any]:
    while True:
        section_title("装饰图库")
        items = [
            MenuItem(
                "1",
                "压缩为 WebP（并行）",
                "保留原图，生成 WebP 并可切换网页使用。",
                disabled=not state["pillow"],
                disabled_reason="缺少 Pillow",
            ),
            MenuItem("0", "返回主菜单", ""),
        ]
        choice = choose_menu("图库子菜单", items, default="0")
        if choice == "0":
            return config
        try:
            if choice == "1":
                config = optimize_gallery_wizard(config)
        except KeyboardInterrupt:
            notice("\n已中断当前操作。", "warn")
        except Exception as exc:  # noqa: BLE001
            notice(f"操作失败：{exc}", "error")
        pause()
    return config


def publish_menu(
    config: dict[str, Any],
    original_holder: list[dict[str, Any]],
    config_path: Path,
    no_build: bool,
    state: dict[str, bool],
) -> tuple[dict[str, Any], bool]:
    """Returns (config, should_exit_main)."""
    while True:
        section_title("构建与发布")
        items = [
            MenuItem("1", "重新构建主页", "build.py → output/index.html，构建后可开浏览器。"),
            MenuItem("2", "保存改动（不构建）", "diff 预览 → 写入 config.yaml。"),
            MenuItem("3", "保存 + 构建", "保存 → build → 询问是否打开浏览器。"),
            MenuItem(
                "4",
                "保存 + 构建 + 推送 GitHub",
                "等价旧菜单 9；推送前会让你确认。",
                disabled=not state["git"],
                disabled_reason="未检测到 git",
            ),
            MenuItem("0", "返回主菜单", ""),
        ]
        choice = choose_menu("发布子菜单", items, default="0")
        if choice == "0":
            return config, False
        try:
            if choice == "1":
                if save_with_preview(config, original_holder[0], config_path):
                    original_holder[0] = copy.deepcopy(config)
                    run_build(open_browser=True)
            elif choice == "2":
                if save_with_preview(config, original_holder[0], config_path):
                    original_holder[0] = copy.deepcopy(config)
            elif choice == "3":
                if save_with_preview(config, original_holder[0], config_path):
                    original_holder[0] = copy.deepcopy(config)
                    if not no_build:
                        run_build(open_browser=True)
            elif choice == "4":
                if not save_with_preview(config, original_holder[0], config_path):
                    pause()
                    continue
                original_holder[0] = copy.deepcopy(config)
                message = (
                    prompt_line("提交信息", "Update academic homepage")
                    or "Update academic homepage"
                )
                if prompt_yes_no("确认提交并推送到 GitHub", True):
                    config = save_build_publish(
                        config, config_path, no_build, message, build_first=False
                    )
                return config, True
        except KeyboardInterrupt:
            notice("\n已中断当前操作。", "warn")
        except Exception as exc:  # noqa: BLE001
            notice(f"操作失败：{exc}", "error")
        pause()


def main_menu(
    config: dict[str, Any],
    config_path: Path,
    model: str | None,
    no_build: bool,
) -> dict[str, Any]:
    original_holder = [copy.deepcopy(config)]
    state = detect_capabilities()

    while True:
        console.clear()
        console.print(
            Panel(
                "[bold]个人主页维护向导[/bold]\n"
                "[dim]录入资料 → Codex 智能补齐 → 装饰图库 → 构建发布。"
                "所有 yaml 改动只在内存里，最后到「发布」菜单或选 s 才写盘。[/dim]",
                border_style="cyan",
            )
        )
        dirty = compute_yaml_diff(original_holder[0], config).strip() != ""
        render_status(config, state, dirty)

        items = [
            MenuItem("1", "录入与维护", "基础信息 / 教育 / 活动 / 论文 / 预印本 / rankings / 中英对照。"),
            MenuItem("2", "Codex 智能任务", "摘要 / 凝练方向 / 翻译 / 一键全套。"),
            MenuItem("3", "装饰图库", "并行 WebP 压缩。"),
            MenuItem("4", "构建与发布", "构建主页、保存、推送 GitHub。"),
            MenuItem("s", "保存并退出", "等价 4 菜单的「保存（不构建）」+ 退出。"),
            MenuItem("q", "丢弃修改退出", "放弃本次未保存改动。"),
        ]
        choice = choose_menu("主菜单", items, default="1")
        # 兼容旧数字键：9 直接进入「保存 + 构建 + 推送」
        if choice == "9":
            config, exit_main = publish_menu(
                config, original_holder, config_path, no_build, state
            )
            if exit_main:
                return config
            continue

        try:
            if choice == "1":
                data_entry_menu(config, model, state)
            elif choice == "2":
                config = codex_menu(config, config_path, model, no_build, state)
                original_holder[0] = copy.deepcopy(config)
            elif choice == "3":
                config = gallery_menu(config, state)
            elif choice == "4":
                config, exit_main = publish_menu(
                    config, original_holder, config_path, no_build, state
                )
                if exit_main:
                    return config
            elif choice == "s":
                if save_with_preview(config, original_holder[0], config_path):
                    original_holder[0] = copy.deepcopy(config)
                    if not no_build and prompt_yes_no("是否立即重新构建主页", True):
                        run_build(open_browser=True)
                    return config
            elif choice == "q":
                if dirty and not prompt_yes_no("有未保存改动，确认放弃", False):
                    continue
                notice("已退出，未保存本次菜单中的改动。", "warn")
                return config
        except KeyboardInterrupt:
            notice("\n已中断当前操作，回到主菜单。", "warn")
        except Exception as exc:  # noqa: BLE001
            notice(f"操作失败：{exc}", "error")
            pause()


# === main / CLI flag 入口 ============================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="交互式录入个人主页信息，并使用 Codex CLI 翻译为英文。"
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="配置文件路径，默认 data/config.yaml")
    parser.add_argument("--model", default=None, help="传给 codex exec 的模型名，例如 gpt-5.4")
    parser.add_argument("--no-translate", action="store_true", help="只录入中文，不调用 Codex CLI 翻译")
    parser.add_argument("--no-build", action="store_true", help="保存后不自动运行 build.py")
    parser.add_argument(
        "--legacy-profile",
        action="store_true",
        help="(已废弃，等同直接进交互菜单) 旧版一次性资料录入流程",
    )
    parser.add_argument("--compress-gallery", action="store_true", help="直接压缩图库，不进入菜单")
    parser.add_argument("--gallery-source", default=None, help="图库源目录，相对 output/")
    parser.add_argument(
        "--gallery-output",
        default=DEFAULT_OPTIMIZED_GALLERY,
        help="优化输出目录，相对 output/",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_WEBP_QUALITY,
        help=f"WebP 质量，默认 {DEFAULT_WEBP_QUALITY}",
    )
    parser.add_argument(
        "--max-side",
        type=int,
        default=DEFAULT_MAX_IMAGE_SIDE,
        help=f"最长边像素，默认 {DEFAULT_MAX_IMAGE_SIDE}",
    )
    parser.add_argument(
        "--target-mb",
        type=float,
        default=0,
        help="目标优化图库总大小（MB）；大于 0 时自动多轮调低 quality / 最长边",
    )
    parser.add_argument(
        "--min-quality",
        type=int,
        default=36,
        help="配合 --target-mb 使用的最低 WebP 质量，默认 36",
    )
    parser.add_argument(
        "--min-side",
        type=int,
        default=560,
        help="配合 --target-mb 使用的最小最长边像素，默认 560",
    )
    parser.add_argument(
        "--target-attempts",
        type=int,
        default=4,
        help="配合 --target-mb 使用的最多自动压缩轮数，默认 4",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="WebP 并行进程数；0 表示自动（min(8, cpu_count)）",
    )
    parser.add_argument(
        "--no-update-gallery-config",
        action="store_true",
        help="压缩后不修改 config 的 gallery_dir",
    )
    parser.add_argument("--fill-abstracts", action="store_true", help="直接补全论文摘要，不进入菜单")
    parser.add_argument("--no-abstract-input", action="store_true", help="摘要检索不到时不进入手动输入")
    parser.add_argument("--classify-research", action="store_true", help="直接调用 Codex CLI 自动凝练研究方向")
    parser.add_argument("--codex-auto", action="store_true", help="摘要检索、研究方向凝练、英文补全并构建")
    parser.add_argument("--publish", action="store_true", help="保存、构建、提交并推送到 GitHub")
    parser.add_argument(
        "--commit-message",
        default="Update academic homepage",
        help="一键推送时使用的 Git 提交信息",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    if args.compress_gallery:
        source_rel = args.gallery_source or gallery_source(config)
        quality = max(1, min(100, args.quality))
        max_side = max(320, args.max_side)
        if args.target_mb and args.target_mb > 0:
            config = optimize_gallery_to_target(
                config,
                source_rel,
                args.gallery_output,
                quality,
                max_side,
                not args.no_update_gallery_config,
                args.target_mb,
                workers=args.workers if args.workers else None,
                min_quality=max(1, min(100, args.min_quality)),
                min_side=max(320, args.min_side),
                max_attempts=max(1, args.target_attempts),
            )
        else:
            config = optimize_gallery(
                config,
                source_rel,
                args.gallery_output,
                quality,
                max_side,
                not args.no_update_gallery_config,
                workers=args.workers if args.workers else None,
            )
        save_config(args.config, config)
        if not args.no_build:
            run_build()
        if args.publish:
            config = save_build_publish(
                config, args.config, args.no_build, args.commit_message, build_first=False
            )
        return 0

    if args.fill_abstracts:
        fill_missing_abstracts(config, not args.no_abstract_input)
        if not args.no_build:
            run_build()
        if args.publish:
            config = save_build_publish(
                config, args.config, args.no_build, args.commit_message, build_first=False
            )
        return 0

    if args.classify_research:
        config = auto_classify_research(config, args.model)
        save_config(args.config, config)
        if not args.no_build:
            run_build()
        if args.publish:
            config = save_build_publish(
                config, args.config, args.no_build, args.commit_message, build_first=False
            )
        return 0

    if args.codex_auto:
        config = codex_auto_maintain(
            config, args.config, args.model, args.no_build, not args.no_abstract_input
        )
        if args.publish:
            config = save_build_publish(
                config, args.config, args.no_build, args.commit_message, build_first=False
            )
        return 0

    if args.publish and not args.legacy_profile:
        config = save_build_publish(
            config, args.config, args.no_build, args.commit_message, build_first=True
        )
        return 0

    if args.legacy_profile:
        notice("--legacy-profile 已废弃，将直接进入新的交互菜单。", "warn")

    main_menu(config, args.config, args.model, args.no_build)
    notice("完成。", "ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
