# 多毒素检验批次编排库

区域实验室采用多毒素同时测定方法后，一份受理样本往往要按分样余量覆盖多个毒素组合：
食品基质限制方法选择，校准物有效期、仪器时段、人员授权与监管送达期限同时逼近。
本库把这些约束收敛为一个可执行、可恢复、可追溯的批次编排器，仅依赖 Python 标准库。

## 解决的核心问题

- **样本拆分始终守恒**：每份测试的取走量落在分样台账上，`Σ已分配 ≤ 受理总量`；
  余量实时汇总，取消尚未开检的任务即原样还回。
- **方法选择可解释**：基质适用范围 + 目标毒素覆盖联合约束，贪心覆盖最少分样；
  每个被采用/被拒绝的方法都给出中文理由。
- **六类资源联合排程**：方法标准版本、基质适用、目标组合、前处理批、
  校准额度与效期、仪器时段、人员授权、复测规则共同决定可执行计划与预计完成时间。
- **质控与样本同批可追溯**：质控挂在分析批（校准批）上；质控失败只失效真正依赖该批的
  任务与结果，未开检任务自动释放资源等待重排，其他分析批结果不受影响。
- **依法出具的报告不删除**：再次出具只追加更正链，原报告标记“已被更正”并保留。
- **封签规则**：相同封签且内容指纹一致的重送沿用原受理结果，不重复消耗；
  封签一致但内容不同立即隔离，不进入任何计划。
- **中断可续排**：资源占用全部以 `task_id` 为唯一键（分样/校准/时段/前处理席位），
  重复或中断后的调度不会再次消耗样本或校准额度。
- **标准换版**：只移动尚未开检的任务；已开检/已完成任务永久绑定旧版本。
  预览给出每个样本任务如何移动，应用时延续持证授权与在效校准，并对全部受影响样本对账重排。

## 目录结构

| 文件 | 职责 |
| --- | --- |
| `src/toxin_lab/domain.py` | 不可变领域对象与状态常量 |
| `src/toxin_lab/clock.py` | 业务时钟（可固定/推进）与 ISO 时刻运算 |
| `src/toxin_lab/store.py` | SQLite 表结构、事务、以 task_id 为键的占用台账 |
| `src/toxin_lab/service.py` | 受理、方法选择、排程、质控、复测、报告、换版全部规则 |
| `src/toxin_lab/api.py` | 进程内 JSON 请求边界（统一中文错误） |
| `src/toxin_lab/cli.py` | 排程员命令行：解释、编排、状态、失效、更正链、换版 |
| `src/toxin_lab/demo.py` | 固定时钟下的可复现演示场景 |
| `tests/` | 37 个用例，覆盖上述全部规则 |

## 快速开始

```bash
# 运行测试（无第三方依赖）
PYTHONPATH=src python3 -m unittest discover -s tests

# 语法检查
python3 -m compileall -q src

# 一键演示：建目录、受理、编排并逐份解释方法选择
PYTHONPATH=src python3 -m toxin_lab.cli --db lab.db demo
```

演示输出会展示：单一方法样本、需拆成两个毒素组合的样本、基质不适用样本、
余量紧张样本，以及每份样本“前处理批 / 校准 / 仪器时段 / 持证人员 / 预计完成”。

## 命令行

```bash
toxin-lab --db lab.db accept --sample SMP-9 --seal SEAL-9 --owner 委托方甲 \
    --matrix 大米 --amount 20 --toxins 黄曲霉毒素B1,呕吐毒素 \
    --deadline 2026-10-12T00:00:00+00:00 --content-hash h9
toxin-lab --db lab.db explain SMP-9        # 为何选这些方法、分样是否守恒
toxin-lab --db lab.db schedule             # 编排；中断后重复执行即为续排
toxin-lab --db lab.db status [SMP-9]       # 余量、任务、可能完成时间、阻塞原因
toxin-lab --db lab.db start  <任务号>
toxin-lab --db lab.db record <任务号> --values '黄曲霉毒素B1=2.1;呕吐毒素=0.3'
toxin-lab --db lab.db qc QC-CAL-CAL-A-1 --detail '加标回收超差'   # 默认失败
toxin-lab --db lab.db invalidations        # 哪些结果因何失效、影响哪份报告
toxin-lab --db lab.db retest <失效任务号>
toxin-lab --db lab.db report SMP-9 --correction-reason '校准批失败，复测后更正'
toxin-lab --db lab.db chain  SMP-9         # 报告与更正链
toxin-lab --db lab.db supersede STD-A@2023 --version 2024          # 仅预览
toxin-lab --db lab.db supersede STD-A@2023 --aliquot 4 --apply    # 应用换版
toxin-lab --db lab.db quarantine           # 封签异常隔离清单
toxin-lab --db lab.db resources            # 校准额度/效期、时段占用
```

无参数且管道传入 JSON 时，CLI 保持单次 JSON 请求模式：

```bash
printf '%s' '{"action":"health"}' | PYTHONPATH=src python3 -m toxin_lab.cli
```

## JSON API

所有请求形如 `{"action": "...", ...}`，成功返回
`{"ok": true, "action": ..., "result": ...}`，业务违例返回
`{"ok": false, "error": "中文原因"}`。主要动作：

`accept_sample` `add_method` `add_analyst` `add_slot` `add_prep_batch`
`add_calibration` `explain` `schedule` `status` `start_task` `record_results`
`evaluate_qc` `invalidations` `retest` `issue_report` `report_chain`
`preview_supersede` `apply_supersede` `quarantine` `resources` `seed`。

数据库可用 `Service(Store("lab.db"))` 指定，或环境变量 `TOXIN_LAB_DB`。

## 关键规则的实现位置

- 守恒与幂等占用：`store.py` 的 `allocations / calib_uses / slot_bookings /
  prep_memberships`（均以 `task_id` 为主键，`INSERT OR IGNORE`），余量实时汇总。
- 方法选择解释：`service.py` 的 `_select_methods / explain`。
- 资源可行组合：`service.py` 的 `_assign_resources`（前处理批凑批 → 授权时段 →
  在效且有额度的同仪器校准 → 期限校验）。
- 失效范围：`service.py` 的 `evaluate_qc`，沿 `calib_uses` 依赖链传播。
- 更正链：`service.py` 的 `issue_report` 与 `reports / corrections` 表。
- 换版：`preview_supersede`（dry-run 投影）与 `apply_supersede`（取消未开检任务、
  延续授权与校准、对账重排）。

## 状态一览

- 样本：已受理 / 已隔离 / 已关闭
- 方法：现行 / 被替代 / 废止（任务永久绑定 `标准号@版本`）
- 任务：待开检 / 开检中 / 已完成 / 已失效 / 已取消 / 无法安排
- 结果：已记录 / 已失效（携带失效质控编号与原因）
- 质控：待评价 / 合格 / 失败
- 报告：草稿 / 已签发 / 已被更正（原报告保留，更正链追加）
