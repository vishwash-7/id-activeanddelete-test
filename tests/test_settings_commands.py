"""Tests for group-only, per-group inactivity settings commands."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from main import Store, inactive_command, setinactive_command, setwarning_command, start, lockall_command, approve_command, now_ts, ACTIVITY_DAYS, WARNING_HOURS
from telegram.constants import ChatMemberStatus, ChatType


class SettingsCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "settings.sqlite3")
        self.admin_id = 800000001
        self.chat_id = -100123456789
        self.actor = SimpleNamespace(
            id=self.admin_id, first_name="Test Admin", full_name="Test Admin",
            username="test_admin", is_bot=False,
        )
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.update = SimpleNamespace(
            effective_message=self.message,
            effective_user=self.actor,
            effective_chat=SimpleNamespace(
                type=ChatType.SUPERGROUP, id=self.chat_id, title="Settings Test Group"
            ),
        )
        self.bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR))
        )
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"store": self.store}),
            bot=self.bot,
            args=[],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_setinactive_changes_only_current_group(self) -> None:
        self.context.args = ["20"]
        asyncio.run(setinactive_command(self.update, self.context))
        self.assertEqual(self.store.group_inactivity_settings(self.chat_id), (20, WARNING_HOURS))
        other_chat_id = -100222222222
        self.assertEqual(self.store.group_inactivity_settings(other_chat_id), (ACTIVITY_DAYS, WARNING_HOURS))
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("Is group", text)
        self.assertIn("Doosre groups ki settings nahi badli", text)

    def test_setwarning_updates_only_current_group_and_restarts_its_warning(self) -> None:
        user = SimpleNamespace(
            id=700000001, first_name="Member", full_name="Member", username="member", is_bot=False
        )
        other_chat_id = -100222222222
        old_activity = now_ts() - 14 * 24 * 60 * 60
        self.store.track_activity(self.chat_id, user, activity_ts=old_activity)
        self.store.track_activity(other_chat_id, user, activity_ts=old_activity)
        self.store.set_warning(self.chat_id, user.id, old_activity + 1, old_activity)
        self.store.set_warning(other_chat_id, user.id, old_activity + 1, old_activity)
        other_before = self.store.get_member(other_chat_id, user.id)["warning_sent_at"]

        self.context.args = ["48"]
        asyncio.run(setwarning_command(self.update, self.context))
        self.assertEqual(self.store.group_inactivity_settings(self.chat_id), (ACTIVITY_DAYS, 48))
        self.assertEqual(self.store.group_inactivity_settings(other_chat_id), (ACTIVITY_DAYS, WARNING_HOURS))
        local_after = self.store.get_member(self.chat_id, user.id)["warning_sent_at"]
        other_after = self.store.get_member(other_chat_id, user.id)["warning_sent_at"]
        self.assertGreaterEqual(int(local_after), now_ts() - 2)
        self.assertEqual(other_after, other_before)

    def test_invalid_setting_rejected_without_mutation(self) -> None:
        self.context.args = ["0"]
        asyncio.run(setinactive_command(self.update, self.context))
        self.assertEqual(self.store.group_inactivity_settings(self.chat_id), (ACTIVITY_DAYS, WARNING_HOURS))
        self.message.reply_text.assert_awaited_once()

    def test_non_admin_cannot_change_current_group(self) -> None:
        self.bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.MEMBER)
        self.context.args = ["5"]
        asyncio.run(setinactive_command(self.update, self.context))
        self.assertEqual(self.store.group_inactivity_settings(self.chat_id), (ACTIVITY_DAYS, WARNING_HOURS))
        self.assertIn("admin", self.message.reply_text.await_args.args[0])

    def test_settings_commands_are_silent_in_private_chat(self) -> None:
        self.update.effective_chat = SimpleNamespace(type=ChatType.PRIVATE, id=self.admin_id)
        self.context.args = ["5"]
        asyncio.run(setinactive_command(self.update, self.context))
        self.message.reply_text.assert_not_awaited()
        self.assertEqual(self.store.group_inactivity_settings(self.chat_id), (ACTIVITY_DAYS, WARNING_HOURS))

    def test_approved_bot_admin_can_manage_group_but_change_is_scoped(self) -> None:
        self.store.approve_user(self.admin_id, "admin", 6195715245)
        self.bot.get_chat_member.return_value = SimpleNamespace(status=ChatMemberStatus.MEMBER)
        other_chat_id = -100333333333
        self.context.args = ["21"]
        asyncio.run(setinactive_command(self.update, self.context))
        self.assertEqual(self.store.group_inactivity_settings(self.chat_id), (21, WARNING_HOURS))
        self.assertEqual(self.store.group_inactivity_settings(other_chat_id), (ACTIVITY_DAYS, WARNING_HOURS))

    def test_setwarning_is_silent_in_private_chat(self) -> None:
        self.update.effective_chat = SimpleNamespace(type=ChatType.PRIVATE, id=self.admin_id)
        self.context.args = ["48"]
        asyncio.run(setwarning_command(self.update, self.context))
        self.message.reply_text.assert_not_awaited()

    def test_inactive_reports_this_groups_settings(self) -> None:
        self.store.set_group_inactivity_days(self.chat_id, 17, self.admin_id)
        self.store.set_group_warning_hours(self.chat_id, 36, self.admin_id)
        asyncio.run(inactive_command(self.update, self.context))
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("17 days", text)
        self.assertIn("36 hours", text)
        self.assertIn("Settings Test Group", text)

    def test_start_is_silent_in_group(self) -> None:
        # /start must not reply or otherwise require store access in a group.
        self.update.effective_chat = SimpleNamespace(
            type=ChatType.SUPERGROUP, id=self.chat_id, title="Settings Test Group"
        )
        context = SimpleNamespace()
        asyncio.run(start(self.update, context))
        self.message.reply_text.assert_not_awaited()

    def test_inactive_is_silent_in_private_chat(self) -> None:
        self.update.effective_chat = SimpleNamespace(type=ChatType.PRIVATE, id=self.admin_id)
        asyncio.run(inactive_command(self.update, self.context))
        self.message.reply_text.assert_not_awaited()

    def test_lockall_is_silent_in_group(self) -> None:
        asyncio.run(lockall_command(self.update, self.context))
        self.message.reply_text.assert_not_awaited()
        self.assertFalse(self.store.lockall_enabled())

    def test_approve_is_silent_in_group(self) -> None:
        self.context.args = ["700000123"]
        asyncio.run(approve_command(self.update, self.context))
        self.message.reply_text.assert_not_awaited()
        self.assertIsNone(self.store.role_for(700000123))

    def test_unapproved_user_cannot_use_lockall_in_private_chat(self) -> None:
        self.update.effective_chat = SimpleNamespace(type=ChatType.PRIVATE, id=self.admin_id)
        asyncio.run(lockall_command(self.update, self.context))
        self.message.reply_text.assert_awaited_once()
        self.assertIn("permission", self.message.reply_text.await_args.args[0].lower())
        self.assertFalse(self.store.lockall_enabled())

    def test_unapproved_user_cannot_use_approve_in_private_chat(self) -> None:
        self.update.effective_chat = SimpleNamespace(type=ChatType.PRIVATE, id=self.admin_id)
        self.context.args = ["700000123"]
        asyncio.run(approve_command(self.update, self.context))
        self.message.reply_text.assert_awaited_once()
        self.assertIn("permission", self.message.reply_text.await_args.args[0].lower())
        self.assertIsNone(self.store.role_for(700000123))


if __name__ == "__main__":
    unittest.main()
