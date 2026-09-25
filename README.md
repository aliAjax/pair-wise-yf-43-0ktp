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

- `instrument`：仪器状态；`calibration`：校准工单；`method`：方法版本；`result`：检测结果。

## 校准流程

- 计量员（`metrology`）对仪器执行`send_calibration`时必须登记`assignee`（承担人）、`planned_date`（计划完成日）和`purpose`（用途）；仪器随即变为`calibrating`（待校准），系统自动开出同字段的校准工单。
- 工单结果未回填（仍为`requested`）时，放行（`release`）检测结果会被拒绝，错误信息中带工单编号。
- `GET /api/calibrations?status=overdue`可筛出过了计划完成日仍无结果的工单。
- 对工单执行`perform`回填结果：`passed`需带`due_at`，仪器恢复`active`并写入新到期日；`failed`需带`disposition`（处置说明），仪器转为`quarantined`并保持停用。仪器的状态变化同样写入审计。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（校准工单支持`?status=overdue`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。创建和动作请求都可带`Idempotency-Key`请求头：重复提交沿用首次记录；`expected_version`与当前版本不一致时返回409冲突，原状态和审计记录不被覆盖。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
