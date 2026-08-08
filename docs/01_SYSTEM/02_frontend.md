# 프론트엔드

세 흐름을 가진 하나의 웹앱이다: **랜딩 → 학교 찾기 → QA**, 그리고 **학교 등록 → QA**.
디자인 원칙은 **모던 · 미니멀 · 타이포그래피 중심 · 애니메이션**. 색은 아끼고(빨강은 마크로만),
구조는 여백과 규칙선(rule)으로 보이며, 전환·리빌은 부드럽게 연출한다.

> 랜딩은 **이미 구현되어 있다** → [`frontend/src/`](../../frontend/src/) (Claude Design 산출물을 런타임 의존 없이 이식). 나머지 두 페이지의 디자인·모션은 랜딩의 토큰을 그대로 계승한다.

> **구현 현황(2026-08)**: 네 페이지(`index`·`find`·`register`·`qa`)가 모두 정적 HTML/CSS/JS 로 구현되어 있고, 아래 §4 백엔드 인터페이스에 **실제로 연동**되어 있다. 연동 범위·목업 대비 조정·현 로직으로 불가능한 항목은 [`02_FEATURES/frontend-integration.md`](../02_FEATURES/frontend-integration.md) 참고. (§0 표의 "Next.js 제안"은 미채택 — 순수 HTML 유지.)

관련 문서: [`10_data-model.md`](10_data-model.md) · [`07_graph-rag-engine.md`](07_graph-rag-engine.md) · [`01_backend-api.md`](01_backend-api.md) · [`00_BASICS/PLAN.md`](../00_BASICS/PLAN.md) §11(API 초안)

실행: `cd "frontend/src" && python3 -m http.server 5500`

---

## 0. 범위 · 스택

| 항목 | 내용 |
|---|---|
| 페이지 | 랜딩 / 학교 찾기(휠 선택) / QA(그래프+질문) / 학교 등록(로딩) |
| 프레임워크 | **Next.js (App Router) + TypeScript** (제안, PLAN §8 "미정") |
| 그래프 렌더 | `react-force-graph`(Canvas/WebGL) 또는 `cytoscape.js` (제안) |
| 모션 | CSS 트랜지션 + IntersectionObserver를 기본으로, 복잡 전환만 `framer-motion` (제안) |
| 상태·페칭 | 서버 상태는 `@tanstack/react-query`, 클라 상태는 로컬(제안) |
| 진행 스트림 | 등록 진행도는 **SSE**(권장) 또는 폴링(폴백) |

> "제안"은 아직 팀 미확정. 새 의존성은 랜딩이 이미 순수 HTML/CSS/JS로 돌아간다는 점을 존중해 **필요할 때만** 추가한다.

---

## 1. 라우팅 · 화면 흐름

```
/                     랜딩 (구현됨)
│  ├─ "학교 찾기" ─────────────► /find
│  └─ "학교 등록" ─────────────► /register
│
/find                 휠 선택 → 학교 확정
│  └─ 선택 ────────────────────► /s/{schoolId}
│
/register             URL 입력 → 로딩(진행도) → 완료
│  └─ done ────────────────────► /s/{schoolId}
│
/s/{schoolId}         QA: 인터랙티브 네트워크 그래프 + 하단 질문바
```

| 경로 | 화면 | 주 진입 |
|---|---|---|
| `/` | 랜딩 | 최초 방문 |
| `/find` | 학교 찾기(휠) | 랜딩 "학교 찾기" |
| `/register` | 학교 등록(로딩) | 랜딩 "학교 등록" |
| `/s/{schoolId}` | QA(그래프+질문) | 휠 선택 / 등록 완료 / 딥링크 |

---

## 2. 디자인 시스템

값의 원본은 [`frontend/src/_ds/.../styles.css`](../../frontend/src/) 와 랜딩 `index.html`. 새 페이지도 여기서만 가져온다.

### 2.1 색 · 테마

| 토큰 | 값 | 용도 |
|---|---|---|
| `--color-bg` | `#f3f2f2` | 밝은 바탕(페이퍼) |
| `--color-surface` | `#eae9e9` | 섹션 대비용 밝은 면 |
| `--color-text` | `#201e1d` | 잉크(본문·다크 섹션 배경) |
| `--color-accent` | `#ec3013` | 빨강 — **마크로만** (선택·강조·CTA) |
| `--color-divider` | `#201e1d 40%` | 2px 규칙선 |
| 그라디언트 | `#ff6a3d · #e12d78 · #8f2fb0` | "Why" 류 필드 모먼트에서만 |

