from __future__ import annotations

import re
from datetime import date

from ai_market_radar.fetchers.base import collapse, sha256
from ai_market_radar.models import RawEntry, SourceConfig

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_VERSION_RE = re.compile(r"^[vV]?\d+\.\d+(\.\d+)*([-+][0-9a-zA-Z.]+)?$")
_FULL_MONTH_RE = re.compile(
    r"^(january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)[^0-9]*",
    re.IGNORECASE,
)


def _leading_label(text: str) -> str | None:
    """Title candidate: first bold phrase on a bullet/line, e.g. ``- **X**: ...``."""
    stripped = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", text.strip())
    match = re.match(r"\*\*(.+?)\*\*", stripped)
    return match.group(1) if match else None


def _plain_text(blocks: list[tuple[str, str]]) -> str:
    """Convert changelog markdown into readable plain text for excerpts."""
    parts: list[str] = []
    for _marker, text in blocks:
        line = text.strip()
        line = re.sub(r"^[>\s]*(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = line.replace("`", "").replace("**", "")
        if line:
            parts.append(line)
    return collapse(" ".join(parts))


def _date_parts(text: str) -> tuple[int | None, int | None, int | None]:
    year = month = day = None
    for token in text.split():
        token = token.strip(".,;:").lower()
        if re.fullmatch(r"\d{4}", token):
            year = int(token)
        elif token in _MONTHS:
            month = _MONTHS[token]
        elif re.fullmatch(r"\d{1,2}", token) and month is not None and day is None:
            day = int(token)
    if not _FULL_MONTH_RE.match(text):
        return None, None, None
    return year, month, day


def _parse_published(heading: str, ctx: tuple[int, int] | None) -> date | None:
    year, month, day = _date_parts(heading)
    if year is None and ctx is not None:
        year = ctx[1]
    if year is None or month is None or day is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def fetch_markdown(source: SourceConfig, client) -> list[RawEntry]:
    response = client.get(source.url)
    response.raise_for_status()
    return parse_changelog(response.text, source)


def parse_changelog(text: str, source: SourceConfig) -> list[RawEntry]:
    """Split a changelog-style Markdown doc into dated/versioned entries.

    Handles the three observed layouts:
    - OpenAI:  ``## <Month, Year>`` group + ``### <Mon d>`` entries
    - Anthropic docs:  ``### <Month d, Year>`` entries
    - Claude Code:  ``## <semver>`` version entries
    """
    entries: list[dict] = []
    current: dict | None = None
    ctx: tuple[int, int] | None = None  # (month, year) of enclosing group

    def close() -> None:
        nonlocal current
        if current is not None:
            entries.append(current)
            current = None

    for raw in text.splitlines():
        heading_match = _HEADING_RE.match(raw)
        if heading_match:
            level = len(heading_match.group(1))
            heading = collapse(heading_match.group(2))
            if not heading:
                continue
            year, month, day = _date_parts(heading)
            if (
                level <= 2
                and _VERSION_RE.match(heading)
                and (current is None or current["kind"] == "version")
            ):
                close()
                current = {
                    "kind": "version",
                    "heading": heading,
                    "body": [],
                    "date": None,
                }
            elif (
                month is not None
                and year is not None
                and day is not None
                and level >= 3
            ):
                close()
                current = {
                    "kind": "dated",
                    "heading": heading,
                    "body": [],
                    "date": date(year, month, day),
                }
            elif month is not None and day is not None and year is None and level >= 3:
                parsed = None
                if ctx is not None:
                    parsed = _parse_published(heading, ctx)
                close()
                current = {
                    "kind": "dated",
                    "heading": heading,
                    "body": [],
                    "date": parsed,
                }
            elif month is not None and year is not None and day is None and level <= 2:
                close()
                ctx = (month, year)
            elif current is not None:
                current["body"].append((("#" * level), heading))
            continue
        if current is not None:
            current["body"].append(("", raw))

    close()

    raw_entries: list[RawEntry] = []
    for entry in entries:
        body_text = "\n".join(
            marker + " " + text if marker else text for marker, text in entry["body"]
        )
        plain_body = collapse(body_text)
        published = entry["date"]
        heading_text = entry["heading"]
        published_iso = published.isoformat() if published else None

        title = None
        if entry["kind"] == "version":
            title = heading_text
        else:
            for marker, text in entry["body"]:
                if marker and text:
                    title = text
                    break
                lead = _leading_label(text)
                if lead:
                    title = lead
                    break
            if title is None and published:
                title = f"{published:%B} {published.day}, {published.year}"
            elif title is None:
                title = heading_text
        title = collapse(re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title or ""))[:200]

        identity = f"{heading_text}\n{plain_body[:400]}"
        raw_entries.append(
            RawEntry(
                title=title,
                url=source.url,
                published_at=published_iso,
                excerpt=_plain_text(entry["body"])[:2000],
                hash_value=sha256(identity),
            )
        )

    if not raw_entries:
        plain = collapse(text)
        raw_entries.append(
            RawEntry(
                title=source.name,
                url=source.url,
                excerpt=plain[:2000],
                hash_value=sha256(plain),
            )
        )
    return raw_entries
