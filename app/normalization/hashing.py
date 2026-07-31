"""Deterministic posting identity (AD-6).

The hash decides what counts as "the same job", which in turn decides what the
user gets notified about. Two failure modes, both bad:

* **Too volatile** — the hash changes when nothing meaningful did, and the user
  is re-notified about a posting they already saw. This is the failure that
  makes people turn notifications off.
* **Too stable** — two genuinely different postings collide, and one is never
  reported at all.

The inputs are chosen to sit between them: company, normalised title,
normalised location, and canonical URL. Everything volatile (tracking
parameters, whitespace, capitalisation, punctuation) is normalised away first.
"""

from __future__ import annotations

import hashlib
import uuid

from app.utils.text import normalize_key
from app.utils.urls import canonicalize_url

#: Bumping this invalidates every stored hash, so a change to the hashing rules
#: does not silently make old rows un-matchable. Migrating means recomputing
#: hashes for existing rows, not just changing the constant.
HASH_VERSION = "1"


def compute_content_hash(
    *,
    company_id: uuid.UUID | str,
    title: str,
    location: str | None = None,
    url: str | None = None,
    external_id: str | None = None,
) -> str:
    """Stable identity for one posting within one company.

    When the platform gave us its own requisition id, that alone is the
    identity — it is the only field guaranteed to survive a title edit, a
    relocation, or a URL migration. Everything else is a reconstruction.
    """
    if external_id and external_id.strip():
        payload = f"{HASH_VERSION}|{company_id}|id:{external_id.strip()}"
    else:
        payload = "|".join(
            (
                HASH_VERSION,
                str(company_id),
                normalize_key(title),
                normalize_key(location) if location else "",
                canonicalize_url(url) if url else "",
            )
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
