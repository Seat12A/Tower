"""Shared adapters of the ports. Every SDK (openai, anthropic, httpx) is imported lazily,
inside constructors or functions: never at module level.
"""
