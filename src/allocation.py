"""违约损失分摊规则（纯函数，不依赖存储与接口）。

规则要点：
- 违约方保证金优先赔付，不足部分由其余参与者分摊；
- 分摊权重为过去一个月的成交额占比（gross_amount）；窗口内无成交时按人数均摊；
- 每家分摊额以自身保证金余额为上限，被封顶后的溢出部分继续向其余有余额者再分配；
- 全部金额用整数"分"计算，避免浮点误差。
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

WINDOW_DAYS = 30

BASIS_TURNOVER = "turnover"
BASIS_EQUAL = "equal"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_cents(amount: Any) -> int:
    return int(round(float(amount) * 100))


def to_amount(cents: int) -> float:
    return round(int(cents) / 100.0, 2)


def turnover_weights(
    records: Sequence[Dict[str, Any]],
    others: Sequence[str],
    *,
    now: Optional[datetime] = None,
    window_days: int = WINDOW_DAYS,
    exclude_record_id: Optional[int] = None,
) -> Tuple[Dict[str, int], str]:
    """统计其余参与者过去一个月成交额（分）。

    返回 (权重表, 权重依据)。冲正单据与触发违约的失败单据不计入成交。
    """
    now = now or utcnow()
    cutoff = (now - timedelta(days=window_days)).isoformat()
    weights = {code: 0 for code in others}
    for record in records:
        if exclude_record_id is not None and int(record["id"]) == int(exclude_record_id):
            continue
        if record.get("state") == "reversed":
            continue
        if str(record.get("created_at", "")) < cutoff:
            continue
        payload = record.get("payload") or {}
        code = payload.get("participant")
        if code in weights:
            weights[code] += to_cents(payload.get("gross_amount", 0))
    if not others or sum(weights.values()) == 0:
        # 窗口内没有成交：按人数均摊，损失仍可在其余参与者间分摊
        return {code: 1 for code in others}, BASIS_EQUAL
    return weights, BASIS_TURNOVER


def proportional_cents(amount_cents: int, items: Sequence[Tuple[str, int]]) -> Dict[str, int]:
    """按正权重把整数金额精确分配（最大余数法补齐尾差）。"""
    total_weight = sum(weight for _, weight in items)
    if amount_cents <= 0 or total_weight <= 0:
        return {key: 0 for key, _ in items}
    shares: Dict[str, int] = {}
    fractions: List[Tuple[int, str]] = []
    for key, weight in items:
        base, remainder = divmod(amount_cents * weight, total_weight)
        shares[key] = base
        fractions.append((remainder, key))
    leftover = amount_cents - sum(shares.values())
    for _, key in sorted(fractions, key=lambda item: item[0], reverse=True)[:leftover]:
        shares[key] += 1
    return shares


def allocate_shortfall(
    shortfall_cents: int,
    weights: Dict[str, int],
    balances_cents: Dict[str, int],
) -> Tuple[Dict[str, int], int]:
    """按权重分摊，单家扣到保证金余额为止，溢出在其他人之间继续分配。

    返回 (各家分摊额, 无法分摊的余额)。
    """
    charges = {code: 0 for code in weights}
    remaining = max(0, shortfall_cents)
    while remaining > 0:
        candidates = [
            code
            for code in weights
            if weights[code] > 0 and balances_cents[code] - charges[code] > 0
        ]
        if not candidates:
            break
        shares = proportional_cents(
            remaining, [(code, weights[code]) for code in candidates]
        )
        overflow = 0
        for code, share in shares.items():
            cap = balances_cents[code] - charges[code]
            if share >= cap:
                charges[code] = balances_cents[code]
                overflow += share - cap
            else:
                charges[code] += share
        if overflow == 0:
            remaining = 0
        else:
            remaining = overflow
    return charges, remaining


def build_default_plan(
    *,
    loss_cents: int,
    defaulter_balance_cents: int,
    others: Sequence[str],
    balances_cents: Dict[str, int],
    weights: Dict[str, int],
    basis: str,
) -> Dict[str, Any]:
    """生成违约处置方案：先扣违约方，再按权重在其余参与者间封顶分摊。"""
    loss_cents = max(0, loss_cents)
    cover = min(loss_cents, max(0, int(defaulter_balance_cents)))
    shortfall = loss_cents - cover
    charges, uncovered = allocate_shortfall(shortfall, weights, balances_cents)
    total_weight = sum(weights.values())
    allocations: List[Dict[str, Any]] = []
    for code in others:
        allocated = int(charges.get(code, 0))
        if basis == BASIS_EQUAL and others:
            ratio = 1.0 / len(others)
        elif total_weight > 0:
            ratio = weights.get(code, 0) / total_weight
        else:
            ratio = 0.0
        allocations.append(
            {
                "participant": code,
                "weight_cents": int(weights.get(code, 0)),
                "ratio": round(ratio, 6),
                "allocated_cents": allocated,
                "balance_after_cents": int(balances_cents.get(code, 0)) - allocated,
            }
        )
    return {
        "basis": basis,
        "loss_cents": loss_cents,
        "defaulter_cover_cents": cover,
        "shortfall_cents": shortfall,
        "allocated_cents": shortfall - uncovered,
        "outstanding_cents": uncovered,
        "allocations": allocations,
    }
