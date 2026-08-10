"""브라우저 렌더링 수집 경로 테스트.

여기서 확인할 것은 **언제 브라우저를 쓰는가** 다. 모든 학교에 브라우저를 띄우면
수집이 느려지고 메모리를 크게 쓴다. 반대로 필요한 학교에서 안 쓰면 목록이
0행으로 조용히 끝난다.
"""

import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from app.adapter_spec import AdapterSpec, DetailSpec, ListingSpec
from app.crawler import Crawler, CrawlRun, SpecNoticeAdapter
from app.schemas import CrawlRequest, CrawlScope


LISTING = "<div class='row'><a href='/notice/1'>공지 하나</a></div>"
DETAIL = "<main><p>공지 본문입니다. 신청 기간은 8월 12일까지입니다.</p></main>"


def spec(render: str) -> AdapterSpec:
    return AdapterSpec(
        host="dynamic.ac.kr",
        render=render,
        listing=ListingSpec(row="div.row", detail_link="a[href]"),
        detail=DetailSpec(body=["main"]),
    )


def crawler(renderer=None) -> Crawler:
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, text=DETAIL)
    return Crawler(
        hash_exists=lambda *_: False,
        session=session,
        sleeper=lambda _: None,
        robots_allowed=lambda _: True,
        renderer=renderer,
    )


def request() -> CrawlRequest:
    return CrawlRequest(
        crawl_id=uuid4(), school_id=1, base_url="https://dynamic.ac.kr/notice",
        mode="initial", scope=CrawlScope(allowed_hosts=["dynamic.ac.kr"]),
    )


class RenderModeTests(unittest.TestCase):
    def test_off_never_opens_a_browser(self) -> None:
        """기본값이다. 정적 학교에 브라우저를 띄우면 수집이 느려지기만 한다."""

        renderer = MagicMock()
        crawl = crawler(renderer)
        crawl.session.get.return_value = MagicMock(status_code=200, text=LISTING)

        crawl.crawl(request(), SpecNoticeAdapter(spec("off")))

        renderer.render.assert_not_called()

    def test_listing_renders_the_list_but_fetches_details_statically(self) -> None:
        """중앙대는 목록만 스크립트로 그려지고 상세는 정적이다.

        상세까지 그리면 공지 300건에 브라우저 호출이 300번 붙는다.
        """

        renderer = MagicMock()
        renderer.render.return_value = LISTING
        crawl = crawler(renderer)

        run = crawl.crawl(request(), SpecNoticeAdapter(spec("listing")))

        self.assertEqual([call.args[0] for call in renderer.render.call_args_list],
                         ["https://dynamic.ac.kr/notice"])
        self.assertEqual([call.args[0] for call in crawl.session.get.call_args_list],
                         ["https://dynamic.ac.kr/notice/1"])
        self.assertEqual(len(run.pages), 1)

    def test_always_renders_details_too(self) -> None:
        renderer = MagicMock()
        renderer.render.side_effect = [LISTING, DETAIL]
        crawl = crawler(renderer)

        crawl.crawl(request(), SpecNoticeAdapter(spec("always")))

        self.assertEqual(renderer.render.call_count, 2)
        crawl.session.get.assert_not_called()

    def test_missing_renderer_is_recorded_not_silently_empty(self) -> None:
        """브라우저가 없으면 목록 0행으로 끝난다. 그대로 두면 원인을 알 수 없다."""

        run = crawler(None).crawl(request(), SpecNoticeAdapter(spec("listing")))

        self.assertEqual([failure.error_code for failure in run.failures], ["RENDERER_UNAVAILABLE"])
        self.assertEqual(run.pages, [])

    def test_render_failure_is_recorded(self) -> None:
        renderer = MagicMock()
        renderer.render.return_value = None

        run = crawler(renderer).crawl(request(), SpecNoticeAdapter(spec("listing")))

        self.assertEqual([failure.error_code for failure in run.failures], ["RENDER_FAILED"])

    def test_rendered_fetch_still_waits_between_requests(self) -> None:
        """브라우저를 쓴다고 남의 서버를 더 빨리 두드려도 되는 것은 아니다."""

        waits: list[float] = []
        renderer = MagicMock()
        renderer.render.return_value = "<div></div>"
        crawl = Crawler(
            hash_exists=lambda *_: False, sleeper=waits.append,
            robots_allowed=lambda _: True, renderer=renderer,
        )

        crawl.crawl(request(), SpecNoticeAdapter(spec("listing")))

        self.assertEqual(waits, [crawl.settings.request_delay_seconds])

    def test_dedicated_adapters_have_no_render_mode(self) -> None:
        """전용 클래스는 `render_mode` 를 모른다. 없으면 정적으로 읽어야 한다."""

        from app.crawler import CommonNoticeAdapter

        self.assertFalse(hasattr(CommonNoticeAdapter(), "render_mode"))
        self.assertEqual(SpecNoticeAdapter(spec("listing")).render_mode, "listing")


if __name__ == "__main__":
    unittest.main()
