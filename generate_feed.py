#!/usr/bin/env python3
"""Generate RSS + a small HTML exhibition dashboard for MoCA Taipei.

Monitored official pages:
- Current Exhibition (當期展覽)
- Upcoming (新展預告)

The official exhibition detail URL is the stable identity, so an exhibition
moving from Upcoming to Current keeps the same RSS GUID and is not duplicated.
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
from zoneinfo import ZoneInfo

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
FEED_PATH = Path("docs/feed.xml")
HTML_PATH = Path("docs/index.html")
MAX_ARCHIVED_ITEMS = 100
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

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


def title_from_anchor(url: str, anchor_text: str) -> str:
    """Prefer the visible card title, then fall back to the URL slug.

    MoCA's URL slug can be shorter than the visible title (for example a card
    may add "MoCA STUDIO"), so using the card text preserves the fuller title.
    """
    match = DATE_RE.search(anchor_text)
    prefix = anchor_text[: match.start()] if match else anchor_text
    prefix = re.sub(r"^(?:FULL\s*已額滿\s*)?(?:BOOK\s*線上報名\s*)?", "", prefix)
    prefix = re.sub(r"\s*\+\s*MORE\s*$", "", prefix, flags=re.IGNORECASE)
    prefix = " ".join(prefix.split()).strip(" ｜|-–—")
    if prefix:
        return prefix

    segment = unquote(urlparse(url).path.rstrip("/").split("/")[-1]).strip()
    return segment or "未命名展覽"


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
                title=title_from_anchor(absolute, anchor.text),
                url=absolute,
                start_date=iso_date(dates[0]),
                end_date=iso_date(dates[1]),
                status=status,
                source_url=source_url,
            )
        )

    return exhibitions


def merge_exhibitions(groups: Iterable[Iterable[Exhibition]]) -> list[Exhibition]:
    """Deduplicate by official detail URL; Current wins over Upcoming."""
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
        item.pop("current", None)  # compatibility with v1 state files

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
        ET.SubElement(item, "title").text = f"[{status}] {title}｜{start_date}–{end_date}"
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


def _html_list_item(entry: dict) -> str:
    title = html.escape(entry.get("title", "未命名展覽"))
    url = html.escape(entry.get("url", BASE_URL), quote=True)
    start = html.escape(human_date(entry.get("start_date", "")))
    end = html.escape(human_date(entry.get("end_date", "")))
    return (
        f'    <li><a href="{url}" target="_blank" rel="noopener">{title}</a>'
        f'<br>展期：{start} ～ {end}</li>'
    )


def build_html(state: dict, now: datetime) -> str:
    listed = [item for item in state.get("items", {}).values() if item.get("listed")]
    current = sorted(
        (item for item in listed if item.get("status") == "當期展覽"),
        key=lambda item: (item.get("end_date", "9999-99-99"), item.get("start_date", "")),
    )
    upcoming = sorted(
        (item for item in listed if item.get("status") == "新展預告"),
        key=lambda item: (item.get("start_date", "9999-99-99"), item.get("end_date", "")),
    )

    current_items = "\n".join(_html_list_item(item) for item in current) or "    <li>目前沒有抓到當期展覽。</li>"
    upcoming_items = "\n".join(_html_list_item(item) for item in upcoming) or "    <li>目前沒有抓到新展預告。</li>"
    updated = now.astimezone(TAIPEI_TZ).strftime("%Y/%m/%d %H:%M")

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="臺北當代藝術館當期展覽與新展預告 RSS，自動整理展覽日期與官方連結。">
  <link rel="alternate" type="application/rss+xml" title="{html.escape(FEED_TITLE)}" href="feed.xml">
  <title>{html.escape(FEED_TITLE)}</title>
</head>
<body>
  <h1>臺北當代藝術館｜當期展覽＋新展預告 RSS</h1>

  <p><a href="feed.xml">開啟 RSS Feed</a></p>
  <p>
    <a href="{SOURCES[0][1]}" target="_blank" rel="noopener">官方當期展覽</a>
    ｜
    <a href="{SOURCES[1][1]}" target="_blank" rel="noopener">官方新展預告</a>
  </p>

  <h2>當期展覽</h2>
  <ul>
{current_items}
  </ul>

  <h2>新展預告</h2>
  <ul>
{upcoming_items}
  </ul>

  <hr>
  <p>最後更新：{updated}（台北時間）</p>
  <p>非臺北當代藝術館官方服務；所有展覽資訊以館方網站為準。</p>
</body>
</html>
"""


def write_outputs(state: dict, feed: ET.ElementTree, page_html: str) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(feed, space="  ")
    feed.write(FEED_PATH, encoding="utf-8", xml_declaration=True)
    HTML_PATH.write_text(page_html, encoding="utf-8")


def main() -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    parsed_groups: list[list[Exhibition]] = []

    for status, source_url, expected_label in SOURCES:
        page_html = fetch_page(source_url)
        # Fail safely if MoCA returns an error/anti-bot page instead of the list.
        if expected_label not in page_html or "Exhibitions" not in page_html:
            raise RuntimeError(
                f"MoCA 回傳內容不像「{expected_label}」頁面；本次停止更新以保護既有 RSS。"
            )
        parsed_groups.append(parse_exhibitions(page_html, status, source_url))

    exhibitions = merge_exhibitions(parsed_groups)
    state = update_state(load_state(), exhibitions, now)
    feed = build_feed(state, now)
    page_html = build_html(state, now)
    write_outputs(state, feed, page_html)

    counts: dict[str, int] = {}
    for exhibition in exhibitions:
        counts[exhibition.status] = counts.get(exhibition.status, 0) + 1

    print(f"找到目前當期展覽：{counts.get('當期展覽', 0)} 筆")
    print(f"找到目前新展預告：{counts.get('新展預告', 0)} 筆")
    print(f"RSS 封存項目：{len(state['items'])} 筆")
    print(f"已輸出：{FEED_PATH}")
    print(f"已輸出：{HTML_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # GitHub Actions should visibly fail on site changes.
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
