"""经营结算履约账页的事务用例。

账页（ledger）按“场站 + 统计周期 + 版本”冻结：出账时固定承诺版本、裁出每个事件落在
周期内的真实区间并写明采用或排除理由；封账后数字不可变，迟到数据只能生成带原因和
差异明细的更正版本；补偿由生产和财务分别确认。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .calculation import (
    aggregate_lines,
    build_event_line,
    canonical_json,
    canonical_line,
    decimal_text,
    digest,
    diff_versions,
)
from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import CommitmentInput, EventInput, PeriodInput, SiteInput
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "settlement": {
        "catalog.write", "event.write", "commitment.write", "commitment.fix",
        "ledger.issue", "ledger.close", "report.read",
    },
    "production": {"ledger.confirm", "report.read"},
    "finance": {"ledger.confirm", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

CONFIRM_PARTY = {"production": "production", "finance": "finance"}


class SettlementService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM settlement_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM settlement_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO settlement_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ---- 基础目录 ----

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO settlement_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def create_site(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        site = SiteInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO sites(site_id,name,capacity_mw,timezone,created_at) VALUES(?,?,?,?,?)",
                    (site.site_id, site.name, decimal_text(site.capacity_mw), site.timezone, self._now()),
                )
                self._audit("site", site.site_id, "site.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("场站编号已经存在") from exc
        return self.site(site.site_id)

    def site(self, site_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFound("场站不存在")
        return dict(row)

    def create_period(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        period = PeriodInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO settlement_periods(period_id,starts_at,ends_at,created_at) VALUES(?,?,?,?)",
                    (period.period_id, utc_text(period.starts_at), utc_text(period.ends_at), self._now()),
                )
                self._audit("period", period.period_id, "period.created", actor_id, {
                    "starts_at": utc_text(period.starts_at),
                    "ends_at": utc_text(period.ends_at),
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("统计周期编号已经存在") from exc
        return self.period(period.period_id)

    def period(self, period_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if row is None:
            raise NotFound("统计周期不存在")
        return dict(row)

    # ---- 承诺版本 ----

    def record_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        commitment = CommitmentInput.from_dict(raw)
        self.site(commitment.site_id)
        rules_text = canonical_json(commitment.rules)
        content_sha256 = hashlib.sha256(rules_text.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                last = self.connection.execute(
                    "SELECT max(version_no) version_no FROM commitments WHERE site_id=?",
                    (commitment.site_id,),
                ).fetchone()
                version_no = 1 if last["version_no"] is None else int(last["version_no"]) + 1
                self.connection.execute(
                    "INSERT INTO commitments(commitment_id,site_id,version_no,rules_json,content_sha256,"
                    "state,created_by,created_at) VALUES(?,?,?,?,?,'draft',?,?)",
                    (
                        commitment.commitment_id,
                        commitment.site_id,
                        version_no,
                        rules_text,
                        content_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("commitment", commitment.commitment_id, "commitment.recorded", actor_id, {
                    "site_id": commitment.site_id,
                    "version_no": version_no,
                    "sha256": content_sha256,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("承诺编号冲突或规则内容已经存在") from exc
        return self.commitment(commitment.commitment_id)

    def fix_commitment(self, actor_id: str, commitment_id: str) -> dict[str, Any]:
        self._require(actor_id, "commitment.fix")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE commitments SET state='fixed',fixed_by=?,fixed_at=? "
                "WHERE commitment_id=? AND state='draft'",
                (actor_id, self._now(), commitment_id),
            )
            if cursor.rowcount != 1:
                row = self.connection.execute(
                    "SELECT state FROM commitments WHERE commitment_id=?", (commitment_id,)
                ).fetchone()
                if row is None:
                    raise NotFound("承诺版本不存在")
                raise InvalidState("承诺版本不是草稿，不能固定")
            self._audit("commitment", commitment_id, "commitment.fixed", actor_id, {})
        return self.commitment(commitment_id)

    def commitment(self, commitment_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("承诺版本不存在")
        result = dict(row)
        result["rules"] = json.loads(result.pop("rules_json"))
        return result

    # ---- 事件登记 ----

    @staticmethod
    def _event_payload(event: EventInput) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "site_id": event.site_id,
            "category": event.category,
            "starts_at": utc_text(event.starts_at),
            "ends_at": utc_text(event.ends_at),
            "derate_percent": None if event.derate_percent is None else decimal_text(event.derate_percent),
            "energy_mwh": None if event.energy_mwh is None else decimal_text(event.energy_mwh),
            "reported_at": None if event.reported_at is None else utc_text(event.reported_at),
            "note": event.note,
            "idempotency_key": event.idempotency_key,
        }

    def _insert_event(self, event: EventInput, actor_id: str, supersedes: str | None) -> None:
        payload = self._event_payload(event)
        self.connection.execute(
            "INSERT INTO events(event_id,site_id,category,starts_at,ends_at,derate_percent,energy_mwh,"
            "reported_at,note,idempotency_key,recorded_by,recorded_at,supersedes_event_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                event.site_id,
                event.category,
                payload["starts_at"],
                payload["ends_at"],
                payload["derate_percent"],
                payload["energy_mwh"],
                payload["reported_at"],
                event.note,
                event.idempotency_key,
                actor_id,
                self._now(),
                supersedes,
            ),
        )

    def record_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        event = EventInput.from_dict(raw)
        self.site(event.site_id)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM settlement_idempotency "
            "WHERE scope='event' AND idempotency_key=?",
            (event.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同事件内容")
            return json.loads(stored["response_json"])
        response = {"event_id": event.event_id, "site_id": event.site_id, "state": "recorded"}
        try:
            with transaction(self.connection, immediate=True):
                self._insert_event(event, actor_id, None)
                self.connection.execute(
                    "INSERT INTO settlement_idempotency(scope,idempotency_key,request_sha256,response_json,"
                    "created_at) VALUES('event',?,?,?,?)",
                    (event.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("event", event.event_id, "event.recorded", actor_id, self._event_payload(event))
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件编号或幂等键冲突") from exc
        return response

    def revise_event(
        self, actor_id: str, event_id: str, raw: Mapping[str, Any], reason: str
    ) -> dict[str, Any]:
        """用新事件取代原事件（如检修报备经核对改为调度限电）。原事件保留可追溯。"""

        self._require(actor_id, "event.write")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("更正事件必须给出原因")
        old = self.connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if old is None:
            raise NotFound("原事件不存在")
        if old["state"] != "recorded":
            raise InvalidState("原事件已经被取代，不能再次更正")
        event = EventInput.from_dict(raw)
        if event.site_id != old["site_id"]:
            raise Conflict("更正事件必须属于同一场站")
        response = {
            "event_id": event.event_id,
            "site_id": event.site_id,
            "state": "recorded",
            "supersedes_event_id": event_id,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE events SET state='superseded',superseded_by=?,superseded_at=? WHERE event_id=?",
                    (event.event_id, self._now(), event_id),
                )
                self._insert_event(event, actor_id, event_id)
                self._audit("event", event.event_id, "event.revised", actor_id, {
                    "supersedes_event_id": event_id,
                    "reason": reason.strip(),
                    "new_event": self._event_payload(event),
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件编号或幂等键冲突") from exc
        return response

    def event(self, event_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFound("事件不存在")
        return dict(row)

    # ---- 出账 / 封账 / 更正 ----

    def _fixed_commitment(self, site_id: str, commitment_id: str | None) -> sqlite3.Row:
        if commitment_id is not None:
            row = self.connection.execute(
                "SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)
            ).fetchone()
            if row is None:
                raise NotFound("承诺版本不存在")
            if row["site_id"] != site_id:
                raise Conflict("承诺版本与场站不匹配")
        else:
            row = self.connection.execute(
                "SELECT * FROM commitments WHERE site_id=? AND state='fixed' ORDER BY version_no DESC LIMIT 1",
                (site_id,),
            ).fetchone()
            if row is None:
                raise InvalidState("场站没有已固定的承诺版本，无法出账")
        if row["state"] != "fixed":
            raise InvalidState("只有已固定的承诺版本可以出账")
        return row

    @staticmethod
    def _period_seconds(starts_at: str, ends_at: str) -> int:
        return int((parse_utc(ends_at) - parse_utc(starts_at)).total_seconds())

    def _active_event_rows(self, site_id: str, period_start: str, period_end: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM events WHERE site_id=? AND state='recorded' "
            "AND starts_at<? AND ends_at>? ORDER BY starts_at,event_id",
            (site_id, period_end, period_start),
        ).fetchall()

    @staticmethod
    def _event_mapping(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "category": row["category"],
            "starts_at": parse_utc(row["starts_at"]),
            "ends_at": parse_utc(row["ends_at"]),
            "derate_percent": row["derate_percent"],
            "energy_mwh": row["energy_mwh"],
            "reported_at": None if row["reported_at"] is None else parse_utc(row["reported_at"]),
        }

    def _compute(
        self,
        site: sqlite3.Row,
        period: sqlite3.Row,
        rules: Mapping[str, Any],
        event_rows: Sequence[sqlite3.Row],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        period_start = parse_utc(period["starts_at"])
        period_end = parse_utc(period["ends_at"])
        lines: list[dict[str, Any]] = []
        for row in event_rows:
            line = build_event_line(
                event=self._event_mapping(row),
                period_start=period_start,
                period_end=period_end,
                capacity_mw=Decimal(str(site["capacity_mw"])),
                rules=rules,
                utc_text=utc_text,
            )
            if line is not None:
                lines.append(line)
        lines.sort(key=lambda item: (item["portion_starts_at"], item["event_id"]))
        result = aggregate_lines(
            lines,
            period_seconds=self._period_seconds(period["starts_at"], period["ends_at"]),
            rules=rules,
        )
        return lines, result

    @staticmethod
    def _input_digest(
        site: Mapping[str, Any],
        period: Mapping[str, Any],
        commitment: sqlite3.Row,
        event_rows: Sequence[sqlite3.Row],
    ) -> str:
        events = [
            {
                "event_id": row["event_id"],
                "category": row["category"],
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "derate_percent": row["derate_percent"],
                "energy_mwh": row["energy_mwh"],
                "reported_at": row["reported_at"],
            }
            for row in sorted(event_rows, key=lambda item: item["event_id"])
        ]
        return digest({
            "site": {"site_id": site["site_id"], "capacity_mw": site["capacity_mw"]},
            "period": {
                "period_id": period["period_id"],
                "starts_at": period["starts_at"],
                "ends_at": period["ends_at"],
            },
            "commitment_id": commitment["commitment_id"],
            "commitment_version": commitment["version_no"],
            "rules": json.loads(commitment["rules_json"]),
            "events": events,
        })

    def issue_ledger(
        self,
        actor_id: str,
        site_id: str,
        period_id: str,
        commitment_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "ledger.issue")
        site = self.connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFound("场站不存在")
        period = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if period is None:
            raise NotFound("统计周期不存在")
        latest = self.connection.execute(
            "SELECT * FROM ledgers WHERE site_id=? AND period_id=? ORDER BY version_no DESC LIMIT 1",
            (site_id, period_id),
        ).fetchone()
        if latest is not None and commitment_id is None:
            commitment = self.connection.execute(
                "SELECT * FROM commitments WHERE commitment_id=?", (latest["commitment_id"],)
            ).fetchone()
        else:
            commitment = self._fixed_commitment(site_id, commitment_id)
        event_rows = self._active_event_rows(site_id, period["starts_at"], period["ends_at"])
        rules = json.loads(commitment["rules_json"])
        lines, result = self._compute(site, period, rules, event_rows)
        input_sha256 = self._input_digest(dict(site), dict(period), commitment, event_rows)
        existing = self.connection.execute(
            "SELECT ledger_id FROM ledgers WHERE site_id=? AND period_id=? AND input_sha256=?",
            (site_id, period_id, input_sha256),
        ).fetchone()
        if existing is not None:
            response = self.ledger(int(existing["ledger_id"]))
            response["replayed"] = True
            return response
        correction_reason: str | None = None
        if latest is not None:
            if latest["state"] == "issued":
                raise InvalidState("上一版账页尚未封账，不能出更正版本")
            if not isinstance(reason, str) or not reason.strip():
                raise ValidationFailed("封账后的更正版本必须填写原因")
            correction_reason = reason.strip()
            if commitment_id is not None and commitment_id != latest["commitment_id"]:
                raise Conflict("更正版本必须沿用原账页冻结的承诺版本")
        with transaction(self.connection, immediate=True):
            if latest is None:
                version_no = 1
                supersedes: int | None = None
                diff_json = None
            else:
                version_no = int(latest["version_no"]) + 1
                supersedes = int(latest["ledger_id"])
                previous_rows = self.connection.execute(
                    "SELECT * FROM ledger_lines WHERE ledger_id=? ORDER BY portion_starts_at,event_id",
                    (supersedes,),
                ).fetchall()
                diff_json = canonical_json(diff_versions(
                    [dict(row) for row in previous_rows],
                    lines,
                    json.loads(latest["result_json"]),
                    result,
                ))
            cursor = self.connection.execute(
                "INSERT INTO ledgers(site_id,period_id,version_no,commitment_id,commitment_version,"
                "commitment_snapshot_json,input_sha256,result_json,diff_json,correction_reason,state,"
                "supersedes_ledger_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'issued',?,?,?)",
                (
                    site_id,
                    period_id,
                    version_no,
                    commitment["commitment_id"],
                    commitment["version_no"],
                    commitment["rules_json"],
                    input_sha256,
                    canonical_json(result),
                    diff_json,
                    correction_reason,
                    supersedes,
                    actor_id,
                    self._now(),
                ),
            )
            ledger_id = int(cursor.lastrowid)
            for line in lines:
                self.connection.execute(
                    "INSERT INTO ledger_lines(ledger_id,event_id,category,portion_starts_at,portion_ends_at,"
                    "overlap_seconds,derate_percent,energy_mwh,adopted,compensable,reason,compensation_cny) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ledger_id,
                        line["event_id"],
                        line["category"],
                        line["portion_starts_at"],
                        line["portion_ends_at"],
                        line["overlap_seconds"],
                        line["derate_percent"],
                        line["energy_mwh"],
                        1 if line["adopted"] else 0,
                        1 if line["compensable"] else 0,
                        line["reason"],
                        line["compensation_cny"],
                    ),
                )
            self._audit("ledger", str(ledger_id), "ledger.issued", actor_id, {
                "site_id": site_id,
                "period_id": period_id,
                "version_no": version_no,
                "commitment_id": commitment["commitment_id"],
                "input_sha256": input_sha256,
                "correction_reason": correction_reason,
            })
        response = self.ledger(ledger_id)
        response["replayed"] = False
        return response

    def close_ledger(self, actor_id: str, ledger_id: int) -> dict[str, Any]:
        self._require(actor_id, "ledger.close")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state FROM ledgers WHERE ledger_id=?", (ledger_id,)
            ).fetchone()
            if row is None:
                raise NotFound("账页不存在")
            if row["state"] != "issued":
                raise InvalidState("只有已出账未封账的版本可以封账")
            self.connection.execute(
                "UPDATE ledgers SET state='closed',closed_by=?,closed_at=? WHERE ledger_id=? AND state='issued'",
                (actor_id, self._now(), ledger_id),
            )
            self._audit("ledger", str(ledger_id), "ledger.closed", actor_id, {})
        return self.ledger(ledger_id)

    def confirm_compensation(self, actor_id: str, ledger_id: int) -> dict[str, Any]:
        user = self._require(actor_id, "ledger.confirm")
        party = CONFIRM_PARTY.get(user["role"])
        if party is None:  # pragma: no cover - 权限表已限定
            raise Forbidden("只有生产或财务角色可以确认补偿")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state FROM ledgers WHERE ledger_id=?", (ledger_id,)
            ).fetchone()
            if row is None:
                raise NotFound("账页不存在")
            if row["state"] == "issued":
                raise InvalidState("账页封账后才能确认补偿")
            try:
                self.connection.execute(
                    "INSERT INTO ledger_confirmations(ledger_id,party,confirmed_by,confirmed_at) "
                    "VALUES(?,?,?,?)",
                    (ledger_id, party, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"{party} 已经确认过该账页") from exc
            parties = self.connection.execute(
                "SELECT count(*) AS n FROM ledger_confirmations WHERE ledger_id=?", (ledger_id,)
            ).fetchone()
            if int(parties["n"]) == 2:
                self.connection.execute(
                    "UPDATE ledgers SET state='confirmed' WHERE ledger_id=? AND state='closed'",
                    (ledger_id,),
                )
            self._audit("ledger", str(ledger_id), "ledger.confirmed", actor_id, {"party": party})
        return self.ledger(ledger_id)

    # ---- 查询与复核 ----

    def _confirmations(self, ledger_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT party,confirmed_by,confirmed_at FROM ledger_confirmations "
            "WHERE ledger_id=? ORDER BY party",
            (ledger_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def ledger(self, ledger_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM ledgers WHERE ledger_id=?", (ledger_id,)
        ).fetchone()
        if row is None:
            raise NotFound("账页不存在")
        result = dict(row)
        result["commitment_snapshot"] = json.loads(result.pop("commitment_snapshot_json"))
        result["result"] = json.loads(result.pop("result_json"))
        result["diff"] = None if row["diff_json"] is None else json.loads(row["diff_json"])
        result["confirmations"] = self._confirmations(ledger_id)
        return result

    def ledger_version(self, site_id: str, period_id: str, version_no: int | None = None) -> dict[str, Any]:
        if version_no is None:
            row = self.connection.execute(
                "SELECT ledger_id FROM ledgers WHERE site_id=? AND period_id=? ORDER BY version_no DESC LIMIT 1",
                (site_id, period_id),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT ledger_id FROM ledgers WHERE site_id=? AND period_id=? AND version_no=?",
                (site_id, period_id, version_no),
            ).fetchone()
        if row is None:
            raise NotFound("该周期没有对应账页版本")
        return self.ledger(int(row["ledger_id"]))

    def list_versions(self, site_id: str, period_id: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT ledger_id,version_no,state,commitment_id,commitment_version,correction_reason,"
            "supersedes_ledger_id,created_at,closed_at "
            "FROM ledgers WHERE site_id=? AND period_id=? ORDER BY version_no",
            (site_id, period_id),
        ).fetchall()
        return {"site_id": site_id, "period_id": period_id, "versions": [dict(row) for row in rows]}

    def ledger_lines(
        self,
        actor_id: str,
        ledger_id: int,
        category: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self.ledger(ledger_id)
        query = "SELECT * FROM ledger_lines WHERE ledger_id=?"
        params: list[Any] = [ledger_id]
        if category is not None:
            query += " AND category=?"
            params.append(category)
        if event_id is not None:
            query += " AND event_id=?"
            params.append(event_id)
        query += " ORDER BY portion_starts_at,event_id"
        rows = self.connection.execute(query, params).fetchall()
        return {
            "ledger_id": ledger_id,
            "lines": [
                {
                    **{key: value for key, value in dict(row).items() if key != "ledger_id"},
                    "adopted": bool(row["adopted"]),
                    "compensable": bool(row["compensable"]),
                }
                for row in rows
            ],
        }

    def verify_ledger(self, actor_id: str, ledger_id: int) -> dict[str, Any]:
        """按冻结快照离线复算，并核对迟到数据是否与原账页发生漂移。

        - result_matches：用账页冻结的逐事件行重算汇总，必须与封账数字逐分一致；
        - inputs_changed：用当前事件表按同一承诺重算，标识封账后到达的差异；
          原数字保持不变，差异只能通过更正版本入账。
        """

        self._require(actor_id, "report.read")
        ledger = self.ledger(ledger_id)
        site = self.connection.execute("SELECT * FROM sites WHERE site_id=?", (ledger["site_id"],)).fetchone()
        period = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period_id=?", (ledger["period_id"],)
        ).fetchone()
        rules = ledger["commitment_snapshot"]
        stored_rows = self.connection.execute(
            "SELECT * FROM ledger_lines WHERE ledger_id=? ORDER BY portion_starts_at,event_id",
            (ledger_id,),
        ).fetchall()
        stored_lines = [canonical_line(dict(row)) for row in stored_rows]
        recomputed = aggregate_lines(
            stored_lines,
            period_seconds=self._period_seconds(period["starts_at"], period["ends_at"]),
            rules=rules,
        )
        current_rows = self._active_event_rows(site["site_id"], period["starts_at"], period["ends_at"])
        current_lines, _ = self._compute(site, period, rules, current_rows)
        stored_by_event = {line["event_id"]: line for line in stored_lines}
        current_by_event = {line["event_id"]: line for line in current_lines}
        arrived = [
            {
                "event_id": event_id,
                "category": current_by_event[event_id]["category"],
                "energy_mwh": current_by_event[event_id]["energy_mwh"],
                "reason": current_by_event[event_id]["reason"],
            }
            for event_id in sorted(current_by_event.keys() - stored_by_event.keys())
        ]
        missing = sorted(stored_by_event.keys() - current_by_event.keys())
        changed = sorted(
            event_id
            for event_id in stored_by_event.keys() & current_by_event.keys()
            if canonical_json(stored_by_event[event_id]) != canonical_json(current_by_event[event_id])
        )
        return {
            "ledger_id": ledger_id,
            "version_no": ledger["version_no"],
            "state": ledger["state"],
            "input_sha256": ledger["input_sha256"],
            "stored_result": ledger["result"],
            "recomputed_result": recomputed,
            "result_matches": canonical_json(recomputed) == canonical_json(ledger["result"]),
            "inputs_changed": bool(arrived or missing or changed),
            "late_events": {
                "arrived": arrived,
                "withdrawn_or_revised": missing,
                "changed": changed,
            },
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM settlement_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
