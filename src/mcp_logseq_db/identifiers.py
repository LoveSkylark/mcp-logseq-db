"""Identifier validation for the boundary between callers and the DB API.

WHY THIS EXISTS
---------------
Every entity kind here has one canonical key, and passing the wrong one does
not raise -- it returns success and does nothing:

    blocks, pages   :block/uuid
    tags            UUID for relations, :db/ident for lookups
    properties      :db/ident        (a UUID here is a silent no-op)

Because the failure is silent, the only place it can be caught cheaply is
before the call. A validator that rejects a bad value is table stakes; the
useful part is naming what was passed instead, since the caller nearly always
has the right value one lookup away.

Observed mistakes, all of which reach the API and quietly do nothing:

    "$TAG-UUID"                     placeholder never substituted
    "{{tagUuid}}"                   Postman-style variable
    ":user.class/xzy-bc0auNqC"      the tag's ident, not its UUID
    "TAG-TEST"                      the tag's title
    "859"                           a :db/id, unstable across rebuilds
"""

from __future__ import annotations

import re

UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# A UUID with the right shape but the wrong case or surrounding whitespace is a
# formatting slip, not a wrong value -- normalise rather than reject.
_HEX_RE = re.compile(r"\A[0-9a-fA-F]{32}\Z")

_IDENT_RE = re.compile(r"\A:?[a-z][\w.]*/[\w.?!-]+\Z", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\A[\$<{\[]|[>}\]]\Z")


class IdentifierError(ValueError):
    """A value that cannot be an entity UUID, with what it looks like instead."""


def require_uuid(value: object, *, role: str, hint: str | None = None) -> str:
    """
    Return `value` as a canonical UUID string, or raise with a diagnosis.

    `role` names the argument ("tag", "target") so a two-UUID call says which
    one is wrong. `hint` names the lookup that produces the right value.
    """
    if not isinstance(value, str):
        raise IdentifierError(
            f"{role} must be a UUID string, not {type(value).__name__}."
            + _hint(hint)
        )

    text = value.strip()
    if not text:
        raise IdentifierError(f"{role} is empty." + _hint(hint))

    # Accept the forms uuid.UUID() has always accepted -- braces, uppercase,
    # missing or unusual separators -- and normalise them. These are cosmetic
    # variations on the right value, not the wrong kind of value.
    compact = re.sub(r"[\s_-]", "", text.strip("{}").removeprefix("urn:uuid:"))
    if len(compact) == 32 and _HEX_RE.match(compact):
        return "-".join((compact[:8], compact[8:12], compact[12:16],
                         compact[16:20], compact[20:])).lower()

    raise IdentifierError(
        f"{role} is not a UUID: {value!r}. {_diagnose(text)}" + _hint(hint)
    )


def _diagnose(text: str) -> str:
    """Say what the value looks like, so the caller knows what to fix."""
    if _PLACEHOLDER_RE.search(text) or text.startswith("{{"):
        return (
            "This looks like an unsubstituted placeholder -- the template "
            "variable was sent literally rather than replaced with a value."
        )
    if _IDENT_RE.match(text):
        return (
            "This is a :db/ident, not a UUID. Tags carry both; relation "
            "operations take the UUID, and the ident is only for lookups."
        )
    if text.isdigit():
        return (
            "This is a :db/id. Those are internal integers that change when a "
            "graph is rebuilt and are never accepted as identifiers."
        )
    # Right characters, wrong separators. Check the hex payload rather than
    # the exact punctuation, so spaces, missing hyphens, and underscores are
    # all reported as a formatting problem instead of an unknown value.
    return "This looks like a title or name."


def _hint(hint: str | None) -> str:
    return f" Use {hint} to resolve it first." if hint else ""