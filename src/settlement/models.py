"""经营结算履约账页的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
EVENT_KINDS = ("generation", "equipment_failure", "sea_condition", "dispatch_curtailment", "maintenance")
LOSS_KINDS = ("equipment_failure", "sea_condition", "dispatch_curtailment", "maintenance")
EVENT_SOURCES = ("grid", "station")
DEFAULT_PRECEDENCE = ("dispatch_curtailment", "equipment_failure", "maintenance", "sea_condition")


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


def _utc_field(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        return utc_text(parse_utc(text, field))
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


def _kind_tuple(value: object, field: str, allowed: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    result: list[str] = []
    for item in value:
        if item not in allowed:
            raise ValidationFailed(f"{field} 含不受支持的事件类型 {item}")
        if item in result:
            raise ValidationFailed(f"{field} 含重复的事件类型 {item}")
        result.append(item)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CommitmentRules:
    """承诺版本中固定的归集与排除规则，结算计算时不再依赖外部解释。"""

    availability_excluded_kinds: tuple[str, ...]
    maintenance_advance_hours: int
    pre_reported_maintenance_excluded: bool
    compensable_kinds: tuple[str, ...]
    compensation_ratio: Decimal
    adopt_sources: Mapping[str, str]
    kind_precedence: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "CommitmentRules":
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValidationFailed("rules 必须是对象")
        excluded = _kind_tuple(
            raw.get("availability_excluded_kinds", ["sea_condition"]),
            "availability_excluded_kinds",
            LOSS_KINDS,
        )
        advance = raw.get("maintenance_advance_hours", 24)
        if isinstance(advance, bool) or not isinstance(advance, int) or not 0 <= advance <= 720:
            raise ValidationFailed("maintenance_advance_hours 必须是 0 到 720 的整数")
        pre_excluded = raw.get("pre_reported_maintenance_excluded", True)
        if not isinstance(pre_excluded, bool):
            raise ValidationFailed("pre_reported_maintenance_excluded 必须是布尔值")
        compensable = _kind_tuple(
            raw.get("compensable_kinds", ["dispatch_curtailment"]),
            "compensable_kinds",
            LOSS_KINDS,
        )
        ratio = decimal_value(
            raw.get("compensation_ratio", "1"),
            "compensation_ratio",
            minimum=Decimal("0"),
            maximum=Decimal("1"),
        )
        adopt_raw = raw.get("adopt_sources", {})
        if not isinstance(adopt_raw, Mapping):
            raise ValidationFailed("adopt_sources 必须是对象")
        adopt: dict[str, str] = {}
        for kind, source in adopt_raw.items():
            if kind not in EVENT_KINDS:
                raise ValidationFailed(f"adopt_sources 含不受支持的事件类型 {kind}")
            if source not in EVENT_SOURCES:
                raise ValidationFailed("adopt_sources 的来源必须是 grid 或 station")
            adopt[kind] = source
        precedence = _kind_tuple(
            raw.get("kind_precedence", list(DEFAULT_PRECEDENCE)),
            "kind_precedence",
            LOSS_KINDS,
        )
        if set(precedence) != set(LOSS_KINDS):
            raise ValidationFailed("kind_precedence 必须恰好包含全部停机类型")
        return cls(
            availability_excluded_kinds=excluded,
            maintenance_advance_hours=advance,
            pre_reported_maintenance_excluded=pre_excluded,
            compensable_kinds=compensable,
            compensation_ratio=ratio,
            adopt_sources=adopt,
            kind_precedence=precedence,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "availability_excluded_kinds": list(self.availability_excluded_kinds),
            "maintenance_advance_hours": self.maintenance_advance_hours,
            "pre_reported_maintenance_excluded": self.pre_reported_maintenance_excluded,
            "compensable_kinds": list(self.compensable_kinds),
            "compensation_ratio": format(self.compensation_ratio, "f"),
            "adopt_sources": dict(sorted(self.adopt_sources.items())),
            "kind_precedence": list(self.kind_precedence),
        }


@dataclass(frozen=True, slots=True)
class Commitment:
    """一个统计周期固定送出的承诺内容：容量、可利用率、单价和归集规则。"""

    committed_capacity_mw: Decimal
    committed_availability: Decimal
    tariff_cny_per_mwh: Decimal
    rules: CommitmentRules

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Commitment":
        return cls(
            committed_capacity_mw=decimal_value(
                raw.get("committed_capacity_mw"), "committed_capacity_mw", minimum=Decimal("0.001")
            ),
            committed_availability=decimal_value(
                raw.get("committed_availability"),
                "committed_availability",
                minimum=Decimal("0"),
                maximum=Decimal("1"),
            ),
            tariff_cny_per_mwh=decimal_value(
                raw.get("tariff_cny_per_mwh"), "tariff_cny_per_mwh", minimum=Decimal("0")
            ),
            rules=CommitmentRules.from_dict(raw.get("rules")),
        )

    def content_dict(self) -> dict[str, Any]:
        return {
            "committed_capacity_mw": format(self.committed_capacity_mw, "f"),
            "committed_availability": format(self.committed_availability, "f"),
            "tariff_cny_per_mwh": format(self.tariff_cny_per_mwh, "f"),
            "rules": self.rules.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class Period:
    """左闭右开的统计周期，跨周期事件按真实时长在边界处拆开。"""

    period_id: str
    farm_id: str
    starts_at: str
    ends_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Period":
        starts_at = _utc_field(raw.get("starts_at"), "starts_at")
        ends_at = _utc_field(raw.get("ends_at"), "ends_at")
        if parse_utc(ends_at) <= parse_utc(starts_at):
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        return cls(
            period_id=identifier(raw.get("period_id"), "period_id"),
            farm_id=identifier(raw.get("farm_id"), "farm_id"),
            starts_at=starts_at,
            ends_at=ends_at,
        )


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    """按发生区间归集的结算事件；记录后只增不改，口径由承诺版本控制。"""

    event_id: str
    farm_id: str
    kind: str
    starts_at: str
    ends_at: str
    energy_mwh: Decimal
    reported_at: str | None
    source: str
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SettlementEvent":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in EVENT_KINDS:
            raise ValidationFailed("kind 必须是 generation、equipment_failure、sea_condition、dispatch_curtailment 或 maintenance")
        starts_at = _utc_field(raw.get("starts_at"), "starts_at")
        ends_at = _utc_field(raw.get("ends_at"), "ends_at")
        if parse_utc(ends_at) <= parse_utc(starts_at):
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        reported_raw = raw.get("reported_at")
        reported_at = None if reported_raw is None else _utc_field(reported_raw, "reported_at")
        if kind == "maintenance" and reported_at is None:
            raise ValidationFailed("检修事件必须提供 reported_at 报备时间")
        if kind != "maintenance" and reported_at is not None:
            raise ValidationFailed("只有检修事件可以携带 reported_at")
        source = required_text(raw.get("source"), "source", 16)
        if source not in EVENT_SOURCES:
            raise ValidationFailed("source 必须是 grid 或 station")
        note = raw.get("note", "")
        if not isinstance(note, str) or len(note) > 256:
            raise ValidationFailed("note 不能超过 256 个字符")
        return cls(
            event_id=identifier(raw.get("event_id"), "event_id"),
            farm_id=identifier(raw.get("farm_id"), "farm_id"),
            kind=kind,
            starts_at=starts_at,
            ends_at=ends_at,
            energy_mwh=decimal_value(raw.get("energy_mwh"), "energy_mwh", minimum=Decimal("0")),
            reported_at=reported_at,
            source=source,
            note=note.strip(),
        )
