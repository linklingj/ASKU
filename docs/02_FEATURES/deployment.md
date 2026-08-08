# 배포 — Railway (백엔드 + DB)

ASKU 백엔드(FastAPI) + DB(Postgres+pgvector)를 **Railway** 에 서비스 2개(`api`·`db`)로 올린다.
프론트엔드는 GitHub Pages 로 따로 배포한다(§7).
관련 파일: [`backend/Dockerfile`](../../backend/Dockerfile), [`.env.example`](../../.env.example), 로컬 개발용 [`docker-compose.yml`](../../docker-compose.yml).

## 왜 이 구성인가

- **임베딩(bge-m3)이 로컬 실행** → 실행에 RAM 3~4GB 필요. api 서비스 메모리 상한을 그 이상으로 잡아야 한다.
- **스케줄러가 인프로세스**(`docs/01_SYSTEM/09_scheduler.md`) → 앱이 **상시 가동**돼야 하고 **워커는 1개**여야 한다(여러 워커면 재크롤 중복). Railway는 유휴 시 잠들지 않고 서비스가 단일 컨테이너라 두 조건을 자연히 만족한다.
- **DB가 Postgres 하나**(문서·그래프·벡터 전부, `docs/01_SYSTEM/06_storage.md`) → 별도 그래프/벡터 DB 불필요. 서비스 2개(api + db)면 끝.
- **비용은 RAM 상주 시간에 붙는다** → Railway는 메모리를 GB·시간으로 과금하고 bge-m3가 ~4GB를 계속 물고 있어, 상시 가동이면 대략 월 수십 달러다. **시연 기간에만 켜두는** 운용을 권한다(§8 비용).

구성 요약:

| 구성요소 | 실행 | 데이터 |
|---|---|---|
| `api` | `backend/Dockerfile` 빌드, uvicorn 1워커, 포트 8000 | bge-m3 캐시 → `/models` 볼륨 |
| `db` | `pgvector/pgvector:pg16` 이미지 | `/var/lib/postgresql/data` 볼륨 |
| 답변 생성 | Gemini API (외부) | — |

> Compose 파일은 그대로 못 쓴다(Railway는 다중 서비스 compose를 한 번에 올리지 않는다). 아래처럼 `db`·`api` 를 각각 만든다. `docker-compose.yml` 은 로컬 개발용으로 남겨 둔다.

## 1. db 서비스 (Postgres + pgvector)

**New → Docker Image** → `pgvector/pgvector:pg16`.

- 변수: `POSTGRES_USER=asku`, `POSTGRES_PASSWORD=<값>`, `POSTGRES_DB=asku`
- 변수 `PGDATA=/var/lib/postgresql/data/pgdata` 를 **반드시** 준다. Railway 볼륨은 포맷된 파일시스템이라 마운트 지점에 `lost+found` 가 있어, `PGDATA` 를 마운트 루트로 두면 `initdb` 가 `directory "…/data" exists but is not empty` 로 거부한다. 하위 폴더로 옮기면 그 폴더가 비어 있어 통과한다.
- 볼륨 하나를 `/var/lib/postgresql/data` 에 마운트(데이터 영속).
- 공개 도메인은 만들지 않고 private 네트워킹만 쓴다. 서비스 이름은 **`db`** 로 둔다(아래 참조 변수에서 이 이름을 쓴다).

로그에 `database system is ready to accept connections` 가 뜨면 정상.

## 2. api 서비스 (FastAPI + bge-m3)

**New → GitHub Repo** → 이 레포 선택.

- **Settings → Source**: Root Directory `backend`(루트 `backend/Dockerfile` 자동 감지), Branch `main`.
- **Settings → Volumes**: 볼륨 하나를 `/models` 에 마운트 — bge-m3 캐시(`HF_HOME`). 없으면 재배포·재시작마다 ~2.3GB를 다시 받는다.
- **Settings → Networking**: 공개 도메인 생성 후 **target port 를 8000** 으로 지정(Dockerfile이 `--port 8000` 고정). 이 도메인이 프론트의 API 베이스 URL이 된다.
- **Settings → Resources**: bge-m3가 3~4GB를 쓰므로 메모리 상한이 그보다 낮지 않은지 확인(낮으면 로드 중 OOM).

**Variables**:

