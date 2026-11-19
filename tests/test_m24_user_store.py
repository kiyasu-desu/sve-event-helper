"""M24 unit tests for UserStore multi-user bindings and isolated notification paths."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/client/core"))

from accountguard import AccountConflict


class UserStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sve-m24-users-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_initializes_owner_from_existing_accounts_without_mutating_identity(self):
        import user_store as us

        identity_path = self.root / "identity-v2.json"
        identity_bytes = b'{"schema_version": 2, "sentinel": "keep-intact"}\n'
        identity_path.write_bytes(identity_bytes)

        store = us.UserStore(self.root)
        data = store.load(initial_accounts=["10001", "10002"], initial_active="10001")

        self.assertEqual(1, data["schema_version"])
        self.assertEqual(["10001", "10002"], store.bound_accounts(data, "owner"))
        self.assertEqual("10001", store.get_active_account(data, "owner"))
        self.assertEqual("admin", data["users"]["owner"]["role"])
        self.assertTrue((self.root / us.USER_DOC).is_file())
        self.assertEqual(identity_bytes, identity_path.read_bytes())

        # Subsequent load reads persisted users-v1.json and ignores initial_accounts.
        reloaded = store.load(initial_accounts=["10003"], initial_active="10003")
        self.assertEqual(["10001", "10002"], store.bound_accounts(reloaded, "owner"))

    def test_multi_user_bind_and_unbind_returns_remaining_holders(self):
        import user_store as us

        store = us.UserStore(self.root)
        data = store.load(initial_accounts=["10001"], initial_active="10001")
        store.bind_account(data, "friend@example.com", "user", "10001", select=True)
        store.bind_account(data, "friend@example.com", "user", "10002", select=True)
        store.save(data)

        reloaded = store.load()
        self.assertEqual(["owner", "friend@example.com"], store.users_for_account(reloaded, "10001"))
        self.assertEqual(["friend@example.com"], store.users_for_account(reloaded, "10002"))
        self.assertEqual("10002", store.get_active_account(reloaded, "friend@example.com"))

        remaining_after_owner = store.unbind_account(reloaded, "owner", "10001", label="TestAccount (UID 10001)")
        self.assertEqual(["friend@example.com"], remaining_after_owner)
        self.assertEqual([], store.bound_accounts(reloaded, "owner"))
        self.assertIsNone(store.get_active_account(reloaded, "owner"))
        self.assertIn("10001", store.unbound_history(reloaded, "owner"))
        self.assertEqual("TestAccount (UID 10001)", store.unbound_history(reloaded, "owner")["10001"]["label"])

        remaining_after_friend = store.unbind_account(reloaded, "friend@example.com", "10001", label="TestAccount (UID 10001)")
        self.assertEqual([], remaining_after_friend)
        self.assertEqual("10002", store.get_active_account(reloaded, "friend@example.com"))
        self.assertIn("10001", store.unbound_history(reloaded, "friend@example.com"))

        # Re-binding clears unbound_history for that user only.
        store.bind_account(reloaded, "owner", "admin", "10001", select=True)
        self.assertNotIn("10001", store.unbound_history(reloaded, "owner"))
        self.assertIn("10001", store.unbound_history(reloaded, "friend@example.com"))

    def test_notify_file_for_isolates_non_owner_with_safe_hashed_directory(self):
        import user_store as us

        store = us.UserStore(self.root)
        owner_path = store.notify_file_for("owner")
        self.assertEqual(self.root.resolve() / "notify.json", owner_path)

        friend_path = store.notify_file_for("Friend@example.com")
        friend_path_lower = store.notify_file_for("friend@example.com")
        evil_path = store.notify_file_for("../evil@example.com")

        self.assertEqual(friend_path, friend_path_lower)
        self.assertNotEqual(owner_path, friend_path)
        self.assertNotEqual(friend_path, evil_path)
        for path in (friend_path, evil_path):
            rel = path.relative_to(self.root.resolve())
            self.assertEqual(3, len(rel.parts))
            self.assertEqual("users", rel.parts[0])
            self.assertEqual(24, len(rel.parts[1]))
            self.assertRegex(rel.parts[1], r"^[0-9a-f]{24}$")
            self.assertEqual("notify.json", rel.parts[2])

    def test_rejects_corrupt_or_symlinked_users_file(self):
        import user_store as us

        store = us.UserStore(self.root)
        users_file = self.root / us.USER_DOC

        users_file.write_text("{corrupt-json", encoding="utf-8")
        with self.assertRaises(AccountConflict):
            store.load()
        self.assertEqual("{corrupt-json", users_file.read_text(encoding="utf-8"))

        users_file.write_text(json.dumps({"schema_version": 99, "users": {}}), encoding="utf-8")
        with self.assertRaises(AccountConflict):
            store.load()

        users_file.write_text(json.dumps({
            "schema_version": 1,
            "users": {"owner": {"role": "admin", "bound_account_ids": ["bad-uid"], "active_account_id": None, "unbound_history": {}}},
        }), encoding="utf-8")
        with self.assertRaises(AccountConflict):
            store.load()

        users_file.unlink()
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            users_file.symlink_to(outside)
        except OSError:
            users_file.write_text(json.dumps({"schema_version": 1, "users": {}}), encoding="utf-8")
            with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == users_file.resolve()):
                with self.assertRaises(AccountConflict):
                    store.load()
        else:
            with self.assertRaises(AccountConflict):
                store.load()


if __name__ == "__main__":
    unittest.main()
