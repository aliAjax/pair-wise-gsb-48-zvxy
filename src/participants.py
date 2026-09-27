"""参与者资料：数据类型、状态常量与输入校验。"""
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from .domain import choice, number, text


STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
PARTICIPANT_STATUSES = [STATUS_ACTIVE, STATUS_SUSPENDED]

# 管理参与者资料与违约处置的角色（admin 永远可用）
MANAGE_ROLES = {"clearing_officer"}

MARGIN_KINDS = ["deposit", "withdraw"]


@dataclass(frozen=True)
class Participant:
    participant_id: str
    name: str
    margin_balance: float
    status: str = STATUS_ACTIVE


def validate_registration(data: Dict[str, Any]) -> Dict[str, Any]:
    """校验参与者注册请求，返回规范化后的资料。"""
    payload = dict(data or {})
    return {
        "participant_id": text(payload, "participant_id"),
        "name": text(payload, "name"),
        "margin_balance": number(payload, "margin_balance", 0),
    }


def validate_margin_adjustment(data: Dict[str, Any]) -> Tuple[str, float]:
    """校验保证金调整请求，返回(方向, 金额)。"""
    payload = dict(data or {})
    kind = choice(payload, "kind", MARGIN_KINDS)
    amount = number(payload, "amount", 0.01)
    return kind, round(amount, 2)
