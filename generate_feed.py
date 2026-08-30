#!/usr/bin/env python3
"""Generate one RSS 2.0 feed for MoCA Taipei current + upcoming exhibitions.

The parser intentionally avoids brittle CSS selectors. It looks for links to
MoCA exhibition detail pages whose anchor text contains two exhibition dates.
Two official pages are monitored:
- Current Exhibition (當期展覽)
- Upcoming (新展預告)

The official exhibition detail URL is used as the stable identity, so an
exhibition moving from Upcoming to Current does not become a duplicate item.
"""

from __future__ import annotations

import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

BASE_URL = "https://www.mocataipei.org.tw"
EXHIBITIONS_URL = f"{BASE_URL}/tw/ExhibitionAndEvent/Exhibitions"
SOURCES = (
    (
        "當期展覽",
        f"{BASE_URL}/tw/ExhibitionAndEvent/Exhibitions/Current%20Exhibition",
        "當期展覽",
    ),
    (
        "新展預告",
        f"{BASE_URL}/tw/ExhibitionAndEvent/Exhibitions/Upcoming",
        "新展預告",
    ),
)
FEED_TITLE = "臺北當代藝術館｜當期展覽＋新展預告"
FEED_DESCRIPTION = (
    "自動追蹤臺北當代藝術館（MoCA Taipei）官方網站的當期展覽與新展預告，"
    "並附上每檔展覽的開始與結束日期。"
)
STATE_PATH = Path("state.json")
OUTPUT_PATH = Path("docs/feed.xml")
MAX_ARCHIVED_ITEMS = 100

# Matches both "2026 09 / 19" and "2026 / 09 / 19".
DATE_RE = re.compile(
    r"(?P<year>20\d{2})\s*/?\s*(?P<month>\d{1,2})\s*/\s*(?P<day>\d{1,2})"
)
DETAIL_PATH_MARKER = "/tw/ExhibitionAndEvent/Info/"


@dataclass(frozen=True)
class Anchor:
    href: str
    text: str


@dataclass(frozen=True)
class Exhibition:
    title: str
    url: str
    start_date: str
    end_date: str
    status: str
    source_url: str


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._parts: list[str] = []
        self.anchors: list[Anchor] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._href is not None:
            return
        attr_map = dict(attrs)
        href = attr_map.get("href")
        if href:
            self._href = href
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        text = " ".join("".join(self._parts).split())
        self.anchors.append(Anchor(self._href, text))
        self._href = None
        self._parts = []


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def title_from_url(url: str, fallback_text: str) -> str:
    segment = unquote(urlparse(url).path.rstrip("/").split("/")[-1]).strip()
    if segment and not segment.isdigit():
        return segment

    match = DATE_RE.search(fallback_text)
    prefix = fallback_text[: match.start()] if match else fallback_text
    prefix = re.sub(r"^(?:FULL\s*已額滿\s*)?(?:BOOK\s*線上報名\s*)?", "", prefix).strip()
    return prefix or "未命名展覽"


def iso_date(match: re.Match[str]) -> str:
    value = datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
    )
    return value.strftime("%Y-%m-%d")


def parse_exhibitions(
    page_html: str,
    status: str = "展覽",
    source_url: str = EXHIBITIONS_URL,
) -> list[Exhibition]:
    parser = AnchorParser()
    parser.feed(page_html)

    exhibitions: list[Exhibition] = []
    seen: set[str] = set()

    for anchor in parser.anchors:
        absolute = canonicalize_url(urljoin(BASE_URL, anchor.href))
        if DETAIL_PATH_MARKER not in urlparse(absolute).path:
            continue

        dates = list(DATE_RE.finditer(anchor.text))
        # Hot-search links have a title but no two-date exhibition range.
        if len(dates) < 2:
            continue

        if absolute in seen:
            continue
        seen.add(absolute)

        exhibitions.append(
            Exhibition(
                title=title_from_url(absolute, anchor.text),
                url=absolute,
                start_date=iso_date(dates[0]),
                end_date=iso_date(dates[1]),
                status=status,
                source_url=source_url,
            )
        )

    return exhibitions


def merge_exhibitions(groups: Iterable[Iterable[Exhibition]]) -> list[Exhibition]:
    """Deduplicate by official detail URL.

    SOURCES is ordered Current first, so if the site briefly lists an exhibition
    in both tabs during a transition, the Current classification wins.
    """
    merged: dict[str, Exhibition] = {}
    for group in groups:
        for exhibition in group:
            merged.setdefault(exhibition.url, exhibition)
    return list(merged.values())


def fetch_page(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed trusted URL
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 2, "items": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("items"), dict):
            raise ValueError("state.json items must be an object")
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"無法讀取 {STATE_PATH}: {exc}") from exc


