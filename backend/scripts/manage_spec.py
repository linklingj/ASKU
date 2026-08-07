"""수집 규격을 등록·조회하는 개발용 명령.

새 학교를 추가할 때 파이썬 어댑터를 짜는 대신 규격 JSON 을 넣는다. 재배포 없이
다음 크롤부터 적용된다.

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/manage_spec.py list
    PYTHONPATH=backend python3 backend/scripts/manage_spec.py show www.example.ac.kr
    PYTHONPATH=backend python3 backend/scripts/manage_spec.py put spec.json
"""

from __future__ import annotations

import argparse
import json

from app.adapter_spec import AdapterSpec
from app.storage import Storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="학교별 수집 규격 관리")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="등록된 규격 목록")
    show = sub.add_parser("show", help="규격 하나를 출력")
    show.add_argument("host")
    put = sub.add_parser("put", help="규격 파일을 등록하거나 갱신")
    put.add_argument("path", help="AdapterSpec 형식의 JSON 파일")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    storage = Storage.from_env()
    try:
        if args.command == "list":
            rows = storage.list_adapter_specs()
            if not rows:
                print("등록된 규격이 없습니다.")
                return
            for row in rows:
                boards = len(row["spec"].get("boards") or [])
                print(f"{row['host']:28} {row['source']:9} 게시판 {boards}개  갱신 {row['updated_at']:%Y-%m-%d}")
            return

        if args.command == "show":
            spec = storage.get_adapter_spec(args.host)
            if spec is None:
                raise SystemExit(f"등록된 규격이 없습니다: {args.host}")
            print(json.dumps(spec, ensure_ascii=False, indent=2))
            return

        with open(args.path, encoding="utf-8") as file:
            payload = json.load(file)
        # 저장 전에 형식을 검증한다. 어긋난 규격이 들어가면 크롤 때 조용히 폴백돼
        # 원인을 찾기 어렵다.
        spec = AdapterSpec.model_validate(payload)
        storage.upsert_adapter_spec(spec.host, spec.model_dump(mode="json"), source=spec.source)
        print(f"등록했습니다: {spec.host} (게시판 {len(spec.boards)}개)")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
