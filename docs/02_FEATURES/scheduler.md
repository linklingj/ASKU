# Scheduler 기능 (`app/scheduler.py`)

`backend/app/scheduler.py`는 ASKU 시스템에 등록된 학교들의 **자동 재크롤링 주기 관리 및 데이터 만료 처리**를 담당하는 독립적이고 얇은 스케줄러 계층이다.

설계·결정의 단일 기준은 [`01_SYSTEM/09_scheduler.md`](../01_SYSTEM/09_scheduler.md)이며, 이 문서는 **공개 타입과 사용법, 주의 지점**을 정리한다.

---

## 공개 타입 및 시그니처

### 1. `SchoolJob` 데이터 클래스

```python
@dataclass
class SchoolJob:
    school_id: int
    crawl_schedule: str
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    error_count: int = 0
    last_status: str = "idle"
```

### 2. `Scheduler` 주요 공개 메서드

- `register_school(school_id, crawl_schedule, last_run_at=None, *, preserve_next_run=False) -> SchoolJob | None`: 학교 스케줄 등록. `preserve_next_run`은 주기 sync 시 `next_run_at` 유지를 위해 사용
- `unregister_school(school_id: int) -> bool`: 학교 스케줄 등록 해제
- `get_job(school_id: int) -> SchoolJob | None`: 작업 조회
- `list_jobs() -> list[SchoolJob]`: 전체 작업 목록 반환
- `sync_jobs_from_storage() -> int`: Storage DB의 학교 정보와 메모리 스케줄 목록 동기화
- `trigger_school(school_id: int, force: bool = False) -> bool`: 특정 학교 재크롤링 즉시 트리거 (스레드 풀 또는 `runner_callback`)
- `on_crawl_finished(school_id: int, *, success: bool) -> None`: 파이프라인 완료 통지 (실패 시 지수 백오프)
- `check_and_run_due_jobs(now: datetime | None = None) -> list[int]`: 실행 시각이 도래한 모든 학교 작업 검사 및 실행
- `expire_stale_documents(school_id: int | None = None, miss_threshold: int | None = None) -> list[int]`: 연속 미관측 임계값 초과 문서 만료
- `start()` / `stop()` / `is_running()`: 비동기 백그라운드 스케줄러 루프 제어
- `run_once()`: 단발성 주기 검사 + 만료 수행

### 3. 유틸리티 함수

- `parse_schedule(schedule_str: str | None) -> dict`: 주기를 해석하여 유형(`keyword`, `cron`, `interval`)과 간격 정보 반환
- `calculate_next_run(schedule_str: str | None, base_time: datetime | None = None) -> datetime | None`: 기준 시각 대비 다음 실행 시각(UTC) 계산

---

## 주기 표현식 예시

```python
from app.scheduler import calculate_next_run, parse_schedule

# 키워드
next_time = calculate_next_run("daily")  # +24시간 후
next_time = calculate_next_run("weekly") # +7일 후

# 간격 지정
next_time = calculate_next_run("12h")    # +12시간 후
next_time = calculate_next_run("30m")    # +30분 후

# Cron 표현식 (요일 1 = 월요일)
next_time = calculate_next_run("0 2 * * *") # 매일 새벽 2시
next_time = calculate_next_run("0 9 * * 1") # 매주 월요일 09:00
```

---

## 사용법

### 백그라운드 태스크 및 FastAPI lifespan 연동

```python
from app.scheduler import get_scheduler

scheduler = get_scheduler()

# 1. 앱 시작 시 동기화 및 백그라운드 시작
await scheduler.start()

# 2. 애플리케이션 종료 시
await scheduler.stop()
```

학교 등록(`POST /schools`) 시에도 `register_school`이 호출되어, 재시작 전에도 스케줄 대상에 포함된다. 백그라운드 루프는 주기적으로 `sync_jobs_from_storage`와 `expire_stale_documents`를 수행한다.

### 동기 / 수동 재크롤링 실행

```python
# 수동으로 특정 학교 트리거
triggered = scheduler.trigger_school(school_id=1)

# 단발성 배치 검사 (크론탭 또는 테스트용)
ran_school_ids = scheduler.run_once()
```

### 주의

- 기본 경로의 `_run_crawl`은 **스레드 풀**에서 실행된다. 이벤트 루프를 막지 않는다.
- skip 시에도 `next_run_at`이 다음 주기로 전진한다.
- `SchoolJob`은 메모리 맵이다. 영속 상태는 Storage의 `crawl_schedule`·문서 `miss_count`/`expired_at`에 있다.

---

## 테스트 방법

```bash
python -m pytest backend/tests/test_scheduler.py
```

---

관련 문서: [`01_SYSTEM/09_scheduler.md`](../01_SYSTEM/09_scheduler.md), [`01_SYSTEM/01_backend-api.md`](../01_SYSTEM/01_backend-api.md).