| 키 | 설명 |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://asku:${{db.POSTGRES_PASSWORD}}@${{db.RAILWAY_PRIVATE_DOMAIN}}:5432/asku` — 코드가 `postgresql+psycopg://` 드라이버 접두어를 요구한다(`storage.py`). Railway 기본 Postgres 플러그인이 주는 `postgresql://` 를 그대로 쓰면 안 되므로 pgvector 이미지를 직접 띄우고 URL도 손으로 구성한다. `${{db.*}}` 는 §1 db 서비스 값을 참조한다 |
| `OPENAI_API_KEY` | 답변 생성·추출용. [OpenAI Platform](https://platform.openai.com/api-keys)에서 발급 |
| `OPENAI_MODEL` | 기본 `gpt-4o-mini` (저비용). 계정에서 쓸 수 있는 모델로 지정 |
| `LLM_PROVIDER` | `openai`(기본) 또는 `gemini`. gemini 로 되돌릴 때만 지정하고, 그때는 `GEMINI_API_KEY`·`GEMINI_MODEL` 를 대신 넣는다 |
| `SPEC_AUTOGEN` | 알려진 템플릿과 맞지 않는 학교의 수집 규격을 LLM 으로 생성할지. 기본 꺼짐(`1`·`true`·`yes` 로 켬) |

`SPEC_AUTOGEN` 을 켜면 새 학교를 등록할 때마다 호출당 약 4만 토큰이 나갈 수 있다. 알려진 게시판 제품은 이 설정과 무관하게 LLM 없이 처리되므로, 먼저 꺼 둔 채로 등록해 보고 템플릿에 걸리지 않는 학교가 나올 때 켜는 편이 안전하다([`03_crawler.md`](../01_SYSTEM/03_crawler.md) §7-2).

## 3. 기동

변수를 저장하면 api가 자동 빌드된다.

- 첫 빌드는 torch/FlagEmbedding 때문에 수 분 소요(CPU 휠로 설치 — CUDA 불필요).
- 기동 시 `lifespan` 이 **스키마·pgvector 확장을 자동 생성**(`create_schema()`)한다. 수동 마이그레이션 없음.
- ⚠️ Railway엔 Compose의 `depends_on: healthy` 같은 게이팅이 없다. db가 아직 안 떴으면 api가 스키마 생성에 실패하며 죽었다가 재시작한다. db가 `ready` 된 뒤 api를 **Redeploy** 하면 깔끔하다.

## 4. 검증 · 학교 등록

```bash
curl -s https://<api-도메인>/schools    # 200 이면 api ↔ db 정상 (빈 목록 형태의 JSON)
```

검증 완료 11개 학교 등록 — Railway CLI로 컨테이너에 붙어 실행:

```bash
railway ssh --service api
python -m app.seed_schools --dry-run    # 등록 대상 11개만 확인, DB 변경 없음
python -m app.seed_schools              # schools 테이블에 등록 (멱등)
```

- **seed 는 등록만 한다.** `schools` 테이블에 행만 넣고 크롤링·Gemini·Extractor 를 실행하지 않는다. 등록 대상은 `school-support-status.md` 표본 검증을 통과한 11곳이며, 한양대·중앙대(렌더링 필요)·서강대(URL 미확인)는 제외돼 있다.
- CLI 접속이 안 되면 공개 API(`POST /schools`)로 등록해도 된다. 단 이 경로는 등록과 동시에 크롤을 시작한다.

## 5. 데이터 채우기 · 워밍업

seed는 등록만 하므로 실제 데이터는 크롤을 돌려야 채워진다. 먼저 학교 1곳으로 전체 파이프라인(크롤→추출→임베딩→그래프)을 검증한 뒤 나머지로 확산하는 편이 안전하다.

```bash
curl -sX POST https://<api-도메인>/schools/1/recrawl   # 크롤 시작
curl -s     https://<api-도메인>/schools/1/status       # crawling → indexing → ready
```

> **첫 질의는 느리다.** `ready` 후 최초 `/query` 때 bge-m3(~2.3GB)를 로드한다. 이후엔 `/models` 볼륨 캐시로 빠르다. 시연 전 미리 한 번 질의해 워밍업해 둘 것.

## 6. 운영

- **로그**: `railway logs --service api` 또는 대시보드 Deployments → Logs.
- **업데이트 배포**: 배포 브랜치(`main`)에 push하면 자동 재빌드·배포. 매 재배포마다 컨테이너가 재시작돼 bge-m3를 다시 로드하니, 시연 직전엔 push를 피하고 워밍업을 다시 해 둔다.
- **데이터 영속**: db 볼륨(`/var/lib/postgresql/data`)·api 볼륨(`/models`)에 남는다. 서비스를 삭제하면 볼륨도 사라질 수 있으니 주의.
- **비용 절약**: 데모 사이에는 서비스를 멈춰 두고(RAM 과금 중단) 시연 전에 켠다. 켠 뒤 첫 질의 워밍업 필요.

## 7. 프론트엔드 (GitHub Pages)

프론트는 `frontend/src/` 의 **정적 HTML/CSS/JS**(빌드 없음)이고, GitHub Pages 로 배포한다.

- API 주소는 [`frontend/src/api.js`](../../frontend/src/api.js) 에 Railway 도메인이 기본값으로 박혀 있다(`?api=<url>` 나 `localStorage.asku_api` 로 재정의 가능).
- [`.github/workflows/pages.yml`](../../.github/workflows/pages.yml) 이 `main` push 때 `frontend/src` 를 Pages 로 올린다. Pages Source 는 **GitHub Actions** 로 지정하고, `github-pages` 환경의 배포 허용 브랜치에 `main` 이 있어야 한다.
- 배포 URL: **https://linklingj.github.io/ASKU/**
- Pages(HTTPS) → Railway(HTTPS) 조합이라 혼합 콘텐츠 차단이 없다.

## 8. 비용 · 한계 (이슈 #30)

- **비용**: Railway 는 RAM·CPU 를 사용 시간으로 과금한다. bge-m3 상주로 api가 ~4GB를 쓰므로 상시 가동 시 대략 월 수십 달러 수준. 시연 기간에만 켜 두면 며칠치라 소액이다. + Gemini 사용량(시연 트래픽이면 월 수천 원, 무료 등급으로도 가능). 실제 발생 비용은 확정되는 대로 이 절에 기록한다.
- **워커 1개 고정**: 스케줄러 중복 방지 때문. 처리량이 문제되면 스케줄러를 별도 프로세스로 분리하는 리팩터가 선행돼야 한다.
- **상시 무료가 목표라면**: RAM 과금이 없는 저가 VPS(Hetzner ARM 등, 월 €4대)에 `docker-compose.yml` 을 그대로 올리는 편이 24/7 비용이 싸다. 이때 프론트 `api.js` 의 API 주소를 해당 서버 주소로 바꾼다.
