# 多毒素检验批次编排

本项目提供多毒素检验批次编排的服务端基础，已有代码包含基础登记对象、可替换时钟、SQLite 本地保存、健康检查、JSON 请求入口和命令行调用。领域模块保持小而清晰，便于继续形成完整业务流程。

## 目录

- `src/toxin_lab/domain.py` 保存基础领域对象。
- `src/toxin_lab/store.py` 管理 SQLite 表结构和事务写入。
- `src/toxin_lab/service.py` 组织登记与查询行为。
- `src/toxin_lab/api.py` 提供进程内 JSON 请求边界。
- `tests/` 覆盖当前已有行为。

## 运行

运行测试：`python3 -m pytest -q`

检查源码：`python3 -m compileall src`

本地冒烟：`printf '%s' '{"action":"health"}' | PYTHONPATH=src python3 -m toxin_lab.cli`

项目只使用 Python 标准库，运行期间不连接其他服务。
