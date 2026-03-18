from __future__ import annotations

import calendar
import threading
from datetime import datetime
from typing import Callable

from .config import ScheduledTaskConfig
from .workspace import RuntimeConfigManager


class CronTaskScheduler:
    def __init__(
        self,
        config_manager: RuntimeConfigManager,
        on_trigger: Callable[[ScheduledTaskConfig], None],
        log: Callable[[str, str], None],
    ):
        self._config_manager = config_manager
        self._on_trigger = on_trigger
        self._log = log
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_triggered: dict[str, str] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)

    def _loop(self) -> None:
        interval = 15
        while not self._stop_event.is_set():
            try:
                config = self._config_manager.reload()
                self._check_tasks(config.scheduled_tasks)
                interval = max(5, int(config.scheduler_check_interval_seconds))
            except Exception as error:  # noqa: BLE001
                self._log("定时", f"检查失败：{error}")
            self._stop_event.wait(interval)

    def _check_tasks(self, tasks) -> None:
        if not tasks:
            return

        now = datetime.now().replace(second=0, microsecond=0)
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        for index, task in enumerate(tasks):
            if not task.enabled:
                continue
            if not _cron_matches(task.cron, now):
                continue
            task_key = f"{index}:{task.name}:{task.cron}"
            if self._last_triggered.get(task_key) == minute_key:
                continue
            self._last_triggered[task_key] = minute_key
            self._log("定时", f"触发任务：{task.name}")
            self._on_trigger(task)


def _cron_matches(expression: str, now: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron expression: {expression}")

    minute, hour, day, month, weekday = fields
    day_match = _field_matches(day, now.day, 1, 31)
    weekday_match = _weekday_matches(weekday, now)
    if day != "*" and weekday != "*":
        day_ok = day_match or weekday_match
    elif day != "*":
        day_ok = day_match
    elif weekday != "*":
        day_ok = weekday_match
    else:
        day_ok = True

    return (
        _field_matches(minute, now.minute, 0, 59)
        and _field_matches(hour, now.hour, 0, 23)
        and _field_matches(month, now.month, 1, 12, aliases=_MONTH_ALIASES)
        and day_ok
    )


def _weekday_matches(field: str, now: datetime) -> bool:
    cron_weekday = (now.weekday() + 1) % 7
    return _field_matches(field, cron_weekday, 0, 7, aliases=_WEEKDAY_ALIASES)


def _field_matches(field: str, value: int, min_value: int, max_value: int, *, aliases: dict[str, int] | None = None) -> bool:
    normalized_field = field.strip().lower()
    if normalized_field == "*":
        return True

    for part in normalized_field.split(","):
        if _part_matches(part, value, min_value, max_value, aliases=aliases):
            return True
    return False


def _part_matches(part: str, value: int, min_value: int, max_value: int, *, aliases: dict[str, int] | None = None) -> bool:
    if "/" in part:
        base, step_text = part.split("/", 1)
        step = int(step_text)
        if step <= 0:
            raise ValueError(f"Invalid cron step: {part}")
        values = _expand_base(base, min_value, max_value, aliases=aliases)
        return value in values[::step]

    return value in _expand_base(part, min_value, max_value, aliases=aliases)


def _expand_base(part: str, min_value: int, max_value: int, *, aliases: dict[str, int] | None = None) -> list[int]:
    if part == "*":
        return list(range(min_value, max_value + 1))
    if "-" in part:
        start_text, end_text = part.split("-", 1)
        start = _parse_field_value(start_text, aliases=aliases)
        end = _parse_field_value(end_text, aliases=aliases)
        if start > end:
            raise ValueError(f"Invalid cron range: {part}")
        return [_normalize_weekday(item, max_value) for item in range(start, end + 1)]
    single = _parse_field_value(part, aliases=aliases)
    return [_normalize_weekday(single, max_value)]


def _parse_field_value(raw_value: str, *, aliases: dict[str, int] | None = None) -> int:
    text = raw_value.strip().lower()
    if aliases and text in aliases:
        return aliases[text]
    return int(text)


def _normalize_weekday(value: int, max_value: int) -> int:
    if max_value == 7 and value == 7:
        return 0
    return value


_MONTH_ALIASES = {name.lower(): index for index, name in enumerate(calendar.month_abbr) if name}
_WEEKDAY_ALIASES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}
