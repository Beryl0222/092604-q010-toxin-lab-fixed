"""处理进程内 JSON 请求。"""
import json

from .service import Service


def handle(raw: str, service: Service | None = None) -> str:
    current = service or Service()
    body = json.loads(raw)
    action = body.get("action")
    if action == "health":
        result = current.health()
    elif action == "register":
        result = current.register(str(body["record_id"]), str(body["owner_id"]))
    elif action == "find":
        result = current.find(str(body["record_id"]))
    else:
        raise ValueError("不支持的请求动作")
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
