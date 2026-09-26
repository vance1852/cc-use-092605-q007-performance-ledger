"""履约账页的确定性计算：事件区间拆分、采用判定、汇总与版本差异。

本模块不接触数据库，同一组输入永远得到同一组结果，供出账、更正和离线复算共用。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence


ZERO = Decimal("0")
HUNDRED = Decimal("100")
SECONDS_PER_HOUR = Decimal(3600)

GENERATION = "generation"
EQUIPMENT_FAILURE = "equipment_failure"
SEA_CONDITION = "sea_condition"
DISPATCH_CURTAILMENT = "dispatch_curtailment"
PLANNED_MAINTENANCE = "planned_maintenance"
DOWNTIME_CATEGORIES = (EQUIPMENT_FAILURE, SEA_CONDITION, DISPATCH_CURTAILMENT, PLANNED_MAINTENANCE)
EVENT_CATEGORIES = (GENERATION,) + DOWNTIME_CATEGORIES

CATEGORY_LABELS = {
    GENERATION: "实际发电",
    EQUIPMENT_FAILURE: "设备故障",
    SEA_CONDITION: "海况停机",
    DISPATCH_CURTAILMENT: "调度限电",
    PLANNED_MAINTENANCE: "计划检修",
}

DIFF_KEYS = (
    "generated_mwh",
    "affected_mwh",
    "accounted_loss_mwh",
    "excluded_mwh",
    "unavailable_hours",
    "availability_percent",
    "compensation_mwh",
    "compensation_cny",
)


def quantize_energy(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def quantize_hours(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def quantize_percent(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def overlap_window(
    event_start: datetime,
    event_end: datetime,
    period_start: datetime,
    period_end: datetime,
) -> tuple[datetime, datetime] | None:
    """返回事件与统计周期的真实重叠区间（半开），没有重叠时返回 None。"""
    start = max(event_start, period_start)
    end = min(event_end, period_end)
    if end <= start:
        return None
    return start, end


def _whole_seconds(start: datetime, end: datetime) -> int:
    delta = end - start
    return delta.days * 86400 + delta.seconds


def _notice_advance_hours(reported_at: datetime | None, starts_at: datetime) -> Decimal | None:
    if reported_at is None:
        return None
    return Decimal(_whole_seconds(reported_at, starts_at)) / SECONDS_PER_HOUR


def _line(
    *,
    event_id: str,
    category: str,
    portion_starts_at: str,
    portion_ends_at: str,
    overlap_seconds: int,
    derate_percent: str | None,
    energy_mwh: str,
    adopted: bool,
    compensable: bool,
    reason: str,
    compensation_cny: str,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "category": category,
        "portion_starts_at": portion_starts_at,
        "portion_ends_at": portion_ends_at,
        "overlap_seconds": overlap_seconds,
        "derate_percent": derate_percent,
        "energy_mwh": energy_mwh,
        "adopted": adopted,
        "compensable": compensable,
        "reason": reason,
        "compensation_cny": compensation_cny,
    }


def canonical_line(row: Mapping[str, Any]) -> dict[str, Any]:
    """把数据库行或内存行归一成同一结构，保证摘要和复算一致。"""
    return _line(
        event_id=str(row["event_id"]),
        category=str(row["category"]),
        portion_starts_at=str(row["portion_starts_at"]),
        portion_ends_at=str(row["portion_ends_at"]),
        overlap_seconds=int(row["overlap_seconds"]),
        derate_percent=None if row["derate_percent"] is None else str(row["derate_percent"]),
        energy_mwh=str(row["energy_mwh"]),
        adopted=bool(row["adopted"]),
        compensable=bool(row["compensable"]),
        reason=str(row["reason"]),
        compensation_cny=str(row["compensation_cny"]),
    )


def build_event_line(
    *,
    event: Mapping[str, Any],
    period_start: datetime,
    period_end: datetime,
    capacity_mw: Decimal,
    rules: Mapping[str, Any],
    utc_text,
) -> dict[str, Any] | None:
    """把单个事件按周期边界裁剪成账页行，并写明采用或排除的理由。

    跨越周期边界的事件在每个周期内只保留真实重叠区间，电量按各自区间时长计算。
    """

    window = overlap_window(event["starts_at"], event["ends_at"], period_start, period_end)
    if window is None:
        return None
    start, end = window
    portion_seconds = _whole_seconds(start, end)
    category = str(event["category"])
    portion = {
        "event_id": str(event["event_id"]),
        "category": category,
        "portion_starts_at": utc_text(start),
        "portion_ends_at": utc_text(end),
        "overlap_seconds": portion_seconds,
    }
    if category == GENERATION:
        total_seconds = _whole_seconds(event["starts_at"], event["ends_at"])
        declared = Decimal(str(event["energy_mwh"]))
        energy = quantize_energy(declared * Decimal(portion_seconds) / Decimal(total_seconds))
        reason = "实际发电按计量区间纳入统计"
        if portion_seconds < total_seconds:
            reason += "，跨界部分按真实时长折算"
        return _line(
            **portion,
            derate_percent=None,
            energy_mwh=decimal_text(energy),
            adopted=True,
            compensable=False,
            reason=reason,
            compensation_cny=decimal_text(quantize_money(ZERO)),
        )
    derate = Decimal(str(event["derate_percent"]))
    hours = Decimal(portion_seconds) / SECONDS_PER_HOUR
    energy = quantize_energy(capacity_mw * hours * derate / HUNDRED)
    rule = rules["category_rules"][category]
    label = CATEGORY_LABELS[category]
    if category == PLANNED_MAINTENANCE:
        notice_required = int(rules["maintenance_notice_hours"])
        advance = _notice_advance_hours(event.get("reported_at"), event["starts_at"])
        if advance is not None and advance >= Decimal(notice_required):
            adopted = bool(rule["counts_toward_availability"])
            if adopted:
                reason = f"计划检修提前{decimal_text(quantize_hours(advance))}小时报备，按承诺仍计入场站不可用"
            else:
                reason = (
                    f"计划检修提前{decimal_text(quantize_hours(advance))}小时报备，"
                    f"满足{notice_required}小时报备门槛，按承诺排除"
                )
        elif advance is None:
            adopted = True
            reason = "计划检修缺少报备时间，不满足提前报备要求，计入场站不可用"
        else:
            adopted = True
            reason = (
                f"计划检修仅提前{decimal_text(quantize_hours(advance))}小时报备，"
                f"不足{notice_required}小时门槛，计入场站不可用"
            )
    else:
        adopted = bool(rule["counts_toward_availability"])
        reason = f"{label}按承诺计入场站不可用" if adopted else f"{label}按承诺排除，不计入场站不可用"
    compensable = bool(rule["compensable"])
    compensation = ZERO
    if compensable:
        compensation = quantize_money(energy * Decimal(str(rules["compensation_price_cny_per_mwh"])))
        reason += "，影响电量由电网侧按承诺单价补偿"
    return _line(
        **portion,
        derate_percent=decimal_text(derate),
        energy_mwh=decimal_text(energy),
        adopted=adopted,
        compensable=compensable,
        reason=reason,
        compensation_cny=decimal_text(compensation),
    )


def aggregate_lines(
    lines: Sequence[Mapping[str, Any]],
    *,
    period_seconds: int,
    rules: Mapping[str, Any],
) -> dict[str, Any]:
    """把账页行汇总成可利用率、影响电量与补偿金额。"""

    if period_seconds <= 0:
        raise ValueError("统计周期时长必须大于零")
    generated = ZERO
    affected = ZERO
    accounted = ZERO
    excluded = ZERO
    compensation_mwh = ZERO
    compensation_cny = ZERO
    weighted_unavailable_seconds = ZERO
    buckets: dict[str, dict[str, Any]] = {
        category: {
            "portions": 0,
            "hours": ZERO,
            "affected_mwh": ZERO,
            "adopted_mwh": ZERO,
            "excluded_mwh": ZERO,
            "compensation_cny": ZERO,
        }
        for category in EVENT_CATEGORIES
    }
    for raw in lines:
        line = canonical_line(raw)
        bucket = buckets[line["category"]]
        bucket["portions"] += 1
        energy = Decimal(line["energy_mwh"])
        seconds = Decimal(line["overlap_seconds"])
        bucket["hours"] += seconds / SECONDS_PER_HOUR
        if line["category"] == GENERATION:
            generated += energy
            continue
        affected += energy
        bucket["affected_mwh"] += energy
        derate = Decimal(str(line["derate_percent"]))
        if line["adopted"]:
            accounted += energy
            bucket["adopted_mwh"] += energy
            weighted_unavailable_seconds += seconds * derate / HUNDRED
        else:
            excluded += energy
            bucket["excluded_mwh"] += energy
        if line["compensable"]:
            compensation_mwh += energy
            amount = Decimal(line["compensation_cny"])
            compensation_cny += amount
            bucket["compensation_cny"] += amount
    availability = HUNDRED * (Decimal(1) - weighted_unavailable_seconds / Decimal(period_seconds))
    availability = quantize_percent(max(ZERO, min(HUNDRED, availability)))
    committed = Decimal(str(rules["committed_availability_percent"]))
    categories: dict[str, Any] = {}
    for category in EVENT_CATEGORIES:
        bucket = buckets[category]
        categories[category] = {
            "portions": bucket["portions"],
            "hours": decimal_text(quantize_hours(bucket["hours"])),
            "affected_mwh": decimal_text(quantize_energy(bucket["affected_mwh"])),
            "adopted_mwh": decimal_text(quantize_energy(bucket["adopted_mwh"])),
            "excluded_mwh": decimal_text(quantize_energy(bucket["excluded_mwh"])),
            "compensation_cny": decimal_text(quantize_money(bucket["compensation_cny"])),
        }
    return {
        "period_hours": decimal_text(quantize_hours(Decimal(period_seconds) / SECONDS_PER_HOUR)),
        "generated_mwh": decimal_text(quantize_energy(generated)),
        "affected_mwh": decimal_text(quantize_energy(affected)),
        "accounted_loss_mwh": decimal_text(quantize_energy(accounted)),
        "excluded_mwh": decimal_text(quantize_energy(excluded)),
        "unavailable_hours": decimal_text(quantize_hours(weighted_unavailable_seconds / SECONDS_PER_HOUR)),
        "availability_percent": decimal_text(availability),
        "committed_availability_percent": decimal_text(committed),
        "availability_met": availability >= committed,
        "compensation_mwh": decimal_text(quantize_energy(compensation_mwh)),
        "compensation_cny": decimal_text(quantize_money(compensation_cny)),
        "categories": categories,
    }


def _diff_line(line: Mapping[str, Any]) -> dict[str, Any]:
    canonical = canonical_line(line)
    return {
        "event_id": canonical["event_id"],
        "category": canonical["category"],
        "portion_starts_at": canonical["portion_starts_at"],
        "portion_ends_at": canonical["portion_ends_at"],
        "energy_mwh": canonical["energy_mwh"],
        "adopted": canonical["adopted"],
        "compensable": canonical["compensable"],
        "reason": canonical["reason"],
    }


def diff_versions(
    previous_lines: Sequence[Mapping[str, Any]],
    new_lines: Sequence[Mapping[str, Any]],
    previous_result: Mapping[str, Any],
    new_result: Mapping[str, Any],
) -> dict[str, Any]:
    """对比两个账页版本，给出逐事件与逐指标的差异明细。"""

    previous_by_event = {str(line["event_id"]): line for line in previous_lines}
    new_by_event = {str(line["event_id"]): line for line in new_lines}
    added = [_diff_line(new_by_event[key]) for key in sorted(new_by_event.keys() - previous_by_event.keys())]
    removed = [_diff_line(previous_by_event[key]) for key in sorted(previous_by_event.keys() - new_by_event.keys())]
    totals: dict[str, Any] = {}
    for key in DIFF_KEYS:
        before = Decimal(str(previous_result[key]))
        after = Decimal(str(new_result[key]))
        totals[key] = {
            "before": decimal_text(before),
            "after": decimal_text(after),
            "delta": decimal_text(after - before),
        }
    return {"added_lines": added, "removed_lines": removed, "totals": totals}
