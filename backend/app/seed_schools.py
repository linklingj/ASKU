"""초기 등록할 검증 완료 학교 목록과 멱등 시드 로직.

여기의 작업은 ``schools`` 테이블에 학교를 등록하는 것뿐이다. 크롤링, Gemini
규격 생성, Extractor 실행은 시작하지 않는다. 실제 수집은 API 등록 흐름 또는
스케줄러가 담당한다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from urllib.parse import urlsplit

from app.models import School
from app.settings import load_env
from app.storage import Storage


@dataclass(frozen=True)
class SeedSchool:
    """운영에 올릴 검증 완료 학교 한 곳의 최소 등록 정보."""

    name: str
    base_url: str

    @property
    def host(self) -> str:
        return (urlsplit(self.base_url).hostname or "").lower()


# `school-support-status.md`에서 목록·상세 표본 수집을 통과한 학교만 둔다.
# 한양대·중앙대(렌더링 필요), 서강대(유효 목록 URL 미확인)는 의도적으로 제외한다.
SUPPORTED_SCHOOLS: tuple[SeedSchool, ...] = (
    SeedSchool("연세대학교", "https://www.yonsei.ac.kr/sc/254/subview.do"),
    SeedSchool("세종대학교", "https://www.sejong.ac.kr/kor/intro/notice1.do?mode=list&article.offset=0"),
    SeedSchool("홍익대학교", "https://www.hongik.ac.kr/kr/newscenter/notice.do"),
    SeedSchool("성균관대학교", "https://www.skku.edu/skku/campus/skk_comm/notice02.do"),
    SeedSchool("아주대학교", "https://www.ajou.ac.kr/kr/ajou/notice.do"),
    SeedSchool("이화여자대학교", "https://www.ewha.ac.kr/ewha/news/notice.do"),
    # 게시판 번호를 직접 가리키던 `/bbs/konkuk/25/artclList.do` 는 폐지됐다("사용중지된 싱글
    # 모듈입니다" 알림 페이지를 200 으로 돌려줘 목록이 0행이 된다). 메뉴 번호를 쓰는
    # `subview.do` 로 넣어야 하위 게시판(장학·학칙·연구과제)까지 자동 발견된다 — 현행
    # 게시판 URL(`/bbs/konkuk/234/artclList.do`)로 넣으면 그 한 곳만 돌고 발견이 0개다.
    SeedSchool("건국대학교", "https://www.konkuk.ac.kr/konkuk/2238/subview.do"),
    SeedSchool("서울대학교", "https://www.snu.ac.kr/snunow/notice/genernal"),
    SeedSchool("국민대학교", "https://www.kookmin.ac.kr/user/kmuNews/notice/1/index.do"),
    SeedSchool("고려대학교", "https://www.korea.ac.kr/ko/566/subview.do"),
    SeedSchool("경희대학교", "https://www.khu.ac.kr/kor/user/bbs/BMSR00040/list.do?menuNo=200317"),
)


def seed_schools(storage: object, schools: tuple[SeedSchool, ...] = SUPPORTED_SCHOOLS) -> dict[str, int]:
    """학교를 호스트 기준으로 한 번만 등록하고, 처리 건수를 반환한다.

    같은 호스트가 이미 있으면 새 행을 만들지 않고 표시용 이름과 기준 URL만
    갱신한다. 기존 ``status``·``crawl_schedule``은 보존한다.
    """

    existing = {
        (urlsplit(school.base_url).hostname or "").lower(): school
        for school in storage.list_schools()
    }
    result = {"created": 0, "updated": 0, "unchanged": 0}

    for target in schools:
        current = existing.get(target.host)
        if current is None:
            storage.create_school(School(name=target.name, base_url=target.base_url))
            result["created"] += 1
            continue

        if current.name == target.name and current.base_url == target.base_url:
            result["unchanged"] += 1
            continue

        storage.update_school_registration(
            current.school_id, name=target.name, base_url=target.base_url
        )
        result["updated"] += 1

    return result


def main() -> None:
    """Docker와 로컬에서 공통으로 쓰는 시드 명령 진입점."""

    parser = argparse.ArgumentParser(description="검증 완료 학교를 schools 테이블에 등록")
    parser.add_argument("--dry-run", action="store_true", help="등록 대상만 출력하고 DB는 바꾸지 않음")
    args = parser.parse_args()

    load_env()
    if args.dry_run:
        print(f"등록 대상: {len(SUPPORTED_SCHOOLS)}개 (DB 변경 없음)")
        for school in SUPPORTED_SCHOOLS:
            print(f"- {school.name}: {school.base_url}")
        return

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL이 필요합니다. backend/.env 또는 운영 환경변수에 설정해 주세요.")

    storage = Storage(database_url)
    try:
        storage.create_schema()
        result = seed_schools(storage)
        total = len(storage.list_schools())
    finally:
        storage.close()

    print(
        "학교 등록 완료: "
        f"신규 {result['created']}개 / 갱신 {result['updated']}개 / 유지 {result['unchanged']}개 "
        f"(현재 총 {total}개)"
    )
    print("※ 이 명령은 크롤링·Gemini 호출을 시작하지 않습니다.")


if __name__ == "__main__":
    main()
