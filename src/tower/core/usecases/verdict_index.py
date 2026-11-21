"""Verdict index for the extension: which posts of a hash bucket have a visible verdict.

The extension never asks about a post it shows. It hashes the post's native id
(``sha256`` of the UTF-8 id), asks for the bucket named by the first 2 bytes (4 hex
characters, 65,536 buckets) and compares the full hashes locally. Gate only learns the
bucket, shared by a huge number of posts. A bucket stays small whatever the number of
fact-checks (2 million verdicts: about 30 hashes per bucket).

The rule is the one of ``get_public_verdict``: a claim of the post belongs (not
removed) to a group whose active verdict is ``active`` and not expired.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput
from tower.core.domain.platforms import Platform
from tower.core.domain.security import Actor, Permission

__all__ = ["MAX_BUCKET_HASHES", "bucket_of", "verdict_bucket"]

MAX_BUCKET_HASHES = 5000
"""Safety bound: about 150 times the expected size of a bucket at 2 million verdicts."""
_BUCKET = re.compile(r"[0-9a-f]{4}")
_PLATFORMS = frozenset(p.value for p in Platform)

# Same expression as the index ix_content_items_verdict_bucket (migration 0017).
_QUERY = text(
    """
    SELECT encode(sha256(ci.external_id::bytea), 'hex') AS hash
    FROM content_items ci
    WHERE ci.platform = :platform
      AND substring(sha256(ci.external_id::bytea) FROM 1 FOR 2) = :bucket
      AND EXISTS (
          SELECT 1
          FROM content_claims cc
          JOIN claim_group_members m ON m.claim_id = cc.claim_id AND m.removed_at IS NULL
          JOIN claim_groups g ON g.id = m.group_id
          JOIN verdicts v ON v.id = g.active_verdict_id
          WHERE cc.content_id = ci.id
            AND v.status = 'active'
            AND (v.expires_at IS NULL OR v.expires_at > now())
      )
    ORDER BY hash
    LIMIT :limit
    """
)


def bucket_of(external_id: str) -> str:
    """Bucket of a native id, as the extension computes it (for tests and tools)."""
    import hashlib

    return hashlib.sha256(external_id.encode()).hexdigest()[:4]


async def verdict_bucket(
    session: AsyncSession, actor: Actor, platform: str, bucket: str
) -> list[str]:
    """Full sha256 hashes (hex) of the bucket's posts that have a visible verdict."""
    actor.require(Permission.CASE_READ_PUBLIC)
    bucket = bucket.lower()
    if platform not in _PLATFORMS or not _BUCKET.fullmatch(bucket):
        raise InvalidInput(
            "Invalid verdict index bucket or platform",
            code=ec.FCK_BAD_INDEX_BUCKET,
            platform=platform[:32],
            bucket=bucket[:16],
        )
    rows = await session.execute(
        _QUERY,
        {"platform": platform, "bucket": bytes.fromhex(bucket), "limit": MAX_BUCKET_HASHES},
    )
    return [str(r.hash) for r in rows]
