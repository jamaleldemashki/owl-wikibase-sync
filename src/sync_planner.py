"""
Two-pass synchronization planner.

This module builds the list of :class:`~src.models.PlannedAction` objects
that both dry-run *and* live mode execute (design decision #10: one
planner, two execution modes). It never performs a write -- reads only
(local cache + optional Wikibase reads via ``WikibaseClient``).

Pass A resolves or plans creation of every OWL class/named individual as a
Wikibase item, and plans label/description/alias changes.
Pass B (run only after Pass A has produced a QID -- real or ``<NEW_ITEM>`` --
for every entity) plans statement additions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .cache_manager import EntityLookupCache
from .config import PipelineConfig
from .datatype_conversion import convert_owl_value_to_wikibase, normalize_value_for_comparison
from .models import (
    NEW_ITEM_MARKER,
    ActionType,
    OntologyEntity,
    OntologyStatement,
    PlannedAction,
    PropertyMapping,
    ResolvedEntity,
    Severity,
)
from .ontology_parser import resolve_property_map_key
from .wikibase_client import WikibaseClient

logger = logging.getLogger("owl_wikibase_sync.sync_planner")


@dataclass
class ItemSnapshot:
    """Cached view of an existing Wikibase item's current state."""

    labels: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    claims: dict[str, list[str]] = field(default_factory=dict)  # pid -> comparable value strings


@dataclass
class PlanResult:
    resolved_entities: dict[str, ResolvedEntity]
    actions: list[PlannedAction]
    snapshots: dict[str, ItemSnapshot]  # qid -> snapshot, for reuse by the synchronizer

    def action_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.action] = counts.get(action.action, 0) + 1
        return counts


def _fetch_snapshot(client: WikibaseClient, qid: str) -> Optional[ItemSnapshot]:
    try:
        item = client.get_item(qid)
        labels = {lang: value.value for lang, value in item.labels.values.items()} if item.labels else {}
        descriptions = (
            {lang: value.value for lang, value in item.descriptions.values.items()} if item.descriptions else {}
        )
        aliases: dict[str, list[str]] = {}
        if item.aliases:
            for lang, alias_values in item.aliases.aliases.items():
                aliases[lang] = [alias.value for alias in alias_values]
        claims = client.get_existing_claim_values(qid)
        return ItemSnapshot(labels=labels, descriptions=descriptions, aliases=aliases, claims=claims)
    except Exception as exc:  # noqa: BLE001 - degrade to "unknown existing state", never crash the plan
        logger.warning("Could not fetch existing snapshot for %s: %s", qid, exc)
        return None


