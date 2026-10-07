"""进程内 JSON 请求边界。

请求为单行 JSON，必填 ``action``。携带 ``request_id`` 的写请求按
``(action, request_id)`` 去重：重复提交直接返回首次结果，配合服务层
资源台账共同保证中断重放不二次消耗样本与校准额度。
"""
from __future__ import annotations

import hashlib
import json

from .service import PlanningError, Service


def handle(raw: str, service: Service | None = None) -> str:
    current = service or Service()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _err("请求不是合法 JSON", "bad_json", str(exc))
    if not isinstance(body, dict):
        return _err("请求体必须是 JSON 对象", "bad_request")
    action = body.get("action")

    request_id = body.get("request_id")
    receipt_key = None
    if request_id is not None:
        receipt_key = f"{action}:{request_id}"
        cached = current.store.get_receipt(receipt_key)
        if cached is not None:
            return cached

    try:
        result = _dispatch(action, body, current)
    except PlanningError as exc:
        return _err(str(exc), "planning_error")
    except KeyError as exc:
        return _err(f"缺少字段: {exc.args[0]}", "missing_field")
    except (TypeError, ValueError) as exc:
        return _err(str(exc), "bad_request")

    text = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if receipt_key is not None:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with current.store.tx():
            current.store.put_receipt(receipt_key, digest, text)
    return text


def _dispatch(action: str, body: dict, service: Service):
    if action == "health":
        return service.health()
    if action == "register":
        return service.register(str(body["record_id"]), str(body["owner_id"]))
    if action == "find":
        result = service.find(str(body["record_id"]))
        return result if result is not None else {"found": False}

    if action == "add_method":
        return service.add_method(body)
    if action == "add_calibrator":
        return service.add_calibrator(body)
    if action == "add_analyst":
        return service.add_analyst(body)
    if action == "add_slot":
        return service.add_slot(body)
    if action == "add_prep":
        return service.add_prep(body)

    if action == "accept_sample":
        return service.accept_sample(body)
    if action == "plan":
        return service.plan(
            plan_key=str(body.get("plan_key", "default")),
            sample_ids=body.get("sample_ids"),
            analyst_ids=body.get("analyst_ids"),
        )
    if action == "reschedule":
        return service.reschedule_blocked()

    if action == "start_task":
        return service.start_task(str(body["task_id"]))
    if action == "assemble_batch":
        return service.assemble_batch(
            [str(t) for t in body["task_ids"]],
            batch_id=body.get("batch_id"),
            qc_kinds=body.get("qc_kinds"),
        )
    if action == "record_results":
        return service.record_results(
            str(body["task_id"]), dict(body["values"]),
            unit=str(body.get("unit", "ug/kg")),
        )
    if action == "decide_qc":
        return service.decide_qc(
            str(body["qc_id"]), bool(body["passed"]),
            failed_analytes=body.get("failed_analytes"),
            reason=body.get("reason"),
            redecide=bool(body.get("redecide", False)),
        )
    if action == "request_rework":
        return service.request_rework(
            str(body["task_id"]), analytes=body.get("analytes")
        )

    if action == "issue_report":
        return service.issue_report(str(body["sample_id"]))
    if action == "correct_after_qc":
        return service.correct_after_qc(
            str(body["sample_id"]), str(body["rework_task_id"]),
            dict(body["values"]), unit=str(body.get("unit", "ug/kg")),
        )
    if action == "supersede_method":
        return service.supersede_method(str(body["old_ref"]), dict(body["new_spec"]))

    if action == "explain":
        return service.explain_sample(str(body["sample_id"]))
    if action == "events":
        return {"events": service.events(body.get("kind"))}
    if action == "status":
        return _status(service)

    raise ValueError(f"不支持的请求动作: {action}")


def _status(service: Service) -> dict:
    store = service.store
    return {
        "samples": [service._sample_dict(s) for s in store.list_samples()],
        "tasks_state": _count(store, "tasks", "state"),
        "calibrators": [service._cal_dict(c) for c in store.list_calibrators()],
        "preps": [service._prep_dict(p) for p in store.list_preps()],
        "slots": [service._slot_dict(s) for s in store.list_slots()],
        "methods": [service._method_dict(m) for m in store.list_methods()],
    }


def _count(store, table: str, column: str) -> dict[str, int]:
    rows = store.connection.execute(
        f"SELECT {column} AS k, COUNT(*) AS n FROM {table} GROUP BY {column}"
    )
    return {r["k"]: r["n"] for r in rows}


def _err(message: str, code: str, detail: str | None = None) -> str:
    payload = {"ok": False, "error": message, "code": code}
    if detail:
        payload["detail"] = detail
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