- **섹션 테마 반전**: 섹션마다 `data-nav="dark|light"` 로 상단 내비 잉크 색을 스크롤에 따라 전환(랜딩의 `IntersectionObserver` 패턴 재사용).
- 빨강은 면(field)이 아니라 점·선·라벨로 소비한다. 유일한 빨강 필드는 랜딩의 그라디언트 섹션.

### 2.2 타이포그래피

| 역할 | 폰트 | 비고 |
|---|---|---|
| 디스플레이 | **Hostero** (`uploads/HosteroRegular-…ttf`) | 거대 로고/숫자("ASKU", 카운터) |
| 헤딩 | **Archivo** 800 | 섹션 제목, 타이트 자간 `-0.02em` |
| 본문 | **Archivo** 400/600 | 15–20px, line-height 1.5–1.6 |

- 반응형 크기는 `clamp()` 로. 헤딩은 문장 단위로 줄바꿈(`.line` 블록).

### 2.3 모션 원칙

| 토큰 | 값 | 쓰임 |
|---|---|---|
| 진입 이징 | `cubic-bezier(.16,1,.3,1)` | reveal, rise, 전환 |
| 드로우 이징 | `cubic-bezier(.65,0,.35,1)` | 선/화살표 stroke |
| reveal | opacity+`translateY(40px)`, ~0.95s | 스크롤 진입 |
| rise | `translateY(118%)` → 0, ~1.05s, overflow 클립 | 글자/줄 등장 |
| hover | ~0.35s | 로고·버튼 강조 |

- **원칙**: 스크롤 진입 시 1회 리빌(`IntersectionObserver`, 재생 후 unobserve), 히어로/최초 화면은 로드 즉시 등장.
- **`prefers-reduced-motion: reduce` 반드시 지원** — 모든 리빌/드리프트/마퀴를 정지하고 최종 상태로 노출(랜딩에 이미 구현).

### 2.4 레이아웃

- 가장자리 여백 `--edge: clamp(20px, 5vw, 72px)`, 콘텐츠 최대 폭 ~1200px.
- 참조용 **12칼럼 그리드** 오버레이(랜딩 `data-grid="on"`).
- 브레이크포인트(랜딩 기준): `≤820`(흐름 세로 전환), `≤680`(내비 축약), `≤720`(2열→1열 등).

---

## 3. 페이지별 명세

### 3.1 랜딩 (`/`) — 구현됨

이미 [`frontend/src/index.html`](../../frontend/src/index.html) 에 구현. 섹션: 히어로(거대 "ASKU"·라이즈) → 사용 흐름(등록/질의 스텝) → Why(마우스 반응 그라디언트) → 지원 학교(로고 마퀴) → 만든 사람들 → 푸터. 두 CTA가 `/find`·`/register` 로 연결된다. 새 페이지는 이 파일의 토큰·모션을 **재사용**하고 재구현하지 않는다.

### 3.2 학교 찾기 (`/find`) — 휠 선택

등록된 학교를 **원호(아크) 휠**로 훑어 하나를 고른다.

**휠 인터랙션**

- 화면 우측에 학교명이 **원호를 따라** 세로로 배치된다. 중앙에서 멀어질수록 회색·기울기 증가·투명도 감소.
- **중앙 = 선택 후보**: 수평·검정 텍스트 + 앞에 **빨강 점**(`--color-accent`).
- 회전 입력: **휠 스크롤 / 드래그(터치·포인터) / ↑↓ 키 / 항목 클릭**. 관성 후 가장 가까운 항목에 **스냅**.
- 좌측에는 현재 선택 학교명을 디스플레이(Archivo/Hostero) 대형으로 미러링.
- 목록이 많으면 **카테고리 탭**(초성 `ㄱ·ㄴ·ㄷ…` 또는 지역)과 범위 인디케이터(`01–100`)로 그룹 점프(레퍼런스의 `01–05`, `01–100` 대응).
- 각 항목에 상태 배지: 인덱싱 완료 / 갱신중. 미완 학교는 선택 시 QA에서 진행 상태를 노출.

**확정 → 전환**

- 중앙 항목 확정(클릭/Enter) → 학교명이 축소·이동하며 `/s/{schoolId}` 로 전환. 그래프가 중앙에서 피어나는 진입 모션.

**데이터**: `GET /schools?query=` (빈 쿼리=전체, 페이지네이션). 필요한 필드: `school_id, name, status, updated_at`(+표시용 `entity_count`). §4 참조.

**접근성**: 휠은 시각 장치일 뿐, 그 아래에 실제 `listbox`(항목=`option`)를 두고 키보드·스크린리더로 동일 선택이 가능해야 한다.

### 3.3 QA (`/s/{schoolId}`) — 네트워크 그래프 + 질문바

