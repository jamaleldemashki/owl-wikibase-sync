"""
Live execution of a previously computed :class:`~src.sync_planner.PlanResult`.

This module is the *only* place (besides ``wikibase_client.py`` itself) that
performs Wikibase writes. It always operates on a plan built by
``sync_planner.build_synchronization_plan`` -- the same plan dry-run mode
displays -- so live execution can never diverge from what was shown to the
user (design decision #10).

Execution order mirrors the two-pass design: every ``CREATE_ITEM`` action
runs first (so every entity has a real QID), then metadata actions, then
statement actions (which may reference newly created QIDs).
"""

from __future__ import annotations

import logging
from typing import Optional

from .cache_manager import EntityLookupCache, SynchronizationState
from .config import PipelineConfig
from .datatype_conversion import convert_owl_value_to_wikibase
from .models import ActionType, OntologyEntity, PropertyMapping, SynchronizationResult
from .sync_planner import PlanResult
from .wikibase_client import WikibaseClient

logger = logging.getLogger("owl_wikibase_sync.synchronizer")


class DryRunGuardError(RuntimeError):
    """Raised if live execution is attempted while the pipeline is configured for dry-run."""


def execute_synchronization(
    plan: PlanResult,
    entities: list[OntologyEntity],
    property_mappings: dict[str, PropertyMapping],
    cache: EntityLookupCache,
    client: WikibaseClient,
    state: SynchronizationState,
    config: PipelineConfig,
    run_id: str,
    timestamp: str,
) -> SynchronizationResult:
    """Execute a synchronization plan against a live Wikibase instance.

    Every successful write is immediately followed by a cache/state save, so
    an interruption at any point leaves the cache consistent with whatever
    Wikibase state was actually achieved (see README, "Interruption Recovery").
    """
    if config.dry_run:
        raise DryRunGuardError("execute_synchronization() called while config.dry_run is True.")

    result = SynchronizationResult(run_id=run_id, status="running")
    entities_by_id = {entity.canonical_id: entity for entity in entities}
    qid_by_canonical_id: dict[str, str] = {
        canonical_id: resolved.qid
        for canonical_id, resolved in plan.resolved_entities.items()
        if resolved.qid
    }

    created_this_run = _execute_creations(plan, entities_by_id, cache, client, state, result, qid_by_canonical_id, timestamp)
    _execute_metadata_updates(plan, client, state, result, qid_by_canonical_id, created_this_run)
    _execute_statement_additions(plan, property_mappings, client, state, result, qid_by_canonical_id, config)

    for canonical_id, resolved in plan.resolved_entities.items():
        if not resolved.is_new and resolved.qid:
            result.items_reused += 1

    result.unresolved_entities = sum(
        1 for a in plan.actions if a.action in (ActionType.CONFLICT.value, ActionType.UNRESOLVED_OBJECT.value)
    )
    result.unresolved_properties = sum(1 for a in plan.actions if a.action == ActionType.SKIP_UNMAPPED_PROPERTY.value)
    result.conflicts = sum(1 for a in plan.actions if a.action == ActionType.CONFLICT.value)
    result.statements_skipped_unchanged += sum(
        1 for a in plan.actions if a.action == ActionType.SKIP_UNCHANGED.value and a.pid
    )

    traffic = client.get_traffic_stats()
    result.api_calls = traffic["api_calls"]
    result.retries = traffic["retries"]

    result.status = "completed" if result.errors == 0 else "completed_with_errors"
    state.mark_phase_complete("synchronization")
    state.finish(result.status)
    state.save()
    return result


def _execute_creations(plan, entities_by_id, cache, client, state, result, qid_by_canonical_id, timestamp) -> set[str]:
    """Create every planned new item and return the set of canonical ids created this run.

    Returned so :func:`_execute_metadata_updates` can skip re-applying
    labels/descriptions/aliases for these entities -- ``create_item`` already
    writes the entity's complete metadata in the same call, so a follow-up
    ``update_metadata`` call would be pure redundant traffic (and redundant
    traffic is exactly what surfaces bugs like an alias update crashing
    after an item's real data is already correct on Wikibase).
    """
    created_this_run: set[str] = set()
    for action in plan.actions:
        if action.action != ActionType.CREATE_ITEM.value:
            continue
        entity = entities_by_id.get(action.source_identifier)
        if entity is None:
            logger.error("CREATE_ITEM action referenced unknown entity %s", action.source_identifier)
            result.errors += 1
            continue
        try:
            qid = client.create_item(entity.labels, entity.descriptions, entity.aliases)
            qid_by_canonical_id[entity.canonical_id] = qid
            cache.set(
                canonical_id=entity.canonical_id,
                qid=qid,
                entity_type=entity.entity_type,
                source_identifier=entity.original_id,
                label=entity.primary_label("en") or entity.local_name,
                content_hash=entity.content_hash,
                synced_at=timestamp,
            )
            cache.save(last_updated=timestamp)
            state.mark_entity_processed(entity.canonical_id)
            state.save()
            result.items_created += 1
            created_this_run.add(entity.canonical_id)
            logger.info("Created item %s for %s", qid, entity.canonical_id)
        except Exception as exc:  # noqa: BLE001 - logged and recorded, never silently dropped
            logger.error("Failed to create item for %s: %s", entity.canonical_id, exc)
            result.errors += 1
            state.record_failed_action({"action": "CREATE_ITEM", "source_identifier": entity.canonical_id, "error": str(exc)})
            state.save()

    return created_this_run


