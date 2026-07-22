# 크롤러

> 개발·테스트용 실제 대학 공지 게시판 목록과 공통 CMS 패턴 관찰: [`references/sample-sites.md`](../references/sample-sites.md)

## 1. 목적

Crawler는 학교의 `base_url`에서 공지·학사 정보를 수집해 Extractor가 처리할 원문 문서를 제공한다. 전체 흐름에서 URL 발견, 정책 준수, HTML 수집과 변경 감지를 담당하며, 문서의 의미 해석·청킹·엔티티 추출은 하지 않는다.

## 2. 책임 범위

### 하는 일

- 학교별 기준 URL에서 목록·상세 페이지를 발견하고, 허용된 호스트와 경로 안에서 순회한다.
- `robots.txt`, 이용약관, 요청 간격을 적용하고 수집 이력을 기록한다.
- 정적 HTML을 수집한다. 자바스크립트 렌더링이 필요할 때는 렌더링 수집기 후보(Playwright)를 선택할 수 있게 요청을 구분한다.
- 목록의 제목·작성 부서·등록일·분류 등 이미 제공되는 메타데이터와 상세 HTML·첨부파일 URL을 `CrawledPage`로 전달한다.
- 정규 URL과 본문 해시로 중복과 변경을 판정한다.

### 하지 않는 일

- 청킹, 텍스트 정제, 문서 유형 확정, 엔티티·관계 추출
- 첨부파일의 PDF/HWP/OCR 본문 파싱
- 임베딩 생성, 그래프 구성, 데이터베이스 직접 쓰기
- 재크롤링 주기 결정(이는 Scheduler의 책임)

Crawler는 Storage의 `doc_hash_exists`만으로 기존 본문 해시를 조회하며, 다른 테이블이나 SQL에 직접 접근하지 않는다.

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
    "max_pages": 1000
  }
}
```

필수 필드는 `crawl_id`, `school_id`, `base_url`, `mode`다. `scope`의 구체값은 학교별 운영 정책으로 아직 미정이다.

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

`school_id`, `source_url`, `canonical_url`, `raw_html`, `content_hash`, `fetched_at`, `crawl_status`가 성공 출력의 필수 필드다. `raw_html`은 Extractor에 전달할 일시 입력이며, 현재 확정된 Storage 스키마는 정제된 청크와 원문 URL을 보관한다. 원본 HTML 영구 보관은 **미정**이다.

실패 시에는 다음 `CrawlFailure`를 실행 이력에 남긴다.

```json
{"crawl_id":"uuid","school_id":1,"source_url":"https://...","stage":"policy | fetch | render","error_code":"...","retryable":true,"occurred_at":"ISO-8601"}
```

## 4. 처리 흐름

1. Backend API 또는 Scheduler가 `CrawlRequest`를 만든다.
2. 기준 URL의 허용 범위·`robots.txt`·요청 제한을 확인한다.
3. 목록 페이지에서 상세 URL과 메타데이터를 수집한다. 국내 대학의 서버 렌더링 `.do` 게시판은 공통 목록/상세 선택자와 학교별 오버라이드로 처리한다.
4. URL을 정규화하고 같은 실행 안에서 이미 방문했으면 건너뛴다.
5. 상세 HTML을 수집하고 본문 해시를 계산한다. 정적 수집이 불완전한 경우에만 렌더링 수집기로 전환한다.
6. `doc_hash_exists(school_id, source_url, content_hash)`로 이전 처리본과 비교한다.
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
- 네트워크 오류, 429, 5xx는 제한 횟수의 지수 백오프 재시도 대상이다. 횟수·최대 지연·타임아웃은 **미정**이다.
- 4xx, 파싱 불가 콘텐츠, 렌더링 실패는 해당 페이지의 실패로 남기고 다른 URL 수집은 계속한다.
- `school_id + canonical_url + content_hash`와 `crawl_id`를 멱등 키로 사용해 재시도 중복을 막는다.
- 한 페이지 실패가 기존 문서나 그래프 데이터를 삭제하지 않는다. 미관측 URL의 만료 판정은 Scheduler의 확인 정책 뒤에만 수행한다.
- 실행 상태는 `queued / running / completed / partial_failed / failed`로 기록한다.

## 7. 확장 가능성

- 모든 작업에 `school_id`와 URL 범위를 넣어 여러 학교를 격리한다.
- 목록 탐색, 상세 파싱, 렌더링 수집을 어댑터로 분리해 사이트별 구조 변경에 대응한다.
- 공지 외 FAQ·규정·시설 안내를 지원할 때는 경로 규칙과 문서 분류 힌트만 추가하고 출력 계약은 유지한다.
- Playwright, Scrapy, BeautifulSoup 중 실제 수집 도구는 PLAN상 **미정**이다.

## 8. 미정 사항

- 학교별 최대 페이지 수·탐색 깊이·최근 N페이지/기간 제한
- 동적 페이지 판정 기준과 인증 페이지 비수집 정책
- 첨부파일 바이너리 수집 및 원본 HTML의 영구 저장 범위
- 속도 제한·재시도·타임아웃 운영값
- 삭제 공지를 만료로 확정하는 연속 미관측 횟수와 수동 확인 절차
