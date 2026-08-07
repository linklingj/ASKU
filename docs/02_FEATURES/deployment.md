# 배포 — Oracle Cloud Always Free 단일 VM (Docker Compose)

ASKU 백엔드(FastAPI) + DB(Postgres+pgvector)를 **무료 ARM VM 한 대**에 Docker Compose로 올린다.
관련 파일: 루트 [`docker-compose.yml`](../../docker-compose.yml), [`backend/Dockerfile`](../../backend/Dockerfile), [`.env.example`](../../.env.example).

## 왜 이 구성인가

- **임베딩(bge-m3)이 로컬 실행** → 실행에 RAM 3~4GB 필요. 대부분의 무료 백엔드 티어(RAM 256~512MB)엔 안 올라간다. Oracle Always Free의 **Ampere A1(최대 4 OCPU / 24GB)** 이 무료로 이 RAM을 주는 유일한 현실적 선택.
- **스케줄러가 인프로세스**(`docs/01_SYSTEM/09_scheduler.md`) → 앱이 **상시 가동**돼야 하고 **워커는 1개**여야 한다(여러 워커면 재크롤 중복). "요청 없으면 잠드는" 티어는 부적합.
- **DB가 Postgres 하나**(문서·그래프·벡터 전부, `docs/01_SYSTEM/06_storage.md`) → 별도 그래프/벡터 DB 불필요. 컨테이너 2개(api + db)면 끝.

구성 요약:

| 구성요소 | 실행 | 데이터 |
|---|---|---|
| `api` | `backend/Dockerfile` 빌드, uvicorn 1워커, 포트 8000 | bge-m3 캐시 → `models` 볼륨 |
| `db` | `pgvector/pgvector:pg16` 이미지 | `pgdata` 볼륨 |
| 답변 생성 | Gemini API (외부) | — |

## 1. Oracle VM 생성

1. Oracle Cloud 가입 → **Compute > Instances > Create**.
2. Image/Shape: **Canonical Ubuntu 22.04**, Shape는 **Ampere (VM.Standard.A1.Flex)**, OCPU **4** / Memory **24GB** (Always Free 한도 내).
   - 재고 부족(`Out of capacity`)이 뜨면 리전/가용성 도메인을 바꿔 재시도하거나 잠시 뒤 다시 시도. Always Free의 흔한 관문.
3. SSH 공개키 등록 후 생성. 공인 IP 확보.
4. **방화벽 2겹을 모두** 연다 (Oracle의 대표적 함정):
   - VCN **Security List**: Ingress 규칙 `0.0.0.0/0 → TCP 8000` 추가.
   - VM 내부: `sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT` (Ubuntu 이미지는 iptables가 기본 차단).

## 2. VM 초기 세팅

```bash
ssh ubuntu@<공인IP>

# Docker + compose 플러그인
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu && newgrp docker   # 재로그인 후 sudo 없이 docker 사용

# 저장소
git clone <레포 URL> asku && cd asku
```

## 3. 환경변수

```bash
cp .env.example .env
nano .env
```

| 키 | 설명 |
|---|---|
| `POSTGRES_PASSWORD` | DB 비밀번호. DB 컨테이너와 `DATABASE_URL`에 함께 쓰임 |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey)에서 발급 |
| `GEMINI_MODEL` | 기본 `gemini-3.5-flash` (저비용). 필요 시 변경. `gemini-2.5-flash` 는 신규 키로 호출하면 404 다 |
| `SPEC_AUTOGEN` | 알려진 템플릿과 맞지 않는 학교의 수집 규격을 LLM 으로 생성할지. 기본 꺼짐(`1`·`true`·`yes` 로 켬) |

`SPEC_AUTOGEN` 을 켜면 새 학교를 등록할 때마다 호출당 약 4만 토큰이 나갈 수 있다. 무료 티어는 학교 몇 개면 한도에 걸린다. 알려진 게시판 제품은 이 설정과 무관하게 LLM 없이 처리되므로, 먼저 꺼 둔 채로 등록해 보고 템플릿에 걸리지 않는 학교가 나올 때 켜는 편이 안전하다([`03_crawler.md`](../01_SYSTEM/03_crawler.md) §7-2).

