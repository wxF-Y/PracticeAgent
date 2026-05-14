"""后台任务调度：一次性任务和 cron 定时任务。

数据模型：
  TaskRecord  — 单条任务（name, prompt, cron, status, …）

持久化：
  ~/.practiceagent/tasks/<project-hash>/tasks.jsonl
  每条记录是一个 JSON 行；更新/删除时全量重写（tmp+rename 保证原子性）。

Cron 解析：
  支持标准 5 字段 "min hour dom month dow"（0-59, 0-23, 1-31, 1-12, 0-6）。
  支持 *  /step  a-b  a,b,c 四种格式。
  不依赖第三方库。

并发：
  每个 JSONL 文件一把 threading.Lock，所有读-改-写操作都在锁内完成。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass
from hashlib import sha1
from pathlib import Path
from uuid import uuid4

_DATA_DIR = Path.home() / ".practiceagent"
_TASKS_ROOT = _DATA_DIR / "tasks"

_file_locks: dict[Path, threading.Lock] = {}
_file_locks_mu = threading.Lock()


def _get_file_lock(path: Path) -> threading.Lock:
    with _file_locks_mu:
        if path not in _file_locks:
            _file_locks[path] = threading.Lock()
        return _file_locks[path]


# ── 路径帮助 ───────────────────────────────────────────────────────────────────

def _project_tasks_dir(cwd: Path) -> Path:
    digest = sha1(str(cwd.resolve()).encode()).hexdigest()[:12]
    d = _TASKS_ROOT / f"{cwd.resolve().name}-{digest}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tasks_file(cwd: Path) -> Path:
    return _project_tasks_dir(cwd) / "tasks.jsonl"


# ── 数据模型 ──────────────────────────────────────────────────────────────────

TASK_STATUSES = ("pending", "running", "done", "failed", "cancelled")


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    name: str
    prompt: str
    cron: str | None          # "0 9 * * *" 或 None
    status: str               # pending / running / done / failed / cancelled
    created_at: float
    last_run_at: float | None = None
    next_run_at: float | None = None
    run_count: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TaskRecord":
        return cls(
            task_id=d["task_id"],
            name=d["name"],
            prompt=d["prompt"],
            cron=d.get("cron"),
            status=d.get("status", "pending"),
            created_at=d["created_at"],
            last_run_at=d.get("last_run_at"),
            next_run_at=d.get("next_run_at"),
            run_count=d.get("run_count", 0),
            last_error=d.get("last_error"),
        )


# ── Cron 解析 ─────────────────────────────────────────────────────────────────

def _parse_field(expr: str, lo: int, hi: int) -> set[int]:
    """把单个 cron 字段解析为整数集合。"""
    result: set[int] = set()
    try:
        for part in expr.split(","):
            if "/" in part:
                base, step_s = part.split("/", 1)
                step = int(step_s)
                if step <= 0:
                    raise ValueError(f"step 必须 > 0，得到 {step}")
                if base == "*":
                    for v in range(lo, hi + 1, step):
                        result.add(v)
                elif "-" in base:
                    a, b = base.split("-", 1)
                    for v in range(int(a), int(b) + 1, step):
                        result.add(v)
                else:
                    for v in range(int(base), hi + 1, step):
                        result.add(v)
            elif "-" in part:
                a, b = part.split("-", 1)
                result.update(range(int(a), int(b) + 1))
            elif part == "*":
                result.update(range(lo, hi + 1))
            else:
                result.add(int(part))
    except (ValueError, OverflowError) as e:
        raise ValueError(f"无效的 cron 字段 {expr!r}：{e}") from e
    return result


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """返回 (mins, hours, doms, months, dows)。"""
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"cron 必须是 5 字段，得到：{expr!r}")
    mins   = _parse_field(fields[0], 0, 59)
    hours  = _parse_field(fields[1], 0, 23)
    doms   = _parse_field(fields[2], 1, 31)
    months = _parse_field(fields[3], 1, 12)
    dows   = _parse_field(fields[4], 0, 6)
    return mins, hours, doms, months, dows


def next_cron_time(expr: str, after: float | None = None) -> float:
    """计算 cron 表达式在 after 时间戳之后的下一个触发时间（本地时间）。"""
    mins, hours, doms, months, dows = parse_cron(expr)
    t = after if after is not None else time.time()
    t = (int(t) // 60 + 1) * 60
    for _ in range(527040):  # 最多搜一年
        s = time.localtime(t)
        if (s.tm_mon in months
                and s.tm_mday in doms
                and s.tm_wday in dows
                and s.tm_hour in hours
                and s.tm_min in mins):
            return t
        t += 60
    raise ValueError(f"在一年内找不到下一次触发时间：{expr!r}")


# ── 内部 IO（调用方持锁后调用）────────────────────────────────────────────────

def _read_all_unlocked(path: Path) -> list[TaskRecord]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(TaskRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f"[tasks] 跳过损坏行：{e}", file=sys.stderr)
    return records


def _write_all_unlocked(path: Path, records: list[TaskRecord]) -> None:
    content = "\n".join(json.dumps(r.to_dict(), ensure_ascii=False) for r in records) + "\n"
    # 原子写：先写临时文件，再 rename
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ── CRUD（每个函数在锁内完成读-改-写）────────────────────────────────────────

def schedule_task(
    cwd: Path,
    name: str,
    prompt: str,
    cron: str | None = None,
    delay_seconds: float | None = None,
) -> TaskRecord:
    """创建并持久化一个任务；同名旧任务被替换。"""
    if cron:
        parse_cron(cron)  # 提前验证格式
        nxt = next_cron_time(cron)
    elif delay_seconds is not None:
        nxt = time.time() + delay_seconds
    else:
        nxt = time.time()

    path = _tasks_file(cwd)
    record = TaskRecord(
        task_id=uuid4().hex,
        name=name,
        prompt=prompt,
        cron=cron,
        status="pending",
        created_at=time.time(),
        next_run_at=nxt,
    )
    with _get_file_lock(path):
        records = _read_all_unlocked(path)
        records = [r for r in records if r.name != name]
        records.append(record)
        _write_all_unlocked(path, records)
    return record


def list_tasks(cwd: Path) -> list[TaskRecord]:
    path = _tasks_file(cwd)
    with _get_file_lock(path):
        return _read_all_unlocked(path)


def get_task_by_name(cwd: Path, name: str) -> TaskRecord | None:
    # 前缀匹配仅限于看起来像 uuid hex 的字符串（>=8 字符且全是十六进制）
    looks_like_id = len(name) >= 8 and all(c in "0123456789abcdef" for c in name.lower())
    path = _tasks_file(cwd)
    with _get_file_lock(path):
        for r in _read_all_unlocked(path):
            if r.name == name:
                return r
            if looks_like_id and r.task_id.startswith(name.lower()):
                return r
    return None


def cancel_task(cwd: Path, name: str) -> bool:
    looks_like_id = len(name) >= 8 and all(c in "0123456789abcdef" for c in name.lower())
    path = _tasks_file(cwd)
    with _get_file_lock(path):
        records = _read_all_unlocked(path)
        found = False
        updated = []
        for r in records:
            if r.name == name or (looks_like_id and r.task_id.startswith(name.lower())):
                updated.append(TaskRecord(
                    task_id=r.task_id, name=r.name, prompt=r.prompt, cron=r.cron,
                    status="cancelled", created_at=r.created_at,
                    last_run_at=r.last_run_at, next_run_at=None,
                    run_count=r.run_count, last_error=r.last_error,
                ))
                found = True
            else:
                updated.append(r)
        if found:
            _write_all_unlocked(path, updated)
    return found


def update_task_after_run(
    cwd: Path,
    task_id: str,
    success: bool,
    error: str | None = None,
) -> None:
    """触发完成后更新状态和下次执行时间。cron 解析失败时标记 failed。"""
    path = _tasks_file(cwd)
    with _get_file_lock(path):
        records = _read_all_unlocked(path)
        updated = []
        for r in records:
            if r.task_id != task_id:
                updated.append(r)
                continue
            now = time.time()
            if r.cron:
                try:
                    nxt = next_cron_time(r.cron, after=now)
                    new_status = "pending"
                    new_error = None if success else error
                except ValueError as e:
                    nxt = None
                    new_status = "failed"
                    new_error = f"cron 解析失败：{e}"
            else:
                nxt = None
                new_status = "done" if success else "failed"
                new_error = None if success else error
            updated.append(TaskRecord(
                task_id=r.task_id, name=r.name, prompt=r.prompt, cron=r.cron,
                status=new_status, created_at=r.created_at,
                last_run_at=now, next_run_at=nxt,
                run_count=r.run_count + 1, last_error=new_error,
            ))
        _write_all_unlocked(path, updated)


def mark_task_running(cwd: Path, task_id: str) -> None:
    """将任务状态更新为 running（原子操作，用于调度器"领取"任务）。"""
    path = _tasks_file(cwd)
    with _get_file_lock(path):
        records = _read_all_unlocked(path)
        updated = []
        for r in records:
            if r.task_id == task_id:
                updated.append(TaskRecord(
                    task_id=r.task_id, name=r.name, prompt=r.prompt, cron=r.cron,
                    status="running", created_at=r.created_at,
                    last_run_at=r.last_run_at, next_run_at=r.next_run_at,
                    run_count=r.run_count, last_error=r.last_error,
                ))
            else:
                updated.append(r)
        _write_all_unlocked(path, updated)


def get_due_tasks(cwd: Path) -> list[TaskRecord]:
    """返回状态为 pending 且 next_run_at <= now 的任务。"""
    now = time.time()
    path = _tasks_file(cwd)
    with _get_file_lock(path):
        return [
            r for r in _read_all_unlocked(path)
            if r.status == "pending"
            and r.next_run_at is not None
            and r.next_run_at <= now
        ]
