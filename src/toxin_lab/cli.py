"""命令行入口。

两种用法：

1. JSON 请求（兼容基线）：``printf '%s' '{...}' | python -m toxin_lab.cli --db lab.db``
2. 只读人读视图：``python -m toxin_lab.cli --db lab.db explain S-1`` /
   ``events`` / ``status`` / ``changes``

排程员通过 ``explain`` 直接看到：每份样本为何采用某方法版本、何时可能完成、
哪些结果因何失效；``changes`` 展示标准换版如何移动尚未开检的任务。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .api import handle
from .service import Service
from .store import Store


def _build_service(db: str | None) -> Service:
    path = db or os.environ.get("TOXIN_LAB_DB", ":memory:")
    return Service(Store(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toxin_lab", description="多毒素检验批次编排")
    parser.add_argument("--db", help="SQLite 数据库路径（默认 :memory: 或环境变量 TOXIN_LAB_DB）")
    parser.add_argument("--format", choices=("json", "text"), default="json",
                        help="JSON 请求模式下的输出格式，默认 json")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="总览资源与任务状态")
    p_explain = sub.add_parser("explain", help="解释单份样本的编排、失效与报告")
    p_explain.add_argument("sample_id")
    p_events = sub.add_parser("events", help="查看审计事件流")
    p_events.add_argument("--kind")
    sub.add_parser("changes", help="查看标准换版导致的任务移动")

    args = parser.parse_args(argv)
    service = _build_service(args.db)

    if args.command is None:
        raw = sys.stdin.read().strip() or '{"action":"health"}'
        out = handle(raw, service)
        if args.format == "text":
            try:
                print(render(json.loads(out)))
            except json.JSONDecodeError:
                print(out)
        else:
            print(out)
        return 0

    if args.command == "explain":
        print(render(service.explain_sample(args.sample_id)))
    elif args.command == "status":
        print(_render_status(json.loads(handle(json.dumps({"action": "status"}), service))))
    elif args.command == "events":
        print(_render_events(service.events(args.kind)))
    elif args.command == "changes":
        print(_render_changes(service.events("method.supersede")))
    return 0


# ---------------------------------------------------------------------------
# 人读渲染
# ---------------------------------------------------------------------------

_STATE_CN = {
    "planned": "待开检", "blocked": "受阻", "in_progress": "检验中",
    "resulted": "已出结果", "invalid": "已失效", "rework": "待复测",
    "canceled": "已取消", "accepted": "已受理", "quarantined": "已隔离",
    "valid": "有效", "reissued": "已由更正取代",
    "issued": "现行", "corrected": "已被更正",
    "pass": "合格", "fail": "不合格", "pending": "待判定",
}


def _cn(state: str | None) -> str:
    return _STATE_CN.get(state or "", state or "-")


def render(payload: dict) -> str:
    if "target_analytes" in payload and "tasks" in payload:
        return _render_explain(payload)
    if "planned" in payload and "blocked" in payload:
        return _render_plan(payload)
    if "moved" in payload and "old" in payload:
        return _render_supersede(payload)
    if "qc_id" in payload and "invalidated" in payload:
        return _render_qc(payload)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _render_explain(s: dict) -> str:
    lines = [
        f"样本 {s['sample_id']}（封签 {s['seal_id']}）状态：{_cn(s['state'])}",
        f"  基质：{s['matrix']}    目标分析物：{', '.join(s['target_analytes']) or '（无）'}",
        f"  净样 {s['mass_g']:g}g，已预留 {s['reserved_mass']:g}g，"
        f"可用余量 {s['available_mass']:g}g",
        f"  监管送达期限：{s['due_at']}",
    ]
    if s.get("first_sample_id"):
        lines.append(f"  ※ 相同封签重送，沿用原受理 {s['first_sample_id']}，不另立检验")
    if s.get("quarantine_reason"):
        lines.append(f"  ※ 已隔离：{s['quarantine_reason']}")
    if not s["tasks"]:
        lines.append("  （尚无编排任务）")
    for t in s["tasks"]:
        lines.append("")
        lines.append(
            f"  任务 {t['task_id']}  {_cn(t['state'])}"
            f"  方法 {t['method_ref']}"
        )
        lines.append(f"    覆盖分析物：{', '.join(t['analytes'])}")
        if t["eta"]:
            lines.append(f"    计划开始 {t['planned_start']} → 预计完成 {t['eta']}")
        if t.get("analyst_id"):
            lines.append(f"    授权检验员：{t['analyst_id']}")
        for reason in t["reasons"]:
            lines.append(f"    · {reason}")
        for reason in t["blocking"]:
            lines.append(f"    ✗ 阻塞：{reason}")
        for qc in t.get("qc", []):
            mark = "✓" if qc["state"] == "pass" else ("✗" if qc["state"] == "fail" else "?")
            lines.append(f"    {mark} 质控 {qc['kind']}：{_cn(qc['state'])}"
                         + (f"（{qc['fail_reason']}）" if qc.get("fail_reason") else ""))
        for r in t.get("results", []):
            value = "未检出" if r["value"] is None else f"{r['value']:g} {r['unit']}"
            line = f"    结果 {r['analyte']}：{value} [{_cn(r['state'])}]"
            if r["state"] == "invalid":
                line += f" —— 因质控 {r['invalidated_by_qc']} 失效：{r['invalid_reason']}"
            lines.append(line)
    if s.get("reports"):
        lines.append("")
        lines.append("  报告链（旧报告不删除）：")
        for r in s["reports"]:
            arrow = f" → 更正自 {r['supersedes_report']}" if r["supersedes_report"] else ""
            lines.append(f"    {r['report_id']} {_cn(r['state'])} 签发 {r['issued_at']}"
                         f"（{r['reason']}）{arrow}")
    return "\n".join(lines)


def _render_plan(p: dict) -> str:
    lines = [f"编排轮次 {p['plan_key']}：成功 {len(p['planned'])} 份，受阻 {len(p['blocked'])} 份"]
    for item in p["planned"]:
        tag = "（沿用既有计划）" if item.get("reused") else ""
        lines.append(f"  样本 {item['sample_id']}{tag}")
        for t in item["tasks"]:
            lines.append(f"    {t['task_id']} {t['method_ref']} → 预计完成 {t['eta']}")
            for reason in t["reasons"]:
                lines.append(f"      · {reason}")
    for b in p["blocked"]:
        lines.append(f"  样本 {b['sample_id']} 受阻：")
        for reason in b["blocking"]:
            lines.append(f"      ✗ {reason}")
    return "\n".join(lines)


def _render_qc(q: dict) -> str:
    lines = [f"质控 {q['qc_id']}：{_cn(q['state'])}"]
    for r in q["invalidated"]:
        lines.append(f"  失效结果 {r['result_id']}（样本 {r['sample_id']} "
                     f"{r['analyte']}）—— {r['invalid_reason']}")
    if not q["invalidated"]:
        lines.append("  无结果受其影响")
    return "\n".join(lines)


def _render_supersede(p: dict) -> str:
    lines = [f"标准换版 {p['old']} → {p['new']}"]
    for m in p["moved"]:
        b, a = m["before"], m["after"]
        lines.append(f"  移动 {m['task_id']}：预计完成 {b['eta']} → {a['eta']}，"
                     f"前处理 {b['prep_id']} → {a['prep_id']}，"
                     f"时段 {b['slot_id']} → {a['slot_id']}")
    for u in p["untouched"]:
        lines.append(f"  不动 {u['task_id']}（{_cn(u['state'])}）：{u['reason']}")
    for b in p["blocked"]:
        lines.append(f"  待续排 {b['task_id']}：{'；'.join(b['blocking'])}")
    return "\n".join(lines)


def _render_status(s: dict) -> str:
    lines = ["实验室编排总览"]
    lines.append("  任务状态：" + "，".join(f"{_cn(k)} {v}" for k, v in
                 sorted(s["tasks_state"].items())))
    for c in s["calibrators"]:
        lines.append(f"  校准物 {c['cal_id']}：余 {c['remaining_units']:g}/"
                     f"{c['total_units']:g}，有效期至 {c['expires_at']}")
    for p in s["preps"]:
        lines.append(f"  前处理 {p['prep_id']}（{p['prep_code']}）："
                     f"{p['used']}/{p['capacity']}，{p['start']} → {p['end']}")
    for sl in s["slots"]:
        lines.append(f"  时段 {sl['slot_id']}（{sl['instrument']}，{sl['method_id']}）："
                     f"{sl['used']}/{sl['capacity']}，{sl['start']} → {sl['end']}")
    for smp in s["samples"]:
        lines.append(f"  样本 {smp['sample_id']}：{_cn(smp['state'])}，"
                     f"余量 {smp['available_mass']:g}g，期限 {smp['due_at']}")
    return "\n".join(lines)


def _render_events(events: list[dict]) -> str:
    return "\n".join(
        f"#{e['seq']} {e['at']} {e['kind']} "
        f"{json.dumps(e['payload'], ensure_ascii=False, sort_keys=True)}"
        for e in events
    ) or "（无事件）"


def _render_changes(events: list[dict]) -> str:
    if not events:
        return "（尚无标准换版记录）"
    blocks = []
    for e in events:
        p = e["payload"]
        lines = [f"{e['at']}  {p['old']} → {p['new']}"]
        lines.append(f"  移动 {p['moved']} 个未开检任务；"
                     f"已开检不动 {p['untouched']} 个；待续排 {p['blocked']} 个")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


if __name__ == "__main__":
    raise SystemExit(main())
