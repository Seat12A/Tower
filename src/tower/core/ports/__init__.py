"""Ports : interfaces des dépendances externes, implémentées par des adaptateurs.

Le socle ne fournit que les contrats. Les adaptateurs concrets (API X, Anthropic,
recherche web, Discord) vivront dans les services qui en ont besoin, et les tests
pourront les remplacer par des doublures.
"""

from tower.core.ports.clock import Clock, SystemClock
from tower.core.ports.external import LLMClient, Notifier, SearchClient, XClient, XPost

__all__ = ["Clock", "LLMClient", "Notifier", "SearchClient", "SystemClock", "XClient", "XPost"]
