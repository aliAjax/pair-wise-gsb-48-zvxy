"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS participants (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    margin_balance INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS loss_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    defaulter TEXT NOT NULL,
                    state TEXT NOT NULL,
                    loss_amount INTEGER NOT NULL,
                    defaulter_cover INTEGER NOT NULL,
                    shortfall_amount INTEGER NOT NULL,
                    allocated_amount INTEGER NOT NULL,
                    outstanding_amount INTEGER NOT NULL,
                    basis TEXT NOT NULL,
                    total_weight INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS loss_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    loss_case_id INTEGER NOT NULL REFERENCES loss_cases(id) ON DELETE CASCADE,
                    participant TEXT NOT NULL,
                    weight_amount INTEGER NOT NULL,
                    ratio REAL NOT NULL,
                    allocated_amount INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS loss_recoveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    loss_case_id INTEGER NOT NULL REFERENCES loss_cases(id) ON DELETE CASCADE,
                    participant TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS margin_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    participant TEXT NOT NULL,
                    change_amount INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    ref_record_id INTEGER,
                    loss_case_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_records_created ON records(created_at);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_loss_cases_state ON loss_cases(state);
                CREATE INDEX IF NOT EXISTS idx_loss_alloc_case ON loss_allocations(loss_case_id);
                CREATE INDEX IF NOT EXISTS idx_loss_recov_case ON loss_recoveries(loss_case_id);
                CREATE INDEX IF NOT EXISTS idx_margin_participant ON margin_ledger(participant, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def records_created_since(self, cutoff_iso: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE created_at >= ? ORDER BY id", (cutoff_iso,)
            ).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def takeover(self, record_id: int, expected_version: int, new_participant: str, actor_id: str, reason: str) -> Dict[str, Any]:
        """未完成单据可由其他参与者接手：仅更换责任参与者，版本递增留痕。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            record = self._row(row)
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            payload = dict(record["payload"])
            old_participant = payload.get("participant", "")
            payload["participant"] = new_participant
            payload["takeover_from"] = old_participant
            payload["takeover_reason"] = reason
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET payload=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), version, actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    record_id,
                    "takeover",
                    actor_id,
                    version,
                    json.dumps(
                        {"from": old_participant, "to": new_participant, "reason": reason},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ---- 参与者与保证金 ----

    @staticmethod
    def _participant_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_participant(self, code: str, name: str, initial_margin_cents: int) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO participants(code,name,status,margin_balance,created_at,updated_at) VALUES(?,?, 'active', ?,?,?)",
                    (code, name, int(initial_margin_cents), now, now),
                )
                connection.execute(
                    "INSERT INTO margin_ledger(participant,change_amount,balance_after,reason,ref_record_id,loss_case_id,created_at) VALUES(?,?,?, 'margin_topup', NULL, NULL, ?)",
                    (code, int(initial_margin_cents), int(initial_margin_cents), now),
                )
                row = connection.execute("SELECT * FROM participants WHERE code=?", (code,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("参与者已存在") from exc
        return self._participant_row(row)

    def get_participant(self, code: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM participants WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFound("参与者不存在")
        return self._participant_row(row)

    def list_participants(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM participants ORDER BY code").fetchall()
        return [self._participant_row(row) for row in rows]

    def margin_ledger(self, participant: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            if participant:
                rows = connection.execute(
                    "SELECT * FROM margin_ledger WHERE participant=? ORDER BY id DESC LIMIT ?",
                    (participant, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM margin_ledger ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def topup_margin(self, code: str, amount_cents: int, reason: str = "margin_topup") -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM participants WHERE code=?", (code,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("参与者不存在")
            balance = int(row["margin_balance"]) + int(amount_cents)
            connection.execute(
                "UPDATE participants SET margin_balance=?, updated_at=? WHERE code=?",
                (balance, now, code),
            )
            connection.execute(
                "INSERT INTO margin_ledger(participant,change_amount,balance_after,reason,ref_record_id,loss_case_id,created_at) VALUES(?,?,?, ?, NULL, NULL, ?)",
                (code, int(amount_cents), balance, reason, now),
            )
            result = connection.execute("SELECT * FROM participants WHERE code=?", (code,)).fetchone()
            connection.commit()
        return self._participant_row(result)

    # ---- 违约处置 ----

    def apply_default_case(
        self,
        *,
        record_id: int,
        expected_version: int,
        new_state: str,
        new_payload: Dict[str, Any],
        defaulter: str,
        reason: str,
        plan: Dict[str, Any],
        actor_id: str,
    ) -> Dict[str, Any]:
        """原子地落库违约案件：扣保证金、分摊、冻结违约方、单据转违约态。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record_row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(record_row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")

            drow = connection.execute("SELECT * FROM participants WHERE code=?", (defaulter,)).fetchone()
            if drow is None:
                connection.rollback()
                raise NotFound("违约参与者不存在")
            cover = int(plan["defaulter_cover_cents"])
            if int(drow["margin_balance"]) < cover:
                connection.rollback()
                raise Conflict("违约方保证金余额已变化，请刷新后重试")
            d_balance = int(drow["margin_balance"]) - cover
            connection.execute(
                "UPDATE participants SET margin_balance=?, updated_at=? WHERE code=?",
                (d_balance, now, defaulter),
            )

            allocation_rows = []
            for item in plan["allocations"]:
                code = item["participant"]
                prow = connection.execute("SELECT * FROM participants WHERE code=?", (code,)).fetchone()
                if prow is None:
                    connection.rollback()
                    raise NotFound("分摊参与者不存在：%s" % code)
                allocated = int(item["allocated_cents"])
                if int(prow["margin_balance"]) < allocated:
                    connection.rollback()
                    raise Conflict("参与者%s保证金余额已变化，请刷新后重试" % code)
                balance_after = int(prow["margin_balance"]) - allocated
                connection.execute(
                    "UPDATE participants SET margin_balance=?, updated_at=? WHERE code=?",
                    (balance_after, now, code),
                )
                allocation_rows.append((code, item, allocated, balance_after))

            cursor = connection.execute(
                """
                INSERT INTO loss_cases(record_id,defaulter,state,loss_amount,defaulter_cover,
                    shortfall_amount,allocated_amount,outstanding_amount,basis,total_weight,
                    reason,created_at,closed_at)
                VALUES(?,?, 'open', ?,?,?,?,?,?,?,?,?, NULL)
                """,
                (
                    record_id,
                    defaulter,
                    int(plan["loss_cents"]),
                    cover,
                    int(plan["shortfall_cents"]),
                    int(plan["allocated_cents"]),
                    int(plan["outstanding_cents"]),
                    plan["basis"],
                    sum(int(item["weight_cents"]) for item in plan["allocations"]),
                    reason,
                    now,
                ),
            )
            case_id = int(cursor.lastrowid)
            for code, item, allocated, balance_after in allocation_rows:
                connection.execute(
                    """
                    INSERT INTO loss_allocations(loss_case_id,participant,weight_amount,ratio,
                        allocated_amount,balance_after,created_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        case_id,
                        code,
                        int(item["weight_cents"]),
                        float(item["ratio"]),
                        allocated,
                        balance_after,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO margin_ledger(participant,change_amount,balance_after,reason,ref_record_id,loss_case_id,created_at) VALUES(?,?,?, 'default_charge', ?,?,?)",
                (defaulter, -cover, d_balance, record_id, case_id, now),
            )
            for code, _, allocated, balance_after in allocation_rows:
                if allocated:
                    connection.execute(
                        "INSERT INTO margin_ledger(participant,change_amount,balance_after,reason,ref_record_id,loss_case_id,created_at) VALUES(?,?,?, 'default_allocation', ?,?,?)",
                        (code, -allocated, balance_after, record_id, case_id, now),
                    )

            # 违约方未补足前冻结
            connection.execute(
                "UPDATE participants SET status='frozen', updated_at=? WHERE code=?",
                (now, defaulter),
            )

            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, version, json.dumps(new_payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    record_id,
                    "declare_default",
                    actor_id,
                    version,
                    json.dumps(
                        {
                            "loss_case_id": case_id,
                            "defaulter": defaulter,
                            "reason": reason,
                            "outstanding": int(plan["outstanding_cents"]),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            connection.commit()
        return self.get_loss_case(case_id)

    def _load_loss_case(self, connection: sqlite3.Connection, case_id: int) -> Optional[Dict[str, Any]]:
        row = connection.execute("SELECT * FROM loss_cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            return None
        case = dict(row)
        case["allocations"] = [
            dict(item)
            for item in connection.execute(
                "SELECT * FROM loss_allocations WHERE loss_case_id=? ORDER BY id", (case_id,)
            ).fetchall()
        ]
        case["recoveries"] = [
            dict(item)
            for item in connection.execute(
                "SELECT * FROM loss_recoveries WHERE loss_case_id=? ORDER BY id", (case_id,)
            ).fetchall()
        ]
        return case

    def get_loss_case(self, case_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            case = self._load_loss_case(connection, case_id)
        if case is None:
            raise NotFound("违约损失案件不存在")
        return case

    def list_loss_cases(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if state:
                rows = connection.execute(
                    "SELECT * FROM loss_cases WHERE state=? ORDER BY id DESC", (state,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM loss_cases ORDER BY id DESC").fetchall()
            cases = [self._load_loss_case(connection, int(row["id"])) for row in rows]
        return cases

    def register_recovery(self, case_id: int, amount_cents: int, actor_id: str) -> Dict[str, Any]:
        """违约方补缴：冲减未弥补损失，补足后解冻。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM loss_cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("违约损失案件不存在")
            case = dict(row)
            if case["state"] != "open":
                connection.rollback()
                raise Conflict("损失案件已结清")
            outstanding = int(case["outstanding_amount"])
            amount = int(amount_cents)
            if amount <= 0:
                connection.rollback()
                raise Conflict("补缴金额必须为正")
            if amount > outstanding:
                connection.rollback()
                raise Conflict("补缴金额不能超过未弥补损失")
            new_outstanding = outstanding - amount
            closed_at = None
            if new_outstanding == 0:
                closed_at = now
                connection.execute(
                    "UPDATE participants SET status='active', updated_at=? WHERE code=?",
                    (now, case["defaulter"]),
                )
            connection.execute(
                "UPDATE loss_cases SET outstanding_amount=?, state=?, closed_at=? WHERE id=?",
                (new_outstanding, "closed" if closed_at else "open", closed_at, case_id),
            )
            connection.execute(
                "INSERT INTO loss_recoveries(loss_case_id,participant,amount,created_at) VALUES(?,?,?,?)",
                (case_id, case["defaulter"], amount, now),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    int(case["record_id"]),
                    "recover_default",
                    actor_id,
                    0,
                    json.dumps(
                        {"loss_case_id": case_id, "amount": amount, "outstanding": new_outstanding},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            connection.commit()
        return self.get_loss_case(case_id)

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
