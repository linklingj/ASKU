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
        f'- 근거가 부족하면 "{no_evidence_answer}" 라고 답하라.\n'
        "답변을 작성할 때 반드시 다음 두 구획으로 명확히 구분하여 작성하라:\n"
        "[핵심 답변]\n"
        "사용자가 질문한 내용에 대한 결론과 핵심 답변을 한두 문장으로 서술하여 질문에 대한 답을 모두 포함하여 나타내라.\n\n"
        "[상세 답변]\n"
        "구체적인 수강 대상, 일정, 과목 목록/표, 자격 조건 등 본문 상세 내용을 마크다운 형식(제목, 굵기, 기울기, 목록, 표 등)으로 가독성 있게 작성하라."
    )


def spec_draft_instruction(
    listing_url: str,
    listing_html: str,
    detail_samples: Iterable[tuple[str, str]],
    schema: str,
    feedback: Iterable[str] = (),
) -> str:
    """수집 규격 초안 지시문을 만든다(spec_generator).

    `feedback` 은 직전 시도가 검증에서 받은 지적이다. 그냥 다시 시키면 같은 실수를
    반복하므로, 무엇이 왜 틀렸는지 돌려준다.
    """

    samples = "\n\n".join(
        f"[상세 페이지 {index}] {url}\n{html}" for index, (url, html) in enumerate(detail_samples, start=1)
    )
    retry = ""
    if feedback := list(feedback):
        retry = (
            "\n[직전 시도의 문제]\n"
            + "\n".join(f"- {item}" for item in feedback)
            + "\n위 지적을 반드시 반영해 선택자를 고쳐라.\n"
        )

    return (
        "너는 대학 공지 게시판의 HTML 구조를 분석해 수집 규격을 만드는 도구다.\n"
        "아래 목록·상세 페이지를 보고 JSON 규격으로만 응답하라.\n\n"
        f"[JSON 스키마]\n{schema}\n\n"
        "규칙:\n"
        "- 선택자는 CSS 선택자로 쓴다. 실제 HTML 에 존재하는 클래스·태그만 사용하라.\n"
        "- row 는 공지 한 건에 해당하는 요소다. 메뉴·배너의 반복 요소를 고르지 마라.\n"
        "- 고정공지와 일반공지의 클래스가 다르면 둘 다 포함하는 선택자를 골라라.\n"
        "- title 은 목록의 제목 그 자체만 담기는 요소여야 한다. 말머리·분류·조회수가\n"
        "  함께 들어가는 요소를 고르면 상세 페이지 제목과 대조되지 않는다.\n"
        "- detail.body 는 게시글 본문만 담긴 요소다. 본문 아래 이전·다음 글 목록이나\n"
        "  사이트 메뉴가 함께 들어가는 상위 요소를 고르지 마라.\n"
        "- pagination 은 다음 세 유형 중 하나다.\n"
        "  link: '다음' 링크의 href 를 따라간다. selector 를 채운다.\n"
        "  offset: URL 파라미터를 일정하게 늘린다. param·step 을 채운다.\n"
        "  form: 폼의 hidden input 을 조합해 URL 을 만든다. form_selector·next_selector 를 채운다.\n"
        "- 판단할 근거가 없는 필드는 지어내지 말고 비워라.\n"
        f"{retry}\n"
        f"[목록 페이지] {listing_url}\n{listing_html}\n\n"
        f"{samples}"
    )
