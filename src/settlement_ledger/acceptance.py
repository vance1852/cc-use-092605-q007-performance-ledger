"""贯通承诺固定、事件归集、出账封账、迟到更正与双方确认的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SettlementService


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


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc))
    service = SettlementService(connection, clock)
    for user_id, role in (
        ("settle", "settlement"), ("ops", "production"), ("fin", "finance"), ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    service.create_site("settle", {
        "site_id": "fanshi-one", "name": "帆石一海上风电场",
        "capacity_mw": "300", "timezone": "Asia/Shanghai",
    })
    service.create_period("settle", {
        "period_id": "2026-09", "starts_at": "2026-09-01T00:00:00Z", "ends_at": "2026-10-01T00:00:00Z",
    })
    service.record_commitment("settle", {"commitment_id": "cmt-fanshi", "site_id": "fanshi-one", "rules": RULES})
    service.fix_commitment("settle", "cmt-fanshi")

    events = [
        {"event_id": "gen-0901", "category": "generation", "starts_at": "2026-09-01T00:00:00Z",
         "ends_at": "2026-10-01T00:00:00Z", "energy_mwh": "158000"},
        {"event_id": "fail-0905", "category": "equipment_failure", "starts_at": "2026-09-05T06:00:00Z",
         "ends_at": "2026-09-05T18:00:00Z", "derate_percent": "50"},
        {"event_id": "sea-0912", "category": "sea_condition", "starts_at": "2026-09-12T00:00:00Z",
         "ends_at": "2026-09-13T00:00:00Z", "derate_percent": "100"},
        {"event_id": "cur-0918", "category": "dispatch_curtailment", "starts_at": "2026-09-18T00:00:00Z",
         "ends_at": "2026-09-19T00:00:00Z", "derate_percent": "100"},
        {"event_id": "mnt-0922", "category": "planned_maintenance", "starts_at": "2026-09-22T00:00:00Z",
         "ends_at": "2026-09-22T06:00:00Z", "derate_percent": "100", "reported_at": "2026-09-20T00:00:00Z"},
        {"event_id": "cur-0930", "category": "dispatch_curtailment", "starts_at": "2026-09-30T12:00:00Z",
         "ends_at": "2026-10-01T12:00:00Z", "derate_percent": "100"},
        # 争议时段：场站按检修报备，且报备不足 24 小时
        {"event_id": "dis-0926", "category": "planned_maintenance", "starts_at": "2026-09-26T08:00:00Z",
         "ends_at": "2026-09-26T10:00:00Z", "derate_percent": "100", "reported_at": "2026-09-26T06:00:00Z"},
    ]
    for index, event in enumerate(events, start=1):
        service.record_event("settle", {
            "site_id": "fanshi-one", "idempotency_key": f"evt-{index:03d}", "note": "", **event,
        })

    first = service.issue_ledger("settle", "fanshi-one", "2026-09")
    service.close_ledger("settle", first["ledger_id"])
    service.confirm_compensation("ops", first["ledger_id"])
    service.confirm_compensation("fin", first["ledger_id"])

    # 封账后迟到：电网确认 9 月 26 日争议时段为调度限电，并补登 9 月 27 日限电
    clock.advance(days=2)
    service.revise_event("settle", "dis-0926", {
        "event_id": "cur-0926", "site_id": "fanshi-one", "category": "dispatch_curtailment",
        "starts_at": "2026-09-26T08:00:00Z", "ends_at": "2026-09-26T10:00:00Z",
        "derate_percent": "100", "idempotency_key": "evt-101", "note": "电网调度确认单",
    }, "电网确认该时段为调度限电")
    service.record_event("settle", {
        "event_id": "cur-0927", "site_id": "fanshi-one", "category": "dispatch_curtailment",
        "starts_at": "2026-09-27T00:00:00Z", "ends_at": "2026-09-27T06:00:00Z",
        "derate_percent": "100", "idempotency_key": "evt-102", "note": "迟到的限电通知",
    })
    drift = service.verify_ledger("audit", first["ledger_id"])

    second = service.issue_ledger(
        "settle", "fanshi-one", "2026-09",
        reason="电网确认9月26日08:00-10:00为调度限电，并补登9月27日限电时段",
    )
    service.close_ledger("settle", second["ledger_id"])
    service.confirm_compensation("ops", second["ledger_id"])
    service.confirm_compensation("fin", second["ledger_id"])

    checks = {
        "first": service.verify_ledger("audit", first["ledger_id"]),
        "second": service.verify_ledger("audit", second["ledger_id"]),
    }
    result = {
        "status": "ok",
        "first_ledger": {
            "ledger_id": first["ledger_id"],
            "state": service.ledger(first["ledger_id"])["state"],
            "availability_percent": first["result"]["availability_percent"],
            "compensation_cny": first["result"]["compensation_cny"],
        },
        "drift_after_late_data": {
            "result_matches": drift["result_matches"],
            "inputs_changed": drift["inputs_changed"],
        },
        "correction": {
            "ledger_id": second["ledger_id"],
            "version_no": second["version_no"],
            "state": service.ledger(second["ledger_id"])["state"],
            "availability_percent": second["result"]["availability_percent"],
            "compensation_cny": second["result"]["compensation_cny"],
            "diff": second["diff"],
        },
        "verify": {
            "first_consistent": checks["first"]["result_matches"],
            "second_consistent": checks["second"]["result_matches"],
            "second_inputs_changed": checks["second"]["inputs_changed"],
        },
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行经营结算履约账页离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