학교의 지식그래프를 **인터랙티브 네트워크**로 보여주고, 하단 질문바로 근거 있는 답을 받는다.

**그래프 캔버스**

- 노드=엔티티(`type` 별 색/아이콘), 엣지=관계(`relation` 라벨). force 레이아웃, **줌·팬·노드 드래그**.
- 초기에는 과밀 방지를 위해 **차수(degree) 상위 코어 서브그래프**만 로드하고, 노드 선택 시 이웃을 **지연 확장**(RAG의 1-hop 확장과 정렬, [`07_graph-rag-engine.md`](07_graph-rag-engine.md)).
- 노드 hover: 라벨·연결 강조. 검색/필터로 타입별 표시 토글.

**노드 선택 → 정보 패널**

- 사이드/시트 패널에 엔티티 상세: `type`, `name`, `attributes`(마감일·금액 등), **근거 문서 링크**(`sources`), 이웃 관계 목록.
- "근거 문서"는 `source_url` 로 원문 새 탭. 근거 없는 노드는 만들지 않는다([`10_data-model.md`](10_data-model.md) §3).

**하단 질문바 → 답변**

- 항상 하단 고정. 질문 입력 → `POST /schools/{id}/query` → `{answer, sources}` 표시.
- 답변 카드에 **근거 `sources`**(제목·URL)를 항상 노출. 근거 없으면 "정보를 찾지 못했습니다"를 그대로 보여준다(환각 방지, [`07_graph-rag-engine.md`](07_graph-rag-engine.md) §3).
- 응답이 사용한 엔티티(`entity_ids`, §4 확장)를 받으면 **그래프에서 해당 노드를 하이라이트**해 답과 그래프를 연결한다.
- 미완 인덱싱 학교면 질문바 위에 진행 상태 배너(§3.4 status 재사용).

### 3.4 학교 등록 (`/register`) — URL 입력 → 로딩 → QA

**입력**

- 화면 **정중앙에 URL 입력칸** 하나가 주인공(공지·학사 기준 URL). 학교명은 자동 추론하되 보조 입력 허용.
- 그 아래 **문서 첨부(선택)** — 수강편람 PDF·학칙 HWP 처럼 크롤링으로 닿지 않는 문서를 함께 올린다
  (`.pdf`·`.hwp`·`.hwpx`·`.txt`·`.md`, 파일당 50MB. [`01_backend-api.md`](01_backend-api.md) §2.4-1).
  지원하지 않는 파일은 올리기 전에 사유와 함께 목록에서 걸러낸다.
- 제출 → `POST /schools` → `{school_id}` 수신 → 첨부가 있으면 `POST /schools/{id}/attachments` → 로딩 화면으로.

**로딩 (이목을 끄는 연출)**

- 파이프라인 단계별 **진행도**를 표시하고, 단계가 바뀔 때마다 **아이콘이 화려하게 변환**(모프/스와프 애니메이션)되어 시선을 끈다.
- 단계(백엔드 파이프라인과 1:1): **크롤링 → 추출 → 그래프 빌드 → 저장/인덱싱** ([`03_crawler.md`](03_crawler.md)·[`04_extractor.md`](04_extractor.md)·[`05_graph-builder.md`](05_graph-builder.md)·[`06_storage.md`](06_storage.md)).
- 전체 진행률(`progress` 0–1)과 단계별 카운트(`pages/chunks/entities/edges`)를 함께 표기. 노드/엣지가 실시간으로 늘며 그래프가 "자라는" 미니 프리뷰로 몰입감을 준다.
- 데이터: **SSE** `GET /schools/{id}/status`(권장) 또는 폴링. `reduce` 모션 시 아이콘 변환은 정적 단계 표시로 대체.
- 첨부를 올렸으면 `GET /schools/{id}/attachments` 도 함께 폴링해 파일별 상태(`pending`→`indexing`→`ready`/`failed`)를
  단계 목록 아래에 따로 보인다. 첨부 색인은 크롤 파이프라인과 별개로 돌아 4단계에 끼워 넣을 수 없다.

**완료 → 이동**

- `stage:"done"` 이면 완성 그래프가 잠깐 피어나는 모션 후 `/s/{schoolId}` 로 이동. `failed` 면 사유·재시도.
- 단, `failed` 라도 **색인이 끝난 첨부가 있으면** 완료로 끝낸다 — 백엔드가 그 학교의 질의를 열어주기 때문이다
  ([`01_backend-api.md`](01_backend-api.md) §2.4). 완료 화면은 크롤 통계 대신 첨부 색인 결과를 보인다.

---

## 4. 백엔드 인터페이스 (프론트가 소비)

