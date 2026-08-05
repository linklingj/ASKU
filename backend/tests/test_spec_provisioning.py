"""학교 등록 시 수집 규격을 확보하는 경로 테스트.

여기서 확인할 것은 **언제 LLM 을 부르는가** 다. 템플릿으로 되는 학교에 모델을
부르면 호출당 수만 토큰이 낭비되고, 검증 안 된 규격이 크롤에 쓰이면 남의 서버를
잘못 긁는다.
"""

import unittest
from unittest.mock import MagicMock, patch

from app.adapter_spec import AdapterSpec, ListingSpec
from app.api import _ensure_adapter_spec, _refresh_broken_spec
from app.validation import ValidationReport


K2WEB_LISTING = """
<table><tbody>
  <tr class='b-top-box'><td class='b-td-left'><div class='b-title-box'>
      <a href='?mode=view&articleNo=1'><span class='b-title'>장학금 신청 안내</span></a>
    </div><div class='b-m-con'><span class='b-writer'>학생지원과</span>
      <span class='b-date'>2026.08.04</span></div></td></tr>
  <tr><td class='b-td-left'><div class='b-title-box'>
      <a href='?mode=view&articleNo=2'><span class='b-title'>등록금 납부 안내</span></a>
    </div><div class='b-m-con'><span class='b-writer'>재무과</span>
      <span class='b-date'>2026.08.01</span></div></td></tr>
</tbody></table>
"""

K2WEB_DETAIL = """
<main><span class='b-title'>장학금 신청 안내</span>
  <div class='b-content-box'><div class='fr-view'>
    <p>신청 기간은 8월 10일까지이며 제출 서류는 성적증명서와 가족관계증명서입니다.
       기한 내에 학생지원과로 제출하시기 바라며 자세한 사항은 붙임을 참고하십시오.</p>
  </div></div></main>
"""

# 공용 어댑터로는 읽히되 알려진 템플릿과는 맞지 않는 마크업. 표본은 얻을 수 있어야
# "템플릿 불일치 뒤 어떻게 하는가" 를 확인할 수 있다.
UNKNOWN_LISTING = """
<table><tbody>
  <tr><td><a href='/read/1'>공지 하나</a></td><td>학사팀</td><td>2026-08-04</td></tr>
  <tr><td><a href='/read/2'>공지 둘</a></td><td>학사팀</td><td>2026-08-01</td></tr>
</tbody></table>
"""


def fake_crawler(pages: dict[str, str]) -> MagicMock:
    crawler = MagicMock()
    crawler.robots_allowed.return_value = True
    crawler._fetch.side_effect = lambda _req, url, _run, **_kw: pages.get(url)
    return crawler


class EnsureAdapterSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = MagicMock()
        self.storage.get_adapter_spec.return_value = None
        self.request = MagicMock()

    def test_dedicated_adapter_needs_no_spec(self) -> None:
        """검증된 전용 클래스가 있으면 표본조차 받지 않는다."""

        crawler = fake_crawler({})

        spec = _ensure_adapter_spec(self.storage, crawler, self.request, "https://www.sejong.ac.kr/kor/intro/notice1.do")

        self.assertIsNone(spec)
        crawler._fetch.assert_not_called()
        self.storage.upsert_adapter_spec.assert_not_called()

    def test_stored_spec_is_reused(self) -> None:
        """이미 등록된 규격이 있으면 다시 만들지 않는다."""

        self.storage.get_adapter_spec.return_value = {
            "host": "new.ac.kr",
            "listing": {"row": "table tbody tr", "detail_link": "a[href]"},
        }
        crawler = fake_crawler({})

        spec = _ensure_adapter_spec(self.storage, crawler, self.request, "https://new.ac.kr/notice.do")

        self.assertIsInstance(spec, AdapterSpec)
        crawler._fetch.assert_not_called()

    def test_known_board_product_is_matched_without_the_model(self) -> None:
        crawler = fake_crawler({
            "https://k2.ac.kr/notice.do": K2WEB_LISTING,
            "https://k2.ac.kr/notice.do?mode=view&articleNo=1": K2WEB_DETAIL,
            "https://k2.ac.kr/notice.do?mode=view&articleNo=2": K2WEB_DETAIL,
        })

        with patch("app.llm.GeminiProvider") as provider:
            spec = _ensure_adapter_spec(self.storage, crawler, self.request, "https://k2.ac.kr/notice.do")

        self.assertIsNotNone(spec)
        provider.assert_not_called()
        self.storage.upsert_adapter_spec.assert_called_once()

    def test_unknown_markup_does_not_call_the_model_by_default(self) -> None:
        """자동 생성은 기본으로 꺼 둔다. 검증 안 된 규격이 곧바로 크롤에 쓰이면 안 된다."""

        crawler = fake_crawler({"https://new.ac.kr/notice.do": UNKNOWN_LISTING, "https://new.ac.kr/read/1": "<main>본문</main>", "https://new.ac.kr/read/2": "<main>본문</main>"})

        with patch.dict("os.environ", {}, clear=False), patch("app.llm.GeminiProvider") as provider:
            spec = _ensure_adapter_spec(self.storage, crawler, self.request, "https://new.ac.kr/notice.do")

        self.assertIsNone(spec)
        provider.assert_not_called()

    def test_autogen_flag_enables_the_model(self) -> None:
        crawler = fake_crawler({"https://new.ac.kr/notice.do": UNKNOWN_LISTING, "https://new.ac.kr/read/1": "<main>본문</main>", "https://new.ac.kr/read/2": "<main>본문</main>"})
        generated = AdapterSpec(host="new.ac.kr", listing=ListingSpec(row="ul.posts li", detail_link="a[href]"))
        # MagicMock(spec=...) 은 목의 제약을 거는 예약 인자라 속성으로 따로 준다
        result = MagicMock()
        result.accepted, result.spec = True, generated
        result.summary.return_value = "통과"

        with (
            patch.dict("os.environ", {"SPEC_AUTOGEN": "1"}),
            patch("app.llm.GeminiProvider", return_value=MagicMock()),
            patch("app.spec_generator.generate_spec", return_value=result) as generate,
        ):
            spec = _ensure_adapter_spec(self.storage, crawler, self.request, "https://new.ac.kr/notice.do")

        self.assertIs(spec, generated)
        generate.assert_called_once()
        self.storage.upsert_adapter_spec.assert_called_once()

    def test_rejected_generation_is_not_saved(self) -> None:
        """검증을 통과하지 못한 규격은 저장하지 않는다."""

        crawler = fake_crawler({"https://new.ac.kr/notice.do": UNKNOWN_LISTING, "https://new.ac.kr/read/1": "<main>본문</main>", "https://new.ac.kr/read/2": "<main>본문</main>"})
        result = MagicMock()
        result.accepted, result.spec = False, None
        result.summary.return_value = "실패"

        with (
            patch.dict("os.environ", {"SPEC_AUTOGEN": "1"}),
            patch("app.llm.GeminiProvider", return_value=MagicMock()),
            patch("app.spec_generator.generate_spec", return_value=result),
        ):
            spec = _ensure_adapter_spec(self.storage, crawler, self.request, "https://new.ac.kr/notice.do")

        self.assertIsNone(spec)
        self.storage.upsert_adapter_spec.assert_not_called()

    def test_sample_failure_does_not_break_the_crawl(self) -> None:
        """표본을 못 받아도 크롤은 공용 파서로 계속된다."""

        crawler = fake_crawler({})  # 목록조차 못 받는 상황

        spec = _ensure_adapter_spec(self.storage, crawler, self.request, "https://new.ac.kr/notice.do")

        self.assertIsNone(spec)


