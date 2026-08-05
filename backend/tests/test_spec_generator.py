"""규격 자동 생성 테스트.

LLM 응답은 가짜로 주입한다. 여기서 확인할 것은 모델의 품질이 아니라 **잘못된
규격을 걸러내고 지적을 붙여 다시 시키는가** 이다.
"""

import json
import unittest

from app.adapter_spec import AdapterSpec, DetailSpec, ListingSpec
from app.llm import SpecDrafter
from app.spec_generator import PageSample, collect_samples, generate_spec, verify_spec


LISTING_URL = "https://new.ac.kr/notice.do"
DETAIL_URL = "https://new.ac.kr/view.do?id=1"

LISTING_HTML = """
<table><tbody>
  <tr><td class='subject'><a href='/view.do?id=1'>장학금 신청 안내</a></td>
      <td class='writer'>학생지원과</td><td class='date'>2026-08-04</td></tr>
  <tr><td class='subject'><a href='/view.do?id=2'>등록금 납부 안내</a></td>
      <td class='writer'>재무과</td><td class='date'>2026-08-01</td></tr>
</tbody></table>
<a class='next' href='/notice.do?page=2'>다음</a>
"""

DETAIL_HTML = """
<main>
  <h3 class='view-title'>장학금 신청 안내</h3>
  <div class='view-content'><p>신청 기간은 8월 10일까지이며 제출 서류는 아래와 같습니다.
    성적증명서와 가족관계증명서를 학생지원과로 제출하시기 바랍니다.</p></div>
  <ul class='next-list'><li><a href='/view.do?id=2'>등록금 납부 안내</a></li></ul>
</main>
"""


def good_spec() -> dict:
    return {
        "host": "new.ac.kr",
        "listing": {
            "row": "table tbody tr",
            "detail_link": "td.subject a[href]",
            "title": "td.subject a",
            "author": "td.writer",
            "date": "td.date",
            "pagination": {"type": "link", "selector": "a.next[href]"},
        },
        "detail": {"body": ["div.view-content"], "title": ["h3.view-title"]},
    }


