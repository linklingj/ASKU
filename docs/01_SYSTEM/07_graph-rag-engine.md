# RAG 엔진 (그래프 RAG → 문서 RAG)

질문을 받아 **① 그래프 RAG** 로 답하고, 근거가 없으면 **② 문서 RAG(업로드 첨부)** 로
fallback 하며, 그래도 없으면 **실패**를 알린다. 답변은 언제나 근거를 포함한다.

> **결정 (PLAN 대비 축소)**
> - 검색 전략은 **hybrid 단일**: `벡터 top-k` + `질문 엔티티의 1-hop 이웃`.
> - Local/Global 라우팅, 커뮤니티 요약(Global search)은 **하지 않는다**. 향후 과제로만 둔다.
> - 근거: **근거 청크가 없으면 답변을 보류**한다(환각 방지).
> - 문서 RAG는 **벡터 top-k 전용**이다. 첨부는 그래프에 반영하지 않는다.
>
> 이유: 대학 공지 데이터는 대부분 원자적(공지 1건=1정보)이라 커뮤니티 요약 이득이 작고,
> 관계 질문은 1-hop 확장으로 대부분 커버된다. 수강편람처럼 길고 구조가 옅은 문서는
> 엔티티로 쪼개는 이득보다 원문 청크를 그대로 인용하는 편이 정확하다.

## 0. 2단 검색 (`HybridRAG`)

```
질문
 │
 ├─ ① GraphRAG.answer()      documents(source_type='web') 벡터 top-k + 1-hop 확장
 │     └─ 근거 있음 → 답변(source_type="graph")  ← 여기서 끝
 │
 ├─ ② DocumentRAG.answer()   documents(source_type='attachment') 벡터 top-k (그래프 확장 없음)
 │     └─ 근거 있음 → 답변(source_type="document")  ← 여기서 끝
 │
 └─ ③ 실패                    "해당 정보를 찾지 못했습니다." (sources=[], source_type=null)
```

- 두 단계는 **검색 풀이 다르다**. `documents.source_type` 이 `'web'`(크롤링 공지)과
  `'attachment'`(사용자 업로드 문서)를 가른다([`06_storage.md`](06_storage.md)).
- 단계 성공 여부는 `RagAnswer.source_type` 이 신호한다 — `null` 이면 그 단계가 보류했다는
  뜻이고, `HybridRAG` 는 그때만 다음 단계로 넘어간다.
- 첨부 수집 경로는 크롤러가 아니라 **사용자 업로드**다. 학교 등록 시점에도, 등록 이후에도
  `POST /schools/{id}/attachments` 로 올린다([`01_backend-api.md`](01_backend-api.md)).
  크롤러의 "첨부 본문 파싱 안 함" 범위는 그대로 유지된다([`03_crawler.md`](03_crawler.md)).

## 1. 질의 흐름 (① 그래프 RAG)

```
질문
 │
 ├─ 1) 임베딩            embed(question) → q_vec
 ├─ 2) 벡터 검색         vector_search(school_id, q_vec, k, source_type='web') → 청크 top-k
 ├─ 3) 질문 엔티티 추출  extract(question).entities             → 엔티티 후보
 ├─ 4) 엔티티 매칭       norm_key로 그래프 엔티티에 매핑
 ├─ 5) 이웃 확장         neighbors(school_id, matched, hops=1)   → 관계 + 근거 문서
 ├─ 6) 컨텍스트 조립     top-k 청크 + 이웃 관계 문장 + 근거 문서 병합
 ├─ 7) 답변 생성         generate(question, context)
 └─ 8) 인용 부착         컨텍스트의 source_url을 sources로 반환
```

모든 저장소 접근은 [`06_storage.md`](06_storage.md) 인터페이스,
LLM 호출은 [`08_llm-provider.md`](08_llm-provider.md)를 쓴다.

질문 엔티티 매핑(4단계)은 저장소의
[`entities_by_norm_keys(school_id, norm_keys)`](06_storage.md)로 `norm_key` → 엔티티를
해소한 뒤 그 `entity_id`로 `neighbors`를 호출한다. `norm_key` 계산은 그래프 빌더의
`normalize_entity_key`를 재사용해 저장된 엔티티 키와 일치시킨다.

## 1-2. 질의 흐름 (② 문서 RAG)

```
질문
 │
 ├─ 1) 임베딩       embed(question) → q_vec
 ├─ 2) 벡터 검색    vector_search(school_id, q_vec, k, source_type='attachment')
 ├─ 3) 임계 필터    min_similarity 미만 청크 제외 → 없으면 보류
 ├─ 4) 답변 생성    generate(question, context)
 └─ 5) 인용 부착    "파일명 - N페이지" 형태로 sources 반환
```

- 그래프 확장은 **하지 않는다**. 질문 엔티티 추출·`neighbors` 호출도 없다.
- 첨부에는 원문 링크가 없으므로 인용 URL 은 `attachment://{attachment_id}` 합성 URI 다.
- 같은 첨부의 여러 페이지가 히트하면 한 출처로 묶고 페이지를 모아 적는다
  (`"수강편람.pdf - 3, 15페이지"`). URL 로만 중복 제거하면 뒤쪽 페이지 인용이 사라진다.

## 2. 컨텍스트 조립

