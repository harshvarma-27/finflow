"""Case workflow: submit -> rule engine decision -> (manager review) -> settlement."""
from flask import Blueprint, g, jsonify, request

from .auth import require_auth
from .db import IntegrityError, transaction
from .errors import ApiError
from .ledger import log, new_id, now, rupees, set_status, settle, to_paise

bp = Blueprint("cases", __name__)
RULE = "AUTO_APPROVE_LIMIT"


def _limit(tx):
    return tx.run("SELECT value_paise FROM rules WHERE name = %s", RULE).one()["value_paise"]


def _view(case_id):
    with transaction() as tx:
        case = tx.run("SELECT * FROM cases WHERE id = %s", case_id).one()
        trail = tx.run("SELECT from_status, to_status, actor, note, at FROM audit_log "
                       "WHERE case_id = %s ORDER BY seq", case_id).all()
    if case is None:
        raise ApiError(404, "case not found")
    case["amount"] = rupees(case.pop("amount_paise"))
    case["audit_trail"] = trail
    return case


def _amount(value):
    try:
        return to_paise(value)
    except ValueError as exc:
        raise ApiError(400, str(exc))


@bp.post("/cases")
@require_auth("CUSTOMER")
def submit_case():
    """Customer submits a transfer. Rule engine: <= limit auto-approves, > limit waits for a manager."""
    key = request.headers.get("Idempotency-Key", "").strip()
    data = request.get_json(silent=True) or {}
    to_account, amount = data.get("to_account"), _amount(data.get("amount"))
    if not key:
        raise ApiError(400, "Idempotency-Key header required")
    uid = g.user["id"]
    with transaction() as tx:
        mine = tx.run("SELECT id FROM accounts WHERE user_id = %s", uid).one()
        dest = tx.run("SELECT id FROM accounts WHERE id = %s", to_account).one() if isinstance(to_account, str) else None
    if dest is None or dest["id"] == mine["id"]:
        raise ApiError(400, "to_account must be a different, existing account")

    case_id, created = new_id(), True
    try:
        with transaction() as tx:
            tx.run("INSERT INTO cases (id, customer_id, from_account, to_account, amount_paise, status, "
                   "idempotency_key, created_at) VALUES (%s, %s, %s, %s, %s, 'CREATED', %s, %s)",
                   case_id, uid, mine["id"], dest["id"], amount, key, now())
            log(tx, case_id, None, "CREATED", uid)
            limit = _limit(tx)
            decision = "APPROVED" if amount <= limit else "PENDING_REVIEW"
            set_status(tx, {"id": case_id, "status": "CREATED"}, decision, "RULE_ENGINE",
                       f"auto-approve limit Rs {rupees(limit)}")
    except IntegrityError:  # same Idempotency-Key seen before: return the original case, move no money twice
        created = False
        with transaction() as tx:
            old = tx.run("SELECT id, to_account, amount_paise FROM cases WHERE customer_id = %s "
                         "AND idempotency_key = %s", uid, key).one()
        if old["to_account"] != dest["id"] or old["amount_paise"] != amount:
            raise ApiError(422, "Idempotency-Key was already used for a different request")
        case_id = old["id"]
    settle(case_id)  # no-op unless APPROVED; also finishes a case an earlier crashed attempt left APPROVED
    return jsonify(_view(case_id)), (201 if created else 200)


@bp.get("/cases/<case_id>")
@require_auth()
def get_case(case_id):
    case = _view(case_id)
    if g.user["role"] != "MANAGER" and case["customer_id"] != g.user["id"]:
        raise ApiError(404, "case not found")
    return jsonify(case)


@bp.get("/cases")
@require_auth("MANAGER")
def review_queue():
    status = request.args.get("status", "PENDING_REVIEW")
    with transaction() as tx:
        rows = tx.run("SELECT id FROM cases WHERE status = %s ORDER BY created_at", status).all()
    return jsonify([_view(r["id"]) for r in rows])


def _review(case_id, new_status):
    with transaction() as tx:
        case = tx.run("SELECT * FROM cases WHERE id = %s FOR UPDATE", case_id).one()
        if case is None:
            raise ApiError(404, "case not found")
        if case["status"] != "PENDING_REVIEW":
            raise ApiError(409, f"case is {case['status']}, not PENDING_REVIEW")
        tx.run("UPDATE cases SET reviewed_by = %s WHERE id = %s", g.user["id"], case_id)
        set_status(tx, case, new_status, g.user["id"], "manager decision")
    settle(case_id)
    return jsonify(_view(case_id))


@bp.post("/cases/<case_id>/approve")
@require_auth("MANAGER")
def approve(case_id):
    return _review(case_id, "APPROVED")


@bp.post("/cases/<case_id>/reject")
@require_auth("MANAGER")
def reject(case_id):
    return _review(case_id, "REJECTED")


@bp.get("/rules/auto-approve-limit")
@require_auth()
def get_limit():
    with transaction() as tx:
        return jsonify(amount=rupees(_limit(tx)))


@bp.put("/rules/auto-approve-limit")
@require_auth("MANAGER")
def set_limit():
    paise = _amount((request.get_json(silent=True) or {}).get("amount"))
    with transaction() as tx:
        tx.run("UPDATE rules SET value_paise = %s WHERE name = %s", paise, RULE)
    return jsonify(amount=rupees(paise))