class FakeDrafter(SpecDrafter):
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def draft_spec(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


def samples() -> tuple[PageSample, list[PageSample]]:
    return PageSample(LISTING_URL, LISTING_HTML), [PageSample(DETAIL_URL, DETAIL_HTML)]


class GenerateSpecTests(unittest.TestCase):
    def test_valid_spec_is_accepted_on_first_attempt(self) -> None:
        listing, details = samples()
        drafter = FakeDrafter([json.dumps(good_spec())])

        result = generate_spec(drafter, "new.ac.kr", listing, details)

        self.assertTrue(result.accepted, result.summary())
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.spec.source, "generated")

    def test_host_comes_from_caller_not_from_the_model(self) -> None:
        """모델이 호스트를 잘못 적어도 등록 대상이 바뀌면 안 된다."""

        payload = good_spec() | {"host": "wrong.ac.kr"}
        listing, details = samples()

        result = generate_spec(FakeDrafter([json.dumps(payload)]), "new.ac.kr", listing, details)

        self.assertEqual(result.spec.host, "new.ac.kr")

    def test_bad_body_selector_is_rejected_and_retried_with_feedback(self) -> None:
        """본문 선택자를 상위 요소로 잡으면 다음 글 제목이 본문에 섞인다."""

        leaky = good_spec()
        leaky["detail"]["body"] = ["main"]
        listing, details = samples()
        drafter = FakeDrafter([json.dumps(leaky), json.dumps(good_spec())])

        result = generate_spec(drafter, "new.ac.kr", listing, details)

        self.assertTrue(result.accepted, result.summary())
        self.assertEqual(result.attempts, 2)
        # 두 번째 프롬프트에는 첫 시도의 지적이 들어가야 한다
        self.assertIn("직전 시도의 문제", drafter.prompts[1])
        self.assertIn("NEIGHBOUR_LEAK", drafter.prompts[1])

    def test_row_selector_matching_nothing_is_rejected(self) -> None:
        broken = good_spec()
        broken["listing"]["row"] = "ul.menu li"
        listing, details = samples()
        drafter = FakeDrafter([json.dumps(broken)] * 3)

        result = generate_spec(drafter, "new.ac.kr", listing, details)

        self.assertFalse(result.accepted)
        self.assertEqual(result.attempts, 3)
        self.assertIn("NO_LISTING_ROWS", [f.code for f in result.findings])

    def test_malformed_json_is_reported_and_retried(self) -> None:
        listing, details = samples()
        drafter = FakeDrafter(["이건 JSON 이 아니다", json.dumps(good_spec())])

        result = generate_spec(drafter, "new.ac.kr", listing, details)

        self.assertTrue(result.accepted, result.summary())
        self.assertEqual(result.attempts, 2)
        self.assertIn("규격 형식에 맞지 않", drafter.prompts[1])

    def test_gives_up_after_max_attempts(self) -> None:
        listing, details = samples()
        drafter = FakeDrafter(["{}"] * 5)

        result = generate_spec(drafter, "new.ac.kr", listing, details, max_attempts=2)

        self.assertFalse(result.accepted)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(drafter.prompts), 2)

    def test_provider_error_is_retried_without_blaming_the_spec(self) -> None:
        """429·503 은 규격의 문제가 아니다. 지적으로 돌려주면 엉뚱한 곳을 고치게 된다."""

        class FlakyDrafter(FakeDrafter):
            def draft_spec(self, prompt: str) -> str:
                self.prompts.append(prompt)
                value = self.responses.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

        drafter = FlakyDrafter([RuntimeError("503 UNAVAILABLE"), json.dumps(good_spec())])
        listing, details = samples()
        naps: list[float] = []

        result = generate_spec(drafter, "new.ac.kr", listing, details, sleeper=naps.append)

        self.assertTrue(result.accepted, result.summary())
        self.assertEqual(result.attempts, 1, "서버 사정은 규격 시도 횟수를 깎지 않는다")
        self.assertEqual(len(naps), 1, "제공자 오류 뒤에는 잠시 쉬어야 한다")
        self.assertNotIn("직전 시도의 문제", drafter.prompts[1])

    def test_provider_error_on_every_attempt_is_reported(self) -> None:
        class AlwaysFailing(FakeDrafter):
            def draft_spec(self, prompt: str) -> str:
                self.prompts.append(prompt)
                raise RuntimeError("429 RESOURCE_EXHAUSTED")

        drafter = AlwaysFailing([])
        result = generate_spec(
            drafter, "new.ac.kr", *samples(), provider_retries=2, sleeper=lambda _seconds: None
        )

        self.assertFalse(result.accepted)
        self.assertEqual([f.code for f in result.findings], ["PROVIDER_ERROR"])
        self.assertEqual(len(drafter.prompts), 3, "재시도 뒤 포기한다")

    def test_backoff_grows_between_provider_retries(self) -> None:
        """같은 간격으로 다시 부르면 과부하가 풀리기 전에 횟수를 다 쓴다."""

        class AlwaysFailing(FakeDrafter):
            def draft_spec(self, prompt: str) -> str:
                self.prompts.append(prompt)
                raise RuntimeError("503 UNAVAILABLE")

        naps: list[float] = []

        generate_spec(
            AlwaysFailing([]), "new.ac.kr", *samples(),
            provider_retries=3, backoff_seconds=10, sleeper=naps.append,
        )

        self.assertEqual(naps, [10, 20, 40])

    def test_prompt_carries_digested_html_not_the_original(self) -> None:
        """원본을 그대로 넣으면 학교 하나 등록에 수십만 토큰이 든다."""

        bulky = LISTING_HTML + "<script>" + "x" * 50_000 + "</script>"
        drafter = FakeDrafter([json.dumps(good_spec())])

        generate_spec(drafter, "new.ac.kr", PageSample(LISTING_URL, bulky), [PageSample(DETAIL_URL, DETAIL_HTML)])

        self.assertNotIn("x" * 1_000, drafter.prompts[0])


class VerifySpecTests(unittest.TestCase):
    def test_detail_missing_from_listing_is_reported(self) -> None:
        """표본으로 받은 상세를 규격이 목록에서 찾지 못하면 선택자가 어긋난 것이다."""

        spec = AdapterSpec.model_validate(good_spec())
        listing, _ = samples()
        stranger = [PageSample("https://new.ac.kr/view.do?id=99", DETAIL_HTML)]

        report = verify_spec(spec, listing, stranger)

        self.assertIn("DETAIL_NOT_IN_LISTING", [f.code for f in report.findings])

    def test_empty_body_selector_is_reported(self) -> None:
        spec = AdapterSpec(
            host="new.ac.kr",
            listing=ListingSpec(
                row="table tbody tr",
                detail_link="td.subject a[href]",
                title="td.subject a",
                date="td.date",
            ),
            detail=DetailSpec(),
        )
        listing, details = samples()

        report = verify_spec(spec, listing, details)

        self.assertIn("NO_BODY_SELECTOR", [f.code for f in report.findings])


if __name__ == "__main__":
    unittest.main()


