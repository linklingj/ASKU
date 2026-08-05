"""개발용 환경변수 로딩.

`backend/.env` 를 읽어 환경변수로 올린다. 운영(도커)에서는 compose 가 환경변수를
직접 주입하므로 이 파일이 없어도 되고, 이미 설정된 값은 덮어쓰지 않는다.

키를 저장소에 커밋하지 않기 위한 장치다. `.env` 는 `.gitignore` 에 있고, 필요한
항목 목록만 `.env.example` 로 공유한다.
"""

from __future__ import annotations

from pathlib import Path


# backend/app/settings.py → backend/.env
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env(path: Path | None = None) -> bool:
    """`.env` 를 읽어 환경변수에 올린다. 읽었으면 True.

    개발용 스크립트의 진입점에서만 부른다. 라이브러리 코드가 부르면 import 만으로
    프로세스 환경이 바뀌어, 호출자가 의도한 설정을 조용히 덮을 수 있다.
    """

    target = path or ENV_PATH
    if not target.exists():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:  # 의존성이 없으면 환경변수로만 동작한다
        return False
    # 이미 설정된 환경변수를 우선한다. 터미널에서 넘긴 값이 파일보다 구체적이다.
    return bool(load_dotenv(target, override=False))
