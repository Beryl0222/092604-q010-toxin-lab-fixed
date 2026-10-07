"""提供可替换的业务时钟与 ISO 时刻运算。"""
from datetime import datetime, timedelta, timezone


def _as_utc(value: str) -> datetime:
    """解析 ISO 字符串；无时区者按 UTC 处理，便于跨环境比较。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Clock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def add_minutes(self, iso: str, minutes: int) -> str:
        return (_as_utc(iso) + timedelta(minutes=minutes)).isoformat()

    def minutes_between(self, start: str, end: str) -> int:
        return int((_as_utc(end) - _as_utc(start)).total_seconds() // 60)

    def is_before(self, left: str, right: str) -> bool:
        return _as_utc(left) < _as_utc(right)

    def is_after(self, left: str, right: str) -> bool:
        return _as_utc(left) > _as_utc(right)


class FixedClock(Clock):
    """固定在某一业务时刻的时钟，用于可复现的演示与测试。"""

    def __init__(self, fixed_at: str) -> None:
        self._fixed = _as_utc(fixed_at).isoformat()

    def now(self) -> str:
        return self._fixed

    def advance(self, minutes: int) -> str:
        self._fixed = self.add_minutes(self._fixed, minutes)
        return self._fixed


def max_iso(left: str, right: str) -> str:
    """两个 ISO 时刻取较晚者（兼容格式差异，统一按 UTC 比较）。"""
    return left if _as_utc(left) >= _as_utc(right) else right
