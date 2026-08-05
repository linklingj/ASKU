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
    "max_items": 300,
    "max_requests": 500,
    "max_duration_seconds": 600
  }
}
```

필수 필드는 `crawl_id`, `school_id`, `base_url`, `mode`다. MVP 최초 수집은 최근 목록 10페이지(`max_listing_pages: 10`)를 순회한다. 한 페이지의 공지 수가 사이트마다 다른 점을 고려해 상세 공지는 총 300건(`max_items: 300`)에서만 중단한다. 두 값은 학교별 운영 정책으로 조정할 수 있다.

`max_listing_pages`·`max_items`는 **게시판 하나에** 적용된다. 하위 게시판(탭)이 여러 개면 그만큼 곱해지므로, 크롤 1회 전체는 `max_requests`·`max_duration_seconds` 예산이 묶는다(§4-2).

Backend API는 `allowed_hosts`만 학교의 `base_url` 호스트로 채우고 `path_prefixes`는 비워 둔다. 홍익대처럼 공지 목록에 같은 학교의 다른 게시판(`/kr/education/`) 링크가 섞이는 경우를 놓치지 않기 위해서다. 외부 도메인 이탈은 `allowed_hosts`가 막는다.

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
{"crawl_id":"uuid","school_id":1,"source_url":"https://...","stage":"policy | fetch | render | budget","error_code":"...","retryable":true,"occurred_at":"ISO-8601"}
```

## 4. 처리 흐름

1. Backend API 또는 Scheduler가 `CrawlRequest`를 만든다.
2. `base_url` 호스트로 학교별 어댑터와 수집할 게시판 목록을 정한다(§4-1).
3. 기준 URL의 허용 범위·`robots.txt`·요청 제한을 확인한다.
4. 목록 페이지에서 상세 URL과 메타데이터를 수집하고, 다음 목록 링크를 따라 최대 `max_listing_pages`까지 순회한다. 국내 대학의 서버 렌더링 `.do` 게시판은 공통 목록/상세 선택자와 학교별 오버라이드(현재 연세대·세종대·홍익대·성균관대 K2Web 목록)로 처리한다.
5. URL을 정규화하고 같은 실행 안에서 이미 방문했으면 건너뛴다. 상세 URL에서는 목록 페이지 문맥(예: `article.offset`)을 제거해 고정 공지 중복을 막는다. 수집 건수가 `max_items`에 도달하면 그 게시판을 끝낸다.
6. 상세 HTML을 수집하고 본문 해시를 계산한다. 목록에서 상세로 이동하는 요청에는 현재 목록 URL을 `Referer`로 전달해 브라우저 클릭 흐름을 요구하는 사이트를 지원한다. 정적 수집이 불완전한 경우에만 렌더링 수집기로 전환한다.
7. `doc_hash_exists(school_id, source_url, content_hash)`와 `doc_url_exists(school_id, source_url)`로 이전 처리본과 비교한다.
8. `new`·`changed`인 `CrawledPage`만 Extractor에 전달한다. `unchanged`는 실행 상태만 기록한다.
9. 실행 완료 시 성공·변경·실패·미관측 URL 통계를 저장한다.

```text
frontier URL -> 정책 검사 -> 목록/상세 수집 -> URL 정규화 + 해시 계산
             -> 동일 해시이면 상태만 기록
             -> 신규/변경이면 CrawledPage를 Extractor로 전달
```

## 4-1. 학교별 어댑터와 게시판 선택

어댑터는 `base_url`의 **호스트**로 고르며, 순서는 다음과 같다(`adapter_for`).

```text
전용 어댑터 클래스  →  수집 규격(adapter_specs)  →  CommonNoticeAdapter
```

