# 크롤러

> 개발·테스트용 실제 대학 공지 게시판 목록과 공통 CMS 패턴 관찰: [`references/sample-sites.md`](../references/sample-sites.md)

## 1. 목적

Crawler는 학교의 `base_url`에서 공지·학사 정보를 수집해 Extractor가 처리할 원문 문서를 제공한다. 전체 흐름에서 URL 발견, 정책 준수, HTML 수집과 변경 감지를 담당하며, 문서의 의미 해석·청킹·엔티티 추출은 하지 않는다.

## 2. 책임 범위

### 하는 일

- 학교별 기준 URL에서 목록·상세 페이지를 발견하고, 허용된 호스트와 경로 안에서 순회한다.
- `robots.txt`, 이용약관, 요청 간격을 적용하고 수집 이력을 기록한다. `robots.txt`를 확인할 수 없거나 허용하지 않는 URL은 수집하지 않는다.
- MVP는 `requests + BeautifulSoup`으로 정적 HTML을 수집한다. 응답 HTML에 목록 링크나 본문이 없을 때만 렌더링 수집기(Playwright) 전환 후보로 기록한다.
- 목록의 제목·작성 부서·등록일·분류 등 이미 제공되는 메타데이터와 상세 HTML·첨부파일 URL을 `CrawledPage`로 전달한다.
- 정규 URL과 본문 해시로 중복과 변경을 판정한다.

### 하지 않는 일

- 청킹, 텍스트 정제, 문서 유형 확정, 엔티티·관계 추출
- 첨부파일의 PDF/HWP/OCR 본문 파싱
- 임베딩 생성, 그래프 구성, 데이터베이스 직접 쓰기
- 재크롤링 주기 결정(이는 Scheduler의 책임)

> **첨부 본문은 업로드 경로로 처리한다.** 수강편람 PDF·학칙 HWP 등은 크롤러가 내려받아
> 파싱하지 않고, 사용자가 `POST /schools/{id}/attachments`로 직접 올린다
> ([`01_backend-api.md`](01_backend-api.md) §2.4-1). 크롤러는 지금까지처럼 첨부의
> URL·파일명 힌트만 `CrawledPage.attachments`에 담아 전달한다. 로그인·세션이 필요한
> 다운로드 서블릿이나 robots.txt 제약에 걸리는 파일도 사용자가 올리면 색인되므로,
> 크롤러 범위를 넓히지 않고도 문서 RAG가 성립한다([`07_graph-rag-engine.md`](07_graph-rag-engine.md)).

Crawler는 Storage의 공개 조회 인터페이스 `doc_hash_exists`와 `doc_url_exists`로 기존 본문 해시와 원문 URL 처리 이력을 조회하며, 다른 테이블이나 SQL에 직접 접근하지 않는다. 같은 해시이면 `unchanged`, 같은 URL의 다른 해시면 `changed`, 처음 보는 URL이면 `new`다. 구현에서는 `Crawler.from_storage(storage)`로 두 조회를 연결한다.

## 3. 입력과 출력

### 입력: `CrawlRequest`

```json
{
  "crawl_id": "uuid",
  "school_id": 1,
  "base_url": "https://university.example.edu/notice",
  "mode": "initial | recrawl",
  "scope": {
    "allowed_hosts": ["university.example.edu"],
    "path_prefixes": ["/notice", "/academic"],
    "max_listing_pages": 10,
    "max_items": 300
  }
}
```

필수 필드는 `crawl_id`, `school_id`, `base_url`, `mode`다. MVP 최초 수집은 최근 목록 10페이지(`max_listing_pages: 10`)를 순회한다. 한 페이지의 공지 수가 사이트마다 다른 점을 고려해 상세 공지는 총 300건(`max_items: 300`)에서만 중단한다. 두 값은 학교별 운영 정책으로 조정할 수 있다.

### 출력: `CrawledPage`

```json
{
  "crawl_id": "uuid",
  "school_id": 1,
  "source_url": "https://...",
  "canonical_url": "https://...",
  "title_hint": "게시글 제목 또는 null",
  "category_hint": "학사 또는 null",
  "author_hint": "작성 부서 또는 null",
  "published_at_hint": "ISO-8601 또는 null",
  "raw_html": "<html>...</html>",
  "attachments": [{"url": "https://...", "name_hint": "신청서.hwp", "content_type": null}],
  "content_hash": "sha256",
  "fetched_at": "ISO-8601",
  "crawl_status": "new | changed | unchanged"
}
```

`school_id`, `source_url`, `canonical_url`, `raw_html`, `content_hash`, `fetched_at`, `crawl_status`가 성공 출력의 필수 필드다. `raw_html`은 Extractor에 전달할 일시 입력이며 장기 보관하지 않는다. 첨부파일은 URL·파일명 힌트만 수집하고 바이너리 다운로드·본문 파싱은 하지 않는다.

실패 시에는 다음 `CrawlFailure`를 실행 이력에 남긴다.

```json
{"crawl_id":"uuid","school_id":1,"source_url":"https://...","stage":"policy | fetch | render","error_code":"...","retryable":true,"occurred_at":"ISO-8601"}
```