def _execute_metadata_updates(plan, client, state, result, qid_by_canonical_id, created_this_run):
    metadata_action_types = {
        ActionType.UPDATE_LABEL.value,
        ActionType.ADD_DESCRIPTION.value,
        ActionType.UPDATE_DESCRIPTION.value,
        ActionType.ADD_ALIAS.value,
    }
    by_entity: dict[str, list] = {}
    for action in plan.actions:
        if action.action in metadata_action_types and action.source_identifier not in created_this_run:
            by_entity.setdefault(action.source_identifier, []).append(action)

    for canonical_id, actions in by_entity.items():
        qid = qid_by_canonical_id.get(canonical_id)
        if not qid:
            logger.error("Cannot apply metadata updates for %s: no resolved QID", canonical_id)
            result.errors += 1
            continue

        labels: dict[str, str] = {}
        descriptions: dict[str, str] = {}
        aliases: dict[str, list[str]] = {}
        for action in actions:
            field_name, _, language = action.property_name.partition("@")
            if action.action == ActionType.UPDATE_LABEL.value:
                labels[language] = action.new_value
            elif action.action in (ActionType.ADD_DESCRIPTION.value, ActionType.UPDATE_DESCRIPTION.value):
                descriptions[language] = action.new_value
            elif action.action == ActionType.ADD_ALIAS.value:
                aliases.setdefault(language, []).append(action.new_value)

        try:
            client.update_metadata(qid, labels, descriptions, aliases)
            result.labels_updated += len(labels)
            result.descriptions_updated += len(descriptions)
            result.aliases_added += sum(len(v) for v in aliases.values())
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to update metadata for %s (%s): %s", canonical_id, qid, exc)
            result.errors += 1
            state.record_failed_action({"action": "UPDATE_METADATA", "source_identifier": canonical_id, "error": str(exc)})
            state.save()


def _execute_statement_additions(plan, property_mappings, client, state, result, qid_by_canonical_id, config):
    for action in plan.actions:
        if action.action != ActionType.ADD_STATEMENT.value:
            continue

        mapping = property_mappings.get(action.property_name)
        if mapping is None or not mapping.is_configured:
            logger.error("ADD_STATEMENT action referenced unmapped property %s", action.property_name)
            result.errors += 1
            continue

        subject_qid = qid_by_canonical_id.get(action.source_identifier)
        if not subject_qid:
            logger.error("Cannot add statement for %s: subject has no resolved QID", action.source_identifier)
            result.errors += 1
            continue

        if mapping.wikibase_datatype == "wikibase-item":
            target_qid = qid_by_canonical_id.get(action.target_source_identifier)
            if not target_qid:
                logger.warning(
                    "Skipping statement %s -%s-> %s: object has no resolved QID",
                    action.source_identifier,
                    action.property_name,
                    action.target_source_identifier,
                )
                result.unresolved_entities += 1
                continue
            conversion = convert_owl_value_to_wikibase(
                value_kind="resource",
                raw_value=action.target_source_identifier,
                wikibase_datatype="wikibase-item",
                resolved_qid=target_qid,
            )
        else:
            conversion = convert_owl_value_to_wikibase(
                value_kind="literal",
                raw_value=action.new_value,
                wikibase_datatype=mapping.wikibase_datatype,
                literal_datatype=action.literal_datatype or None,
                language=action.language or None,
                default_language=config.default_language,
            )

        if not conversion.success:
            logger.error("Cannot convert value for statement %s: %s", action.property_name, conversion.error)
            result.errors += 1
            state.record_failed_action(
                {"action": "ADD_STATEMENT", "source_identifier": action.source_identifier, "error": conversion.error}
            )
            state.save()
            continue

        try:
            client.add_claim(subject_qid, mapping.pid, mapping.wikibase_datatype, conversion.datavalue)
            result.statements_added += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to add claim %s on %s: %s", mapping.pid, subject_qid, exc)
            result.errors += 1
            state.record_failed_action(
                {"action": "ADD_STATEMENT", "source_identifier": action.source_identifier, "error": str(exc)}
            )
            state.save()
