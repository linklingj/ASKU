"""스케줄러 — 학교별 재크롤링 주기 관리 및 데이터 만료 처리 모듈.

단일 기준 문서: docs/01_SYSTEM/09_scheduler.md 및 docs/02_FEATURES/scheduler.md
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)

# 키워드별 기본 간격 정의 (초)
KEYWORD_SCHEDULES: dict[str, int] = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
    "monthly": 2592000,  # 30일 기준
}

# 실패 재시도 백오프 (초). error_count=1 → 60s, 이후 지수 증가, 상한 24h
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_MAX_SECONDS = 86400
_DEFAULT_MISS_THRESHOLD = 3


@dataclass
class SchoolJob:
    """학교 재크롤링 주기 작업 메타데이터."""

    school_id: int
    crawl_schedule: str
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    run_count: int = 0
    error_count: int = 0
    last_status: str = "idle"  # "idle" | "triggered" | "skipped" | "failed" | "disabled"


def parse_schedule(schedule_str: str | None) -> dict:
    """주기 설정 문자열을 파싱한다.

    지원 형태:
    1. 키워드: 'hourly', 'daily', 'weekly', 'monthly', 'disabled' (대소문자 무관)
    2. 상대 시간 간격: '30s', '15m', '12h', '3d'
    3. 순수 숫자: 초 단위
    4. Cron 표현식: 5개 필드 (예: '0 0 * * *')
    """
    if not schedule_str or schedule_str.strip().lower() in ("disabled", "none", "null", ""):
        return {"type": "disabled"}

    clean_str = schedule_str.strip().lower()

    # 1. 키워드 매칭
    if clean_str in KEYWORD_SCHEDULES:
        return {"type": "keyword", "keyword": clean_str, "seconds": KEYWORD_SCHEDULES[clean_str]}

    # 2. 상대 시간 간격 매칭 (예: 30s, 15m, 12h, 3d)
    match = re.match(r"^(\d+)\s*([smhd])$", clean_str)
    if match:
        val = int(match.group(1))
        unit = match.group(2)
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        seconds = val * multiplier
        return {"type": "interval", "seconds": seconds}

    # 3. 순수 숫자인 경우 (초 단위)
    if clean_str.isdigit():
        return {"type": "interval", "seconds": int(clean_str)}

    # 4. Cron 표현식 (5개 필드)
    parts = schedule_str.strip().split()
    if len(parts) == 5:
        return {
            "type": "cron",
            "expression": schedule_str.strip(),
            "fields": {
                "minute": parts[0],
                "hour": parts[1],
                "day": parts[2],
                "month": parts[3],
                "day_of_week": parts[4],
            },
        }

    raise ValueError(f"유효하지 않은 crawl_schedule 표현식입니다: '{schedule_str}'")


def _match_cron_field(field_str: str, val: int) -> bool:
    """단일 cron 필드(숫자, '*', '*/N', 'N-M', 'A,B') 매칭 여부를 판단한다."""
    if field_str == "*":
        return True

    # '*/N' 형식 (step)
    if field_str.startswith("*/"):
        try:
            step = int(field_str[2:])
            return step > 0 and (val % step == 0)
        except ValueError:
            return False

    # 'A,B,C' 형식 (comma list)
    if "," in field_str:
        subfields = field_str.split(",")
        return any(_match_cron_field(sf, val) for sf in subfields)

    # 'N-M' 형식 (range)
    if "-" in field_str:
        try:
            start_str, end_str = field_str.split("-", 1)
            return int(start_str) <= val <= int(end_str)
        except ValueError:
            return False

    # 순수 숫자 (cron 요일은 0과 7 모두 일요일)
    try:
        return int(field_str) == val
    except ValueError:
        return False


def _cron_day_of_week(dt: datetime) -> int:
    """표준 cron 요일: 0=일요일 … 6=토요일 (7도 일요일로 취급)."""
    return (dt.weekday() + 1) % 7


def _match_cron_date(fields: dict, dt: datetime) -> bool:
    """day-of-month / day-of-week 매칭. 둘 다 제한되면 표준 cron처럼 OR."""
    day_field = fields["day"]
    dow_field = fields["day_of_week"]
    day_match = _match_cron_field(day_field, dt.day)
    dow_val = _cron_day_of_week(dt)
    # 7 = Sunday alias
    dow_match = _match_cron_field(dow_field, dow_val) or (
        dow_val == 0 and _match_cron_field(dow_field, 7)
    )

    day_any = day_field == "*"
    dow_any = dow_field == "*"
    if day_any and dow_any:
        return True
    if day_any:
        return dow_match
    if dow_any:
        return day_match
    return day_match or dow_match


def _calculate_cron_next_run(cron_info: dict, base_time: datetime) -> datetime:
    """Cron 필드 표현식을 해석하여 다음 도래 시각을 계산한다 (1분 단위 탐색)."""
    # 초 단위 절사 후 1분 뒤부터 탐색
    dt = base_time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    fields = cron_info["fields"]

    for _ in range(525600):  # 최대 1년(525,600분) 범위 탐색
        if (
            _match_cron_field(fields["minute"], dt.minute)
            and _match_cron_field(fields["hour"], dt.hour)
            and _match_cron_field(fields["month"], dt.month)
            and _match_cron_date(fields, dt)
        ):
            return dt
        dt += timedelta(minutes=1)

    return base_time + timedelta(days=1)


def calculate_next_run(schedule_str: str | None, base_time: datetime | None = None) -> datetime | None:
    """주기 설정 문자열과 기준 시각으로부터 다음 실행 시각(UTC aware)을 계산한다."""
    parsed = parse_schedule(schedule_str)
    if parsed["type"] == "disabled":
        return None

    if base_time is None:
        base_time = datetime.now(timezone.utc)
    elif base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=timezone.utc)

    if parsed["type"] in ("keyword", "interval"):
        seconds = parsed["seconds"]
        return base_time + timedelta(seconds=seconds)

    if parsed["type"] == "cron":
        return _calculate_cron_next_run(parsed, base_time)

    return None


def _backoff_seconds(error_count: int) -> int:
    """지수 백오프 초 계산. error_count는 1 이상이어야 한다."""
    capped = max(1, min(error_count, 10))
    return min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** (capped - 1)))


class Scheduler:
    """학교 주기적 크롤링 스케줄 관리기."""

    def __init__(
        self,
        storage=None,
        runner_callback: Callable[[int, str, str], None] | None = None,
        check_interval: float = 5.0,
        sync_every_ticks: int = 12,
        expire_every_ticks: int = 60,
        miss_threshold: int = _DEFAULT_MISS_THRESHOLD,
        max_workers: int = 2,
    ) -> None:
        self._storage = storage
        self._runner_callback = runner_callback
        self.check_interval = check_interval
        self.sync_every_ticks = max(1, sync_every_ticks)
        self.expire_every_ticks = max(1, expire_every_ticks)
        self.miss_threshold = miss_threshold
        self._jobs: dict[int, SchoolJob] = {}
        self._running: bool = False
        self._task: asyncio.Task | None = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="scheduler-crawl")
        self._inflight: set[int] = set()

    def _get_storage(self):
        if self._storage is None:
            from app.storage import Storage

            self._storage = Storage.from_env()
        return self._storage

    def register_school(
        self,
        school_id: int,
        crawl_schedule: str | None,
        last_run_at: datetime | None = None,
        *,
        preserve_next_run: bool = False,
    ) -> SchoolJob | None:
        """학교 스케줄 작업을 등록하거나 갱신한다.

        preserve_next_run=True 이고 기존 작업의 주기가 같으면 next_run_at을 유지한다
        (주기 sync 시 다음 실행 시각이 계속 밀리는 것을 방지).
        """
        parsed = parse_schedule(crawl_schedule)
        if parsed["type"] == "disabled":
            self.unregister_school(school_id)
            return None

        assert crawl_schedule is not None
        now = datetime.now(timezone.utc)
        base_time = last_run_at or now
        next_run = calculate_next_run(crawl_schedule, base_time=base_time)

        job = self._jobs.get(school_id)
        if job is None:
            job = SchoolJob(
                school_id=school_id,
                crawl_schedule=crawl_schedule,
                last_run_at=last_run_at,
                next_run_at=next_run,
            )
            self._jobs[school_id] = job
        else:
            schedule_changed = job.crawl_schedule != crawl_schedule
            job.crawl_schedule = crawl_schedule
            if last_run_at is not None:
                job.last_run_at = last_run_at
            if not (preserve_next_run and not schedule_changed and job.next_run_at is not None):
                job.next_run_at = next_run

        logger.info(
            "School job registered: school_id=%d, schedule=%s, next_run=%s",
            school_id,
            crawl_schedule,
            job.next_run_at,
        )
        return job

    def unregister_school(self, school_id: int) -> bool:
        """등록된 학교 스케줄 작업을 해제한다."""
        if school_id in self._jobs:
            del self._jobs[school_id]
            logger.info("School job unregistered: school_id=%d", school_id)
            return True
        return False

    def get_job(self, school_id: int) -> SchoolJob | None:
        return self._jobs.get(school_id)

    def list_jobs(self) -> list[SchoolJob]:
        return list(self._jobs.values())

    def sync_jobs_from_storage(self) -> int:
        """Storage의 학교 목록에서 valid schedule이 있는 학교들을 스케줄러에 동기화한다."""
        storage = self._get_storage()
        schools = storage.list_schools()
        active_ids = set()

        for school in schools:
            if school.school_id is None:
                continue
            if school.crawl_schedule:
                # 진행 중이면 시작 시각, 그 외에는 updated_at을 마지막 실행 근사치로 사용
                if school.status in ("crawling", "indexing"):
                    last_run_at = school.crawl_started_at
                else:
                    last_run_at = school.updated_at or school.crawl_started_at
                job = self.register_school(
                    school_id=school.school_id,
                    crawl_schedule=school.crawl_schedule,
                    last_run_at=last_run_at,
                    preserve_next_run=True,
                )
                if job is not None:
                    active_ids.add(school.school_id)

        removed = [sid for sid in list(self._jobs.keys()) if sid not in active_ids]
        for sid in removed:
            self.unregister_school(sid)

        return len(active_ids)

    def _advance_next_run(self, job: SchoolJob, now: datetime) -> None:
        if job.crawl_schedule:
            job.next_run_at = calculate_next_run(job.crawl_schedule, base_time=now)

    def _apply_failure_backoff(self, job: SchoolJob, now: datetime) -> None:
        job.error_count += 1
        job.last_status = "failed"
        delay = _backoff_seconds(job.error_count)
        job.next_run_at = now + timedelta(seconds=delay)
        logger.warning(
            "Scheduler backoff: school_id=%d, error_count=%d, next_run_in=%ds",
            job.school_id,
            job.error_count,
            delay,
        )

    def _dispatch_crawl(self, school_id: int, base_url: str, mode: str) -> None:
        """크롤 파이프라인을 동기 콜백 또는 스레드 풀로 실행한다 (이벤트 루프 비블로킹)."""
        if self._runner_callback is not None:
            self._runner_callback(school_id, base_url, mode)
            return

        if school_id in self._inflight:
            logger.info("Crawl already inflight in scheduler pool: school_id=%d", school_id)
            return

        from app.api import _run_crawl

        self._inflight.add(school_id)

        def _runner() -> None:
            try:
                _run_crawl(school_id, base_url, mode)
            finally:
                self._inflight.discard(school_id)

        self._executor.submit(_runner)

    def trigger_school(self, school_id: int, force: bool = False, mode: str = "recrawl") -> bool:
        """학교 크롤링을 즉시 트리거한다.

        원자적 DB try_start_crawl 검사 후 runner_callback 또는 스레드 풀로 _run_crawl 호출.
        """
        storage = self._get_storage()
        school = storage.get_school(school_id)
        if school is None:
            logger.warning("Trigger failed: school_id=%d not found", school_id)
            return False

        now = datetime.now(timezone.utc)
        job = self._jobs.get(school_id)

        if not force:
            updated = storage.try_start_crawl(school_id)
            if updated is None:
                if job:
                    job.last_status = "skipped"
                    # 문서 §5.1: skip 후에도 다음 주기로 시각을 갱신해 busy-retry를 막는다
                    self._advance_next_run(job, now)
                logger.info("Trigger skipped (already crawling/indexing): school_id=%d", school_id)
                return False

        if job:
            job.last_run_at = now
            job.run_count += 1
            job.last_status = "triggered"
            self._advance_next_run(job, now)

        try:
            self._dispatch_crawl(school_id, school.base_url, mode)
            return True
        except Exception as e:
            if job:
                self._apply_failure_backoff(job, now)
            logger.exception("Trigger error for school_id=%d: %s", school_id, e)
            return False

    def on_crawl_finished(self, school_id: int, *, success: bool) -> None:
        """파이프라인 완료 콜백. 실패 시 지수 백오프로 next_run_at을 재조정한다."""
        job = self._jobs.get(school_id)
        if job is None:
            return
        now = datetime.now(timezone.utc)
        if success:
            job.error_count = 0
            if job.last_status != "skipped":
                job.last_status = "triggered"
            return
        self._apply_failure_backoff(job, now)

    def check_and_run_due_jobs(self, now: datetime | None = None) -> list[int]:
        """next_run_at <= now 인 도래 작업들을 확인하고 트리거한다."""
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        triggered_ids = []
        for job in list(self._jobs.values()):
            if job.next_run_at and job.next_run_at <= now:
                success = self.trigger_school(job.school_id)
                if success:
                    triggered_ids.append(job.school_id)
        return triggered_ids

    def expire_stale_documents(
        self,
        school_id: int | None = None,
        miss_threshold: int | None = None,
    ) -> list[int]:
        """연속 미관측 횟수가 임계값을 넘는 문서를 만료 처리한다."""
        storage = self._get_storage()
        threshold = self.miss_threshold if miss_threshold is None else miss_threshold
        expired_ids = storage.expire_documents_by_miss_count(
            school_id=school_id,
            threshold=threshold,
        )
        if expired_ids:
            logger.info(
                "Expired %d documents (miss_threshold=%d, school_id=%s)",
                len(expired_ids),
                threshold,
                school_id,
            )
        return expired_ids

    async def start(self) -> None:
        """백그라운드 스케줄러 루프를 시작한다."""
        if self._running:
            return
        self._running = True
        self.sync_jobs_from_storage()
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        """백그라운드 스케줄러 루프를 종료한다."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._executor.shutdown(wait=False)
        logger.info("Scheduler stopped")

    def is_running(self) -> bool:
        return self._running

    def run_once(self) -> list[int]:
        """단발성 동기 주기 검사를 수행한다."""
        self.sync_jobs_from_storage()
        triggered = self.check_and_run_due_jobs()
        self.expire_stale_documents()
        return triggered

    async def _loop(self) -> None:
        ticks = 0
        while self._running:
            try:
                if ticks % self.sync_every_ticks == 0:
                    self.sync_jobs_from_storage()
                self.check_and_run_due_jobs()
                if ticks % self.expire_every_ticks == 0:
                    self.expire_stale_documents()
            except Exception as e:
                logger.exception("Error in scheduler loop: %s", e)
            ticks += 1
            await asyncio.sleep(self.check_interval)


_scheduler_instance: Scheduler | None = None


def get_scheduler() -> Scheduler:
    """Scheduler 싱글턴 반환."""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = Scheduler()
    return _scheduler_instance
