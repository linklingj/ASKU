# Graph RAG 엔진 기능

`backend/app/rag.py`의 `GraphRAG`는 질문을 받아 **벡터 top-k 검색 + 질문 엔티티
1-hop 그래프 확장**으로 근거 컨텍스트를 만들고, LLM으로 근거 기반 답변을 생성한다.
설계 단일 기준은 [`docs/01_SYSTEM/07_graph-rag-engine.md`](../01_SYSTEM/07_graph-rag-engine.md).

## 공개 인터페이스

```python
GraphRAG(storage, embedder, extractor, generator, *, top_k=5, min_similarity=0.3)
GraphRAG.answer(school_id: int, question: str) -> RagAnswer
```

- `storage`: `RagStorage` 계약(`vector_search`·`entities_by_norm_keys`·`neighbors`·
  `get_documents`)을 만족하는 저장소. 실제 구현은 `app.storage.Storage`.
- `embedder`·`extractor`·`generator`: `app.llm`의 능력별 인터페이스. 단계마다 다른
  제공자를 주입할 수 있다(예: 임베딩 = `LocalEmbedder`, 추출·답변 = `GeminiProvider`).
- `RagAnswer`(`schemas.py`)는 `{ "answer": str, "sources": [ {title, url} ] }`로 직렬화된다.

## 질의 흐름

1. `embed(question)` → 질문 벡터
2. `vector_search(school_id, q, top_k)` → 유사도 `min_similarity` 이상 청크만 유지
3. 근거 청크가 없으면 **생성 없이** `"해당 정보를 찾지 못했습니다"` 반환(환각 방지)
4. `extract(question).entities` → `normalize_entity_key`로 `norm_key` 계산
5. `entities_by_norm_keys` → `neighbors(hops=1)`로 1-hop 이웃 확장
6. top-k 청크 + `"A —관계→ B"` 이웃 문장으로 컨텍스트 조립(각 조각에 출처 URL)
7. `generate(prompt, context)`로 답변, 근거 `sources`를 URL 중복 없이 부착

## 사용 예

```python
from app.rag import GraphRAG
from app.storage import Storage
from app.llm import GeminiProvider, LocalEmbedder

gemini = GeminiProvider()
engine = GraphRAG(Storage.from_env(), LocalEmbedder(), gemini, gemini)
result = engine.answer(school_id=1, question="장학금 신청 기간이 언제야?")
print(result.answer)
for source in result.sources:
    print(source.title, source.url)
```

## 테스트

```bash
PYTHONPATH=backend python -m unittest backend/tests/test_rag.py -v
```

단위 테스트는 저장소·LLM을 가짜로 주입해 pgvector·모델 없이 흐름을 검증한다.

## 범위 (MVP)

- 검색 전략은 hybrid 단일(벡터 top-k + 1-hop). 모든 조회는 `school_id`로 격리한다.
- Global/community 요약, multi-hop 순회, 재랭킹은 **하지 않는다**(향후 과제, 07 §5).
- `min_similarity`(기본 0.3)는 환각 방지 임계값이자 튜닝 노브다. 실제 pgvector·bge-m3
  데이터가 준비되면 조정한다.
