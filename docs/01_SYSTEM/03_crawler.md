# 크롤러

> 개발·테스트용 실제 대학 공지 게시판 목록과 공통 CMS 패턴 관찰: [`references/sample-sites.md`](../references/sample-sites.md)

## 1. 목적

Crawler는 학교의 `base_url`에서 공지·학사 정보를 수집해 Extractor가 처리할 원문 문서를 제공한다. 전체 흐름에서 URL 발견, 정책 준수, HTML 수집과 변경 감지를 담당하며, 문서의 의미 해석·청킹·엔티티 추출은 하지 않는다.

## 2. 책임 범위

### 하는 일

- 학교별 기준 URL에서 목록·상세 페이지를 발견하고, 허용된 호스트와 경로 안에서 순회한다.
- `robots.txt`, 이용약관, 요청 간격을 적용하고 수집 이력을 기록한다. `robots.txt`를 확인할 수 없거나 허용하지 않는 URL은 수집하지 않는다.
  - 파서는 **`protego`**를 쓴다. 표준 `urllib.robotparser`는 와일드카드 규칙(`Disallow: /*?mode=download`)의 `*`·`?`를 URL 인코딩해 규칙을 통째로 무력화하고, 상위 `Disallow` 아래의 구체적인 `Allow`가 우선하는 규칙도 처리하지 못한다. 실제 세종대 `robots.txt`에서 두 오판(금지된 첨부 다운로드를 허용으로, 허용된 경로를 금지로)이 모두 재현돼 교체했다.
