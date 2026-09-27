"""违约处置存储：参与者、违约案件、分摊明细与保证金流水的SQLite实现。"""
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound, ValidationError
from .participants import STATUS_ACTIVE, STATUS_SUSPENDED


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DefaultRepository:
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
                CREATE TABLE IF NOT EXISTS participants (
                    participant_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    margin_balance REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS default_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL UNIQUE REFERENCES records(id) ON DELETE CASCADE,
                    reference TEXT NOT NULL,
                    defaulter_id TEXT NOT NULL,
                    loss_amount REAL NOT NULL,
                    defaulter_margin_used REAL NOT NULL,
                    allocated_total REAL NOT NULL,
                    uncovered_amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS default_allocations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES default_cases(id) ON DELETE CASCADE,
                    round INTEGER NOT NULL,
                    participant_id TEXT NOT NULL,
                    share_ratio REAL NOT NULL,
                    amount REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS margin_moves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    participant_id TEXT NOT NULL,
                    case_id INTEGER,
                    kind TEXT NOT NULL,
                    amount REAL NOT NULL,
                    balance_after REAL NOT NULL,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cases_defaulter ON default_cases(defaulter_id, status);
                CREATE INDEX IF NOT EXISTS idx_allocations_case ON default_allocations(case_id, id);
                CREATE INDEX IF NOT EXISTS idx_margin_moves_participant ON margin_moves(participant_id, id);
                """
            )

    # ---------- 参与者 ----------

    def create_participant(self, participant_id: str, name: str, margin_balance: float, actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO participants(participant_id,name,margin_balance,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (participant_id, name, margin_balance, STATUS_ACTIVE, now, now),
                )
                if margin_balance > 0:
                    connection.execute(
                        "INSERT INTO margin_moves(participant_id,case_id,kind,amount,balance_after,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                        (participant_id, None, "deposit", margin_balance, margin_balance, actor_id, now),
                    )
        except sqlite3.IntegrityError as exc:
            raise Conflict("参与者已存在") from exc
        return self.get_participant(participant_id)

    def get_participant(self, participant_id: str) -> Dict[str, Any]:
        participant = self.find_participant(participant_id)
        if participant is None:
            raise NotFound("参与者不存在")
        return participant

    def find_participant(self, participant_id: str) -> Optional[Dict[str, Any]]:
        if not participant_id:
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM participants WHERE participant_id=?", (participant_id,)).fetchone()
        return dict(row) if row else None

    def list_participants(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM participants ORDER BY participant_id").fetchall()
        return [dict(row) for row in rows]

    def adjust_margin(self, participant_id: str, kind: str, amount: float, actor_id: str) -> Dict[str, Any]:
        delta = amount if kind == "deposit" else -amount
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT margin_balance FROM participants WHERE participant_id=?", (participant_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("参与者不存在")
            balance = float(row["margin_balance"])
            if delta < 0 and balance + delta < -1e-9:
                connection.rollback()
                raise ValidationError("保证金余额不足")
            new_balance = round(balance + delta, 2)
            connection.execute(
                "UPDATE participants SET margin_balance=?,updated_at=? WHERE participant_id=?",
                (new_balance, now, participant_id),
            )
            connection.execute(
                "INSERT INTO margin_moves(participant_id,case_id,kind,amount,balance_after,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (participant_id, None, kind, delta, new_balance, actor_id, now),
            )
            connection.commit()
        return self.get_participant(participant_id)

    def margin_moves(self, participant_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM margin_moves WHERE participant_id=? ORDER BY id DESC LIMIT ?",
                (participant_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 违约案件 ----------

    def _deduct_margin(self, connection: sqlite3.Connection, participant_id: str, amount: float, kind: str, case_id: int, actor_id: str, now: str) -> None:
        if amount <= 0:
            return
        row = connection.execute("SELECT margin_balance FROM participants WHERE participant_id=?", (participant_id,)).fetchone()
        if row is None:
            return
        new_balance = round(float(row["margin_balance"]) - amount, 2)
        if new_balance < -1e-9:
            connection.rollback()
            raise Conflict("参与者保证金余额已变化，请重试")
        connection.execute(
            "UPDATE participants SET margin_balance=?,updated_at=? WHERE participant_id=?",
            (new_balance, now, participant_id),
        )
        connection.execute(
            "INSERT INTO margin_moves(participant_id,case_id,kind,amount,balance_after,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (participant_id, case_id, kind, -amount, new_balance, actor_id, now),
        )

    def _refresh_status(self, connection: sqlite3.Connection, participant_id: str, now: str) -> None:
        row = connection.execute("SELECT participant_id FROM participants WHERE participant_id=?", (participant_id,)).fetchone()
        if row is None:
            return
        open_cases = connection.execute(
            "SELECT COUNT(*) AS total FROM default_cases WHERE defaulter_id=? AND status='open'",
            (participant_id,),
        ).fetchone()["total"]
        status = STATUS_SUSPENDED if int(open_cases) > 0 else STATUS_ACTIVE
        connection.execute("UPDATE participants SET status=?,updated_at=? WHERE participant_id=?", (status, now, participant_id))

    def open_case(self, record_id: int, reference: str, defaulter_id: str, loss: float, defaulter_used: float,
                  allocations: List[Dict[str, Any]], uncovered: float, actor_id: str) -> Dict[str, Any]:
        """开立违约案件：扣违约方保证金、登记并扣收分摊，全部在一个事务内完成。"""
        now = _now()
        status = "recovered" if uncovered <= 0 else "open"
        allocated_total = round(sum(item["amount"] for item in allocations), 2)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO default_cases(record_id,reference,defaulter_id,loss_amount,defaulter_margin_used,allocated_total,uncovered_amount,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (record_id, reference, defaulter_id, loss, defaulter_used, allocated_total, uncovered, status, now, now),
                )
                case_id = int(cursor.lastrowid)
                self._deduct_margin(connection, defaulter_id, defaulter_used, "default_deduction", case_id, actor_id, now)
                for item in allocations:
                    connection.execute(
                        "INSERT INTO default_allocations(case_id,round,participant_id,share_ratio,amount,created_at) VALUES(?,?,?,?,?,?)",
                        (case_id, 1, item["participant_id"], item["share_ratio"], item["amount"], now),
                    )
                    self._deduct_margin(connection, item["participant_id"], item["amount"], "allocation_deduction", case_id, actor_id, now)
                self._refresh_status(connection, defaulter_id, now)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("该单据已存在违约案件") from exc
        return self.get_case(case_id)

    def apply_recovery(self, case_id: int, defaulter_extra: float, allocations: List[Dict[str, Any]],
                       uncovered: float, actor_id: str) -> Dict[str, Any]:
        """恢复处置：对未补足缺口再次扣违约方保证金并向其余参与者分摊。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            case = connection.execute("SELECT * FROM default_cases WHERE id=?", (case_id,)).fetchone()
            if case is None:
                connection.rollback()
                raise NotFound("违约案件不存在")
            if case["status"] == "recovered":
                connection.rollback()
                raise Conflict("案件已补足，无需恢复")
            round_no = connection.execute(
                "SELECT COALESCE(MAX(round),0) AS max_round FROM default_allocations WHERE case_id=?", (case_id,)
            ).fetchone()["max_round"] + 1
            allocated_extra = round(sum(item["amount"] for item in allocations), 2)
            status = "recovered" if uncovered <= 0 else "open"
            connection.execute(
                "UPDATE default_cases SET defaulter_margin_used=defaulter_margin_used+?,allocated_total=allocated_total+?,uncovered_amount=?,status=?,updated_at=? WHERE id=?",
                (defaulter_extra, allocated_extra, uncovered, status, now, case_id),
            )
            self._deduct_margin(connection, case["defaulter_id"], defaulter_extra, "default_deduction", case_id, actor_id, now)
            for item in allocations:
                connection.execute(
                    "INSERT INTO default_allocations(case_id,round,participant_id,share_ratio,amount,created_at) VALUES(?,?,?,?,?,?)",
                    (case_id, round_no, item["participant_id"], item["share_ratio"], item["amount"], now),
                )
                self._deduct_margin(connection, item["participant_id"], item["amount"], "allocation_deduction", case_id, actor_id, now)
            self._refresh_status(connection, case["defaulter_id"], now)
            connection.commit()
        return self.get_case(case_id)

    def get_case(self, case_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM default_cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFound("违约案件不存在")
        return dict(row)

    def find_case_by_record(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM default_cases WHERE record_id=?", (record_id,)).fetchone()
        return dict(row) if row else None

    def list_cases(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM default_cases ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
        return [dict(row) for row in rows]

    def case_allocations(self, case_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM default_allocations WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        return [dict(row) for row in rows]
