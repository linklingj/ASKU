# LLM 추상화 레이어

로컬·API LLM을 **동일한 얇은 인터페이스**로 다룬다. 추출·임베딩·답변 단계가
각각 다른 제공자를 쓸 수 있게 한다.

> **결정**
> - 기본 구성: **추출 = API(Claude/GPT)**, **임베딩 = 로컬 bge-m3**, **답변 = API 또는 중형 로컬**.
> - 인터페이스는 `generate` · `embed` · `extract` **3개만**. 라우팅·폴백·재시도·캐시는 넣지 않는다(YAGNI, 필요할 때 추가).
> - 구현체는 처음엔 실제로 쓰는 것만 만든다. 전환이 필요해질 때 두 번째를 추가.

## 1. 인터페이스

```
LLMProvider
 ├─ generate(prompt, context) -> answer:str
 ├─ embed(text) -> vector(1024)
 └─ extract(text) -> { entities: [...], relations: [...] }   # JSON 강제 출력
```

- `embed`의 차원(1024)은 [`06_storage.md`](06_storage.md) `documents.embedding`과 반드시 일치.
  모델을 바꾸면 스키마 차원과 재임베딩이 함께 따라온다.
- `extract`의 출력 스키마는 [`04_extractor.md`](04_extractor.md)의 화이트리스트를 따른다.

## 2. 단계별 제공자 지정

설정에서 단계마다 제공자를 고른다. 기본값:

| 단계 | 제공자 | 이유 |
|---|---|---|
| 추출 | API (Claude/GPT) | 구조화 추출 품질이 그래프 전체 품질 결정 |
| 임베딩 | 로컬 bge-m3 | 다국어·한국어 강함, 무료, hybrid 지원 |
| 답변 | API 또는 중형 로컬 | 근거 기반 요약이라 중형이면 충분 |

## 3. 구현체

```
AnthropicProvider / OpenAIProvider   # generate, extract (API)
LocalEmbedder (bge-m3)               # embed (로컬)
OllamaProvider                       # 선택: 답변 로컬화 시
```

- API 키·모델명·엔드포인트는 환경변수/설정으로 주입. 코드에 하드코딩 금지.
- 구조화 출력은 각 API의 JSON/스키마 강제 기능을 사용.

관련: [`04_extractor.md`](04_extractor.md), [`05_graph-builder.md`](05_graph-builder.md), [`07_graph-rag-engine.md`](07_graph-rag-engine.md).
