from datetime import date, datetime

from .domain import (
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def today():
    return date.today().isoformat()


def _parse_date(value, field):
    if not value:
        raise ValidationError("missing required field: " + field)
    try:
        datetime.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValidationError(field + " must be an ISO date (YYYY-MM-DD)")


def calibration_current(due_at, as_of):
    return str(due_at)[:10] >= str(as_of)[:10]


def is_calibration_overdue(entity, as_of=None):
    """送检工单仍处于待回填状态，且已过计划完成日（计划日当天不算逾期）。"""
    as_of = as_of or today()
    if entity.get("status") != "requested":
        return False
    planned = entity.get("data", {}).get("planned_finish_at")
    if not planned:
        return False
    return _date_ordinal(planned) < _date_ordinal(as_of)


def _open_calibration(instrument, lookup):
    linked = instrument["data"].get("calibration_id")
    if linked:
        tickets = lookup("calibration", "id", linked) or []
        if tickets and tickets[0]["status"] == "requested":
            return tickets[0]
    tickets = lookup("calibration", "instrument_id", instrument["id"]) or []
    open_tickets = [ticket for ticket in tickets if ticket["status"] == "requested"]
    return open_tickets[-1] if open_tickets else None


def _validate_send(actor, entity, data, lookup):
    _parse_date(data.get("planned_finish_at"), "planned_finish_at")
    if data.get("requested_at"):
        _parse_date(data.get("requested_at"), "requested_at")
    return {}


def _validate_perform(actor, entity, data, lookup):
    result = data.get("result")
    if result not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    _parse_date(data.get("performed_at"), "performed_at")
    instrument = _find_one(lookup, "instrument", "id", entity["data"].get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")
    if instrument["status"] != "calibrating":
        raise InvalidTransition("instrument is not awaiting calibration backfill")

    patch = {"result": result, "performed_at": data.get("performed_at")}
    # 送检期间仪器不应被他人改动：以工单登记的仪器版本为准
    expected_instrument_version = entity["data"].get("instrument_version") or instrument["version"]
    if result == "passed":
        due_at = data.get("due_at")
        if not due_at:
            raise ValidationError("passed calibration requires due_at")
        _parse_date(due_at, "due_at")
        patch["due_at"] = due_at
        side_effect = {
            "id": instrument["id"],
            "expected_version": expected_instrument_version,
            "action": "calibrate",
            "next_status": "active",
            "patch": {"due_at": due_at},
        }
    else:
        disposition = data.get("disposition")
        if not disposition:
            raise ValidationError("failed calibration requires disposition")
        patch["disposition"] = disposition
        side_effect = {
            "id": instrument["id"],
            "expected_version": expected_instrument_version,
            "action": "quarantine",
            "next_status": "quarantined",
            "patch": {"disposition": disposition},
        }
    patch["__next_status__"] = result
    patch["__side_effect__"] = side_effect
    return patch


def _validate_result_release(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    if not instrument:
        raise ValidationError("result requires an instrument")
    open_ticket = _open_calibration(instrument, lookup)
    if open_ticket:
        # 校准结果尚未回填：放行请求带工单编号退回
        raise ValidationError(
            "calibration result not backfilled; work order %s is pending"
            % open_ticket["id"]
        )
    if instrument["status"] != "active":
        raise ValidationError(
            "instrument is out of service (status %s)" % instrument["status"]
        )
    if not calibration_current(instrument["data"].get("due_at", ""), today()):
        raise ValidationError("instrument calibration is not current")
    return {"released_by": actor.user_id}


CUSTOM_TRANSITIONS = {
    ("instrument", "send_calibration"): _validate_send,
    ("calibration", "perform"): _validate_perform,
    ("result", "release"): _validate_result_release,
}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active', 'calibrating'), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested',), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending',), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement')}
    ACTION_REQUIRED = {('instrument', 'send_calibration'): ('assignee', 'planned_finish_at', 'purpose'), ('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'metrology', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        patch = dict(data)
        if custom:
            patch.update(custom(actor, entity, data, lookup) or {})
        # 合格/不合格由回填数据决定，允许自定义校验覆盖目标状态
        next_status = patch.pop("__next_status__", next_status)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
