"""经营结算履约账页的事务用例。

账页生命周期：创建周期 → 送出承诺版本 → 归集事件 → 计算账页 → 生产与财务
分别确认 → 封账。封账后原版本冻结，晚到的数据只能通过带原因和差异明细的
更正版本进入；每个版本保存完整输入快照，查询与离线复算结果一致。
"""

from __future__ import annotations

import functools
import hashlib
import json
import sqlite3
import threading
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .ledger import canonical_json, compute_ledger, diff_results, digest
from .models import Commitment, Period, SettlementEvent, identifier, required_text
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "operator": {
        "period.write", "commitment.issue", "event.write",
        "ledger.compute", "ledger.close", "correction.initiate", "ledger.read",
    },
    "production": {"event.write", "ledger.confirm", "ledger.read"},
    "finance": {"ledger.confirm", "ledger.read"},
    "auditor": {"ledger.read", "audit.read"},
}

CONFIRM_SIDES = {"production": "production", "finance": "finance"}


def _serialized(method):
    """共享 SQLite 连接在 HTTP 工作线程间使用时必须串行化。"""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class SettlementService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
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

    @_serialized
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

    def _period(self, period_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM settlement_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if row is None:
            raise NotFound("统计周期不存在")
        return row

    @_serialized
    def create_period(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "period.write")
        period = Period.from_dict(raw)
        overlap = self.connection.execute(
            "SELECT period_id FROM settlement_periods WHERE farm_id=? AND starts_at<? AND ends_at>?",
            (period.farm_id, period.ends_at, period.starts_at),
        ).fetchone()
        if overlap is not None:
            raise Conflict(f"场站已存在重叠的统计周期 {overlap['period_id']}")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO settlement_periods(period_id,farm_id,starts_at,ends_at,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (period.period_id, period.farm_id, period.starts_at, period.ends_at, actor_id, self._now()),
                )
                self._audit("period", period.period_id, "period.created", actor_id, dict(raw))
        except sqlite3.IntegrityError as exc:
            raise Conflict("统计周期编号已经存在") from exc
        return {
            "period_id": period.period_id,
            "farm_id": period.farm_id,
            "starts_at": period.starts_at,
            "ends_at": period.ends_at,
            "state": "open",
        }

    @_serialized
    def issue_commitment(self, actor_id: str, period_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "commitment.issue")
        period = self._period(period_id)
        if period["state"] != "open":
            raise InvalidState("周期已封账，不能再送出承诺版本")
        commitment = Commitment.from_dict(raw)
        content = commitment.content_dict()
        content_sha256 = digest(content)
        latest = self.connection.execute(
            "SELECT version_no,content_sha256 FROM commitment_versions WHERE period_id=? "
            "ORDER BY version_no DESC LIMIT 1",
            (period_id,),
        ).fetchone()
        if latest is not None and latest["content_sha256"] == content_sha256:
            raise Conflict("相同内容的承诺版本已经送出")
        version_no = 1 if latest is None else int(latest["version_no"]) + 1
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE commitment_versions SET state='superseded' WHERE period_id=? AND state='issued'",
                (period_id,),
            )
            cursor = self.connection.execute(
                "INSERT INTO commitment_versions(period_id,version_no,committed_capacity_mw,"
                "committed_availability,tariff_cny_per_mwh,rules_json,content_sha256,issued_by,issued_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    period_id,
                    version_no,
                    content["committed_capacity_mw"],
                    content["committed_availability"],
                    content["tariff_cny_per_mwh"],
                    canonical_json(content["rules"]),
                    content_sha256,
                    actor_id,
                    self._now(),
                ),
            )
            commitment_id = int(cursor.lastrowid)
            self._audit(
                "period", period_id, "commitment.issued", actor_id,
                {"commitment_id": commitment_id, "version_no": version_no, "content_sha256": content_sha256},
            )
        return {
            "commitment_id": commitment_id,
            "period_id": period_id,
            "version_no": version_no,
            "state": "issued",
            "content_sha256": content_sha256,
        }

    @_serialized
    def record_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        event = SettlementEvent.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO settlement_events(event_id,farm_id,kind,starts_at,ends_at,energy_mwh,"
                    "reported_at,source,note,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.farm_id,
                        event.kind,
                        event.starts_at,
                        event.ends_at,
                        format(event.energy_mwh, "f"),
                        event.reported_at,
                        event.source,
                        event.note,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "event", event.event_id, "event.recorded", actor_id,
                    {"farm_id": event.farm_id, "kind": event.kind, "starts_at": event.starts_at, "ends_at": event.ends_at},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件编号已经存在") from exc
        return self.get_event(actor_id, event.event_id)

    @_serialized
    def withdraw_event(self, actor_id: str, event_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        reason = required_text(reason, "reason")
        row = self.connection.execute(
            "SELECT * FROM settlement_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound("事件不存在")
        if row["state"] != "recorded":
            raise InvalidState("事件已被撤回")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE settlement_events SET state='withdrawn',withdrawn_by=?,withdrawn_at=?,"
                "withdraw_reason=? WHERE event_id=?",
                (actor_id, self._now(), reason, event_id),
            )
            self._audit("event", event_id, "event.withdrawn", actor_id, {"reason": reason})
        return self.get_event(actor_id, event_id)

    @_serialized
    def get_event(self, actor_id: str, event_id: str) -> dict[str, Any]:
        self._require(actor_id, "ledger.read")
        row = self.connection.execute(
            "SELECT * FROM settlement_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound("事件不存在")
        return dict(row)

    @_serialized
    def period_events(self, actor_id: str, period_id: str) -> dict[str, Any]:
        self._require(actor_id, "ledger.read")
        period = self._period(period_id)
        rows = self.connection.execute(
            "SELECT * FROM settlement_events WHERE farm_id=? AND starts_at<? AND ends_at>? "
            "ORDER BY starts_at,event_id",
            (period["farm_id"], period["ends_at"], period["starts_at"]),
        ).fetchall()
        return {"period_id": period_id, "events": [dict(row) for row in rows]}

    def _snapshot(self, period: sqlite3.Row) -> tuple[sqlite3.Row, dict[str, Any]]:
        commitment = self.connection.execute(
            "SELECT * FROM commitment_versions WHERE period_id=? AND state='issued'",
            (period["period_id"],),
        ).fetchone()
        if commitment is None:
            raise InvalidState("周期没有已送出的承诺版本")
        events = self.connection.execute(
            "SELECT * FROM settlement_events WHERE farm_id=? AND state='recorded' "
            "AND starts_at<? AND ends_at>? ORDER BY starts_at,event_id",
            (period["farm_id"], period["ends_at"], period["starts_at"]),
        ).fetchall()
        snapshot = {
            "period": {
                "period_id": period["period_id"],
                "farm_id": period["farm_id"],
                "starts_at": period["starts_at"],
                "ends_at": period["ends_at"],
            },
            "commitment": {
                "committed_capacity_mw": commitment["committed_capacity_mw"],
                "committed_availability": commitment["committed_availability"],
                "tariff_cny_per_mwh": commitment["tariff_cny_per_mwh"],
                "rules": json.loads(commitment["rules_json"]),
            },
            "events": [
                {
                    "event_id": row["event_id"],
                    "kind": row["kind"],
                    "source": row["source"],
                    "starts_at": row["starts_at"],
                    "ends_at": row["ends_at"],
                    "energy_mwh": row["energy_mwh"],
                    "reported_at": row["reported_at"],
                    "note": row["note"],
                }
                for row in events
            ],
        }
        return commitment, snapshot

    def _ledger_row(self, period_id: str, version_no: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM ledger_versions WHERE period_id=? AND version_no=?",
            (period_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFound("账页版本不存在")
        return row

    def _latest_ledger(self, period_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ledger_versions WHERE period_id=? ORDER BY version_no DESC LIMIT 1",
            (period_id,),
        ).fetchone()

    def _ledger_payload(self, row: sqlite3.Row, replayed: bool = False) -> dict[str, Any]:
        return {
            "ledger_id": row["ledger_id"],
            "period_id": row["period_id"],
            "version_no": row["version_no"],
            "kind": row["kind"],
            "state": row["state"],
            "commitment_id": row["commitment_id"],
            "input_sha256": row["input_sha256"],
            "correction_reason": row["correction_reason"],
            "diff": None if row["diff_json"] is None else json.loads(row["diff_json"]),
            "confirmations": {
                "production": None if row["production_confirmed_by"] is None else {
                    "by": row["production_confirmed_by"], "at": row["production_confirmed_at"],
                },
                "finance": None if row["finance_confirmed_by"] is None else {
                    "by": row["finance_confirmed_by"], "at": row["finance_confirmed_at"],
                },
            },
            "result": json.loads(row["result_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "replayed": replayed,
        }

    @_serialized
    def compute_ledger(self, actor_id: str, period_id: str) -> dict[str, Any]:
        """封账前计算账页：输入未变时幂等回放，输入变化时更新原版本并清空确认。"""

        self._require(actor_id, "ledger.compute")
        period = self._period(period_id)
        if period["state"] != "open":
            raise InvalidState("周期已封账，原版本已冻结，只能发起更正版本")
        commitment, snapshot = self._snapshot(period)
        input_sha256 = digest(snapshot)
        existing = self.connection.execute(
            "SELECT * FROM ledger_versions WHERE period_id=? AND version_no=1", (period_id,)
        ).fetchone()
        if existing is not None and existing["input_sha256"] == input_sha256:
            return self._ledger_payload(existing, replayed=True)
        result = compute_ledger(snapshot["period"], snapshot["commitment"], snapshot["events"])
        with transaction(self.connection, immediate=True):
            if existing is None:
                cursor = self.connection.execute(
                    "INSERT INTO ledger_versions(period_id,version_no,kind,commitment_id,input_json,"
                    "input_sha256,result_json,created_by,created_at) VALUES(?,1,'original',?,?,?,?,?,?)",
                    (
                        period_id,
                        commitment["commitment_id"],
                        canonical_json(snapshot),
                        input_sha256,
                        canonical_json(result),
                        actor_id,
                        self._now(),
                    ),
                )
                ledger_id = int(cursor.lastrowid)
                self._audit("period", period_id, "ledger.computed", actor_id, {"ledger_id": ledger_id, "version_no": 1})
            else:
                ledger_id = existing["ledger_id"]
                self.connection.execute(
                    "UPDATE ledger_versions SET commitment_id=?,input_json=?,input_sha256=?,result_json=?,"
                    "state='draft',production_confirmed_by=NULL,production_confirmed_at=NULL,"
                    "finance_confirmed_by=NULL,finance_confirmed_at=NULL,created_by=?,created_at=? "
                    "WHERE ledger_id=?",
                    (
                        commitment["commitment_id"],
                        canonical_json(snapshot),
                        input_sha256,
                        canonical_json(result),
                        actor_id,
                        self._now(),
                        ledger_id,
                    ),
                )
                self._audit(
                    "period", period_id, "ledger.recomputed", actor_id,
                    {"ledger_id": ledger_id, "previous_input_sha256": existing["input_sha256"]},
                )
        return self._ledger_payload(self._ledger_row(period_id, 1))

    @_serialized
    def confirm_ledger(self, actor_id: str, period_id: str, version_no: int | None = None) -> dict[str, Any]:
        """生产或财务一方确认补偿结果；双方均确认后版本生效。"""

        user = self._require(actor_id, "ledger.confirm")
        side = CONFIRM_SIDES.get(user["role"])
        if side is None:
            raise Forbidden("只有生产和财务角色可以确认补偿结果")
        self._period(period_id)
        latest = self._latest_ledger(period_id)
        if latest is None:
            raise NotFound("账页版本不存在")
        if version_no is not None and version_no != latest["version_no"]:
            raise InvalidState("只能确认最新账页版本")
        row = latest
        if row["state"] == "confirmed":
            raise InvalidState("该版本已完成生产和财务双方确认")
        if row[f"{side}_confirmed_by"] is not None:
            raise Conflict("该方已确认过当前版本")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                f"UPDATE ledger_versions SET {side}_confirmed_by=?,{side}_confirmed_at=? WHERE ledger_id=?",
                (actor_id, now, row["ledger_id"]),
            )
            other = "finance" if side == "production" else "production"
            if row[f"{other}_confirmed_by"] is not None:
                self.connection.execute(
                    "UPDATE ledger_versions SET state='confirmed' WHERE ledger_id=?", (row["ledger_id"],)
                )
            self._audit(
                "period", period_id, "ledger.confirmed", actor_id,
                {"version_no": row["version_no"], "side": side},
            )
        return self._ledger_payload(self._ledger_row(period_id, row["version_no"]))

    @_serialized
    def close_period(self, actor_id: str, period_id: str) -> dict[str, Any]:
        self._require(actor_id, "ledger.close")
        period = self._period(period_id)
        if period["state"] != "open":
            raise InvalidState("周期已经封账")
        latest = self._latest_ledger(period_id)
        if latest is None or latest["state"] != "confirmed":
            raise InvalidState("账页未经生产和财务双方确认，不能封账")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE settlement_periods SET state='closed',closed_by=?,closed_at=? WHERE period_id=?",
                (actor_id, self._now(), period_id),
            )
            self._audit("period", period_id, "period.closed", actor_id, {"version_no": latest["version_no"]})
        return {"period_id": period_id, "state": "closed", "closed_version_no": latest["version_no"]}

    @_serialized
    def initiate_correction(
        self,
        actor_id: str,
        period_id: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """封账后对晚到数据发起更正版本，携带原因和与上一版本的差异明细。"""

        self._require(actor_id, "correction.initiate")
        reason = required_text(reason, "reason")
        idempotency_key = identifier(idempotency_key, "idempotency_key")
        stored = self.connection.execute(
            "SELECT * FROM ledger_versions WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if stored is not None:
            if stored["period_id"] != period_id or stored["correction_reason"] != reason:
                raise Conflict("幂等键对应不同更正内容")
            return self._ledger_payload(stored, replayed=True)
        period = self._period(period_id)
        if period["state"] != "closed":
            raise InvalidState("周期尚未封账，直接重新计算账页即可")
        latest = self._latest_ledger(period_id)
        if latest is None or latest["state"] != "confirmed":
            raise InvalidState("上一账页版本尚未经生产和财务双方确认")
        commitment, snapshot = self._snapshot(period)
        input_sha256 = digest(snapshot)
        if input_sha256 == latest["input_sha256"]:
            raise Conflict("归集数据没有变化，无需发起更正")
        result = compute_ledger(snapshot["period"], snapshot["commitment"], snapshot["events"])
        diff = diff_results(json.loads(latest["result_json"]), result)
        version_no = int(latest["version_no"]) + 1
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO ledger_versions(period_id,version_no,kind,commitment_id,input_json,"
                    "input_sha256,result_json,correction_reason,diff_json,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        period_id,
                        version_no,
                        "correction",
                        commitment["commitment_id"],
                        canonical_json(snapshot),
                        input_sha256,
                        canonical_json(result),
                        reason,
                        canonical_json(diff),
                        idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                ledger_id = int(cursor.lastrowid)
                self._audit(
                    "period", period_id, "correction.initiated", actor_id,
                    {"ledger_id": ledger_id, "version_no": version_no, "reason": reason},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("更正版本冲突") from exc
        return self._ledger_payload(self._ledger_row(period_id, version_no))

    @_serialized
    def get_ledger(self, actor_id: str, period_id: str, version_no: int | None = None) -> dict[str, Any]:
        self._require(actor_id, "ledger.read")
        self._period(period_id)
        if version_no is None:
            row = self._latest_ledger(period_id)
            if row is None:
                raise NotFound("账页版本不存在")
        else:
            row = self._ledger_row(period_id, version_no)
        return self._ledger_payload(row)

    @_serialized
    def period_summary(self, actor_id: str, period_id: str) -> dict[str, Any]:
        self._require(actor_id, "ledger.read")
        period = self._period(period_id)
        versions = self.connection.execute(
            "SELECT * FROM ledger_versions WHERE period_id=? ORDER BY version_no", (period_id,)
        ).fetchall()
        effective = None
        for row in versions:
            if row["state"] == "confirmed":
                effective = row
        commitment = self.connection.execute(
            "SELECT version_no,content_sha256,state FROM commitment_versions WHERE period_id=? "
            "ORDER BY version_no DESC LIMIT 1",
            (period_id,),
        ).fetchone()
        return {
            "period_id": period_id,
            "farm_id": period["farm_id"],
            "starts_at": period["starts_at"],
            "ends_at": period["ends_at"],
            "state": period["state"],
            "commitment": None if commitment is None else dict(commitment),
            "effective_version_no": None if effective is None else effective["version_no"],
            "versions": [
                {
                    "version_no": row["version_no"],
                    "kind": row["kind"],
                    "state": row["state"],
                    "input_sha256": row["input_sha256"],
                    "correction_reason": row["correction_reason"],
                    "availability": json.loads(row["result_json"])["availability"],
                    "lost_energy_mwh": json.loads(row["result_json"])["lost_energy_mwh"]["total"],
                    "compensation_cny": json.loads(row["result_json"])["compensation_cny"],
                }
                for row in versions
            ],
        }

    @_serialized
    def recalculate(self, actor_id: str, period_id: str, version_no: int | None = None) -> dict[str, Any]:
        """用版本保存的输入快照离线复算，校验与保存结果一致。"""

        self._require(actor_id, "ledger.read")
        self._period(period_id)
        if version_no is None:
            row = self._latest_ledger(period_id)
            if row is None:
                raise NotFound("账页版本不存在")
        else:
            row = self._ledger_row(period_id, version_no)
        snapshot = json.loads(row["input_json"])
        recomputed = compute_ledger(snapshot["period"], snapshot["commitment"], snapshot["events"])
        stored = json.loads(row["result_json"])
        input_ok = digest(snapshot) == row["input_sha256"]
        result_ok = recomputed == stored
        return {
            "period_id": period_id,
            "version_no": row["version_no"],
            "consistent": input_ok and result_ok,
            "input_sha256_ok": input_ok,
            "result_ok": result_ok,
            "availability": stored["availability"],
            "lost_energy_mwh": stored["lost_energy_mwh"]["total"],
            "compensation_cny": stored["compensation_cny"],
        }

    @_serialized
    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM settlement_audit_events ORDER BY event_id").fetchall()
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
