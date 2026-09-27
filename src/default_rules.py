"""违约处置分摊规则：损失计算、成交占比统计与限额分摊，全部为纯函数。"""
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Tuple


# 成交占比统计窗口：过去一个月按30天计
WINDOW_DAYS = 30


def to_cents(amount: float) -> int:
    return int(round(float(amount) * 100))


def from_cents(cents: int) -> float:
    return round(int(cents) / 100, 2)


def loss_cents(payload: Dict[str, Any]) -> int:
    """失败单据的未交付金额：应付净额减去已付资金，下限为0。"""
    due = payload.get("approved_amount", payload.get("net_amount", 0))
    paid = payload.get("cash_paid", 0)
    return max(0, to_cents(due) - to_cents(paid))


def trading_volumes(records: Iterable[Dict[str, Any]], exclude: str = "", now: datetime = None) -> Dict[str, int]:
    """统计过去一个月已交收记录的成交金额（分），按参与者(org)汇总，可排除违约方。"""
    now = now or datetime.now().astimezone()
    since = now - timedelta(days=WINDOW_DAYS)
    volumes: Dict[str, int] = {}
    for record in records:
        if record.get("state") != "settled":
            continue
        org = (record.get("org") or "").strip()
        if not org or org == exclude:
            continue
        try:
            updated = datetime.fromisoformat(str(record.get("updated_at")))
        except ValueError:
            continue
        if updated.tzinfo is None:
            updated = updated.astimezone()
        if updated < since:
            continue
        amount = record.get("payload", {}).get("net_amount", 0)
        volumes[org] = volumes.get(org, 0) + to_cents(amount)
    return volumes


def allocate_loss(shortfall_cents: int, volumes: Dict[str, int], capacities: Dict[str, int]) -> Tuple[Dict[str, int], int]:
    """按成交占比分摊缺口，每家最多扣到保证金余额（capacity）。

    被限额卡住的部分会在仍有额度的参与者之间继续按比例再分配，
    直到分完或所有参与者额度耗尽。返回(分摊结果{参与者: 金额(分)}, 未补足缺口(分))。
    """
    allocation = {pid: 0 for pid in volumes}
    remaining = max(0, int(shortfall_cents))
    while remaining > 0:
        candidates = [pid for pid in volumes if volumes[pid] > 0 and allocation[pid] < capacities.get(pid, 0)]
        if not candidates:
            break
        total_volume = sum(volumes[pid] for pid in candidates)
        distributed = 0
        for pid in candidates:
            ideal = remaining * volumes[pid] // total_volume
            room = capacities[pid] - allocation[pid]
            take = min(ideal, room)
            if take > 0:
                allocation[pid] += take
                distributed += take
        remaining -= distributed
        if distributed == 0 and remaining > 0:
            # 占比取整产生的尾差，按成交量从大到小逐分补齐
            for pid in sorted(candidates, key=lambda p: volumes[p], reverse=True):
                if remaining <= 0:
                    break
                if allocation[pid] < capacities[pid]:
                    allocation[pid] += 1
                    remaining -= 1
            if all(allocation[pid] >= capacities.get(pid, 0) for pid in candidates):
                break
    return allocation, remaining


def share_ratios(volumes: Dict[str, int]) -> Dict[str, float]:
    """各参与者成交占比（0~1），用于分摊明细展示。"""
    total = sum(volumes.values())
    if total <= 0:
        return {pid: 0.0 for pid in volumes}
    return {pid: round(volumes[pid] / total, 6) for pid in volumes}
