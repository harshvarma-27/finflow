"""Database layer. PostgreSQL in production; SQLite only for quick local test runs."""
import contextlib
import os
import sqlite3

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:  # SQLite-only environment
    psycopg2 = None

IntegrityError = (sqlite3.IntegrityError,) + ((psycopg2.IntegrityError,) if psycopg2 else ())

# Money is stored as integer paise (never floats). CHECK constraints are the last line of defence.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('CUSTOMER', 'MANAGER')));
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY, user_id TEXT UNIQUE NOT NULL REFERENCES users(id),
  balance_paise BIGINT NOT NULL CHECK (balance_paise >= 0));
CREATE TABLE IF NOT EXISTS rules (name TEXT PRIMARY KEY, value_paise BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES users(id),
  from_account TEXT NOT NULL REFERENCES accounts(id), to_account TEXT NOT NULL REFERENCES accounts(id),
  amount_paise BIGINT NOT NULL CHECK (amount_paise > 0), status TEXT NOT NULL,
  idempotency_key TEXT NOT NULL, reviewed_by TEXT, created_at TEXT NOT NULL,
  UNIQUE (customer_id, idempotency_key));
CREATE TABLE IF NOT EXISTS ledger_entries (
  id TEXT PRIMARY KEY, case_id TEXT UNIQUE NOT NULL REFERENCES cases(id),
  from_account TEXT NOT NULL, to_account TEXT NOT NULL, amount_paise BIGINT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), seq BIGINT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, actor TEXT NOT NULL, note TEXT, at TEXT NOT NULL)
"""


class Tx:
    """Thin cursor wrapper. SQL is written once with %s placeholders and FOR UPDATE."""

    def __init__(self, conn, pg):
        self.pg = pg
        self.cur = conn.cursor(cursor_factory=RealDictCursor) if pg else conn.cursor()

    def run(self, sql, *params):
        if not self.pg:  # SQLite: ? placeholders, no row locks (writes are already serialised)
            sql = sql.replace("%s", "?").replace(" FOR UPDATE", "")
        self.cur.execute(sql, params)
        return self

    def one(self):
        row = self.cur.fetchone()
        return dict(row) if row else None

    def all(self):
        return [dict(r) for r in self.cur.fetchall()]


@contextlib.contextmanager
def transaction():
    """BEGIN ... COMMIT on success, ROLLBACK on any exception."""
    url = os.environ.get("DATABASE_URL", "sqlite:///finflow.db")
    pg = not url.startswith("sqlite")
    if pg:
        conn = psycopg2.connect(url)
    else:
        conn = sqlite3.connect(url.replace("sqlite:///", "", 1), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield Tx(conn, pg)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with transaction() as tx:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                tx.run(statement)
        tx.run("INSERT INTO rules (name, value_paise) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
               "AUTO_APPROVE_LIMIT", 5_000_000)  # Rs 50,000 default; managers can change it
