"""하위 게시판 자동 발견 테스트.

라벨만 믿으면 '인공지능대학'(공지가 우연히 들어감)이나 자유게시판까지 들어오고,
URL 패턴만 믿으면 규칙적인 학교 말고는 찾지 못한다. 실제로 목록이 읽히는지로
판정하는지 확인한다.
"""

import unittest

from app.board_discovery import find_boards
from app.crawler import CommonNoticeAdapter


BASE = "https://x.ac.kr/notice.do"


def board_html(rows: int = 5) -> str:
    body = "".join(
        f"<tr><td><a href='/v.do?id={i}'>공지 제목 {i}</a></td><td>학사팀</td><td>2026-08-0{i % 9}</td></tr>"
        for i in range(1, rows + 1)
    )
    return f"<table><tbody>{body}</tbody></table>"


def listing_with_menu(*links: tuple[str, str]) -> str:
    menu = "".join(f"<li><a href='{href}'>{label}</a></li>" for label, href in links)
    return f"<body><nav><ul><li><a href='/notice.do'>공지사항</a></li>{menu}</ul></nav>{board_html()}</body>"


class FindBoardsTests(unittest.TestCase):
    def fetch_map(self, pages: dict[str, str]):
        return lambda url: pages.get(url)

    def test_real_boards_are_added(self) -> None:
        html = listing_with_menu(("장학공지", "/scholarship.do"), ("채용공고", "/jobs.do"))
        pages = {"https://x.ac.kr/scholarship.do": board_html(), "https://x.ac.kr/jobs.do": board_html()}

        boards = find_boards(html, BASE, CommonNoticeAdapter(), self.fetch_map(pages))

        self.assertEqual([b.label for b in boards], ["공지사항", "장학공지", "채용공고"])
        self.assertEqual(boards[0].url, BASE)  # 기준 URL 이 항상 첫 번째

    def test_page_without_a_listing_is_rejected(self) -> None:
        """라벨에 '공지' 가 들어가도 목록이 아니면 게시판이 아니다(예: 인공지능대학)."""

        html = listing_with_menu(("인공지능대학", "/ai.do"))
        pages = {"https://x.ac.kr/ai.do": "<main><h1>학과 소개</h1><p>안내 문구</p></main>"}

        boards = find_boards(html, BASE, CommonNoticeAdapter(), self.fetch_map(pages))

        self.assertEqual(len(boards), 1)

    def test_excluded_labels_are_not_even_fetched(self) -> None:
        """자유게시판·자료실은 미리 걸러 남의 서버에 보내는 요청을 줄인다."""

        html = listing_with_menu(("자유게시판", "/free.do"), ("자료실", "/archive.do"))
        requested: list[str] = []

        def fetch(url: str) -> str | None:
            requested.append(url)
            return board_html()

        boards = find_boards(html, BASE, CommonNoticeAdapter(), fetch)

        self.assertEqual(len(boards), 1)
        self.assertEqual(requested, [])

    def test_other_hosts_are_ignored(self) -> None:
        """다른 도메인을 게시판으로 잡으면 남의 사이트를 긁게 된다."""

        html = listing_with_menu(("장학공지", "https://other.com/scholarship.do"))

        boards = find_boards(html, BASE, CommonNoticeAdapter(), lambda _url: board_html())

        self.assertEqual(len(boards), 1)

    def test_short_listing_is_not_a_board(self) -> None:
        """한두 줄짜리는 목록이 아니라 안내 페이지다."""

        html = listing_with_menu(("장학공지", "/scholarship.do"))
        pages = {"https://x.ac.kr/scholarship.do": board_html(rows=1)}

        boards = find_boards(html, BASE, CommonNoticeAdapter(), self.fetch_map(pages))

        self.assertEqual(len(boards), 1)

    def test_unreachable_candidate_is_skipped(self) -> None:
        html = listing_with_menu(("장학공지", "/scholarship.do"), ("채용공고", "/jobs.do"))
        pages = {"https://x.ac.kr/jobs.do": board_html()}  # 장학은 받지 못한다

        boards = find_boards(html, BASE, CommonNoticeAdapter(), self.fetch_map(pages))

        self.assertEqual([b.label for b in boards], ["공지사항", "채용공고"])

    def test_candidate_count_is_capped(self) -> None:
        """후보마다 요청이 한 번 나간다. 상한이 없으면 메뉴가 큰 사이트에서 폭증한다."""

        links = tuple((f"공지 {i}", f"/n{i}.do") for i in range(30))
        html = listing_with_menu(*links)
        requested: list[str] = []

        def fetch(url: str) -> str | None:
            requested.append(url)
            return board_html()

        find_boards(html, BASE, CommonNoticeAdapter(), fetch, max_candidates=5)

        self.assertEqual(len(requested), 5)


if __name__ == "__main__":
    unittest.main()
