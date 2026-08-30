import unittest
from datetime import datetime, timezone

from generate_feed import (
    build_feed,
    build_html,
    merge_exhibitions,
    parse_exhibitions,
    update_state,
)


CURRENT_HTML = r"""
<html><body>
  <section class="hot-search">
    <a href="/tw/ExhibitionAndEvent/Info/%E9%87%8E%E8%8D%89%E4%B8%8D%E6%9C%8D%E7%AE%A1">野草不服管 MoCA STUDIO</a>
  </section>
  <main>
    <a class="card" href="/tw/ExhibitionAndEvent/Info/%E9%87%8E%E8%8D%89%E4%B8%8D%E6%9C%8D%E7%AE%A1">
      野草不服管 MoCA STUDIO
      <span>2026 07 / 11 Sat.</span>
      <span>2026 09 / 20 Sun.</span>
      + MORE
    </a>
  </main>
</body></html>
"""

UPCOMING_HTML = r"""
<html><body>
  <main>
    <a class="card" href="/tw/ExhibitionAndEvent/Info/%E7%BE%8E%E8%A1%93%E9%A4%A8%E4%B9%8B%E5%BE%8C">
      美術館之後
      <span>2026 09 / 19 Sat.</span>
      <span>2026 12 / 31 Thu.</span>
      + MORE
    </a>
  </main>
</body></html>
"""


class ParserTests(unittest.TestCase):
    def test_extracts_current_exhibition_and_dates(self):
        items = parse_exhibitions(CURRENT_HTML, "當期展覽", "https://example/current")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "野草不服管 MoCA STUDIO")
        self.assertEqual(items[0].start_date, "2026-07-11")
        self.assertEqual(items[0].end_date, "2026-09-20")
        self.assertEqual(items[0].status, "當期展覽")

    def test_extracts_upcoming_exhibition_and_dates(self):
        items = parse_exhibitions(UPCOMING_HTML, "新展預告", "https://example/upcoming")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "美術館之後")
        self.assertEqual(items[0].start_date, "2026-09-19")
        self.assertEqual(items[0].end_date, "2026-12-31")
        self.assertEqual(items[0].status, "新展預告")

    def test_current_wins_if_same_exhibition_is_on_both_pages(self):
        current = parse_exhibitions(CURRENT_HTML, "當期展覽", "https://example/current")
        same_as_upcoming = parse_exhibitions(CURRENT_HTML, "新展預告", "https://example/upcoming")
        merged = merge_exhibitions([current, same_as_upcoming])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].status, "當期展覽")

    def test_feed_title_contains_status_and_exhibition_dates(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        items = merge_exhibitions([
            parse_exhibitions(CURRENT_HTML, "當期展覽", "https://example/current"),
            parse_exhibitions(UPCOMING_HTML, "新展預告", "https://example/upcoming"),
        ])
        state = update_state({"version": 2, "items": {}}, items, now)
        feed = build_feed(state, now)
        titles = [node.text for node in feed.findall("./channel/item/title")]
        self.assertIn("[當期展覽] 野草不服管 MoCA STUDIO｜2026/07/11–2026/09/20", titles)
        self.assertIn("[新展預告] 美術館之後｜2026/09/19–2026/12/31", titles)

    def test_html_contains_both_sections_dates_and_rss_discovery(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        items = merge_exhibitions([
            parse_exhibitions(CURRENT_HTML, "當期展覽", "https://example/current"),
            parse_exhibitions(UPCOMING_HTML, "新展預告", "https://example/upcoming"),
        ])
        state = update_state({"version": 2, "items": {}}, items, now)
        page = build_html(state, now)
        self.assertIn("當期展覽", page)
        self.assertIn("新展預告", page)
        self.assertIn("2026/07/11 ～ 2026/09/20", page)
        self.assertIn("2026/09/19 ～ 2026/12/31", page)
        self.assertIn('type="application/rss+xml"', page)
        self.assertIn("feed.xml", page)


if __name__ == "__main__":
    unittest.main()
