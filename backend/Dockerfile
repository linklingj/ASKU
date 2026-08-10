# ASKU 백엔드 — FastAPI + 인프로세스 스케줄러 + 로컬 bge-m3 임베딩
# build context = backend/ (docker-compose.yml 의 `build: ./backend`)
FROM python:3.12-slim

# HF_HOME: bge-m3/torch 캐시 위치. compose 에서 볼륨으로 마운트해 재시작 후에도 유지.
ENV HF_HOME=/models \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 의존성 먼저 — 소스만 바뀌면 이 레이어는 캐시 재사용.
# ponytail: slim + wheel 만으로 설치. sentencepiece 등 sdist 빌드가 필요해 실패하면
#           `apt-get install -y build-essential` 한 줄을 이 위에 추가하면 됨.
COPY requirements.txt .
RUN pip install -r requirements.txt

# 목록을 자바스크립트로 그리는 학교(중앙대) 전용 브라우저. 이미지가 수백 MB
# 커지므로 Chromium 하나만, 필요한 OS 라이브러리와 함께 받는다.
# 이 학교를 수집하지 않는 배포라면 이 두 줄을 지워도 나머지는 그대로 동작한다.
RUN playwright install --with-deps chromium

COPY app ./app

EXPOSE 8000

# ponytail: 워커 1개 고정 — 스케줄러가 인프로세스라 여러 워커면 재크롤이 중복 실행됨.
#           (uvicorn 기본이 1워커라 --workers 를 주지 않는다.)
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
