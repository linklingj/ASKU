"""검증 완료 학교를 DB에 등록하는 개발·배포용 명령.

실행 예시:
    PYTHONPATH=backend python3 backend/scripts/seed_schools.py --dry-run
    PYTHONPATH=backend python3 backend/scripts/seed_schools.py

크롤링이나 Gemini 호출은 하지 않는다. 같은 DB에서 여러 번 실행해도 학교가
중복 생성되지 않는다.
"""

from app.seed_schools import main


if __name__ == "__main__":
    main()
