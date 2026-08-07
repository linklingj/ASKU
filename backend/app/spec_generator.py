"""수집 규격 자동 생성.

새 학교의 게시판 HTML 을 LLM 에 보여 주고 규격(`AdapterSpec`)을 받는다. 학교마다
파이썬 어댑터를 짜던 일을 대신하는 것이 목적이며, 학교 등록 시 한 번만 호출한다.
수집 자체에는 LLM 을 쓰지 않는다 — 통과한 규격으로 수십·수백 건을 처리한다.

**생성한 규격을 그대로 믿지 않는다.** 받은 규격으로 실제 페이지를 파싱해 보고
검증기(`app.validation`)를 통과한 것만 돌려준다. 실패하면 무엇이 왜 틀렸는지를
지적으로 붙여 다시 시킨다. 같은 프롬프트로 재시도하면 같은 실수를 반복한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from time import sleep
from typing import Callable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from pydantic import ValidationError

from app.adapter_spec import AdapterSpec
from app.crawler import SpecNoticeAdapter
from app.extractor import SpecContentParser
from app.html_digest import digest_html
from app.llm import SpecDrafter
from app.prompts import spec_draft_instruction
from app.spec_templates import templates_for
from app.validation import Finding, ValidationReport, validate_detail, validate_listing


LOGGER = logging.getLogger(__name__)

# 규격 생성 시도 횟수. 지적을 붙여 다시 시켜도 안 되면 사람이 봐야 한다.
DEFAULT_MAX_ATTEMPTS = 3
# 제공자 오류(429·503) 뒤 첫 대기 시간. 재시도마다 두 배로 늘린다. 무료 티어에서는
# 분당 한도와 일시적 과부하가 흔해, 곧바로 다시 부르면 같은 오류를 받는다.
DEFAULT_BACKOFF_SECONDS = 20.0
# 제공자 오류로 인한 재시도 횟수. 규격 시도 횟수와 따로 센다. 503 은 규격 품질과
# 무관한 서버 사정이라, 이것 때문에 생성을 포기하면 멀쩡한 학교를 놓친다.
DEFAULT_PROVIDER_RETRIES = 4

# 규격 탓이 아닐 수 있는 판정. 본문이 이미지 한 장뿐인 공지가 실제로 있어(아주대
# '학위수여일 운영 안내'), 표본에 섞이면 어떤 선택자로도 통과할 수 없다. 표본
# 전체가 이러면 규격 문제지만, 일부면 그 공지의 사정이다.
SAMPLE_LEVEL_CODES = frozenset({"EMPTY_CONTENT", "TITLE_MISMATCH", "NEIGHBOUR_LEAK", "BODY_SELECTOR_NOT_FOUND"})

# 이것이 걸리면 규격을 쓸 수 없다. 목록을 못 읽거나 제목이 없으면 수집·검색이
# 성립하지 않는다.
BLOCKING_CODES = frozenset({
    "NO_LISTING_ROWS",
    "MISSING_TITLES",
    "LISTING_FETCH_FAILED",
    "DETAIL_NOT_IN_LISTING",
    "NO_BODY_SELECTOR",
})

# 나머지(날짜 미달·페이지네이션 이상 등)는 아쉽지만 쓸 수 있다. 날짜를 아예 주지
# 않는 게시판이 실제로 있고, 페이지네이션이 안 되면 1페이지만 수집될 뿐이다.
# 이것 때문에 학교를 통째로 버리면 손해가 더 크므로 경고로만 남긴다.


@dataclass
class SpecDraftResult:
    """규격 생성 결과. `spec` 이 None 이면 사람 검토가 필요하다."""

    spec: AdapterSpec | None = None
    attempts: int = 0
    reports: list[ValidationReport] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # 어디서 온 규격인지. 템플릿으로 끝나면 LLM 을 부르지 않았다는 뜻이다.
    origin: str = "generated"

    @property
    def accepted(self) -> bool:
        return self.spec is not None

    def summary(self) -> str:
        """통과해도 남은 지적을 함께 보여 준다. 표본 일부의 문제는 규격을 막지
        않지만, 사람이 알고는 있어야 한다."""

        state = "통과" if self.accepted else "실패"
        if not self.findings:
            return f"{state} (시도 {self.attempts}회)"
        label = "경고" if self.accepted else "사유"
        return f"{state} (시도 {self.attempts}회) — {label}: " + "; ".join(str(f) for f in self.findings[:3])


@dataclass(frozen=True)
class PageSample:
    """규격 생성에 보여 줄 페이지 한 장."""

    url: str
    html: str


def generate_spec(
    drafter: SpecDrafter,
    host: str,
    listing: PageSample,
    details: list[PageSample],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    provider_retries: int = DEFAULT_PROVIDER_RETRIES,
    sleeper: Callable[[float], None] = sleep,
) -> SpecDraftResult:
    """규격을 만들고 검증한다. 통과한 규격만 `spec` 에 담아 돌려준다.

    제공자 오류(429·503)는 규격의 문제가 아니므로 지적으로 돌려주지 않고, 잠시
    쉬었다가 같은 프롬프트로 다시 부른다. 무료 티어에서는 이런 오류가 흔해 그대로
    두면 생성이 통째로 실패한다.
    """

    result = SpecDraftResult()

    matched = match_template(host, listing, details)
    if matched is not None:
        name, spec, report = matched
        result.spec, result.origin = spec, f"template:{name}"
        result.reports.append(report)
        result.findings = list(report.findings)  # 통과해도 남은 지적은 경고로 알린다
        LOGGER.info("템플릿 적용(host=%s, %s): %s", host, name, report.summary())
        return result

    listing_digest = digest_html(listing.html)
    detail_digests = [(sample.url, digest_html(sample.html)) for sample in details]
    feedback: list[str] = []

    for attempt in range(1, max_attempts + 1):
        result.attempts = attempt
        prompt = spec_draft_instruction(
            listing.url,
            listing_digest,
            detail_digests,
            schema=json.dumps(AdapterSpec.model_json_schema(), ensure_ascii=False),
            feedback=feedback,
        )
        raw = _call_with_backoff(
            drafter, prompt, host, result,
            retries=provider_retries, backoff_seconds=backoff_seconds, sleeper=sleeper,
        )
        if raw is None:  # 서버 사정으로 끝내 응답을 못 받았다
            return result

        try:
            spec = _parse_spec(raw, host)
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            feedback = [f"응답이 규격 형식에 맞지 않았다: {error}"]
            result.findings = [Finding("SPEC_INVALID", str(error))]
            LOGGER.warning("규격 형식 오류(host=%s, 시도 %d): %s", host, attempt, error)
            continue

        report = verify_spec(spec, listing, details)
        result.reports.append(report)
        if _acceptable(report, len(details)):
            result.spec = spec
            result.findings = list(report.findings)  # 통과해도 남은 지적은 경고로 알린다
            return result

        result.findings = list(report.findings)
        feedback = [str(finding) for finding in report.findings]
        LOGGER.warning("규격 검증 실패(host=%s, 시도 %d): %s", host, attempt, report.summary())
        for finding in report.findings:
            LOGGER.warning("    - %s", finding)
    return result


def collect_samples(crawler, request, base_url: str, count: int = 2) -> tuple[PageSample, list[PageSample]] | None:
    """규격을 판정할 표본(목록 1장 + 상세 몇 건)을 받는다.

    상세 링크는 기존 어댑터로 찾되, 못 찾으면 **목록 HTML 안의 링크 패턴**으로
    추정한다. 기존 어댑터가 읽지 못하는 학교가 바로 규격이 필요한 학교라,
    어댑터에만 기대면 정작 필요한 곳에서 표본을 못 모은다.

    상세를 한 장도 못 얻으면 목록만 돌려준다. 목록만으로도 규격 초안은 만들 수
    있고, 상세 선택자는 그 규격으로 링크를 찾아 확인하면 된다.
    """

    from app.crawler import CommonNoticeAdapter, CrawlRun, adapter_for

    run = CrawlRun()
    if not crawler.robots_allowed(base_url):
        LOGGER.warning("robots.txt 가 수집을 허용하지 않습니다: %s", base_url)
        return None
    listing_html = crawler._fetch(request, base_url, run)  # noqa: SLF001
    if listing_html is None:
        return None

    items = list(adapter_for(base_url).parse_listing(listing_html, base_url))
    if not items:
        items = list(CommonNoticeAdapter().parse_listing(listing_html, base_url))
    if not items:
        items = _guess_detail_links(listing_html, base_url)

    details: list[PageSample] = []
    for item in items:
        if len(details) >= count:
            break
        if not crawler.robots_allowed(item.url):
            continue
        html = crawler._fetch(request, item.url, run, referer=base_url)  # noqa: SLF001
        if html is not None:
            details.append(PageSample(item.url, html))
    return PageSample(base_url, listing_html), details


def _guess_detail_links(listing_html: str, base_url: str, limit: int = 3) -> list:
    """목록 HTML 의 링크 패턴으로 상세 링크를 추정한다.

    상세 공지 링크는 **같은 모양이 여러 개 반복된다**. 숫자만 다른 URL 을 묶어
    가장 큰 무리를 고르면, 선택자를 몰라도 상세 페이지를 찾을 수 있다. 메뉴·배너는
    같은 패턴이 여러 개 나오지 않는다.
    """

    from app.crawler import ListingItem

    soup = BeautifulSoup(listing_html, "html.parser")
    host = urlsplit(base_url).hostname
    groups: dict[str, list[tuple[str, str]]] = {}
    for link in soup.select("a[href]"):
        href = str(link["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        absolute = urljoin(base_url, href)
        if urlsplit(absolute).hostname != host:
            continue
        text = " ".join(link.get_text(" ", strip=True).split())
        if len(text) < 6:  # 메뉴·아이콘 링크를 걸러낸다
            continue
        groups.setdefault(re.sub(r"\d+", "#", absolute), []).append((absolute, text))

    if not groups:
        return []
    best = max(groups.values(), key=len)
    if len(best) < 3:  # 반복이 없으면 목록이 아니다
        return []
    return [ListingItem(url=url, title_hint=text) for url, text in best[:limit]]


def discover_boards(spec: AdapterSpec, listing: PageSample, crawler, request) -> AdapterSpec:
    """규격에 게시판 목록이 비어 있으면 찾아서 채운다.

    공지가 `일반공지 / 장학 / 채용` 으로 나뉜 학교가 흔한데, 규격만 확보하고 끝내면
    등록한 URL 하나만 돌아 나머지를 통째로 놓친다. 템플릿으로 규격을 얻은 학교는
    LLM 을 부르지 않으므로, 이 단계가 없으면 탭을 찾을 기회 자체가 없다.

    LLM 을 쓰지 않는다. 후보를 실제로 받아 보고 목록이 읽히는지로 판정한다.
    """

    from app.board_discovery import find_boards
    from app.crawler import CrawlRun, adapter_for

    if spec.boards:
        return spec  # 이미 있으면 그대로 둔다(사람이 적었거나 LLM 이 채웠다)

    run = CrawlRun()

    def fetch(url: str) -> str | None:
        if not crawler.robots_allowed(url):
            return None
        return crawler._fetch(request, url, run, referer=listing.url)  # noqa: SLF001

    boards = find_boards(listing.html, listing.url, adapter_for(listing.url, spec), fetch)
    if len(boards) <= 1:
        return spec
    LOGGER.info("게시판 %d개 발견: %s", len(boards), ", ".join(b.label or "?" for b in boards))
    from app.adapter_spec import BoardSpec

    return spec.model_copy(update={"boards": [BoardSpec(url=b.url, label=b.label) for b in boards]})


def match_template(
    host: str, listing: PageSample, details: list[PageSample]
) -> tuple[str, AdapterSpec, ValidationReport] | None:
    """알려진 게시판 제품의 규격을 대보고, 통과하는 첫 번째를 돌려준다.

    국내 대학은 같은 게시판 제품을 널리 쓴다. 이미 아는 규격으로 되는 학교에
    LLM 을 부르면 토큰과 시간을 낭비하고, 제공자 과부하에도 걸린다.

    채택 기준은 자동 생성 규격과 같다. 기준이 다르면 어느 쪽이 나은지 비교할 수
    없고, 템플릿만 느슨하게 통과하는 일이 생긴다.
    """

    for name, spec in templates_for(host):
        report = verify_spec(spec, listing, details)
        if _acceptable(report, len(details)):
            return name, spec, report
        LOGGER.debug("템플릿 불일치(host=%s, %s): %s", host, name, report.summary())
    return None


def _acceptable(report: ValidationReport, sample_count: int) -> bool:
    """규격을 받아들일지 판정한다. 지적의 무게가 서로 다르다.

    - 차단: 목록을 못 읽거나 제목이 없다. 수집·검색이 성립하지 않는다.
    - 표본: 본문 없음·제목 불일치 등. 그 공지의 사정일 수 있어 절반까지 봐준다.
    - 경고: 날짜 미달·페이지네이션 이상. 아쉽지만 쓸 수 있다.

    전부 똑같이 막으면 날짜를 안 주는 게시판 하나 때문에 학교를 통째로 버린다.
    """

    if any(finding.code in BLOCKING_CODES for finding in report.findings):
        return False
    sample_findings = [f for f in report.findings if f.code in SAMPLE_LEVEL_CODES]
    if not sample_findings:
        return True
    return bool(sample_count) and len(sample_findings) <= sample_count // 2


def _call_with_backoff(
    drafter: SpecDrafter,
    prompt: str,
    host: str,
    result: SpecDraftResult,
    *,
    retries: int,
    backoff_seconds: float,
    sleeper: Callable[[float], None],
) -> str | None:
    """제공자를 부르고, 서버 오류면 간격을 늘려 가며 다시 부른다.

    429·503 은 규격 품질과 무관하다. 이를 규격 시도 횟수에서 차감하면 서버가
    바쁜 동안 멀쩡한 학교를 포기하게 된다.
    """

    wait = backoff_seconds
    for attempt in range(retries + 1):
        try:
            return drafter.draft_spec(prompt)
        except Exception as error:
            detail = str(error)[:200]
            result.findings = [Finding("PROVIDER_ERROR", detail)]
            LOGGER.warning("규격 생성 호출 실패(host=%s, %d/%d): %s", host, attempt + 1, retries + 1, detail)
            if attempt == retries:
                return None
            sleeper(wait)
            wait *= 2
    return None


def verify_spec(spec: AdapterSpec, listing: PageSample, details: list[PageSample]) -> ValidationReport:
    """규격으로 실제 페이지를 파싱해 채점한다.

    사람이 쓴 규격과 자동 생성한 규격에 같은 기준을 적용한다. 여기서 쓰는 판정은
    크롤 파이프라인이 매 실행마다 쓰는 것과 같다.
    """

    report = ValidationReport(target=spec.host)
    adapter = SpecNoticeAdapter(spec)
    validate_listing(adapter, listing.html, listing.url, report=report)
    if not report.passed:
        return report

    items = {item.url: item for item in adapter.parse_listing(listing.html, listing.url)}
    titles = [item.title_hint or "" for item in items.values()]
    parser = SpecContentParser(spec.detail) if spec.detail.body else None

    for sample in details:
        item = items.get(sample.url)
        if item is None:
            # 목록에서 나오지 않은 상세는 채점 대상이 아니다. 표본을 뽑을 때 쓴
            # 링크와 규격이 찾아낸 링크가 다르다는 뜻이라 그 자체가 지적거리다.
            report.add("DETAIL_NOT_IN_LISTING", f"규격의 목록 선택자가 이 상세 링크를 찾지 못했다: {sample.url}")
            continue
        if parser is None:
            report.add("NO_BODY_SELECTOR", "본문 선택자가 비어 있다")
            continue
        try:
            document = parser.parse(sample.html)
        except ValueError:
            report.add("BODY_SELECTOR_NOT_FOUND", f"본문 선택자가 상세 페이지에 없다: {sample.url}")
            continue
        others = [title for title in titles if title and title != item.title_hint]
        validate_detail(document, item, other_titles=others, report=report)
    return report


def _parse_spec(raw: str, host: str) -> AdapterSpec:
    """LLM 응답을 규격으로 읽는다. 호스트는 호출자 값으로 고정한다."""

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("규격은 JSON 객체여야 한다")
    payload["host"] = host
    payload["source"] = "generated"
    return AdapterSpec.model_validate(payload)
