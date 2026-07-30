# 스케줄러 (Scheduler)

학교별 공지·학사 정보의 **주기적 재크롤링**과 **데이터 최신성 유지(증분 갱신 및 만료 처리)**를 관리하는 시스템 모듈이다.

> **결정**
> - 주기 설정: 학교 등록/변경 시 `crawl_schedule` 키워드(`hourly`, `daily`, `weekly`, `monthly`), 초/분/시간 단위 지정자(`30m`, `1h`, `7d`), 또는 5필드 Cron 표현식(`0 0 * * *`)을 지원한다. Cron 요일은 표준 규약(0·7=일요일, 1=월요일)을 따른다.
> - 실행 동시성: 원자적 DB 상태 변경(`Storage.try_start_crawl`)을 활용하여 이미 크롤링·인덱싱 진행 중인 학교에 대해 중복 스케줄 실행을 방지(Skip 및 logging)한다.
> - 라이프사이클: FastAPI 애플리케이션의 lifespan 이벤트와 연동하여 비동기 타이머 백그라운드 루프로 동작하며, 크롤 파이프라인은 스레드 풀에서 실행해 이벤트 루프를 막지 않는다. 필요 시 수동 단발성 실행(`run_once()`) 및 즉시 트리거(`trigger_school()`)를 제공한다. (Celery/APScheduler 대신 내장 asyncio 루프 — MVP 단순화)
> - 데이터 만료 정책: 재크롤에서 **연속 미관측 N회**(기본 3)인 문서를 만료한다. 크롤러/저장소는 단일 미관측만으로 삭제하지 않으며, 스케줄러가 `Storage.expire_documents_by_miss_count`로 확정한다.

---

## 1. 책임 범위

### 하는 일

- `School.crawl_schedule` 주기 설정(키워드, Cron, 상대 시간) 파싱 및 다음 실행 시각(`next_run_at`) 계산.
- 저장소(Storage)의 모든 학교 스케줄 상태를 동기화하여 **메모리** 작업 맵(`SchoolJob`)으로 관리. (프로세스 재시작 시 Storage에서 재동기화)
- 백그라운드 주기 검사를 수행하여 실행 시각이 도래한 학교에 대해 재크롤링 파이프라인(`CrawlRequest(mode="recrawl")`) 트리거.
- 크롤링 중복 실행 방지 (DB 원자적 상태 변경 및 중복 건너뜀 추적).
- 연속 미관측 임계값을 넘는 문서에 대한 스케줄러 기반 만료 판정(`expire_stale_documents`).

### 하지 않는 일

- 직접 웹 HTML 수집·파싱 (→ Crawler)
- 본문 텍스트 청킹 및 엔티티/관계 추출 (→ Extractor)
- 지식그래프 노드/엣지 구축 및 저장 (→ Graph Builder)
- DB 테이블 생성 및 direct SQL 처리 (→ Storage)

---

## 2. 입력과 출력

### 작업 등록 및 정보 (`SchoolJob`)

```python
@dataclass
class SchoolJob:
    school_id: int
    crawl_schedule: str
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    error_count: int = 0
    last_status: str = "idle"  # "idle" | "triggered" | "skipped" | "failed" | "disabled"
```

---

## 3. 주기 및 Cron 파싱 규칙

`crawl_schedule` 필드는 다음 세 가지 형식을 지원한다:

| 형식 | 예시 | 설명 |
|---|---|---|
| 키워드 | `hourly`, `daily`, `weekly`, `monthly`, `disabled` | 직관적인 사전 정의 주기 |
| 상대 시간 | `30s`, `15m`, `12h`, `3d` | 단위(s/m/h/d) 포함 상대 시간 간격 |
| Cron 표현식 | `0 0 * * *` (매일 00:00), `0 9 * * 1` (매주 월요일 09:00) | 표준 5필드 Cron (요일: 0·7=일, 1=월) |

---

## 4. 연동 흐름 및 다른 시스템과의 관계

```text
[Storage] ◄── list_schools / try_start_crawl / miss_count·expire ──► [Scheduler]
                                                       │
                                            trigger due job / recrawl
                                                       │
                                                       ▼
                                            [Crawl Pipeline / API]
```

| 시스템 | 방향 | 설명 |
|---|---|---|
| Storage | 양방향 | 학교 목록 및 `crawl_schedule` 조회, `try_start_crawl`로 원자적 상태 변경, 관측 URL/`miss_count`·만료 처리. |
| Backend API | 이전 / 다음 | FastAPI lifespan 백그라운드 루프 실행 관리, 학교 등록·수동 재크롤 시 스케줄 등록/갱신, `_run_crawl` 완료 통지. |
| Crawler | 다음 | 주기 도래 시 `CrawlRequest(mode="recrawl")` 파이프라인 트리거. 재크롤 관측 URL은 API 경로에서 Storage에 반영. |

---

## 5. 예외 처리 및 복구 정책

1. **중복 크롤링 경합(Conflict Handling)**:
   - 스케줄 주기가 도래했더라도 Storage의 `try_start_crawl(school_id)`가 `None`을 반환하면 이미 크롤링/인덱싱 중으로 판단한다.
   - 이때 작업을 실행하지 않고 `last_status="skipped"`로 남긴 뒤 **다음 주기로 `next_run_at`을 갱신**한다 (busy-retry 방지).

2. **실패 및 재시도(Failure & Backoff)**:
   - 파이프라인 실패 시(`on_crawl_finished(success=False)`) `error_count`를 증가시키고, 지수 백오프(60s → 120s → …, 상한 24h)로 `next_run_at`을 재조정한다.

3. **안전한 종료(Graceful Shutdown)**:
   - 백그라운드 작업 중 종료 요청(`stop()`) 시 현재 실행 중인 주기 검사 작업을 안전하게 마무리하고 루프를 정지한다.

---

## 6. 미정 사항 및 확장 가능성

- 다중 서버/멀티 워커 환경 전환 시 분산 락(Redis Lock / DB Advisory Lock) 및 Celery/APScheduler 백엔드 이관.
- 학교별 수집 피크 시간대 분산을 위한 Jitter(무작위 지연) 옵션 도입.
- 목록 수집이 `max_items`에 걸려 잘린 경우 미관측 카운트 오탐 완화(완전 수집 플래그).
- 만료 문서의 그래프 기여분 물리 삭제·비활성화 방식 (Storage 미정 사항과 연계).
