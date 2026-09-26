"""贯通承诺版本、事件归集、账页确认、封账和更正的离线验收。

场景：2026 年 8 月统计周期内，电网侧把一段调度限电计入补偿，场站侧把
重叠时段报备为计划检修；承诺版本中的归集规则决定双方口径，账页逐行
保留采用或排除的理由。封账后晚到的限电数据只能通过更正版本进入，
原版本数字保持冻结，两个版本都可以离线复算核对。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState
from .service import SettlementService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SettlementService(connection, FrozenClock(datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("settle", "operator"), ("prod", "production"), ("fin", "finance"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)

    service.create_period("settle", {
        "period_id": "fanshi-2026-08",
        "farm_id": "fanshi-one",
        "starts_at": "2026-08-01T00:00:00+08:00",
        "ends_at": "2026-09-01T00:00:00+08:00",
    })
    service.issue_commitment("settle", "fanshi-2026-08", {
        "committed_capacity_mw": "300",
        "committed_availability": "0.97",
        "tariff_cny_per_mwh": "850",
        "rules": {
            "availability_excluded_kinds": ["sea_condition"],
            "maintenance_advance_hours": 24,
            "pre_reported_maintenance_excluded": True,
            "compensable_kinds": ["dispatch_curtailment"],
            "compensation_ratio": "1",
            "adopt_sources": {"dispatch_curtailment": "grid", "maintenance": "station"},
            "kind_precedence": ["dispatch_curtailment", "equipment_failure", "maintenance", "sea_condition"],
        },
    })

    events = [
        {"event_id": "gen-aug", "kind": "generation", "source": "station",
         "starts_at": "2026-08-01T00:00:00+08:00", "ends_at": "2026-09-01T00:00:00+08:00",
         "energy_mwh": "126000", "note": "月度上网电量"},
        {"event_id": "fail-01", "kind": "equipment_failure", "source": "station",
         "starts_at": "2026-08-05T00:00:00+08:00", "ends_at": "2026-08-05T10:00:00+08:00",
         "energy_mwh": "1500", "note": "海缆故障"},
        {"event_id": "sea-01", "kind": "sea_condition", "source": "station",
         "starts_at": "2026-08-10T00:00:00+08:00", "ends_at": "2026-08-10T20:00:00+08:00",
         "energy_mwh": "2800", "note": "台风外围海况"},
        {"event_id": "curt-01", "kind": "dispatch_curtailment", "source": "grid",
         "starts_at": "2026-08-20T00:00:00+08:00", "ends_at": "2026-08-22T00:00:00+08:00",
         "energy_mwh": "6000", "note": "电网调度限电"},
        {"event_id": "maint-01", "kind": "maintenance", "source": "station",
         "starts_at": "2026-08-21T00:00:00+08:00", "ends_at": "2026-08-23T00:00:00+08:00",
         "energy_mwh": "5500", "reported_at": "2026-08-10T00:00:00+08:00",
         "note": "场站报备的年度检修，与限电时段重叠一天"},
        {"event_id": "fail-02", "kind": "equipment_failure", "source": "station",
         "starts_at": "2026-08-31T12:00:00+08:00", "ends_at": "2026-09-02T12:00:00+08:00",
         "energy_mwh": "2400", "note": "跨月故障，按真实时长拆开"},
    ]
    for event in events:
        service.record_event("prod", {"farm_id": "fanshi-one", **event})

    original = service.compute_ledger("settle", "fanshi-2026-08")
    assert original["result"]["availability"] == "0.905914", original["result"]["availability"]
    assert original["result"]["lost_energy_mwh"]["total"] == "13650.000"
    assert original["result"]["compensation_cny"] == "5100000.00"
    maint_lines = [line for line in original["result"]["lines"] if line["event_id"] == "maint-01"]
    assert any(not line["adopted"] for line in maint_lines), "重叠时段应按承诺规则让位"
    boundary = [line for line in original["result"]["lines"] if line["event_id"] == "fail-02"]
    assert boundary[0]["hours"] == "12.000" and boundary[0]["energy_mwh"] == "600.000"

    service.confirm_ledger("prod", "fanshi-2026-08")
    service.confirm_ledger("fin", "fanshi-2026-08")
    service.close_period("settle", "fanshi-2026-08")

    # 封账后晚到的电网限电数据：可以登记事件，但不能直接改原账页。
    service.record_event("prod", {
        "event_id": "curt-02", "farm_id": "fanshi-one", "kind": "dispatch_curtailment", "source": "grid",
        "starts_at": "2026-08-25T06:00:00+08:00", "ends_at": "2026-08-25T12:00:00+08:00",
        "energy_mwh": "900", "note": "电网延迟送达的限电记录",
    })
    try:
        service.compute_ledger("settle", "fanshi-2026-08")
        raise AssertionError("封账后不应允许直接改数")
    except InvalidState:
        pass

    correction = service.initiate_correction(
        "settle", "fanshi-2026-08", "电网调度限电数据延迟到达", "corr-2026-08-001"
    )
    assert correction["diff"]["events"]["added"] == ["curt-02"]
    assert correction["diff"]["scalars"]["compensation_cny"]["delta"] == "765000.00"
    service.confirm_ledger("prod", "fanshi-2026-08")
    service.confirm_ledger("fin", "fanshi-2026-08")

    frozen = service.get_ledger("audit", "fanshi-2026-08", version_no=1)
    assert frozen["result"]["compensation_cny"] == "5100000.00", "原版本数字必须保持冻结"
    recheck_original = service.recalculate("audit", "fanshi-2026-08", version_no=1)
    recheck_correction = service.recalculate("audit", "fanshi-2026-08", version_no=2)
    assert recheck_original["consistent"] and recheck_correction["consistent"]

    summary = service.period_summary("audit", "fanshi-2026-08")
    result = {
        "status": "ok",
        "original": {
            "availability": original["result"]["availability"],
            "lost_energy_mwh": original["result"]["lost_energy_mwh"]["total"],
            "compensation_cny": original["result"]["compensation_cny"],
        },
        "correction": {
            "availability": correction["result"]["availability"],
            "compensation_cny": correction["result"]["compensation_cny"],
            "compensation_delta_cny": correction["diff"]["scalars"]["compensation_cny"]["delta"],
        },
        "effective_version_no": summary["effective_version_no"],
        "recalculate": {"original": recheck_original["consistent"], "correction": recheck_correction["consistent"]},
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
