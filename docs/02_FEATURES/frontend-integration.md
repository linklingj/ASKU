# 프론트엔드 ↔ 백엔드 연동

정적 HTML 프론트엔드(`frontend/src/`)를 백엔드 REST API([`01_SYSTEM/01_backend-api.md`](../01_SYSTEM/01_backend-api.md))에
실제로 연결한다. 합성 샘플 데이터·시뮬레이션을 모두 제거하고, 화면은 API 응답으로만 채운다.

## 실행

```bash
# 백엔드 (Postgres·LLM 키·임베더 필요. 기본 제공자는 OpenAI — 08_llm-provider.md)
cd backend && uvicorn app.api:app --port 8000
# 프론트엔드
cd frontend/src && python3 -m http.server 5500
```

- 백엔드 주소는 `frontend/src/api.js` 가 결정: `?api=<url>` → `localStorage.asku_api` → 기본값 `http://localhost:8000`.
  다른 호스트를 쓰려면 `index.html?api=http://호스트:포트` 처럼 한 번 열면 이후 저장된다.
- CORS 는 백엔드가 `allow_origins=["*"]` 로 열어둠.

## 연결된 흐름

| 페이지 | 호출하는 API | 동작 |
|---|---|---|
| `index.html` | `GET /schools` | "등록된 학교" 섹션을 실제 수·이름으로 채움. 0개거나 백엔드 미연결이면 섹션 숨김 |
| `find.html` | `GET /schools` | 휠·listbox 를 실제 학교로 구성, 노드 수·상태·갱신일 표시, `school_id` 를 QA로 전달 |
| `register.html` | `POST /schools` → `GET /schools/{id}/status` (폴링) | URL 등록 후 실제 진행도/단계로 로딩 표시, 완료 시 `qa.html?school={id}` 로 이동 |
| `qa.html` | `GET /schools/{id}`, `GET /schools/{id}/graph`, `GET /schools/{id}/entities/{eid}`, `POST /schools/{id}/query` 또는 `POST /schools/{id}/retrieve` | 그래프 렌더, 노드 클릭 시 상세(속성·근거문서·이웃), 질문바 답변+근거링크+`entity_ids` 하이라이트 |

- 공통 fetch 헬퍼: `api.js` (`ASKU.get`/`ASKU.post`, 공통 에러 `{error:{code,message}}` 파싱).
- 순수 변환 로직은 노드 자가검증으로 커버: `qa.selfcheck.js`(`buildGraph`/`assignTypeColors`/`parseEid`), `register.selfcheck.js`(`valid`/`normalizeUrl`/`deriveName`/`stageToPhase`), `model.selfcheck.js`(`normalize`/`validate`/`classifyError`/`geminiText`/`ollamaModelNames`).

## 답변 모델 선택 (`model.js`)

질문 답변을 만드는 모델만 사용자가 고른다. 검색·추출·임베딩은 서버 구현 그대로다.

| 선택 | 질문 시 호출 | 답변을 만드는 곳 |
|---|---|---|
| ASKU 기본 | `POST /schools/{id}/query` | ASKU 서버(프로젝트 키) |
| 내 Gemini | `POST /schools/{id}/retrieve` → `generativelanguage.googleapis.com` | 브라우저(사용자 개인 키) |
| 내 PC Ollama | `POST /schools/{id}/retrieve` → `{ollamaHost}/api/generate` | 사용자 PC |

- 설정(제공자·API 키·모델명·Ollama 주소)은 **`sessionStorage`에만** 둔다. 백엔드로 보내지
  않고 `localStorage`에도 쓰지 않는다 — 탭을 닫으면 지워지는 것이 기본값이다.
- `retrieve` 가 준 `instruction`+`context` 를 그대로 모델에 넘기므로, 어느 모델로 답하든
  근거와 지시문이 같다. `source_type` 이 `null` 이면 모델을 부르지 않고 보류 문구를 쓴다.
- Ollama 는 설정창의 "연결 확인"이 `GET {host}/api/tags` 로 설치된 모델 목록을 채운다.
- Gemini 오류는 키 거부(`GEMINI_AUTH`)·한도(`GEMINI_QUOTA`)·모델명(`GEMINI_MODEL_NOT_FOUND`)·
  빈 응답(`GEMINI_EMPTY`)으로 구분한다.

### 배포(HTTPS) 화면에서 내 PC Ollama 쓰기

