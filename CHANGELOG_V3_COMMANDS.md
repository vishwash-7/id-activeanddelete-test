# ID Inactive & Deleted — V3 Editable Settings

- Added private-chat-only `/inactive`, `/setinactive DAYS`, and `/setwarning HOURS` commands.
- Only the owner and approved Bot Admins can read/change settings.
- Settings are persisted in SQLite and survive restarts.
- Activity threshold accepts 1–365 days; warning grace accepts 1–720 hours.
- Changing the activity limit preserves last-message timestamps and gives currently warned members a fresh grace period.
- Raising the activity threshold clears warnings that are no longer due, so the warning cannot expire in the background and cause an instant ban later.
- Changing the warning grace period gives currently warned members a fresh full period.
- Welcome, activation, warning, and ban messages use current saved settings.
- Added state regression tests for persistence and warning timer safety.
