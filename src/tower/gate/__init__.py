"""gate: the Tower's single HTTP API (public site, extension, bot, future clients).

No client accesses the database directly: everything goes through here. gate is stateless
and scales by adding Railway replicas.
"""