프론트가 필요로 하는 계약. **API 문서([`01_backend-api.md`](01_backend-api.md))·PLAN §11과 같은 커밋에서 맞춘다.** 표의 ✚ 는 현 초안에 없어 **추가가 필요한 항목**.

| 메서드 | 엔드포인트 | 화면 | 비고 |
|---|---|---|---|
| GET | `/schools?query=` | 찾기(휠) | 빈 쿼리=전체, 페이지네이션 |
| GET | `/schools/{id}` | QA 헤더 | 이름·카운트·갱신시각 |
| GET ✚ | `/schools/{id}/graph` | QA | 코어 서브그래프(노드·엣지) |
| GET ✚ | `/schools/{id}/entities/{eid}` | QA 패널 | 노드 상세·이웃·근거 |
| POST | `/schools/{id}/query` | 질문바 | 답변+근거(+`entity_ids` 확장) |
| POST | `/schools` | 등록 | 등록 시작, `school_id` 반환 |
| POST | `/schools/{id}/attachments` | 등록 | 문서 첨부(multipart, 필드명 `files`) — 선택 |
| GET | `/schools/{id}/attachments` | 등록 로딩 | 첨부별 색인 상태 |
| GET | `/schools/{id}/status` | 등록 로딩 | 단계·진행도 (SSE 권장) |

**그래프 payload** (`/graph`, `/entities/{eid}` 로 확장 로드):

```json
{
  "nodes": [
    { "id": "e_123", "type": "장학금", "name": "국가장학금", "degree": 5, "doc_count": 3 }
  ],
  "edges": [
    { "source": "e_123", "target": "e_45", "relation": "담당" }
  ]
}
```

**노드 상세** (`/entities/{eid}`):

```json
{
  "id": "e_123", "type": "장학금", "name": "국가장학금",
  "attributes": { "마감일": "3/15", "금액": "300만원" },
  "sources": [ { "title": "2026 교내 장학금 안내", "url": "https://…" } ],
  "neighbors": [ { "id": "e_45", "name": "학생지원팀", "relation": "담당" } ]
}
```

**질의 응답** (`/query`, [`07_graph-rag-engine.md`](07_graph-rag-engine.md) §4 기반, `entity_ids` 는 하이라이트용 확장 제안):

```json
{
  "answer": "국가장학금 마감일은 3월 15일입니다.",
  "sources": [ { "title": "2026 교내 장학금 안내", "url": "https://…" } ],
  "entity_ids": ["e_123", "e_45"]
}
```

**등록 상태** (`/status`, 로딩 화면):

```json
{
  "school_id": "…",
  "stage": "crawling|extracting|building|indexing|done|ready|partial_failed|failed",
  "progress": 0.42,
  "detail": { "pages": 128, "chunks": 540, "entities": 210, "edges": 180 },
  "message": "공지 페이지 순회 중…"
}
```

> `stage`가 `"done"` 또는 `"ready"` 또는 `"partial_failed"` 일 경우 인덱싱 완료 상태로 처리하여 화면 전환을 수행한다.

> 노드/엣지 필드 정의의 원본은 [`10_data-model.md`](10_data-model.md). 프론트는 그 부분집합만 사용한다. 모든 조회는 `school_id` 로 격리된다(학교 간 참조 없음).

---

## 5. 공유 컴포넌트

| 컴포넌트 | 쓰이는 곳 |
|---|---|
| `NavBar`(테마 반전) | 전 페이지 |
| `RevealOnScroll` / `RiseText` | 랜딩·전환 |
| `SchoolWheel`(+숨은 `listbox`) | 찾기 |
| `GraphCanvas` / `NodeInspector` | QA·등록 프리뷰 |
| `QuestionBar` / `AnswerCard`(근거 칩) | QA |
| `UrlField` / `PipelineLoader`(단계 아이콘·진행도) | 등록 |

---

## 6. 접근성 · 성능 · 상태

- **모션 안전**: 모든 애니메이션은 `prefers-reduced-motion` 대체 경로를 가진다.
- **키보드/SR**: 휠·그래프는 시각 표현이고, 그 아래 시맨틱 대체(`listbox`, 노드 목록)로 조작 가능해야 한다.
- **그래프 성능**: 코어 서브그래프 + 지연 확장. 대형 그래프는 Canvas/WebGL 렌더러 사용.
- **에러/로딩**: 등록 실패·질의 무근거·인덱싱 미완은 각각 명시적 상태 UI로 노출(숨기지 않는다).
- **문서 동기화 원칙**: 위 인터페이스가 바뀌면 이 문서와 [`01_backend-api.md`](01_backend-api.md)를 같은 커밋에서 고친다([`00_BASICS/code-convention.md`](../00_BASICS/code-convention.md)).
