"""履约账页的确定性计算：事件拆分、归集、可利用率和补偿。

同一输入（周期、承诺版本内容、事件快照）在任何机器上都得到完全一致的结果，
因此结算查询与离线复算可以对账。所有金额与电量使用十进制定点运算。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from .clock import parse_utc, utc_text
from .models import LOSS_KINDS


ZERO = Decimal("0")
HOURS_QUANT = Decimal("0.001")
ENERGY_QUANT = Decimal("0.001")
RATIO_QUANT = Decimal("0.000001")
MONEY_QUANT = Decimal("0.01")
SECONDS_PER_HOUR = Decimal("3600")


def quantize(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _seconds(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    return (
        Decimal(delta.days) * Decimal(86400)
        + Decimal(delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1000000)
    )


def _subtract(
    free_intervals: Sequence[tuple[datetime, datetime]],
    claim: tuple[datetime, datetime],
) -> tuple[list[tuple[datetime, datetime]], list[tuple[datetime, datetime]]]:
    """从空闲区间中扣除一段占用，返回 (剩余区间, 被占用区间)。"""

    c_start, c_end = claim
    remaining: list[tuple[datetime, datetime]] = []
    taken: list[tuple[datetime, datetime]] = []
    for start, end in free_intervals:
        lo, hi = max(start, c_start), min(end, c_end)
        if lo < hi:
            taken.append((lo, hi))
            if start < lo:
                remaining.append((start, lo))
            if hi < end:
                remaining.append((hi, end))
        else:
            remaining.append((start, end))
    return remaining, taken


def _segment_line(
    event: Mapping[str, Any],
    seg_start: datetime,
    seg_end: datetime,
    event_seconds: Decimal,
) -> dict[str, Any]:
    hours = quantize(_seconds(seg_start, seg_end) / SECONDS_PER_HOUR, HOURS_QUANT)
    energy = quantize(
        Decimal(str(event["energy_mwh"])) * _seconds(seg_start, seg_end) / event_seconds,
        ENERGY_QUANT,
    )
    return {
        "event_id": event["event_id"],
        "kind": event["kind"],
        "source": event["source"],
        "starts_at": utc_text(seg_start),
        "ends_at": utc_text(seg_end),
        "hours": decimal_text(hours),
        "energy_mwh": decimal_text(energy),
    }


def _classify(line: dict[str, Any], event: Mapping[str, Any], rules: Mapping[str, Any]) -> None:
    """为已采用行写入可利用率扣减与补偿口径及理由。"""

    kind = event["kind"]
    if kind == "generation":
        line["availability_deducted"] = False
        line["availability_reason"] = "实际发电不属于停机，不参与可利用率扣减"
        line["compensable"] = False
        line["compensation_reason"] = "实际发电不属于补偿事件"
        return
    pre_reported = False
    if kind == "maintenance" and event.get("reported_at"):
        lead = _seconds(parse_utc(event["reported_at"]), parse_utc(event["starts_at"]))
        pre_reported = lead >= Decimal(int(rules["maintenance_advance_hours"])) * SECONDS_PER_HOUR
    if kind in rules["availability_excluded_kinds"]:
        line["availability_deducted"] = False
        line["availability_reason"] = "承诺规则将该类型列为可利用率扣减的排除项"
    elif kind == "maintenance" and pre_reported and rules["pre_reported_maintenance_excluded"]:
        line["availability_deducted"] = False
        line["availability_reason"] = "检修已按承诺规则提前报备，免于可利用率扣减"
    elif kind == "maintenance" and not pre_reported:
        line["availability_deducted"] = True
        line["availability_reason"] = "检修未满足提前报备时限，计入可利用率扣减"
    else:
        line["availability_deducted"] = True
        line["availability_reason"] = "承诺规则未排除该类型，计入可利用率扣减"
    if kind in rules["compensable_kinds"]:
        line["compensable"] = True
        line["compensation_reason"] = "承诺规则将该类型列为补偿范围"
    else:
        line["compensable"] = False
        line["compensation_reason"] = "承诺规则未将该类型列为补偿范围"


def compute_ledger(
    period: Mapping[str, Any],
    commitment: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """按承诺版本归集事件快照，产出可利用率、影响电量、补偿金额和逐行台账。"""

    period_start = parse_utc(period["starts_at"])
    period_end = parse_utc(period["ends_at"])
    period_seconds = _seconds(period_start, period_end)
    if period_seconds <= ZERO:
        raise ValueError("统计周期必须为正")
    rules = commitment["rules"]
    precedence = {kind: rank for rank, kind in enumerate(rules["kind_precedence"])}
    adopt_sources = rules["adopt_sources"]

    generation_events: list[Mapping[str, Any]] = []
    loss_events: list[Mapping[str, Any]] = []
    for event in events:
        if parse_utc(event["ends_at"]) <= period_start or parse_utc(event["starts_at"]) >= period_end:
            continue
        (generation_events if event["kind"] == "generation" else loss_events).append(event)

    lines: list[dict[str, Any]] = []

    def emit(event: Mapping[str, Any], seg_start: datetime, seg_end: datetime, adopted: bool, reason: str) -> None:
        event_seconds = _seconds(parse_utc(event["starts_at"]), parse_utc(event["ends_at"]))
        line = _segment_line(event, seg_start, seg_end, event_seconds)
        line["adopted"] = adopted
        line["reason"] = reason
        if adopted:
            _classify(line, event, rules)
        else:
            line["availability_deducted"] = False
            line["availability_reason"] = "该行未被采用，不参与可利用率扣减"
            line["compensable"] = False
            line["compensation_reason"] = "该行未被采用，不参与补偿"
        lines.append(line)

    for event in sorted(generation_events, key=lambda item: (item["starts_at"], item["event_id"])):
        expected = adopt_sources.get("generation")
        seg_start = max(parse_utc(event["starts_at"]), period_start)
        seg_end = min(parse_utc(event["ends_at"]), period_end)
        if expected is not None and event["source"] != expected:
            emit(event, seg_start, seg_end, False, f"承诺规则指定 generation 以 {expected} 来源为准，该记录未被采用")
        else:
            emit(event, seg_start, seg_end, True, "实际发电按发生区间归集")

    claimed: list[tuple[datetime, datetime, str, str]] = []
    ordered = sorted(
        loss_events,
        key=lambda item: (precedence[item["kind"]], item["starts_at"], item["event_id"]),
    )
    for event in ordered:
        expected = adopt_sources.get(event["kind"])
        source_ok = expected is None or event["source"] == expected
        free = [(max(parse_utc(event["starts_at"]), period_start), min(parse_utc(event["ends_at"]), period_end))]
        for c_start, c_end, c_kind, c_event_id in claimed:
            next_free: list[tuple[datetime, datetime]] = []
            for seg_start, seg_end in free:
                remaining, taken = _subtract([(seg_start, seg_end)], (c_start, c_end))
                for lo, hi in taken:
                    emit(event, lo, hi, False, f"与更高优先级的 {c_kind} 事件 {c_event_id} 重叠，该时段按承诺规则让位")
                next_free.extend(remaining)
            free = next_free
        for seg_start, seg_end in free:
            if not source_ok:
                emit(event, seg_start, seg_end, False, f"承诺规则指定 {event['kind']} 以 {expected} 来源为准，该记录未被采用")
            else:
                emit(event, seg_start, seg_end, True, "按承诺规则采用")
                claimed.append((seg_start, seg_end, event["kind"], event["event_id"]))

    lines.sort(key=lambda item: (item["starts_at"], item["event_id"], item["ends_at"]))

    def total(field: str, predicate, quantum: Decimal) -> Decimal:
        return quantize(sum((Decimal(line[field]) for line in lines if predicate(line)), ZERO), quantum)

    adopted = lambda line: line["adopted"]
    actual_generation = total("energy_mwh", lambda line: adopted(line) and line["kind"] == "generation", ENERGY_QUANT)
    lost_by_kind = {
        kind: total("energy_mwh", lambda line, k=kind: adopted(line) and line["kind"] == k, ENERGY_QUANT)
        for kind in LOSS_KINDS
    }
    hours_by_kind = {
        kind: total("hours", lambda line, k=kind: adopted(line) and line["kind"] == k, HOURS_QUANT)
        for kind in LOSS_KINDS
    }
    deducted_hours = total("hours", lambda line: adopted(line) and line["availability_deducted"], HOURS_QUANT)
    relieved_hours = total(
        "hours",
        lambda line: adopted(line) and not line["availability_deducted"] and line["kind"] != "generation",
        HOURS_QUANT,
    )
    excluded_hours = total("hours", lambda line: not adopted(line), HOURS_QUANT)
    excluded_energy = total("energy_mwh", lambda line: not adopted(line), ENERGY_QUANT)
    compensable_energy = total("energy_mwh", lambda line: adopted(line) and line["compensable"], ENERGY_QUANT)

    period_hours = period_seconds / SECONDS_PER_HOUR
    available_hours = max(ZERO, period_hours - deducted_hours)
    availability = quantize(available_hours / period_hours, RATIO_QUANT)
    committed_availability = Decimal(str(commitment["committed_availability"]))
    tariff = Decimal(str(commitment["tariff_cny_per_mwh"]))
    ratio = Decimal(str(rules["compensation_ratio"]))
    compensation = quantize(compensable_energy * tariff * ratio, MONEY_QUANT)

    return {
        "period_id": period["period_id"],
        "farm_id": period["farm_id"],
        "starts_at": period["starts_at"],
        "ends_at": period["ends_at"],
        "period_hours": decimal_text(quantize(period_hours, HOURS_QUANT)),
        "committed_capacity_mw": decimal_text(Decimal(str(commitment["committed_capacity_mw"]))),
        "committed_energy_mwh": decimal_text(quantize(Decimal(str(commitment["committed_capacity_mw"])) * period_hours, ENERGY_QUANT)),
        "committed_availability": decimal_text(committed_availability),
        "actual_generation_mwh": decimal_text(actual_generation),
        "lost_energy_mwh": {
            "total": decimal_text(quantize(sum(lost_by_kind.values(), ZERO), ENERGY_QUANT)),
            **{kind: decimal_text(value) for kind, value in lost_by_kind.items()},
        },
        "unavailable_hours": {kind: decimal_text(value) for kind, value in hours_by_kind.items()},
        "deducted_hours": decimal_text(deducted_hours),
        "relieved_hours": decimal_text(relieved_hours),
        "excluded_hours": decimal_text(excluded_hours),
        "excluded_energy_mwh": decimal_text(excluded_energy),
        "availability": decimal_text(availability),
        "availability_gap": decimal_text(quantize(availability - committed_availability, RATIO_QUANT)),
        "compensable_energy_mwh": decimal_text(compensable_energy),
        "compensation_cny": decimal_text(compensation),
        "lines": lines,
    }


def _event_totals(result: Mapping[str, Any]) -> dict[str, dict[str, Decimal]]:
    totals: dict[str, dict[str, Decimal]] = {}
    for line in result["lines"]:
        entry = totals.setdefault(line["event_id"], {"hours": ZERO, "energy_mwh": ZERO})
        if line["adopted"]:
            entry["hours"] += Decimal(line["hours"])
            entry["energy_mwh"] += Decimal(line["energy_mwh"])
    return totals


def diff_results(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """比较两个账页结果，产出更正版本随附的差异明细。"""

    scalars: dict[str, Any] = {}
    for field in (
        "actual_generation_mwh",
        "deducted_hours",
        "relieved_hours",
        "excluded_hours",
        "excluded_energy_mwh",
        "availability",
        "availability_gap",
        "compensable_energy_mwh",
        "compensation_cny",
    ):
        if old[field] != new[field]:
            scalars[field] = {
                "old": old[field],
                "new": new[field],
                "delta": decimal_text(Decimal(new[field]) - Decimal(old[field])),
            }
    if old["lost_energy_mwh"]["total"] != new["lost_energy_mwh"]["total"]:
        scalars["lost_energy_mwh"] = {
            "old": old["lost_energy_mwh"]["total"],
            "new": new["lost_energy_mwh"]["total"],
            "delta": decimal_text(
                Decimal(new["lost_energy_mwh"]["total"]) - Decimal(old["lost_energy_mwh"]["total"])
            ),
        }
    kinds: dict[str, Any] = {}
    for kind in LOSS_KINDS:
        entry: dict[str, Any] = {}
        for group, field in (("lost_energy_mwh", kind), ("unavailable_hours", kind)):
            if old[group][field] != new[group][field]:
                entry[group] = {
                    "old": old[group][field],
                    "new": new[group][field],
                    "delta": decimal_text(Decimal(new[group][field]) - Decimal(old[group][field])),
                }
        if entry:
            kinds[kind] = entry
    old_events = _event_totals(old)
    new_events = _event_totals(new)
    added = sorted(set(new_events) - set(old_events))
    removed = sorted(set(old_events) - set(new_events))
    changed = [
        {
            "event_id": event_id,
            "old_hours": decimal_text(old_events[event_id]["hours"]),
            "new_hours": decimal_text(new_events[event_id]["hours"]),
            "old_energy_mwh": decimal_text(old_events[event_id]["energy_mwh"]),
            "new_energy_mwh": decimal_text(new_events[event_id]["energy_mwh"]),
        }
        for event_id in sorted(set(old_events) & set(new_events))
        if old_events[event_id] != new_events[event_id]
    ]
    return {
        "scalars": scalars,
        "kinds": kinds,
        "events": {"added": added, "removed": removed, "changed": changed},
    }
