"""
Whole-page import, and the repair pass that follows it.

WHY TWO STEPS
-------------
Logseq mints an entity for any reference it parses on write, so importing a
page with live `[[X]]` syntax creates a stub page for every target that does
not exist yet -- which is how a graph accumulates duplicate stubs. So the
import escapes references to inert placeholders, and `repair_links` converts
them once the targets exist.

The split also matches how the work actually happens: import twenty pages,
then repair once, at which point most links resolve because their targets are
now present.

WHY ONE TOOL CALL
-----------------
The saving is on the way IN. A 68-block page is 68 tool calls and 68 responses
if built block by block; as one import it is one call carrying the markdown.
The internal call count (34 for that page) is a smaller consideration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import get_close_matches
from typing import Any

import httpx

from ._shared import VerifiedWriteHelpers
from .client import LogseqDBClient, serialized_write
from .content import VerifiedContent
from .markdown import (
    ParsedBlock,
    find_placeholders,
    parse_markdown,
    restore_reference,
)

# Above this, a repair is more likely a broken import than an intention.
DEFAULT_MAX_PAGES_TO_CREATE = 5
# Distance for "did you mean" against existing titles.
NEAR_MISS_CUTOFF = 0.82


@dataclass(frozen=True, kw_only=True)
class ImportResult:
    verified: bool = True
    page_uuid: str | None = None
    page_title: str | None = None
    created_page: bool = False
    blocks: int = 0
    calls: int = 0
    escaped_links: tuple[str, ...] = ()
    escaped_tags: tuple[str, ...] = ()
    page_properties: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    diagnostic: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VerifiedImport(VerifiedWriteHelpers):
    def __init__(self, client: LogseqDBClient) -> None:
        self._client = client
        self._content = VerifiedContent(client)

    # ------------------------------------------------------------- import

    @serialized_write
    async def import_page(
        self,
        target: str,
        markdown: str,
        *,
        replace: bool = False,
        dry_run: bool = False,
    ) -> ImportResult:
        """
        Build a whole page from Logseq markdown in one call.

        `target` is either a page UUID (write into that page) or a title
        (create it). References are escaped rather than written live -- see
        the module docstring.
        """
        parsed = parse_markdown(markdown)

        if dry_run:
            return ImportResult(
                verified=False,
                blocks=parsed.block_count(),
                calls=parsed.call_count(),
                escaped_links=tuple(sorted(set(parsed.links))),
                escaped_tags=tuple(sorted(set(parsed.tags))),
                page_properties=parsed.page_properties,
                warnings=tuple(parsed.warnings),
                diagnostic=(
                    "Dry run: nothing was written, so verified is false by "
                    "design. Page properties are parsed but NOT applied — "
                    "they are outside the writable namespace."))

        page, created = await self._resolve_target(target)
        page_uuid = page["uuid"]

        if replace:
            await self._content.clear_page(page_uuid)

        calls = await self._insert_tree(page_uuid, parsed.blocks)

        # Verified by count against a parent-walking read, so a block whose
        # owning page came out wrong is still counted and the mismatch shows.
        present = await self._content.get_block_uuid(page_uuid)
        expected = parsed.block_count()
        if not replace:
            # Appending, so only the delta is ours to account for.
            expected = min(expected, len(present))

        notes = list(parsed.warnings)
        if parsed.page_properties:
            notes.append(
                "page properties were parsed but not applied: "
                + ", ".join(parsed.page_properties)
                + " — built-in and user-namespace properties cannot be "
                "written through this API")
        if parsed.links or parsed.tags:
            notes.append(
                f"{len(set(parsed.links))} link(s) and "
                f"{len(set(parsed.tags))} tag(s) were escaped as placeholders; "
                "run repairLinks once their targets exist")

        return ImportResult(
            verified=len(present) >= expected,
            page_uuid=page_uuid,
            page_title=page.get("title"),
            created_page=created,
            blocks=parsed.block_count(),
            calls=calls,
            escaped_links=tuple(sorted(set(parsed.links))),
            escaped_tags=tuple(sorted(set(parsed.tags))),
            page_properties=parsed.page_properties,
            warnings=tuple(notes),
            diagnostic=(
                None if len(present) >= expected else
                f"Expected at least {expected} blocks on the page, found "
                f"{len(present)}. The import is partial; batches are not "
                "atomic, so earlier levels are committed."))

    async def _resolve_target(self, target: str) -> tuple[dict[str, Any], bool]:
        """
        A UUID names an existing page; anything else is a title to create.

        Disambiguated by shape rather than by a flag, since a page title is
        never a bare UUID in practice.
        """
        if not isinstance(target, str) or not target.strip():
            raise ValueError("target must be a page UUID or a page title")

        try:
            page_uuid = self._validated_uuid(target)
        except ValueError:
            page_uuid = None

        if page_uuid:
            page = await self._content._entity_by_uuid(page_uuid)
            if not page.get("name"):
                raise ValueError("target UUID identifies a block, not a page")
            return page, False

        result = await self._content.create_page(target)
        if not result.verified_entities:
            raise RuntimeError(f"Could not create the page {target!r}")
        return result.verified_entities[0], True

    async def _insert_tree(
        self, parent_uuid: str, blocks: list[ParsedBlock]
    ) -> int:
        """
        Breadth-first, one batched insert per parent.

        Breadth-first rather than depth-first so a failure leaves whole levels
        complete instead of one ragged branch -- easier to audit and easier to
        resume. The response carries the created entities, so a parent's UUID
        is known before its children are inserted.
        """
        calls = 0
        queue: list[tuple[str, list[ParsedBlock]]] = [(parent_uuid, blocks)]

        while queue:
            target, group = queue.pop(0)
            if not group:
                continue
            response = await self._client.call(
                "logseq.DB.insertBatchBlock",
                [target,
                 [{"content": b.content} for b in group],
                 {"sibling": False}])
            calls += 1

            uuids = self._content._created_uuids(response)
            if len(uuids) != len(group):
                raise RuntimeError(
                    f"Inserted {len(group)} blocks under {target} but the "
                    f"response named {len(uuids)}. The import is partial — "
                    "run findOrphans and pageStats before retrying, because "
                    "retrying would duplicate what already landed.")

            for block, block_uuid in zip(group, uuids):
                if block.children:
                    queue.append((block_uuid, block.children))
        return calls

    # ------------------------------------------------------------- repair

    @serialized_write
    async def repair_links(
        self,
        page_uuid: str | None = None,
        *,
        create_missing: bool = False,
        acknowledge_page_creation: bool = False,
        max_pages_to_create: int = DEFAULT_MAX_PAGES_TO_CREATE,
        include_tags: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Convert `{{link:X}}` and `{{tag:X}}` placeholders back to live syntax.

        Idempotent by construction: once Logseq resolves a reference it
        rewrites the stored text to `[[uuid]]`, which no longer matches a
        placeholder. So re-running after further imports is safe.

        With no `page_uuid`, every page is scanned -- the realistic workflow,
        since links usually resolve only after several pages are imported.
        """
        targets = ([page_uuid] if page_uuid
                   else await self._pages_with_placeholders())

        wanted_links: set[str] = set()
        wanted_tags: set[str] = set()
        blocks_by_page: dict[str, list[dict[str, Any]]] = {}

        for uuid in targets:
            blocks = await self._content.get_block_uuid(
                self._validated_uuid(uuid))
            holders = []
            for block in blocks:
                links, tags = find_placeholders(block.get("title") or "")
                if links or (tags and include_tags):
                    holders.append(block)
                    wanted_links.update(links)
                    if include_tags:
                        wanted_tags.update(tags)
            if holders:
                blocks_by_page[uuid] = holders

        resolved, missing, ambiguous = await self._resolve_names(wanted_links)
        near = await self._near_misses(missing)

        if missing and create_missing:
            if not acknowledge_page_creation:
                return {
                    "verified": False,
                    "would_create": sorted(missing),
                    "did_you_mean": near,
                    "diagnostic": (
                        f"{len(missing)} page(s) do not exist and would be "
                        "created: " + ", ".join(sorted(missing)) + ". This is "
                        "how stub pages accumulate — confirm these are real "
                        "pages rather than typos or renames. Set "
                        "acknowledge_page_creation=true to proceed."),
                }
            if len(missing) > max_pages_to_create:
                return {
                    "verified": False,
                    "would_create": sorted(missing),
                    "did_you_mean": near,
                    "diagnostic": (
                        f"{len(missing)} pages would be created, above the "
                        f"limit of {max_pages_to_create}. A number this large "
                        "is more often a broken import than an intention. "
                        "Raise max_pages_to_create deliberately if it is not."),
                }

        if dry_run:
            return {
                "verified": False,
                "pages_scanned": len(targets),
                "blocks_with_placeholders": sum(
                    len(v) for v in blocks_by_page.values()),
                "resolved": sorted(resolved),
                "missing": sorted(missing),
                "ambiguous": sorted(ambiguous),
                "did_you_mean": near,
                "tags": sorted(wanted_tags),
                "diagnostic": "Dry run: nothing was written.",
            }

        if missing and create_missing and acknowledge_page_creation:
            for name in sorted(missing):
                result = await self._content.create_page(name)
                if result.verified_entities:
                    resolved.add(name)
            missing = {n for n in missing if n not in resolved}

        updated, refs_created, unverified = await self._rewrite(
            blocks_by_page, resolved, wanted_tags if include_tags else set())

        return {
            "verified": not unverified,
            "pages_scanned": len(targets),
            "blocks_updated": updated,
            "refs_created": refs_created,
            "resolved": sorted(resolved),
            "missing": sorted(missing),
            "ambiguous": sorted(ambiguous),
            "did_you_mean": near,
            "unverified": unverified,
            "diagnostic": (
                f"Repaired {updated} block(s)."
                + (f" {len(missing)} name(s) still unresolved; their "
                   "placeholders were left in place."
                   if missing else "")
                + (f" {len(ambiguous)} name(s) matched more than one page and "
                   "were skipped rather than guessed." if ambiguous else "")),
        }

    async def _pages_with_placeholders(self) -> list[str]:
        """Pages holding at least one placeholder, so a graph-wide repair does
        not read every page in full."""
        query = (
            '[:find [?uuid ...] :where '
            '[?block :block/title ?title] '
            '[(clojure.string/includes? ?title "{{link:")] '
            '[?block :block/page ?page] [?page :block/uuid ?uuid]]'
        )
        found = await self._client.call(
            "logseq.DB.datascriptQuery", [query]) or []
        return [str(u) for u in found if u]

    async def _resolve_names(
        self, names: set[str]
    ) -> tuple[set[str], set[str], set[str]]:
        resolved: set[str] = set()
        missing: set[str] = set()
        ambiguous: set[str] = set()
        for name in names:
            result = await self._content.get_page_uuid(name)
            if result.get("found"):
                resolved.add(name)
            elif result.get("candidates"):
                # Never pick a write target from a fuzzy match: repairing a
                # link to the wrong page is silent damage.
                ambiguous.add(name)
            else:
                missing.add(name)
        return resolved, missing, ambiguous

    async def _near_misses(self, missing: set[str]) -> dict[str, list[str]]:
        """
        Suggest existing titles close to a missing one.

        Surfaced BEFORE any acknowledgement, because a typo or a rename is the
        common cause of a missing target and creating a page for it is exactly
        the accident the confirmation exists to prevent.
        """
        if not missing:
            return {}
        pages = await self._client.call(
            "logseq.DB.datascriptQuery",
            ['[:find [?title ...] :where [?p :block/name] '
             '[?p :block/title ?title]]']) or []
        titles = [str(t) for t in pages if t]
        suggestions = {}
        for name in missing:
            close = get_close_matches(name, titles, n=3,
                                      cutoff=NEAR_MISS_CUTOFF)
            if close:
                suggestions[name] = close
        return suggestions

    async def _rewrite(
        self,
        blocks_by_page: dict[str, list[dict[str, Any]]],
        resolved: set[str],
        tags: set[str],
    ) -> tuple[int, int, list[dict[str, Any]]]:
        updated = 0
        refs_created = 0
        unverified: list[dict[str, Any]] = []

        for blocks in blocks_by_page.values():
            for block in blocks:
                original = block.get("title") or ""
                content = original
                links, block_tags = find_placeholders(original)

                for name in links:
                    if name in resolved:
                        content = restore_reference(
                            content, name, is_tag=False)
                for name in block_tags:
                    if name in tags:
                        content = restore_reference(content, name, is_tag=True)

                if content == original:
                    continue

                before = len(block.get("refs") or [])
                try:
                    await self._client.call(
                        "logseq.DB.updateBlock",
                        [block["uuid"], content])
                except httpx.TimeoutException:
                    unverified.append(
                        {"uuid": block["uuid"], "reason": "timed out"})
                    continue

                # The text changing is not evidence. Logseq rewrites it to
                # [[uuid]] only when the reference actually resolved, so the
                # ref count is what proves it.
                after = await self._content._optional_entity_by_uuid(
                    block["uuid"])
                gained = len(after.get("refs") or []) - before if after else 0
                if after is None or gained <= 0:
                    unverified.append({
                        "uuid": block["uuid"],
                        "reason": "the text was rewritten but no reference "
                                  "was created",
                    })
                    continue
                updated += 1
                refs_created += gained

        return updated, refs_created, unverified
