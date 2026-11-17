# FinFlow - Banking Case Workflow Engine

A small Flask + PostgreSQL service that models how banks route work: a customer raises a
**case** (fund transfer), a **rule engine** decides whether it is auto-approved or escalated to a
manager, and a **ledger** moves the money safely with a full **audit trail**.

## Flow
1. Customer registers / logs in (JWT, roles `CUSTOMER` and `MANAGER`).
2. `POST /cases` with an `Idempotency-Key` header creates a case (`CREATED`).
3. Rule engine: amount <= auto-approve limit (default Rs 50,000) -> `APPROVED`; otherwise `PENDING_REVIEW`.
4. A manager calls `/cases/<id>/approve` or `/reject` for escalated cases.
5. Approved cases settle in one database transaction -> `SETTLED` (or `FAILED` on insufficient funds).

Statuses: `CREATED -> APPROVED -> SETTLED` (low risk) or `CREATED -> PENDING_REVIEW -> APPROVED -> SETTLED` (escalated).

## Design decisions
- **Rules are data, not code:** the limit lives in a `rules` table and managers change it via `PUT /rules/auto-approve-limit`.
- **ACID settlement:** case row and both accounts locked with `SELECT ... FOR UPDATE` (accounts in sorted order, so no deadlocks); balance update + ledger entry + status change commit or roll back together. A `CHECK (balance_paise >= 0)` is the backstop.
- **Idempotency:** unique `(customer_id, idempotency_key)`; a retry returns the original case. Reusing a key for a different request returns 422. `ledger_entries.case_id` is unique, so one case can never settle twice.
- **Money = integer paise** (no floats). Amounts with more than 2 decimals are rejected.
- **Audit trail:** `audit_log` is insert-only: every status change records who, when and why.

## Run
```bash
docker compose up --build          # API on :5000, PostgreSQL on :5432
```
```bash
# register, log in, transfer
curl -s localhost:5000/auth/register -H 'Content-Type: application/json' -d '{"email":"a@x.com","password":"password123"}'
curl -s localhost:5000/auth/login    -H 'Content-Type: application/json' -d '{"email":"a@x.com","password":"password123"}'
curl -s localhost:5000/cases -H "Authorization: Bearer $TOKEN" -H 'Idempotency-Key: k1' \
     -H 'Content-Type: application/json' -d '{"to_account":"<other account id>","amount":60000}'
```
Endpoints: `POST /auth/register`, `POST /auth/login`, `GET /accounts/me`, `POST /cases`, `GET /cases/<id>`,
`GET /cases?status=` (manager), `POST /cases/<id>/approve|reject` (manager), `GET|PUT /rules/auto-approve-limit`.

## Tests
```bash
pip install -r requirements.txt
pytest                                                    # quick run (temporary SQLite file)
TEST_DATABASE_URL=postgresql://finflow:finflow@localhost:5432/finflow pytest   # against PostgreSQL
```
Covers auth/RBAC, both rule paths, manager approve/reject, configurable threshold, idempotent retries,
insufficient funds (no balance change) and input validation.

## Known limits / next steps
- One deployable service with separate modules (auth, cases/rules, ledger) - not yet split into microservices.
- Self-service `MANAGER` sign-up is for demo only; no refresh tokens or token revocation.
- Next: split ledger into its own service, add an outbox/saga for cross-service failure recovery, concurrency stress test.
