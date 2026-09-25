# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤；校准工单另支持`?overdue=true`（可配`?as_of=YYYY-MM-DD`，默认今天）筛出已过计划完成日仍未回填的工单。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`；动作可带`Idempotency-Key`请求头，重复提交沿用首次响应且不重复写审计。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 送检与回填流程

1. 计量员（metrology）对`active`仪器执行`send_calibration`，必须登记`assignee`（承担人）、`planned_finish_at`（计划完成日）和`purpose`（用途）。仪器随即进入`calibrating`（待校准），系统同时生成一张`requested`校准工单，仪器数据记录`calibration_id`。
2. 校准结果未回填期间放行检测结果（result `release`）会被退回（400），错误信息携带待处理工单编号，检测结果保持`pending`。
3. 待回填工单过了计划完成日即可通过`?overdue=true`筛出（计划日当天不算逾期）。
4. 回填（calibration `perform`）：
   - `result="passed"`必须带`due_at`（新到期日）；工单转为`passed`，仪器恢复`active`并写入新到期日。
   - `result="failed"`必须带`disposition`（处置说明）；工单转为`failed`，仪器进入`quarantined`并保持停用，处置说明留在仪器数据中。
5. 冲突与幂等：带旧`expected_version`提交时返回409，版本冲突在任何写入前判定，原状态与审计均不被覆盖；送检与回填在同一乐观锁事务里联动工单和仪器。同`Idempotency-Key`的重复提交直接返回首次记录。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
