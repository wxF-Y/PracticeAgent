"""tests/test_tasks.py — core/tasks.py 单元测试"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.tasks import (
    cancel_task,
    get_due_tasks,
    get_task_by_name,
    list_tasks,
    mark_task_running,
    next_cron_time,
    parse_cron,
    schedule_task,
    update_task_after_run,
)


# ── cron 解析 ─────────────────────────────────────────────────────────────────

def test_parse_cron_star():
    mins, hours, doms, months, dows = parse_cron("* * * * *")
    assert 0 in mins and 59 in mins
    assert 0 in hours and 23 in hours


def test_parse_cron_specific():
    mins, hours, doms, months, dows = parse_cron("30 9 * * 1-5")
    assert mins == {30}
    assert hours == {9}
    assert dows == {1, 2, 3, 4, 5}


def test_parse_cron_step():
    mins, *_ = parse_cron("*/15 * * * *")
    assert mins == {0, 15, 30, 45}


def test_parse_cron_list():
    mins, *_ = parse_cron("0,15,30,45 * * * *")
    assert mins == {0, 15, 30, 45}


def test_parse_cron_invalid_fields():
    with pytest.raises(ValueError):
        parse_cron("* * * *")  # 只有 4 字段


def test_next_cron_time_returns_future():
    now = time.time()
    nxt = next_cron_time("* * * * *", after=now)
    # 结果应在下一分钟内（最多 120 秒）
    assert nxt > now
    assert nxt - now <= 120


def test_next_cron_time_9am():
    # 找到下一个 9:00
    nxt = next_cron_time("0 9 * * *")
    import time as _time
    s = _time.localtime(nxt)
    assert s.tm_hour == 9
    assert s.tm_min == 0


# ── CRUD ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_cwd(tmp_path):
    """返回一个隔离的临时工作目录（tasks.jsonl 写入其中）。"""
    return tmp_path


def test_schedule_one_shot(tmp_cwd):
    rec = schedule_task(tmp_cwd, "test-one", "do something", delay_seconds=60)
    assert rec.name == "test-one"
    assert rec.status == "pending"
    assert rec.cron is None
    assert rec.next_run_at is not None
    assert rec.next_run_at > time.time()


def test_schedule_cron(tmp_cwd):
    rec = schedule_task(tmp_cwd, "daily", "push log", cron="0 9 * * *")
    assert rec.cron == "0 9 * * *"
    assert rec.status == "pending"


def test_schedule_same_name_replaces(tmp_cwd):
    schedule_task(tmp_cwd, "dup", "v1", delay_seconds=10)
    schedule_task(tmp_cwd, "dup", "v2", delay_seconds=20)
    tasks = list_tasks(tmp_cwd)
    assert len(tasks) == 1
    assert tasks[0].prompt == "v2"


def test_list_tasks(tmp_cwd):
    schedule_task(tmp_cwd, "a", "pa", delay_seconds=60)
    schedule_task(tmp_cwd, "b", "pb", delay_seconds=120)
    tasks = list_tasks(tmp_cwd)
    assert len(tasks) == 2
    names = {t.name for t in tasks}
    assert names == {"a", "b"}


def test_cancel_task(tmp_cwd):
    schedule_task(tmp_cwd, "to-cancel", "p", delay_seconds=60)
    ok = cancel_task(tmp_cwd, "to-cancel")
    assert ok is True
    tasks = list_tasks(tmp_cwd)
    assert tasks[0].status == "cancelled"


def test_cancel_nonexistent(tmp_cwd):
    ok = cancel_task(tmp_cwd, "ghost")
    assert ok is False


def test_get_task_by_name(tmp_cwd):
    schedule_task(tmp_cwd, "find-me", "p", delay_seconds=60)
    r = get_task_by_name(tmp_cwd, "find-me")
    assert r is not None
    assert r.name == "find-me"


def test_get_task_by_name_missing(tmp_cwd):
    assert get_task_by_name(tmp_cwd, "no-such") is None


def test_get_due_tasks_immediate(tmp_cwd):
    # delay_seconds=0 → next_run_at = now → 应立即到期
    schedule_task(tmp_cwd, "now-task", "p", delay_seconds=0)
    due = get_due_tasks(tmp_cwd)
    assert any(t.name == "now-task" for t in due)


def test_get_due_tasks_future_not_due(tmp_cwd):
    schedule_task(tmp_cwd, "future", "p", delay_seconds=3600)
    due = get_due_tasks(tmp_cwd)
    assert not any(t.name == "future" for t in due)


def test_mark_task_running(tmp_cwd):
    rec = schedule_task(tmp_cwd, "runner", "p", delay_seconds=0)
    mark_task_running(tmp_cwd, rec.task_id)
    tasks = list_tasks(tmp_cwd)
    assert tasks[0].status == "running"


def test_update_task_after_run_one_shot_success(tmp_cwd):
    rec = schedule_task(tmp_cwd, "once", "p", delay_seconds=0)
    update_task_after_run(tmp_cwd, rec.task_id, success=True)
    tasks = list_tasks(tmp_cwd)
    assert tasks[0].status == "done"
    assert tasks[0].run_count == 1
    assert tasks[0].next_run_at is None


def test_update_task_after_run_one_shot_failure(tmp_cwd):
    rec = schedule_task(tmp_cwd, "fail-once", "p", delay_seconds=0)
    update_task_after_run(tmp_cwd, rec.task_id, success=False, error="boom")
    tasks = list_tasks(tmp_cwd)
    assert tasks[0].status == "failed"
    assert tasks[0].last_error == "boom"


def test_update_task_after_run_cron_requeues(tmp_cwd):
    rec = schedule_task(tmp_cwd, "cron-task", "p", cron="* * * * *")
    update_task_after_run(tmp_cwd, rec.task_id, success=True)
    tasks = list_tasks(tmp_cwd)
    assert tasks[0].status == "pending"
    assert tasks[0].next_run_at is not None
    assert tasks[0].next_run_at > time.time()
    assert tasks[0].run_count == 1


def test_schedule_invalid_cron(tmp_cwd):
    with pytest.raises(ValueError):
        schedule_task(tmp_cwd, "bad", "p", cron="not a cron")
