# LLM Provider (`app/llm.py`)

로컬·API LLM을 **능력별 얇은 인터페이스**로 감싸, 추출·임베딩·답변 단계가 각각 다른
제공자를 쓰게 한다. 설계·결정의 단일 기준은
[`01_SYSTEM/08_llm-provider.md`](../01_SYSTEM/08_llm-provider.md)이고, 이 문서는 **다른 모듈이
쓰는 공개 타입과 사용법·주의 지점**을 정리한다.

## 제공 타입

| 성격 | 타입 | 계약 |
|---|---|---|
| 인터페이스(ABC) | `Generator` | `generate(prompt, context) -> str` |
| | `Embedder` | `embed(text) -> list[float]` (길이 = `EMBEDDING_DIM` 1024) |
| | `Extractor` | `extract(text) -> Extraction` (JSON 강제 출력) |
| 반환형 | `Extraction` | `entities: [ExtractedEntity]` · `relations: [ExtractedRelation]` (이름 기반, 영속 ID 없음) |
| 구현체 | `GeminiProvider(Generator, Extractor)` | 답변·추출(API) |
| | `LocalEmbedder(Embedder)` | 임베딩(로컬 bge-m3, dense 전용) |
| | `OllamaProvider(Generator)` | 선택: 답변 로컬화 |

`LLMProvider`는 이 셋을 아우르는 개념 이름일 뿐, 코드는 능력별 ABC로 나뉜다. 한 제공자가
셋을 다 구현할 필요는 없다 — 호출자는 필요한 능력에만 의존한다.

## 사용법

단계마다 제공자를 골라 주입한다(설정·환경변수). 각 SDK는 해당 제공자를 **생성할 때만**
지연 import 되므로, 인터페이스만 쓰는 모듈에는 `torch`·`google-genai` 등이 필요 없다.

```python
from app.llm import GeminiProvider, LocalEmbedder, OllamaProvider

extractor = GeminiProvider()          # GEMINI_API_KEY · GEMINI_MODEL 주입
embedder = LocalEmbedder()            # BGE_M3_MODEL(기본 BAAI/bge-m3)
answerer = OllamaProvider()           # OLLAMA_MODEL · OLLAMA_HOST

vec = embedder.embed("등록금 납부 안내")          # len(vec) == 1024
out = extractor.extract(chunk_text)              # -> Extraction
answer = answerer.generate(question, context)    # -> str
```

호출자는 능력 타입에만 의존한다: 추출기(04)는 `Extractor`, 그래프 빌더(05)는 `Embedder`,
Graph RAG(07)는 `Generator`+`Embedder`+`Extractor`. 구현체 교체는 인터페이스만 지키면 된다.

## 주입 환경변수

| 제공자 | 변수 | 기본값 |
|---|---|---|
| `GeminiProvider` | `GEMINI_API_KEY` · `GEMINI_MODEL` | 없음(미주입 시 `ValueError`) |
| `LocalEmbedder` | `BGE_M3_MODEL` | `BAAI/bge-m3` |
| `OllamaProvider` | `OLLAMA_MODEL` · `OLLAMA_HOST` | 모델 없음(미주입 시 `ValueError`), 호스트는 ollama 기본 |

키·모델·엔드포인트는 코드에 하드코딩하지 않는다. 인자로도 주입 가능(`GeminiProvider(api_key=..., model=...)`).

## 주의 지점

- **임베딩 차원 = 1024**: `LocalEmbedder.embed`는 dense 벡터만 뽑고(sparse·ColBERT 미사용),
  길이가 `EMBEDDING_DIM`과 다르면 즉시 `ValueError`로 실패한다 → 저장소 `vector(1024)` 오염 방지.
- **화이트리스트는 제공자가 소유하지 않는다**: `extract`는 JSON 형식만 강제하고, 04 타입·관계
  화이트리스트 검증(목록 밖 폐기)은 추출기(04)가 한다. Gemini는 스키마 강제 대신 JSON MIME +
  `Extraction` pydantic 검증을 쓴다(자유형 `attributes` 때문).
- **라우팅·폴백·재시도·캐시 없음**(YAGNI). 단계별 제공자 선택은 호출자가 설정으로 한다.

관련: [`01_SYSTEM/08_llm-provider.md`](../01_SYSTEM/08_llm-provider.md) ·
[`data-model.md`](data-model.md) (`ExtractedEntity`/`ExtractedRelation`).
