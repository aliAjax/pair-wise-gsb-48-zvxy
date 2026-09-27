"""违约处置用例编排：开庭、分摊、恢复、冻结检查与单据接手。"""
from typing import Any, Dict, List, Optional

from . import default_rules
from .default_repository import DefaultRepository
from .domain import Actor, Conflict, PermissionDenied
from .participants import MANAGE_ROLES, STATUS_SUSPENDED, validate_margin_adjustment, validate_registration
from .repository import Repository


BLOCKED_MESSAGE = "存在未补足的违约损失，补足前不能新建或交收"
TAKEOVER_STATES = {"captured", "adjusted", "approved"}


class DefaultService:
    def __init__(self, repository: Repository, store: DefaultRepository, rules: Any) -> None:
        self.repository = repository
        self.store = store
        self.rules = rules

    # ---------- 身份与权限 ----------

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _require_manager(self, actor: Actor) -> Actor:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin" and actor.role not in MANAGE_ROLES:
            raise PermissionDenied("角色无权管理参与者与违约处置")
        return actor

    # ---------- 参与者资料 ----------

    def register_participant(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require_manager(actor)
        info = validate_registration(data)
        return self.store.create_participant(info["participant_id"], info["name"], info["margin_balance"], actor.user_id)

    def list_participants(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.store.list_participants()

    def participant_detail(self, actor: Actor, participant_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        participant = self.store.get_participant(participant_id)
        return {"participant": participant, "margin_moves": self.store.margin_moves(participant_id)}

    def adjust_margin(self, actor: Actor, participant_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._require_manager(actor)
        kind, amount = validate_margin_adjustment(data)
        return self.store.adjust_margin(participant_id, kind, amount, actor.user_id)

    # ---------- 违约处置 ----------

    def _capacities(self, exclude: str) -> Dict[str, int]:
        return {
            p["participant_id"]: default_rules.to_cents(p["margin_balance"])
            for p in self.store.list_participants()
            if p["participant_id"] != exclude
        }

    def _volumes(self, exclude: str) -> Dict[str, int]:
        records = self.repository.list_records(state="settled", limit=500)
        registered = {p["participant_id"] for p in self.store.list_participants()}
        volumes = default_rules.trading_volumes(records, exclude=exclude)
        return {pid: cents for pid, cents in volumes.items() if pid in registered and cents > 0}

    def _plan(self, loss: int, defaulter_id: str) -> Dict[str, Any]:
        """损失处置方案：先扣违约方保证金，缺口按成交占比向其余参与者分摊。"""
        defaulter = self.store.find_participant(defaulter_id)
        defaulter_margin = default_rules.to_cents(defaulter["margin_balance"]) if defaulter else 0
        defaulter_used = min(loss, defaulter_margin)
        shortfall = loss - defaulter_used
        volumes = self._volumes(exclude=defaulter_id)
        allocation, uncovered = default_rules.allocate_loss(shortfall, volumes, self._capacities(defaulter_id))
        ratios = default_rules.share_ratios(volumes)
        items = [
            {"participant_id": pid, "share_ratio": ratios.get(pid, 0.0), "amount": default_rules.from_cents(cents)}
            for pid, cents in allocation.items()
            if cents > 0
        ]
        return {"defaulter_used": defaulter_used, "items": items, "uncovered": uncovered}

    def open_case_for_record(self, record: Dict[str, Any], actor: Actor) -> Optional[Dict[str, Any]]:
        """交收失败后自动开庭：未交付金额转成损失并执行分摊。幂等，重复调用直接返回已有案件。"""
        existing = self.store.find_case_by_record(record["id"])
        if existing is not None:
            return existing
        loss = default_rules.loss_cents(record["payload"])
        defaulter_id = (record.get("org") or "").strip() or record.get("created_by", "")
        plan = self._plan(loss, defaulter_id)
        case = self.store.open_case(
            record_id=record["id"],
            reference=record["reference"],
            defaulter_id=defaulter_id,
            loss=default_rules.from_cents(loss),
            defaulter_used=default_rules.from_cents(plan["defaulter_used"]),
            allocations=plan["items"],
            uncovered=default_rules.from_cents(plan["uncovered"]),
            actor_id=actor.user_id,
        )
        self.repository.add_audit(record["id"], actor.user_id, "default_case", {
            "summary": "违约处置开庭：损失%s，违约方保证金抵扣%s，分摊%s，未补足%s" % (
                case["loss_amount"], case["defaulter_margin_used"], case["allocated_total"], case["uncovered_amount"]),
            "case_id": case["id"],
            "status": case["status"],
        })
        return case

    def list_cases(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.store.list_cases()

    def case_detail(self, actor: Actor, case_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._detail(self.store.get_case(case_id))

    def _detail(self, case: Dict[str, Any]) -> Dict[str, Any]:
        record = self.repository.get(case["record_id"])
        payload = record.get("payload", {})
        recovered = round(case["defaulter_margin_used"] + case["allocated_total"], 2)
        return {
            "case": case,
            "loss": {
                "reference": case["reference"],
                "defaulter_id": case["defaulter_id"],
                "due_amount": payload.get("approved_amount", payload.get("net_amount", 0)),
                "cash_paid": payload.get("cash_paid", 0),
                "loss_amount": case["loss_amount"],
                "defaulter_margin_used": case["defaulter_margin_used"],
                "allocated_total": case["allocated_total"],
                "uncovered_amount": case["uncovered_amount"],
            },
            "allocations": self.store.case_allocations(case["id"]),
            "recovery": {
                "status": case["status"],
                "recovered": case["status"] == "recovered",
                "recovered_amount": recovered,
                "remaining_amount": case["uncovered_amount"],
            },
        }

    def recover(self, actor: Actor, case_id: int) -> Dict[str, Any]:
        """恢复处置：保证金补足后对未补足缺口重新扣收与分摊。"""
        actor = self._require_manager(actor)
        case = self.store.get_case(case_id)
        if case["status"] == "recovered":
            return self._detail(case)
        remaining = default_rules.to_cents(case["uncovered_amount"])
        plan = self._plan(remaining, case["defaulter_id"])
        updated = self.store.apply_recovery(
            case_id=case_id,
            defaulter_extra=default_rules.from_cents(plan["defaulter_used"]),
            allocations=plan["items"],
            uncovered=default_rules.from_cents(plan["uncovered"]),
            actor_id=actor.user_id,
        )
        self.repository.add_audit(case["record_id"], actor.user_id, "default_recovery", {
            "summary": "违约恢复：本次违约方补扣%s，补充分摊%s，剩余未补足%s" % (
                default_rules.from_cents(plan["defaulter_used"]),
                round(sum(i["amount"] for i in plan["items"]), 2),
                updated["uncovered_amount"]),
            "case_id": case_id,
            "status": updated["status"],
        })
        return self._detail(updated)

    # ---------- 冻结与接手检查（供结算主流程调用） ----------

    def _suspended(self, participant_id: str) -> bool:
        participant = self.store.find_participant(participant_id)
        return participant is not None and participant["status"] == STATUS_SUSPENDED

    def ensure_can_create(self, org: str) -> None:
        if self._suspended((org or "").strip()):
            raise Conflict(BLOCKED_MESSAGE)

    def ensure_can_settle(self, record: Dict[str, Any]) -> None:
        if self._suspended((record.get("org") or "").strip()):
            raise Conflict(BLOCKED_MESSAGE)

    def ensure_takeover(self, record: Dict[str, Any], new_org: str) -> None:
        if record["state"] not in TAKEOVER_STATES:
            raise Conflict("只有未完成单据可以接手")
        owner = (record.get("org") or "").strip()
        if not self._suspended(owner):
            raise Conflict("当前持有方未处于违约暂停状态，无需接手")
        if not new_org or new_org == owner:
            raise Conflict("接手方必须是其他参与者")
        if self._suspended(new_org):
            raise Conflict("接手方存在未补足违约损失，不能接手")