def plan_entity_resolution(
    entities: list[OntologyEntity],
    property_mappings: dict[str, PropertyMapping],
    cache: EntityLookupCache,
    client: Optional[WikibaseClient],
    config: PipelineConfig,
) -> PlanResult:
    """Pass A: resolve every entity to a QID (existing or planned-new) and plan metadata changes."""
    identity_mapping = property_mappings.get(config.identity_property_key)
    can_remote_lookup = bool(
        client is not None and identity_mapping is not None and identity_mapping.pid and client.credentials.sparql_url
    )
    if identity_mapping is None or not identity_mapping.pid:
        logger.warning(
            "Identity property '%s' has no PID configured; remote duplicate detection is disabled "
            "and only the local cache will be used to avoid creating duplicate items.",
            config.identity_property_key,
        )

    # Batch remote identity lookups for every entity the local cache doesn't already
    # know about, instead of issuing one SPARQL round-trip per entity. On a first run
    # over hundreds/thousands of entities, one-query-per-entity is impractically slow
    # against a real endpoint; batching by config.batch_size keeps traffic to roughly
    # len(uncached)/batch_size requests (see README, "Traffic Control and Reliability").
    remote_matches: dict[str, list[str]] = {}
    if can_remote_lookup:
        uncached_ids = [e.canonical_id for e in entities if cache.get(e.canonical_id) is None]
        batch_size = max(1, config.batch_size)
        for batch_start in range(0, len(uncached_ids), batch_size):
            batch = uncached_ids[batch_start : batch_start + batch_size]
            remote_matches.update(client.find_qids_by_identity_values_batch(identity_mapping.pid, batch))
        if uncached_ids:
            logger.info(
                "Resolved %d cache-miss entities against Wikibase using %d batched SPARQL queries (batch_size=%d).",
                len(uncached_ids),
                -(-len(uncached_ids) // batch_size),
                batch_size,
            )

    resolved: dict[str, ResolvedEntity] = {}
    actions: list[PlannedAction] = []
    snapshots: dict[str, ItemSnapshot] = {}

    for entity in entities:
        label = entity.primary_label(config.default_language) or entity.local_name
        cached_entry = cache.get(entity.canonical_id)

        qid: Optional[str] = None
        is_new = False
        source = "unresolved"

        if cached_entry:
            qid = cached_entry["qid"]
            source = "cache"
        elif can_remote_lookup:
            matches = remote_matches.get(entity.canonical_id, [])
            if len(matches) == 1:
                qid = matches[0]
                source = "wikibase_lookup"
            elif len(matches) > 1:
                actions.append(
                    PlannedAction(
                        action=ActionType.CONFLICT.value,
                        entity_type=entity.entity_type,
                        source_identifier=entity.canonical_id,
                        qid="",
                        label=label,
                        reason=f"{len(matches)} Wikibase items already have identity value '{entity.canonical_id}'",
                        severity=Severity.BLOCKING.value,
                    )
                )
                resolved[entity.canonical_id] = ResolvedEntity(entity.canonical_id, None, False, "conflict")
                continue
            else:
                is_new = True
        else:
            is_new = True

        if qid is None and not is_new:
            is_new = True

        if is_new:
            actions.append(
                PlannedAction(
                    action=ActionType.CREATE_ITEM.value,
                    entity_type=entity.entity_type,
                    source_identifier=entity.canonical_id,
                    qid=NEW_ITEM_MARKER,
                    label=label,
                    reason="no existing Wikibase item found" if can_remote_lookup or cached_entry else
                    "no local cache entry and remote duplicate detection unavailable; will create new item",
                    severity=Severity.INFO.value
                    if (can_remote_lookup or cached_entry is not None)
                    else Severity.WARNING.value,
                )
            )
            resolved[entity.canonical_id] = ResolvedEntity(entity.canonical_id, None, True, "new")
            snapshot = ItemSnapshot()
        else:
            resolved[entity.canonical_id] = ResolvedEntity(entity.canonical_id, qid, False, source)
            snapshot = snapshots.get(qid)
            if snapshot is None:
                snapshot = _fetch_snapshot(client, qid) if client is not None else None
                if snapshot is None:
                    snapshot = ItemSnapshot()
                    actions.append(
                        PlannedAction(
                            action=ActionType.SKIP_UNCHANGED.value,
                            entity_type=entity.entity_type,
                            source_identifier=entity.canonical_id,
                            qid=qid,
                            label=label,
                            reason="existing item state could not be verified (no Wikibase connection); "
                            "metadata/statement changes cannot be safely diffed this run",
                            severity=Severity.WARNING.value,
                        )
                    )
                snapshots[qid] = snapshot

        display_qid = NEW_ITEM_MARKER if is_new else qid
        _plan_metadata_actions(entity, snapshot, display_qid, label, config, actions)

    return PlanResult(resolved_entities=resolved, actions=actions, snapshots=snapshots)


def _plan_metadata_actions(
    entity: OntologyEntity,
    snapshot: ItemSnapshot,
    display_qid: str,
    label: str,
    config: PipelineConfig,
    actions: list[PlannedAction],
) -> None:
    if config.update_labels:
        for language, text in entity.labels.items():
            existing = snapshot.labels.get(language)
            normalized_new = " ".join(text.split())
            if existing is None:
                actions.append(
                    PlannedAction(
                        action=ActionType.UPDATE_LABEL.value,
                        entity_type=entity.entity_type,
                        source_identifier=entity.canonical_id,
                        qid=display_qid,
                        label=label,
                        property_name=f"label@{language}",
                        old_value="",
                        new_value=text,
                        reason="label missing for this language",
                        severity=Severity.INFO.value,
                    )
                )
            elif " ".join(existing.split()) != normalized_new:
                actions.append(
                    PlannedAction(
                        action=ActionType.UPDATE_LABEL.value,
                        entity_type=entity.entity_type,
                        source_identifier=entity.canonical_id,
                        qid=display_qid,
                        label=label,
                        property_name=f"label@{language}",
                        old_value=existing,
                        new_value=text,
                        reason="label differs from OWL source",
                        severity=Severity.INFO.value,
                    )
                )

    if config.update_descriptions:
        for language, text in entity.descriptions.items():
            existing = snapshot.descriptions.get(language)
            normalized_new = " ".join(text.split())
            if existing is None:
                actions.append(
                    PlannedAction(
                        action=ActionType.ADD_DESCRIPTION.value,
                        entity_type=entity.entity_type,
                        source_identifier=entity.canonical_id,
                        qid=display_qid,
                        label=label,
                        property_name=f"description@{language}",
                        old_value="",
                        new_value=text,
                        reason="description missing for this language",
                        severity=Severity.INFO.value,
                    )
                )
            elif " ".join(existing.split()) != normalized_new:
                actions.append(
                    PlannedAction(
                        action=ActionType.UPDATE_DESCRIPTION.value,
                        entity_type=entity.entity_type,
                        source_identifier=entity.canonical_id,
                        qid=display_qid,
                        label=label,
                        property_name=f"description@{language}",
                        old_value=existing,
                        new_value=text,
                        reason="description differs from OWL source",
                        severity=Severity.INFO.value,
                    )
                )

    if config.update_aliases:
        for language, values in entity.aliases.items():
            existing_aliases = {" ".join(v.split()) for v in snapshot.aliases.get(language, [])}
            for value in values:
                normalized_value = " ".join(value.split())
                if normalized_value in existing_aliases:
                    continue
                actions.append(
                    PlannedAction(
                        action=ActionType.ADD_ALIAS.value,
                        entity_type=entity.entity_type,
                        source_identifier=entity.canonical_id,
                        qid=display_qid,
                        label=label,
                        property_name=f"alias@{language}",
                        old_value="",
                        new_value=value,
                        reason="alias not present on existing item",
                        severity=Severity.INFO.value,
                    )
                )


def _identity_statement(entity: OntologyEntity) -> OntologyStatement:
    return OntologyStatement(
        subject_canonical_id=entity.canonical_id,
        predicate_original="ontology_iri",
        predicate_local_name="ontology_iri",
        value_kind="literal",
        value=entity.canonical_id,
        literal_datatype=None,
        language=None,
    )


def plan_statements(
    entities: list[OntologyEntity],
    property_mappings: dict[str, PropertyMapping],
    plan: PlanResult,
    config: PipelineConfig,
) -> None:
    """Pass B: append statement-related :class:`PlannedAction` rows onto ``plan.actions`` in place.

    Must run after :func:`plan_entity_resolution` so every entity already has
    a resolved (real-or-placeholder) QID in ``plan.resolved_entities``.
    """
    if not config.update_statements:
        return

    identity_mapping = property_mappings.get(config.identity_property_key)

    for entity in entities:
        resolved_subject = plan.resolved_entities.get(entity.canonical_id)
        if resolved_subject is None or (resolved_subject.qid is None and not resolved_subject.is_new):
            continue  # unresolved/conflicting subject already reported as CONFLICT in Pass A

        subject_qid = NEW_ITEM_MARKER if resolved_subject.is_new else resolved_subject.qid
        label = entity.primary_label(config.default_language) or entity.local_name
        snapshot = plan.snapshots.get(subject_qid, ItemSnapshot())

        statements = list(entity.statements)
        if identity_mapping is not None and identity_mapping.is_configured:
            statements.append(_identity_statement(entity))

        for statement in statements:
            map_key = resolve_property_map_key(statement)
            mapping = property_mappings.get(map_key)

            if mapping is None or not mapping.is_configured:
                actions_severity = Severity.BLOCKING.value if config.stop_on_unresolved_property else Severity.WARNING.value
                plan.actions.append(
                    PlannedAction(
                        action=ActionType.SKIP_UNMAPPED_PROPERTY.value,
                        entity_type=entity.entity_type,
                        source_identifier=entity.canonical_id,
                        qid=subject_qid,
                        label=label,
                        property_name=map_key,
                        new_value=statement.value,
                        reason=f"predicate '{map_key}' has no configured PID/datatype in PROPERTY_MAP",
                        severity=actions_severity,
                    )
                )
                continue

            # A resource-valued OWL statement only needs to resolve against our own
            # entities when it is destined for a wikibase-item property (a link to
            # another Wikibase item). When it is mapped to url/string/external-id, the
            # resource's URI is written verbatim as text -- e.g. some `wikidata_uri`
            # triples in this ontology use `rdf:resource="https://.../Q123"` instead of
            # a literal, and that external Wikidata URI is not one of our own entities
            # and must never be looked up as one.
            resolved_qid_for_value: Optional[str] = None
            if statement.value_kind == "resource" and mapping.wikibase_datatype == "wikibase-item":
                target = plan.resolved_entities.get(statement.value)
                if target is None or (target.qid is None and not target.is_new):
                    plan.actions.append(
                        PlannedAction(
                            action=ActionType.UNRESOLVED_OBJECT.value,
                            entity_type=entity.entity_type,
                            source_identifier=entity.canonical_id,
                            qid=subject_qid,
                            label=label,
                            pid=mapping.pid or "",
                            property_name=map_key,
                            new_value=statement.value,
                            reason=f"object '{statement.value}' did not resolve to a known/plannable Wikibase item",
                            severity=Severity.WARNING.value,
                        )
                    )
                    continue
                resolved_qid_for_value = NEW_ITEM_MARKER if target.is_new else target.qid

            target_is_pending_new_item = mapping.wikibase_datatype == "wikibase-item" and resolved_qid_for_value == NEW_ITEM_MARKER

            if target_is_pending_new_item:
                # The object item does not exist yet (it will be created earlier in this
                # same run/live-execution). Its real QID is unknown during planning, so
                # datavalue construction (which requires a real "Qxxx" id) is deferred to
                # execution time; the plan simply records the intended link.
                comparable_value = NEW_ITEM_MARKER
            else:
                conversion = convert_owl_value_to_wikibase(
                    value_kind=statement.value_kind,
                    raw_value=statement.value,
                    wikibase_datatype=mapping.wikibase_datatype,
                    literal_datatype=statement.literal_datatype,
                    language=statement.language,
                    default_language=config.default_language,
                    resolved_qid=resolved_qid_for_value,
                )
                if not conversion.success:
                    plan.actions.append(
                        PlannedAction(
                            action=ActionType.ERROR.value,
                            entity_type=entity.entity_type,
                            source_identifier=entity.canonical_id,
                            qid=subject_qid,
                            label=label,
                            pid=mapping.pid or "",
                            property_name=map_key,
                            new_value=statement.value,
                            reason=f"conversion failed: {conversion.error}",
                            severity=Severity.ERROR.value,
                        )
                    )
                    continue
                comparable_value = conversion.comparable_value
            existing_values = {
                normalize_value_for_comparison(mapping.wikibase_datatype, v)
                for v in snapshot.claims.get(mapping.pid, [])
            }

            target_identifier = (
                statement.value if statement.value_kind == "resource" and mapping.wikibase_datatype == "wikibase-item" else ""
            )

            if resolved_subject.is_new or resolved_qid_for_value == NEW_ITEM_MARKER or comparable_value not in existing_values:
                plan.actions.append(
                    PlannedAction(
                        action=ActionType.ADD_STATEMENT.value,
                        entity_type=entity.entity_type,
                        source_identifier=entity.canonical_id,
                        qid=subject_qid,
                        label=label,
                        pid=mapping.pid or "",
                        property_name=map_key,
                        new_value=comparable_value,
                        reason="statement not present on existing item" if not resolved_subject.is_new else "new item",
                        severity=Severity.INFO.value,
                        target_source_identifier=target_identifier,
                        literal_datatype=statement.literal_datatype or "",
                        language=statement.language or "",
                    )
                )
            else:
                plan.actions.append(
                    PlannedAction(
                        action=ActionType.SKIP_UNCHANGED.value,
                        entity_type=entity.entity_type,
                        source_identifier=entity.canonical_id,
                        qid=subject_qid,
                        label=label,
                        pid=mapping.pid or "",
                        property_name=map_key,
                        new_value=comparable_value,
                        reason="statement already present with the same value",
                        severity=Severity.INFO.value,
                        target_source_identifier=target_identifier,
                        literal_datatype=statement.literal_datatype or "",
                        language=statement.language or "",
                    )
                )


def build_synchronization_plan(
    entities: list[OntologyEntity],
    property_mappings: dict[str, PropertyMapping],
    cache: EntityLookupCache,
    client: Optional[WikibaseClient],
    config: PipelineConfig,
) -> PlanResult:
    """Run Pass A followed by Pass B and return the combined plan."""
    plan = plan_entity_resolution(entities, property_mappings, cache, client, config)
    plan_statements(entities, property_mappings, plan, config)
    return plan
