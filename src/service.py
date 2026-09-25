from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, is_calibration_overdue, today


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None, idempotency_key=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)

        # 版本冲突优先判定：仪器被别人更新后，旧请求一律冲突退回，
        # 后续状态校验和任何写入都不会发生
        if expected_version is not None and entity["version"] != int(expected_version):
            raise ConflictError(
                "version conflict on %s: expected %s, found %s"
                % (entity_id, expected_version, entity["version"])
            )

        # 重复提交沿用首次记录，不重复变更状态、不重复写审计
        if idempotency_key:
            stored = self.repository.get_action_idempotency(actor.user_id, idempotency_key)
            if stored is not None:
                if stored["entity_id"] != entity_id:
                    raise ConflictError("idempotency key already used for another entity")
                return stored["response"]

        payload = dict(data or {})

        # 送检：登记承担人、计划完成日、用途，仪器进入待校准并生成校准工单
        if self.rules.normalize_kind(entity["kind"]) == "instrument" and action == "send_calibration":
            result = self._send_for_calibration(actor, entity, payload, expected_version)
        else:
            result = self._apply_transition(actor, entity, action, payload, expected_version)

        if idempotency_key:
            response = result if isinstance(result, dict) else result[0]
            self.repository.save_action_idempotency(
                actor.user_id, idempotency_key, entity_id, response
            )
        return result

    def _send_for_calibration(self, actor, instrument, data, expected_version):
        expected = int(expected_version) if expected_version is not None else instrument["version"]
        _, patch = self.rules.validate_transition(
            actor, instrument, "send_calibration", data, self._lookup
        )
        requested_at = patch.pop("requested_at", None) or today()
        ticket_data = {
            "instrument_id": instrument["id"],
            "assignee": patch["assignee"],
            "planned_finish_at": patch["planned_finish_at"],
            "purpose": patch["purpose"],
            "requested_at": requested_at,
        }
        ticket_id = str(patch.pop("id", "") or uuid4())
        if self.repository.get_entity(ticket_id):
            raise ConflictError("entity already exists: " + ticket_id)

        instrument_data = dict(instrument["data"])
        instrument_data["calibration_id"] = ticket_id
        audit_entries = [
            (
                instrument["id"],
                "send_calibration",
                instrument["status"],
                "calibrating",
                {"calibration_id": ticket_id},
            ),
            (ticket_id, "create", None, "requested", {"kind": "calibration"}),
        ]

        # 版本冲突会在任何写入（工单、状态、审计）之前抛出，
        # 仪器原状态与已有审计都不被覆盖
        updated, = self.repository.apply_changes([
            {
                "id": instrument["id"],
                "expected_version": expected,
                "status": "calibrating",
                "data": instrument_data,
            }
        ])
        ticket_data["instrument_version"] = updated["version"]
        ticket = self.repository.create_entity(
            ticket_id, "calibration", "requested", ticket_data, actor.user_id
        )
        for target_id, audit_action, from_status, to_status, detail in audit_entries:
            self.audit.record(target_id, actor, audit_action, from_status, to_status, detail)
        return updated

    def _apply_transition(self, actor, entity, action, payload, expected_version):
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        side_effect = patch.pop("__side_effect__", None)

        merged = dict(entity["data"])
        merged.update(patch)

        audit_entries = []
        changes = [{
            "id": entity["id"],
            "expected_version": expected,
            "status": next_status,
            "data": merged,
        }]
        audit_entries.append(
            (entity["id"], action, entity["status"], next_status, {"patch": patch})
        )

        if side_effect:
            instrument = self.repository.get_entity(side_effect["id"])
            if not instrument:
                raise NotFoundError("entity not found: " + side_effect["id"])
            instrument_data = dict(instrument["data"])
            instrument_data.update(side_effect["patch"])
            if side_effect["next_status"] == "active":
                instrument_data.pop("calibration_id", None)
            changes.append({
                "id": instrument["id"],
                "expected_version": side_effect.get("expected_version", instrument["version"]),
                "status": side_effect["next_status"],
                "data": instrument_data,
            })
            audit_entries.append(
                (
                    instrument["id"],
                    side_effect["action"],
                    instrument["status"],
                    side_effect["next_status"],
                    {"patch": side_effect["patch"], "calibration_id": entity["id"]},
                )
            )

        # 单事务乐观锁更新：仪器被别人更新时整体冲突，原状态与审计均不被覆盖
        updated = self.repository.apply_changes(changes)
        for target_id, audit_action, from_status, to_status, detail in audit_entries:
            self.audit.record(target_id, actor, audit_action, from_status, to_status, detail)
        return updated[0]

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None, overdue=False, as_of=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        items = self.repository.list_entities(kind=kind, status=status)
        if overdue:
            items = [item for item in items if is_calibration_overdue(item, as_of)]
        return items

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
