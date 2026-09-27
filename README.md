# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性、公司行动调整和冲突检查。
- `src/participants.py`：参与者资料与保证金账户规则（纯业务校验，独立于存储和接口）。
- `src/allocation.py`：违约损失分摊规则（纯函数，独立于存储和接口）。
- `src/repository.py`：SQLite建表、事务和查询（含参与者、损失案件、分摊明细、补缴、保证金流水）。
- `src/service.py`：用例编排、权限检查、乐观并发、冻结拦截和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：演示页面（参与者、单据流转、违约损失构成与分摊明细、补缴恢复）。
- `tests/`：完整流程、规则计算、失败场景、分摊规则与违约处置测试。

## 违约处置规则

失败单据不再只标记失败：结算员可将`failed`单据声明违约（`declare_default`），单据转入`defaulted`终态：

1. **损失认定**：失败单据的未交付金额（`net_amount`）全额计为损失。
2. **先扣保证金**：违约方保证金按损失额扣减，扣到零为止。
3. **其余参与者分摊**：不足部分按违约时点**过去一个月**（30天）成交额（`gross_amount`）占比分摊；窗口内无成交时按其余参与者人数均摊。每家最多扣到自身保证金余额；某家被封顶后，其溢出额在其余仍有余额者之间继续按权重再分配；全部扣完仍不足的部分挂账。
4. **冻结与恢复**：违约方在损失全部补足前为`frozen`状态，不能新建单据也不能交收；补缴资金逐笔冲减未弥补损失，全部补足后案件关闭、违约方自动恢复`active`。
5. **单据接手**：`captured`/`adjusted`/`approved`的未完成单据可由其他正常状态参与者接手（变更责任参与者并留审计），接手后可继续交收。

金额一律以整数"分"存储与分摊，避免浮点尾差；违约扣减、分摊、补缴均在单事务内落库。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

单据：

- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data.participant`可选。
- `POST /api/records/{id}/actions/{action}`：业务动作（`apply_corporate`/`approve`/`settle`/`fail`/`reverse`）。
- `POST /api/records/{id}/default`：声明违约并执行处置，请求体为`{"expected_version":3,"data":{"fail_reason":"..."}}`，返回损失案件（含`loss_composition`损失构成与`allocations`分摊明细）。
- `POST /api/records/{id}/takeover`：未完成单据转手，请求体为`{"expected_version":1,"data":{"new_participant":"A","reason":"..."}}`。

参与者与保证金：

- `GET /api/participants` / `GET /api/participants/{code}`：参与者资料。
- `POST /api/participants`：登记参与者（admin/结算员），请求体为`{"data":{"code":"A","name":"...","initial_margin":100000}}`。
- `POST /api/participants/{code}/margin-topup`：保证金充值，`{"data":{"amount":50000}}`。
- `GET /api/participants/{code}/margin-ledger` / `GET /api/margin-ledger?participant=A`：保证金流水。

违约损失：

- `GET /api/loss-cases?state=open`：损失案件列表，含损失构成、分摊明细、补缴记录。
- `GET /api/loss-cases/{id}`：损失案件详情。
- `POST /api/loss-cases/{id}/recover`：违约方补缴，`{"data":{"amount":5000}}`，返回`recovery_result`（是否恢复、剩余未弥补额、违约方状态）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。违约声明、接手、参与者登记/充值限`settlement_officer`或`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、成交占比/封顶/挂账分摊，以及违约冻结、接手、补缴恢复的端到端流程。
