# 프론트엔드 ↔ 백엔드 연동

정적 HTML 프론트엔드(`frontend/src/`)를 백엔드 REST API([`01_SYSTEM/01_backend-api.md`](../01_SYSTEM/01_backend-api.md))에
실제로 연결한다. 합성 샘플 데이터·시뮬레이션을 모두 제거하고, 화면은 API 응답으로만 채운다.

## 실행

```bash
# 백엔드 (Postgres·GEMINI_API_KEY·임베더 필요)
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
| `register.html` | `POST /schools` → `POST /schools/{id}/attachments` → `GET /schools/{id}/status` · `GET /schools/{id}/attachments` (폴링) | URL 등록 + 문서 첨부(선택) 후 실제 진행도/단계로 로딩 표시, 완료 시 `qa.html?school={id}` 로 이동 |
| `qa.html` | `GET /schools/{id}`, `GET /schools/{id}/graph`, `GET /schools/{id}/entities/{eid}`, `POST /schools/{id}/query` | 그래프 렌더, 노드 클릭 시 상세(속성·근거문서·이웃), 질문바 답변+근거링크+`entity_ids` 하이라이트 |

- 공통 fetch 헬퍼: `api.js` (`ASKU.get`/`ASKU.post`/`ASKU.upload`, 공통 에러 `{error:{code,message,details}}` 파싱).
- 순수 변환 로직은 노드 자가검증으로 커버: `qa.selfcheck.js`(`buildGraph`/`assignTypeColors`/`parseEid`/`sourceChip`), `register.selfcheck.js`(`valid`/`normalizeUrl`/`deriveName`/`stageToPhase`/`fileError`/`attachSummary`).

### 학교 등록 시 문서 첨부 ([#41](https://github.com/linklingj/ASKU/issues/41))

크롤링으로는 닿지 않는 수강편람 PDF·학칙 HWP 를 등록 화면에서 함께 올린다
([`01_SYSTEM/01_backend-api.md`](../01_SYSTEM/01_backend-api.md) §2.4-1).

- **선택 입력이다.** URL 만으로 등록하던 흐름은 그대로다(요청도 예전과 같이 `POST /schools` 하나뿐).
- 파일 선택·드래그 앤 드롭 모두 받고, `fileError()` 가 **백엔드와 같은 규칙**(`.pdf`·`.hwp`·`.hwpx`·`.txt`·`.md`,
  파일당 50MB, 빈 파일 거부)으로 먼저 걸러 올리지 않는다. 걸러진 파일도 사유와 함께 목록에 남긴다.
- 첨부는 학교가 생긴 **뒤에** 올린다(`school_id` 가 있어야 붙는다). 업로드가 실패해도 크롤은 이미
  시작됐으므로 등록을 실패로 되돌리지 않고 사유만 남긴다.
- 색인은 크롤 파이프라인과 **별개로** 백그라운드에서 돈다. 그래서 로딩 화면은 `/status` 와
  `/attachments` 를 함께 폴링하고, 첨부가 `pending`·`indexing` 인 동안에는 진행률을 100% 로
  올리지 않는다(끝나지 않았는데 끝난 것처럼 보이면 안 된다).
- **크롤이 실패해도 색인된 첨부가 있으면 완료로 끝낸다.** 백엔드가 그 경우 질의를 열어주기 때문이다
  (`POST /query` 는 `count_ready_attachments > 0` 이면 `SCHOOL_NOT_READY` 를 내지 않는다).
  이때는 "지식 그래프 생성" 문구·크롤 통계를 감추고 문서로 답할 수 있다는 안내로 바꾼다.
- QA 화면의 근거 표시: 첨부 근거의 `url` 은 `attachment://{id}` 합성 URI 라 링크로 걸면 열리지
  않는다 → `sourceChip()` 이 웹 출처만 새 탭 링크로 만들고 첨부는 파일명·페이지 칩으로 보여준다.

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
