from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from settlement_ledger.api import JsonApplication
from settlement_ledger.calculation import (
    aggregate_lines,
    build_event_line,
    diff_versions,
)
from settlement_ledger.clock import FrozenClock, parse_utc, utc_text
from settlement_ledger.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from settlement_ledger.service import SettlementService


RULES = {
    "committed_availability_percent": "95",
    "compensation_price_cny_per_mwh": "400",
    "maintenance_notice_hours": 24,
    "category_rules": {
        "equipment_failure": {"counts_toward_availability": True, "compensable": False},
        "sea_condition": {"counts_toward_availability": False, "compensable": False},
        "dispatch_curtailment": {"counts_toward_availability": False, "compensable": True},
        "planned_maintenance": {"counts_toward_availability": False, "compensable": False},
    },
}

CAPACITY = Decimal("300")


def downtime_event(event_id, category, starts_at, ends_at, derate="100", reported_at=None):
    return {
        "event_id": event_id,
        "category": category,
        "starts_at": parse_utc(starts_at),
        "ends_at": parse_utc(ends_at),
        "derate_percent": derate,
        "energy_mwh": None,
        "reported_at": None if reported_at is None else parse_utc(reported_at),
    }


class CalculationTests(unittest.TestCase):
    def build(self, event, period_start, period_end, rules=None):
        return build_event_line(
            event=event,
            period_start=parse_utc(period_start),
            period_end=parse_utc(period_end),
            capacity_mw=CAPACITY,
            rules=rules or RULES,
            utc_text=utc_text,
        )

    def test_cross_boundary_event_is_split_by_true_duration(self) -> None:
        event = downtime_event("cur-1", "dispatch_curtailment", "2026-09-30T12:00:00Z", "2026-10-01T12:00:00Z")
        september = self.build(event, "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
        october = self.build(event, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
        self.assertEqual(september["overlap_seconds"], 12 * 3600)
        self.assertEqual(october["overlap_seconds"], 12 * 3600)
        self.assertEqual(september["portion_starts_at"], "2026-09-30T12:00:00Z")
        self.assertEqual(september["portion_ends_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(september["energy_mwh"], "3600.000")
        self.assertEqual(october["energy_mwh"], "3600.000")
        outside = self.build(event, "2026-11-01T00:00:00Z", "2026-12-01T00:00:00Z")
        self.assertIsNone(outside)

    def test_generation_energy_is_prorated_across_boundary(self) -> None:
        event = {
            "event_id": "gen-1",
            "category": "generation",
            "starts_at": parse_utc("2026-09-30T00:00:00Z"),
            "ends_at": parse_utc("2026-10-02T00:00:00Z"),
            "derate_percent": None,
            "energy_mwh": "4800",
            "reported_at": None,
        }
        september = self.build(event, "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
        self.assertEqual(september["energy_mwh"], "2400.000")
        self.assertIn("按真实时长折算", september["reason"])

    def test_maintenance_notice_decides_exclusion(self) -> None:
        timely = downtime_event(
            "mnt-1", "planned_maintenance", "2026-09-22T00:00:00Z", "2026-09-22T06:00:00Z",
            reported_at="2026-09-20T00:00:00Z",
        )
        line = self.build(timely, "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
        self.assertFalse(line["adopted"])
        self.assertIn("满足24小时报备门槛", line["reason"])
        late = downtime_event(
            "mnt-2", "planned_maintenance", "2026-09-22T00:00:00Z", "2026-09-22T06:00:00Z",
            reported_at="2026-09-21T12:00:00Z",
        )
        line = self.build(late, "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
        self.assertTrue(line["adopted"])
        self.assertIn("不足24小时门槛", line["reason"])

    def test_curtailment_is_excluded_and_compensated(self) -> None:
        event = downtime_event("cur-1", "dispatch_curtailment", "2026-09-18T00:00:00Z", "2026-09-19T00:00:00Z")
        line = self.build(event, "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
        self.assertFalse(line["adopted"])
        self.assertTrue(line["compensable"])
        self.assertEqual(line["energy_mwh"], "7200.000")
        self.assertEqual(line["compensation_cny"], "2880000.00")

    def test_aggregate_availability_and_compensation(self) -> None:
        lines = [
            self.build(downtime_event("f", "equipment_failure", "2026-09-05T06:00:00Z", "2026-09-05T18:00:00Z", derate="50"), "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z"),
            self.build(downtime_event("s", "sea_condition", "2026-09-12T00:00:00Z", "2026-09-13T00:00:00Z"), "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z"),
            self.build(downtime_event("c", "dispatch_curtailment", "2026-09-18T00:00:00Z", "2026-09-19T00:00:00Z"), "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z"),
        ]
        result = aggregate_lines(lines, period_seconds=30 * 24 * 3600, rules=RULES)
        self.assertEqual(result["unavailable_hours"], "6.000")
        self.assertEqual(result["availability_percent"], "99.1667")
        self.assertEqual(result["accounted_loss_mwh"], "1800.000")
        self.assertEqual(result["excluded_mwh"], "14400.000")
        self.assertEqual(result["compensation_cny"], "2880000.00")
        self.assertTrue(result["availability_met"])
        again = aggregate_lines(lines, period_seconds=30 * 24 * 3600, rules=RULES)
        self.assertEqual(result, again)

    def test_diff_versions_reports_added_removed_and_totals(self) -> None:
        before = [self.build(downtime_event("a", "equipment_failure", "2026-09-05T00:00:00Z", "2026-09-05T06:00:00Z"), "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")]
        after = before + [self.build(downtime_event("b", "dispatch_curtailment", "2026-09-06T00:00:00Z", "2026-09-06T06:00:00Z"), "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")]
        seconds = 30 * 24 * 3600
        diff = diff_versions(
            before, after,
            aggregate_lines(before, period_seconds=seconds, rules=RULES),
            aggregate_lines(after, period_seconds=seconds, rules=RULES),
        )
        self.assertEqual([line["event_id"] for line in diff["added_lines"]], ["b"])
        self.assertEqual(diff["removed_lines"], [])
        self.assertEqual(diff["totals"]["compensation_cny"]["delta"], "720000.00")


class SettlementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc))
        self.service = SettlementService(self.connection, self.clock)
        for user_id, role in (
            ("settle", "settlement"), ("ops", "production"), ("fin", "finance"), ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_site("settle", {
            "site_id": "fanshi-one", "name": "帆石一海上风电场",
            "capacity_mw": "300", "timezone": "Asia/Shanghai",
        })
        self.service.create_period("settle", {
            "period_id": "2026-09", "starts_at": "2026-09-01T00:00:00Z", "ends_at": "2026-10-01T00:00:00Z",
        })
        self.service.record_commitment("settle", {
            "commitment_id": "cmt-1", "site_id": "fanshi-one", "rules": RULES,
        })
        self.service.fix_commitment("settle", "cmt-1")
        self._event_seq = 0

    def tearDown(self) -> None:
        self.connection.close()

    def record(self, **event) -> dict:
        self._event_seq += 1
        payload = {"site_id": "fanshi-one", "idempotency_key": f"key-{self._event_seq}", **event}
        return self.service.record_event("settle", payload)

    def seed_events(self) -> None:
        self.record(event_id="gen-1", category="generation",
                    starts_at="2026-09-01T00:00:00Z", ends_at="2026-10-01T00:00:00Z", energy_mwh="158000")
        self.record(event_id="fail-1", category="equipment_failure",
                    starts_at="2026-09-05T06:00:00Z", ends_at="2026-09-05T18:00:00Z", derate_percent="50")
        self.record(event_id="cur-1", category="dispatch_curtailment",
                    starts_at="2026-09-18T00:00:00Z", ends_at="2026-09-19T00:00:00Z", derate_percent="100")
        self.record(event_id="mnt-1", category="planned_maintenance",
                    starts_at="2026-09-22T00:00:00Z", ends_at="2026-09-22T06:00:00Z",
                    derate_percent="100", reported_at="2026-09-20T00:00:00Z")

    def issue(self, **kwargs):
        return self.service.issue_ledger("settle", "fanshi-one", "2026-09", **kwargs)

    def test_period_requires_end_after_start(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_period("settle", {
                "period_id": "bad", "starts_at": "2026-10-01T00:00:00Z", "ends_at": "2026-09-01T00:00:00Z",
            })

    def test_commitment_rules_reject_double_counting(self) -> None:
        bad = json.loads(json.dumps(RULES))
        bad["category_rules"]["dispatch_curtailment"] = {"counts_toward_availability": True, "compensable": True}
        with self.assertRaises(ValidationFailed):
            self.service.record_commitment("settle", {
                "commitment_id": "cmt-bad", "site_id": "fanshi-one", "rules": bad,
            })

    def test_issue_requires_fixed_commitment(self) -> None:
        other = dict(RULES, committed_availability_percent="96")
        self.service.record_commitment("settle", {
            "commitment_id": "cmt-2", "site_id": "fanshi-one", "rules": other,
        })
        with self.assertRaises(InvalidState):
            self.issue(commitment_id="cmt-2")
        self.service.fix_commitment("settle", "cmt-2")
        ledger = self.issue(commitment_id="cmt-2")
        self.assertEqual(ledger["commitment_id"], "cmt-2")

    def test_event_validation_and_idempotent_replay(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.record(event_id="bad-1", category="generation",
                        starts_at="2026-09-01T00:00:00Z", ends_at="2026-09-02T00:00:00Z",
                        energy_mwh="100", derate_percent="50")
        with self.assertRaises(ValidationFailed):
            self.record(event_id="bad-2", category="planned_maintenance",
                        starts_at="2026-09-01T00:00:00Z", ends_at="2026-09-02T00:00:00Z",
                        derate_percent="100")
        payload = {
            "event_id": "cur-1", "site_id": "fanshi-one", "category": "dispatch_curtailment",
            "starts_at": "2026-09-18T00:00:00Z", "ends_at": "2026-09-19T00:00:00Z",
            "derate_percent": "100", "idempotency_key": "dup-key",
        }
        first = self.service.record_event("settle", payload)
        self.assertEqual(first, self.service.record_event("settle", payload))
        with self.assertRaises(Conflict):
            self.service.record_event("settle", dict(payload, derate_percent="90"))

    def test_issue_freezes_commitment_snapshot_and_replays(self) -> None:
        self.seed_events()
        ledger = self.issue()
        self.assertFalse(ledger["replayed"])
        self.assertEqual(ledger["version_no"], 1)
        self.assertEqual(ledger["state"], "issued")
        self.assertEqual(ledger["commitment_snapshot"], RULES)
        result = ledger["result"]
        self.assertEqual(result["generated_mwh"], "158000.000")
        self.assertEqual(result["accounted_loss_mwh"], "1800.000")
        self.assertEqual(result["compensation_cny"], "2880000.00")
        replay = self.issue()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["ledger_id"], ledger["ledger_id"])

    def test_closed_ledger_is_immutable_and_correction_carries_reason_and_diff(self) -> None:
        self.seed_events()
        first = self.issue()
        self.service.close_ledger("settle", first["ledger_id"])
        # 封账后迟到的数据不能直接改变原数字
        self.record(event_id="cur-late", category="dispatch_curtailment",
                    starts_at="2026-09-27T00:00:00Z", ends_at="2026-09-27T06:00:00Z", derate_percent="100")
        stored = self.service.ledger(first["ledger_id"])
        self.assertEqual(stored["result"]["compensation_cny"], "2880000.00")
        drift = self.service.verify_ledger("audit", first["ledger_id"])
        self.assertTrue(drift["result_matches"])
        self.assertTrue(drift["inputs_changed"])
        self.assertEqual([e["event_id"] for e in drift["late_events"]["arrived"]], ["cur-late"])
        # 更正版本必须带原因和差异明细
        with self.assertRaises(ValidationFailed):
            self.issue()
        second = self.issue(reason="补登9月27日调度限电时段")
        self.assertEqual(second["version_no"], 2)
        self.assertEqual(second["correction_reason"], "补登9月27日调度限电时段")
        self.assertEqual(second["supersedes_ledger_id"], first["ledger_id"])
        self.assertEqual([line["event_id"] for line in second["diff"]["added_lines"]], ["cur-late"])
        self.assertEqual(second["diff"]["totals"]["compensation_cny"]["delta"], "720000.00")
        self.assertEqual(second["result"]["compensation_cny"], "3600000.00")
        # 原版本数字保持不变
        self.assertEqual(self.service.ledger(first["ledger_id"])["result"]["compensation_cny"], "2880000.00")

    def test_correction_requires_closed_previous_version(self) -> None:
        self.seed_events()
        self.issue()
        self.record(event_id="cur-late", category="dispatch_curtailment",
                    starts_at="2026-09-27T00:00:00Z", ends_at="2026-09-27T06:00:00Z", derate_percent="100")
        with self.assertRaises(InvalidState):
            self.issue(reason="上一版未封账")

    def test_correction_reuses_frozen_commitment(self) -> None:
        self.seed_events()
        first = self.issue(commitment_id="cmt-1")
        self.service.close_ledger("settle", first["ledger_id"])
        changed = json.loads(json.dumps(RULES))
        changed["compensation_price_cny_per_mwh"] = "500"
        self.service.record_commitment("settle", {
            "commitment_id": "cmt-2", "site_id": "fanshi-one", "rules": changed,
        })
        self.service.fix_commitment("settle", "cmt-2")
        self.record(event_id="cur-late", category="dispatch_curtailment",
                    starts_at="2026-09-27T00:00:00Z", ends_at="2026-09-27T06:00:00Z", derate_percent="100")
        with self.assertRaises(Conflict):
            self.issue(commitment_id="cmt-2", reason="试图换用新承诺")
        second = self.issue(reason="补登迟到限电")
        self.assertEqual(second["commitment_id"], "cmt-1")
        self.assertEqual(second["commitment_snapshot"]["compensation_price_cny_per_mwh"], "400")

    def test_revised_event_replaces_original_in_correction(self) -> None:
        self.seed_events()
        self.record(event_id="dis-1", category="planned_maintenance",
                    starts_at="2026-09-26T08:00:00Z", ends_at="2026-09-26T10:00:00Z",
                    derate_percent="100", reported_at="2026-09-26T06:00:00Z")
        first = self.issue()
        self.assertEqual(first["result"]["accounted_loss_mwh"], "2400.000")
        self.service.close_ledger("settle", first["ledger_id"])
        self.service.revise_event("settle", "dis-1", {
            "event_id": "cur-26", "site_id": "fanshi-one", "category": "dispatch_curtailment",
            "starts_at": "2026-09-26T08:00:00Z", "ends_at": "2026-09-26T10:00:00Z",
            "derate_percent": "100", "idempotency_key": "rev-1",
        }, "电网确认该时段为调度限电")
        self.assertEqual(self.service.event("dis-1")["state"], "superseded")
        second = self.issue(reason="争议时段定性更正")
        removed = [line["event_id"] for line in second["diff"]["removed_lines"]]
        added = [line["event_id"] for line in second["diff"]["added_lines"]]
        self.assertEqual(removed, ["dis-1"])
        self.assertEqual(added, ["cur-26"])
        self.assertEqual(second["result"]["accounted_loss_mwh"], "1800.000")
        self.assertEqual(second["result"]["compensation_cny"], "3120000.00")
        with self.assertRaises(InvalidState):
            self.service.revise_event("settle", "dis-1", {
                "event_id": "cur-26b", "site_id": "fanshi-one", "category": "dispatch_curtailment",
                "starts_at": "2026-09-26T08:00:00Z", "ends_at": "2026-09-26T10:00:00Z",
                "derate_percent": "100", "idempotency_key": "rev-2",
            }, "重复更正")

    def test_compensation_requires_production_and_finance_confirmation(self) -> None:
        self.seed_events()
        ledger = self.issue()
        with self.assertRaises(InvalidState):
            self.service.confirm_compensation("ops", ledger["ledger_id"])
        self.service.close_ledger("settle", ledger["ledger_id"])
        with self.assertRaises(Forbidden):
            self.service.confirm_compensation("audit", ledger["ledger_id"])
        halfway = self.service.confirm_compensation("ops", ledger["ledger_id"])
        self.assertEqual(halfway["state"], "closed")
        self.assertEqual([c["party"] for c in halfway["confirmations"]], ["production"])
        with self.assertRaises(Conflict):
            self.service.confirm_compensation("ops", ledger["ledger_id"])
        done = self.service.confirm_compensation("fin", ledger["ledger_id"])
        self.assertEqual(done["state"], "confirmed")
        self.assertEqual([c["party"] for c in done["confirmations"]], ["finance", "production"])

    def test_verify_recompute_is_consistent_for_every_version(self) -> None:
        self.seed_events()
        first = self.issue()
        self.service.close_ledger("settle", first["ledger_id"])
        self.record(event_id="cur-late", category="dispatch_curtailment",
                    starts_at="2026-09-27T00:00:00Z", ends_at="2026-09-27T06:00:00Z", derate_percent="100")
        second = self.issue(reason="补登迟到限电")
        for ledger_id in (first["ledger_id"], second["ledger_id"]):
            verification = self.service.verify_ledger("audit", ledger_id)
            self.assertTrue(verification["result_matches"])
            self.assertEqual(verification["recomputed_result"], verification["stored_result"])
        self.assertFalse(self.service.verify_ledger("audit", second["ledger_id"])["inputs_changed"])

    def test_lines_trace_to_events_with_reasons(self) -> None:
        self.seed_events()
        ledger = self.issue()
        lines = self.service.ledger_lines("audit", ledger["ledger_id"], category="planned_maintenance")
        self.assertEqual(len(lines["lines"]), 1)
        line = lines["lines"][0]
        self.assertEqual(line["event_id"], "mnt-1")
        self.assertFalse(line["adopted"])
        self.assertIn("满足24小时报备门槛", line["reason"])
        curtailment = self.service.ledger_lines("audit", ledger["ledger_id"], event_id="cur-1")
        self.assertTrue(curtailment["lines"][0]["compensable"])

    def test_permissions_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.record_event("ops", {
                "event_id": "x", "site_id": "fanshi-one", "category": "generation",
                "starts_at": "2026-09-01T00:00:00Z", "ends_at": "2026-09-02T00:00:00Z",
                "energy_mwh": "1", "idempotency_key": "x-key",
            })
        with self.assertRaises(Forbidden):
            self.service.issue_ledger("ops", "fanshi-one", "2026-09")
        with self.assertRaises(Forbidden):
            self.service.create_site("fin", {
                "site_id": "s2", "name": "n", "capacity_mw": "1", "timezone": "UTC",
            })
        with self.assertRaises(Forbidden):
            self.service.audit_chain("settle")

    def test_audit_chain_detects_tampering(self) -> None:
        self.seed_events()
        self.issue()
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE settlement_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_missing_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.ledger(999)
        with self.assertRaises(NotFound):
            self.service.ledger_version("fanshi-one", "2026-09")
        with self.assertRaises(NotFound):
            self.service.close_ledger("settle", 999)

    def test_api_exposes_json_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("GET", "/ledgers/1")
        self.assertEqual(missing_actor.status, 422)
        unknown = app.handle("GET", "/nope", {"X-Actor-Id": "audit"})
        self.assertEqual(unknown.status, 404)
        self.seed_events()
        issued = app.handle("POST", "/ledgers/issue", {"X-Actor-Id": "settle"},
                            json.dumps({"site_id": "fanshi-one", "period_id": "2026-09"}).encode())
        self.assertEqual(issued.status, 201)
        ledger_id = issued.body["ledger_id"]
        closed = app.handle("POST", f"/ledgers/{ledger_id}/close", {"X-Actor-Id": "settle"})
        self.assertEqual(closed.status, 200)
        confirmed = app.handle("POST", f"/ledgers/{ledger_id}/confirm", {"X-Actor-Id": "ops"})
        self.assertEqual(confirmed.status, 200)
        statement = app.handle("GET", "/statements/fanshi-one/2026-09", {"X-Actor-Id": "fin"})
        self.assertEqual(statement.status, 200)
        self.assertEqual(statement.body["result"]["availability_percent"], "99.1667")
        verify = app.handle("GET", f"/ledgers/{ledger_id}/verify", {"X-Actor-Id": "audit"})
        self.assertTrue(verify.body["result_matches"])


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        from settlement_ledger.acceptance import run
        from pathlib import Path

        result = run(Path(__file__).resolve().parents[1])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["first_ledger"]["state"], "confirmed")
        self.assertEqual(result["first_ledger"]["availability_percent"], "98.8889")
        self.assertEqual(result["first_ledger"]["compensation_cny"], "4320000.00")
        self.assertTrue(result["drift_after_late_data"]["result_matches"])
        self.assertTrue(result["drift_after_late_data"]["inputs_changed"])
        self.assertEqual(result["correction"]["version_no"], 2)
        self.assertEqual(result["correction"]["state"], "confirmed")
        self.assertEqual(result["correction"]["compensation_cny"], "5280000.00")
        self.assertTrue(result["verify"]["first_consistent"])
        self.assertTrue(result["verify"]["second_consistent"])
        self.assertFalse(result["verify"]["second_inputs_changed"])
        self.assertTrue(result["audit"]["valid"])


if __name__ == "__main__":
    unittest.main()
