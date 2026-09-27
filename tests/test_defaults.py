import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.default_rules import allocate_loss, loss_cents
from src.domain import Actor, Conflict, PermissionDenied


CLEARING = Actor("clearing", "clearing_officer", "CCP")
OFFICER = Actor("officer", "settlement_officer", "CCP")


def org_actor(org, role="trader"):
    return Actor("%s-user" % org.lower(), role, org)


def record_data(quantity, price, fees=0.0, instrument="ACME"):
    return {'instrument': instrument, 'side': 'buy', 'quantity': quantity, 'price': price, 'fees': fees,
            'currency': 'CNY', 'settlement_day': 2, 'corporate_action': 'none', 'action_ratio': 1.0}


class AllocationRulesTest(unittest.TestCase):
    def test_proportional_allocation(self):
        allocation, uncovered = allocate_loss(100000, {'A': 10000, 'B': 30000}, {'A': 10 ** 9, 'B': 10 ** 9})
        self.assertEqual(allocation, {'A': 25000, 'B': 75000})
        self.assertEqual(uncovered, 0)

    def test_caps_trigger_redistribution(self):
        allocation, uncovered = allocate_loss(100000, {'A': 10000, 'B': 30000}, {'A': 10000, 'B': 10 ** 9})
        self.assertEqual(allocation['A'], 10000)
        self.assertEqual(allocation['B'], 90000)
        self.assertEqual(uncovered, 0)

    def test_exhausted_capacity_leaves_uncovered(self):
        allocation, uncovered = allocate_loss(100000, {'A': 10000, 'B': 30000}, {'A': 10000, 'B': 20000})
        self.assertEqual(allocation, {'A': 10000, 'B': 20000})
        self.assertEqual(uncovered, 70000)

    def test_no_volume_means_no_allocation(self):
        allocation, uncovered = allocate_loss(100000, {}, {'A': 10 ** 9})
        self.assertEqual(allocation, {})
        self.assertEqual(uncovered, 100000)

    def test_loss_is_undelivered_amount(self):
        self.assertEqual(loss_cents({'approved_amount': 2000.0, 'cash_paid': 500.0}), 150000)
        self.assertEqual(loss_cents({'net_amount': 2000.0}), 200000)
        self.assertEqual(loss_cents({'approved_amount': 100.0, 'cash_paid': 500.0}), 0)


class DefaultHandlingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.defaults = self.service.defaults

    def tearDown(self):
        self.temp.cleanup()

    def register(self, participant_id, margin):
        return self.defaults.register_participant(CLEARING, {
            "participant_id": participant_id, "name": "参与者%s" % participant_id, "margin_balance": margin})

    def make_record(self, org, reference, quantity, price, fees=0.0, settle=False, instrument="ACME"):
        record = self.service.create(org_actor(org), reference, record_data(quantity, price, fees, instrument))
        record = self.service.act(OFFICER, record["id"], record["version"], "approve", {})
        if settle:
            net = round(quantity * price + fees, 2)
            record = self.service.act(OFFICER, record["id"], record["version"], "settle",
                                      {"delivered_quantity": quantity, "cash_paid": net})
        return record

    def participant(self, participant_id):
        return self.defaults.participant_detail(CLEARING, participant_id)["participant"]

    def only_case(self):
        cases = self.defaults.list_cases(CLEARING)
        self.assertEqual(len(cases), 1)
        return self.defaults.case_detail(CLEARING, cases[0]["id"])

    def test_failure_opens_case_and_allocates_by_trading_share(self):
        self.register("P1", 1000.0)
        self.register("P2", 5000.0)
        self.register("P3", 3000.0)
        self.make_record("P2", "TRD-P2", 10, 10.0, settle=True)   # 成交100
        self.make_record("P3", "TRD-P3", 30, 10.0, settle=True)   # 成交300
        failed = self.make_record("P1", "TRD-P1", 200, 10.0)      # 净额2000
        failed = self.service.act(OFFICER, failed["id"], failed["version"], "fail", {"fail_reason": "券款不足"})
        self.assertEqual(failed["state"], "failed")

        detail = self.only_case()
        loss = detail["loss"]
        self.assertEqual(loss["loss_amount"], 2000.0)
        self.assertEqual(loss["defaulter_margin_used"], 1000.0)   # 先扣违约方保证金
        self.assertEqual(loss["allocated_total"], 1000.0)         # 缺口按1:3分摊
        self.assertEqual(loss["uncovered_amount"], 0.0)
        self.assertEqual(detail["case"]["status"], "recovered")
        self.assertTrue(detail["recovery"]["recovered"])

        shares = {a["participant_id"]: a for a in detail["allocations"]}
        self.assertEqual(shares["P2"]["share_ratio"], 0.25)
        self.assertEqual(shares["P2"]["amount"], 250.0)
        self.assertEqual(shares["P3"]["share_ratio"], 0.75)
        self.assertEqual(shares["P3"]["amount"], 750.0)

        self.assertEqual(self.participant("P1")["margin_balance"], 0.0)
        self.assertEqual(self.participant("P2")["margin_balance"], 4750.0)
        self.assertEqual(self.participant("P3")["margin_balance"], 2250.0)
        self.assertEqual(self.participant("P1")["status"], "active")  # 已补足，不暂停

    def test_uncovered_loss_suspends_defaulter_until_recovered(self):
        self.register("D", 500.0)
        self.register("A", 100.0)
        self.register("B", 200.0)
        self.make_record("A", "TRD-A", 10, 10.0, settle=True)
        self.make_record("B", "TRD-B", 10, 10.0, settle=True)
        pending = self.make_record("D", "TRD-D2", 10, 10.0)       # 违约方未完成单据（违约前已存在）
        failed = self.make_record("D", "TRD-D1", 200, 10.0)       # 损失2000
        self.service.act(OFFICER, failed["id"], failed["version"], "fail", {"fail_reason": "拒付"})

        detail = self.only_case()
        self.assertEqual(detail["loss"]["defaulter_margin_used"], 500.0)
        self.assertEqual(detail["loss"]["allocated_total"], 300.0)  # A、B各以保证金余额为限
        self.assertEqual(detail["loss"]["uncovered_amount"], 1200.0)
        self.assertEqual(detail["case"]["status"], "open")
        self.assertEqual(self.participant("D")["status"], "suspended")

        # 没补足前：违约方不能新建
        with self.assertRaises(Conflict):
            self.service.create(org_actor("D"), "TRD-D3", record_data(1, 10.0))
        # 也不能交收自己的单据
        with self.assertRaises(Conflict):
            self.service.act(OFFICER, pending["id"], pending["version"], "settle",
                             {"delivered_quantity": 10, "cash_paid": 100.0})

        # 未完成单据可由其他参与者接手并继续交收
        taken = self.service.act(org_actor("A"), pending["id"], pending["version"], "takeover", {})
        self.assertEqual(taken["org"], "A")
        self.assertEqual(taken["payload"]["taken_over_from"], "D")
        settled = self.service.act(OFFICER, taken["id"], taken["version"], "settle",
                                   {"delivered_quantity": 10, "cash_paid": 100.0})
        self.assertEqual(settled["state"], "settled")

        # 违约方补足保证金后恢复处置
        self.defaults.adjust_margin(CLEARING, "D", {"kind": "deposit", "amount": 2000.0})
        recovered = self.defaults.recover(CLEARING, detail["case"]["id"])
        self.assertEqual(recovered["case"]["status"], "recovered")
        self.assertEqual(recovered["recovery"]["remaining_amount"], 0.0)
        self.assertEqual(recovered["recovery"]["recovered_amount"], 2000.0)
        self.assertEqual(self.participant("D")["margin_balance"], 800.0)
        self.assertEqual(self.participant("D")["status"], "active")

        # 补足后恢复正常
        self.service.create(org_actor("D"), "TRD-D3", record_data(1, 10.0))

    def test_takeover_guards(self):
        self.register("D", 0.0)
        self.register("A", 0.0)
        pending = self.make_record("D", "TRD-D1", 10, 10.0)
        # 持有方未违约暂停，不能接手
        with self.assertRaises(Conflict):
            self.service.act(org_actor("A"), pending["id"], pending["version"], "takeover", {})
        # 让D违约（无成交量参与者，缺口全部未补足）
        failed = self.make_record("D", "TRD-D2", 50, 10.0)
        self.service.act(OFFICER, failed["id"], failed["version"], "fail", {"fail_reason": "违约"})
        self.assertEqual(self.participant("D")["status"], "suspended")
        # 本方不能接手自己的单据
        with self.assertRaises(Conflict):
            self.service.act(org_actor("D"), pending["id"], pending["version"], "takeover", {})
        # 已完成单据不能接手
        settled = self.make_record("A", "TRD-A1", 10, 10.0, settle=True, instrument="OTHER")
        with self.assertRaises(Conflict):
            self.service.act(org_actor("A"), settled["id"], settled["version"], "takeover", {})

    def test_participant_management_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.defaults.register_participant(Actor("t", "trader", "P1"),
                                               {"participant_id": "P1", "name": "x", "margin_balance": 0})
        with self.assertRaises(PermissionDenied):
            self.defaults.list_participants(Actor("x", "outsider"))
        with self.assertRaises(PermissionDenied):
            self.defaults.adjust_margin(Actor("t", "trader", "P1"), "P1", {"kind": "deposit", "amount": 1})
        self.register("P1", 100.0)
        participant = self.defaults.adjust_margin(CLEARING, "P1", {"kind": "withdraw", "amount": 40.0})
        self.assertEqual(participant["margin_balance"], 60.0)
        detail = self.defaults.participant_detail(CLEARING, "P1")
        self.assertEqual(len(detail["margin_moves"]), 2)  # 注册存入 + 提取


if __name__ == "__main__":
    unittest.main()
