import unittest

from src import allocation as alloc


class TurnoverWeightsTest(unittest.TestCase):
    def _record(self, record_id, participant, gross, state="settled"):
        return {
            "id": record_id,
            "state": state,
            "created_at": alloc.utcnow().isoformat(),
            "payload": {"participant": participant, "gross_amount": gross},
        }

    def test_weights_follow_turnover_share(self):
        records = [
            self._record(1, "A", 100.0),
            self._record(2, "B", 300.0),
            self._record(3, "C", 0.0),
            self._record(4, "A", 100.0, state="reversed"),
        ]
        weights, basis = alloc.turnover_weights(records, ["A", "B", "C"])
        self.assertEqual(basis, alloc.BASIS_TURNOVER)
        self.assertEqual(weights, {"A": 10000, "B": 30000, "C": 0})

    def test_equal_fallback_when_no_turnover(self):
        records = [self._record(1, "A", 100.0, state="reversed")]
        weights, basis = alloc.turnover_weights(records, ["A", "B", "C"])
        self.assertEqual(basis, alloc.BASIS_EQUAL)
        self.assertEqual(weights, {"A": 1, "B": 1, "C": 1})

    def test_exclude_trigger_record(self):
        # 触发违约的失败单据被排除后窗口无成交，回落为均摊权重
        records = [self._record(9, "B", 900.0)]
        weights, basis = alloc.turnover_weights(records, ["B"], exclude_record_id=9)
        self.assertEqual(weights, {"B": 1})
        self.assertEqual(basis, alloc.BASIS_EQUAL)

    def test_exclude_only_skips_trigger(self):
        records = [self._record(9, "B", 900.0), self._record(10, "B", 100.0)]
        weights, basis = alloc.turnover_weights(records, ["B"], exclude_record_id=9)
        self.assertEqual(weights, {"B": 10000})
        self.assertEqual(basis, alloc.BASIS_TURNOVER)


class AllocationTest(unittest.TestCase):
    def test_proportional_split_exact(self):
        shares = alloc.proportional_cents(10000, [("A", 1), ("B", 3)])
        self.assertEqual(shares, {"A": 2500, "B": 7500})

    def test_largest_remainder_covers_leftover(self):
        shares = alloc.proportional_cents(100, [("A", 1), ("B", 1), ("C", 1)])
        self.assertEqual(sum(shares.values()), 100)

    def test_cap_spills_over_to_others(self):
        # 缺口100元，按权重A应承担90但保证金仅10元，溢出由B承担
        charges, uncovered = alloc.allocate_shortfall(
            10000, {"A": 90, "B": 10}, {"A": 1000, "B": 100000}
        )
        self.assertEqual(charges, {"A": 1000, "B": 9000})
        self.assertEqual(uncovered, 0)

    def test_uncovered_when_all_margin_exhausted(self):
        charges, uncovered = alloc.allocate_shortfall(
            10000, {"A": 1, "B": 1}, {"A": 100, "B": 200}
        )
        self.assertEqual(sum(charges.values()), 300)
        self.assertEqual(uncovered, 9700)

    def test_full_plan_defaulter_first_then_allocation(self):
        plan = alloc.build_default_plan(
            loss_cents=15000,
            defaulter_balance_cents=5000,
            others=["A", "B"],
            balances_cents={"A": 100000, "B": 100000},
            weights={"A": 30000, "B": 10000},
            basis=alloc.BASIS_TURNOVER,
        )
        self.assertEqual(plan["defaulter_cover_cents"], 5000)
        self.assertEqual(plan["shortfall_cents"], 10000)
        self.assertEqual(plan["outstanding_cents"], 0)
        self.assertEqual(
            {item["participant"]: item["allocated_cents"] for item in plan["allocations"]},
            {"A": 7500, "B": 2500},
        )
