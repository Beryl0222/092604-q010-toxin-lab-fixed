"""提供可替换的业务时钟。"""
from datetime import datetime, timezone


class Clock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()
