"""命令行入口。

排程员视角的只读/操作命令，输出中文工作清单：

* ``explain``   —— 每份样本为何拆给某些方法、被拒方法原因、分样是否守恒；
* ``schedule``  —— 生成/续排可执行计划，显示前处理批、校准、时段与预计完成；
* ``status``    —— 每份样本的任务、余量与可能完成时间；
* ``invalidations`` —— 哪些结果因哪个质控、何种原因失效；
* ``supersede`` —— 标准换版预览（默认）或应用，显示未开检任务如何移动。

无参数且标准输入有 JSON 时保持早期的 JSON 单次请求模式。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import demo
from .api import handle
from .service import LabError, Service
from .store import Store


# ---------------------------------------------------------------------------
# 展示辅助
# ---------------------------------------------------------------------------


def short(iso: str) -> str:
    if not iso:
        return "—"
    return iso.replace("T", " ").replace("+00:00", "Z")


def line(char: str = "-", width: int = 78) -> str:
    return char * width


def render_explain(data: dict[str, Any]) -> str:
    out = [f"样本 {data['sample_id']}（基质：{data['matrix']}）方法选择说明",
           line("="),
           f"目标毒素：{'、'.join(data['target_toxins'])}",
           f"需分样合计 {data['sample_needed_g']:g} g，余量 {data['sample_remaining_g']:g} g，"
           f"守恒检查：{'通过' if data['conservation_ok'] else '不通过'}",
           ""]
    for choice in data["chosen"]:
        out.append(f"● 采用 {choice['method_key']}（{choice['title']}）")
        out.append(f"  覆盖：{'、'.join(choice['covered_toxins'])}；每份 {choice['aliquot_amount_g']:g} g")
        for reason in choice["reasons"]:
            out.append(f"  - {reason}")
        out.append("")
    if data["rejected"]:
        out.append("未采用的方法：")
        for rejected in data["rejected"]:
            out.append(f"  × {rejected['method_key']}：{rejected['reason']}")
    if data["uncovered_toxins"]:
        out.append(f"⚠ 无方法覆盖：{'、'.join(data['uncovered_toxins'])}")
    return "\n".join(out)


def render_schedule(data: dict[str, Any]) -> str:
    tag = "续排" if data.get("resumed") else "编排"
    out = [f"批次{tag}运行 {data['run_id']}：处理 {data['processed']} 份样本",
           line("=")]
    for entry in data["planned"]:
        reused = "（沿用既有计划，未重复消耗）" if entry.get("reused") else ""
        out.append(f"✔ 样本 {entry['sample_id']} 封签 {entry['seal_id']} 可执行{reused}")
        out.append(f"  方法：{' + '.join(entry['method_keys'])}")
        for task in entry["tasks"]:
            out.append(
                f"  任务 {task['task_id']}：{task['method_key']} 毒素[{ '、'.join(task['toxins']) }]")
            out.append(
                f"    前处理 {task['prep_id']}｜校准 {task['calib_id']}｜时段 {task['slot_id']}"
                f"｜人员 {task['analyst_id']}")
            out.append(f"    开检 {short(task['scheduled_start'])} → 完成 {short(task['scheduled_end'])}")
        out.append(f"  预计完成：{short(entry['expected_completion'])}；消耗分样 {entry['sample_used_g']:g} g")
        out.append("")
    for entry in data["blocked"]:
        out.append(f"✘ 样本 {entry['sample_id']} 封签 {entry['seal_id']} 无法安排")
        out.append(f"  原因：{entry['reason']}")
        for rejected in entry.get("rejected", []):
            out.append(f"  × {rejected['method_key']}：{rejected['reason']}")
        out.append("")
    if not data["planned"] and not data["blocked"]:
        out.append("没有需要编排的样本（隔离样本不参与）。")
    return "\n".join(out)


def render_status(rows: list[dict[str, Any]]) -> str:
    out = ["样本排程状态", line("=")]
    for row in rows:
        out.append(f"● {row['sample_id']} 封签 {row['seal_id']} [{row['state']}] 基质 {row['matrix']}")
        out.append(f"  目标：{'、'.join(row['target_toxins'])}｜送达期限 {short(row['deadline'])}")
        out.append(f"  分样：总量 {row['total_amount_g']:g} g，已分配 {row['allocated_g']:g} g，"
                   f"余量 {row['remaining_g']:g} g")
        if row["expected_completion"]:
            out.append(f"  可能完成时间：{short(row['expected_completion'])}")
        if row["completed_at"]:
            out.append(f"  已完成：{short(row['completed_at'])}")
        if row["blocked_reason"]:
            out.append(f"  ⚠ 无法安排：{row['blocked_reason']}")
        for task in row["tasks"]:
            out.append(
                f"    [{task['state']}] {task['task_id']} {task['method_key']} "
                f"{short(task['scheduled_start'])}~{short(task['scheduled_end'])}"
                + (f" 失效原因：{task['invalidated_reason']}" if task["invalidated_reason"] else "")
                + (f" 复测轮次 r{task['retest_round']}<-{task['retest_of']}" if task["retest_of"] else ""))
        out.append("")
    return "\n".join(out)


def render_invalidations(rows: list[dict[str, Any]]) -> str:
    out = ["失效结果清单（只列真正依赖失败质控的结果）", line("=")]
    if not rows:
        out.append("当前没有失效结果。")
    for row in rows:
        out.append(f"✘ 样本 {row['sample_id']} 封签 {row['seal_id']}｜任务 {row['task_id']}｜"
                   f"{row['toxin']} = {row['value']:g} {row['unit']}")
        out.append(f"  失效质控：{row['qc_id']}")
        out.append(f"  原因：{row['reason']}")
        if row["report_id"]:
            out.append(f"  已影响依法出具的报告 {row['report_id']}，须走更正链，不得删除原报告")
        out.append("")
    return "\n".join(out)


def render_chain(rows: list[dict[str, Any]]) -> str:
    out = ["报告与更正链（原报告保留不删除）", line("=")]
    for item in rows:
        report = item["report"]
        out.append(f"● 报告 {report['report_id']} [{report['state']}] "
                   f"样本 {report['sample_id']} 封签 {report['seal_id']}")
        out.append(f"  签发 {short(report['issued_at'])}｜依据任务 {'、'.join(report['task_ids'])}")
        out.append(f"  说明：{report['reason']}")
        for correction in item["corrections"]:
            out.append(f"  ↳ 更正链 {correction['correction_id']}：新报告 {correction['new_report_id']}"
                       f"（{correction['reason']}，关联质控 {correction['qc_id'] or '—'}）")
        out.append("")
    if not rows:
        out.append("尚无报告。")
    return "\n".join(out)


def render_supersede(preview: dict[str, Any], applied: bool = False) -> str:
    out = [f"标准换版：{preview['old_key']} → {preview['new_key']}"
           + ("（已应用）" if applied else "（预览，尚未落库）"), line("="),
           preview["note"], ""]
    for move in preview["movements"]:
        out.append(f"● 样本 {move['sample_id']}：{len(move['old_tasks'])} 个未开检任务将移动")
        for old in move["old_tasks"]:
            out.append(f"  旧 {old['task_id']} 时段 {old['slot_id']} 校准 {old['calib_id']} "
                       f"完成 {short(old['scheduled_end'])}")
        projection = move["projection"]
        if projection.get("feasible"):
            for task in projection.get("tasks", []):
                out.append(f"  新 {task['task_id']} 方法 {task['method_key']} 时段 {task['slot_id']} "
                           f"校准 {task['calib_id']} 完成 {short(task['scheduled_end'])}")
            out.append(f"  新版预计完成：{short(projection.get('expected_completion', ''))}")
        else:
            out.append(f"  ⚠ 换版后无法安排：{projection.get('reason', '')}")
        out.append("")
    out.append(f"不受影响（已开检/已完成，继续绑定旧版本）："
               f"{'、'.join(preview['untouched_started_tasks']) or '无'}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 命令构造
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="SQLite 数据库路径（默认取 TOXIN_LAB_DB 或 toxin_lab.db）")

    parser = argparse.ArgumentParser(prog="toxin-lab", description="多毒素检验批次编排")
    parser.add_argument("--db", default=None,
                        help="SQLite 数据库路径（默认取 TOXIN_LAB_DB 或 toxin_lab.db）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("seed", parents=[common], help="写入演示目录与样本")
    p_demo = sub.add_parser("demo", parents=[common], help="一键演示：写入数据、编排并打印计划")
    p_demo.add_argument("--fresh", action="store_true", help="删除已有数据库后重建")

    p_accept = sub.add_parser("accept", parents=[common], help="受理样本（封签去重/隔离自动判定）")
    p_accept.add_argument("--sample", required=True)
    p_accept.add_argument("--seal", required=True)
    p_accept.add_argument("--owner", required=True)
    p_accept.add_argument("--matrix", required=True)
    p_accept.add_argument("--amount", type=float, required=True, help="受理总量 g")
    p_accept.add_argument("--toxins", required=True, help="逗号分隔目标毒素")
    p_accept.add_argument("--deadline", required=True)
    p_accept.add_argument("--content-hash", required=True)

    sub.add_parser("quarantine", parents=[common], help="列出隔离样本")

    p_explain = sub.add_parser("explain", parents=[common], help="解释某样本为何采用这些方法")
    p_explain.add_argument("sample")

    p_sched = sub.add_parser("schedule", parents=[common], help="编排或续排")
    p_sched.add_argument("--samples", help="逗号分隔样本编号；缺省为全部待排")
    p_sched.add_argument("--run", default="RUN-1")

    p_status = sub.add_parser("status", parents=[common], help="样本/任务状态与预计完成")
    p_status.add_argument("sample", nargs="?")

    p_start = sub.add_parser("start", parents=[common], help="开检任务")
    p_start.add_argument("task")
    p_record = sub.add_parser("record", parents=[common], help="录入结果并完成任务")
    p_record.add_argument("task")
    p_record.add_argument("--values", required=True,
                          help="毒素=数值，分号分隔，如 黄曲霉毒素B1=2.1;赭曲霉毒素A=0.3")
    p_record.add_argument("--unit", default="μg/kg")

    p_qc = sub.add_parser("qc", parents=[common], help="评价质控（默认失败，--pass 为合格）")
    p_qc.add_argument("qc_id")
    p_qc.add_argument("--pass", dest="passed", action="store_true")
    p_qc.add_argument("--detail", default="")

    sub.add_parser("invalidations", parents=[common], help="列出失效结果及原因")

    p_retest = sub.add_parser("retest", parents=[common], help="对失效任务发起复测")
    p_retest.add_argument("task")
    p_retest.add_argument("--reason", default="质控失效复测")

    p_report = sub.add_parser("report", parents=[common], help="出具报告（再次出具须更正理由）")
    p_report.add_argument("sample")
    p_report.add_argument("--correction-reason", default="")
    p_chain = sub.add_parser("chain", parents=[common], help="查看报告更正链")
    p_chain.add_argument("sample", nargs="?")

    p_super = sub.add_parser("supersede", parents=[common], help="标准换版预览；--apply 应用")
    p_super.add_argument("old_key")
    p_super.add_argument("--version", default=None, help="新版本号（缺省自动递增）")
    p_super.add_argument("--toxins", default=None, help="逗号分隔，覆盖新版毒素组合")
    p_super.add_argument("--aliquot", type=float, default=None)
    p_super.add_argument("--run-minutes", type=int, default=None)
    p_super.add_argument("--apply", action="store_true")

    sub.add_parser("resources", parents=[common], help="查看方法/人员/时段/校准余量")
    return parser


def _service(ns: argparse.Namespace) -> Service:
    import os
    path = ns.db or os.environ.get("TOXIN_LAB_DB", "toxin_lab.db")
    return Service(Store(path))


def _parse_values(raw: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value = pair.partition("=")
        values[key.strip()] = float(value)
    return values


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv and not sys.stdin.isatty():
        print(handle(sys.stdin.read().strip() or '{"action":"health"}'))
        return 0

    parser = build_parser()
    ns = parser.parse_args(argv)
    if not ns.command:
        parser.print_help()
        return 0

    try:
        service = _service(ns)
        if ns.command == "seed":
            print(json.dumps(demo.seed(service), ensure_ascii=False))
        elif ns.command == "demo":
            if ns.fresh:
                import os
                db_path = ns.db or os.environ.get("TOXIN_LAB_DB", "toxin_lab.db")
                if os.path.exists(db_path):
                    os.remove(db_path)
                service = _service(ns)
            print(json.dumps(demo.seed(service), ensure_ascii=False))
            print(render_schedule(service.schedule(run_id="DEMO")))
            for sample_id in ("SMP-001", "SMP-002", "SMP-003", "SMP-004"):
                print()
                print(render_explain(service.explain(sample_id)))
        elif ns.command == "accept":
            result = service.accept_sample(
                ns.sample, ns.seal, ns.owner, ns.matrix, ns.amount,
                [t.strip() for t in ns.toxins.split(",") if t.strip()],
                ns.deadline, ns.content_hash)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif ns.command == "quarantine":
            rows = service.list_quarantine()
            print(json.dumps(rows, ensure_ascii=False, indent=2) if rows else "无隔离样本。")
        elif ns.command == "explain":
            print(render_explain(service.explain(ns.sample)))
        elif ns.command == "schedule":
            ids = [s.strip() for s in ns.samples.split(",")] if ns.samples else None
            print(render_schedule(service.schedule(ids, ns.run)))
        elif ns.command == "status":
            print(render_status(service.sample_status(ns.sample or "")))
        elif ns.command == "start":
            print(json.dumps(service.start_task(ns.task), ensure_ascii=False))
        elif ns.command == "record":
            print(json.dumps(service.record_results(ns.task, _parse_values(ns.values), ns.unit),
                             ensure_ascii=False, indent=2))
        elif ns.command == "qc":
            print(json.dumps(service.evaluate_qc(ns.qc_id, ns.passed, ns.detail),
                             ensure_ascii=False, indent=2))
        elif ns.command == "invalidations":
            print(render_invalidations(service.invalidation_list()))
        elif ns.command == "retest":
            print(json.dumps(service.retest(ns.task, ns.reason), ensure_ascii=False, indent=2))
        elif ns.command == "report":
            print(json.dumps(service.issue_report(ns.sample, ns.correction_reason),
                             ensure_ascii=False, indent=2))
        elif ns.command == "chain":
            print(render_chain(service.report_chain(ns.sample or "")))
        elif ns.command == "supersede":
            new_fields: dict[str, Any] = {}
            if ns.version:
                new_fields["version"] = ns.version
            if ns.toxins:
                new_fields["toxins"] = [t.strip() for t in ns.toxins.split(",") if t.strip()]
            if ns.aliquot is not None:
                new_fields["aliquot_amount_g"] = ns.aliquot
            if ns.run_minutes is not None:
                new_fields["run_minutes"] = ns.run_minutes
            if ns.apply:
                applied = service.apply_supersede(ns.old_key, new_fields)
                preview = dict(applied["preview"])
                preview["cancelled"] = applied["cancelled_open_tasks"]
                print(render_supersede(preview, applied=True))
                print()
                print(render_schedule(applied["replanned"]))
            else:
                print(render_supersede(service.preview_supersede(ns.old_key, new_fields)))
        elif ns.command == "resources":
            for calib in service.store.list_calibrations():
                print(f"校准 {calib.calib_id} {calib.method_key} 仪器{calib.instrument_id} "
                      f"[{calib.state}] 额度 {calib.used}/{calib.capacity} 效期至 {short(calib.expires_at)}")
            for slot in service.store.list_slots():
                print(f"时段 {slot.slot_id} 仪器{slot.instrument_id} 人员{slot.analyst_id} "
                      f"{short(slot.start_at)}~{short(slot.end_at)} 占用 {slot.used}/{slot.capacity}")
    except LabError as exc:
        print(f"业务错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
