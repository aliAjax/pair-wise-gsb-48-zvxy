"""业务用例编排、权限检查与审计。"""
from datetime import timedelta
from typing import Any, Dict, List, Optional

from . import allocation as alloc
from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .participants import ACTIVE, ParticipantDirectory
from .repository import Repository
from .rules import DomainRules

# 未完成、可被接手的单据状态
OPEN_STATES = {"captured", "adjusted", "approved"}


class Service:
    def __init__(
        self,
        repository: Repository,
        rules: DomainRules,
        audit: AuditRecorder = None,
        directory: ParticipantDirectory = None,
    ) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.directory = directory or ParticipantDirectory()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _is_admin_or(self, actor: Actor, roles: set) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行该操作")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        # 冻结（违约未补足）参与者不能新建单据
        code = prepared.get("participant")
        if code:
            self.directory.require_active(self._require_participant(code))
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        # 冻结（违约未补足）参与者不能交收
        if action == "settle":
            code = record["payload"].get("participant")
            if code:
                self.directory.require_active(self._require_participant(code))
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 参与者资料 ----

    def _require_participant(self, code: str) -> Dict[str, Any]:
        return self.repository.get_participant(code)

    def register_participant(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._is_admin_or(actor, {"settlement_officer"})
        profile = self.directory.normalize_profile(payload or {})
        participant = self.repository.create_participant(
            profile["code"], profile["name"], alloc.to_cents(profile["initial_margin"])
        )
        return self.serialize_participant(participant)

    def list_participants(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return [self.serialize_participant(row) for row in self.repository.list_participants()]

    def get_participant(self, actor: Actor, code: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.serialize_participant(self.repository.get_participant(code))

    def topup_margin(self, actor: Actor, code: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._is_admin_or(actor, {"settlement_officer"})
        amount = self.directory.normalize_topup(data or {})
        participant = self.repository.topup_margin(code, alloc.to_cents(amount))
        return self.serialize_participant(participant)

    def margin_ledger(self, actor: Actor, code: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        rows = self.repository.margin_ledger(code, limit=limit)
        result = []
        for row in rows:
            item = dict(row)
            item["change_amount_amount"] = alloc.to_amount(row["change_amount"])
            item["balance_after_amount"] = alloc.to_amount(row["balance_after"])
            result.append(item)
        return result

    # ---- 违约处置 ----

    def declare_default(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """失败单据转违约：未交付金额计为损失，先扣违约方保证金，不足按成交占比分摊。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._is_admin_or(actor, {"settlement_officer"})

        record = self.repository.get(record_id)
        action = "declare_default"
        self.rules.require_transition(record, action)
        payload = record["payload"]
        defaulter = payload.get("participant")
        if not defaulter:
            raise ValidationError("失败单据缺少责任参与者，无法认定违约方")
        def_row = self.repository.get_participant(defaulter)
        if def_row["status"] != ACTIVE:
            raise ValidationError("违约方已处于冻结状态")

        reason = text(data or {}, "fail_reason") if (data or {}).get("fail_reason") else payload.get("fail_reason", "")
        # 失败单据未交付金额：失败时未交券也未付款，以净额作为损失
        loss_cents = alloc.to_cents(payload.get("net_amount", payload.get("gross_amount", 0)))
        if loss_cents <= 0:
            raise ValidationError("失败单据未交付金额为零，不构成损失")

        others = [
            row["code"]
            for row in self.repository.list_participants()
            if row["code"] != defaulter
        ]
        balances = {row["code"]: int(row["margin_balance"]) for row in self.repository.list_participants()}
        now = alloc.utcnow()
        window_records = self.repository.records_created_since((now - timedelta(days=alloc.WINDOW_DAYS)).isoformat())
        weights, basis = alloc.turnover_weights(
            window_records, others, now=now, exclude_record_id=int(record["id"])
        )
        plan = alloc.build_default_plan(
            loss_cents=loss_cents,
            defaulter_balance_cents=int(def_row["margin_balance"]),
            others=others,
            balances_cents=balances,
            weights=weights,
            basis=basis,
        )

        new_state, new_payload, _ = self.rules.apply_action(
            record, action, {"fail_reason": reason} if reason else {}
        )
        new_payload["default_loss_case_pending"] = True
        case = self.repository.apply_default_case(
            record_id=int(record["id"]),
            expected_version=int(expected_version),
            new_state=new_state,
            new_payload=new_payload,
            defaulter=defaulter,
            reason=reason,
            plan=plan,
            actor_id=actor.user_id,
        )
        return self.serialize_loss_case(case, record_id=int(record["id"]))

    def takeover(
        self, actor: Actor, record_id: int, expected_version: int, new_participant: str, reason: str
    ) -> Dict[str, Any]:
        """未完成单据可由其他参与者接手。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._is_admin_or(actor, {"settlement_officer"})
        new_participant = text({"new_participant": new_participant}, "new_participant")
        reason = text({"reason": reason}, "reason")
        record = self.repository.get(record_id)
        if record["state"] not in OPEN_STATES:
            raise Conflict("仅未完成单据可被接手")
        current = record["payload"].get("participant", "")
        if new_participant == current:
            raise Conflict("接手参与者不能与当前责任参与者相同")
        profile = self.repository.get_participant(new_participant)
        self.directory.require_active(profile)
        return self.repository.takeover(
            record_id, int(expected_version), new_participant, actor.user_id, reason
        )

    def list_loss_cases(self, actor: Actor, state: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return [self.serialize_loss_case(case) for case in self.repository.list_loss_cases(state)]

    def get_loss_case(self, actor: Actor, case_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.serialize_loss_case(self.repository.get_loss_case(case_id))

    def recover_default(self, actor: Actor, case_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """违约方补缴资金，冲减未弥补损失；补足后恢复交易与交收资格。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        amount = self.directory.normalize_topup(data or {})
        before = self.repository.get_loss_case(case_id)
        case = self.repository.register_recovery(case_id, alloc.to_cents(amount), actor.user_id)
        serialized = self.serialize_loss_case(case)
        serialized["recovery_result"] = {
            "restored": case["state"] == "closed" and before["state"] != "closed",
            "paid_amount": round(amount, 2),
            "outstanding_amount": alloc.to_amount(case["outstanding_amount"]),
            "defaulter_status": self.repository.get_participant(case["defaulter"])["status"],
        }
        return serialized

    # ---- 序列化 ----

    @staticmethod
    def serialize_participant(row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["margin_balance_amount"] = alloc.to_amount(row["margin_balance"])
        return item

    @staticmethod
    def serialize_loss_case(case: Dict[str, Any], record_id: Optional[int] = None) -> Dict[str, Any]:
        item = dict(case)
        allocations = []
        for row in case.get("allocations", []):
            allocations.append(
                {
                    "participant": row["participant"],
                    "weight_amount": alloc.to_amount(row["weight_amount"]),
                    "ratio": round(float(row["ratio"]), 6),
                    "allocated_amount": alloc.to_amount(row["allocated_amount"]),
                    "balance_after": alloc.to_amount(row["balance_after"]),
                }
            )
        recoveries = [
            {
                "id": row["id"],
                "participant": row["participant"],
                "amount": alloc.to_amount(row["amount"]),
                "created_at": row["created_at"],
            }
            for row in case.get("recoveries", [])
        ]
        item["allocations"] = allocations
        item["recoveries"] = recoveries
        # 损失构成：违约方保证金赔付 + 向他人分摊 + 仍未弥补
        item["loss_composition"] = {
            "loss_amount": alloc.to_amount(case["loss_amount"]),
            "defaulter_cover": alloc.to_amount(case["defaulter_cover"]),
            "shortfall_amount": alloc.to_amount(case["shortfall_amount"]),
            "allocated_amount": alloc.to_amount(case["allocated_amount"]),
            "outstanding_amount": alloc.to_amount(case["outstanding_amount"]),
        }
        item["total_weight_amount"] = alloc.to_amount(case.get("total_weight", 0))
        if record_id is not None:
            item["record_id"] = record_id
        return item