손으로 검증한 전용 클래스가 규격보다 앞선다. 자동 생성한 규격이 잘못돼도 이미 검증된 학교는 영향을 받지 않는다. 공통 파서는 `table tbody tr` 기반이라 연세대(`ul > li`)·성균관대(`dl`)처럼 목록 구조가 다른 게시판에서는 한 줄도 읽지 못하므로, **두 단계 어디에도 걸리지 않으면 수집이 0건**이 된다. 이 상태는 §6-2 검증기가 `NO_LISTING_ROWS`로 잡는다.

수집 규격은 선택자와 페이지네이션 방식을 데이터로 적은 것이다(`app/adapter_spec.py`). 학교가 늘어도 코드는 그대로고 규격 행만 늘어나며, 재배포 없이 다음 크롤부터 적용된다. Extractor의 본문 파서도 같은 규격을 같은 순서로 쓴다([`04_extractor.md`](04_extractor.md)).

규격은 호출자(Backend API)가 저장소에서 꺼내 `adapter_for(url, spec)`으로 넘긴다. Crawler가 Storage를 직접 알지 않게 하고, 페이지마다 조회하는 일도 없게 하기 위해서다.

페이지네이션은 선택자만으로 표현되지 않아 세 유형으로 나눈다.

| 유형 | 방식 | 예 |
|---|---|---|
| `link` | '다음' 링크의 `href`를 따라간다 | 세종대·홍익대·성균관대 |
| `offset` | URL 파라미터를 일정하게 늘린다 | 링크가 없는 게시판의 폴백 |
| `form` | 폼의 hidden input을 조합해 URL을 만든다 | 연세대 |

새 방식이 나오면 유형을 추가해야 한다. 규격만으로 모든 사이트를 덮지는 못하며, 그때는 전용 클래스가 마지막 안전장치다.

한 학교의 공지가 여러 탭으로 쪼개진 경우(세종대 `notice1~10.do`) 게시판 목록을 등록하고 `crawl_boards`로 전부 순회한다. 등록이 없는 호스트는 `base_url` 하나만 수집한다.

게시판 목록도 같은 순서로 고른다 — 등록 목록 → 규격의 `boards` → 기준 URL 하나.

- **게시판 목록은 자동 탐색하지 않는다.** 세종대는 같은 메뉴에 `qna1~8.do`(Q&A)가 섞여 있고 `robots.txt`가 이를 막고 있어, URL 패턴만으로는 공지 게시판을 가려낼 수 없다.
- 게시판 라벨(`장학`, `채용·모집` 등)은 목록이 분류를 주지 않을 때만 `category_hint`로 전달한다. 홍익대처럼 행마다 분류가 붙는 학교의 값을 탭 이름으로 덮어쓰지 않는다.
- 게시판을 하나씩 끝까지 도는 대신 **한 페이지씩 번갈아** 돈다(라운드 로빈). 목록은 최신순이므로 예산이 부족해도 모든 탭의 최신 공지가 먼저 확보된다. 순서대로 돌면 공지가 많은 앞쪽 탭이 예산을 다 써서 뒤쪽 탭이 한 건도 수집되지 않는다. 세종대 9개 탭·기본 예산 기준으로 순차 순회는 `160/160/150/0/0/0/0/0/0`, 라운드 로빈은 `64/64/54/48/48/48/48/48/48`이다.
- 예산과 중복 URL 집합은 게시판 사이에서 **공유**한다. 게시판마다 새로 잡으면 탭이 늘어난 만큼 총 요청량이 늘고, 여러 탭에 함께 걸린 공지를 중복 수집한다.
- 반면 `max_items`는 게시판마다 **따로** 센다. 공유하면 공지가 많은 첫 탭이 상한을 다 써서 뒤쪽 탭이 한 건도 수집되지 않는다.

## 4-2. 수집 예산과 조기 종료

`max_requests`·`max_duration_seconds`는 게시판 수와 무관하게 크롤 1회 전체를 묶는 상한이다. 재시도도 서버에 대한 요청이므로 시도마다 예산을 쓴다.

