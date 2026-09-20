"""Helpers shared by the verified content and mutation write paths."""

from __future__ import annotations

import re
from typing import Any

import httpx

from .identifiers import require_uuid

# A line beginning with `- ` inside block content is DESTRUCTIVE. Logseq
# truncates the block there and discards everything after it -- measured
# 2026-09-20 on 2.0.1-alpha+nightly.20260826, where "alpha\n- beta" stored
# only "alpha" with a successful response and a correct block count. It does
# not become a child block; it is dropped.
#
# Lives here rather than in the markdown parser because the parser only
# guarded `importPage`. `createBlock` and `updateBlock` take multi-line
# titles too and had no guard at all, which is the same silent loss on a path
# nobody had looked at.
DASH_LINE = re.compile(r"^[\t ]*-\s")


def reject_truncating_content(title: str, *, role: str = "title") -> None:
    """Refuse content Logseq would silently truncate."""
    offender = next(
        (line for line in title.splitlines()[1:] if DASH_LINE.match(line)),
        None)
    if offender is not None:
        raise ValueError(
            f"{role}: a line begins with '- ' ({offender.strip()[:40]!r}). "
            "Logseq truncates the block there and discards the rest, so this "
            "would lose content silently. Use '* ' instead, or split it into "
            "separate blocks.")


def content_loss(sent: str, stored: str) -> str | None:
    """
    Did a write lose content? Returns a diagnostic, or None.

    Equality is NOT the test, and cannot be: Logseq parses content on write,
    so `[[X]]` comes back as `[[uuid]]` and a heading loses its marker. The
    invariant that survives those rewrites is the LINE COUNT -- neither
    reference rewriting nor heading conversion adds or removes a newline,
    while truncation always does.

    The last-line check catches a same-line truncation the count would miss,
    and is skipped when the final line holds a reference or a heading marker,
    since those are exactly the cases Logseq legitimately rewrites.
    """
    sent, stored = sent.rstrip(), stored.rstrip()
    if stored == sent:
        return None
    if sent.count("\n") != stored.count("\n"):
        return (f"{sent.count(chr(10)) + 1} line(s) sent, "
                f"{stored.count(chr(10)) + 1} stored -- content was dropped "
                "on write. The call reported success; this is the read-back "
                "disagreeing with it.")
    last = sent.splitlines()[-1].strip() if sent else ""
    if last and "[[" not in last and "#" not in last:
        if not stored.endswith(last):
            return ("The stored text does not end with the last line sent, "
                    "so content was rewritten or dropped at the end.")
    return None

# Keys a terse digest keeps. Enough to act on the result -- identify the
# entity, see where it landed, see where it sits among its siblings -- and
# nothing proportional to content.
#
# `ident` earns its place: for a tag or property it IS the identity, assigned
# by Logseq rather than derived from the title, and the whole instruction is
# to read it back rather than construct it. A terse mode that dropped it would
# make createProperty unusable.
DIGEST_KEYS = ("uuid", "ident", "parent", "page", "order")


def entity_digest(entity: Any) -> dict[str, Any]:
    """
    An entity reduced to identity and position.

    Read-back verification is right; echoing the payload back is not. A write
    envelope carries the entity TWICE, before and after, so a moved block of
    prose costs its own text twice over for a result whose informational
    content is "it landed, here". Titles, refs, tags and property values are
    dropped; `:db/id` is dropped too, since it is renumbered when a graph is
    rebuilt and must never be persisted.

    References are flattened to the id they point at, because `{"id": 1234}`
    around every value doubles the size of the one part a caller reads.
    """
    if not isinstance(entity, dict):
        # Nothing was observed. The structural keys are still present, so a
        # caller reading result["uuid"] gets None rather than a KeyError.
        return {"uuid": None, "parent": None, "page": None}
    digest: dict[str, Any] = {}
    for key in DIGEST_KEYS:
        value = entity.get(key)
        if value is None and key not in ("uuid", "parent", "page"):
            # Structural keys are reported even when absent, because their
            # absence is information -- a page has no parent. `ident` and
            # `order` are simply not applicable to most entities, and a null
            # per write adds up for nothing.
            continue
        digest[key] = (value.get("id") if isinstance(value, dict) else value)
    return digest


def entity_digests(
    entities: Any, *, limit: int | None = 20
) -> list[dict[str, Any]]:
    """
    Digests for a collection, bounded by default.

    Bounded because the collections this was written for are unbounded: a
    cleared page or a deleted subtree can be hundreds of entities, and a terse
    mode that grows with the damage is not terse.

    `limit=None` for a collection of IDENTIFIERS THE CALLER MUST ACT ON. That
    distinction was learned the hard way: `createPageofBlocks` returned
    `created_count: 55` beside 20 UUIDs, so the blocks past the cap were
    unaddressable and nothing said so. A count that disagrees with the list
    beside it is worse than either a long list or an honest refusal. Anywhere
    the bound still applies, report the true count next to it.
    """
    items = [e for e in (entities or []) if isinstance(e, dict)]
    if limit is not None:
        items = items[:limit]
    return [entity_digest(e) for e in items]


class VerifiedWriteHelpers:
    """UUID validation, write-scope checks, and ambiguous-write handling."""

    _client: Any

    async def _call_ambiguous(self, method: str, args: list[Any]) -> tuple[Any, bool]:
        """
        Call a write and report whether the outcome is ambiguous.

        A timeout is not a failure -- Logseq may have applied the write before
        the connection dropped. The caller must resolve it by reading back,
        which is why the flag is returned rather than an exception raised.
        """
        try:
            return await self._client.call(method, args), False
        except httpx.TimeoutException:
            return None, True

    @staticmethod
    def _validated_uuid(value: Any, *, role: str = "UUID") -> str:
        """
        Normalise a UUID or explain what was passed instead.

        Accepts every form `uuid.UUID()` did -- braces, uppercase, missing
        separators, urn: prefix -- and rejects the wrong KIND of value with a
        message naming what it looks like. That distinction matters because
        passing a title or an ident here does not error at the API; it returns
        success and does nothing.
        """
        return require_uuid(value, role=role)

    @staticmethod
    def _validate_title(title: Any) -> str:
        """
        Check that a title is usable, without applying any write scope.

        Used for block content. LOGSEQ_WRITE_TITLE_PREFIXES scopes which NAMED
        ENTITIES may be created -- pages, tags, properties. Applying it to
        block bodies too would mean every sentence written into the graph had
        to begin with the operator's prefix, which is not what it is for.
        """
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Expected a non-empty title, got {title!r}")
        return title

    def _require_title(self, title: Any) -> str:
        """
        Validate a NAMED ENTITY title, then apply any configured write scope.

        For block content use `_validate_title`, which skips the scope.
        """
        self._validate_title(title)
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_title(title)
        return title

    def _require_entity(self, entity_uuid: str) -> str:
        """
        Apply any configured entity-UUID write scope.

        This must be called on every write target. An operator who sets
        LOGSEQ_WRITE_ENTITY_UUIDS is fail-closing deliberately; a write path
        that skips the check leaves them believing in a restriction that is not
        being applied.
        """
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_entity(entity_uuid)
        return entity_uuid