class SampleLevelFindingTests(unittest.TestCase):
    """표본 일부의 문제와 규격의 문제를 구분한다."""

    def two_details(self) -> tuple[PageSample, list[PageSample]]:
        empty = "<main><h3 class='view-title'>등록금 납부 안내</h3><div class='view-content'><img src='/a.png'></div></main>"
        return PageSample(LISTING_URL, LISTING_HTML), [
            PageSample(DETAIL_URL, DETAIL_HTML),
            PageSample("https://new.ac.kr/view.do?id=2", empty),
        ]

    def test_one_empty_notice_does_not_block_the_spec(self) -> None:
        """본문이 이미지뿐인 공지가 실제로 있다. 어떤 선택자로도 통과할 수 없다."""

        listing, details = self.two_details()
        drafter = FakeDrafter([json.dumps(good_spec())])

        result = generate_spec(drafter, "new.ac.kr", listing, details)

        self.assertTrue(result.accepted, result.summary())
        self.assertEqual(result.attempts, 1)
        # 통과시키되 남은 문제는 알린다
        self.assertIn("EMPTY_CONTENT", [f.code for f in result.findings])
        self.assertIn("경고", result.summary())

    def test_all_samples_failing_is_treated_as_a_spec_problem(self) -> None:
        empty = "<main><div class='view-content'></div></main>"
        listing = PageSample(LISTING_URL, LISTING_HTML)
        details = [PageSample(DETAIL_URL, empty), PageSample("https://new.ac.kr/view.do?id=2", empty)]
        drafter = FakeDrafter([json.dumps(good_spec())] * 3)

        result = generate_spec(drafter, "new.ac.kr", listing, details)

        self.assertFalse(result.accepted)

    def test_unreadable_listing_is_never_tolerated(self) -> None:
        """목록을 한 줄도 못 읽으면 표본 비율과 무관하게 규격 문제다."""

        broken = good_spec()
        broken["listing"]["row"] = "ul.menu li"
        listing, details = self.two_details()
        drafter = FakeDrafter([json.dumps(broken)] * 3)

        result = generate_spec(drafter, "new.ac.kr", listing, details)

        self.assertFalse(result.accepted)
        self.assertIn("NO_LISTING_ROWS", [f.code for f in result.findings])

    def test_missing_titles_block_the_spec(self) -> None:
        """제목이 없으면 검색이 성립하지 않는다.

        제목 선택자가 빗나가도 링크 텍스트로 폴백하므로, 링크 자체가 비어 있어야
        비로소 제목이 없는 상태가 된다(이미지 링크 게시판 등).
        """

        blank = """
        <table><tbody>
          <tr><td class='subject'><a href='/view.do?id=1'><img src='/t.png'></a></td></tr>
          <tr><td class='subject'><a href='/view.do?id=2'><img src='/t.png'></a></td></tr>
        </tbody></table>
        """
        _, details = self.two_details()
        drafter = FakeDrafter([json.dumps(good_spec())] * 3)

        result = generate_spec(drafter, "new.ac.kr", PageSample(LISTING_URL, blank), details)

        self.assertFalse(result.accepted)
        self.assertIn("MISSING_TITLES", [f.code for f in result.findings])

    def test_missing_dates_are_a_warning_not_a_blocker(self) -> None:
        """날짜를 아예 주지 않는 게시판이 있다. 이것으로 학교를 버리면 손해가 크다."""

        no_date = good_spec()
        no_date["listing"]["date"] = "td.nope"
        listing, details = self.two_details()
        drafter = FakeDrafter([json.dumps(no_date)])

        result = generate_spec(drafter, "new.ac.kr", listing, details)

        self.assertTrue(result.accepted, result.summary())
        self.assertIn("MISSING_DATES", [f.code for f in result.findings])
        self.assertIn("경고", result.summary())


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
<div class='b-paging'><ul><li class='next'><a href='?article.offset=10'>다음</a></li></ul></div>
"""

K2WEB_DETAIL = """
<main><span class='b-title'>장학금 신청 안내</span>
  <div class='b-content-box'><div class='fr-view'>
    <p>신청 기간은 8월 10일까지이며 제출 서류는 아래와 같습니다.
       성적증명서와 가족관계증명서를 학생지원과로 제출하시기 바랍니다.</p>
  </div></div>
