"""UTC scheduling helpers for durable jobs and company heartbeat entries."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_local(value: str, timezone_name: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(UTC)


def next_interval(interval_seconds: int, now: datetime | None = None) -> datetime:
    return (now or utc_now()) + timedelta(seconds=int(interval_seconds))


def next_cron(expression: str, timezone_name: str,
              now: datetime | None = None) -> datetime:
    try:
        from croniter import croniter
    except ImportError as exc:
        raise RuntimeError("cron schedules require `pip install croniter`") from exc
    local_now = (now or utc_now()).astimezone(ZoneInfo(timezone_name))
    return croniter(expression, local_now).get_next(datetime).astimezone(UTC)


def initial_due(schedule_type: str, schedule_value: str, timezone_name: str,
                now: datetime | None = None) -> datetime:
    current = now or utc_now()
    if schedule_type == "once":
        return parse_local(schedule_value, timezone_name)
    if schedule_type == "interval":
        return next_interval(int(schedule_value), current)
    if schedule_type == "cron":
        return next_cron(schedule_value, timezone_name, current)
    raise ValueError(f"invalid schedule type: {schedule_type}")


def advance_due(schedule_type: str, schedule_value: str, timezone_name: str,
                now: datetime | None = None) -> datetime | None:
    """Advance after a run, skipping directly over any offline backlog."""
    if schedule_type == "once":
        return None
    return initial_due(schedule_type, schedule_value, timezone_name, now)
