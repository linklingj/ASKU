"""LLM 추상화 레이어 — 추출·임베딩·답변 단계를 서로 다른 제공자로 교체할 수 있게 하는
얇은 인터페이스. 설계 단일 기준은 docs/01_SYSTEM/08_llm-provider.md.

능력(generate·embed·extract)을 인터페이스로 나눈다. 한 제공자가 셋을 모두 가질 필요는
없다 — 단계마다 다른 제공자를 쓰기 때문이다(예: LocalEmbedder 는 embed 만,
GeminiProvider 는 generate·extract 만). 호출자는 필요한 능력에만 의존한다.

라우팅·폴백·재시도·캐시는 넣지 않는다(YAGNI). 단계별 제공자 선택은 설정으로 호출자가
하고, 여기서는 입출력 계약만 정의한다. API 키·모델명·엔드포인트는 구현체가 환경변수/
설정으로 주입받으며 코드에 하드코딩하지 않는다.
"""

from __future__ import annotations

import abc
import os

from pydantic import BaseModel, Field

from app.models import EMBEDDING_DIM
from app.prompts import EXTRACT_JSON_INSTRUCTION
from app.schemas import ExtractedEntity, ExtractedRelation


class Generator(abc.ABC):
    """답변 생성 능력. 근거 컨텍스트에 기반한 답변 문자열을 낸다."""

    @abc.abstractmethod
    def generate(self, prompt: str, context: str) -> str:
        """prompt(질문·지시)와 context(근거 청크·관계 문장)로 답변을 생성한다.

        '컨텍스트에만 근거해 답하라'는 제약과 근거 부족 시 보류는 호출자의 책임이고, 여기서는 str in/out 계약만 정의한다.
        """


class Embedder(abc.ABC):
    """임베딩 능력. 텍스트를 dense 벡터로 만든다."""

    @abc.abstractmethod
    def embed(self, text: str) -> list[float]:
        """text 를 dense 벡터로 변환한다.

        차원은 저장소 documents.embedding(vector(1024)) 및 models.EMBEDDING_DIM 과 반드시 일치한다(06_storage.md).
        bge-m3 의 sparse·ColBERT 표현은 현재 스키마가 저장하지 않는다(dense 전용).
        """


class Extraction(BaseModel):
    """extract() 결과 = 화이트리스트를 따르는 이름 기반 엔티티·관계.

    영속 ID 는 없다(그래프 빌더가 부여). 그래프 빌더 입력(ExtractedChunk)의
    entities·relations 부분과 같은 형태다(schemas.py).
    """

    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)


class Extractor(abc.ABC):
    """구조화 추출 능력. 텍스트에서 엔티티·관계를 뽑는다."""

    @abc.abstractmethod
    def extract(self, text: str) -> Extraction:
        """text 에서 엔티티·관계를 추출한다. 출력은 04_extractor.md 화이트리스트를
        따르고, 제공자는 각 API 의 JSON 강제 출력 기능으로 스키마를 강제한다.
        목록 밖 타입·관계 폐기(검증)는 추출기(04)가 맡는다.
        """


class SpecDrafter(abc.ABC):
    """게시판 HTML 에서 수집 규격 초안을 만드는 능력.

    답변 생성·엔티티 추출과 목적이 다르다. 학교를 등록할 때 한 번만 부르고,
    결과는 사람이 쓴 규격과 같은 자리(`adapter_specs`)에 들어간다.
    """

    @abc.abstractmethod
    def draft_spec(self, prompt: str) -> str:
        """규격 JSON 문자열을 만든다. 스키마 검증과 채점은 호출자가 한다."""


