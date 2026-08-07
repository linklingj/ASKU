# LLM 추상화 레이어

로컬·API LLM을 **동일한 얇은 인터페이스**로 다룬다. 추출·임베딩·답변 단계가
각각 다른 제공자를 쓸 수 있게 한다.

> **결정**
> - 기본 구성: **추출 = API(Claude/GPT)**, **임베딩 = 로컬 bge-m3**, **답변 = API 또는 중형 로컬**.
> - 인터페이스는 `generate` · `embed` · `extract` **3개만**. 라우팅·폴백·재시도·캐시는 넣지 않는다(YAGNI, 필요할 때 추가).
> - 구현체는 처음엔 실제로 쓰는 것만 만든다. 전환이 필요해질 때 두 번째를 추가.

## 1. 인터페이스

능력별 3개(`app/llm.py`). `LLMProvider`는 이 셋을 아우르는 **개념 이름**일 뿐,
실제 코드는 **능력별 ABC로 분리**한다 — 한 제공자가 셋을 다 구현할 필요가 없기 때문이다
(예: `LocalEmbedder`는 `embed`만, `GeminiProvider`는 `generate`·`extract`만). 호출자는
필요한 능력에만 의존한다.

```
Generator.generate(prompt: str, context: str) -> str
Embedder.embed(text: str) -> list[float]        # 길이 = EMBEDDING_DIM(1024)
Extractor.extract(text: str) -> Extraction       # JSON 강제 출력
```

`extract`의 반환 `Extraction`은 이름 기반 엔티티·관계를 담는 pydantic 모델이다(영속 ID 없음,
`schemas.ExtractedEntity`/`ExtractedRelation` 재사용):

```
Extraction { entities: [ExtractedEntity], relations: [ExtractedRelation] }
```

- `embed`의 차원(1024)은 [`06_storage.md`](06_storage.md) `documents.embedding`과 반드시 일치.
  모델을 바꾸면 스키마 차원과 재임베딩이 함께 따라온다.
- `extract`의 출력 스키마는 [`04_extractor.md`](04_extractor.md)의 화이트리스트를 따른다.
  제공자는 **JSON 형식만 강제**하고, 목록 밖 타입·관계 폐기(화이트리스트 검증)는 추출기(04)가 맡는다.

## 2. 단계별 제공자 지정

설정에서 단계마다 제공자를 고른다. 기본값:

| 단계 | 제공자 | 이유 |
|---|---|---|
| 추출 | API (Gemini) | 구조화 추출 품질이 그래프 전체 품질 결정 |
| 임베딩 | 로컬 bge-m3 | 다국어·한국어 강함, 무료, hybrid 지원[^hybrid] |
| 답변 | API 또는 중형 로컬 | 근거 기반 요약이라 중형이면 충분 |

[^hybrid]: **"hybrid 지원"의 의미** — bge-m3는 한 번의 인코딩으로 dense(밀집)·sparse(어휘)·multi-vector(ColBERT) 세 표현을 낼 수 있다는 뜻이다. 다만 현재 [`06_storage.md`](06_storage.md) 스키마는 dense(`vector(1024)`)만 저장하므로 sparse/ColBERT는 **아직 쓰지 않는 잠재 역량**이다(쓰려면 sparse 컬럼·인덱스 추가 필요). 이 "hybrid"(임베딩 **표현** 수준)는 [`07_graph-rag-engine.md`](07_graph-rag-engine.md)의 "hybrid"(벡터 top-k + 그래프 1-hop, 검색 **전략** 수준)와 다른 층위의 용어다.

## 3. 구현체

`app/llm.py`에 구현된 것(실제로 쓰는 것만):

```
GeminiProvider(Generator, Extractor)   # 답변·추출(API).  키·모델: GEMINI_API_KEY · GEMINI_MODEL
LocalEmbedder(Embedder)                # 임베딩(로컬 bge-m3).  모델: BGE_M3_MODEL(기본 BAAI/bge-m3)
OllamaProvider(Generator)              # 선택: 답변 로컬화.  모델·엔드포인트: OLLAMA_MODEL · OLLAMA_HOST
```

- API 키·모델명·엔드포인트는 환경변수/설정으로 주입. 코드에 하드코딩 금지. SDK는 **지연 import**(인터페이스만 쓰는 모듈에 무거운 의존을 강제하지 않는다).
- 구조화 출력: Gemini는 JSON MIME 응답을 받아 `Extraction`으로 파싱한다(자유형 `attributes` 때문에 스키마 강제 대신 pydantic 검증).
- `OpenAIProvider` 등 대체 제공자는 전환이 필요할 때 같은 인터페이스로 추가한다.

관련: [`04_extractor.md`](04_extractor.md), [`05_graph-builder.md`](05_graph-builder.md), [`07_graph-rag-engine.md`](07_graph-rag-engine.md).
