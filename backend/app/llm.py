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


class GeminiProvider(Generator, Extractor):
    """Gemini API 로 답변 생성·구조화 추출을 담당한다(임베딩은 LocalEmbedder 몫).

    api_key·model 은 환경변수/인자로 주입한다(하드코딩 금지): `GEMINI_API_KEY`,
    `GEMINI_MODEL`. SDK(google-genai)는 생성 시에만 지연 import 한다 — 인터페이스만
    쓰는 모듈에 무거운 의존을 강제하지 않기 위해서다.
    """

    # 형식(JSON 모양)만 지시한다. 타입·관계 화이트리스트는 04(추출기)가 소유·검증하므로
    # 여기 프롬프트에 옮겨 담지 않는다(단일 기준 중복 방지).
    _EXTRACT_INSTRUCTION = (
        "다음 대학 공지 본문에서 엔티티(노드)와 관계를 추출해 JSON 으로만 응답하라.\n"
        '형식: {"entities":[{"type":"타입","name":"이름","attributes":{}}],'
        '"relations":[{"source":"엔티티이름","relation":"관계","target":"엔티티이름"}]}\n'
        "source/target 은 엔티티 name 문자열이다. 날짜·금액·인원 등은 관계가 아니라 "
        "해당 엔티티의 attributes 에 담는다. 타입·관계 화이트리스트 검증은 추출기가 "
        "하므로 형식만 지키면 된다."
    )

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
            contents=f"{self._EXTRACT_INSTRUCTION}\n\n본문:\n{text}",
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return Extraction.model_validate_json(resp.text)

