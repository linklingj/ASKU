# RAG 엔진 (그래프 → 문서 2단 검색)

질문을 받아 **① 그래프 RAG → ② 문서 RAG(PDF) → ③ 실패** 순으로 근거를 찾아
LLM으로 답변을 생성한다. 그래프 RAG는 벡터 검색 + 엔티티 1-hop 그래프 확장으로
크롤링된 웹 문서에서 답하고, 근거가 없으면 문서 RAG가 PDF 청크에서 벡터 top-k만으로
다시 찾는다(그래프 확장 없음). 둘 다 근거가 없으면 정보 부재로 처리한다.

PDF는 사용자가 따로 업로드하는 게 아니라, **크롤링 중 발견한 첨부파일**에서 자동으로
수집된다: Crawler가 공지 상세에서 `.pdf` 링크를 찾으면 `fetch_pdf_attachments()`로
바이트를 내려받고([`03_crawler.md`](03_crawler.md)), Backend API의 인덱싱 파이프라인이
그 바이트를 `PdfIngestor`에 넘겨 페이지별로 청킹·임베딩해 `documents(source_type='pdf')`로
저장한다. 즉 PDF 수집은 `POST /schools`(최초 등록)·`POST /schools/{id}/recrawl`(재크롤링)과
같은 크롤링 파이프라인 한 번에 웹 문서와 함께 처리된다. 저장되는 `source_url`은 첨부의
실제 링크라, 근거 인용을 그대로 열어볼 수 있고 재크롤 미관측 판정에도 그대로 쓰인다.