- 벡터 top-k 청크(원문 근거)를 뼈대로 삼되, **유사도 `min_similarity` 이상인 청크만** 남긴다.
- 1-hop 이웃은 `"A —관계→ B"` 형태의 짧은 사실 문장으로 직렬화해 덧붙인다.
- 각 조각에 `source_url`을 매단다. 이웃 문장의 근거 URL은 엣지의 `source_doc_ids`를
  `get_documents`로 해소해 얻고, top-k에 없던 문서는 이때만 추가로 조회한다.
- 답변 `sources`는 URL 기준 중복을 제거하고 벡터 top-k 순서를 우선한다.
- 모든 조회는 `school_id`로 격리한다.

## 3. 근거 · 환각 방지

- 프롬프트에 **"제공된 컨텍스트에만 근거해 답하라"**를 명시(제약은 엔진 책임, 08 §1).
- 유사도 `min_similarity` 이상인 청크가 하나도 없으면 **LLM 생성 없이** 보류하고
  `"해당 정보를 찾지 못했습니다"`를 반환한다(임계값은 생성자 인자로 튜닝).
  두 단계 모두 같은 규칙을 따르므로, 어느 단계도 임계를 넘기지 못하면 LLM 을 한 번도
  호출하지 않고 실패를 알린다.
- **임계값은 두 단계가 비대칭이다.** API 계층이 그래프 **0.6** / 문서 **0.3** 으로
  주입한다(`api.py` `_get_rag_engine`). 클래스 기본값은 둘 다 0.3 이지만, 그 값으로는
  주제만 겹치는 공지가 그래프 단계를 통과해 단계가 "성공"해 버리고 정답이 든 첨부까지
  내려가지 않는다. 반대로 문서 단계까지 같이 조이면 fallback 자체가 막히므로 0.3 을
  유지한다. 절대 임계값은 질문마다 점수 분포가 달라 풀 선택 기준으로는 근사치다 —
  근본 해법은 §5 의 통합 검색(양쪽 best score 비교)이다.
- 임계 판정은 `_filter_by_similarity` 한곳에 모여 있고, **컷 전 top-k 점수를
  `logger.info` 로 남긴다**(`rag 검색: 단계=… 임계=… 점수=[…]`). 응답에는 점수가 없어서
  걸러진 청크를 볼 방법이 없으니, 임계값 튜닝 근거는 로그로만 확인한다.
- **점수는 사용자에게 노출하지 않는다.** 코사인 유사도는 질문–청크의 검색 근접도이고
  답변 정확도가 아니다. 캘리브레이션이 없어 확률로 읽을 수 없고, 질문마다 스케일이
  달라 답변 간 비교도 성립하지 않는다. 1-hop 이웃 문장은 벡터 검색을 거치지 않아
  애초에 점수가 없다. 신뢰도 배지는 틀린 답을 검증된 것처럼 보이게 해서 사용자가
  출처 확인을 그만두게 만든다 — 사용자에게 주는 신뢰 신호는 `sources` 와
  `source_type` 이다.
- 답변과 함께 근거 `sources`(제목·URL)를 항상 반환. 보류 시 `sources`는 빈 목록.

## 4. 공개 인터페이스

```
GraphRAG(storage, embedder, extractor, generator, *, top_k=5, min_similarity=0.3)
GraphRAG.answer(school_id, question) -> RagAnswer      # source_type: "graph" | None
# ↑ API 계층은 min_similarity=0.6 으로 주입한다(§3)

DocumentRAG(storage, embedder, generator, *, top_k=5, min_similarity=0.3)
DocumentRAG.answer(school_id, question) -> RagAnswer    # source_type: "document" | None

HybridRAG(graph_rag, document_rag)
HybridRAG.answer(school_id, question) -> RagAnswer      # ① → ② → 실패
```

`RagAnswer`(`schemas.py`)는 아래 JSON 형태로 직렬화된다.

```
{
  "answer": str,
  "sources": [ { "title": str|None, "url": str } ],
  "entity_ids": [str],                        # 그래프 단계에서 쓴 노드 (프론트 하이라이트용)
  "source_type": "graph" | "document" | null  # null = 두 단계 모두 근거 없음
}
```

- 저장소·LLM은 구현이 아니라 인터페이스로 주입한다(06_storage.md · 08_llm-provider.md).
  엔진은 `RagStorage` 구조적 계약에만 의존해 pgvector 등 저장소 구현과 분리된다.
- API 계층([`01_backend-api.md`](01_backend-api.md))의 `POST /schools/{id}/query`는
  **`HybridRAG.answer` 만** 호출하고 `RagAnswer`를 응답 본문으로 반환한다.
  단계 선택·fallback 판단은 API 가 아니라 `HybridRAG` 의 책임이다.

## 5. 향후 과제 (지금은 구현 안 함)

- Global search(커뮤니티 탐지 + 요약)로 "장학금 종류 전체" 같은 포괄 질문 강화.
- multi-hop 순회, 검색 결과 재랭킹, 답변 품질 평가 지표.
- 두 풀을 한 번에 검색해 재랭킹하는 통합 검색. 지금은 그래프 우선 순서를 고정한다 —
  공지가 최신·정확하고, 첨부(편람 등)는 갱신 주기가 길기 때문이다.
- 첨부에서도 엔티티를 추출해 그래프에 합치기(문서 RAG를 그래프 RAG로 승격).
