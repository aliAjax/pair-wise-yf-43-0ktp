from datetime import date
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, calibration_outcome, work_order_overdue


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
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if updated["kind"] == "instrument" and action == "send_calibration":
            self._open_work_order(actor, updated)
        if updated["kind"] == "calibration" and action == "perform":
            self._apply_calibration_outcome(actor, updated)
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return updated

    def _open_work_order(self, actor, instrument):
        data = instrument["data"]
        payload = {
            "instrument_id": instrument["id"],
            "assignee": data.get("assignee"),
            "planned_date": data.get("planned_date"),
            "purpose": data.get("purpose"),
            "requested_at": date.today().isoformat(),
        }
        status = self.rules.initial_status("calibration")
        order = self.repository.create_entity(
            str(uuid4()), "calibration", status, payload, actor.user_id
        )
        self.audit.record(
            order["id"],
            actor,
            "create",
            None,
            status,
            {"kind": "calibration", "instrument_id": instrument["id"]},
        )
        return order

    def _apply_calibration_outcome(self, actor, calibration):
        instrument_id = calibration["data"].get("instrument_id")
        instrument = instrument_id and self.repository.get_entity(instrument_id)
        if not instrument:
            return
        status, patch = calibration_outcome(calibration["data"])
        merged = dict(instrument["data"])
        merged.update(patch)
        updated = self.repository.update_entity(
            instrument["id"], instrument["version"], status, merged
        )
        self.audit.record(
            instrument["id"],
            actor,
            "calibration_outcome",
            instrument["status"],
            updated["status"],
            {"patch": patch, "calibration_id": calibration["id"]},
        )

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        if kind == "calibration" and status == "overdue":
            return [
                entity
                for entity in self.repository.list_entities(kind="calibration")
                if work_order_overdue(entity)
            ]
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