> **결정 (PLAN 대비 축소, #29로 2단 검색 추가)**
> - 그래프 RAG 검색 전략은 **hybrid 단일**: `벡터 top-k(웹 문서)` + `질문 엔티티의 1-hop 이웃`.
>   Local/Global 라우팅, 커뮤니티 요약(Global search)은 **하지 않는다**. 향후 과제로만 둔다.
> - 문서 RAG는 **벡터 top-k만**으로 동작한다(크롤링 중 수집한 PDF 첨부, 그래프 확장 없음).
> - **검색 풀을 분리**한다: `documents.source_type`이 `'web'`이면 그래프 RAG,
>   `'pdf'`면 문서 RAG만 본다. 분리하지 않으면 문서 RAG가 그래프 RAG와 같은 걸
>   다시 검색하는 셈이라 fallback이 무의미해진다.
> - PDF 청크는 **엔티티·관계 추출을 하지 않는다**(그래프에 반영 안 됨). 수강편람류
>   대용량 문서에 LLM 추출을 돌리는 비용을 피하고, "문서 RAG=벡터 전용"이라는
>   설계를 코드로 강제한다.
> - 근거: 각 단계는 **근거 청크가 없으면 답변을 보류**한다(환각 방지). 두 단계 모두
>   보류하면 최종 실패로 처리한다.
>
> 이유: 대학 공지 데이터는 대부분 원자적(공지 1건=1정보)이라 커뮤니티 요약 이득이 작고,
> 관계 질문은 1-hop 확장으로 대부분 커버된다. PDF(수강편람 등)는 표·조항 위주라
> 그래프 엔티티화 이득이 낮고, 벡터 검색만으로도 근거 인용은 충분하다.

## 1. 2단 검색 흐름

```
질문
 │
 ├─ ① 그래프 RAG (GraphRAG)
 │    ├─ embed(question) → q_vec
 │    ├─ vector_search(school_id, q_vec, k, source_type="web")  → 웹 청크 top-k
 │    ├─ min_similarity 미달 → source_type=None (보류, ②로 진행)
 │    ├─ extract(question).entities → norm_key 매칭 → neighbors(hops=1)
 │    ├─ 컨텍스트 조립(청크 + 이웃 관계 문장) → generate → source_type="graph"
 │    └─ 반환
 │
 ├─ ② 문서 RAG (DocumentRAG) — ①이 보류했을 때만 호출
 │    ├─ embed(question) → q_vec (①과 동일 임베더, 같은 질문)
 │    ├─ vector_search(school_id, q_vec, k, source_type="pdf")  → PDF 청크 top-k
 │    ├─ min_similarity 미달 → source_type=None (보류, ③으로 진행)
 │    ├─ 컨텍스트 조립(청크만, 그래프 확장 없음) → generate → source_type="document"
 │    └─ 반환
 │
 └─ ③ 실패 — ①·②가 모두 보류하면 NO_EVIDENCE_ANSWER, sources=[], source_type=None
```

`HybridRAG`가 이 오케스트레이션을 담당하고, API 계층은 `HybridRAG.answer()`만
호출한다. 모든 저장소 접근은 [`06_storage.md`](06_storage.md) 인터페이스,
LLM 호출은 [`08_llm-provider.md`](08_llm-provider.md)를 쓴다.

질문 엔티티 매핑(그래프 RAG 전용)은 저장소의
[`entities_by_norm_keys(school_id, norm_keys)`](06_storage.md)로 `norm_key` → 엔티티를
해소한 뒤 그 `entity_id`로 `neighbors`를 호출한다. `norm_key` 계산은 그래프 빌더의
`normalize_entity_key`를 재사용해 저장된 엔티티 키와 일치시킨다.

## 2. 컨텍스트 조립

**그래프 RAG**
- 벡터 top-k 청크(원문 근거, `source_type="web"`)를 뼈대로 삼되, **유사도
  `min_similarity` 이상인 청크만** 남긴다.
- 1-hop 이웃은 `"A —관계→ B"` 형태의 짧은 사실 문장으로 직렬화해 덧붙인다.
- 각 조각에 `source_url`을 매단다. 이웃 문장의 근거 URL은 엣지의 `source_doc_ids`를
  `get_documents`로 해소해 얻고, top-k에 없던 문서는 이때만 추가로 조회한다.
- 답변 `sources`는 URL 기준 중복을 제거하고 벡터 top-k 순서를 우선한다.

**문서 RAG**
- 벡터 top-k 청크(`source_type="pdf"`)만 쓴다. 그래프 확장·이웃 문장 없음.
- 컨텍스트 안의 각 청크 인용 표시는 `제목 - N페이지` 형태(`Document.page`가 있으면).
  페이지가 없으면 제목만.
- 답변 `sources`는 **문서(URL)당 한 줄로 모으되 근거로 쓴 페이지를 모두 병합**한다
  (예: `"수강편람.pdf - 3, 15페이지"`). URL 기준으로만 중복을 제거하면 같은 PDF의
  뒤쪽 페이지 인용이 통째로 사라지기 때문이다. 문서 순서는 벡터 top-k 순서, 페이지
  번호는 읽기 순서로 정렬한다.
- `Source.url`은 첨부파일의 실제 링크라 프론트엔드에서 그대로 열 수 있다.

모든 조회는 `school_id`로 격리한다.

## 3. 근거 · 환각 방지

- 프롬프트에 **"제공된 컨텍스트에만 근거해 답하라"**를 명시(제약은 엔진 책임, 08 §1).
  두 엔진이 같은 지시문(`_ANSWER_INSTRUCTION`)을 공유한다.
- 각 단계에서 유사도 `min_similarity` 이상인 청크가 하나도 없으면 **LLM 생성 없이**
  보류하고 `RagAnswer(source_type=None)`를 반환한다(임계값은 생성자 인자로 튜닝,
  기본 0.3). `HybridRAG`는 이 신호로 다음 단계로 넘어가거나 최종 실패를 반환한다.
- 두 단계 모두 보류하면 `"해당 정보를 찾지 못했습니다"`(`NO_EVIDENCE_ANSWER`)를
  반환한다.
- 답변과 함께 근거 `sources`(제목·URL)를 항상 반환. 보류 시 `sources`는 빈 목록.

## 4. 공개 인터페이스

```
GraphRAG(storage, embedder, extractor, generator, *, top_k=5, min_similarity=0.3)
GraphRAG.answer(school_id, question) -> RagAnswer   # source_type: "graph" | None

DocumentRAG(storage, embedder, generator, *, top_k=5, min_similarity=0.3)
DocumentRAG.answer(school_id, question) -> RagAnswer  # source_type: "document" | None

HybridRAG(graph_rag: GraphRAG, document_rag: DocumentRAG)
HybridRAG.answer(school_id, question) -> RagAnswer   # source_type: "graph" | "document" | None
```

`RagAnswer`(`schemas.py`)는 아래 JSON 형태로 직렬화된다.

```
{
  "answer": str,
  "sources": [ { "title": str|None, "url": str } ],
  "entity_ids": [str],
  "source_type": "graph" | "document" | None
}
```

`source_type`은 어느 단계가 답했는지 나타낸다. `None`은 두 단계 모두 근거를 찾지
못한 실패 응답이다(§3). `entity_ids`는 그래프 RAG가 컨텍스트 확장에 쓴 엔티티
ID이며, 문서 RAG 응답에는 항상 빈 목록이다.

- 저장소·LLM은 구현이 아니라 인터페이스로 주입한다(06_storage.md · 08_llm-provider.md).
  두 엔진 모두 `RagStorage` 구조적 계약에만 의존해 pgvector 등 저장소 구현과 분리된다.
- API 계층([`01_backend-api.md`](01_backend-api.md))의 `POST /schools/{id}/query`가
  `HybridRAG.answer`를 호출하고 `RagAnswer`를 응답 본문으로 반환한다.

## 5. 향후 과제 (지금은 구현 안 함)

- Global search(커뮤니티 탐지 + 요약)로 "장학금 종류 전체" 같은 포괄 질문 강화.
- multi-hop 순회, 검색 결과 재랭킹, 답변 품질 평가 지표.
- PDF 청크의 엔티티·관계 추출(그래프 반영) — 현재는 벡터 전용으로 의도적으로 범위 밖.
- HWP/DOC 등 PDF 이외 첨부파일의 자동 수집(현재는 PDF만 다운로드, [`03_crawler.md`](03_crawler.md)).