예산이 바닥나면 예외를 던지지 않고 **수집을 멈추되 그때까지 모은 페이지는 유지한다.** 부분 수집이 전량 실패보다 낫다. 중단 사유는 `stage: "budget"`, `error_code: "REQUEST_BUDGET_EXCEEDED" | "TIME_BUDGET_EXCEEDED"`로 남긴다. 기록이 없으면 목록이 원래 짧은 것인지 상한에 걸린 것인지 구분되지 않는다. 다음 크롤에서 이어받으면 되므로 재시도 대상으로 표시한다.

재크롤(`mode: "recrawl"`)에서는 목록 한 페이지가 통째로 `unchanged`이면 그 게시판을 끝낸다. 목록은 최신순이라 더 오래된 페이지도 마찬가지다. 초기 수집에는 적용하지 않는다.

> `robots.txt` 요청은 예산에서 세지 않는다. 오리진당 한 번만 가져와 캐시하므로 크롤 1회에 1~2건이다.

## 5. 다른 시스템과의 연동

| 시스템 | 방향 | 전달 또는 호출 |
|---|---|---|
| Backend API | 이전 | 학교 등록·수동 재크롤링 요청을 받는다. |
| Scheduler | 이전 | 학교별 주기에 따른 `CrawlRequest`를 받는다. |
| Storage | 양방향 | 기존 본문 해시 조회와 실행 상태 기록을 공개 인터페이스로 요청한다. |
| Extractor | 다음 | `new`·`changed`의 `CrawledPage`를 전달한다. |

호출을 동기 API로 할지 작업 큐로 할지는 **미정**이다. 어느 방식이든 `crawl_id`와 `school_id`를 포함해 실행을 추적한다.

## 5-2. robots.txt 정책과 요청 간격

파서는 `protego`를 쓴다. 표준 `urllib.robotparser`는 경로 안 와일드카드(`Disallow: /*?mode=view`)와 `$` 앵커를 지원하지 않고, Allow/Disallow 우선순위도 RFC 9309의 최장 일치가 아니라 파일에 쓰인 순서로 정한다. 두 한계가 각각 **금지된 URL을 허용으로**, **허용된 게시판을 금지로** 오판한다.

응답 상태별 판정(RFC 9309 §2.3.1):

| robots.txt 응답 | 판정 | 실패 코드 |
|---|---|---|
| 200 | 규칙대로 | — |
| 401 · 403 | 오리진 전체 금지 | `ROBOTS_DISALLOWED` (재시도 안 함) |
| 그 밖의 4xx (404 등) | 전면 허용 | — |
| 5xx · 네트워크 오류 | 일시적 전체 금지 | `ROBOTS_UNREACHABLE` (재시도 대상) |

- 판정은 **오리진당 한 번만** 가져와 캐시한다. 거부까지 캐시해야 robots.txt가 401/403인 사이트에 URL마다 다시 요청하는 일이 없다.
- 검사 대상은 기준 URL·상세 URL뿐 아니라 **모든 목록 페이지**다. 페이지네이션 URL만 막아 둔 사이트를 2페이지부터 그냥 통과시키면 안 된다.
- 요청 간격은 `max(설정값 1초, robots.txt의 Crawl-delay)`이며, **응답 상태와 무관하게** 지킨다. 200일 때만 쉬면 죽은 링크가 늘어선 목록을 무지연으로 연타하게 된다. 재시도 백오프(1→2→4초)는 이와 별개로 더해진다.
- `robots.txt` 자체를 가져올 때는 아직 선언값을 모르므로 설정값(1초)을 쓴다.

## 6. 오류 처리

