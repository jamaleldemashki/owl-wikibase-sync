"""
Report generation: ontology statistics, dry-run plans, validation and
synchronization reports. All writers are pure functions over already-computed
in-memory data structures -- no parsing or Wikibase logic lives here.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from rdflib import BNode, Graph
from rdflib.namespace import OWL, RDF, RDFS

from .models import OntologyEntity, PlannedAction, PropertyMapping, SynchronizationResult, ValidationFinding
from .ontology_parser import DEFAULT_DESCRIPTION_PREDICATES, OWL_META_TYPES, get_local_name_from_term


def save_json_report(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True, default=str)
        handle.write("\n")


def save_csv_report(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def compute_ontology_statistics(
    graph: Graph,
    entities: list[OntologyEntity],
    predicate_usage: dict[str, dict],
    property_mappings: dict[str, PropertyMapping],
    alias_local_names: frozenset[str],
) -> dict[str, Any]:
    """Compute the full statistics set required by Section 7 of the notebook."""
    subjects = set(graph.subjects())
    predicates = set(graph.predicates())
    objects = set(graph.objects())
    blank_nodes = {t for t in subjects | objects if isinstance(t, BNode)}

    label_triples = list(graph.subject_objects(RDFS.label))
    description_triples: list[tuple] = []
    for predicate in DEFAULT_DESCRIPTION_PREDICATES:
        description_triples.extend(graph.subject_objects(predicate))
    alias_triples: list[tuple] = []
    for predicate in predicates:
        if get_local_name_from_term(predicate).lower() in alias_local_names:
            alias_triples.extend(graph.subject_objects(predicate))

    subclass_triples = list(graph.subject_objects(RDFS.subClassOf))
    instance_of_triples = [
        (s, o) for s, o in graph.subject_objects(RDF.type) if o not in OWL_META_TYPES
    ]

    literal_statement_count = sum(1 for e in entities for s in e.statements if s.value_kind == "literal")
    resource_statement_count = sum(1 for e in entities for s in e.statements if s.value_kind == "resource")
    blank_node_statement_count = sum(1 for e in entities for s in e.statements if s.value_kind == "blank_node")

    entities_without_labels = [e.canonical_id for e in entities if not e.labels]

    language_counter: Counter = Counter()
    label_by_language: dict[str, Counter] = {}
    for entity in entities:
        for language, text in entity.labels.items():
            language_counter[language] += 1
            label_by_language.setdefault(language, Counter())[text] += 1

    duplicate_labels = {
        language: {label: count for label, count in counter.items() if count > 1}
        for language, counter in label_by_language.items()
    }
    duplicate_labels = {language: labels for language, labels in duplicate_labels.items() if labels}

    discovered_predicates = set(predicate_usage.keys())
    configured_predicates = set(property_mappings.keys())
    unmapped_predicates = sorted(discovered_predicates - configured_predicates)

    return {
        "total_triples": len(graph),
        "unique_subjects": len(subjects),
        "unique_predicates": len(predicates),
        "unique_objects": len(objects),
        "owl_classes": len(set(graph.subjects(RDF.type, OWL.Class))),
        "named_individuals": len(set(graph.subjects(RDF.type, OWL.NamedIndividual))),
        "object_properties": len(set(graph.subjects(RDF.type, OWL.ObjectProperty))),
        "datatype_properties": len(set(graph.subjects(RDF.type, OWL.DatatypeProperty))),
        "annotation_properties": len(set(graph.subjects(RDF.type, OWL.AnnotationProperty))),
        "blank_nodes": len(blank_nodes),
        "labels": len(label_triples),
        "descriptions": len(description_triples),
        "aliases": len(alias_triples),
        "subclass_statements": len(subclass_triples),
        "instance_of_statements": len(instance_of_triples),
        "literal_valued_statements": literal_statement_count,
        "resource_valued_statements": resource_statement_count,
        "blank_node_valued_statements": blank_node_statement_count,
        "unmapped_predicates": unmapped_predicates,
        "unmapped_predicate_count": len(unmapped_predicates),
        "entities_without_labels": len(entities_without_labels),
        "entities_without_labels_sample": entities_without_labels[:25],
        "duplicate_labels": duplicate_labels,
        "language_distribution": dict(language_counter),
        "total_entities_parsed": len(entities),
    }


def actions_to_rows(actions: list[PlannedAction]) -> list[dict[str, Any]]:
    return [action.as_dict() for action in actions]


PLAN_CSV_FIELDNAMES = [
    "action",
    "entity_type",
    "source_identifier",
    "qid",
    "label",
    "pid",
    "property_name",
    "old_value",
    "new_value",
    "reason",
    "severity",
    "target_source_identifier",
]


def save_dry_run_plan(report_dir: Path, actions: list[PlannedAction]) -> dict[str, Any]:
    rows = actions_to_rows(actions)
    action_counts = Counter(row["action"] for row in rows)
    severity_counts = Counter(row["severity"] for row in rows)
    payload = {
        "action_counts": dict(action_counts),
        "severity_counts": dict(severity_counts),
        "total_actions": len(rows),
        "actions": rows,
    }
    save_json_report(Path(report_dir) / "dry_run_plan.json", payload)
    save_csv_report(Path(report_dir) / "dry_run_plan.csv", rows, PLAN_CSV_FIELDNAMES)
    return payload


def save_validation_report(report_dir: Path, findings: list[ValidationFinding], summary: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "summary": summary,
        "findings": [f.as_dict() for f in findings],
    }
    save_json_report(Path(report_dir) / "validation_report.json", payload)
    return payload


def save_synchronization_report(report_dir: Path, result: SynchronizationResult, actions: list[PlannedAction]) -> dict[str, Any]:
    payload = {
        "result": result.as_dict(),
        "action_counts": dict(Counter(a.action for a in actions)),
    }
    save_json_report(Path(report_dir) / "synchronization_report.json", payload)
    return payload


def save_unresolved_resources(report_dir: Path, actions: list[PlannedAction]) -> list[dict[str, Any]]:
    unresolved_action_types = {"UNRESOLVED_OBJECT", "CONFLICT", "ERROR", "SKIP_UNMAPPED_PROPERTY"}
    rows = [action.as_dict() for action in actions if action.action in unresolved_action_types]
    save_csv_report(Path(report_dir) / "unresolved_resources.csv", rows, PLAN_CSV_FIELDNAMES)
    return rows


def save_ontology_statistics(report_dir: Path, statistics: dict[str, Any]) -> None:
    save_json_report(Path(report_dir) / "ontology_statistics.json", statistics)
    flat_rows = [{"metric": key, "value": json.dumps(value) if isinstance(value, (dict, list)) else value}
                 for key, value in statistics.items()]
    save_csv_report(Path(report_dir) / "ontology_statistics.csv", flat_rows, ["metric", "value"])
