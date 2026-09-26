from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from settlement.api import JsonApplication
from settlement.clock import FrozenClock
from settlement.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from settlement.ledger import compute_ledger, diff_results
from settlement.service import SettlementService


RULES = {
    "availability_excluded_kinds": ["sea_condition"],
    "maintenance_advance_hours": 24,
    "pre_reported_maintenance_excluded": True,
    "compensable_kinds": ["dispatch_curtailment"],
    "compensation_ratio": "1",
    "adopt_sources": {"dispatch_curtailment": "grid", "maintenance": "station"},
    "kind_precedence": ["dispatch_curtailment", "equipment_failure", "maintenance", "sea_condition"],
}

COMMITMENT = {
    "committed_capacity_mw": "300",
    "committed_availability": "0.97",
    "tariff_cny_per_mwh": "850",
    "rules": RULES,
}

PERIOD = {
    "period_id": "p-2026-08",
    "farm_id": "farm-1",
    "starts_at": "2026-08-01T00:00:00+08:00",
    "ends_at": "2026-09-01T00:00:00+08:00",
}


def event(event_id, kind, starts_at, ends_at, energy, source="station", **extra):
    return {
        "event_id": event_id,
        "farm_id": "farm-1",
        "kind": kind,
        "source": source,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "energy_mwh": energy,
        **extra,
    }


class SettlementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc))
        self.service = SettlementService(self.connection, self.clock)
        for user_id, role in (("ops", "operator"), ("prod", "production"), ("fin", "finance"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_period("ops", PERIOD)
        self.service.issue_commitment("ops", "p-2026-08", COMMITMENT)

    def tearDown(self) -> None:
        self.connection.close()

    def lines(self, period_id="p-2026-08", version_no=1):
        return self.service.get_ledger("audit", period_id, version_no)["result"]["lines"]

    # --- 契约与权限 ---

    def test_period_rejects_overlapping_period_for_same_farm(self) -> None:
        with self.assertRaises(Conflict):
            self.service.create_period("ops", {
                "period_id": "p-2026-08-b",
                "farm_id": "farm-1",
                "starts_at": "2026-08-15T00:00:00+08:00",
                "ends_at": "2026-09-15T00:00:00+08:00",
            })

    def test_period_requires_end_after_start(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_period("ops", {
                "period_id": "p-bad",
                "farm_id": "farm-2",
                "starts_at": "2026-09-01T00:00:00Z",
                "ends_at": "2026-09-01T00:00:00Z",
            })

    def test_maintenance_requires_reported_at(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.record_event("prod", event(
                "m-1", "maintenance", "2026-08-03T00:00:00+08:00", "2026-08-03T04:00:00+08:00", "100",
            ))
        with self.assertRaises(ValidationFailed):
            self.service.record_event("prod", event(
                "f-1", "equipment_failure", "2026-08-03T00:00:00+08:00", "2026-08-03T04:00:00+08:00", "100",
                reported_at="2026-08-01T00:00:00+08:00",
            ))

    def test_commitment_content_cannot_be_issued_twice(self) -> None:
        with self.assertRaises(Conflict):
            self.service.issue_commitment("ops", "p-2026-08", COMMITMENT)

    def test_commitment_versions_supersede_and_are_immutable(self) -> None:
        second = self.service.issue_commitment("ops", "p-2026-08", {**COMMITMENT, "tariff_cny_per_mwh": "860"})
        self.assertEqual(second["version_no"], 2)
        rows = self.connection.execute(
            "SELECT version_no,state FROM commitment_versions WHERE period_id='p-2026-08' ORDER BY version_no"
        ).fetchall()
        self.assertEqual([row["state"] for row in rows], ["superseded", "issued"])

    def test_roles_are_enforced(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_period("prod", {**PERIOD, "period_id": "p-x", "farm_id": "farm-9"})
        with self.assertRaises(Forbidden):
            self.service.record_event("fin", event(
                "e-x", "equipment_failure", "2026-08-03T00:00:00+08:00", "2026-08-03T04:00:00+08:00", "10",
            ))
        with self.assertRaises(Forbidden):
            self.service.audit_chain("ops")

    # --- 归集计算 ---

    def test_boundary_event_is_split_by_true_duration(self) -> None:
        self.service.record_event("prod", event(
            "fail-x", "equipment_failure",
            "2026-08-31T12:00:00+08:00", "2026-09-02T12:00:00+08:00", "2400",
        ))
        self.service.compute_ledger("ops", "p-2026-08")
        line = [line for line in self.lines() if line["event_id"] == "fail-x"][0]
        self.assertEqual(line["hours"], "12.000")
        self.assertEqual(line["energy_mwh"], "600.000")
        self.assertEqual(line["ends_at"], "2026-08-31T16:00:00Z")

    def test_curtailment_wins_overlapping_maintenance_by_committed_precedence(self) -> None:
        self.service.record_event("prod", event(
            "curt", "dispatch_curtailment",
            "2026-08-20T00:00:00+08:00", "2026-08-22T00:00:00+08:00", "6000", source="grid",
        ))
        self.service.record_event("prod", event(
            "maint", "maintenance",
            "2026-08-21T00:00:00+08:00", "2026-08-23T00:00:00+08:00", "5500",
            reported_at="2026-08-01T00:00:00+08:00",
        ))
        ledger = self.service.compute_ledger("ops", "p-2026-08")
        maint_lines = [line for line in self.lines() if line["event_id"] == "maint"]
        adopted = [line for line in maint_lines if line["adopted"]]
        excluded = [line for line in maint_lines if not line["adopted"]]
        self.assertEqual(len(adopted), 1)
        self.assertEqual(adopted[0]["hours"], "24.000")
        self.assertEqual(adopted[0]["energy_mwh"], "2750.000")
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["hours"], "24.000")
        self.assertIn("curt", excluded[0]["reason"])
        # 提前报备的检修免于扣减，让位部分不计入任何口径
        self.assertFalse(adopted[0]["availability_deducted"])
        self.assertEqual(ledger["result"]["unavailable_hours"]["maintenance"], "24.000")
        self.assertEqual(ledger["result"]["deducted_hours"], "48.000")

    def test_sea_condition_is_relieved_but_counts_as_lost_energy(self) -> None:
        self.service.record_event("prod", event(
            "sea", "sea_condition",
            "2026-08-10T00:00:00+08:00", "2026-08-10T20:00:00+08:00", "2800",
        ))
        ledger = self.service.compute_ledger("ops", "p-2026-08")
        self.assertEqual(ledger["result"]["lost_energy_mwh"]["sea_condition"], "2800.000")
        self.assertEqual(ledger["result"]["deducted_hours"], "0.000")
        self.assertEqual(ledger["result"]["relieved_hours"], "20.000")
        self.assertEqual(ledger["result"]["availability"], "1.000000")

    def test_late_reported_maintenance_is_deducted(self) -> None:
        self.service.record_event("prod", event(
            "maint", "maintenance",
            "2026-08-03T00:00:00+08:00", "2026-08-03T10:00:00+08:00", "500",
            reported_at="2026-08-02T20:00:00+08:00",
        ))
        ledger = self.service.compute_ledger("ops", "p-2026-08")
        line = [line for line in self.lines() if line["event_id"] == "maint"][0]
        self.assertTrue(line["availability_deducted"])
        self.assertIn("未满足提前报备", line["availability_reason"])
        self.assertEqual(ledger["result"]["deducted_hours"], "10.000")

    def test_source_rule_rejects_station_claimed_curtailment(self) -> None:
        self.service.record_event("prod", event(
            "curt", "dispatch_curtailment",
            "2026-08-20T00:00:00+08:00", "2026-08-20T06:00:00+08:00", "900", source="station",
        ))
        ledger = self.service.compute_ledger("ops", "p-2026-08")
        line = [line for line in self.lines() if line["event_id"] == "curt"][0]
        self.assertFalse(line["adopted"])
        self.assertIn("grid", line["reason"])
        self.assertEqual(ledger["result"]["compensable_energy_mwh"], "0.000")
        self.assertEqual(ledger["result"]["compensation_cny"], "0.00")

    def test_compensation_uses_committed_tariff_and_ratio(self) -> None:
        self.service.record_event("prod", event(
            "curt", "dispatch_curtailment",
            "2026-08-20T00:00:00+08:00", "2026-08-20T08:00:00+08:00", "1200", source="grid",
        ))
        ledger = self.service.compute_ledger("ops", "p-2026-08")
        self.assertEqual(ledger["result"]["compensable_energy_mwh"], "1200.000")
        self.assertEqual(ledger["result"]["compensation_cny"], "1020000.00")

    def test_every_line_traces_to_event_with_reason(self) -> None:
        self.service.record_event("prod", event(
            "curt", "dispatch_curtailment",
            "2026-08-20T00:00:00+08:00", "2026-08-22T00:00:00+08:00", "6000", source="grid",
        ))
        self.service.record_event("prod", event(
            "maint", "maintenance",
            "2026-08-21T00:00:00+08:00", "2026-08-23T00:00:00+08:00", "5500",
            reported_at="2026-08-01T00:00:00+08:00",
        ))
        ledger = self.service.compute_ledger("ops", "p-2026-08")
        result = ledger["result"]
        self.assertTrue(result["lines"])
        for line in result["lines"]:
            self.assertTrue(line["event_id"])
            self.assertTrue(line["reason"])
            self.assertTrue(line["availability_reason"])
            self.assertTrue(line["compensation_reason"])
        adopted_energy = sum(Decimal(line["energy_mwh"]) for line in result["lines"] if line["adopted"] and line["kind"] != "generation")
        self.assertEqual(Decimal(result["lost_energy_mwh"]["total"]), adopted_energy)

    # --- 封账前的计算与确认 ---

    def test_compute_replays_when_inputs_unchanged(self) -> None:
        self.service.record_event("prod", event(
            "f-1", "equipment_failure", "2026-08-03T00:00:00+08:00", "2026-08-03T04:00:00+08:00", "100",
        ))
        first = self.service.compute_ledger("ops", "p-2026-08")
        second = self.service.compute_ledger("ops", "p-2026-08")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["ledger_id"], second["ledger_id"])

    def test_recompute_while_open_updates_in_place_and_resets_confirmation(self) -> None:
        self.service.compute_ledger("ops", "p-2026-08")
        self.service.confirm_ledger("prod", "p-2026-08")
        self.service.record_event("prod", event(
            "f-1", "equipment_failure", "2026-08-03T00:00:00+08:00", "2026-08-03T04:00:00+08:00", "100",
        ))
        recomputed = self.service.compute_ledger("ops", "p-2026-08")
        self.assertEqual(recomputed["version_no"], 1)
        self.assertIsNone(recomputed["confirmations"]["production"])
        self.assertEqual(recomputed["result"]["lost_energy_mwh"]["equipment_failure"], "100.000")

    def test_close_requires_dual_confirmation(self) -> None:
        self.service.compute_ledger("ops", "p-2026-08")
        with self.assertRaises(InvalidState):
            self.service.close_period("ops", "p-2026-08")
        self.service.confirm_ledger("prod", "p-2026-08")
        with self.assertRaises(InvalidState):
            self.service.close_period("ops", "p-2026-08")
        self.service.confirm_ledger("fin", "p-2026-08")
        closed = self.service.close_period("ops", "p-2026-08")
        self.assertEqual(closed["state"], "closed")

    def test_confirmation_rules(self) -> None:
        self.service.compute_ledger("ops", "p-2026-08")
        with self.assertRaises(Forbidden):
            self.service.confirm_ledger("ops", "p-2026-08")
        self.service.confirm_ledger("prod", "p-2026-08")
        with self.assertRaises(Conflict):
            self.service.confirm_ledger("prod", "p-2026-08")
        confirmed = self.service.confirm_ledger("fin", "p-2026-08")
        self.assertEqual(confirmed["state"], "confirmed")
        with self.assertRaises(InvalidState):
            self.service.confirm_ledger("fin", "p-2026-08")

    # --- 封账后的更正 ---

    def close_with_curtailment(self) -> None:
        self.service.record_event("prod", event(
            "curt-1", "dispatch_curtailment",
            "2026-08-20T00:00:00+08:00", "2026-08-20T08:00:00+08:00", "1200", source="grid",
        ))
        self.service.compute_ledger("ops", "p-2026-08")
        self.service.confirm_ledger("prod", "p-2026-08")
        self.service.confirm_ledger("fin", "p-2026-08")
        self.service.close_period("ops", "p-2026-08")

    def test_closed_period_freezes_original_and_requires_correction(self) -> None:
        self.close_with_curtailment()
        self.service.record_event("prod", event(
            "curt-2", "dispatch_curtailment",
            "2026-08-25T06:00:00+08:00", "2026-08-25T12:00:00+08:00", "900", source="grid",
        ))
        with self.assertRaises(InvalidState):
            self.service.compute_ledger("ops", "p-2026-08")
        correction = self.service.initiate_correction("ops", "p-2026-08", "限电数据晚到", "corr-1")
        self.assertEqual(correction["version_no"], 2)
        self.assertEqual(correction["kind"], "correction")
        self.assertEqual(correction["correction_reason"], "限电数据晚到")
        self.assertEqual(correction["diff"]["events"]["added"], ["curt-2"])
        self.assertEqual(correction["diff"]["scalars"]["compensation_cny"]["old"], "1020000.00")
        self.assertEqual(correction["diff"]["scalars"]["compensation_cny"]["new"], "1785000.00")
        self.assertEqual(correction["diff"]["scalars"]["compensation_cny"]["delta"], "765000.00")
        original = self.service.get_ledger("audit", "p-2026-08", version_no=1)
        self.assertEqual(original["result"]["compensation_cny"], "1020000.00")

    def test_correction_requires_changed_inputs_and_confirmed_predecessor(self) -> None:
        self.close_with_curtailment()
        with self.assertRaises(Conflict):
            self.service.initiate_correction("ops", "p-2026-08", "没有新数据", "corr-1")
        self.service.record_event("prod", event(
            "curt-2", "dispatch_curtailment",
            "2026-08-25T06:00:00+08:00", "2026-08-25T12:00:00+08:00", "900", source="grid",
        ))
        self.service.initiate_correction("ops", "p-2026-08", "限电数据晚到", "corr-1")
        self.service.record_event("prod", event(
            "curt-3", "dispatch_curtailment",
            "2026-08-26T06:00:00+08:00", "2026-08-26T08:00:00+08:00", "300", source="grid",
        ))
        with self.assertRaises(InvalidState):
            self.service.initiate_correction("ops", "p-2026-08", "上一版未确认", "corr-2")
        self.service.confirm_ledger("prod", "p-2026-08")
        self.service.confirm_ledger("fin", "p-2026-08")
        third = self.service.initiate_correction("ops", "p-2026-08", "第二批限电数据", "corr-2")
        self.assertEqual(third["version_no"], 3)
        self.assertEqual(third["diff"]["events"]["added"], ["curt-3"])

    def test_correction_idempotency(self) -> None:
        self.close_with_curtailment()
        self.service.record_event("prod", event(
            "curt-2", "dispatch_curtailment",
            "2026-08-25T06:00:00+08:00", "2026-08-25T12:00:00+08:00", "900", source="grid",
        ))
        first = self.service.initiate_correction("ops", "p-2026-08", "限电数据晚到", "corr-1")
        replay = self.service.initiate_correction("ops", "p-2026-08", "限电数据晚到", "corr-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["ledger_id"], replay["ledger_id"])
        with self.assertRaises(Conflict):
            self.service.initiate_correction("ops", "p-2026-08", "另一个原因", "corr-1")

    def test_correction_before_close_is_rejected(self) -> None:
        self.service.compute_ledger("ops", "p-2026-08")
        with self.assertRaises(InvalidState):
            self.service.initiate_correction("ops", "p-2026-08", "尚未封账", "corr-1")

    def test_withdrawn_event_enters_next_version_via_correction(self) -> None:
        self.close_with_curtailment()
        self.service.withdraw_event("prod", "curt-1", "电网确认为重复报送")
        correction = self.service.initiate_correction("ops", "p-2026-08", "撤回重复限电", "corr-1")
        self.assertEqual(correction["diff"]["events"]["removed"], ["curt-1"])
        self.assertEqual(correction["result"]["compensation_cny"], "0.00")
        original = self.service.get_ledger("audit", "p-2026-08", version_no=1)
        self.assertEqual(original["result"]["compensation_cny"], "1020000.00")

    def test_effective_version_follows_latest_confirmed(self) -> None:
        self.close_with_curtailment()
        self.service.record_event("prod", event(
            "curt-2", "dispatch_curtailment",
            "2026-08-25T06:00:00+08:00", "2026-08-25T12:00:00+08:00", "900", source="grid",
        ))
        self.service.initiate_correction("ops", "p-2026-08", "限电数据晚到", "corr-1")
        summary = self.service.period_summary("audit", "p-2026-08")
        self.assertEqual(summary["effective_version_no"], 1)
        self.service.confirm_ledger("prod", "p-2026-08")
        self.service.confirm_ledger("fin", "p-2026-08")
        summary = self.service.period_summary("audit", "p-2026-08")
        self.assertEqual(summary["effective_version_no"], 2)

    # --- 复算与审计 ---

    def test_recalculate_is_consistent_and_detects_tampering(self) -> None:
        self.close_with_curtailment()
        check = self.service.recalculate("audit", "p-2026-08", version_no=1)
        self.assertTrue(check["consistent"])
        self.assertEqual(check["compensation_cny"], "1020000.00")
        row = self.connection.execute("SELECT result_json FROM ledger_versions WHERE version_no=1").fetchone()
        tampered = json.loads(row["result_json"])
        tampered["compensation_cny"] = "1.00"
        self.connection.execute(
            "UPDATE ledger_versions SET result_json=? WHERE version_no=1", (json.dumps(tampered),)
        )
        self.assertFalse(self.service.recalculate("audit", "p-2026-08", version_no=1)["consistent"])

    def test_offline_recompute_from_snapshot_matches_stored_result(self) -> None:
        self.close_with_curtailment()
        row = self.connection.execute(
            "SELECT input_json,result_json FROM ledger_versions WHERE version_no=1"
        ).fetchone()
        snapshot = json.loads(row["input_json"])
        recomputed = compute_ledger(snapshot["period"], snapshot["commitment"], snapshot["events"])
        self.assertEqual(recomputed, json.loads(row["result_json"]))

    def test_audit_chain_detects_tampering(self) -> None:
        self.service.compute_ledger("ops", "p-2026-08")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE settlement_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_diff_results_reports_changed_events(self) -> None:
        old = {"lines": [
            {"event_id": "a", "adopted": True, "hours": "2.000", "energy_mwh": "10.000"},
        ]}
        new = {"lines": [
            {"event_id": "a", "adopted": True, "hours": "1.000", "energy_mwh": "5.000"},
            {"event_id": "b", "adopted": True, "hours": "3.000", "energy_mwh": "7.000"},
        ]}
        events = diff_events(old, new)
        self.assertEqual(events["added"], ["b"])
        self.assertEqual(events["changed"][0]["event_id"], "a")
        self.assertEqual(events["changed"][0]["new_energy_mwh"], "5.000")


class SettlementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SettlementService(
            self.connection, FrozenClock(datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc))
        )
        self.app = JsonApplication(self.service)
        self.service.create_user("ops", "结算", "operator")

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_header_is_required(self) -> None:
        response = self.app.handle("POST", "/periods", body=b"{}")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_period_flow_over_http(self) -> None:
        created = self.app.handle("POST", "/periods", {"X-Actor-Id": "ops"}, json.dumps(PERIOD).encode())
        self.assertEqual(created.status, 201)
        commitment = self.app.handle(
            "POST", "/periods/p-2026-08/commitments", {"X-Actor-Id": "ops"}, json.dumps(COMMITMENT).encode()
        )
        self.assertEqual(commitment.status, 201)
        missing = self.app.handle("GET", "/periods/p-2026-08/ledger", {"X-Actor-Id": "ops"})
        self.assertEqual(missing.status, 404)
        computed = self.app.handle("POST", "/periods/p-2026-08/compute", {"X-Actor-Id": "ops"})
        self.assertEqual(computed.status, 200)
        recheck = self.app.handle("GET", "/periods/p-2026-08/recalculate?version=1", {"X-Actor-Id": "ops"})
        self.assertTrue(recheck.body["consistent"])
        unknown = self.app.handle("GET", "/nope", {"X-Actor-Id": "ops"})
        self.assertEqual(unknown.status, 404)


def diff_events(old, new):
    base = {
        "actual_generation_mwh": "0.000", "deducted_hours": "0.000", "relieved_hours": "0.000",
        "excluded_hours": "0.000", "excluded_energy_mwh": "0.000", "availability": "1.000000",
        "availability_gap": "0.000000", "compensable_energy_mwh": "0.000", "compensation_cny": "0.00",
        "lost_energy_mwh": {"total": "0.000", "equipment_failure": "0.000", "sea_condition": "0.000",
                            "dispatch_curtailment": "0.000", "maintenance": "0.000"},
        "unavailable_hours": {"equipment_failure": "0.000", "sea_condition": "0.000",
                              "dispatch_curtailment": "0.000", "maintenance": "0.000"},
    }
    return diff_results({**base, **old}, {**base, **new})["events"]


if __name__ == "__main__":
    unittest.main()
