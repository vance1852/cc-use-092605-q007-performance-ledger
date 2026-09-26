"""经营结算履约账页的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .calculation import DOWNTIME_CATEGORIES, EVENT_CATEGORIES, GENERATION, PLANNED_MAINTENANCE, decimal_text
from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def instant(value: object, field: str) -> datetime:
    text = required_text(value, field, 40)
    try:
        parsed = parse_utc(text, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    if parsed.microsecond:
        raise ValidationFailed(f"{field} 最多精确到秒")
    return parsed


def optional_instant(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return instant(value, field)


def optional_text(value: object, field: str, maximum: int = 512) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} 必须是字符串")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def commitment_rules(raw: object) -> dict[str, Any]:
    """校验并归一化承诺与排除规则，归一结果会随账页冻结保存。"""

    if not isinstance(raw, Mapping):
        raise ValidationFailed("rules 必须是对象")
    committed = decimal_value(
        raw.get("committed_availability_percent"),
        "committed_availability_percent",
        minimum=Decimal("0"),
        maximum=Decimal("100"),
    )
    price = decimal_value(
        raw.get("compensation_price_cny_per_mwh"),
        "compensation_price_cny_per_mwh",
        minimum=Decimal("0"),
    )
    notice = raw.get("maintenance_notice_hours")
    if isinstance(notice, bool) or not isinstance(notice, int) or notice < 0:
        raise ValidationFailed("maintenance_notice_hours 必须是非负整数")
    category_rules = raw.get("category_rules")
    if not isinstance(category_rules, Mapping):
        raise ValidationFailed("category_rules 必须是对象")
    unknown = set(category_rules) - set(DOWNTIME_CATEGORIES)
    missing = set(DOWNTIME_CATEGORIES) - set(category_rules)
    if unknown or missing:
        raise ValidationFailed("category_rules 必须且只能包含设备故障、海况停机、调度限电、计划检修四类")
    normalized: dict[str, Any] = {}
    for category in DOWNTIME_CATEGORIES:
        rule = category_rules[category]
        if not isinstance(rule, Mapping):
            raise ValidationFailed(f"category_rules.{category} 必须是对象")
        counts = rule.get("counts_toward_availability")
        compensable = rule.get("compensable")
        if not isinstance(counts, bool) or not isinstance(compensable, bool):
            raise ValidationFailed(f"category_rules.{category} 的标记必须是布尔值")
        if counts and compensable:
            raise ValidationFailed(f"category_rules.{category} 不能既计入场站不可用又参与补偿")
        normalized[category] = {"counts_toward_availability": counts, "compensable": compensable}
    return {
        "committed_availability_percent": decimal_text(committed),
        "compensation_price_cny_per_mwh": decimal_text(price),
        "maintenance_notice_hours": notice,
        "category_rules": normalized,
    }


@dataclass(frozen=True, slots=True)
class SiteInput:
    site_id: str
    name: str
    capacity_mw: Decimal
    timezone: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SiteInput":
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            site_id=identifier(raw.get("site_id"), "site_id"),
            name=required_text(raw.get("name"), "name"),
            capacity_mw=decimal_value(raw.get("capacity_mw"), "capacity_mw", minimum=Decimal("0.001")),
            timezone=timezone,
        )


@dataclass(frozen=True, slots=True)
class PeriodInput:
    period_id: str
    starts_at: datetime
    ends_at: datetime

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PeriodInput":
        starts_at = instant(raw.get("starts_at"), "starts_at")
        ends_at = instant(raw.get("ends_at"), "ends_at")
        if ends_at <= starts_at:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        return cls(
            period_id=identifier(raw.get("period_id"), "period_id"),
            starts_at=starts_at,
            ends_at=ends_at,
        )


@dataclass(frozen=True, slots=True)
class CommitmentInput:
    commitment_id: str
    site_id: str
    rules: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitmentInput":
        return cls(
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            site_id=identifier(raw.get("site_id"), "site_id"),
            rules=commitment_rules(raw.get("rules")),
        )


@dataclass(frozen=True, slots=True)
class EventInput:
    event_id: str
    site_id: str
    category: str
    starts_at: datetime
    ends_at: datetime
    derate_percent: Decimal | None
    energy_mwh: Decimal | None
    reported_at: datetime | None
    note: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EventInput":
        category = required_text(raw.get("category"), "category", 32)
        if category not in EVENT_CATEGORIES:
            raise ValidationFailed("category 必须是实际发电、设备故障、海况停机、调度限电或计划检修之一")
        starts_at = instant(raw.get("starts_at"), "starts_at")
        ends_at = instant(raw.get("ends_at"), "ends_at")
        if ends_at <= starts_at:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        derate = raw.get("derate_percent")
        energy = raw.get("energy_mwh")
        derate_percent: Decimal | None = None
        energy_mwh: Decimal | None = None
        if category == GENERATION:
            if derate is not None:
                raise ValidationFailed("实际发电事件不能携带 derate_percent")
            energy_mwh = decimal_value(energy, "energy_mwh", minimum=Decimal("0.001"))
        else:
            if energy is not None:
                raise ValidationFailed("停机类事件不能携带 energy_mwh，影响电量由时长和降出力计算")
            derate_percent = decimal_value(
                derate, "derate_percent", minimum=Decimal("0.001"), maximum=Decimal("100")
            )
        reported_at = optional_instant(raw.get("reported_at"), "reported_at")
        if category == PLANNED_MAINTENANCE and reported_at is None:
            raise ValidationFailed("计划检修必须携带 reported_at 报备时间")
        return cls(
            event_id=identifier(raw.get("event_id"), "event_id"),
            site_id=identifier(raw.get("site_id"), "site_id"),
            category=category,
            starts_at=starts_at,
            ends_at=ends_at,
            derate_percent=derate_percent,
            energy_mwh=energy_mwh,
            reported_at=reported_at,
            note=optional_text(raw.get("note"), "note"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
