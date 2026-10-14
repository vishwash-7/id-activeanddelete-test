# V4 — Group-only commands and per-group settings

- `/start` responds only in private chat; it silently ignores group/channel use.
- `/setinactive`, `/setwarning`, and `/inactive` run only in a group/supergroup and are silent in private chats.
- Activity thresholds and warning deadlines are stored by Telegram group ID; changing one group never mutates another group's settings or warning clocks.
- Group settings commands require the current group's Telegram admin/owner, or the configured bot owner. Bot-level approval alone does not grant cross-group configuration access.
- Existing groups receive per-group defaults from `ACTIVITY_DAYS` and `WARNING_HOURS` when the group settings table is first added; settings remain persistent across restarts.
- Includes regression tests for command silence in the wrong chat type, per-group setting isolation, warning-clock reset scope, group admin authorization, and existing ban/unban concurrency logic.
