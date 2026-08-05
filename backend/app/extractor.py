"""Crawler 원문을 Graph Builder 입력 청크로 바꾸는 정보 추출기.

HTML 정제·청킹·화이트리스트 검증은 이 모듈이 맡고, 의미 추출은 주입받은
``app.llm.Extractor`` 구현체에 맡긴다. DB 접근·엔티티 병합·임베딩은 하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from time import sleep
from typing import Callable, Protocol

from bs4 import BeautifulSoup
from pydantic import ValidationError

from app.llm import Extraction, Extractor as LLMExtractor
from app.prompts import whitelist_instruction
from app.schemas import CrawledPage, ExtractedChunk, ExtractedEntity, ExtractedRelation, ExtractionFailure


LOGGER = logging.getLogger(__name__)

# docs/01_SYSTEM/04_extractor.md §2의 단일 기준을 코드에서 강제한다.
ENTITY_TYPES = frozenset(
    {
        "대학·캠퍼스", "단과대학", "학과·전공", "부서·기관", "담당자", "외부기관",
        "공지", "장학금", "프로그램·사업", "채용·모집", "행사", "학사일정", "교과목·수업",
        "절차·신청방법", "규정·정책", "대상·자격", "연락처", "시설", "첨부·링크", "주제·카테고리",
    }
)
RELATION_TYPES = frozenset(
    {
        "소속", "담당", "주최", "게시", "안내", "포함", "분류", "관련", "대상", "연락처",
        "위치", "선행조건", "신청방법", "제공", "첨부",
    }
)

# 프롬프트 문안은 app/prompts.py 가 소유한다. 화이트리스트 타입 집합은 이 모듈이
# 소유·검증(§2)하므로, 문안 빌더에 넘겨 한 번만 조립한다.
_WHITELIST_INSTRUCTION = whitelist_instruction(ENTITY_TYPES, RELATION_TYPES)

DEFAULT_CHUNK_CHARS = 2_000  # 약 500 tokens의 의존성 없는 근사치
DEFAULT_OVERLAP_CHARS = 200  # 약 50 tokens의 의존성 없는 근사치


@dataclass(frozen=True)
class CleanedDocument:
    """HTML에서 얻은 제목·본문. 영속 DTO가 아닌 Extractor 내부 값이다."""

    title: str | None
    content: str
    used_body_fallback: bool = False


class ContentParser(Protocol):
    """학교별 본문 선택자 차이를 추가할 수 있는 확장 지점."""

    def parse(self, html: str) -> CleanedDocument: ...


class CommonContentParser:
    """서버 렌더링 공지의 제목·본문을 보수적으로 정제하는 기본 파서."""

    _BODY_SELECTORS = (
        "main", "article", "[role='main']", ".board-view", ".board-content",
        ".view-content", ".view-content-wrap", "#content", ".content",
    )
    _TITLE_SELECTORS = ("h1", ".board-view-title", ".view-title", ".title")
    _REMOVE_SELECTORS = "script, style, noscript, nav, header, footer, aside, form, iframe"

    def parse(self, html: str) -> CleanedDocument:
        soup = BeautifulSoup(html, "html.parser")
        for node in soup.select(self._REMOVE_SELECTORS):
            node.decompose()

        title = _first_text(soup, self._TITLE_SELECTORS)
        selected_body = _first_node(soup, self._BODY_SELECTORS)
        body = selected_body or soup.body or soup
        content = _body_text(body)
        return CleanedDocument(title=title, content=content, used_body_fallback=selected_body is None)


class SejongContentParser:
    """세종대 공지 상세 페이지의 실제 게시글 영역만 선택하는 파서.

    세종대 ``main`` 영역에는 대학 소개·챗봇·메뉴도 함께 들어간다. 공통 파서가
    그 전체를 본문으로 오인하지 않도록, 게시판의 ``.b-content-box``를 명시적으로
    선택한다. 해당 선택자가 바뀌면 ``DocumentExtractor``가 공통 파서로 폴백한다.
    """

    _TITLE_SELECTORS = (".b-title", *CommonContentParser._TITLE_SELECTORS)
    _CONTENT_SELECTORS = (".b-content-box .fr-view", ".b-content-box")

    def parse(self, html: str) -> CleanedDocument:
        soup = BeautifulSoup(html, "html.parser")
        for node in soup.select(CommonContentParser._REMOVE_SELECTORS):
            node.decompose()

        title = _first_text(soup, self._TITLE_SELECTORS)
        body = _first_node(soup, self._CONTENT_SELECTORS)
        if body is None:
            raise ValueError("sejong content selector not found")
        return CleanedDocument(title=title, content=_body_text(body))


def _first_node(soup: BeautifulSoup, selectors: tuple[str, ...]):
    for selector in selectors:
        node = soup.select_one(selector)
        if node is not None:
            return node
    return None


def _first_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    node = _first_node(soup, selectors)
    return _normalise_text(node.get_text(" ", strip=True)) if node is not None else None


def _normalise_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _body_text(body) -> str:
    """인라인 태그(span 등) 때문에 문장이 한 단어씩 끊어지지 않게 본문을 정제한다."""
    block_selectors = "p, li, tr, h1, h2, h3, h4, h5, h6, dt, dd"
    blocks = [_normalise_text(node.get_text(" ", strip=True)) for node in body.select(block_selectors)]
    blocks = [block for block in blocks if block and not _is_ui_block(block)]
    blocks = _deduplicate_adjacent_blocks(blocks)
    if blocks:
        return "\n".join(blocks)
    return _normalise_text(body.get_text(" ", strip=True))


def _is_ui_block(text: str) -> bool:
    return bool(re.match(r"^(?:이전글|다음글)(?:\s|이 없습니다|$)|^(?:수정|삭제|목록|공유|프린트|SNS 공유)$", text))


def _deduplicate_adjacent_blocks(blocks: list[str]) -> list[str]:
    """바로 연달아 반복되는 반응형 표·문단만 제거해 실제 반복 내용을 보존한다."""
    unique: list[str] = []
    previous_key: str | None = None
    for block in blocks:
        key = re.sub(r"\s+", " ", block).strip()
        if key != previous_key:
            unique.append(block)
        previous_key = key
    return unique


def chunk_document(content: str, chunk_chars: int, overlap_chars: int) -> list[tuple[str, int]]:
    """정제된 텍스트 하나는 유지하고, 긴 본문만 문단 경계 우선으로 분할한다.

    HTML 청킹(``DocumentExtractor.chunk``)과 첨부 문서 청킹(``app.attachment_ingest``)이
    공유하는 문단 분할 규칙. 줄바꿈을 문단 경계로 취급한다.
    """
    content = _normalise_text(content)
    if not content:
        return []
    if len(content) <= chunk_chars:
        return [(content, 0)]

    paragraphs = [part.strip() for part in re.split(r"\n+", content) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > chunk_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_paragraph(paragraph, chunk_chars, overlap_chars))
            continue
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= chunk_chars:
            current = candidate
            continue
        chunks.append(current)
        overlap = current[-overlap_chars:] if overlap_chars else ""
        current = f"{overlap}\n\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return [(item, index) for index, item in enumerate(chunks)]


def _split_long_paragraph(paragraph: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    step = chunk_chars - overlap_chars
    return [paragraph[start : start + chunk_chars] for start in range(0, len(paragraph), step)]


class DocumentExtractor:
    """`CrawledPage`를 정제·추출해 Graph Builder 계약으로 변환한다.

    ``llm_extractor``는 GeminiProvider 같은 ``app.llm.Extractor`` 구현체다. 호출자는
    테스트에서는 가짜 구현체를, 운영에서는 환경변수로 설정한 실제 구현체를 주입한다.
    """

    def __init__(
        self,
        llm_extractor: LLMExtractor,
        *,
        parsers: dict[int, ContentParser] | None = None,
        max_retries: int = 2,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries 는 0 이상이어야 한다")
        if chunk_chars <= 0 or not 0 <= overlap_chars < chunk_chars:
            raise ValueError("chunk_chars/overlap_chars 범위가 올바르지 않다")
        self.llm_extractor = llm_extractor
        self.parsers = parsers or {}
        self.max_retries = max_retries
        self.chunk_chars = chunk_chars
        self.overlap_chars = overlap_chars
        self.sleeper = sleeper
        self.common_parser = CommonContentParser()

    def chunk(self, document: str) -> list[tuple[str, int]]:
        """공지 하나는 유지하고, 긴 본문만 문단 경계 우선으로 분할한다."""
        # HTML 정제 후 각 블록은 줄바꿈으로 남는다. 줄 단위를 문단 경계로 취급한다.
        return chunk_document(document, self.chunk_chars, self.overlap_chars)

    def extract(self, chunk_text: str) -> Extraction:
        """LLM 추출과 화이트리스트 검증을 수행하는 공개 청크 단위 인터페이스."""
        extraction = self._call_llm(chunk_text)
        validated, _warnings = self._validate_extraction(extraction)
        return validated

    def process(self, page: CrawledPage) -> list[ExtractedChunk] | ExtractionFailure:
        """Crawler 출력 하나를 처리한다. 일부 청크 실패는 성공분을 partial로 유지한다."""
        if page.crawl_status not in {"new", "changed"}:
            return self._failure(page, "UNSUPPORTED_CRAWL_STATUS", retryable=False)

        parsed = self._parse_page(page)
        if parsed.used_body_fallback:
            LOGGER.warning("content selector fallback used for %s", page.source_url)
        if not parsed.content:
            return self._failure(page, "EMPTY_CONTENT", retryable=False)
        title = page.title_hint or parsed.title
        chunks = self.chunk(parsed.content)
        if not chunks:
            return self._failure(page, "EMPTY_CONTENT", retryable=False)

        results: list[ExtractedChunk] = []
        failures: list[str] = []
        retryable_failure = False
        for text, chunk_index in chunks:
            try:
                extraction = self._call_llm(self._llm_input(page, title, text))
                validated, warnings = self._validate_extraction(extraction)
                validated = self._ensure_crawler_metadata(validated, page, title)
                results.append(
                    ExtractedChunk(
                        school_id=page.school_id,
                        source_url=page.source_url,
                        title=title,
                        content=text,
                        chunk_index=chunk_index,
                        content_hash=page.content_hash,
                        crawled_at=page.fetched_at,
                        entities=validated.entities,
                        relations=validated.relations,
                        extraction_status="partial" if warnings else "complete",
                    )
                )
                if warnings:
                    LOGGER.warning("extractor validation warning: %s", "; ".join(warnings))
            except _LLMCallError as error:
                detail = f" ({error.detail})" if error.detail else ""
                failures.append(f"chunk {chunk_index}: {error.code}{detail}")
                retryable_failure = retryable_failure or error.retryable
                LOGGER.warning("extractor LLM failure for %s chunk %s: %s%s", page.source_url, chunk_index, error.code, detail)

        if not results:
            return self._failure(
                page,
                "LLM_EXTRACTION_FAILED",
                retryable=retryable_failure,
                warnings=failures,
            )
        if failures:
            results = [chunk.model_copy(update={"extraction_status": "partial"}) for chunk in results]
        return results

    def _parse_page(self, page: CrawledPage) -> CleanedDocument:
        parser = self.parsers.get(page.school_id, self.common_parser)
        try:
            return parser.parse(page.raw_html)
        except Exception as error:  # 전용 파서 실패 시 공통 파서로 안전하게 폴백
            if parser is self.common_parser:
                raise
            LOGGER.warning("content parser failed for %s: %s; using common parser", page.source_url, type(error).__name__)
            return self.common_parser.parse(page.raw_html)

    def _call_llm(self, text: str) -> Extraction:
        last_error: Exception | None = None
        retryable = True
        for attempt in range(self.max_retries + 1):
            try:
                return self.llm_extractor.extract(text)
            except ValidationError as error:
                # JSON 형식/스키마 오류는 같은 입력을 다시 보내도 복구될 가능성이 낮다.
                raise _LLMCallError("LLM_INVALID_RESPONSE", retryable=False) from error
            except Exception as error:  # SDK별 네트워크/일시 오류 타입에 결합하지 않는다.
                last_error = error
                if attempt < self.max_retries:
                    self.sleeper(float(attempt + 1))
        detail = _safe_error_detail(last_error)
        raise _LLMCallError("LLM_CALL_FAILED", retryable=retryable, detail=detail) from last_error

    def _validate_extraction(self, extraction: Extraction) -> tuple[Extraction, list[str]]:
        warnings: list[str] = []
        entities: list[ExtractedEntity] = []
        names: set[str] = set()
        types_by_name: dict[str, set[str]] = {}
        for entity in extraction.entities:
            name = entity.name.strip()
            if entity.type not in ENTITY_TYPES:
                warnings.append(f"discarded entity type: {entity.type}")
            elif not name:
                warnings.append("discarded entity with empty name")
            else:
                entities.append(entity.model_copy(update={"name": name}))
                names.add(name)
                types_by_name.setdefault(name, set()).add(entity.type)

        relations: list[ExtractedRelation] = []
        for relation in extraction.relations:
            source, target = relation.source.strip(), relation.target.strip()
            # LLM이 자주 만드는 '부서 → 공지 = 게시'를 문서 계약의 '공지 → 부서 = 게시'로 정리한다.
            if (
                relation.relation == "게시"
                and types_by_name.get(source, set()) & {"부서·기관", "담당자"}
                and "공지" in types_by_name.get(target, set())
            ):
                source, target = target, source
            if relation.relation not in RELATION_TYPES:
                warnings.append(f"discarded relation type: {relation.relation}")
            elif not source or not target:
                warnings.append("discarded relation with empty endpoint")
            elif source not in names or target not in names:
                warnings.append(f"discarded relation with unknown endpoint: {source} -> {target}")
            else:
                relations.append(relation.model_copy(update={"source": source, "target": target}))
        return Extraction(entities=entities, relations=relations), warnings

    @staticmethod
    def _ensure_crawler_metadata(
        extraction: Extraction,
        page: CrawledPage,
        title: str | None,
    ) -> Extraction:
        """Crawler가 확정한 게시판 분류는 LLM 누락 여부와 무관하게 보존한다."""
        category = page.category_hint.strip() if page.category_hint else ""
        if not category:
            return extraction

        entities = list(extraction.entities)
        relations = list(extraction.relations)
        entity_pairs = {(entity.type, entity.name) for entity in entities}
        notice_names = [entity.name for entity in entities if entity.type == "공지"]
        if not notice_names and title:
            entities.append(ExtractedEntity(type="공지", name=title))
            entity_pairs.add(("공지", title))
            notice_names.append(title)
        if ("주제·카테고리", category) not in entity_pairs:
            entities.append(ExtractedEntity(type="주제·카테고리", name=category))

        relation_keys = {(relation.source, relation.relation, relation.target) for relation in relations}
        for notice_name in notice_names:
            relation_key = (notice_name, "분류", category)
            if relation_key not in relation_keys:
                relations.append(ExtractedRelation(source=notice_name, relation="분류", target=category))
                relation_keys.add(relation_key)
        return Extraction(entities=entities, relations=relations)

    def _llm_input(self, page: CrawledPage, title: str | None, content: str) -> str:
        metadata = []
        if title:
            metadata.append(f"제목: {title}")
        if page.category_hint:
            metadata.append(f"분류: {page.category_hint}")
        if page.author_hint:
            metadata.append(f"작성자: {page.author_hint}")
        if page.published_at_hint:
            metadata.append(f"게시일: {page.published_at_hint.isoformat()}")
        for attachment in page.attachments:
            label = attachment.name_hint or attachment.url
            metadata.append(f"첨부 링크: {label} ({attachment.url})")
        prefix = "\n".join(metadata)
        document = f"{prefix}\n\n본문:\n{content}" if prefix else content
        return f"{_WHITELIST_INSTRUCTION}\n\n{document}"

    @staticmethod
    def _failure(
        page: CrawledPage,
        error_code: str,
        *,
        retryable: bool,
        warnings: list[str] | None = None,
    ) -> ExtractionFailure:
        return ExtractionFailure(
            school_id=page.school_id,
            source_url=page.source_url,
            content_hash=page.content_hash,
            error_code=error_code,
            retryable=retryable,
            warnings=warnings or [],
        )


class _LLMCallError(Exception):
    def __init__(self, code: str, *, retryable: bool, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.detail = detail


def _safe_error_detail(error: Exception | None) -> str:
    """API 키·본문을 출력하지 않고, 외부 호출 실패 원인만 짧게 남긴다."""
    if error is None:
        return "UnknownError"
    message = " ".join(str(error).splitlines()).strip()
    # Google SDK 오류에는 일반적으로 상태 코드·원인이 들어 있다. 지나치게 긴 응답은 남기지 않는다.
    return f"{type(error).__name__}: {message[:300]}" if message else type(error).__name__
