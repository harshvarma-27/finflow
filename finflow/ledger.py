"""Money movement, status transitions and the append-only audit trail."""
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .db import transaction


class InsufficientFunds(Exception):
    pass


def new_id():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


def to_paise(value):
    """Rupees (max 2 decimals, > 0) -> integer paise. Rejects floats-with-noise, NaN, negatives."""
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d <= 0 or d != d.quantize(Decimal("0.01")):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError("amount must be a positive number with at most 2 decimals")
    return int(d * 100)


def rupees(paise):
    return f"{Decimal(paise) / 100:.2f}"


def log(tx, case_id, from_status, to_status, actor, note=None):
    """Audit rows are only ever INSERTed - never updated or deleted."""
    tx.run("INSERT INTO audit_log (id, case_id, seq, from_status, to_status, actor, note, at) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
           new_id(), case_id, time.time_ns(), from_status, to_status, actor, note, now())


def set_status(tx, case, new_status, actor, note=None):
    tx.run("UPDATE cases SET status = %s WHERE id = %s", new_status, case["id"])
    log(tx, case["id"], case["status"], new_status, actor, note)
    case["status"] = new_status


def settle(case_id, actor="LEDGER"):
    """Move money for an APPROVED case in ONE transaction; a no-op for any other status.

    - The case row is locked first, so two workers can never settle the same case.
    - Accounts are locked in sorted-id order, so opposite transfers cannot deadlock.
    - On insufficient funds nothing is written except a FAILED status + audit row.
    """
    try:
        with transaction() as tx:
            case = tx.run("SELECT * FROM cases WHERE id = %s FOR UPDATE", case_id).one()
            if case is None or case["status"] != "APPROVED":
                return
            src, dst, amount = case["from_account"], case["to_account"], case["amount_paise"]
            balance = {a: tx.run("SELECT balance_paise FROM accounts WHERE id = %s FOR UPDATE", a)
                       .one()["balance_paise"] for a in sorted({src, dst})}
            if balance[src] < amount:
                raise InsufficientFunds()
            tx.run("UPDATE accounts SET balance_paise = balance_paise - %s WHERE id = %s", amount, src)
            tx.run("UPDATE accounts SET balance_paise = balance_paise + %s WHERE id = %s", amount, dst)
            tx.run("INSERT INTO ledger_entries (id, case_id, from_account, to_account, amount_paise, created_at) "
                   "VALUES (%s, %s, %s, %s, %s, %s)", new_id(), case_id, src, dst, amount, now())
            set_status(tx, case, "SETTLED", actor)
    except InsufficientFunds:
        with transaction() as tx:
            case = tx.run("SELECT * FROM cases WHERE id = %s FOR UPDATE", case_id).one()
            if case and case["status"] == "APPROVED":
                set_status(tx, case, "FAILED", actor, "insufficient funds")