def update_state(state: dict, exhibitions: Iterable[Exhibition], now: datetime) -> dict:
    now_iso = now.isoformat()
    items: dict[str, dict] = state.setdefault("items", {})

    for item in items.values():
        item["listed"] = False
        # Compatibility with v1 state files.
        item.pop("current", None)

    for exhibition in exhibitions:
        old = items.get(exhibition.url)
        if old is None:
            old = {"first_seen": now_iso}
            items[exhibition.url] = old

        old.update(
            {
                "title": exhibition.title,
                "url": exhibition.url,
                "start_date": exhibition.start_date,
                "end_date": exhibition.end_date,
                "status": exhibition.status,
                "source_url": exhibition.source_url,
                "last_seen": now_iso,
                "listed": True,
            }
        )

    # Keep the archive bounded. Stable first_seen timestamps are preserved.
    ordered = sorted(
        items.items(),
        key=lambda pair: pair[1].get("first_seen", ""),
        reverse=True,
    )[:MAX_ARCHIVED_ITEMS]
    state["items"] = dict(ordered)
    state["version"] = 2
    state["updated_at"] = now_iso
    return state


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def human_date(value: str) -> str:
    return value.replace("-", "/")


def build_feed(state: dict, now: datetime) -> ET.ElementTree:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = FEED_TITLE
    ET.SubElement(channel, "link").text = EXHIBITIONS_URL
    ET.SubElement(channel, "description").text = FEED_DESCRIPTION
    ET.SubElement(channel, "language").text = "zh-TW"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now)
    ET.SubElement(channel, "generator").text = "moca-rss GitHub Actions"

    entries = sorted(
        state.get("items", {}).values(),
        key=lambda item: item.get("first_seen", ""),
        reverse=True,
    )

    for entry in entries:
        status = entry.get("status", "展覽")
        start_date = human_date(entry.get("start_date", ""))
        end_date = human_date(entry.get("end_date", ""))
        title = entry.get("title", "未命名展覽")

        item = ET.SubElement(channel, "item")
        # Date is deliberately included in the RSS title so readers that hide
        # descriptions still show the exhibition period at a glance.
        ET.SubElement(item, "title").text = (
            f"[{status}] {title}｜{start_date}–{end_date}"
        )
        ET.SubElement(item, "link").text = entry["url"]
        guid = ET.SubElement(item, "guid", {"isPermaLink": "true"})
        guid.text = entry["url"]
        ET.SubElement(item, "pubDate").text = format_datetime(parse_dt(entry["first_seen"]))
        ET.SubElement(item, "category").text = status
        ET.SubElement(item, "category").text = "臺北當代藝術館"

        listing_status = (
            f"目前列於「{status}」"
            if entry.get("listed")
            else "目前已不在「當期展覽／新展預告」清單"
        )
        source_url = entry.get("source_url", EXHIBITIONS_URL)
        description_html = (
            f"<p><strong>分類：</strong>{html.escape(status)}</p>"
            f"<p><strong>展期：</strong>{html.escape(start_date)} ～ {html.escape(end_date)}</p>"
            f"<p>{html.escape(listing_status)}</p>"
            f"<p><a href=\"{html.escape(entry['url'], quote=True)}\">查看官方展覽頁面</a>"
            f" ｜ <a href=\"{html.escape(source_url, quote=True)}\">查看官方{html.escape(status)}</a></p>"
        )
        ET.SubElement(item, "description").text = description_html

    return ET.ElementTree(rss)


def write_outputs(state: dict, feed: ET.ElementTree) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(feed, space="  ")
    feed.write(OUTPUT_PATH, encoding="utf-8", xml_declaration=True)


def main() -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    parsed_groups: list[list[Exhibition]] = []

    for status, source_url, expected_label in SOURCES:
        page_html = fetch_page(source_url)

        # Fail safely if MoCA returns an error/anti-bot page instead of the
        # exhibition list. A valid page is allowed to contain zero cards.
        if expected_label not in page_html or "Exhibitions" not in page_html:
            raise RuntimeError(
                f"MoCA 回傳內容不像「{expected_label}」頁面；本次停止更新以保護既有 RSS。"
            )

        parsed_groups.append(parse_exhibitions(page_html, status, source_url))

    exhibitions = merge_exhibitions(parsed_groups)
    state = update_state(load_state(), exhibitions, now)
    feed = build_feed(state, now)
    write_outputs(state, feed)

    counts: dict[str, int] = {}
    for exhibition in exhibitions:
        counts[exhibition.status] = counts.get(exhibition.status, 0) + 1

    print(f"找到目前當期展覽：{counts.get('當期展覽', 0)} 筆")
    print(f"找到目前新展預告：{counts.get('新展預告', 0)} 筆")
    print(f"RSS 封存項目：{len(state['items'])} 筆")
    print(f"已輸出：{OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # GitHub Actions should visibly fail on site changes.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
