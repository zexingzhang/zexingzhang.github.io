"""Synchronize public reviewing venues from a read-only IMAP mailbox.

The scanner intentionally minimizes mailbox exposure:

* every selectable folder is scanned, but only a small header set is fetched;
* text bodies are fetched only for likely editorial/review messages;
* MIME parts marked as attachments are never fetched;
* subjects, senders, manuscript metadata, and message bodies are never logged or
  written to the repository;
* only high-confidence accepted/completed events with a configured venue alias
  can update ``data/config.yaml``.

Incremental IMAP state contains only hashed folder identifiers, UIDVALIDITY, and
the last processed UID. The GitHub workflow stores it in an Actions cache rather
than committing it to the public repository.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import imaplib
import json
import os
import re
import ssl
import unicodedata
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "config.yaml"
RULES_PATH = ROOT / "data" / "review_email_rules.yaml"
DEFAULT_STATE_PATH = ROOT / ".cache" / "review-email-sync" / "state.json"
STATE_VERSION = 1
HEADER_FIELDS = "DATE FROM SUBJECT MESSAGE-ID CONTENT-TYPE CONTENT-DISPOSITION"
UID_RE = re.compile(rb"\bUID\s+(\d+)\b", re.IGNORECASE)
LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delimiter>"(?:\\.|[^"])*"|NIL)\s+(?P<name>.+)$')


def log(message: str) -> None:
    """Log only aggregate/public-safe information."""

    print(f"[review-sync] {message}")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def decoded_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError, ValueError):
        return str(value)


def normalized_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
    return re.sub(r"\s+", " ", text).strip()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden_depth += 1
        elif tag in {"br", "p", "div", "li", "tr"} and not self.hidden_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in {"p", "div", "li", "tr"} and not self.hidden_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", value)
    return "".join(parser.parts)


def message_text_part(message: Message, max_chars: int) -> str:
    """Decode one non-attachment text part without exposing metadata."""

    if message.get_content_maintype() != "text":
        return ""
    disposition = (message.get_content_disposition() or "").casefold()
    if disposition == "attachment" or message.get_filename():
        return ""
    try:
        payload = message.get_payload(decode=True)
    except Exception:
        return ""
    if payload is None:
        raw_payload = message.get_payload()
        value = raw_payload if isinstance(raw_payload, str) else ""
    else:
        charset = message.get_content_charset() or "utf-8"
        try:
            value = payload.decode(charset, errors="replace")
        except LookupError:
            value = payload.decode("utf-8", errors="replace")
    if message.get_content_subtype() == "html":
        value = html_to_text(value)
    return value[:max_chars]


def local_message_text(message: Message, max_chars: int) -> str:
    """Extract text from an already-local .eml while ignoring attachments."""

    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            value = message_text_part(part, max_chars)
            if value:
                parts.append(value)
            if sum(len(item) for item in parts) >= max_chars:
                break
    else:
        value = message_text_part(message, max_chars)
        if value:
            parts.append(value)
    return "\n".join(parts)[:max_chars]


def compile_patterns(values: Iterable[str]) -> list[re.Pattern[str]]:
    return [re.compile(str(value), re.IGNORECASE) for value in values]


def any_pattern(patterns: Iterable[re.Pattern[str]], value: str) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def is_candidate_header(subject: str, sender: str, rules: dict[str, Any]) -> bool:
    candidate = rules.get("candidate") or {}
    value = f"{subject}\n{sender}"
    return any_pattern(compile_patterns(candidate.get("header_patterns") or []), value)


def classify_review_state(subject: str, body: str, rules: dict[str, Any]) -> str | None:
    """Return a conservative review state; only accepted/completed are publishable."""

    state_patterns = rules.get("state_patterns") or {}
    subject_value = normalized_text(subject)
    combined = normalized_text(f"{subject}\n{body}")

    compiled = {
        name: compile_patterns(state_patterns.get(name) or [])
        for name in ("completed", "accepted", "rejected", "invited")
    }
    if any_pattern(compiled["completed"], subject_value):
        return "completed"
    if any_pattern(compiled["rejected"], subject_value):
        return "rejected"
    if any_pattern(compiled["accepted"], subject_value):
        return "accepted"
    if any_pattern(compiled["completed"], combined):
        return "completed"
    if any_pattern(compiled["rejected"], combined):
        return "rejected"
    if any_pattern(compiled["accepted"], combined):
        return "accepted"
    if any_pattern(compiled["invited"], combined):
        return "invited"
    return None


def alias_in_text(alias: str, value: str) -> bool:
    needle = normalized_text(alias)
    haystack = normalized_text(value)
    if not needle:
        return False
    if re.fullmatch(r"[a-z0-9]+", needle):
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack


def detect_venue(subject: str, sender: str, body: str, rules: dict[str, Any]) -> str | None:
    """Map an email to an allow-listed public venue name."""

    aliases = rules.get("venue_aliases") or {}
    header_value = f"{subject}\n{sender}"
    matches: list[tuple[int, int, str]] = []
    for canonical, values in aliases.items():
        candidates = [str(canonical), *(str(value) for value in (values or []))]
        for alias in candidates:
            if alias_in_text(alias, header_value):
                matches.append((2, len(normalized_text(alias)), str(canonical)))
            elif alias_in_review_context(alias, body):
                matches.append((1, len(normalized_text(alias)), str(canonical)))
    if not matches:
        return None
    matches.sort(reverse=True)
    return matches[0][2]


def alias_in_review_context(alias: str, body: str) -> bool:
    """Require review/editorial context near a body-only venue match."""

    needle = normalized_text(alias)
    haystack = normalized_text(body)
    if not needle:
        return False
    pattern = (
        rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
        if re.fullmatch(r"[a-z0-9]+", needle)
        else re.escape(needle)
    )
    context = re.compile(
        r"review|reviewer|referee|journal|conference|transactions|proceedings|"
        r"editorial|manuscript|审稿|评审|审阅|期刊|会议|稿件",
        re.IGNORECASE,
    )
    for match in re.finditer(pattern, haystack, re.IGNORECASE):
        window = haystack[max(0, match.start() - 180) : match.end() + 180]
        if context.search(window):
            return True
    return False


@dataclass(frozen=True)
class HeaderRecord:
    uid: int
    subject: str
    sender: str
    message_id: str
    raw: bytes


@dataclass
class ScanSummary:
    folders: int = 0
    headers: int = 0
    candidates: int = 0
    accepted: int = 0
    completed: int = 0
    unpublished: int = 0
    duplicate_messages: int = 0


def parse_header(uid: int, raw: bytes) -> HeaderRecord:
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return HeaderRecord(
        uid=uid,
        subject=decoded_header(message.get("Subject")),
        sender=decoded_header(message.get("From")),
        message_id=str(message.get("Message-ID") or "").strip(),
        raw=raw,
    )


def response_literal(data: list[Any] | tuple[Any, ...] | None) -> bytes | None:
    for item in data or []:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
    return None


def fetch_header_batch(
    client: imaplib.IMAP4_SSL, uids: list[int]
) -> dict[int, HeaderRecord]:
    if not uids:
        return {}
    uid_set = ",".join(str(uid) for uid in uids)
    status, data = client.uid(
        "FETCH",
        uid_set,
        f"(UID BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])",
    )
    if status != "OK":
        raise RuntimeError("IMAP header fetch failed")
    records: dict[int, HeaderRecord] = {}
    for item in data or []:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        metadata, raw = item[0], item[1]
        if not isinstance(metadata, bytes) or not isinstance(raw, bytes):
            continue
        match = UID_RE.search(metadata)
        if not match:
            continue
        uid = int(match.group(1))
        records[uid] = parse_header(uid, raw)
    return records


def fetch_section(
    client: imaplib.IMAP4_SSL,
    uid: int,
    section: str,
    max_bytes: int | None = None,
) -> bytes | None:
    partial = f"<0.{max_bytes}>" if max_bytes else ""
    status, data = client.uid("FETCH", str(uid), f"(BODY.PEEK[{section}]{partial})")
    if status != "OK":
        raise RuntimeError("IMAP body fetch failed")
    return response_literal(data)


def decode_fetched_part(mime_header: bytes, payload: bytes, max_chars: int) -> str:
    raw = mime_header.rstrip(b"\r\n") + b"\r\n\r\n" + payload
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return message_text_part(message, max_chars)


def fetch_text_children(
    client: imaplib.IMAP4_SSL,
    uid: int,
    parent: str,
    max_bytes: int,
    max_chars: int,
    max_parts: int,
    depth: int = 0,
) -> list[str]:
    if depth > 4:
        return []
    values: list[str] = []
    for index in range(1, max_parts + 1):
        section = f"{parent}.{index}" if parent else str(index)
        mime_header = fetch_section(client, uid, f"{section}.MIME")
        if mime_header is None:
            break
        part = BytesParser(policy=policy.default).parsebytes(mime_header, headersonly=True)
        disposition = (part.get_content_disposition() or "").casefold()
        if disposition == "attachment" or part.get_filename():
            continue
        maintype = part.get_content_maintype()
        if maintype == "multipart":
            values.extend(
                fetch_text_children(
                    client,
                    uid,
                    section,
                    max_bytes,
                    max_chars,
                    max_parts,
                    depth + 1,
                )
            )
        elif maintype == "text":
            payload = fetch_section(client, uid, section, max_bytes)
            if payload is not None:
                value = decode_fetched_part(mime_header, payload, max_chars)
                if value:
                    values.append(value)
                    break
        if sum(len(value) for value in values) >= max_chars:
            break
    return values


def fetch_message_text(
    client: imaplib.IMAP4_SSL,
    record: HeaderRecord,
    max_bytes: int,
    max_chars: int,
    max_parts: int,
) -> str:
    root = BytesParser(policy=policy.default).parsebytes(record.raw, headersonly=True)
    if root.get_content_maintype() == "multipart":
        values = fetch_text_children(
            client,
            record.uid,
            "",
            max_bytes,
            max_chars,
            max_parts,
        )
    elif root.get_content_maintype() == "text":
        payload = fetch_section(client, record.uid, "TEXT", max_bytes)
        values = [decode_fetched_part(record.raw, payload, max_chars)] if payload is not None else []
    else:
        values = []
    return "\n".join(value for value in values if value)[:max_chars]


def unquote_imap_name(value: bytes) -> str:
    value = value.strip()
    if value.startswith(b'"') and value.endswith(b'"'):
        value = value[1:-1]
        value = re.sub(rb"\\([\\\"])", rb"\1", value)
    return value.decode("ascii", errors="strict")


def quote_imap_name(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def selectable_folders(client: imaplib.IMAP4_SSL) -> list[str]:
    status, data = client.list("", "*")
    if status != "OK":
        raise RuntimeError("IMAP folder listing failed")
    folders: list[str] = []
    for raw in data or []:
        if not isinstance(raw, bytes):
            continue
        match = LIST_RE.match(raw)
        if not match:
            continue
        flags = match.group("flags").lower()
        if b"\\noselect" in flags:
            continue
        try:
            folders.append(unquote_imap_name(match.group("name")))
        except UnicodeDecodeError:
            continue
    if not folders:
        folders.append("INBOX")
    return folders


def folder_key(folder: str) -> str:
    return hashlib.sha256(folder.encode("ascii", errors="replace")).hexdigest()[:24]


def account_key(address: str) -> str:
    return hashlib.sha256(address.strip().casefold().encode("utf-8")).hexdigest()


def load_state(path: Path, address: str, full_scan: bool) -> dict[str, Any]:
    empty = {"version": STATE_VERSION, "account": account_key(address), "folders": {}}
    if full_scan or not path.exists():
        return empty
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if state.get("version") != STATE_VERSION or state.get("account") != empty["account"]:
        return empty
    if not isinstance(state.get("folders"), dict):
        return empty
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def uidvalidity(client: imaplib.IMAP4_SSL) -> str:
    _, values = client.response("UIDVALIDITY")
    if values and values[0] is not None:
        value = values[0]
        return value.decode("ascii", errors="replace") if isinstance(value, bytes) else str(value)
    return ""


def search_uids(client: imaplib.IMAP4_SSL, first_uid: int) -> list[int]:
    if first_uid <= 1:
        status, data = client.uid("SEARCH", None, "ALL")
    else:
        status, data = client.uid("SEARCH", None, "UID", f"{first_uid}:*")
    if status != "OK":
        raise RuntimeError("IMAP UID search failed")
    raw = data[0] if data else b""
    return [int(value) for value in raw.split() if value.isdigit() and int(value) >= first_uid]


def chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def scan_mailbox(
    address: str,
    password: str,
    rules: dict[str, Any],
    state: dict[str, Any],
) -> tuple[set[str], dict[str, Any], ScanSummary]:
    imap_config = rules.get("imap") or {}
    host = str(imap_config.get("host") or "imap.exmail.qq.com")
    port = int(imap_config.get("port") or 993)
    batch_size = max(1, min(int(imap_config.get("header_batch_size") or 100), 500))
    max_bytes = max(4096, int(imap_config.get("max_text_part_bytes") or 262144))
    max_chars = max(4096, int(imap_config.get("max_message_text_chars") or 300000))
    max_parts = max(1, min(int(imap_config.get("max_mime_parts") or 20), 100))
    publish_states = set(rules.get("publish_states") or ["accepted", "completed"])

    summary = ScanSummary()
    venues: set[str] = set()
    seen_messages: set[str] = set()
    context = ssl.create_default_context()
    client = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=45)
    try:
        client.login(address, password)
        folders = selectable_folders(client)
        for folder_index, folder in enumerate(folders, start=1):
            status, _ = client.select(quote_imap_name(folder), readonly=True)
            if status != "OK":
                raise RuntimeError("IMAP read-only folder selection failed")
            summary.folders += 1
            key = folder_key(folder)
            validity = uidvalidity(client)
            prior = (state.get("folders") or {}).get(key) or {}
            first_uid = int(prior.get("last_uid") or 0) + 1 if prior.get("uidvalidity") == validity else 1
            uids = search_uids(client, first_uid)
            log(f"Scanning folder {folder_index}/{len(folders)}: {len(uids)} new headers")
            for uid_batch in chunks(uids, batch_size):
                records = fetch_header_batch(client, uid_batch)
                if len(records) != len(uid_batch):
                    raise RuntimeError("IMAP returned an incomplete header batch")
                for uid in uid_batch:
                    record = records[uid]
                    summary.headers += 1
                    message_key = record.message_id.casefold() if record.message_id else hashlib.sha256(record.raw).hexdigest()
                    if message_key in seen_messages:
                        summary.duplicate_messages += 1
                        continue
                    seen_messages.add(message_key)
                    if not is_candidate_header(record.subject, record.sender, rules):
                        continue
                    summary.candidates += 1
                    body = fetch_message_text(client, record, max_bytes, max_chars, max_parts)
                    state_name = classify_review_state(record.subject, body, rules)
                    if state_name not in publish_states:
                        continue
                    if state_name == "accepted":
                        summary.accepted += 1
                    elif state_name == "completed":
                        summary.completed += 1
                    venue = detect_venue(record.subject, record.sender, body, rules)
                    if venue:
                        venues.add(venue)
                    else:
                        summary.unpublished += 1
            if uids:
                state.setdefault("folders", {})[key] = {
                    "uidvalidity": validity,
                    "last_uid": max(uids),
                }
        return venues, state, summary
    finally:
        try:
            client.logout()
        except Exception:
            pass


def yaml_list_scalar(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .&/+():-]*", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def update_reviewing_venues(path: Path, discovered: Iterable[str], dry_run: bool) -> list[str]:
    config = load_yaml(path)
    current = [str(value) for value in ((config.get("reviewing") or {}).get("venues") or [])]
    additions = sorted(set(discovered).difference(current), key=str.casefold)
    if not additions or dry_run:
        return additions

    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    lines = raw.decode("utf-8").splitlines()
    reviewing_index = next((i for i, line in enumerate(lines) if line == "reviewing:"), None)
    if reviewing_index is None:
        raise RuntimeError("data/config.yaml has no reviewing section")
    venues_index = next(
        (i for i in range(reviewing_index + 1, len(lines)) if lines[i] == "  venues:"),
        None,
    )
    if venues_index is None:
        raise RuntimeError("data/config.yaml has no reviewing.venues list")
    insert_at = venues_index + 1
    while insert_at < len(lines) and lines[insert_at].startswith("  - "):
        insert_at += 1
    new_lines = [f"  - {yaml_list_scalar(value)}" for value in additions]
    lines[insert_at:insert_at] = new_lines
    path.write_bytes((newline.join(lines) + newline).encode("utf-8"))
    return additions


def classify_eml(path: Path, rules: dict[str, Any]) -> tuple[str | None, str | None]:
    raw = path.read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = decoded_header(message.get("Subject"))
    sender = decoded_header(message.get("From"))
    max_chars = int((rules.get("imap") or {}).get("max_message_text_chars") or 300000)
    body = local_message_text(message, max_chars)
    state_name = classify_review_state(subject, body, rules)
    venue = detect_venue(subject, sender, body, rules) if state_name else None
    return state_name, venue


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--rules", type=Path, default=RULES_PATH)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--dry-run", action="store_true", help="scan without writing config or state")
    parser.add_argument("--full-scan", action="store_true", help="ignore cached UIDs and scan all history")
    parser.add_argument(
        "--classify-eml",
        type=Path,
        action="append",
        default=[],
        help="classify a local redacted .eml without connecting to IMAP",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rules = load_yaml(args.rules)
    if args.classify_eml:
        for eml_path in args.classify_eml:
            state_name, venue = classify_eml(eml_path, rules)
            log(f"Local sample result: state={state_name or 'none'}, venue={venue or 'unmapped'}")
        return 0

    address = os.environ.get("REVIEW_EMAIL_ADDRESS", "").strip()
    password = os.environ.get("REVIEW_EMAIL_APP_PASSWORD", "")
    if not address or not password:
        log("Mailbox credentials are missing; no mailbox access was attempted.")
        return 2

    state = load_state(args.state_path, address, args.full_scan)
    venues, next_state, summary = scan_mailbox(address, password, rules, state)
    additions = update_reviewing_venues(args.config, venues, args.dry_run)
    if not args.dry_run:
        save_state(args.state_path, next_state)

    log(
        "Scan complete: "
        f"folders={summary.folders}, headers={summary.headers}, "
        f"candidates={summary.candidates}, accepted={summary.accepted}, "
        f"completed={summary.completed}, unmapped_publishable={summary.unpublished}"
    )
    if additions:
        prefix = "Would add" if args.dry_run else "Added"
        log(f"{prefix} public reviewing venues: {', '.join(additions)}")
    else:
        log("No new public reviewing venues")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (imaplib.IMAP4.error, OSError, RuntimeError, ssl.SSLError) as exc:
        log(f"Synchronization failed safely ({type(exc).__name__}); no mailbox content was logged.")
        raise SystemExit(1) from exc
