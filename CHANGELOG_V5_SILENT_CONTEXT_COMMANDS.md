# V5 — Silent wrong-context commands

- `/start` is private-chat only and silent in groups/channels.
- `/lockall` and `/approve` are private-chat only and now silently ignore group use (no reply and no action).
- `/setinactive`, `/setwarning`, and `/inactive` remain group-only and silently ignore private-chat use.
- `/lockall` and `/approve` require the owner ID or an approved Bot Admin role. Ordinary Telegram group admins and `Bot Use Only` users do not get these bot-management permissions automatically.
- Per-group inactivity and warning settings remain scoped to the current group.
