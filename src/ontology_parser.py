"""
OWL/RDF parsing and normalization layer.

This module turns an ``rdflib.Graph`` (the authoritative source of truth for
the whole pipeline -- see design decision #1 in the README) into the typed
:class:`~src.models.OntologyEntity` / :class:`~src.models.OntologyStatement`
objects that every later stage (statistics, validation, planning,
synchronization) consumes. No Wikibase-specific logic lives here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from .hashing import compute_entity_hash, compute_statement_hash
from .identifiers import get_local_name, normalize_resource_identifier
from .models import EntityType, OntologyEntity, OntologyStatement

logger = logging.getLogger("owl_wikibase_sync.ontology_parser")

DC_DESCRIPTION = URIRef("http://purl.org/dc/elements/1.1/description")
DCTERMS_DESCRIPTION = URIRef("http://purl.org/dc/terms/description")

DEFAULT_DESCRIPTION_PREDICATES: frozenset[URIRef] = frozenset(
    {RDFS.comment, DC_DESCRIPTION, DCTERMS_DESCRIPTION}
)
DEFAULT_ALIAS_LOCAL_NAMES: frozenset[str] = frozenset(
    {"aliase", "alias", "aliases", "altlabel", "alt_label"}
)

# rdf:type objects that classify a *meta* resource (an OWL property/class
# declaration itself) rather than an application-level "instance of" fact.
OWL_META_TYPES: frozenset[URIRef] = frozenset(
    {
        OWL.Class,
        OWL.NamedIndividual,
        OWL.ObjectProperty,
        OWL.DatatypeProperty,
        OWL.AnnotationProperty,
        OWL.Ontology,
        RDF.Property,
    }
)

# rdfs:subClassOf targets that are structurally trivial and therefore not
# meaningful as Wikibase "subclass of" statements. owl:Thing is deliberately
# *not* included here: this ontology explicitly asserts every class as
# `rdfs:subClassOf owl:Thing`, and that top-of-hierarchy relationship is
# preserved -- see ``OWL_THING_CANONICAL_ID`` / ``maybe_synthesize_owl_thing_entity``
# below, which ensures owl:Thing itself resolves to a real (created-if-missing)
# Wikibase item instead of becoming a dangling/unresolved statement target.
TRIVIAL_SUBCLASS_TARGETS: frozenset[URIRef] = frozenset({OWL.Nothing})

OWL_THING_CANONICAL_ID: str = normalize_resource_identifier(str(OWL.Thing))

METADATA_PREDICATES_BASE: frozenset[URIRef] = frozenset({RDFS.label})


class OntologyLoadError(Exception):
    """Raised when the OWL file cannot be located or parsed."""


@dataclass
class ParserConfig:
    """Configuration that affects how metadata predicates are recognized.

    Kept separate from the global :class:`~src.config.PipelineConfig` so this
    module has no import-time dependency on environment/dotenv loading,
    which keeps it trivially unit-testable.
    """

    default_language: str = "en"
    alias_local_names: frozenset[str] = field(default_factory=lambda: DEFAULT_ALIAS_LOCAL_NAMES)
    description_predicates: frozenset[URIRef] = field(
        default_factory=lambda: DEFAULT_DESCRIPTION_PREDICATES
    )


def load_ontology_graph(owl_file_path: Path, rdf_format: Optional[str] = None) -> Graph:
    """Parse an OWL file into an ``rdflib.Graph``.

    Parameters
    ----------
    owl_file_path:
        Path to the OWL/RDF file.
    rdf_format:
        Explicit rdflib format name (e.g. ``"xml"``, ``"turtle"``). When
        ``None``, the format is guessed from the file extension and, if that
        fails, a small set of common formats is attempted in turn.

    Raises
    ------
    OntologyLoadError
        If the file does not exist, is empty, or cannot be parsed by any
        attempted serialization.
    """
    path = Path(owl_file_path)
    if not path.exists():
        raise OntologyLoadError(f"OWL file not found: {path}")
    if path.stat().st_size == 0:
        raise OntologyLoadError(f"OWL file is empty: {path}")

    graph = Graph()
    formats_to_try: list[str]
    if rdf_format:
        formats_to_try = [rdf_format]
    else:
        from rdflib.util import guess_format

        guessed = guess_format(str(path))
        formats_to_try = [guessed] if guessed else []
        formats_to_try += [f for f in ("xml", "turtle", "n3", "nt", "json-ld") if f not in formats_to_try]

    last_error: Optional[Exception] = None
    for candidate_format in formats_to_try:
        try:
            graph.parse(str(path), format=candidate_format)
            logger.info("Parsed %s using format=%s (%d triples)", path, candidate_format, len(graph))
            return graph
        except Exception as exc:  # noqa: BLE001 - we deliberately try multiple formats
            last_error = exc
            graph = Graph()
            continue

    raise OntologyLoadError(
        f"Could not parse {path} with any of the attempted formats {formats_to_try}: {last_error}"
    )


def get_local_name_from_term(term) -> str:
    """Local-name extraction for an rdflib term (URIRef or BNode)."""
    if isinstance(term, BNode):
        return f"_:{term}"
    return get_local_name(normalize_resource_identifier(str(term)))


def classify_resource(graph: Graph, term) -> str:
    """Classify an rdflib term into an :class:`~src.models.EntityType` value."""
    if isinstance(term, BNode):
        return EntityType.BLANK_NODE.value
    if not isinstance(term, URIRef):
        return EntityType.UNKNOWN.value

    declared_types = set(graph.objects(term, RDF.type))
    if OWL.Class in declared_types:
        return EntityType.OWL_CLASS.value
    if OWL.NamedIndividual in declared_types:
        return EntityType.NAMED_INDIVIDUAL.value
    if OWL.ObjectProperty in declared_types:
        return EntityType.OBJECT_PROPERTY.value
    if OWL.DatatypeProperty in declared_types:
        return EntityType.DATATYPE_PROPERTY.value
    if OWL.AnnotationProperty in declared_types:
        return EntityType.ANNOTATION_PROPERTY.value
    return EntityType.UNKNOWN.value


def classify_predicate(graph: Graph, predicate: URIRef) -> str:
    """Classify a predicate URI by its own declared rdf:type in the graph."""
    return classify_resource(graph, predicate)


def get_language_values(
    graph: Graph, subject: URIRef, predicate: URIRef, default_language: str
) -> dict[str, list[str]]:
    """Collect literal values of ``predicate`` for ``subject``, grouped by language.

    Literals without an explicit ``xml:lang`` tag are grouped under
    ``default_language``. Multiple literals can share a language (this is
    used for aliases as well as for detecting duplicate labels).
    """
    grouped: dict[str, list[str]] = {}
    for obj in graph.objects(subject, predicate):
        if not isinstance(obj, Literal):
            continue
        language = str(obj.language) if obj.language else default_language
        text = str(obj).strip()
        if not text:
            continue
        grouped.setdefault(language, []).append(text)
    return grouped


def build_property_label_index(graph: Graph) -> dict[str, str]:
    """Map every declared property/class canonical id to its rdfs:label.

    Used to make unmapped-predicate reports human-readable even when the
    predicate's local name is an opaque identifier (as WebProtege emits for
    properties created interactively, e.g. ``webp:RBzBtdfpmBBJgBJZp2NcA9``).
    """
    index: dict[str, str] = {}
    for subject in set(graph.subjects(RDF.type, OWL.ObjectProperty)) | set(
        graph.subjects(RDF.type, OWL.DatatypeProperty)
    ) | set(graph.subjects(RDF.type, OWL.AnnotationProperty)):
        labels = get_language_values(graph, subject, RDFS.label, "en")
        if labels:
            first_language = next(iter(labels))
            canonical = normalize_resource_identifier(str(subject))
            index[canonical] = labels[first_language][0]
    return index


def extract_entity_metadata(
    graph: Graph, subject: URIRef, config: ParserConfig
) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """Extract labels, descriptions, and aliases for a resource.

    Returns
    -------
    labels:
        One label per language (first literal wins; duplicates are reported
        separately by the statistics/validation stages, not silently merged).
    descriptions:
        One description per language, drawn from all configured description
        predicates (first match wins per language).
    aliases:
        All literal values (deduplicated, whitespace-normalized) from
        configured alias predicates, per language.
    """
    label_groups = get_language_values(graph, subject, RDFS.label, config.default_language)
    labels = {language: values[0] for language, values in label_groups.items()}

    descriptions: dict[str, str] = {}
    for predicate in config.description_predicates:
        for language, values in get_language_values(graph, subject, predicate, config.default_language).items():
            descriptions.setdefault(language, values[0])

    aliases: dict[str, list[str]] = {}
    for predicate in graph.predicates(subject, None):
        local_name = get_local_name_from_term(predicate).lower()
        if local_name not in config.alias_local_names:
            continue
        for language, values in get_language_values(graph, subject, predicate, config.default_language).items():
            bucket = aliases.setdefault(language, [])
            for value in values:
                normalized = " ".join(value.split())
                if normalized not in bucket:
                    bucket.append(normalized)

    return labels, descriptions, aliases


def _is_metadata_predicate(predicate: URIRef, config: ParserConfig) -> bool:
    if predicate in METADATA_PREDICATES_BASE:
        return True
    if predicate in config.description_predicates:
        return True
    if get_local_name_from_term(predicate).lower() in config.alias_local_names:
        return True
    return False


def extract_statements(
    graph: Graph, subject: URIRef, canonical_subject_id: str, config: ParserConfig
) -> list[OntologyStatement]:
    """Extract outgoing, non-metadata statements for a resource.

    ``rdf:type`` triples pointing at OWL meta-classes (``owl:Class``,
    ``owl:NamedIndividual``, ...) are classification facts, not domain
    statements, and are excluded. ``rdf:type`` triples pointing at a
    domain class become an ``instance_of`` statement. ``rdfs:subClassOf``
    triples targeting ``owl:Thing``/``owl:Nothing`` are excluded as
    structurally trivial.
    """
    statements: list[OntologyStatement] = []

    for predicate, obj in graph.predicate_objects(subject):
        if _is_metadata_predicate(predicate, config):
            continue

        if predicate == RDF.type:
            if obj in OWL_META_TYPES:
                continue
        elif predicate == RDFS.subClassOf:
            if obj in TRIVIAL_SUBCLASS_TARGETS:
                continue

        predicate_canonical = normalize_resource_identifier(str(predicate))
        predicate_local_name = get_local_name(predicate_canonical)

        if isinstance(obj, Literal):
            value_kind = "literal"
            value = str(obj).strip()
            literal_datatype = str(obj.datatype) if obj.datatype else None
            language = str(obj.language) if obj.language else None
        elif isinstance(obj, BNode):
            value_kind = "blank_node"
            value = f"_:{obj}"
            literal_datatype = None
            language = None
        else:
            value_kind = "resource"
            value = normalize_resource_identifier(str(obj))
            literal_datatype = None
            language = None

        statement_hash = compute_statement_hash(
            canonical_subject_id, predicate_local_name, value_kind, value, literal_datatype, language
        )
        statements.append(
            OntologyStatement(
                subject_canonical_id=canonical_subject_id,
                predicate_original=str(predicate),
                predicate_local_name=predicate_local_name,
                value_kind=value_kind,
                value=value,
                literal_datatype=literal_datatype,
                language=language,
                statement_hash=statement_hash,
            )
        )

    return statements


def build_ontology_entity(graph: Graph, subject: URIRef, config: ParserConfig) -> OntologyEntity:
    """Build a fully-populated :class:`~src.models.OntologyEntity` for one resource."""
    canonical_id = normalize_resource_identifier(str(subject))
    entity_type = classify_resource(graph, subject)
    local_name = get_local_name(canonical_id)
    labels, descriptions, aliases = extract_entity_metadata(graph, subject, config)
    statements = extract_statements(graph, subject, canonical_id, config)

    content_hash = compute_entity_hash(
        canonical_id,
        entity_type,
        labels,
        descriptions,
        aliases,
        [stmt.as_dict() for stmt in statements],
    )

    return OntologyEntity(
        canonical_id=canonical_id,
        original_id=str(subject),
        entity_type=entity_type,
        local_name=local_name,
        labels=labels,
        descriptions=descriptions,
        aliases=aliases,
        statements=statements,
        content_hash=content_hash,
    )


def synthesize_owl_thing_entity(default_language: str) -> OntologyEntity:
    """Build the synthetic entity representing ``owl:Thing``.

    ``owl:Thing`` is the implicit root of every OWL class hierarchy. It is
    virtually never declared with its own ``rdf:type owl:Class`` triple, so
    ``collect_syncable_entities`` would otherwise never create a Wikibase
    item for it -- leaving every class's ``rdfs:subClassOf owl:Thing``
    statement pointing at a target that can never resolve. This synthetic
    entity has no statements of its own (it is the top of the hierarchy) and
    is created/cached exactly like any other class-derived item.
    """
    labels = {default_language: "Thing"}
    content_hash = compute_entity_hash(OWL_THING_CANONICAL_ID, EntityType.OWL_CLASS.value, labels, {}, {}, [])
    return OntologyEntity(
        canonical_id=OWL_THING_CANONICAL_ID,
        original_id=str(OWL.Thing),
        entity_type=EntityType.OWL_CLASS.value,
        local_name=get_local_name(OWL_THING_CANONICAL_ID),
        labels=labels,
        descriptions={},
        aliases={},
        statements=[],
        content_hash=content_hash,
    )


def collect_syncable_entities(graph: Graph, config: ParserConfig) -> list[OntologyEntity]:
    """Collect every OWL class and named individual as an :class:`OntologyEntity`.

    Object/datatype/annotation *property declarations* are intentionally
    excluded here -- they are handled separately as the ``PROPERTY_MAP``
    configuration surface (see ``src/validators.py`` and Section 4 of the
    notebook), not as items to create in Wikibase.

    If the graph asserts ``rdfs:subClassOf owl:Thing`` for any class, a
    synthetic entity for ``owl:Thing`` itself is appended (see
    :func:`synthesize_owl_thing_entity`) so that relationship resolves to a
    real Wikibase item instead of being reported as unresolved.
    """
    classes = set(graph.subjects(RDF.type, OWL.Class))
    individuals = set(graph.subjects(RDF.type, OWL.NamedIndividual))

    entities: list[OntologyEntity] = []
    references_owl_thing = False
    for subject in sorted(classes | individuals, key=str):
        if isinstance(subject, BNode):
            logger.warning("Skipping blank-node class/individual subject: %s", subject)
            continue
        entity = build_ontology_entity(graph, subject, config)
        entities.append(entity)
        if not references_owl_thing and any(
            s.predicate_original == str(RDFS.subClassOf) and s.value == OWL_THING_CANONICAL_ID
            for s in entity.statements
        ):
            references_owl_thing = True

    if references_owl_thing:
        thing_entity = synthesize_owl_thing_entity(config.default_language)
        entities.insert(0, thing_entity)
        logger.info(
            "owl:Thing is used as a superclass in this ontology; synthesized a syncable entity for it (%s).",
            OWL_THING_CANONICAL_ID,
        )

    return entities


def resolve_property_map_key(statement: OntologyStatement) -> str:
    """Map a statement's predicate to the key it should use in ``PROPERTY_MAP``.

    ``rdf:type`` and ``rdfs:subClassOf`` are addressed in ``PROPERTY_MAP`` by
    their conventional CURIE strings (matching the project specification's
    example dictionary) rather than by their bare local name (``type`` /
    ``subClassOf``), since those local names are too generic to be
    self-explanatory in a configuration file.
    """
    if statement.predicate_original == str(RDF.type):
        return "rdf:type"
    if statement.predicate_original == str(RDFS.subClassOf):
        return "rdfs:subClassOf"
    return statement.predicate_local_name


def iter_all_typed_resources(graph: Graph) -> Iterable[tuple[URIRef, str]]:
    """Yield (resource, entity_type) for every resource with a recognized rdf:type."""
    seen: set[URIRef] = set()
    for subject in graph.subjects(RDF.type, None):
        if subject in seen or isinstance(subject, BNode):
            continue
        seen.add(subject)
        yield subject, classify_resource(graph, subject)
