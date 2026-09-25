"""从标准输入接收 JSON 请求。"""
import sys

from .api import handle


def main() -> int:
    raw = sys.stdin.read().strip() or '{"action":"health"}'
    print(handle(raw))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
