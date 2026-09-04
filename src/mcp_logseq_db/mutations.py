"""Tag and property mutations with exact resolution and mandatory read-back.

WHAT CHANGED, AND WHY
---------------------
The page/block method pairs are gone. `add_page_tag`/`add_block_tag` and
`upsert_page_property`/`upsert_block_property` did the same thing through the
same API method -- a page IS a block in the DB, so the target is uniform.
Exposing both forced a caller to choose between identical operations. There is
now one `add_tag` and one `set_property`, each taking a target UUID that may be
either.

The graph-worker CLI tag paths are gone. `addBlockTag` and `removeBlockTag`
work over HTTP; the CLI fallback existed on the strength of a capability list
that turned out to be wrong.

Dropped entirely, having no tool in the current surface: `rename_tag`
(routed through `renamePage`), `add_tag_property`, `remove_tag_property`,
`set_tag_parent`, `remove_tag_extends`, `set_block_icon`, `remove_block_icon`.
None of their API methods are in the client allowlist any more.

`getTag` and `getProperty` are likewise gone as routes -- entities are resolved
through Datascript, which is the only read this surface relies on beyond a
handful of dedicated methods.

IDENTIFIER DISCIPLINE
---------------------
Tags are keyed by UUID for relation operations. Properties are keyed by
`:db/ident` -- a UUID passed to `removeProperty` returns success and does
nothing. Targets are always UUIDs. Passing the wrong type is the single most
common failure here and it is silent, so every write reads back.

NAMESPACE SANDBOX
-----------------
Property writes reach only `plugin.property.<caller>/*`. Properties created in
the Logseq UI live under `user.property/*` and are readable but not writable.
This is checked before the call so the failure is a clear error rather than a
silent no-op.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ._shared import VerifiedWriteHelpers
from .client import LogseqDBClient, poll_readback, serialized_write

# Fallback when the client carries no configured prefix. This is the namespace
# FAMILY, not this caller's own namespace -- it admits another plugin's
# properties, which pass the guard and then fail at the API. Setting
# LOGSEQ_PLUGIN_ID replaces it with the exact prefix.
DEFAULT_WRITABLE_PROPERTY_PREFIX = "plugin.property."

# Class markers. Resolved by ident rather than hardcoded :db/id so the code
# survives a rebuilt graph, where integer ids are renumbered.
TAG_CLASS_IDENT = ":logseq.class/Tag"
PROPERTY_CLASS_IDENT = ":logseq.class/Property"


@dataclass(frozen=True, kw_only=True)
class MutationResult:
    response: Any
    verified_state: Any
    recovered_after_timeout: bool = False
    previous_state: Any = None
    diagnostic: str | None = None
    verified: bool = True
    observed_state: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MutationVerificationError(RuntimeError):
    def __init__(self, result: MutationResult) -> None:
        super().__init__(result.diagnostic or "Mutation verification failed")
        self.result = result


class VerifiedMutations(VerifiedWriteHelpers):
    def __init__(self, client: LogseqDBClient) -> None:
        self._client = client

    # ------------------------------------------------------------ tag reads

    async def get_tag_uuid(self, title: str) -> dict[str, Any]:
        """
        Resolve a tag title to exactly one UUID.

        Refuses an ambiguous match. Tag titles are not unique -- the random
        suffix lives in the ident, not the title -- so two tags can share one.
        """
        self._require_title(title)
        tags = await self._client.call("logseq.DB.getTagsByName", [title])
        tags = [t for t in (tags or []) if isinstance(t, dict)]
        if not tags:
            return {"found": False, "title": title, "tag_uuid": None}
        if len(tags) > 1:
            return {
                "found": False,
                "title": title,
                "tag_uuid": None,
                "reason": f"{len(tags)} tags share this title; use a UUID",
                "candidates": [t.get("uuid") for t in tags],
            }
        return {"found": True, "title": title, "tag_uuid": tags[0].get("uuid")}

    async def get_tag(self, tag_uuid: str) -> dict[str, Any]:
        """Read one exact tag entity."""
        return await self._tag(tag_uuid)

    async def get_tag_users(self, tag_uuid: str) -> list[dict[str, Any]]:
        """
        Everything carrying the tag -- pages and blocks together.

        `:block/name` distinguishes them: present means page, absent means
        block. `:block/page` locates a block. This is the work list for
        removing a tag everywhere, and the check to run before deleting it.
        """
        tag_uuid = self._validated_uuid(tag_uuid)
        tag = await self._tag(tag_uuid)
        query = (
            "[:find [(pull ?holder [:db/id :block/uuid :block/title "
            ":block/name {:block/page [:db/id :block/uuid :block/title]}]) "
            "...] :in $ ?tag :where [?holder :block/tags ?tag]]"
        )
        return await self._query_list(query, "Tag usage lookup", tag["id"])

    # ----------------------------------------------------------- tag writes

    @serialized_write
    async def create_tag(
        self, title: str, options: dict[str, Any] | None = None
    ) -> MutationResult:
        """
        Create a tag and verify it through the identity Logseq assigns.

        Tag idents carry a random suffix (`:user.class/xzy-bc0auNqC`), so the
        ident cannot be constructed from the title and must be read back.
        """
        self._require_title(title)
        if not title.strip():
            raise ValueError("Tag title must not be empty")

        response, timed_out = await self._call_ambiguous(
            "logseq.DB.createTag", [title, options or {}])
        if timed_out:
            raise RuntimeError(
                "Tag creation timed out before returning its identity; resolve "
                "the outcome with get_tag_uuid before retrying, or a duplicate "
                "tag may be created"
            )
        if not isinstance(response, dict) or not response.get("uuid"):
            raise RuntimeError(
                "Tag creation did not return an entity with a UUID; the call "
                "may have done nothing"
            )
        tag_uuid = self._validated_uuid(str(response["uuid"]))

        current = await poll_readback(
            self._client,
            lambda: self._optional_entity(tag_uuid),
            lambda e: e is not None,
        )
        if current is None:
            self._raise_verification(
                "Tag creation reported success but the tag is not present",
                response=response, previous_state=None, observed_state=None,
                timed_out=timed_out)
        return MutationResult(response=response, verified_state=current)

    @serialized_write
    async def delete_tag(
        self, tag_uuid: str, *, acknowledge_child_reparent: bool = False
    ) -> MutationResult:
        """
        Delete one tag entity.

        UNVERIFIED ROUTE. This goes through `deletePage`, which has never been
        run against a tag and whose identifier type is unconfirmed -- a wrong
        identifier here returns success and does nothing. The read-back below
        is the only thing standing between that and a false success.
        """
        tag_uuid = self._require_entity(self._validated_uuid(tag_uuid))
        previous = await self._tag(tag_uuid)

        children = await self._child_tags(previous["id"])
        if children and not acknowledge_child_reparent:
            raise ValueError(
                "Deleting this tag will reparent its child tags; set "
                "acknowledge_child_reparent=true to proceed"
            )
        holders = await self.get_tag_users(tag_uuid)

        response, timed_out = await self._call_ambiguous(
            "logseq.DB.deletePage", [tag_uuid])
        current = await poll_readback(
            self._client,
            lambda: self._optional_entity(tag_uuid),
            lambda e: e is None,
        )
        if current is not None:
            self._raise_verification(
                "Tag deletion was not observed; the tag is still present. This "
                "route is unverified and may require a name rather than a UUID.",
                response=response,
                previous_state={"tag": previous, "holders": holders,
                                "child_tags": children},
                observed_state=current,
                timed_out=timed_out)

        dangling = await poll_readback(
            self._client,
            lambda: self._referencing_ids(previous["id"]),
            lambda ids: not ids,
        )
        if dangling:
            self._raise_verification(
                f"Tag deletion left dangling references on entities "
                f"{sorted(dangling)!r}",
                response=response,
                previous_state={"tag": previous, "holders": holders},
                observed_state={"referencing_entity_ids": sorted(dangling)},
                timed_out=timed_out)

        return MutationResult(
            response=response,
            verified_state=None,
            recovered_after_timeout=timed_out,
            previous_state={"tag": previous, "holders": holders,
                            "child_tags": children},
        )

    @serialized_write
    async def add_tag(self, target_uuid: str, tag_uuid: str) -> MutationResult:
        """Attach a tag to a page or a block. The target may be either."""
        return await self._update_tag(target_uuid, tag_uuid, remove=False)

    @serialized_write
    async def remove_tag(self, target_uuid: str, tag_uuid: str) -> MutationResult:
        """
        Detach one tag from a page or a block.

        Removes that relation only. Other tags on the target are untouched and
        the tag entity survives. There is no `upsertNodes` route for this --
        `operation` offers only `add` and `edit`, with no retraction verb, so a
        removal expressed as an upsert would mean overwriting the whole tag set
        and risking the loss of `:logseq.class/Page`.
        """
        return await self._update_tag(target_uuid, tag_uuid, remove=True)

    async def _update_tag(
        self, target_uuid: str, tag_uuid: str, *, remove: bool
    ) -> MutationResult:
        # The scope applies to the entity being changed. The tag is a
        # reference, not a write target, so it is validated but not scoped.
        target_uuid = self._require_entity(self._validated_uuid(target_uuid))
        tag_uuid = self._validated_uuid(tag_uuid)
        previous = await self._entity(target_uuid)
        tag = await self._tag(tag_uuid)

        method = ("logseq.DB.removeBlockTag" if remove
                  else "logseq.DB.addBlockTag")
        response, timed_out = await self._call_ambiguous(
            method, [target_uuid, tag_uuid])

        current = await poll_readback(
            self._client,
            lambda: self._entity(target_uuid),
            lambda e: (tag["id"] in self._reference_ids(e.get("tags", []))) != remove,
        )
        present = tag["id"] in self._reference_ids(current.get("tags", []))
        if present == remove:
            action = "removal" if remove else "addition"
            self._raise_verification(
                f"Tag {action} was not observed on the target",
                response=response, previous_state=previous,
                observed_state=current, timed_out=timed_out)

        # A page that lost :logseq.class/Page is no longer a page. Nothing here
        # should cause that, but it is cheap to notice and expensive to miss.
        if previous.get("name") and not current.get("name"):
            self._raise_verification(
                "Target lost its page identity during the tag change",
                response=response, previous_state=previous,
                observed_state=current, timed_out=timed_out)

        return MutationResult(
            response=response, verified_state=current,
            recovered_after_timeout=timed_out, previous_state=previous)

    # ------------------------------------------------------- property reads

    async def get_property_ident(self, title: str) -> dict[str, Any]:
        """
        Resolve a property title to exactly one `:db/ident`.

        Filters on the Property class rather than merely on the presence of an
        ident -- tags carry idents too and would otherwise match.
        """
        self._require_title(title)
        property_class = await self._class_id(PROPERTY_CLASS_IDENT)
        query = (
            "[:find [(pull ?prop [:db/id :db/ident :block/uuid :block/title "
            ":logseq.property/type]) ...] :in $ ?class ?title :where "
            "[?prop :block/tags ?class] [?prop :block/title ?title]]"
        )
        found = await self._query_list(
            query, "Property title lookup", property_class, title)
        if not found:
            return {"found": False, "title": title, "ident": None}
        if len(found) > 1:
            return {
                "found": False,
                "title": title,
                "ident": None,
                "reason": f"{len(found)} properties share this title",
                "candidates": [p.get("ident") for p in found],
            }
        return {"found": True, "title": title,
                "ident": found[0].get("ident"),
                "type": found[0].get(":logseq.property/type")}

    async def get_property_users(self, property_ident: str) -> list[dict[str, Any]]:
        """
        Everything holding a value for this property, with the value.

        Values come back in both raw and resolved form: reference-typed
        properties store an entity id, scalar types store a literal, and one
        query has to serve both.
        """
        ident = self._validated_ident(property_ident)
        query = (
            "[:find (pull ?holder [:db/id :block/uuid :block/title "
            ":block/name {:block/page [:db/id :block/uuid :block/title]}]) "
            "?value (pull ?value [:db/id :db/ident :block/title]) "
            f":where [?holder {ident} ?value]]"
        )
        rows = await self._query_list(query, "Property usage lookup")
        return [
            {"holder": r[0], "value": r[1], "value_entity": r[2]}
            for r in rows if isinstance(r, list) and len(r) == 3
        ]

    # ------------------------------------------------------ property writes

    @serialized_write
    async def create_property(
        self,
        title: str,
        schema: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> MutationResult:
        """
        Create a property definition.

        Takes a plain title. A namespaced string is rejected by Logseq as a
        page name, and an explicit ident in the schema is silently discarded --
        the namespace comes from caller identity and cannot be chosen.
        """
        self._require_title(title)
        if not title.strip():
            raise ValueError("Property title must not be empty")
        if "/" in title:
            raise ValueError(
                "Property title must be a plain title, not a namespaced ident; "
                "Logseq rejects a '/' as an invalid page name"
            )

        response, timed_out = await self._call_ambiguous(
            "logseq.DB.upsertProperty", [title, schema, options or {}])
        if timed_out:
            raise RuntimeError(
                "Property creation timed out before returning its ident; "
                "resolve the outcome with get_property_ident before retrying"
            )
        if not isinstance(response, dict) or not response.get("ident"):
            raise RuntimeError("Property creation did not return an ident")
        ident = self._validated_ident(str(response["ident"]))

        current = await poll_readback(
            self._client,
            lambda: self._optional_property(ident),
            lambda e: e is not None,
        )
        if current is None:
            self._raise_verification(
                "Property creation reported success but the property is absent",
                response=response, previous_state=None, observed_state=None,
                timed_out=timed_out)

        diagnostic = None
        actual_title = current.get("title")
        if isinstance(actual_title, str) and actual_title != title:
            diagnostic = (
                f"Logseq normalized the title {title!r} to {actual_title!r}; "
                f"use the exact ident {ident!r} for later operations"
            )
        return MutationResult(
            response=response, verified_state=current, diagnostic=diagnostic)

    @serialized_write
    async def delete_property(self, property_ident: str) -> MutationResult:
        """
        Delete a property definition graph-wide.

        UNVERIFIED ROUTE and destructive: every value goes with the definition,
        and recreating the property mints a new entity rather than restoring
        the old values. A UUID passed here returns success and does nothing,
        which is why the ident is required and the removal is read back.
        """
        ident = self._validated_ident(property_ident)
        self._require_writable_property(ident)
        existing = await self._property(ident)
        usage_before = await self.get_property_users(ident)

        response, timed_out = await self._call_ambiguous(
            "logseq.DB.removeProperty", [ident])
        current = await poll_readback(
            self._client,
            lambda: self._optional_property(ident),
            lambda e: e is None,
        )
        if current is not None:
            self._raise_verification(
                f"Property {ident} is still present after removal. This route "
                "is unverified; a wrong identifier type returns success and "
                "does nothing.",
                response=response,
                previous_state={"property": existing, "usage": usage_before},
                observed_state=current,
                timed_out=timed_out)

        remaining = await self.get_property_users(ident)
        if remaining:
            self._raise_verification(
                "Property definition is gone but values remain attached",
                response=response,
                previous_state={"property": existing, "usage": usage_before},
                observed_state=remaining,
                timed_out=timed_out)

        return MutationResult(
            response=response,
            verified_state=None,
            recovered_after_timeout=timed_out,
            previous_state={"property": existing, "usage": usage_before},
        )

    @serialized_write
    async def set_property(
        self,
        target_uuid: str,
        property_ident: str,
        value: Any,
        options: dict[str, Any] | None = None,
    ) -> MutationResult:
        """
        Set a property value on a page or a block. The target may be either.

        Reference-typed properties (node, page, class, property) take an entity
        id, not a literal. Closed enums such as Status and Priority take one of
        the entities listed in `:property/closed-values`.
        """
        target_uuid = self._require_entity(self._validated_uuid(target_uuid))
        ident = self._validated_ident(property_ident)
        self._require_writable_property(ident)
        await self._property(ident)
        previous = await self._entity(target_uuid)

        response, timed_out = await self._call_ambiguous(
            "logseq.DB.upsertBlockProperty",
            [target_uuid, ident, value, options or {}])

        current = await poll_readback(
            self._client,
            lambda: self._entity(target_uuid),
            lambda e: ident in e,
        )
        if ident not in current:
            self._raise_verification(
                f"Property {ident} was not set on the target",
                response=response, previous_state=previous,
                observed_state=current, timed_out=timed_out)
        return MutationResult(
            response=response, verified_state=current,
            recovered_after_timeout=timed_out, previous_state=previous,
            observed_state=current)

    @serialized_write
    async def clear_property(
        self, target_uuid: str, property_ident: str
    ) -> MutationResult:
        """
        Clear a property value from a page or a block.

        Removes the value only; the property definition survives and other
        targets keep theirs. Use `delete_property` to remove the definition.
        """
        target_uuid = self._require_entity(self._validated_uuid(target_uuid))
        ident = self._validated_ident(property_ident)
        self._require_writable_property(ident)
        await self._property(ident)
        previous = await self._entity(target_uuid)

        response, timed_out = await self._call_ambiguous(
            "logseq.DB.removeBlockProperty", [target_uuid, ident])

        current = await poll_readback(
            self._client,
            lambda: self._entity(target_uuid),
            lambda e: ident not in e,
        )
        if ident in current:
            self._raise_verification(
                f"Property {ident} is still set on the target",
                response=response, previous_state=previous,
                observed_state=current, timed_out=timed_out)
        return MutationResult(
            response=response, verified_state=current,
            recovered_after_timeout=timed_out, previous_state=previous)

    # --------------------------------------------------------------- shared

    async def _query_list(
        self, query: str, description: str, *params: Any
    ) -> list[Any]:
        result = await self._client.call(
            "logseq.DB.datascriptQuery", [query, *params])
        if result is None:
            return []
        if not isinstance(result, list):
            raise RuntimeError(f"{description} returned an unexpected shape")
        return [r for r in result if r is not None]

    async def _class_id(self, ident: str) -> int:
        query = f"[:find ?class . :where [?class :db/ident {ident}]]"
        value = await self._client.call("logseq.DB.datascriptQuery", [query])
        if not isinstance(value, int):
            raise RuntimeError(f"Could not resolve the class {ident}")
        return value

    async def _optional_entity(self, entity_uuid: str) -> dict[str, Any] | None:
        entity_uuid = self._validated_uuid(entity_uuid)
        query = (
            "[:find (pull ?entity [*]) . :where "
            f"[?entity :block/uuid #uuid \"{entity_uuid}\"]]"
        )
        entity = await self._client.call("logseq.DB.datascriptQuery", [query])
        if entity is None:
            return None
        if not isinstance(entity, dict) or entity.get("uuid") != entity_uuid:
            raise RuntimeError("Entity lookup returned an unexpected result")
        return entity

    async def _entity(self, entity_uuid: str) -> dict[str, Any]:
        entity = await self._optional_entity(entity_uuid)
        if entity is None:
            raise LookupError(
                f"No entity exists with exact UUID {entity_uuid}")
        return entity

    async def _tag(self, tag_uuid: str) -> dict[str, Any]:
        entity = await self._entity(tag_uuid)
        tag_class = await self._class_id(TAG_CLASS_IDENT)
        if tag_class not in self._reference_ids(entity.get("tags", [])):
            raise ValueError(
                f"UUID {tag_uuid} identifies an entity that is not a tag")
        return entity

    async def _optional_property(self, ident: str) -> dict[str, Any] | None:
        query = f"[:find (pull ?prop [*]) . :where [?prop :db/ident {ident}]]"
        entity = await self._client.call("logseq.DB.datascriptQuery", [query])
        if entity is None:
            return None
        if not isinstance(entity, dict):
            raise RuntimeError("Property lookup returned an unexpected result")
        return entity

    async def _property(self, ident: str) -> dict[str, Any]:
        entity = await self._optional_property(ident)
        if entity is None:
            raise LookupError(f"No property exists with exact ident {ident}")
        return entity

    async def _child_tags(self, parent_tag_id: int) -> list[dict[str, Any]]:
        query = (
            "[:find [(pull ?child [:db/id :db/ident :block/uuid "
            ":block/title]) ...] :in $ ?parent :where "
            "[?child :logseq.property.class/extends ?parent]]"
        )
        return await self._query_list(query, "Child tag lookup", parent_tag_id)

    async def _referencing_ids(self, entity_id: int) -> set[int]:
        found: set[int] = set()
        for attribute in (":block/tags", ":block/refs"):
            query = (
                "[:find [?entity ...] :in $ ?target :where "
                f"[?entity {attribute} ?target]]"
            )
            result = await self._query_list(
                query, "Reference lookup", entity_id)
            found.update(v for v in result if isinstance(v, int))
        return found

    def _require_writable_property(self, ident: str) -> None:
        """
        Reject a property this caller cannot write before the call is made.

        Without this the write reaches Logseq and either errors obscurely or
        returns success having done nothing, depending on the operation.

        The prefix comes from the client so that the namespace limit and the
        configured write policy are one mechanism. Two independent guards on
        the same thing drift apart.
        """
        prefix = getattr(
            self._client, "writable_property_prefix",
            DEFAULT_WRITABLE_PROPERTY_PREFIX)
        bare = ident[1:] if ident.startswith(":") else ident
        if not bare.startswith(prefix):
            raise ValueError(
                f"Property {ident} is outside this caller's namespace and is "
                f"read-only over the HTTP API. Only :{prefix}* properties can "
                "be written; properties created in the Logseq UI live under "
                "user.property/* and cannot."
            )
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_property(ident)

    @staticmethod
    def _raise_verification(
        diagnostic: str,
        *,
        response: Any,
        previous_state: Any,
        observed_state: Any,
        timed_out: bool = False,
    ) -> None:
        raise MutationVerificationError(
            MutationResult(
                response=response,
                verified_state=None,
                recovered_after_timeout=timed_out,
                previous_state=previous_state,
                diagnostic=diagnostic,
                verified=False,
                observed_state=observed_state,
            )
        )

    @staticmethod
    def _validated_ident(value: str) -> str:
        """
        Require a namespaced keyword.

        Built-ins such as `alias` and `tags` carry bare idents with no
        namespace, but nothing in this surface writes them, so the stricter
        form is correct here and catches a UUID passed by mistake.
        """
        if (not isinstance(value, str) or not value.startswith(":")
                or "/" not in value):
            raise ValueError(
                "Expected an exact namespaced property ident such as "
                ":plugin.property.my_plugin/Effort, not a title or a UUID"
            )
        return value

    @staticmethod
    def _reference_ids(references: Any) -> set[int]:
        if not isinstance(references, list):
            return set()
        return {
            r["id"] for r in references
            if isinstance(r, dict) and isinstance(r.get("id"), int)
        }