"""
Typed internal data models used throughout the OWL-to-Wikibase pipeline.

These dataclasses form the boundary between the OWL/RDF world (rdflib) and
the Wikibase world (wikibaseintegrator). Every later stage of the pipeline
(planning, synchronization, validation, reporting) operates on these models
rather than on raw rdflib terms, so the rest of the codebase stays testable
without a live RDF graph or a live Wikibase instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EntityType(str, Enum):
    """Classification of an OWL/RDF resource discovered in the ontology."""

    OWL_CLASS = "OwlClass"
    NAMED_INDIVIDUAL = "NamedIndividual"
    OBJECT_PROPERTY = "ObjectProperty"
    DATATYPE_PROPERTY = "DatatypeProperty"
    ANNOTATION_PROPERTY = "AnnotationProperty"
    BLANK_NODE = "BlankNode"
    UNKNOWN = "Unknown"


class ActionType(str, Enum):
    """The kind of change a :class:`PlannedAction` represents."""

    CREATE_ITEM = "CREATE_ITEM"
    UPDATE_LABEL = "UPDATE_LABEL"
    ADD_DESCRIPTION = "ADD_DESCRIPTION"
    UPDATE_DESCRIPTION = "UPDATE_DESCRIPTION"
    ADD_ALIAS = "ADD_ALIAS"
    ADD_STATEMENT = "ADD_STATEMENT"
    UPDATE_STATEMENT = "UPDATE_STATEMENT"
    SKIP_UNCHANGED = "SKIP_UNCHANGED"
    SKIP_UNMAPPED_PROPERTY = "SKIP_UNMAPPED_PROPERTY"
    UNRESOLVED_OBJECT = "UNRESOLVED_OBJECT"
    CONFLICT = "CONFLICT"
    ERROR = "ERROR"


class Severity(str, Enum):
    """Severity classification used by validation findings and plan rows."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    BLOCKING = "BLOCKING"


NEW_ITEM_MARKER = "<NEW_ITEM>"
"""Placeholder QID used in dry-run output for items that do not exist yet."""


@dataclass
class OntologyStatement:
    """A single outgoing statement (predicate/value pair) for an entity.

    ``value_kind`` distinguishes resource-valued statements (the object is
    another OWL resource that must resolve to a Wikibase item or URL) from
    literal-valued statements (strings, dates, numbers, language-tagged
    text).
    """

    subject_canonical_id: str
    predicate_original: str
    predicate_local_name: str
    value_kind: str  # "resource" or "literal"
    value: str
    literal_datatype: Optional[str] = None
    language: Optional[str] = None
    statement_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject_canonical_id": self.subject_canonical_id,
            "predicate_original": self.predicate_original,
            "predicate_local_name": self.predicate_local_name,
            "value_kind": self.value_kind,
            "value": self.value,
            "literal_datatype": self.literal_datatype,
            "language": self.language,
            "statement_hash": self.statement_hash,
        }


@dataclass
class OntologyEntity:
    """Normalized representation of an OWL class or named individual."""

    canonical_id: str
    original_id: str
    entity_type: str
    local_name: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    statements: list[OntologyStatement] = field(default_factory=list)
    content_hash: str = ""

    def primary_label(self, default_language: str) -> Optional[str]:
        """Return the label in the default language, else any available label."""
        if default_language in self.labels:
            return self.labels[default_language]
        if self.labels:
            return next(iter(self.labels.values()))
        return None


@dataclass
class PropertyMapping:
    """One entry of the user-maintained ``PROPERTY_MAP`` configuration."""

    source_predicate: str
    pid: Optional[str]
    wikibase_datatype: Optional[str]
    role: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        return bool(self.pid) and bool(self.wikibase_datatype)


@dataclass
class ResolvedEntity:
    """An :class:`OntologyEntity` after identity resolution against Wikibase."""

    canonical_id: str
    qid: Optional[str]
    is_new: bool
    source: str  # "cache", "wikibase_lookup", "created", "unresolved"


@dataclass
class PlannedAction:
    """One row of the synchronization plan (used by both dry-run and live mode)."""

    action: str
    entity_type: str
    source_identifier: str
    qid: str
    label: str = ""
    pid: str = ""
    property_name: str = ""
    old_value: str = ""
    new_value: str = ""
    reason: str = ""
    severity: str = Severity.INFO.value
    target_source_identifier: str = ""
    """For ADD_STATEMENT rows on wikibase-item properties: the canonical id of
    the object entity, so the synchronizer can re-resolve its real QID at
    execution time even if it was newly created earlier in the same run."""
    literal_datatype: str = ""
    language: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "entity_type": self.entity_type,
            "source_identifier": self.source_identifier,
            "qid": self.qid,
            "label": self.label,
            "pid": self.pid,
            "property_name": self.property_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "reason": self.reason,
            "severity": self.severity,
            "target_source_identifier": self.target_source_identifier,
        }


@dataclass
class ValidationFinding:
    """One preflight or post-sync validation result."""

    check: str
    severity: str
    message: str
    source_identifier: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "message": self.message,
            "source_identifier": self.source_identifier,
            "details": self.details,
        }


@dataclass
class SynchronizationResult:
    """Aggregate counters describing the outcome of a synchronization run."""

    run_id: str = ""
    items_created: int = 0
    items_reused: int = 0
    labels_updated: int = 0
    descriptions_updated: int = 0
    aliases_added: int = 0
    statements_added: int = 0
    statements_skipped_unchanged: int = 0
    unresolved_entities: int = 0
    unresolved_properties: int = 0
    conflicts: int = 0
    errors: int = 0
    api_calls: int = 0
    retries: int = 0
    status: str = "not_started"

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)
