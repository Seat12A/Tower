"""Ports: interfaces for external dependencies, implemented by adapters.

The core only provides the contracts. Concrete adapters (X API, Anthropic, web search,
Discord) will live in the services that need them, and tests can replace them with
doubles.
"""

from tower.core.ports.clock import Clock, SystemClock
from tower.core.ports.external import LLMClient, Notifier, SearchClient, XClient, XPost

__all__ = ["Clock", "LLMClient", "Notifier", "SearchClient", "SystemClock", "XClient", "XPost"]