class GeminiProvider(Generator, Extractor, SpecDrafter):
    """Gemini API 로 답변 생성·구조화 추출을 담당한다(임베딩은 LocalEmbedder 몫).

    api_key·model 은 환경변수/인자로 주입한다(하드코딩 금지): `GEMINI_API_KEY`,
    `GEMINI_MODEL`. SDK(google-genai)는 생성 시에만 지연 import 한다 — 인터페이스만
    쓰는 모듈에 무거운 의존을 강제하지 않기 위해서다.
    """

    # 추출 프롬프트 문안은 app/prompts.py(EXTRACT_JSON_INSTRUCTION)가 소유한다.

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        from google import genai

        key = api_key or os.getenv("GEMINI_API_KEY")
        self._model = model or os.getenv("GEMINI_MODEL")
        if not key or not self._model:
            raise ValueError("GEMINI_API_KEY·GEMINI_MODEL 를 환경변수/인자로 주입해야 한다")
        self._client = genai.Client(api_key=key)

    def generate(self, prompt: str, context: str) -> str:
        resp = self._client.models.generate_content(
            model=self._model,
            contents=f"[컨텍스트]\n{context}\n\n[요청]\n{prompt}",
        )
        return resp.text

    def extract(self, text: str) -> Extraction:
        from google.genai import types

        resp = self._client.models.generate_content(
            model=self._model,
            contents=f"{EXTRACT_JSON_INSTRUCTION}\n\n본문:\n{text}",
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        # 응답이 아예 비는 경우가 있다 — 길이 제한에 걸려 잘리거나, 안전 필터에
        # 막히거나, 한도에 가까울 때다. 그대로 스키마 검증에 넘기면 '모델이 형식을
        # 어겼다'로 분류돼 재시도 없이 끝난다. 형식 오류와 달리 이쪽은 다시 부르면
        # 되는 일시적 실패다.
        if not resp.text:
            reason = getattr(resp.candidates[0], "finish_reason", None) if resp.candidates else None
            raise RuntimeError(f"빈 응답 (finish_reason={reason})")
        return Extraction.model_validate_json(resp.text)

    def draft_spec(self, prompt: str) -> str:
        from google.genai import types

        resp = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return resp.text


class OpenAIProvider(Generator, Extractor, SpecDrafter):
    """OpenAI(GPT) API 로 답변 생성·구조화 추출을 담당한다(임베딩은 LocalEmbedder 몫).

    GeminiProvider 와 같은 3개 인터페이스를 구현하므로 호출부는 동일하다. api_key·model 은
    환경변수/인자로 주입한다(하드코딩 금지): `OPENAI_API_KEY`, `OPENAI_MODEL`. SDK(openai)는
    생성 시에만 지연 import 한다. 구조화 추출은 Chat Completions 의 JSON 모드로 강제한다.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        from openai import OpenAI

        key = api_key or os.getenv("OPENAI_API_KEY")
        self._model = model or os.getenv("OPENAI_MODEL")
        if not key or not self._model:
            raise ValueError("OPENAI_API_KEY·OPENAI_MODEL 를 환경변수/인자로 주입해야 한다")
        self._client = OpenAI(api_key=key)

    def generate(self, prompt: str, context: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": f"[컨텍스트]\n{context}\n\n[요청]\n{prompt}"}],
        )
        return resp.choices[0].message.content or ""

    def extract(self, text: str) -> Extraction:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": f"{EXTRACT_JSON_INSTRUCTION}\n\n본문:\n{text}"}],
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        # 응답이 비는 경우(길이 제한·안전 필터·한도 근접)는 형식 오류가 아니라 재시도 대상이다.
        if not content:
            reason = resp.choices[0].finish_reason if resp.choices else None
            raise RuntimeError(f"빈 응답 (finish_reason={reason})")
        return Extraction.model_validate_json(content)

    def draft_spec(self, prompt: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""


def make_llm_provider() -> GeminiProvider | OpenAIProvider:
    """답변 생성·추출 제공자를 `LLM_PROVIDER`(openai|gemini) 로 고른다. 기본 openai.

    두 제공자 모두 Generator·Extractor·SpecDrafter 를 구현하므로 호출부는 동일하다.
    임베딩은 항상 로컬 bge-m3(LocalEmbedder)라 여기서 다루지 않는다.
    """
    name = os.getenv("LLM_PROVIDER", "openai").lower()
    if name == "gemini":
        return GeminiProvider()
    return OpenAIProvider()


class LocalEmbedder(Embedder):
    """로컬 bge-m3 임베딩 — dense 전용. sparse·ColBERT 표현은 뽑지 않는다(저장소가
    dense `vector(1024)` 만 쓰기 때문, 06_storage.md). 모델명은 `BGE_M3_MODEL` 로
    덮어쓸 수 있다. FlagEmbedding(torch 등)은 생성 시에만 지연 import 한다.
    """

    def __init__(self, *, model: str | None = None, use_fp16: bool = True) -> None:
        from FlagEmbedding import BGEM3FlagModel

        name = model or os.getenv("BGE_M3_MODEL", "BAAI/bge-m3")
        self._model = BGEM3FlagModel(name, use_fp16=use_fp16)

    def embed(self, text: str) -> list[float]:
        out = self._model.encode(
            [text], return_dense=True, return_sparse=False, return_colbert_vecs=False
        )
        vec = out["dense_vecs"][0]
        if len(vec) != EMBEDDING_DIM:  # 저장소 vector(1024) 와 어긋나면 조기 실패
            raise ValueError(f"임베딩 차원 {len(vec)} != EMBEDDING_DIM {EMBEDDING_DIM}")
        return [float(x) for x in vec]


class OllamaProvider(Generator):
    """로컬 Ollama 로 답변을 생성한다(답변 로컬화 옵션, 08 §3). model 은
    `OLLAMA_MODEL`, 엔드포인트는 `OLLAMA_HOST` 로 주입한다. ollama SDK 는 지연 import.
    """

    def __init__(self, *, model: str | None = None, host: str | None = None) -> None:
        import ollama

        self._model = model or os.getenv("OLLAMA_MODEL")
        if not self._model:
            raise ValueError("OLLAMA_MODEL 를 환경변수/인자로 주입해야 한다")
        self._client = ollama.Client(host=host or os.getenv("OLLAMA_HOST"))

    def generate(self, prompt: str, context: str) -> str:
        resp = self._client.generate(
            model=self._model,
            prompt=f"[컨텍스트]\n{context}\n\n[요청]\n{prompt}",
        )
        return resp["response"]