**HTTPS 페이지에서 `http://localhost:11434` 호출은 막지 않는다.** 크롬 계열 브라우저는
`localhost` 를 신뢰할 수 있는 출처로 보아 허용한다 — 배포 origin(`https://…github.io`)에서
실제 Ollama(0.32.6, `qwen2.5:1.5b`)의 `/api/tags`(단순 요청)와 `/api/generate`(프리플라이트
후 POST)가 200 으로 돌고 답변까지 생성되는 것을 확인했다. 사파리처럼 이 조합을 막는
브라우저가 있으므로, 차단은 사전에 하지 않고 실패했을 때 원인 후보로 안내한다.

사용자가 해야 할 설정은 CORS 허용 하나다. 설정창이 배포 주소를 넣은 명령을 그대로 보여준다.

```bash
OLLAMA_ORIGINS=https://linklingj.github.io ollama serve
```

이 설정 없이 배포 origin에서 부르면 `TypeError: Failed to fetch` 로 끝난다(확인함). 즉
CORS 허용은 선택이 아니라 필수다. Ollama 는 기본 바인딩(`127.0.0.1:11434`)을 유지한다. `OLLAMA_HOST=0.0.0.0` 으로 외부에 열거나
ngrok 같은 터널을 쓰는 방법은 권하지 않는다 — 개인 PC의 모델 서버가 인터넷에 노출된다.

연결 실패는 브라우저가 원인을 알려주지 않는다(미실행·CORS 거절·브라우저 차단이 모두 같은
`TypeError`). 그래서 `OLLAMA_UNREACHABLE` 하나로 묶고 확인할 것을 순서대로 나열한다 —
실행 여부, `OLLAMA_ORIGINS`, 주소, 그리고 https 페이지일 때만 브라우저 차단 가능성.
나머지는 코드로 갈린다: 모델 없음(`OLLAMA_MODEL_NOT_FOUND`), 목록은 비었지만 연결은 됨,
설정 미완성(`NO_MODEL`·`NO_HOST`), 응답 오류(`OLLAMA_ERROR`).

## 디자인 목업 → 백엔드 필드 매핑에서 조정한 것

목업에 있던 값 중 백엔드가 제공하지 않는 것은 **삭제하거나 실존 필드로 대체**했다(허위 표기 금지).

| 목업 요소 | 조정 |
|---|---|
| 학교 영문명(`en`) | 백엔드에 필드 없음 → 상태(status) 표기로 대체 |
| 학과 수(`dept`) | 집계 API 없음 → 노드/상태/갱신일로 대체 |
| 찾기 페이지의 "340K" 등 고정 통계 | `entity_count` 등 실제 값으로 대체 |
| 랜딩 "국내 30개+ 대학" + 세종대 로고 마퀴 | 삭제, `GET /schools` 실데이터 마퀴로 교체 |
| 등록 6단계 시뮬레이션 타이머 | 삭제, `/status` 폴링(파이프라인 4단계)으로 교체 |
| QA 고정 5타입(학과/강의/교수/문서/공지) | 삭제, 백엔드 임의 타입에 색을 동적 배정 |

## 현재 로직으로 불가능/미구현 (백엔드 확장 필요)

- **SSE 진행 스트림**: 백엔드는 폴링용 `GET /status` 만 제공(설계 문서의 SSE 권장은 미구현). → 프론트는 1.2s 폴링으로 대체.
- **등록 로딩의 실시간 그래프 성장 프리뷰**: 진행 중 노드/엣지를 스트리밍하는 채널이 없음(`/status` 는 누적 카운트만). → 수치 진행도·단계 표시까지만.
- **학교명 자동 추론**: 크롤러가 학교명을 반환하지 않음. → 등록 시 호스트에서 naive 추론(`deriveName`). 정식 학교명 입력/추론은 백엔드 몫.
- **그래프 지연 확장(코어 100 밖 1-hop lazy-expand)**: `/graph` 는 상위 100 코어만, `/entities/{eid}` 이웃은 `id·name·relation` 만 주고 `type·degree` 가 없어 캔버스에 새 노드로 이어붙이기 어려움. → 이웃은 패널 칩으로 표시하고, **이미 로드된 그래프 안에 있으면** 선택·하이라이트. 캔버스로의 점진 확장은 미구현.
- **`/schools` 페이지네이션·검색어**: 백엔드가 전체를 반환(offset/limit 없음). → 프론트도 전량 로드. 학교 수가 많아지면 서버 페이지네이션 필요.
- **찾기 휠의 상태 배지(인덱싱 완료/갱신중)**: 별도 배지 UI 대신 상태 텍스트로 표기.

> 위 확장이 생기면 이 문서와 [`01_SYSTEM/02_frontend.md`](../01_SYSTEM/02_frontend.md)·[`01_SYSTEM/01_backend-api.md`](../01_SYSTEM/01_backend-api.md)를 같은 커밋에서 갱신한다.