`DATABASE_URL`은 compose가 `db` 서비스명으로 자동 구성하므로 직접 넣지 않는다.

## 4. 배포

```bash
docker compose up -d --build
```

- 첫 빌드는 torch/FlagEmbedding 때문에 수 분 소요(ARM VM에서 native arm64 CPU 휠로 설치됨 — CUDA 불필요).
- `api`는 `db`가 healthy가 된 뒤 기동하고, 시작 시 `lifespan`이 **스키마·pgvector 확장을 자동 생성**(`create_schema()`)한다. 수동 마이그레이션 없음.

## 5. 검증

```bash
docker compose ps                       # 두 서비스 up + db healthy 확인
curl -s http://localhost:8000/schools   # 200 + 빈 목록이면 정상

# 검증 완료 11개 학교 일괄 등록 (멱등 — 여러 번 실행해도 중복 없음)
docker compose exec api python -m app.seed_schools --dry-run   # 등록 대상만 확인, DB 변경 없음
docker compose exec api python -m app.seed_schools             # schools 테이블에 등록

# (선택) 학교 하나만 임시 등록
curl -sX POST http://localhost:8000/schools \
  -H 'Content-Type: application/json' \
  -d '{"name":"테스트대","base_url":"https://example.ac.kr","crawl_schedule":"weekly"}'

curl -s http://localhost:8000/schools/1/status   # crawling → indexing → ready 추적
```

> **seed 는 등록만 한다.** `app.seed_schools` 는 `schools` 테이블에 행만 넣고 크롤링·Gemini·Extractor 를 실행하지 않는다(`crawl_schedule` 기본값에 따라 스케줄러가 수집을 돌린다). 등록 대상은 `school-support-status.md` 표본 검증을 통과한 11곳이며, 한양대·중앙대(렌더링 필요)·서강대(URL 미확인)는 제외돼 있다. `SPEC_AUTOGEN` 을 켜지 않아도 이 11곳은 전용 Adapter·템플릿·학교별 규격으로 수집된다.

> **첫 질의는 느리다.** 최초 `/query` 때 bge-m3(~2.3GB)를 내려받아 로드한다. 이후엔 `models` 볼륨 캐시로 빠르다. 시연 전 미리 한 번 질의해 워밍업해 둘 것.

외부에서 `http://<공인IP>:8000/schools` 가 열리면 방화벽까지 OK. 프론트엔드는 이 주소를 API 베이스로 지정하면 된다(CORS는 이미 전체 허용, `api.py`).

## 6. 운영

```bash
docker compose logs -f api        # 로그
docker compose restart api        # 재시작
git pull && docker compose up -d --build   # 업데이트 배포

# DB 백업 / 복구
docker compose exec db pg_dump -U asku asku > backup_$(date +%F).sql
cat backup.sql | docker compose exec -T db psql -U asku asku
```

- 데이터는 `pgdata`(DB)·`models`(임베딩 캐시) 볼륨에 남으므로 컨테이너를 지워도 유지된다. `docker compose down -v`는 **볼륨까지 삭제**하니 주의.
- 재부팅 후 자동 기동: `restart: unless-stopped`로 설정돼 있음.

## 7. 대안 배포: Railway (VM 없이 Git 배포)

VM·방화벽을 직접 다루지 않고 Git 연동으로 올리고 싶을 때의 선택지. 구조(로컬 bge-m3 · 인프로세스 스케줄러 · Postgres 하나)는 그대로 두되, **Compose 파일은 못 쓰고 서비스 2개(db·api)를 각각 만든다**.

> **RAM 과금 주의.** Railway는 메모리를 GB·시간 단위로 과금한다. bge-m3 상주로 api가 ~4GB를 쓰므로 상시 가동이면 대략 월 수십 달러가 붙는다. **시연 기간에만 켜두는** 운용이 맞고, 상시 무료가 목표면 Oracle(§1~6)이나 저가 VPS(Hetzner 등)가 낫다.

