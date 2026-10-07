# 多毒素检验批次编排库

区域实验室改用多毒素同时测定后，一份封签样本不再对应一项检验：它要按分样余量
覆盖不同毒素组合，食品基质限定可选方法，校准物有效期、仪器时段、监管送达期限
同时逼近，质控失败还要精确作废并复测。

本库用纯 Python 标准库 + SQLite 给出可执行、可追溯、可续排的批次编排内核，
并提供进程内 JSON 边界与命令行视图。排程员可直接从命令行获知：

* 每份样本**为何**采用某个方法版本、何时**可能完成**；
* 哪些结果**因何失效**、复测如何另立；
* 标准**换版**会怎样移动尚未开检的任务。

## 领域规则如何落地

| 约束 / 诉求 | 实现 |
| --- | --- |
| 样本拆分始终守恒 | 分样创建即从净样量预留；未取消分样量之和 ≤ 净样量，DB 有 `CHECK`，服务层预留前强校验 |
| 多方法联合覆盖目标组合 | 覆盖搜索（回溯）选出最少方法、最少分样量的组合，每法只负责与目标的交集 |
| 基质限定方法 | 方法声明适用基质集合，不匹配的方法不进入候选 |
| 校准物有效期 / 额度 | 编排时校验有效期与余额并按份额预留；开检前再次复核有效期，过期不得开检 |
| 前处理批次 / 仪器时段 | 前处理方式必须匹配窗口；时段须在前处理结束后、送达期限前；容量实时扣减 |
| 人员授权 | 授权范围按 `方法@版本` 精确匹配；提供排班名单时无授权即阻塞 |
| 监管送达期限 | 最早可完成时间晚于期限即阻塞并给出原因 |
| 关键质控同批次可追溯 | 汇编批次时同建空白/校准核查，质控与样本结果共享 `batch_id` |
| 质控失败精确失效 | 仅「同批次 + 分析物落在失败作用域」的有效结果失效，精确到分析物；他批次不受影响 |
| 复测规则 | 仅失效分析物可复测；旧分样已消耗不回收，复测另取分样、重受余量与额度约束 |
| 报告不删除 | 已出具报告永久保留为 `corrected`，更正沿 `chain_root` 出具新报告 |
| 相同封签重送 | 封签 + 内容指纹一致：沿用原受理、不另立检验；指纹不同：**立即隔离** |
| 中断续排 | 资源预留写幂等台账；`plan`/`reschedule` 重放不二次消耗样本或校准额度 |
| 标准换版 | 只移动 `planned` 任务；已开检/已完结不溯及；迁移前后时间、前处理、时段均留痕 |

### 任务生命周期

```
planned ──开检──► in_progress ──录入结果──► resulted
   │                   ▲                        │
   │ 标准换版/取消       │ 续排                    │ 质控按分析物失效
   ▼                   │                        ├─ 部分失效：保持 resulted，失效子集复测
 blocked ──reschedule──┘                         └─ 全部失效：rework ──► 新 planned 任务
```

## 运行

```bash
python3 -m unittest discover -s tests     # 运行测试（无需第三方依赖）
python3 -m compileall -q src              # 语法检查
```

指定一个文件库即可跨进程持久化（默认内存库）：

```bash
export TOXIN_LAB_DB=/tmp/lab.db
# 或每次加 --db /tmp/lab.db
```

## JSON 请求

所有请求为单行 JSON，必填 `action`。写请求可带 `request_id` 做幂等去重。

```bash
printf '%s' '{"action":"health"}' | PYTHONPATH=src python3 -m toxin_lab.cli --db /tmp/lab.db
```

### 1. 建立主数据

