"""Regression tests for inactivity timer and concurrent unban/ban state handling.

Run from the project directory with: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from main import Store, now_ts


class FakeUser:
    id = 123456789
    first_name = "Test"
    full_name = "Test Member"
    username = "test_member"
    is_bot = False


class InactivityStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.sqlite3"
        self.store = Store(self.db_path)
        self.chat_id = -100123456789
        self.user = FakeUser()
        self.old_activity = now_ts() - 15 * 24 * 60 * 60
        self.warning_at = self.old_activity + 1
        self.store.track_activity(self.chat_id, self.user, activity_ts=self.old_activity)
        self.assertTrue(
            self.store.set_warning(
                self.chat_id, self.user.id, self.warning_at,
                expected_last_activity=self.old_activity,
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_new_activity_rejects_stale_warning_write(self) -> None:
        fresh_activity = now_ts()
        self.store.track_activity(self.chat_id, self.user, activity_ts=fresh_activity)

        saved = self.store.set_warning(
            self.chat_id, self.user.id, fresh_activity + 5,
            expected_last_activity=self.old_activity,
        )
        self.assertFalse(saved)
        row = self.store.get_member(self.chat_id, self.user.id)
        self.assertEqual(row["last_activity"], fresh_activity)
        self.assertIsNone(row["warning_sent_at"])

    def test_unban_activity_during_ban_request_is_not_overwritten(self) -> None:
        self.assertTrue(
            self.store.claim_ban(
                self.chat_id, self.user.id, self.old_activity, self.warning_at
            )
        )

        # Models chat_member_update after an admin unbans/reinstates the member
        # while ban_chat_member is awaiting Telegram's response.
        fresh_activity = now_ts()
        self.store.track_activity(self.chat_id, self.user, activity_ts=fresh_activity)
        self.store.finish_ban_claim(
            self.chat_id, self.user.id, self.old_activity, self.warning_at
        )

        row = self.store.get_member(self.chat_id, self.user.id)
        self.assertEqual(row["state"], "member")
        self.assertEqual(row["last_activity"], fresh_activity)
        self.assertIsNone(row["warning_sent_at"])
        self.assertFalse(
            self.store.claim_ban(
                self.chat_id, self.user.id, self.old_activity, self.warning_at
            )
        )

    def test_normal_ban_claim_can_finish(self) -> None:
        self.assertTrue(
            self.store.claim_ban(
                self.chat_id, self.user.id, self.old_activity, self.warning_at
            )
        )
        self.store.finish_ban_claim(
            self.chat_id, self.user.id, self.old_activity, self.warning_at
        )
        row = self.store.get_member(self.chat_id, self.user.id)
        self.assertEqual(row["state"], "kicked")

    def test_restart_recovers_interrupted_ban_claim(self) -> None:
        self.assertTrue(
            self.store.claim_ban(
                self.chat_id, self.user.id, self.old_activity, self.warning_at
            )
        )
        Store(self.db_path)  # simulates process restart
        row = self.store.get_member(self.chat_id, self.user.id)
        self.assertEqual(row["state"], "member")

    def test_runtime_settings_persist_after_restart(self) -> None:
        self.assertEqual(self.store.inactivity_settings(), (13, 24))
        self.store.set_setting_int("activity_days", 21)
        self.store.set_setting_int("warning_hours", 48)
        restarted_store = Store(self.db_path)
        self.assertEqual(restarted_store.inactivity_settings(), (21, 48))

    def test_setting_warning_period_atomically_resets_existing_claim_and_deadline(self) -> None:
        self.assertTrue(
            self.store.claim_ban(
                self.chat_id, self.user.id, self.old_activity, self.warning_at
            )
        )
        reset_count = self.store.set_warning_hours(48)
        row = self.store.get_member(self.chat_id, self.user.id)
        self.assertEqual(reset_count, 1)
        self.assertEqual(self.store.inactivity_settings(), (13, 48))
        self.assertEqual(row["state"], "member")
        self.assertIsNotNone(row["warning_sent_at"])
        self.assertGreaterEqual(int(row["warning_sent_at"]), now_ts() - 2)
        self.assertFalse(
            self.store.claim_ban(
                self.chat_id, self.user.id, self.old_activity, self.warning_at
            )
        )

    def test_raised_activity_threshold_can_clear_stale_warning(self) -> None:
        self.store.clear_warning_if_unchanged(
            self.chat_id, self.user.id, self.old_activity, self.warning_at
        )
        row = self.store.get_member(self.chat_id, self.user.id)
        self.assertEqual(row["state"], "member")
        self.assertIsNone(row["warning_sent_at"])

    def test_setting_only_allows_known_integer_keys(self) -> None:
        with self.assertRaises(ValueError):
            self.store.set_setting_int("lockall", 1)


if __name__ == "__main__":
    unittest.main()