- `robots.txt` 거부·허용 범위 밖 URL은 재시도하지 않고 정책 거부로 기록한다. 다만 `robots.txt`를 읽지 못해 막은 경우(`ROBOTS_UNREACHABLE`)는 나중에 풀릴 수 있으므로 재시도 대상으로 남긴다.
- 네트워크 오류, 429, 5xx는 최대 3회, 기본 1초 간격의 지수 백오프 재시도 대상이다. 요청 간 기본 간격은 1초이며 `robots.txt`가 더 엄격하면 그 값을 우선한다.
- 4xx, 파싱 불가 콘텐츠, 렌더링 실패는 해당 페이지의 실패로 남기고 다른 URL 수집은 계속한다.
- `school_id + canonical_url + content_hash`와 `crawl_id`를 멱등 키로 사용해 재시도 중복을 막는다.
- 한 페이지 실패가 기존 문서나 그래프 데이터를 삭제하지 않는다. 미관측 URL의 만료 판정은 Scheduler의 확인 정책 뒤에만 수행한다.
- 실행 상태는 `queued / running / completed / partial_failed / failed`로 기록한다.

## 6-2. 수집 품질 검증

파서가 사이트 구조와 어긋나도 크롤은 예외 없이 **0건 성공**으로 끝난다. 상태값만 보면 정상과 구분되지 않으므로, 실행마다 지표를 남겨 드러낸다(`app/validation.py`).

크롤이 끝나면 게시판별로 판정해 `crawl_quality` 테이블에 이력으로 쌓고, `GET /schools/{id}` 의 `crawl_quality` 로 노출한다. 이력으로 쌓는 이유는 직전 실행과 비교해야 급감을 알 수 있기 때문이다. 판정은 이미 받아 둔 HTML 로만 하며 추가 요청을 보내지 않는다.

| 코드 | 뜻 |
|---|---|
| `NO_LISTING_ROWS` | 목록을 한 줄도 읽지 못했다(파서와 구조 불일치) |
| `LISTING_ROWS_DROPPED` | 직전 크롤의 절반 아래로 줄었다(부분 불일치·사이트 개편) |
| `MISSING_TITLES` · `MISSING_DATES` | 링크는 잡았으나 메타데이터 선택자가 어긋났다 |
| `EMPTY_CONTENT` | 본문이 최소 길이에 못 미친다 |
| `NEIGHBOUR_LEAK` | 다른 공지 제목이 본문에 섞였다(이전·다음 글 목록까지 잡음) |
| `TITLE_MISMATCH` | 목록 제목이 상세 페이지에 없다(상세 링크 오연결) |
| `BODY_FALLBACK` | 본문 영역을 못 찾아 페이지 전체를 본문으로 썼다 |
| `PAGINATION_LOOP` | 다음 페이지 링크가 현재 페이지와 같다 |

기준값(`MIN_TITLE_RATIO`, `LISTING_DROP_RATIO` 등)은 운영하며 오탐·미탐을 보고 조정한다. 특히 행 수 급감은 공지가 실제로 줄어드는 경우와 구분되지 않으므로 경고 성격으로 다룬다.

학교를 등록하기 전이나 파서를 고친 뒤에는 같은 기준을 개발용 명령으로 확인한다.

```bash
PYTHONPATH=backend python3 backend/scripts/validate_school.py --all
```

> **조사에 존재하지 않는 게시글 번호를 쓰지 않는다.** 일부 사이트는 없는 `articleNo` 를 요청해도 404 대신 목록을 돌려준다. 이를 상세 페이지로 오인하면 멀쩡한 학교를 "본문이 없는 사이트"로 잘못 판정한다. 반드시 목록에서 얻은 실제 링크를 쓴다.

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

> **미리보기는 목록 1페이지만 본다.** `--max-items`를 올려도 첫 페이지의 공지 수(사이트에 따라 10~26건)를 넘지 못하고, 스크립트가 학교별로 좁은 `path_prefixes`를 걸어 다른 게시판으로 나가는 링크도 제외한다. 이 때문에 실제 서비스보다 수집량이 적게 나오는 것이 정상이며, 서비스 수집량을 가늠하는 용도로는 쓸 수 없다.
