"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, defaults: Any = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.defaults = defaults

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        if self.defaults is not None:
            self.defaults.ensure_can_create(actor.organization)
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, org=actor.organization)

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
        if action == "takeover":
            return self._takeover(actor, record_id, expected_version)
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action == "settle" and self.defaults is not None:
            self.defaults.ensure_can_settle(record)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        if action == "fail" and self.defaults is not None:
            # 交收失败自动进入违约处置：未交付金额转损失并分摊
            self.defaults.open_case_for_record(updated, actor)
        return updated

    def _takeover(self, actor: Actor, record_id: int, expected_version: int) -> Dict[str, Any]:
        if self.defaults is None:
            raise PermissionDenied("违约处置未启用")
        record = self.repository.get(record_id)
        new_org = (actor.organization or "").strip() or actor.user_id
        self.defaults.ensure_takeover(record, new_org)
        payload = dict(record["payload"])
        payload["taken_over_from"] = record.get("org") or ""
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=record["state"],
            payload=payload,
            actor_id=actor.user_id,
            action="takeover",
            details={"summary": "单据由%s接手" % new_org, "from_org": record.get("org") or "", "to_org": new_org},
            org=new_org,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
