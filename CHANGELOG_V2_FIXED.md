# ID Inactive & Deleted — Fixed V2

## Inactivity race-condition fixes

- A warning is saved only if the member's activity timestamp is unchanged while the warning message is being sent.
- Ban claims use conditional SQLite updates tied to the exact `last_activity` and `warning_sent_at` values.
- The bot re-reads the member record after Telegram API waits and rechecks membership immediately before a ban request.
- A successful ban request no longer unconditionally overwrites the database with `kicked`. If a fresh unban/rejoin/activity event has already reset the row, the old scan cannot overwrite that state.
- A post-ban membership check suppresses an inaccurate ban notice if the member is no longer banned and refreshes the baseline if they are already back in the group.
- In-flight `banning` state is recovered on restart; the next scan checks actual Telegram membership before action.
- Member joins/reinstatements clear stale warnings and start a fresh inactivity clock.

## Owner configuration

The provided owner ID `6195715245` is the default in `main.py`, `.env.example`, and the Render blueprint. `BOT_TOKEN` is still required from BotFather. You can override `OWNER_IDS` with comma-separated numeric IDs if needed.

## Regression tests

Run from this directory:

```bash
python -m unittest discover -s tests -v
```

The tests cover stale warning writes, concurrent unban/rejoin during a ban request, normal ban completion, and restart recovery.

## Known Telegram API limitation

This bot tracks group messages, not a user's Telegram-wide online/last-seen status. The official Bot API does not expose a complete group member list or a reliable deleted-account flag, so automated removal of deleted accounts after 30 days is not implemented.
