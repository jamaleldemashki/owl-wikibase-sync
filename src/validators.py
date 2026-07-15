"""
Preflight validation: property-mapping checks and ontology data-quality checks.

Everything here is pure inspection -- no Wikibase network calls, no mutation
of the parsed ontology. Findings are classified as INFO / WARNING / ERROR /
BLOCKING (see :class:`~src.models.Severity`) so the notebook can decide,
according to ``STOP_ON_VALIDATION_ERROR`` / ``STOP_ON_UNRESOLVED_PROPERTY``,
whether to proceed to live synchronization.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Iterable

from .identifiers import is_probably_malformed_uri
from .models import OntologyEntity, PropertyMapping, Severity, ValidationFinding
from .ontology_parser import resolve_property_map_key

_URI_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://\S+$")

RESOURCE_COMPATIBLE_DATATYPES = frozenset({"wikibase-item", "url", "string", "external-id"})


# ---------------------------------------------------------------------------
# Property-mapping validation
# ---------------------------------------------------------------------------


def collect_predicate_usage(entities: Iterable[OntologyEntity]) -> dict[str, dict]:
    """Summarize how each PROPERTY_MAP-keyed predicate is actually used in the OWL file."""
    usage: dict[str, dict] = {}
    for entity in entities:
        for statement in entity.statements:
            key = resolve_property_map_key(statement)
            bucket = usage.setdefault(
                key, {"count": 0, "value_kinds": set(), "predicate_local_name": statement.predicate_local_name}
            )
            bucket["count"] += 1
            bucket["value_kinds"].add(statement.value_kind)
    return usage


def validate_property_map(
    property_mappings: dict[str, PropertyMapping],
    predicate_usage: dict[str, dict],
) -> tuple[list[ValidationFinding], dict]:
    """Validate the user-maintained PROPERTY_MAP against the OWL file's actual usage.

    Returns a list of findings plus a summary dict with the counts requested
    by the project specification (discovered / mapped / unmapped / duplicate
    PIDs / missing datatypes / incompatible datatypes / unused mappings).
    """
    findings: list[ValidationFinding] = []

    discovered = set(predicate_usage.keys())
    configured = set(property_mappings.keys())

    mapped = sorted(discovered & configured)
    unmapped = sorted(discovered - configured)
    unused_mappings = sorted(configured - discovered)

    for key in unmapped:
        usage = predicate_usage[key]
        findings.append(
            ValidationFinding(
                check="unmapped_predicate",
                severity=Severity.WARNING.value,
                message=(
                    f"Predicate '{key}' is used {usage['count']} time(s) in the OWL file "
                    f"but has no PROPERTY_MAP entry."
                ),
                source_identifier=key,
                details={"count": usage["count"], "value_kinds": sorted(usage["value_kinds"])},
            )
        )

    for key in unused_mappings:
        findings.append(
            ValidationFinding(
                check="unused_property_mapping",
                severity=Severity.INFO.value,
                message=f"PROPERTY_MAP entry '{key}' is configured but not used by any statement in the OWL file.",
                source_identifier=key,
            )
        )

    pid_to_keys: dict[str, list[str]] = defaultdict(list)
    for key, mapping in property_mappings.items():
        if mapping.pid:
            pid_to_keys[mapping.pid].append(key)
    duplicate_pids = {pid: keys for pid, keys in pid_to_keys.items() if len(keys) > 1}
    for pid, keys in duplicate_pids.items():
        findings.append(
            ValidationFinding(
                check="duplicate_pid_assignment",
                severity=Severity.BLOCKING.value,
                message=f"PID {pid} is assigned to multiple PROPERTY_MAP entries: {', '.join(keys)}.",
                details={"pid": pid, "keys": keys},
            )
        )

    missing_pid_but_used: list[str] = []
    missing_datatype: list[str] = []
    for key in mapped:
        mapping = property_mappings[key]
        if not mapping.pid:
            missing_pid_but_used.append(key)
            findings.append(
                ValidationFinding(
                    check="missing_pid",
                    severity=Severity.ERROR.value,
                    message=f"Predicate '{key}' is used in the OWL file but PROPERTY_MAP['{key}']['pid'] is None.",
                    source_identifier=key,
                )
            )
        if not mapping.wikibase_datatype:
            missing_datatype.append(key)
            findings.append(
                ValidationFinding(
                    check="missing_datatype",
                    severity=Severity.ERROR.value,
                    message=f"PROPERTY_MAP['{key}'] has no wikibase_datatype configured.",
                    source_identifier=key,
                )
            )

    incompatible: list[str] = []
    for key in mapped:
        mapping = property_mappings[key]
        if not mapping.wikibase_datatype:
            continue
        usage = predicate_usage[key]
        value_kinds = usage["value_kinds"]
        if "resource" in value_kinds and mapping.wikibase_datatype not in RESOURCE_COMPATIBLE_DATATYPES:
            incompatible.append(key)
            findings.append(
                ValidationFinding(
                    check="incompatible_datatype",
                    severity=Severity.ERROR.value,
                    message=(
                        f"Predicate '{key}' has resource-valued statements in the OWL file, "
                        f"but is mapped to wikibase_datatype='{mapping.wikibase_datatype}', "
                        f"which cannot represent a resource reference."
                    ),
                    source_identifier=key,
                )
            )
        if value_kinds == {"literal"} and mapping.wikibase_datatype == "wikibase-item":
            incompatible.append(key)
            findings.append(
                ValidationFinding(
                    check="incompatible_datatype",
                    severity=Severity.ERROR.value,
                    message=(
                        f"Predicate '{key}' only has literal-valued statements in the OWL file, "
                        f"but is mapped to wikibase_datatype='wikibase-item'."
                    ),
                    source_identifier=key,
                )
            )
        if "blank_node" in value_kinds:
            findings.append(
                ValidationFinding(
                    check="blank_node_statement",
                    severity=Severity.WARNING.value,
                    message=f"Predicate '{key}' has blank-node-valued statements, which cannot become Wikibase items.",
                    source_identifier=key,
                )
            )

    summary = {
        "discovered_predicates": sorted(discovered),
        "mapped_predicates": mapped,
        "unmapped_predicates": unmapped,
        "unused_mappings": unused_mappings,
        "duplicate_pid_assignments": duplicate_pids,
        "predicates_missing_pid": missing_pid_but_used,
        "predicates_missing_datatype": missing_datatype,
        "incompatible_datatype_predicates": sorted(set(incompatible)),
    }
    return findings, summary


# ---------------------------------------------------------------------------
# Ontology data-quality validation
# ---------------------------------------------------------------------------


def validate_ontology_quality(
    entities: list[OntologyEntity],
    property_mappings: dict[str, PropertyMapping],
    default_language: str,
    supported_languages: Iterable[str],
) -> list[ValidationFinding]:
    """Run preflight data-quality checks over the parsed ontology entities."""
    findings: list[ValidationFinding] = []
    supported = set(supported_languages)

    canonical_id_counts = Counter(entity.canonical_id for entity in entities)
    known_canonical_ids = set(canonical_id_counts.keys())

    for canonical_id, count in canonical_id_counts.items():
        if count > 1:
            findings.append(
                ValidationFinding(
                    check="duplicate_canonical_id",
                    severity=Severity.BLOCKING.value,
                    message=f"Canonical id '{canonical_id}' resolves to {count} distinct OWL resources.",
                    source_identifier=canonical_id,
                )
            )

    labels_by_language: dict[str, Counter] = defaultdict(Counter)

    for entity in entities:
        if not entity.canonical_id:
            findings.append(
                ValidationFinding(
                    check="missing_identifier",
                    severity=Severity.BLOCKING.value,
                    message=f"Entity with original id '{entity.original_id}' has no canonical identifier.",
                    source_identifier=entity.original_id,
                )
            )

        if is_probably_malformed_uri(entity.original_id):
            findings.append(
                ValidationFinding(
                    check="malformed_uri",
                    severity=Severity.INFO.value,
                    message=(
                        f"Original identifier '{entity.original_id}' embeds more than one absolute URI; "
                        f"normalized to '{entity.canonical_id}'."
                    ),
                    source_identifier=entity.canonical_id,
                )
            )

        if not entity.labels:
            findings.append(
                ValidationFinding(
                    check="missing_label",
                    severity=Severity.WARNING.value,
                    message=f"Entity '{entity.canonical_id}' has no rdfs:label in any language.",
                    source_identifier=entity.canonical_id,
                )
            )
        else:
            for language, label_text in entity.labels.items():
                labels_by_language[language][label_text] += 1
                if language not in supported and language != default_language:
                    findings.append(
                        ValidationFinding(
                            check="unsupported_language_tag",
                            severity=Severity.INFO.value,
                            message=(
                                f"Entity '{entity.canonical_id}' has a label in language '{language}', "
                                f"which is not in SUPPORTED_LANGUAGES."
                            ),
                            source_identifier=entity.canonical_id,
                        )
                    )

        for statement in entity.statements:
            map_key = resolve_property_map_key(statement)
            mapping = property_mappings.get(map_key)

            if statement.value_kind == "blank_node":
                findings.append(
                    ValidationFinding(
                        check="blank_node_statement",
                        severity=Severity.WARNING.value,
                        message=f"Entity '{entity.canonical_id}' has a blank-node value for predicate '{map_key}'.",
                        source_identifier=entity.canonical_id,
                    )
                )
                continue

            # A resource-valued statement only needs to resolve against our own parsed
            # entities when it targets a wikibase-item property (a link to another
            # Wikibase item). When mapped to url/string/external-id -- or when no
            # mapping is configured yet -- the resource's URI is (or would be) written
            # verbatim as text, so an external resource (e.g. a raw Wikidata URI used
            # via `rdf:resource` instead of a literal) is expected and not an error.
            targets_wikibase_item = mapping is not None and mapping.wikibase_datatype == "wikibase-item"
            if statement.value_kind == "resource" and targets_wikibase_item and statement.value not in known_canonical_ids:
                findings.append(
                    ValidationFinding(
                        check="unresolved_object",
                        severity=Severity.WARNING.value,
                        message=(
                            f"Entity '{entity.canonical_id}' has a resource-valued statement on '{map_key}' "
                            f"pointing to '{statement.value}', which is not among the parsed classes/individuals."
                        ),
                        source_identifier=entity.canonical_id,
                        details={"predicate": map_key, "object": statement.value},
                    )
                )

            if statement.value_kind == "resource" and not _URI_RE.match(statement.value):
                findings.append(
                    ValidationFinding(
                        check="malformed_uri",
                        severity=Severity.WARNING.value,
                        message=f"Resource value '{statement.value}' for predicate '{map_key}' is not a well-formed URI.",
                        source_identifier=entity.canonical_id,
                    )
                )

            if (
                mapping is not None
                and mapping.wikibase_datatype == "url"
                and statement.value_kind == "literal"
                and not _URI_RE.match(statement.value.strip())
            ):
                findings.append(
                    ValidationFinding(
                        check="invalid_url_value",
                        severity=Severity.WARNING.value,
                        message=(
                            f"Value '{statement.value}' for predicate '{map_key}' is not a well-formed URL and will "
                            f"be reported as an ERROR in the synchronization plan rather than written to Wikibase "
                            f"(commonly a placeholder like 'None' left by the authoring tool when no value was set)."
                        ),
                        source_identifier=entity.canonical_id,
                        details={"predicate": map_key, "value": statement.value},
                    )
                )

            if mapping is None:
                findings.append(
                    ValidationFinding(
                        check="predicate_without_mapping",
                        severity=Severity.WARNING.value,
                        message=f"Entity '{entity.canonical_id}' uses predicate '{map_key}', which has no PROPERTY_MAP entry.",
                        source_identifier=entity.canonical_id,
                        details={"predicate": map_key},
                    )
                )
            elif mapping.wikibase_datatype == "time" and statement.value_kind == "literal":
                if not re.match(r"^[+-]?\d{1,9}(-\d{2}(-\d{2})?)?", statement.value.strip()):
                    findings.append(
                        ValidationFinding(
                            check="invalid_time_value",
                            severity=Severity.ERROR.value,
                            message=f"Value '{statement.value}' for predicate '{map_key}' cannot be parsed as a time.",
                            source_identifier=entity.canonical_id,
                        )
                    )
            elif mapping.wikibase_datatype == "quantity" and statement.value_kind == "literal":
                if not re.match(r"^[+-]?\d+(\.\d+)?", statement.value.strip()):
                    findings.append(
                        ValidationFinding(
                            check="invalid_quantity_value",
                            severity=Severity.ERROR.value,
                            message=f"Value '{statement.value}' for predicate '{map_key}' cannot be parsed as a quantity.",
                            source_identifier=entity.canonical_id,
                        )
                    )

    for language, counter in labels_by_language.items():
        for label_text, count in counter.items():
            if count > 1:
                findings.append(
                    ValidationFinding(
                        check="duplicate_label",
                        severity=Severity.INFO.value,
                        message=f"Label '{label_text}' ({language}) is used by {count} different entities.",
                        details={"language": language, "label": label_text, "count": count},
                    )
                )

    return findings


def summarize_findings(findings: list[ValidationFinding]) -> dict[str, int]:
    """Count findings per severity level."""
    counts = {severity.value: 0 for severity in Severity}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


def has_blocking_findings(findings: list[ValidationFinding]) -> bool:
    return any(finding.severity == Severity.BLOCKING.value for finding in findings)


# ---------------------------------------------------------------------------
# Post-synchronization validation
# ---------------------------------------------------------------------------


def validate_post_synchronization(entities, cache, plan_actions, verify_with_client=None) -> list[ValidationFinding]:
    """Verify pipeline invariants after a live synchronization run.

    Checks: every entity has a cached QID, no two canonical ids share a QID,
    and every CONFLICT/UNRESOLVED_OBJECT/ERROR/SKIP_UNMAPPED_PROPERTY action
    from the executed plan is surfaced as a finding rather than silently
    dropped. If ``verify_with_client`` (a callable ``qid -> bool exists``) is
    supplied, cached QIDs are spot-checked against live Wikibase state.
    """
    findings: list[ValidationFinding] = []

    for entity in entities:
        cached_entry = cache.get(entity.canonical_id)
        if not cached_entry or not cached_entry.get("qid"):
            findings.append(
                ValidationFinding(
                    check="missing_qid_after_sync",
                    severity=Severity.ERROR.value,
                    message=f"Entity '{entity.canonical_id}' has no QID in the cache after synchronization.",
                    source_identifier=entity.canonical_id,
                )
            )

    for qid, canonical_ids in cache.find_duplicate_qids().items():
        findings.append(
            ValidationFinding(
                check="duplicate_qid_mapping",
                severity=Severity.BLOCKING.value,
                message=f"QID {qid} is mapped from {len(canonical_ids)} different canonical ids.",
                details={"qid": qid, "canonical_ids": canonical_ids},
            )
        )

    if verify_with_client is not None:
        for canonical_id, entry in cache.entities.items():
            if not verify_with_client(entry["qid"]):
                findings.append(
                    ValidationFinding(
                        check="cached_qid_not_found_live",
                        severity=Severity.ERROR.value,
                        message=f"Cached QID {entry['qid']} for '{canonical_id}' could not be found on Wikibase.",
                        source_identifier=canonical_id,
                    )
                )

    unresolved_action_types = {
        "UNRESOLVED_OBJECT": Severity.WARNING.value,
        "CONFLICT": Severity.BLOCKING.value,
        "ERROR": Severity.ERROR.value,
        "SKIP_UNMAPPED_PROPERTY": Severity.WARNING.value,
    }
    for action in plan_actions:
        if action.action in unresolved_action_types:
            findings.append(
                ValidationFinding(
                    check=f"unresolved_action_{action.action.lower()}",
                    severity=unresolved_action_types[action.action],
                    message=f"{action.action}: {action.reason}",
                    source_identifier=action.source_identifier,
                )
            )

    return findings
