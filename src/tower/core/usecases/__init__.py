"""Use cases: the only entry point for changing the business state.

Each use case receives a session (transaction opened by the caller) and an
actor, checks permissions, applies the domain rules, writes the audit trail and
publishes events within the same transaction.
"""
