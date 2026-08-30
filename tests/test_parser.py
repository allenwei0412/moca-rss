import unittest

from generate_feed import parse_exhibitions


SAMPLE_HTML = r"""
<html><body>
  <section class="hot-search">
    <a href="/tw/ExhibitionAndEvent/Info/%E7%BE%8E%E8%A1%93%E9%A4%A8%E4%B9%8B%E5%BE%8C">美術館之後</a>
  </section>
  <main>
    <a class="card" href="/tw/ExhibitionAndEvent/Info/%E7%BE%8E%E8%A1%93%E9%A4%A8%E4%B9%8B%E5%BE%8C">
      美術館之後
      <span>2026 09 / 19 Sat.</span>
      <span>2026 12 / 31 Thu.</span>
      + MORE
    </a>
    <a class="card" href="/tw/ExhibitionAndEvent/Info/%E6%B8%AC%E8%A9%A6%E5%B1%95">
      測試展 2027 / 01 / 02 Sat. 2027 / 03 / 04 Thu.
    </a>
  </main>
</body></html>
"""


class ParserTests(unittest.TestCase):
    def test_extracts_cards_and_ignores_hot_search(self):
        items = parse_exhibitions(SAMPLE_HTML)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "美術館之後")
        self.assertEqual(items[0].start_date, "2026-09-19")
        self.assertEqual(items[0].end_date, "2026-12-31")
        self.assertEqual(items[1].title, "測試展")
        self.assertEqual(items[1].start_date, "2027-01-02")
        self.assertEqual(items[1].end_date, "2027-03-04")


if __name__ == "__main__":
    unittest.main()
