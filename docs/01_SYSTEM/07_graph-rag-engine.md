# Graph RAG 엔진

질문을 받아 **벡터 검색 + 엔티티 1-hop 그래프 확장**으로 컨텍스트를 만들고,
LLM으로 근거 포함 답변을 생성한다.

> **결정 (PLAN 대비 축소)**
> - 검색 전략은 **hybrid 단일**: `벡터 top-k` + `질문 엔티티의 1-hop 이웃`.
> - Local/Global 라우팅, 커뮤니티 요약(Global search)은 **하지 않는다**. 향후 과제로만 둔다.
> - 근거: **근거 청크가 없으면 답변을 보류**한다(환각 방지).
>
> 이유: 대학 공지 데이터는 대부분 원자적(공지 1건=1정보)이라 커뮤니티 요약 이득이 작고,
> 관계 질문은 1-hop 확장으로 대부분 커버된다.

## 1. 질의 흐름

```
질문
 │
 ├─ 1) 임베딩            embed(question) → q_vec
 ├─ 2) 벡터 검색         vector_search(school_id, q_vec, k)      → 청크 top-k
 ├─ 3) 질문 엔티티 추출  extract(question).entities             → 엔티티 후보
 ├─ 4) 엔티티 매칭       norm_key로 그래프 엔티티에 매핑
 ├─ 5) 이웃 확장         neighbors(school_id, matched, hops=1)   → 관계 + 근거 문서
 ├─ 6) 컨텍스트 조립     top-k 청크 + 이웃 관계 문장 + 근거 문서 병합
 ├─ 7) 답변 생성         generate(question, context)
 └─ 8) 인용 부착         컨텍스트의 source_url을 sources로 반환
```

모든 저장소 접근은 [`06_storage.md`](06_storage.md) 인터페이스,
LLM 호출은 [`08_llm-provider.md`](08_llm-provider.md)를 쓴다.

## 2. 컨텍스트 조립

- 벡터 top-k 청크(원문 근거)를 뼈대로 삼는다.
- 1-hop 이웃은 `"A —관계→ B"` 형태의 짧은 사실 문장으로 직렬화해 덧붙인다.
- 각 조각에 `source_url`을 매달아 답변이 인용할 수 있게 한다.
- 모든 조회는 `school_id`로 격리한다.

## 3. 근거 · 환각 방지

- 프롬프트에 **"제공된 컨텍스트에만 근거해 답하라"**를 명시.
- 관련 청크가 비었거나 유사도가 임계 미만이면 답변하지 않고
  `"해당 정보를 찾지 못했습니다"`를 반환.
- 답변과 함께 근거 `sources`(제목·URL)를 항상 반환.

## 4. 공개 인터페이스

```
answer(school_id, question) -> {
  "answer": str,
  "sources": [ { "title": str, "url": str } ]
}
```

API 계층([`01_backend-api.md`](01_backend-api.md))의 `POST /schools/{id}/query`가 이 함수를 호출한다.

## 5. 향후 과제 (지금은 구현 안 함)

- Global search(커뮤니티 탐지 + 요약)로 "장학금 종류 전체" 같은 포괄 질문 강화.
- multi-hop 순회, 검색 결과 재랭킹, 답변 품질 평가 지표.
