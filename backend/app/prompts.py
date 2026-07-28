"""LLM 프롬프트 문안 모음. 파이프라인 각 단계(추출·답변)의 프롬프트를 한곳에서 관리한다.

여기에는 프롬프트 **문안**만 둔다. 프롬프트가 끼워 넣는 **데이터**(엔티티·관계
화이트리스트 타입 집합, 근거 없음 응답값)는 각 소유 모듈(extractor·rag)이 계속 소유하고
빌더 함수 인자로 넘긴다 — 단일 기준 중복을 만들지 않기 위해서다.

- EXTRACT_JSON_INSTRUCTION   → llm.GeminiProvider.extract (형식만 지시)
- whitelist_instruction(...)  → extractor (허용 타입 목록 포함)
- rag_answer_instruction(...) → rag.GraphRAG (근거 기반 답변)
"""

from __future__ import annotations

from collections.abc import Iterable


# 구조화 추출(llm, Gemini) — 형식(JSON 모양)만 지시한다. 타입·관계 화이트리스트는
# 추출기(04)가 소유·검증하므로 여기 프롬프트에 옮겨 담지 않는다(단일 기준 중복 방지).
EXTRACT_JSON_INSTRUCTION = (
    "다음 대학 공지 본문에서 엔티티(노드)와 관계를 추출해 JSON 으로만 응답하라.\n"
    '형식: {"entities":[{"type":"타입","name":"이름","attributes":{}}],'
    '"relations":[{"source":"엔티티이름","relation":"관계","target":"엔티티이름"}]}\n'
    "source/target 은 엔티티 name 문자열이다. 날짜·금액·인원 등은 관계가 아니라 "
    "해당 엔티티의 attributes 에 담는다. 타입·관계 화이트리스트 검증은 추출기가 "
    "하므로 형식만 지키면 된다."
)


def whitelist_instruction(entity_types: Iterable[str], relation_types: Iterable[str]) -> str:
    """허용 타입 목록을 끼워 넣은 추출 지시문을 만든다(extractor).

    타입 집합의 소유·검증은 추출기(04)가 하므로, 여기서는 문안만 만들고 집합은 인자로 받는다.
    """

    return (
        "아래 대학 공지에서 엔티티와 관계를 추출하라. 반드시 다음 타입만 사용하고, "
        "목록 밖 타입·관계는 만들지 마라. 날짜·기간·금액·인원은 관계가 아니라 관련 "
        "엔티티의 attributes에 담아라.\n"
        f"허용 엔티티 타입: {', '.join(sorted(entity_types))}\n"
        f"허용 관계 타입: {', '.join(sorted(relation_types))}\n"
        "관계의 source와 target은 entities에 포함한 정확한 name 문자열을 사용하라. "
        "관계 방향은 의미의 주체에서 대상으로 쓴다. 예: 공지→부서·기관/담당자=게시, "
        "공지→장학금·프로그램·채용·행사·학사일정·규정=안내, "
        "공지→주제·카테고리=분류, 공지·프로그램·장학금→대상·자격=대상이다. "
        "소속 관계는 본문이 조직의 상하 관계를 명시할 때만 만들고, 표에서 함께 보이거나 "
        "대학명·부서명이 함께 등장한다는 이유만으로 추정하지 마라."
    )


def rag_answer_instruction(no_evidence_answer: str) -> str:
    """근거 기반 답변 지시문을 만든다(rag).

    "컨텍스트에만 근거해 답하라"는 제약은 호출자(엔진) 책임이다(08_llm-provider.md).
    근거 부족 시 쓸 보류 문구는 엔진이 소유하므로 인자로 받는다.
    """

    return (
        "너는 대학 공지 안내 도우미다. 아래 [컨텍스트]의 근거에만 기반해 한국어로 답하라.\n"
        "- 컨텍스트에 없는 내용은 추측하지 말고 모른다고 답하라.\n"
        "- 날짜·금액·자격 등 조건은 근거에 있는 값을 그대로 인용하라.\n"
        f'- 근거가 부족하면 "{no_evidence_answer}" 라고 답하라.'
    )
