"""参与者资料与保证金账户规则（不包含存储与接口）。"""
from typing import Any, Dict

from .domain import ValidationError, number, text

ACTIVE = "active"
FROZEN = "frozen"
STATUSES = (ACTIVE, FROZEN)


class ParticipantDirectory:
    """参与者资料、保证金的纯业务校验。"""

    def normalize_profile(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload or {})
        code = text(data, "code")
        name = text(data, "name")
        initial_margin = number(data, "initial_margin", 0)
        return {"code": code, "name": name, "initial_margin": round(initial_margin, 2)}

    def normalize_topup(self, data: Dict[str, Any]) -> float:
        return round(number(data or {}, "amount", 0.01), 2)

    def require_active(self, profile: Dict[str, Any]) -> None:
        """未补足违约损失（冻结）期间不能新建单据或交收。"""
        if profile.get("status") != ACTIVE:
            raise ValidationError("参与者%s处于冻结状态，补足违约损失前不能新建或交收" % profile.get("code", ""))