</main>
"""


class TemplateFirstTests(unittest.TestCase):
    """알려진 게시판 제품은 LLM 없이 규격을 얻는다."""

    def k2web_samples(self):
        return (
            PageSample("https://k2.ac.kr/notice.do", K2WEB_LISTING),
            [PageSample("https://k2.ac.kr/notice.do?mode=view&articleNo=1", K2WEB_DETAIL)],
        )

    def test_matching_template_skips_the_model(self) -> None:
        drafter = FakeDrafter([])  # 부르면 IndexError 가 난다

        result = generate_spec(drafter, "k2.ac.kr", *self.k2web_samples())

        self.assertTrue(result.accepted, result.summary())
        self.assertEqual(len(drafter.prompts), 0, "템플릿이 맞으면 모델을 부르지 않는다")
        self.assertEqual(result.origin, "template:k2web")
        self.assertEqual(result.spec.host, "k2.ac.kr")

    def test_unknown_markup_falls_through_to_the_model(self) -> None:
        drafter = FakeDrafter([json.dumps(good_spec())])

        result = generate_spec(drafter, "new.ac.kr", *samples())

        self.assertTrue(result.accepted, result.summary())
        self.assertEqual(result.origin, "generated")
        self.assertEqual(len(drafter.prompts), 1)

    def test_template_is_judged_by_the_same_validator(self) -> None:
        """템플릿이라고 느슨하게 통과시키면 어느 쪽이 나은지 비교할 수 없다."""

        listing, details = self.k2web_samples()
        broken = [PageSample(details[0].url, "<main><div class='b-content-box'></div></main>")]
        drafter = FakeDrafter([json.dumps(good_spec())] * 3)

        result = generate_spec(drafter, "k2.ac.kr", listing, broken)

        self.assertNotEqual(result.origin, "template:k2web")
        self.assertGreater(len(drafter.prompts), 0, "템플릿이 안 맞으면 모델로 넘어간다")


class SampleCollectionTests(unittest.TestCase):
    """표본 수집이 기존 어댑터에만 기대면, 정작 규격이 필요한 학교에서 막힌다."""

    def crawler(self, pages: dict[str, str]):
        from unittest.mock import MagicMock

        crawler = MagicMock()
        crawler.robots_allowed.return_value = True
        crawler._fetch.side_effect = lambda _req, url, _run, **_kw: pages.get(url)
        return crawler

    def request(self):
        from unittest.mock import MagicMock

        return MagicMock()

    def test_detail_links_are_guessed_when_adapters_fail(self) -> None:
        """공용 파서가 못 읽는 목록에서도 링크 패턴으로 상세를 찾는다."""

        rows = "".join(f"<div class='row'><a href='/read.do?no={i}'>공지 제목 {i}번</a></div>" for i in range(5))
        pages = {"https://x.ac.kr/list.do": f"<body><nav><a href='/menu'>메뉴</a></nav>{rows}</body>"}
        pages.update({f"https://x.ac.kr/read.do?no={i}": f"<main>본문 {i}</main>" for i in range(5)})

        result = collect_samples(self.crawler(pages), self.request(), "https://x.ac.kr/list.do", 2)

        self.assertIsNotNone(result)
        _listing, details = result
        self.assertEqual(len(details), 2)
        self.assertTrue(all("read.do" in sample.url for sample in details))

    def test_menu_links_are_not_mistaken_for_notices(self) -> None:
        """같은 패턴이 몇 개 없으면 목록이 아니다."""

        html = "<body><a href='/about'>학교 소개 페이지</a><a href='/map'>오시는 길 안내</a></body>"

        result = collect_samples(self.crawler({"https://x.ac.kr/list.do": html}), self.request(), "https://x.ac.kr/list.do", 2)

        self.assertIsNotNone(result)
        self.assertEqual(result[1], [])

    def test_listing_only_is_still_returned(self) -> None:
        """상세를 못 받아도 목록만으로 규격 초안은 만들 수 있다."""

        html = "<table><tbody><tr><td><a href='/v?id=1'>공지</a></td></tr></tbody></table>"

        result = collect_samples(self.crawler({"https://x.ac.kr/list.do": html}), self.request(), "https://x.ac.kr/list.do", 2)

        self.assertIsNotNone(result)
        listing, details = result
        self.assertEqual(listing.html, html)
        self.assertEqual(details, [])

    def test_external_links_are_ignored(self) -> None:
        """다른 도메인 링크를 상세로 잡으면 남의 사이트를 긁게 된다."""

        rows = "".join(f"<a href='https://other.com/p?id={i}'>바깥 링크 제목 {i}</a>" for i in range(5))

        result = collect_samples(self.crawler({"https://x.ac.kr/list.do": f"<body>{rows}</body>"}), self.request(), "https://x.ac.kr/list.do", 2)

        self.assertEqual(result[1], [])