if __name__ == "__main__":
    unittest.main()


class RefreshBrokenSpecTests(unittest.TestCase):
    """사이트 개편으로 규격이 깨졌을 때 다시 만드는 경로."""

    def setUp(self) -> None:
        self.storage = MagicMock()
        self.request = MagicMock()
        self.k2web_pages = {
            "https://k2.ac.kr/notice.do": K2WEB_LISTING,
            "https://k2.ac.kr/notice.do?mode=view&articleNo=1": K2WEB_DETAIL,
            "https://k2.ac.kr/notice.do?mode=view&articleNo=2": K2WEB_DETAIL,
        }

    def spec(self, source: str = "generated") -> AdapterSpec:
        return AdapterSpec(
            host="k2.ac.kr",
            listing=ListingSpec(row="table.old tbody tr", detail_link="a.old[href]"),
            source=source,
        )

    def report(self, *codes: str) -> ValidationReport:
        report = ValidationReport(target="기본")
        for code in codes:
            report.add(code, "테스트")
        return report

    def test_blocking_finding_triggers_regeneration(self) -> None:
        crawler = fake_crawler(self.k2web_pages)

        _refresh_broken_spec(
            self.storage, crawler, self.request, "https://k2.ac.kr/notice.do",
            self.spec(), [self.report("NO_LISTING_ROWS")],
        )

        self.storage.upsert_adapter_spec.assert_called_once()

    def test_warning_only_does_not_trigger_regeneration(self) -> None:
        """날짜 미달 같은 경고로 규격을 갈아엎으면 멀쩡한 규격을 잃는다."""

        crawler = fake_crawler(self.k2web_pages)

        _refresh_broken_spec(
            self.storage, crawler, self.request, "https://k2.ac.kr/notice.do",
            self.spec(), [self.report("MISSING_DATES")],
        )

        self.storage.upsert_adapter_spec.assert_not_called()
        crawler._fetch.assert_not_called()

    def test_human_written_spec_is_not_overwritten(self) -> None:
        """손으로 고친 결정을 자동 생성이 밀어내면 왜 바뀌었는지 알 수 없다."""

        crawler = fake_crawler(self.k2web_pages)

        _refresh_broken_spec(
            self.storage, crawler, self.request, "https://k2.ac.kr/notice.do",
            self.spec(source="human"), [self.report("NO_LISTING_ROWS")],
        )

        self.storage.upsert_adapter_spec.assert_not_called()

    def test_regeneration_prefers_templates_over_the_model(self) -> None:
        crawler = fake_crawler(self.k2web_pages)

        with patch("app.llm.GeminiProvider") as provider:
            _refresh_broken_spec(
                self.storage, crawler, self.request, "https://k2.ac.kr/notice.do",
                self.spec(), [self.report("NO_LISTING_ROWS")],
            )

        provider.assert_not_called()

    def test_no_spec_means_nothing_to_refresh(self) -> None:
        """전용 클래스를 쓰는 학교는 규격이 없다. 재생성 대상이 아니다."""

        crawler = fake_crawler(self.k2web_pages)

        _refresh_broken_spec(
            self.storage, crawler, self.request, "https://k2.ac.kr/notice.do",
            None, [self.report("NO_LISTING_ROWS")],
        )

        self.storage.upsert_adapter_spec.assert_not_called()
