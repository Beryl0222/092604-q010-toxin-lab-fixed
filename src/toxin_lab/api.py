"""进程内 JSON 请求边界。

请求形如 ``{"action": "schedule", "run_id": "RUN-1"}``；
业务违例返回 ``{"ok": false, "error": "..."}``，不抛出到调用方之外。
服务可通过环境变量 ``TOXIN_LAB_DB`` 指定 SQLite 文件，默认进程内内存库。
"""
from __future__ import annotations

import json
import os
from typing import Any

from .service import LabError, Service
from .store import Store


def service_for_path(path: str | None = None) -> Service:
    db = path or os.environ.get("TOXIN_LAB_DB", ":memory:")
    return Service(Store(db))


def handle(raw: str, service: Service | None = None) -> str:
    current = service or service_for_path()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _fail(f"请求不是合法 JSON：{exc}")
    if not isinstance(body, dict):
        return _fail("请求体必须是 JSON 对象")
    action = body.get("action")
    try:
        result = _dispatch(action, body, current)
    except LabError as exc:
        return _fail(str(exc))
    except KeyError as exc:
        return _fail(f"请求缺少必填字段：{exc.args[0]}")
    except (TypeError, ValueError) as exc:
        return _fail(f"请求参数不合法：{exc}")
    if isinstance(result, dict) and "ok" in result:
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    return json.dumps({"ok": True, "action": action, "result": result},
                      ensure_ascii=False, sort_keys=True)


def _fail(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False, sort_keys=True)


def _dispatch(action: str, body: dict[str, Any], service: Service) -> Any:
    args = {k: v for k, v in body.items() if k != "action"}

    if action == "health":
        return service.health()
    if action == "register":
        return service.register(str(body["record_id"]), str(body["owner_id"]))
    if action == "find":
        return service.find(str(body["record_id"]))

    if action == "accept_sample":
        return service.accept_sample(**args)
    if action == "quarantine":
        return service.list_quarantine()

    if action == "add_method":
        return service.add_method(**args)
    if action == "add_analyst":
        return service.add_analyst(**args)
    if action == "add_slot":
        return service.add_slot(**args)
    if action == "add_prep_batch":
        return service.add_prep_batch(**args)
    if action == "add_calibration":
        return service.add_calibration(**args)

    if action == "explain":
        return service.explain(str(body["sample_id"]))
    if action == "schedule":
        return service.schedule(
            sample_ids=body.get("sample_ids"),
            run_id=str(body.get("run_id", "RUN-1")))
    if action == "status":
        return service.sample_status(str(body["sample_id"]) if body.get("sample_id") else "")

    if action == "start_task":
        return service.start_task(str(body["task_id"]))
    if action == "record_results":
        return service.record_results(
            str(body["task_id"]), dict(body["values"]),
            unit=str(body.get("unit", "μg/kg")))
    if action == "evaluate_qc":
        return service.evaluate_qc(str(body["qc_id"]), bool(body["passed"]),
                                   str(body.get("detail", "")))
    if action == "invalidations":
        return service.invalidation_list()
    if action == "retest":
        return service.retest(str(body["task_id"]), str(body.get("reason", "质控失效复测")))

    if action == "issue_report":
        return service.issue_report(
            str(body["sample_id"]), str(body.get("correction_reason", "")))
    if action == "report_chain":
        return service.report_chain(
            str(body.get("sample_id", "")), str(body.get("report_id", "")))

    if action == "preview_supersede":
        return service.preview_supersede(str(body["old_key"]), dict(body.get("new_method", {})))
    if action == "apply_supersede":
        return service.apply_supersede(str(body["old_key"]), dict(body.get("new_method", {})))

    if action == "seed":
        from . import demo
        return demo.seed(service, str(body.get("base", "2026-10-07T08:00:00+00:00")))

    if action == "resources":
        kind = str(body.get("kind", "all"))
        out: dict[str, Any] = {}
        if kind in ("methods", "all"):
            out["methods"] = [m.__dict__ for m in service.store.list_methods()]
        if kind in ("analysts", "all"):
            out["analysts"] = [a.__dict__ for a in service.store.list_analysts()]
        if kind in ("slots", "all"):
            out["slots"] = [s.__dict__ for s in service.store.list_slots()]
        if kind in ("calibrations", "all"):
            out["calibrations"] = [c.__dict__ for c in service.store.list_calibrations()]
        if kind in ("preps", "all"):
            out["preps"] = [p.__dict__ for p in service.store.list_preps()]
        if kind in ("qc", "all"):
            out["qc"] = [q.__dict__ for q in service.store.list_qc()]
        return out

    raise LabError(f"不支持的请求动作：{action}")
