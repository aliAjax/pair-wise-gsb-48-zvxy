import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError


ADMIN = Actor("admin", "admin")
OFFICER = Actor("officer", "settlement_officer")
TRADER = Actor("trader", "trader")


def make_data(day, **overrides):
    data = {
        "instrument": "ACME", "side": "buy", "quantity": 1000, "price": 12.5,
        "fees": 18.0, "currency": "CNY", "settlement_day": day,
        "corporate_action": "none", "action_ratio": 1,
    }
    data.update(overrides)
    return data


SETTLE_DATA = {"delivered_quantity": 1000, "cash_paid": 12518.0}


class DefaultWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        for code, name, margin in [("D", "违约方", 1000.0), ("A", "承接方甲", 100000.0), ("B", "承接方乙", 100000.0)]:
            self.service.register_participant(ADMIN, {"code": code, "name": name, "initial_margin": margin})
        self._day = 100

    def tearDown(self):
        self.temp.cleanup()

    def _day_next(self):
        self._day += 1
        return self._day

    def _create(self, ref, participant=None):
        data = make_data(self._day_next())
        if participant:
            data["participant"] = participant
        return self.service.create(TRADER, ref, data)

    def _approve(self, record):
        return self.service.act(OFFICER, record["id"], record["version"], "approve", {})

    def _fail(self, ref, participant="D"):
        record = self._create(ref, participant)
        record = self._approve(record)
        return self.service.act(OFFICER, record["id"], record["version"], "fail", {"fail_reason": "券款不足"})

    def _drain_margin(self, *codes):
        for code in codes:
            balance = self.service.repository.get_participant(code)["margin_balance"]
            if balance:
                self.service.repository.topup_margin(code, -balance)

    def _declare_latest_failed(self, **data):
        failed = self.service.list_records(ADMIN, state="failed")[0]
        payload = {"fail_reason": "未补券款"}
        payload.update(data)
        return self.service.declare_default(ADMIN, failed["id"], failed["version"], payload)

    def test_default_loss_composition_and_turnover_allocation(self):
        # D 有一笔违约单；A 另有一笔在窗成交；B 无成交。违约单本身不计入权重
        failed = self._fail("TRD-D")
        self._create("TRD-A", "A")
        case = self.service.declare_default(
            ADMIN, failed["id"], failed["version"], {"fail_reason": "未补券款"}
        )

        composition = case["loss_composition"]
        self.assertEqual(composition["loss_amount"], 12518.0)
        self.assertEqual(composition["defaulter_cover"], 1000.0)
        self.assertEqual(composition["shortfall_amount"], 11518.0)
        self.assertEqual(composition["outstanding_amount"], 0.0)
        charges = {row["participant"]: row["allocated_amount"] for row in case["allocations"]}
        self.assertEqual(charges["A"], 11518.0)
        self.assertEqual(charges["B"], 0.0)

        participants = {p["code"]: p for p in self.service.list_participants(ADMIN)}
        self.assertEqual(participants["D"]["margin_balance_amount"], 0.0)
        self.assertEqual(participants["D"]["status"], "frozen")
        self.assertEqual(participants["A"]["margin_balance_amount"], 100000.0 - 11518.0)

        record = self.service.get_record(ADMIN, failed["id"])
        self.assertEqual(record["state"], "defaulted")
        self.assertEqual(case["basis"], "turnover")

    def test_cap_margin_balance_leaves_outstanding(self):
        # A 保证金只有1000元且独占成交占比：扣到0后缺口仍无法弥补
        self._create("TRD-A", "A")
        self._fail("TRD-D")
        self._drain_margin("A", "B")
        self.service.topup_margin(ADMIN, "A", {"amount": 1000.0})
        case = self._declare_latest_failed()
        self.assertEqual(case["loss_composition"]["defaulter_cover"], 1000.0)
        self.assertEqual(case["loss_composition"]["allocated_amount"], 1000.0)
        self.assertEqual(case["loss_composition"]["outstanding_amount"], 10518.0)
        self.assertEqual(case["state"], "open")

    def test_frozen_defaulter_cannot_create_or_settle(self):
        # 违约前先留有一张 D 名下已复核未完成单据
        pending = self._approve(self._create("TRD-PENDING", "D"))
        self._create("TRD-A", "A")
        self._fail("TRD-D")
        self._declare_latest_failed()

        with self.assertRaises(ValidationError):
            self._create("TRD-D2", "D")
        with self.assertRaises(ValidationError):
            self.service.act(OFFICER, pending["id"], pending["version"], "settle", SETTLE_DATA)

    def test_takeover_then_settle(self):
        pending = self._approve(self._create("TRD-PENDING", "D"))
        self._create("TRD-A", "A")
        self._fail("TRD-D")
        self._declare_latest_failed()

        taken = self.service.takeover(OFFICER, pending["id"], pending["version"], "B", "违约转手")
        self.assertEqual(taken["payload"]["participant"], "B")
        self.assertEqual(taken["payload"]["takeover_from"], "D")
        settled = self.service.act(OFFICER, taken["id"], taken["version"], "settle", SETTLE_DATA)
        self.assertEqual(settled["state"], "settled")

        with self.assertRaises(Conflict):
            self.service.takeover(OFFICER, settled["id"], settled["version"], "A", "x")

    def test_partial_then_full_recovery_restores_defaulter(self):
        self._create("TRD-A", "A")
        self._fail("TRD-D")
        self._drain_margin("A", "B")  # 其余人无力分摊，全部缺口挂账
        case = self._declare_latest_failed()
        self.assertEqual(case["loss_composition"]["outstanding_amount"], 11518.0)

        partial = self.service.recover_default(ADMIN, case["id"], {"amount": 1000.0})
        self.assertEqual(partial["state"], "open")
        self.assertFalse(partial["recovery_result"]["restored"])
        self.assertEqual(partial["recovery_result"]["defaulter_status"], "frozen")
        self.assertEqual(self.service.get_participant(ADMIN, "D")["status"], "frozen")
        with self.assertRaises(Conflict):
            self.service.recover_default(ADMIN, case["id"], {"amount": 999999.0})

        remaining = partial["loss_composition"]["outstanding_amount"]
        full = self.service.recover_default(ADMIN, case["id"], {"amount": remaining})
        self.assertEqual(full["state"], "closed")
        self.assertTrue(full["recovery_result"]["restored"])
        self.assertEqual(full["recovery_result"]["defaulter_status"], "active")
        self.assertEqual(self.service.get_participant(ADMIN, "D")["status"], "active")

        # 恢复后可重新新建与交收
        record = self._approve(self._create("TRD-OK", "D"))
        record = self.service.act(OFFICER, record["id"], record["version"], "settle", SETTLE_DATA)
        self.assertEqual(record["state"], "settled")

        with self.assertRaises(Conflict):
            self.service.recover_default(ADMIN, case["id"], {"amount": 1.0})

    def test_audit_and_ledger_trail(self):
        self._create("TRD-A", "A")
        failed = self._fail("TRD-D")
        case = self._declare_latest_failed()
        timeline = self.service.timeline(ADMIN, failed["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions, ["created", "approve", "fail", "declare_default"])
        ledger = self.service.margin_ledger(ADMIN, "D")
        charge = next(item for item in ledger if item["reason"] == "default_charge")
        self.assertEqual(charge["change_amount_amount"], -1000.0)
        recovered = self.service.get_loss_case(ADMIN, case["id"])
        self.assertEqual(recovered["record_id"], failed["id"])

    def test_no_participant_record_cannot_default(self):
        record = self._create("TRD-LEGACY")
        record = self._approve(record)
        record = self.service.act(OFFICER, record["id"], record["version"], "fail", {"fail_reason": "x"})
        with self.assertRaises(ValidationError):
            self.service.declare_default(ADMIN, record["id"], record["version"], {"fail_reason": "x"})
