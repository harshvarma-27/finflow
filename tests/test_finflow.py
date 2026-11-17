"""Run with `pytest` (or `python -m unittest`). Uses a temp SQLite file by default;
set TEST_DATABASE_URL=postgresql://... to run the same tests against PostgreSQL."""
import os
import tempfile
import unittest


class FinFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = None
        if os.environ.get("TEST_DATABASE_URL"):
            os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
        else:
            self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            self.tmp.close()
            os.environ["DATABASE_URL"] = f"sqlite:///{self.tmp.name}"
        os.environ["JWT_SECRET"] = "test-secret"
        from finflow.app import create_app
        from finflow.db import transaction
        self.app = create_app()
        with transaction() as tx:  # clean slate (needed when pointing at a shared PostgreSQL)
            for table in ("audit_log", "ledger_entries", "cases", "accounts", "users"):
                tx.run(f"DELETE FROM {table}")
            tx.run("UPDATE rules SET value_paise = 5000000")
        self.client = self.app.test_client()

    def tearDown(self):
        if self.tmp:
            os.unlink(self.tmp.name)

    # ---- helpers ----
    def signup(self, email, role="CUSTOMER"):
        r = self.client.post("/auth/register", json={"email": email, "password": "password123", "role": role})
        self.assertEqual(r.status_code, 201)
        token = self.client.post("/auth/login", json={"email": email, "password": "password123"}).json["token"]
        headers = {"Authorization": f"Bearer {token}"}
        acc = self.client.get("/accounts/me", headers=headers).json["account_id"] if role == "CUSTOMER" else None
        return headers, acc

    def balance(self, headers):
        return self.client.get("/accounts/me", headers=headers).json["balance"]

    def transfer(self, headers, to_account, amount, key="k1"):
        return self.client.post("/cases", json={"to_account": to_account, "amount": amount},
                                headers={**headers, "Idempotency-Key": key})

    @staticmethod
    def trail(case):
        return [step["to_status"] for step in case["audit_trail"]]

    # ---- auth / RBAC ----
    def test_requires_token_and_role(self):
        self.assertEqual(self.client.get("/accounts/me").status_code, 401)
        customer, _ = self.signup("c@x.com")
        self.assertEqual(self.client.get("/cases", headers=customer).status_code, 403)
        self.assertEqual(self.client.put("/rules/auto-approve-limit", json={"amount": 1}, headers=customer).status_code, 403)

    def test_duplicate_email_and_bad_login(self):
        self.signup("c@x.com")
        dup = self.client.post("/auth/register", json={"email": "c@x.com", "password": "password123"})
        self.assertEqual(dup.status_code, 409)
        bad = self.client.post("/auth/login", json={"email": "c@x.com", "password": "wrong-password"})
        self.assertEqual(bad.status_code, 401)

    # ---- rule engine ----
    def test_low_risk_transfer_is_auto_approved_and_settled(self):
        alice, _ = self.signup("alice@x.com")
        _, bob_acc = self.signup("bob@x.com")
        r = self.transfer(alice, bob_acc, 50000)  # exactly at the limit -> auto-approve
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json["status"], "SETTLED")
        self.assertEqual(self.trail(r.json), ["CREATED", "APPROVED", "SETTLED"])
        self.assertEqual(self.balance(alice), "50000.00")

    def test_high_risk_waits_for_manager_then_settles(self):
        alice, _ = self.signup("alice@x.com")
        bob, bob_acc = self.signup("bob@x.com")
        manager, _ = self.signup("boss@x.com", "MANAGER")
        r = self.transfer(alice, bob_acc, 50000.01)
        self.assertEqual(r.json["status"], "PENDING_REVIEW")
        self.assertEqual(self.balance(alice), "100000.00")  # nothing moved yet
        case_id = r.json["id"]
        queue = self.client.get("/cases", headers=manager).json
        self.assertEqual([c["id"] for c in queue], [case_id])
        self.assertEqual(self.client.post(f"/cases/{case_id}/approve", headers=alice).status_code, 403)
        done = self.client.post(f"/cases/{case_id}/approve", headers=manager)
        self.assertEqual(done.json["status"], "SETTLED")
        self.assertEqual(self.trail(done.json), ["CREATED", "PENDING_REVIEW", "APPROVED", "SETTLED"])
        self.assertEqual(self.balance(bob), "150000.01")
        again = self.client.post(f"/cases/{case_id}/approve", headers=manager)
        self.assertEqual(again.status_code, 409)  # cannot approve twice

    def test_manager_reject_moves_no_money(self):
        alice, _ = self.signup("alice@x.com")
        _, bob_acc = self.signup("bob@x.com")
        manager, _ = self.signup("boss@x.com", "MANAGER")
        case_id = self.transfer(alice, bob_acc, 60000).json["id"]
        r = self.client.post(f"/cases/{case_id}/reject", headers=manager)
        self.assertEqual(r.json["status"], "REJECTED")
        self.assertEqual(self.balance(alice), "100000.00")

    def test_manager_can_change_the_rule_threshold(self):
        alice, _ = self.signup("alice@x.com")
        _, bob_acc = self.signup("bob@x.com")
        manager, _ = self.signup("boss@x.com", "MANAGER")
        self.client.put("/rules/auto-approve-limit", json={"amount": 1000}, headers=manager)
        self.assertEqual(self.transfer(alice, bob_acc, 5000).json["status"], "PENDING_REVIEW")

    # ---- ledger safety ----
    def test_idempotency_key_prevents_double_debit(self):
        alice, _ = self.signup("alice@x.com")
        _, bob_acc = self.signup("bob@x.com")
        first = self.transfer(alice, bob_acc, 10000, key="retry-1")
        second = self.transfer(alice, bob_acc, 10000, key="retry-1")  # client retried
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json["id"], second.json["id"])
        self.assertEqual(self.balance(alice), "90000.00")  # debited once
        clash = self.transfer(alice, bob_acc, 99, key="retry-1")
        self.assertEqual(clash.status_code, 422)  # same key, different request

    def test_insufficient_funds_fails_case_and_changes_no_balance(self):
        alice, _ = self.signup("alice@x.com")
        bob, bob_acc = self.signup("bob@x.com")
        manager, _ = self.signup("boss@x.com", "MANAGER")
        self.client.put("/rules/auto-approve-limit", json={"amount": 500000}, headers=manager)
        r = self.transfer(alice, bob_acc, 100000.01)  # more than the balance
        self.assertEqual(r.json["status"], "FAILED")
        self.assertEqual(self.trail(r.json), ["CREATED", "APPROVED", "FAILED"])
        self.assertEqual((self.balance(alice), self.balance(bob)), ("100000.00", "100000.00"))

    def test_invalid_amounts_rejected(self):
        alice, alice_acc = self.signup("alice@x.com")
        _, bob_acc = self.signup("bob@x.com")
        for bad in (-5, 0, "abc", 10.555, None):
            self.assertEqual(self.transfer(alice, bob_acc, bad).status_code, 400)
        self.assertEqual(self.transfer(alice, alice_acc, 10).status_code, 400)  # to self


if __name__ == "__main__":
    unittest.main()
