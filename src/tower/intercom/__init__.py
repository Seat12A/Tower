"""intercom: the internal Discord bot. A gate client only: it never touches the database.

Only ``app.py`` imports discord.py; the rest (gate client, handlers, i18n, notifier) is
testable without Discord. See docs/intercom.md.
"""