```jsonc
{"action":"add_method","method_id":"LCMS-A","version":"1",
 "analytes":["AFB1","AFB2","DON"],"matrices":["corn","peanut"],
 "sample_mass_g":10,"prep_code":"QuEChERS","cal_id":"CAL-A",
 "cal_units_per_sample":1,"runtime_min":30}

{"action":"add_calibrator","cal_id":"CAL-A",
 "expires_at":"2026-12-31T00:00:00+00:00","total_units":10}

{"action":"add_analyst","analyst_id":"ana-1","scope":["LCMS-A@1"]}

{"action":"add_prep","prep_id":"prep-a1","prep_code":"QuEChERS",
 "start":"2026-10-10T08:00:00+00:00","end":"2026-10-10T10:00:00+00:00","capacity":2}

{"action":"add_slot","slot_id":"slot-a1","instrument":"LCMS-01","method_id":"LCMS-A",
 "start":"2026-10-10T10:00:00+00:00","end":"2026-10-10T11:00:00+00:00","capacity":2}
```

### 2. 受理（重送沿用 / 内容不符隔离）

```jsonc
{"action":"accept_sample","sample_id":"S-1","seal_id":"SEAL-1","content_hash":"h1",
 "matrix":"corn","target_analytes":["AFB1","DON","ZEA"],
 "mass_g":50,"due_at":"2026-10-20T18:00:00+00:00"}
```

同封签同指纹再次受理 → `state=accepted` 且 `first_sample_id` 指向首样本，不另立检验；
同封签异指纹 → `state=quarantined`，附 `quarantine_reason`，不得编排。

### 3. 编排与续排

```jsonc
{"action":"plan","plan_key":"run-2026-10-07","analyst_ids":["ana-1"]}
{"action":"reschedule"}
```

成功计划的每个任务都带 `reasons`（为何此方法、何时完成、用哪份校准/窗口/时段/人员）
与 `eta`；无法落位的样本进入 `blocked` 并列出每条阻塞原因，且**不预留任何资源**。
资源补齐后 `reschedule` 续排；重复调用安全。

### 4. 开检、批次、结果、质控

```jsonc
{"action":"start_task","task_id":"..."}
{"action":"assemble_batch","task_ids":["..."],"batch_id":"B-1"}
{"action":"record_results","task_id":"...","values":{"AFB1":1.5,"DON":0.3}}

{"action":"decide_qc","qc_id":"B-1:blank","passed":false,
 "failed_analytes":["AFB1"],"reason":"空白 AFB1 超限"}
```

质控默认一次判定终局；报告出具后依法复判需显式 `"redecide":true`，全程留痕。
复判翻案为合格会恢复仅因该质控失效、尚未复测的结果。

### 5. 复测与更正报告

```jsonc
{"action":"request_rework","task_id":"...","analytes":["AFB1"]}
{"action":"correct_after_qc","sample_id":"S-1","rework_task_id":"...",
 "values":{"AFB1":0.9}}
```

更正报告 `supersedes_report` 指向上一份，`chain_root` 指向链首；旧报告状态变
`corrected` 但不删除，原失效结果保留为 `reissued` 可追溯。

### 6. 标准换版

```jsonc
{"action":"supersede_method","old_ref":"LCMS-A@1",
 "new_spec":{"version":"2","prep_code":"QuEChERS-v2","cal_id":"CAL-A2"}}
```

返回 `moved`（含每条任务迁移前后的时间/窗口/时段）、`untouched`（已开检等，附原因）、
`blocked`（新版本下无法落位、等待续排）。

## 命令行人读视图

```bash
python3 -m toxin_lab.cli --db /tmp/lab.db status      # 资源与任务总览
python3 -m toxin_lab.cli --db /tmp/lab.db explain S-1 # 为何此法/何时完成/何结果失效/报告链
python3 -m toxin_lab.cli --db /tmp/lab.db events      # 审计事件流（可 --kind 过滤）
python3 -m toxin_lab.cli --db /tmp/lab.db changes     # 标准换版移动了什么
```

## 代码结构

- `domain.py`：不可变/轻变领域对象与状态常量。
- `store.py`：SQLite 表结构、资源台账 `resource_ledger`、幂等回执、可重入事务。
- `service.py`：全部业务规则（受理守恒、覆盖编排、续排、质控失效、复测、报告链、换版）。
- `api.py`：JSON 动作分发、统一错误、请求幂等回执。
- `cli.py`：JSON 入口与人读渲染。
- `tests/`：基线行为 + 29 项业务规则测试。
