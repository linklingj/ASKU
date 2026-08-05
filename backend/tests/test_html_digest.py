"""HTML 축약 테스트.

각 항목은 축약이 **지우면 안 되는 것**을 지켰는지 본다. 부피가 얼마나 줄었는지는
검증하지 않는다. 덜 줄어드는 것은 손해가 작지만, 구조를 잃으면 그 위에 세우는
규격이 통째로 틀어진다.
"""

import unittest

from bs4 import BeautifulSoup

from app.html_digest import digest_html


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(digest_html(html), "html.parser")


class DigestTests(unittest.TestCase):
    def test_noise_tags_are_dropped(self) -> None:
        html = "<body><script>var a=1</script><style>.a{}</style><p>본문</p></body>"

        result = soup(html)

        self.assertEqual(result.select("script, style"), [])
        self.assertIn("본문", result.get_text())

    def test_selector_attributes_survive_and_styling_is_dropped(self) -> None:
        html = "<div class='b-title-box' id='top' style='color:red' onclick='go()' data-x='1'><a href='/v.do'>글</a></div>"

        tag = soup(html).select_one("div")

        self.assertEqual(tag.get("class"), ["b-title-box"])
        self.assertEqual(tag.get("id"), "top")
        self.assertIsNone(tag.get("style"))
        self.assertIsNone(tag.get("onclick"))

    def test_form_attributes_needed_for_pagination_survive(self) -> None:
        """연세대는 pageForm 의 hidden input 을 조합해 다음 목록 URL 을 만든다."""

        html = "<form name='pageForm' action='/list.do'><input name='page' value='2'></form>"

        result = soup(html)

        self.assertIsNotNone(result.select_one("form[name='pageForm'][action]"))
        self.assertEqual(result.select_one("input").get("value"), "2")

    def test_repeated_rows_are_sampled_not_erased(self) -> None:
        rows = "".join(f"<tr><td><a href='/v.do?id={i}'>공지 {i}</a></td></tr>" for i in range(20))

        result = soup(f"<table><tbody>{rows}</tbody></table>")

        kept = result.select("tbody tr")
        self.assertGreaterEqual(len(kept), 2)  # 반복 구조임을 알 수 있어야 한다
        self.assertLess(len(kept), 20)  # 전부 남기면 접는 의미가 없다

    def test_rows_with_different_classes_each_keep_samples(self) -> None:
        """세종대 고정공지(`tr.b-top-box`)와 일반공지는 클래스가 다르다.

        한쪽만 남으면 그 차이를 알 수 없어, 행 선택자를 고정공지에만 맞추는
        실수를 그대로 반복하게 된다.
        """

        pinned = "".join(f"<tr class='b-top-box'><td>고정 {i}</td></tr>" for i in range(6))
        normal = "".join(f"<tr><td>일반 {i}</td></tr>" for i in range(10))

        result = soup(f"<table><tbody>{pinned}{normal}</tbody></table>")

        self.assertGreater(len(result.select("tr.b-top-box")), 0)
        self.assertGreater(len([tr for tr in result.select("tr") if not tr.get("class")]), 0)

    def test_largest_sibling_survives_collapsing(self) -> None:
        """연세대는 같은 위젯 컨테이너가 여러 개고 게시판이 뒤쪽에 들어 있다.

        앞에서부터 표본을 세면 정작 본문이 든 컨테이너가 통째로 잘린다.
        """

        empty = "".join("<div class='widget'><span>배너</span></div>" for _ in range(8))
        board = "<div class='widget'><table><tbody><tr><td>공지 목록</td></tr></tbody></table></div>"

        result = soup(f"<body>{empty}{board}</body>")

        self.assertIn("공지 목록", result.get_text())

    def test_pagination_region_is_kept_whole(self) -> None:
        pages = "".join(f"<li><a href='?offset={i}0'>{i}</a></li>" for i in range(1, 11))
        html = f"<div class='b-paging'><ul>{pages}<li class='next'><a href='?offset=10'>다음</a></li></ul></div>"

        result = soup(html)

        self.assertIsNotNone(result.select_one(".b-paging .next a[href]"))

    def test_links_inside_collapsed_structure_stay_in_inventory(self) -> None:
        """세종대 하위 게시판 탭은 메뉴 안에 있어 구조를 접으면 함께 사라진다."""

        tabs = "".join(f"<li><a href='/kor/intro/notice{i}.do'>탭 {i}</a></li>" for i in range(1, 11))
        html = f"<body><nav><ul>{tabs}</ul></nav><p>본문</p></body>"

        hrefs = {a["href"] for a in soup(html).select("a[href]")}

        for index in range(1, 11):
            self.assertIn(f"/kor/intro/notice{index}.do", hrefs)

    def test_detail_links_are_folded_by_url_pattern(self) -> None:
        """상세 공지 링크 수백 개는 숫자만 다르므로 표본 몇 개면 충분하다."""

        rows = "".join(f"<div><a href='/notice.do?articleNo={i}'>공지 {i}</a></div>" for i in range(200))

        hrefs = [a["href"] for a in soup(f"<body>{rows}</body>").select("a[href]")]

        self.assertLess(len(hrefs), 200)
        self.assertGreater(len(hrefs), 0)

    def test_long_text_is_truncated_but_present(self) -> None:
        html = f"<p>{'가' * 500}</p>"

        text = soup(html).get_text()

        self.assertLess(len(text.strip()), 200)
        self.assertIn("가", text)


if __name__ == "__main__":
    unittest.main()
