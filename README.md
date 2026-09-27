# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。覆盖净额结算、交收完整性、公司行动调整与违约处置。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整和冲突检查。
- `src/repository.py`：结算记录与审计的SQLite建表、事务和查询。
- `src/service.py`：结算用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `src/participants.py`：参与者资料（数据类型、状态常量与校验）。
- `src/default_rules.py`：违约分摊规则（损失计算、成交占比统计、限额分摊，纯函数）。
- `src/default_repository.py`：参与者、违约案件、分摊明细与保证金流水的SQLite存储。
- `src/default_service.py`：违约处置用例编排（开庭、恢复、冻结检查、接手检查）。
- `src/default_http_api.py`：参与者与违约处置的HTTP路由。
- `static/index.html`：演示页面（损失构成、分摊明细、恢复结果）。
- `tests/`：完整流程、规则计算、失败场景与违约处置测试。

## 违约处置

交收失败（`fail`）后自动开庭：未交付金额（应付净额减已付资金）转成损失，先扣违约方保证金；不足部分按过去一个月（30天）成交占比向其余已注册参与者分摊，每家最多扣到自己的保证金余额，被限额卡住的部分在仍有额度的参与者之间继续按比例再分配。仍有缺口时案件保持`open`，违约方被暂停：不能新建记录，其名下单据不能交收；其未完成单据（captured/adjusted/approved）可由其他未暂停参与者通过`takeover`接手后继续交收。违约方补足保证金后调用恢复接口重新扣收与分摊，补足后案件变为`recovered`，参与者自动恢复正常。

参与者以`X-Org`请求头作为编号，管理操作（注册、保证金调整、恢复）需要`clearing_officer`或`admin`角色。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作（含`takeover`接手），请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/participants`：参与者列表；`GET /api/participants/{id}`：参与者详情与保证金流水。
- `POST /api/participants`：注册参与者，请求体为`{"participant_id":"...","name":"...","margin_balance":0}`。
- `POST /api/participants/{id}/margin`：调整保证金，请求体为`{"kind":"deposit|withdraw","amount":100}`。
- `GET /api/defaults`：违约案件列表；`GET /api/defaults/{id}`：损失构成、分摊明细与恢复结果。
- `POST /api/defaults/{id}/recover`：对未补足缺口重新扣收与分摊。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及违约开庭、限额分摊、冻结拦截、单据接手和恢复处置。
