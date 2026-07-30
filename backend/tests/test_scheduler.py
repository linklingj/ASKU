"""backend/app/scheduler.py 단위 및 연동 테스트 suite."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.models import School
from app.scheduler import (
    KEYWORD_SCHEDULES,
    Scheduler,
    SchoolJob,
    calculate_next_run,
    parse_schedule,
)


def test_parse_schedule_keywords():
    """키워드(hourly, daily, weekly, monthly, disabled) 파싱 검증."""
    for kw, expected_sec in KEYWORD_SCHEDULES.items():
        res = parse_schedule(kw)
        assert res["type"] == "keyword"
        assert res["keyword"] == kw
        assert res["seconds"] == expected_sec

    # 대소문자 혼합 검증
    res_upper = parse_schedule("DAILY")
    assert res_upper["type"] == "keyword"
    assert res_upper["seconds"] == 86400

    # disabled / None / 빈 문자열
    assert parse_schedule(None)["type"] == "disabled"
    assert parse_schedule("disabled")["type"] == "disabled"
    assert parse_schedule("")["type"] == "disabled"


def test_parse_schedule_intervals():
    """상대 시간(30s, 15m, 12h, 3d, 3600) 파싱 검증."""
    assert parse_schedule("30s") == {"type": "interval", "seconds": 30}
    assert parse_schedule("15m") == {"type": "interval", "seconds": 900}
    assert parse_schedule("12h") == {"type": "interval", "seconds": 43200}
    assert parse_schedule("3d") == {"type": "interval", "seconds": 259200}
    assert parse_schedule("3600") == {"type": "interval", "seconds": 3600}


def test_parse_schedule_cron():
    """5필드 Cron 표현식 파싱 검증."""
    res = parse_schedule("0 0 * * *")
    assert res["type"] == "cron"
    assert res["expression"] == "0 0 * * *"
    assert res["fields"]["minute"] == "0"
    assert res["fields"]["hour"] == "0"


def test_parse_schedule_invalid():
    """잘못된 주기 표현식 파싱시 ValueError 발생 검증."""
    with pytest.raises(ValueError, match="유효하지 않은 crawl_schedule 표현식"):
        parse_schedule("invalid_schedule_format")


def test_calculate_next_run():
    """다음 실행 시각 계산 검증."""
    base_time = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)

    # disabled
    assert calculate_next_run("disabled", base_time) is None

    # daily (+24h)
    next_daily = calculate_next_run("daily", base_time)
    assert next_daily == datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)

    # 1h (+1h)
    next_1h = calculate_next_run("1h", base_time)
    assert next_1h == datetime(2026, 7, 30, 13, 0, 0, tzinfo=timezone.utc)

    # cron (매시 30분 '30 * * * *')
    next_cron = calculate_next_run("30 * * * *", base_time)
    assert next_cron == datetime(2026, 7, 30, 12, 30, 0, tzinfo=timezone.utc)


def test_scheduler_register_and_unregister():
    """스케줄러 작업 등록, 조회, 해제 검증."""
    scheduler = Scheduler(check_interval=0.1)

    # 작업 등록
    job = scheduler.register_school(school_id=1, crawl_schedule="daily")
    assert job is not None
    assert job.school_id == 1
    assert job.crawl_schedule == "daily"
    assert job.next_run_at is not None

    # 등록 확인
    assert scheduler.get_job(1) == job
    assert len(scheduler.list_jobs()) == 1

    # disabled 등록시 작업 제거
    scheduler.register_school(school_id=1, crawl_schedule="disabled")
    assert scheduler.get_job(1) is None
    assert len(scheduler.list_jobs()) == 0

    # 작업 해제
    scheduler.register_school(school_id=2, crawl_schedule="hourly")
    assert scheduler.unregister_school(2) is True
    assert scheduler.unregister_school(999) is False


def test_scheduler_sync_from_storage():
    """Storage 데이터베이스 학교 목록과의 작업 동기화 검증."""
    mock_storage = MagicMock()
    mock_storage.list_schools.return_value = [
        School(school_id=1, name="연세대학교", base_url="https://yonsei.ac.kr", crawl_schedule="daily"),
        School(school_id=2, name="세종대학교", base_url="https://sejong.ac.kr", crawl_schedule="weekly"),
        School(school_id=3, name="홍익대학교", base_url="https://hongik.ac.kr", crawl_schedule=None),
    ]

    scheduler = Scheduler(storage=mock_storage)
    count = scheduler.sync_jobs_from_storage()

    assert count == 2
    assert scheduler.get_job(1) is not None
    assert scheduler.get_job(2) is not None
    assert scheduler.get_job(3) is None


def test_scheduler_trigger_school_success():
    """학교 트리거 성공 시 상태 업데이트 및 콜백 호출 검증."""
    mock_storage = MagicMock()
    mock_school = School(school_id=1, name="연세대학교", base_url="https://yonsei.ac.kr", crawl_schedule="daily")
    mock_storage.get_school.return_value = mock_school
    mock_storage.try_start_crawl.return_value = mock_school

    runner_callback = MagicMock()
    scheduler = Scheduler(storage=mock_storage, runner_callback=runner_callback)
    scheduler.register_school(school_id=1, crawl_schedule="daily")

    success = scheduler.trigger_school(school_id=1)

    assert success is True
    runner_callback.assert_called_once_with(1, "https://yonsei.ac.kr", "recrawl")
    job = scheduler.get_job(1)
    assert job.run_count == 1
    assert job.last_status == "triggered"


def test_scheduler_trigger_school_conflict_skip():
    """이미 크롤링/인덱싱 중인 경우 트리거 건너뜀 검증."""
    mock_storage = MagicMock()
    mock_school = School(school_id=1, name="연세대학교", base_url="https://yonsei.ac.kr", crawl_schedule="daily")
    mock_storage.get_school.return_value = mock_school
    mock_storage.try_start_crawl.return_value = None  # crawling 진행 중

    runner_callback = MagicMock()
    scheduler = Scheduler(storage=mock_storage, runner_callback=runner_callback)
    scheduler.register_school(school_id=1, crawl_schedule="daily")

    success = scheduler.trigger_school(school_id=1, force=False)

    assert success is False
    runner_callback.assert_not_called()
    job = scheduler.get_job(1)
    assert job.last_status == "skipped"


def test_scheduler_check_and_run_due_jobs():
    """실행 시각이 도래한 작업만 선별하여 실행하는지 검증."""
    mock_storage = MagicMock()
    mock_school1 = School(school_id=1, name="연세대학교", base_url="https://yonsei.ac.kr", crawl_schedule="10s")
    mock_school2 = School(school_id=2, name="세종대학교", base_url="https://sejong.ac.kr", crawl_schedule="1d")
    mock_storage.get_school.side_effect = lambda sid: mock_school1 if sid == 1 else mock_school2
    mock_storage.try_start_crawl.side_effect = lambda sid: mock_school1 if sid == 1 else mock_school2

    runner_callback = MagicMock()
    scheduler = Scheduler(storage=mock_storage, runner_callback=runner_callback)

    past_time = datetime.now(timezone.utc) - timedelta(seconds=20)
    future_time = datetime.now(timezone.utc) + timedelta(days=1)

    job1 = scheduler.register_school(1, "10s")
    job1.next_run_at = past_time

    job2 = scheduler.register_school(2, "1d")
    job2.next_run_at = future_time

    now = datetime.now(timezone.utc)
    triggered = scheduler.check_and_run_due_jobs(now=now)

    assert triggered == [1]
    runner_callback.assert_called_once_with(1, "https://yonsei.ac.kr", "recrawl")


def test_scheduler_async_lifespan():
    """Async 백그라운드 스케줄러 시작 및 정지 라이프사이클 검증."""
    import asyncio

    async def _runner():
        mock_storage = MagicMock()
        mock_storage.list_schools.return_value = []

        scheduler = Scheduler(storage=mock_storage, check_interval=0.05)
        assert scheduler.is_running() is False

        await scheduler.start()
        assert scheduler.is_running() is True

        await asyncio.sleep(0.1)

        await scheduler.stop()
        assert scheduler.is_running() is False

    asyncio.run(_runner())