- MVP는 `requests + BeautifulSoup`으로 정적 HTML을 수집한다. 응답 HTML에 목록 링크나 본문이 없을 때만 렌더링 수집기(Playwright) 전환 후보로 기록한다.
- 목록의 제목·작성 부서·등록일·분류 등 이미 제공되는 메타데이터와 상세 HTML·첨부파일 URL을 `CrawledPage`로 전달한다.
- 정규 URL과 본문 해시로 중복과 변경을 판정한다.
- 공지 상세에서 발견한 첨부파일 중 **PDF만** `fetch_pdf_attachments()`로 실제로
  내려받아 (파일명, 바이트) 목록으로 반환한다(#29). 이 바이트는 Crawler를 거쳐
  Backend API의 인덱싱 파이프라인이 `PdfIngestor`에 넘겨 문서 RAG 청크로 저장한다
  ([`07_graph-rag-engine.md`](07_graph-rag-engine.md)).

### 하지 않는 일

- 청킹, 텍스트 정제, 문서 유형 확정, 엔티티·관계 추출
- **PDF 본문 파싱** — Crawler는 PDF 바이트를 받아오기만 하고, 텍스트 추출·청킹·임베딩은
  `PdfIngestor`의 책임이다([`07_graph-rag-engine.md`](07_graph-rag-engine.md)).
- **HWP/DOC/OCR 등 PDF 이외 첨부파일의 다운로드·본문 파싱** — 여전히 URL·파일명
  힌트만 `attachments`에 남기고 받지 않는다. PDF만 자동 수집 대상이다.
- 임베딩 생성, 그래프 구성, 데이터베이스 직접 쓰기
- 재크롤링 주기 결정(이는 Scheduler의 책임)

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

`school_id`, `source_url`, `canonical_url`, `raw_html`, `content_hash`, `fetched_at`, `crawl_status`가 성공 출력의 필수 필드다. `raw_html`은 Extractor에 전달할 일시 입력이며 장기 보관하지 않는다. `CrawledPage.attachments`는 여전히 URL·파일명 힌트만 담는다(스키마 변경 없음) — 실제 바이너리 다운로드는 아래 `fetch_pdf_attachments()` 호출로 별도로 이뤄진다.

실패 시에는 다음 `CrawlFailure`를 실행 이력에 남긴다.

```json
{"crawl_id":"uuid","school_id":1,"source_url":"https://...","stage":"policy | fetch | render","error_code":"...","retryable":true,"occurred_at":"ISO-8601"}
```

### 출력: `fetch_pdf_attachments(request, page, run) -> [DownloadedAttachment]`

인덱싱 단계에서 `new`·`changed` 페이지마다 호출한다. `page.attachments` 중 URL이
`.pdf`로 끝나는 것만 실제로 내려받아 `DownloadedAttachment(url, filename, content)`
목록을 반환한다. HWP/DOC 등은 이 호출에서도 다운로드하지 않는다.

- `url`은 첨부의 실제 링크이며 그대로 `documents.source_url`이 된다(근거 인용 링크이자
  재크롤 미관측 판정의 키, [`06_storage.md`](06_storage.md)).
- **정책 검사**: 첨부도 페이지와 똑같이 `robots.txt`를 지킨다. 허용하지 않는 URL은
  요청 자체를 보내지 않고 `ROBOTS_DISALLOWED` 정책 거부로 기록한다. 대학 사이트는
  다운로드 경로를 `robots.txt`로 막아두는 경우가 흔하다.
- **범위 제한은 호스트만**: 첨부는 `scope.allowed_hosts` 밖이면 건너뛰지만
  `scope.path_prefixes`는 적용하지 않는다(`is_allowed_host`). 첨부는 게시판(`/notice`)과
  다른 경로(`/files`, `/download` 등)에서 서빙되는 것이 일반적이라, 경로 제한을 걸면
  정상 첨부가 모두 막힌다. 경로 제한은 크롤러가 **순회할 페이지** 범위를 묶기 위한
  장치이지 첨부의 위치를 정하는 규칙이 아니다.
- **크기 상한**: `CrawlSettings.max_attachment_bytes`(기본 50MB)까지만 스트리밍으로
  읽는다. `Content-Length`가 있으면 본문을 읽기 전에 거르고, 없거나 값이 거짓이면
  누적 크기로 다시 막는다. 초과분은 `ATTACHMENT_TOO_LARGE` 실패로 기록하고 건너뛴다.
- **실행당 1회**: 같은 첨부 URL이 여러 공지에 걸려 있어도 `run`당 한 번만 시도한다
  (`CrawlRun.attempted_attachment_urls`). 중복 다운로드와 중복 임베딩을 막기 위한
  것으로, 실패한 URL도 시도한 것으로 기록해 재시도 폭주를 함께 막는다.
- 다운로드 실패는 `CrawlFailure`(`stage="fetch"`)로 같은 `run`에 기록되고, 나머지
  첨부·페이지 처리는 계속된다.

## 4. 처리 흐름

1. Backend API 또는 Scheduler가 `CrawlRequest`를 만든다.
2. 기준 URL의 허용 범위·`robots.txt`·요청 제한을 확인한다.
3. 목록 페이지에서 상세 URL과 메타데이터를 수집하고, 다음 목록 링크를 따라 최대 `max_listing_pages`까지 순회한다. 국내 대학의 서버 렌더링 `.do` 게시판은 공통 목록/상세 선택자와 학교별 오버라이드(현재 연세대·세종대·홍익대·성균관대 K2Web 목록)로 처리한다.
4. URL을 정규화하고 같은 실행 안에서 이미 방문했으면 건너뛴다. 상세 URL에서는 목록 페이지 문맥(예: `article.offset`)을 제거해 고정 공지 중복을 막는다. 상세 URL이 `max_items`에 도달하면 수집을 끝낸다.
5. 상세 HTML을 수집하고 본문 해시를 계산한다. 목록에서 상세로 이동하는 요청에는 현재 목록 URL을 `Referer`로 전달해 브라우저 클릭 흐름을 요구하는 사이트를 지원한다. 정적 수집이 불완전한 경우에만 렌더링 수집기로 전환한다.
6. `doc_hash_exists(school_id, source_url, content_hash)`와 `doc_url_exists(school_id, source_url)`로 이전 처리본과 비교한다.
7. `new`·`changed`인 `CrawledPage`만 Extractor에 전달한다. `unchanged`는 실행 상태만 기록한다.
8. Backend API의 인덱싱 단계가 `new`·`changed` 페이지마다 `fetch_pdf_attachments()`를
   호출해 PDF 첨부를 내려받고, `PdfIngestor`로 넘겨 문서 RAG 청크로 저장한다(#29,
   [`07_graph-rag-engine.md`](07_graph-rag-engine.md)).
9. 재크롤이면 관측 URL을 `record_url_observations`에 넘긴다. 이때 공지 URL뿐 아니라
   **모든 `run.pages`의 첨부 URL도 함께 넘긴다** — 첨부 목록은 `unchanged` 페이지에도
   채워지므로, 재다운로드 없이 관측만으로 PDF 청크가 미관측으로 오인돼 만료되는 것을
   막을 수 있다([`06_storage.md`](06_storage.md) §6).
10. 실행 완료 시 성공·변경·실패·미관측 URL 통계를 저장한다.

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
| PdfIngestor | 다음(간접) | Backend API가 `fetch_pdf_attachments()` 결과(URL·파일명·바이트)를 받아 전달한다([`07_graph-rag-engine.md`](07_graph-rag-engine.md)). |

호출을 동기 API로 할지 작업 큐로 할지는 **미정**이다. 어느 방식이든 `crawl_id`와 `school_id`를 포함해 실행을 추적한다.

## 6. 오류 처리

- `robots.txt` 거부·허용 범위 밖 URL은 재시도하지 않고 정책 거부로 기록한다. **첨부파일 다운로드에도 같은 규칙을 적용한다**(§3 참고 — 단 첨부의 범위 제한은 호스트만 본다).
- 네트워크 오류, 429, 5xx는 최대 3회, 기본 1초 간격의 지수 백오프 재시도 대상이다.
- 요청 간 기본 간격은 1초이며 `robots.txt`의 `Crawl-delay`가 더 길면 그 값을 따른다(둘 중 큰 값). 이미 받아둔 robots.txt만 참고하므로 간격을 알아내려고 추가 요청을 보내지 않는다.
  - **운영상 영향**: 수집 시간이 사이트의 `Crawl-delay`에 그대로 비례한다. 예를 들어 세종대는 `Crawl-delay: 10`이라 상세 300건(`max_items` 기본값)을 모으면 목록 요청까지 더해 50분을 넘는다. 수집 시간을 줄여야 하면 간격이 아니라 `max_items`·`max_listing_pages`로 조절한다.
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

개발자는 다음 명령으로 연세대·세종대·홍익대·성균관대 중 한 학교의 최근 공지 수집 결과를 터미널에서 확인할 수 있다. 이 명령은 저장소에 쓰지 않으며, 기존 Crawler와 동일하게 `robots.txt`, 요청 간격(기본 1초, 사이트의 `Crawl-delay`가 더 길면 그 값), `ASKU-Crawler/0.1` 사용자 에이전트를 적용한다. 세종대처럼 `Crawl-delay: 10`인 사이트는 미리보기도 그만큼 느리게 진행된다.

```bash
PYTHONPATH=backend python3 backend/scripts/preview_crawl.py hongik
```

기본 수집량은 목록 1페이지·상세 공지 5건이며, 필요하면 최대 30건까지 `--max-items`를 지정할 수 있다.