## 4. 처리 흐름

1. Backend API 또는 Scheduler가 `CrawlRequest`를 만든다.
2. 기준 URL의 허용 범위·`robots.txt`·요청 제한을 확인한다.
3. 목록 페이지에서 상세 URL과 메타데이터를 수집하고, 다음 목록 링크를 따라 최대 `max_listing_pages`까지 순회한다. 국내 대학의 서버 렌더링 `.do` 게시판은 공통 목록/상세 선택자와 학교별 오버라이드(현재 연세대·세종대·홍익대·성균관대 K2Web 목록)로 처리한다.
4. URL을 정규화하고 같은 실행 안에서 이미 방문했으면 건너뛴다. 상세 URL에서는 목록 페이지 문맥(예: `article.offset`)을 제거해 고정 공지 중복을 막는다. 상세 URL이 `max_items`에 도달하면 수집을 끝낸다.
5. 상세 HTML을 수집하고 본문 해시를 계산한다. 목록에서 상세로 이동하는 요청에는 현재 목록 URL을 `Referer`로 전달해 브라우저 클릭 흐름을 요구하는 사이트를 지원한다. 정적 수집이 불완전한 경우에만 렌더링 수집기로 전환한다.
6. `doc_hash_exists(school_id, source_url, content_hash)`와 `doc_url_exists(school_id, source_url)`로 이전 처리본과 비교한다.
7. `new`·`changed`인 `CrawledPage`만 Extractor에 전달한다. `unchanged`는 실행 상태만 기록한다.
8. 실행 완료 시 성공·변경·실패·미관측 URL 통계를 저장한다.

```text
frontier URL -> 정책 검사 -> 목록/상세 수집 -> URL 정규화 + 해시 계산
             -> 동일 해시이면 상태만 기록
             -> 신규/변경이면 CrawledPage를 Extractor로 전달
```

## 5. 다른 시스템과의 연동

| 시스템 | 방향 | 전달 또는 호출 |
|---|---|---|
| Backend API | 이전 | 학교 등록·수동 재크롤링 요청을 받는다. |
| Scheduler | 이전 | 학교별 주기에 따른 `CrawlRequest`를 받는다. |
| Storage | 양방향 | 기존 본문 해시 조회와 실행 상태 기록을 공개 인터페이스로 요청한다. |
| Extractor | 다음 | `new`·`changed`의 `CrawledPage`를 전달한다. |

호출을 동기 API로 할지 작업 큐로 할지는 **미정**이다. 어느 방식이든 `crawl_id`와 `school_id`를 포함해 실행을 추적한다.

## 6. 오류 처리

- `robots.txt` 거부·허용 범위 밖 URL은 재시도하지 않고 정책 거부로 기록한다.
- 네트워크 오류, 429, 5xx는 최대 3회, 기본 1초 간격의 지수 백오프 재시도 대상이다. 요청 간 기본 간격은 1초이며 `robots.txt`가 더 엄격하면 그 값을 우선한다.
- 4xx, 파싱 불가 콘텐츠, 렌더링 실패는 해당 페이지의 실패로 남기고 다른 URL 수집은 계속한다.
- `school_id + canonical_url + content_hash`와 `crawl_id`를 멱등 키로 사용해 재시도 중복을 막는다.
- 한 페이지 실패가 기존 문서나 그래프 데이터를 삭제하지 않는다. 미관측 URL의 만료 판정은 Scheduler의 확인 정책 뒤에만 수행한다.
- 실행 상태는 `queued / running / completed / partial_failed / failed`로 기록한다.

## 7. 확장 가능성

- 모든 작업에 `school_id`와 URL 범위를 넣어 여러 학교를 격리한다.
- 목록 탐색, 상세 파싱, 렌더링 수집을 어댑터로 분리해 사이트별 구조 변경에 대응한다.
- 공지 외 FAQ·규정·시설 안내를 지원할 때는 경로 규칙과 문서 분류 힌트만 추가하고 출력 계약은 유지한다.
- MVP 수집 도구는 `requests + BeautifulSoup`이다. 동적 페이지가 확인되면 Playwright 어댑터를 추가할 수 있다.

## 8. 미정 사항

- 학교별 탐색 깊이·기간 필터, 인증 페이지 비수집 정책
- Playwright 전환 뒤의 렌더링 대기·인증 처리 방식
- 삭제 공지를 만료로 확정하는 연속 미관측 횟수와 수동 확인 절차

## 9. 개발용 수집 미리보기

개발자는 다음 명령으로 연세대·세종대·홍익대·성균관대 중 한 학교의 최근 공지 수집 결과를 터미널에서 확인할 수 있다. 이 명령은 저장소에 쓰지 않으며, 기존 Crawler와 동일하게 `robots.txt`, 기본 1초 요청 간격, `ASKU-Crawler/0.1` 사용자 에이전트를 적용한다.

```bash
PYTHONPATH=backend python3 backend/scripts/preview_crawl.py hongik
```

기본 수집량은 목록 1페이지·상세 공지 5건이며, 필요하면 최대 30건까지 `--max-items`를 지정할 수 있다.
