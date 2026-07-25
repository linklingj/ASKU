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


class Generator(abc.ABC):
    """답변 생성 능력. 근거 컨텍스트에 기반한 답변 문자열을 낸다."""

    @abc.abstractmethod
    def generate(self, prompt: str, context: str) -> str:
        """prompt(질문·지시)와 context(근거 청크·관계 문장)로 답변을 생성한다.

        '컨텍스트에만 근거해 답하라'는 제약과 근거 부족 시 보류는 호출자
        (07_graph-rag-engine)의 책임이고, 여기서는 str in/out 계약만 정의한다.
        """