**1) db 서비스** — `pgvector/pgvector:pg16` 이미지로 생성
- 변수: `POSTGRES_USER=asku`, `POSTGRES_PASSWORD=<값>`, `POSTGRES_DB=asku`
- 변수 `PGDATA=/var/lib/postgresql/data/pgdata` 를 **반드시** 준다. Railway 볼륨은 포맷된 파일시스템이라 마운트 지점에 `lost+found` 가 있어, `PGDATA` 를 마운트 루트로 두면 `initdb` 가 `directory "…/data" exists but is not empty` 로 거부한다(Compose의 빈 명명 볼륨에선 안 나던 문제). 하위 폴더로 옮기면 그 폴더가 비어 있어 통과한다.
- 볼륨 하나를 `/var/lib/postgresql/data` 에 마운트(데이터 영속). 공개 도메인은 만들지 않고 private 네트워킹만 쓴다.

**2) api 서비스** — 이 레포 연결, Root Directory `backend`(루트 `backend/Dockerfile` 자동 감지)
- 변수:
  - `DATABASE_URL=postgresql+psycopg://asku:${{db.POSTGRES_PASSWORD}}@${{db.RAILWAY_PRIVATE_DOMAIN}}:5432/asku`
    - 코드가 `postgresql+psycopg://` 드라이버 접두어를 요구한다(`storage.py`). Railway 기본 Postgres 플러그인이 주는 `postgresql://` URL을 그대로 넣으면 안 되므로, 위처럼 pgvector 이미지로 직접 띄우고 URL도 손으로 구성한다.
  - `GEMINI_API_KEY` · `GEMINI_MODEL`(§3 표와 동일), 필요 시 `SPEC_AUTOGEN`.
- 볼륨 하나를 `/models` 에 마운트 — bge-m3 캐시(`HF_HOME`). 없으면 재배포마다 ~2.3GB를 다시 받는다.
- 네트워킹: 공개 도메인을 만들고 **target port 를 8000** 으로 지정(Dockerfile이 8000 고정). 이 도메인이 프론트의 API 베이스 URL이 된다.
- 리소스: bge-m3가 3~4GB를 쓰므로 서비스 메모리 상한이 그보다 낮지 않은지 확인.

**3) 기동·검증**
- db가 먼저 뜬 뒤 api가 기동하고, api `lifespan` 이 `create_schema()` 로 스키마·pgvector 확장을 자동 생성한다(수동 마이그레이션 없음 — VM과 동일).
- 검증 완료 11개 학교 등록 — Railway CLI로 컨테이너에 붙어 실행:

  ```bash
  railway ssh --service api
  python -m app.seed_schools        # 컨테이너 안에서
  ```

  CLI 접속이 안 되면 공개 API(`POST /schools`)로 등록해도 된다. 단 이 경로는 등록과 동시에 크롤을 시작한다(시드는 등록만 하고 크롤을 돌리지 않는다).
- 나머지(`/schools` 200 확인, 첫 질의 워밍업)는 §5 와 같다.

스케줄러의 워커 1개 제약은 단일 컨테이너라 자연히 충족된다. 유휴 시 잠드는 티어가 아니므로 상시 가동 요건도 만족한다.

## 8. 한계 / 다음 단계

- **HTTPS 없음**: 지금은 평문 `:8000`. 도메인 + HTTPS가 필요하면 Caddy/nginx 리버스 프록시를 앞단에 두면 된다(선택, 시연엔 불필요).
- **워커 1개 고정**: 스케줄러 중복 방지 때문. 처리량이 문제되면 스케줄러를 별도 프로세스로 분리하는 리팩터가 선행돼야 한다.
- **비용**: VM ₩0(영구 무료) + Gemini 사용량(시연 트래픽이면 월 수천 원, 무료 등급으로도 가능).
