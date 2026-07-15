"""
Focused unit tests for the pure/testable core of the OWL-to-Wikibase pipeline.

No test in this file makes a real network call. Wikibase interaction is
exercised only through mocked ``requests``/``WikibaseClient`` internals, per
the project specification ("Tests must not write to a real Wikibase.").
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rdflib import Graph
from rdflib.namespace import RDF, RDFS

from src.cache_manager import EntityLookupCache, SynchronizationState, _atomic_write_json
from src.config import PROPERTY_MAP, PipelineConfig, build_property_mappings
from src.datatype_conversion import (
    build_wikibase_datavalue,
    convert_owl_value_to_wikibase,
    normalize_uri_for_comparison,
    normalize_value_for_comparison,
)
from src.hashing import compute_entity_hash, compute_file_hash
from src.identifiers import get_local_name, is_probably_malformed_uri, normalize_resource_identifier
from src.models import ActionType, EntityType, PropertyMapping
from src.ontology_parser import (
    OWL_THING_CANONICAL_ID,
    ParserConfig,
    build_ontology_entity,
    classify_resource,
    collect_syncable_entities,
    get_language_values,
    resolve_property_map_key,
    synthesize_owl_thing_entity,
)
from src.sync_planner import ItemSnapshot, build_synchronization_plan, plan_entity_resolution, plan_statements
from src.synchronizer import _execute_metadata_updates
from src.validators import collect_predicate_usage, validate_ontology_quality, validate_property_map


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_OWL_XML = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:xsd="http://www.w3.org/2001/XMLSchema#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xml:base="http://example.org/onto"
         xmlns="http://example.org/onto#">

<owl:Ontology rdf:about="http://example.org/onto"/>

<owl:ObjectProperty rdf:about="#hasPart">
  <rdfs:label rdf:datatype="http://www.w3.org/2001/XMLSchema#string">has part</rdfs:label>
</owl:ObjectProperty>

<owl:AnnotationProperty rdf:about="#aliase">
  <rdfs:label rdf:datatype="http://www.w3.org/2001/XMLSchema#string">alias</rdfs:label>
</owl:AnnotationProperty>

<owl:AnnotationProperty rdf:about="#source"/>

<owl:Class rdf:about="#Widget">
  <rdfs:subClassOf rdf:resource="http://www.w3.org/2002/07/owl#Thing"/>
  <rdfs:label rdf:datatype="http://www.w3.org/2001/XMLSchema#string">widget</rdfs:label>
</owl:Class>

<owl:Class rdf:about="http://purl.example.org/GADGET_0001">
  <rdfs:subClassOf rdf:resource="http://www.w3.org/2002/07/owl#Thing"/>
  <rdfs:label rdf:datatype="http://www.w3.org/2001/XMLSchema#string">gadget</rdfs:label>
</owl:Class>

<owl:NamedIndividual rdf:about="#thing1">
  <rdf:type rdf:resource="#Widget"/>
  <hasPart rdf:resource="http://example.org/onto#http://purl.example.org/GADGET_0001"/>
  <source rdf:datatype="http://www.w3.org/2001/XMLSchema#string">literature review</source>
  <wikidata_uri rdf:resource="https://www.wikidata.org/wiki/Q999"/>
  <aliase rdf:datatype="http://www.w3.org/2001/XMLSchema#string">Thing One</aliase>
  <aliase rdf:datatype="http://www.w3.org/2001/XMLSchema#string">Thing One</aliase>
  <rdfs:label rdf:datatype="http://www.w3.org/2001/XMLSchema#string">thing one</rdfs:label>
</owl:NamedIndividual>

</rdf:RDF>
"""


@pytest.fixture()
def sample_graph() -> Graph:
    graph = Graph()
    graph.parse(data=SAMPLE_OWL_XML, format="xml")
    return graph


@pytest.fixture()
def parser_config() -> ParserConfig:
    return ParserConfig(default_language="en")


@pytest.fixture()
def sample_entities(sample_graph, parser_config):
    return collect_syncable_entities(sample_graph, parser_config)


@pytest.fixture()
def sample_property_map():
    raw = dict(PROPERTY_MAP)
    raw = {
        "rdf:type": {"pid": "P1", "wikibase_datatype": "wikibase-item", "role": "instance_of"},
        "rdfs:subClassOf": {"pid": "P2", "wikibase_datatype": "wikibase-item", "role": "subclass_of"},
        "source": {"pid": "P3", "wikibase_datatype": "string"},
        "hasPart": {"pid": "P4", "wikibase_datatype": "wikibase-item"},
        "ontology_iri": {"pid": "P5", "wikibase_datatype": "url", "role": "identity"},
        "wikidata_uri": {"pid": "P6", "wikibase_datatype": "url"},
    }
    return build_property_mappings(raw)


# ---------------------------------------------------------------------------
# Identifier normalization
# ---------------------------------------------------------------------------


class TestIdentifierNormalization:
    def test_plain_fragment(self):
        assert normalize_resource_identifier("http://tib.eu/slr#Contribution") == "http://tib.eu/slr#Contribution"

    def test_embedded_absolute_uri_in_fragment(self):
        raw = "http://tib.eu/slr#http://purl.obolibrary.org/obo/BFO_0000015"
        assert normalize_resource_identifier(raw) == "http://purl.obolibrary.org/obo/BFO_0000015"

    def test_plain_absolute_uri_unchanged(self):
        raw = "http://purl.obolibrary.org/obo/BFO_0000015"
        assert normalize_resource_identifier(raw) == raw

    def test_embedded_and_plain_forms_match(self):
        embedded = normalize_resource_identifier("http://tib.eu/slr#http://purl.obolibrary.org/obo/BFO_0000015")
        plain = normalize_resource_identifier("http://purl.obolibrary.org/obo/BFO_0000015")
        assert embedded == plain

    def test_is_probably_malformed_uri(self):
        assert is_probably_malformed_uri("http://tib.eu/slr#http://purl.obolibrary.org/obo/BFO_0000015")
        assert not is_probably_malformed_uri("http://tib.eu/slr#Contribution")


class TestLocalNameExtraction:
    def test_fragment_local_name(self):
        assert get_local_name("http://tib.eu/slr#Contribution") == "Contribution"

    def test_path_local_name(self):
        assert get_local_name("http://purl.obolibrary.org/obo/BFO_0000015") == "BFO_0000015"

    def test_bare_identifier(self):
        assert get_local_name("Contribution") == "Contribution"


# ---------------------------------------------------------------------------
# Resource classification and language extraction
# ---------------------------------------------------------------------------


class TestClassification:
    def test_classify_class(self, sample_graph):
        from rdflib import URIRef

        result = classify_resource(sample_graph, URIRef("http://example.org/onto#Widget"))
        assert result == EntityType.OWL_CLASS.value

    def test_classify_named_individual(self, sample_graph):
        from rdflib import URIRef

        result = classify_resource(sample_graph, URIRef("http://example.org/onto#thing1"))
        assert result == EntityType.NAMED_INDIVIDUAL.value

    def test_classify_object_property(self, sample_graph):
        from rdflib import URIRef

        result = classify_resource(sample_graph, URIRef("http://example.org/onto#hasPart"))
        assert result == EntityType.OBJECT_PROPERTY.value


class TestLanguageValueExtraction:
    def test_default_language_applied_when_untagged(self, sample_graph):
        from rdflib import URIRef

        values = get_language_values(sample_graph, URIRef("http://example.org/onto#Widget"), RDFS.label, "en")
        assert values == {"en": ["widget"]}

    def test_missing_predicate_returns_empty(self, sample_graph):
        from rdflib import URIRef

        values = get_language_values(sample_graph, URIRef("http://example.org/onto#Widget"), RDFS.comment, "en")
        assert values == {}


# ---------------------------------------------------------------------------
# Entity extraction, hashing, and predicate-key resolution
# ---------------------------------------------------------------------------


class TestEntityExtraction:
    def test_collects_classes_and_individuals(self, sample_entities):
        canonical_ids = {e.canonical_id for e in sample_entities}
        assert "http://example.org/onto#Widget" in canonical_ids
        assert "http://example.org/onto#thing1" in canonical_ids
        # object/annotation property declarations are not syncable entities
        assert "http://example.org/onto#hasPart" not in canonical_ids

    def test_malformed_embedded_uri_normalized_on_object_value(self, sample_entities):
        thing = next(e for e in sample_entities if e.canonical_id == "http://example.org/onto#thing1")
        resource_values = [s.value for s in thing.statements if s.value_kind == "resource"]
        assert "http://purl.example.org/GADGET_0001" in resource_values

    def test_aliases_are_deduplicated(self, sample_entities):
        thing = next(e for e in sample_entities if e.canonical_id == "http://example.org/onto#thing1")
        assert thing.aliases["en"] == ["Thing One"]

    def test_subclass_of_owl_thing_is_preserved(self, sample_entities):
        widget = next(e for e in sample_entities if e.canonical_id == "http://example.org/onto#Widget")
        thing_statements = [
            s for s in widget.statements if s.predicate_local_name == "subClassOf" and s.value == OWL_THING_CANONICAL_ID
        ]
        assert len(thing_statements) == 1

    def test_owl_thing_is_synthesized_when_referenced(self, sample_entities):
        thing_entities = [e for e in sample_entities if e.canonical_id == OWL_THING_CANONICAL_ID]
        assert len(thing_entities) == 1
        assert thing_entities[0].labels == {"en": "Thing"}
        assert thing_entities[0].entity_type == EntityType.OWL_CLASS.value
        assert thing_entities[0].statements == []

    def test_owl_thing_not_synthesized_when_not_referenced(self, parser_config):
        graph = Graph()
        graph.parse(
            data="""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xml:base="http://example.org/onto2" xmlns="http://example.org/onto2#">
<owl:Class rdf:about="#Standalone">
  <rdfs:label rdf:datatype="http://www.w3.org/2001/XMLSchema#string">standalone</rdfs:label>
</owl:Class>
</rdf:RDF>""",
            format="xml",
        )
        entities = collect_syncable_entities(graph, parser_config)
        assert all(e.canonical_id != OWL_THING_CANONICAL_ID for e in entities)

    def test_synthesize_owl_thing_entity_hash_is_deterministic(self):
        a = synthesize_owl_thing_entity("en")
        b = synthesize_owl_thing_entity("en")
        assert a.content_hash == b.content_hash
        assert a.canonical_id == OWL_THING_CANONICAL_ID

    def test_instance_of_meta_type_excluded_but_domain_type_kept(self, sample_entities):
        thing = next(e for e in sample_entities if e.canonical_id == "http://example.org/onto#thing1")
        type_statements = [s for s in thing.statements if s.predicate_original == str(RDF.type)]
        assert len(type_statements) == 1
        assert type_statements[0].value == "http://example.org/onto#Widget"

    def test_resolve_property_map_key_for_rdf_type_and_subclassof(self, sample_entities):
        thing = next(e for e in sample_entities if e.canonical_id == "http://example.org/onto#thing1")
        type_statement = next(s for s in thing.statements if s.predicate_original == str(RDF.type))
        assert resolve_property_map_key(type_statement) == "rdf:type"


class TestEntityHashing:
    def test_hash_is_deterministic(self):
        h1 = compute_entity_hash("id1", "OwlClass", {"en": "a"}, {}, {}, [])
        h2 = compute_entity_hash("id1", "OwlClass", {"en": "a"}, {}, {}, [])
        assert h1 == h2

    def test_hash_changes_when_label_changes(self):
        h1 = compute_entity_hash("id1", "OwlClass", {"en": "a"}, {}, {}, [])
        h2 = compute_entity_hash("id1", "OwlClass", {"en": "b"}, {}, {}, [])
        assert h1 != h2

    def test_hash_insensitive_to_statement_order(self):
        stmts_a = [{"p": "x"}, {"p": "y"}]
        stmts_b = [{"p": "y"}, {"p": "x"}]
        h1 = compute_entity_hash("id1", "OwlClass", {}, {}, {}, stmts_a)
        h2 = compute_entity_hash("id1", "OwlClass", {}, {}, {}, stmts_b)
        assert h1 == h2

    def test_file_hash_matches_for_identical_content(self, tmp_path):
        file_a = tmp_path / "a.owl"
        file_b = tmp_path / "b.owl"
        file_a.write_text("hello world", encoding="utf-8")
        file_b.write_text("hello world", encoding="utf-8")
        assert compute_file_hash(file_a) == compute_file_hash(file_b)

    def test_file_hash_differs_for_different_content(self, tmp_path):
        file_a = tmp_path / "a.owl"
        file_b = tmp_path / "b.owl"
        file_a.write_text("hello world", encoding="utf-8")
        file_b.write_text("goodbye world", encoding="utf-8")
        assert compute_file_hash(file_a) != compute_file_hash(file_b)


# ---------------------------------------------------------------------------
# Datatype conversion
# ---------------------------------------------------------------------------


class TestDatatypeConversion:
    def test_wikibase_item_requires_resource_kind(self):
        result = convert_owl_value_to_wikibase("literal", "some text", "wikibase-item")
        assert not result.success
        assert "datatype_mismatch" in result.error

    def test_wikibase_item_success(self):
        result = convert_owl_value_to_wikibase("resource", "http://x/y", "wikibase-item", resolved_qid="Q42")
        assert result.success
        assert result.datavalue["value"]["id"] == "Q42"
        assert result.comparable_value == "Q42"

    def test_wikibase_item_missing_qid(self):
        result = convert_owl_value_to_wikibase("resource", "http://x/y", "wikibase-item")
        assert not result.success
        assert result.error == "unresolved_target_qid"

    def test_string_normalizes_whitespace(self):
        result = build_wikibase_datavalue("string", "  hello   world  ")
        assert result.success
        assert result.datavalue["value"] == "hello world"

    def test_url_rejects_non_uri(self):
        result = build_wikibase_datavalue("url", "not-a-uri")
        assert not result.success

    def test_url_accepts_valid_uri(self):
        result = build_wikibase_datavalue("url", "http://www.wikidata.org/entity/Q3319996")
        assert result.success
        assert result.datavalue["type"] == "string"

    def test_monolingualtext_uses_default_language(self):
        result = build_wikibase_datavalue("monolingualtext", "hello", default_language="de")
        assert result.success
        assert result.datavalue["value"]["language"] == "de"

    def test_time_full_date(self):
        result = build_wikibase_datavalue("time", "2023-05-17")
        assert result.success
        assert result.datavalue["value"]["time"] == "+2023-05-17T00:00:00Z"
        assert result.datavalue["value"]["precision"] == 11

    def test_time_year_only(self):
        result = build_wikibase_datavalue("time", "2023")
        assert result.success
        assert result.datavalue["value"]["precision"] == 9

    def test_time_unparseable(self):
        result = build_wikibase_datavalue("time", "not a date")
        assert not result.success

    def test_quantity_with_unit(self):
        result = build_wikibase_datavalue("quantity", "42.5 meters")
        assert result.success
        assert result.datavalue["value"]["amount"] == "+42.5"
        assert result.datavalue["value"]["unit"] == "meters"

    def test_quantity_unparseable(self):
        result = build_wikibase_datavalue("quantity", "not a number")
        assert not result.success

    def test_unsupported_datatype(self):
        result = build_wikibase_datavalue("geo-shape", "irrelevant")
        assert not result.success
        assert "unsupported_wikibase_datatype" in result.error


class TestValueNormalization:
    def test_url_normalization_strips_trailing_slash(self):
        a = normalize_uri_for_comparison("http://Example.org/path/")
        b = normalize_uri_for_comparison("http://example.org/path")
        assert a == b

    def test_string_normalization_collapses_whitespace(self):
        assert normalize_value_for_comparison("string", "a   b\tc") == "a b c"

    def test_item_normalization_passthrough(self):
        assert normalize_value_for_comparison("wikibase-item", " Q123 ") == "Q123"


# ---------------------------------------------------------------------------
# Property-map validation (malformed mapping detection)
# ---------------------------------------------------------------------------


class TestPropertyMapValidation:
    def test_detects_duplicate_pid(self):
        mappings = build_property_mappings(
            {
                "source": {"pid": "P1", "wikibase_datatype": "string"},
                "orkg_id": {"pid": "P1", "wikibase_datatype": "external-id"},
            }
        )
        findings, summary = validate_property_map(mappings, {"source": {"count": 1, "value_kinds": {"literal"}}})
        codes = {f.check for f in findings}
        assert "duplicate_pid_assignment" in codes
        assert summary["duplicate_pid_assignments"] == {"P1": ["orkg_id", "source"]} or summary[
            "duplicate_pid_assignments"
        ] == {"P1": ["source", "orkg_id"]}

    def test_detects_missing_datatype(self):
        mappings = build_property_mappings({"source": {"pid": "P1", "wikibase_datatype": None}})
        findings, _ = validate_property_map(mappings, {"source": {"count": 1, "value_kinds": {"literal"}}})
        assert any(f.check == "missing_datatype" for f in findings)

    def test_detects_missing_pid_for_used_predicate(self):
        mappings = build_property_mappings({"source": {"pid": None, "wikibase_datatype": "string"}})
        findings, _ = validate_property_map(mappings, {"source": {"count": 1, "value_kinds": {"literal"}}})
        assert any(f.check == "missing_pid" for f in findings)

    def test_detects_incompatible_datatype_for_resource_values(self):
        # "quantity"/"time"/"monolingualtext" cannot represent a resource reference,
        # unlike "string"/"url"/"external-id", which the pipeline allows for storing
        # a resource's URI verbatim.
        mappings = build_property_mappings({"hasPart": {"pid": "P4", "wikibase_datatype": "quantity"}})
        findings, _ = validate_property_map(mappings, {"hasPart": {"count": 1, "value_kinds": {"resource"}}})
        assert any(f.check == "incompatible_datatype" for f in findings)

    def test_reports_unmapped_and_unused(self):
        mappings = build_property_mappings({"source": {"pid": "P1", "wikibase_datatype": "string"}})
        usage = {"hasPart": {"count": 3, "value_kinds": {"resource"}}}
        findings, summary = validate_property_map(mappings, usage)
        assert summary["unmapped_predicates"] == ["hasPart"]
        assert summary["unused_mappings"] == ["source"]

    def test_predicate_usage_collection(self, sample_entities):
        usage = collect_predicate_usage(sample_entities)
        assert "rdf:type" in usage
        assert "hasPart" in usage
        assert usage["hasPart"]["value_kinds"] == {"resource"}


class TestOntologyQualityValidation:
    def test_resource_value_mapped_to_url_is_not_flagged_unresolved(self, sample_entities, sample_property_map):
        # wikidata_uri's rdf:resource value (an external Wikidata URI) is mapped to
        # "url", not "wikibase-item", so it must not be treated as a dangling
        # reference to one of our own entities.
        findings = validate_ontology_quality(sample_entities, sample_property_map, "en", ["en", "de"])
        assert not any(
            f.check == "unresolved_object" and f.details.get("predicate") == "wikidata_uri" for f in findings
        )

    def test_resource_value_mapped_to_wikibase_item_still_flags_unresolved(self, sample_property_map):
        from src.models import OntologyEntity, OntologyStatement

        dangling = OntologyEntity(
            canonical_id="http://example.org/onto#Orphan",
            original_id="http://example.org/onto#Orphan",
            entity_type=EntityType.NAMED_INDIVIDUAL.value,
            labels={"en": "orphan"},
            statements=[
                OntologyStatement(
                    subject_canonical_id="http://example.org/onto#Orphan",
                    predicate_original="http://example.org/onto#hasPart",
                    predicate_local_name="hasPart",
                    value_kind="resource",
                    value="http://example.org/onto#DoesNotExist",
                )
            ],
        )
        findings = validate_ontology_quality([dangling], sample_property_map, "en", ["en", "de"])
        assert any(f.check == "unresolved_object" for f in findings)

    def test_placeholder_literal_flagged_as_invalid_url_value(self, sample_property_map):
        from src.models import OntologyEntity, OntologyStatement

        entity = OntologyEntity(
            canonical_id="http://example.org/onto#Placeholder",
            original_id="http://example.org/onto#Placeholder",
            entity_type=EntityType.NAMED_INDIVIDUAL.value,
            labels={"en": "placeholder"},
            statements=[
                OntologyStatement(
                    subject_canonical_id="http://example.org/onto#Placeholder",
                    predicate_original="wikidata_uri",
                    predicate_local_name="wikidata_uri",
                    value_kind="literal",
                    value="None",
                )
            ],
        )
        findings = validate_ontology_quality([entity], sample_property_map, "en", ["en", "de"])
        assert any(f.check == "invalid_url_value" for f in findings)


# ---------------------------------------------------------------------------
# Dry-run plan generation and duplicate-statement detection
# ---------------------------------------------------------------------------


class TestSyncPlanner:
    def test_plan_creates_all_new_items_with_empty_cache(self, sample_entities, sample_property_map, tmp_path):
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        config = PipelineConfig(dry_run=True, identity_property_key="ontology_iri")
        plan = build_synchronization_plan(sample_entities, sample_property_map, cache, None, config)
        create_actions = [a for a in plan.actions if a.action == ActionType.CREATE_ITEM.value]
        assert len(create_actions) == len(sample_entities)
        assert all(a.qid == "<NEW_ITEM>" for a in create_actions)

    def test_cached_entity_is_not_recreated(self, sample_entities, sample_property_map, tmp_path):
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        widget = next(e for e in sample_entities if e.canonical_id == "http://example.org/onto#Widget")
        cache.set(widget.canonical_id, "Q999", widget.entity_type, widget.original_id, "widget", widget.content_hash, "2026-01-01T00:00:00Z")

        config = PipelineConfig(dry_run=True)
        plan = build_synchronization_plan(sample_entities, sample_property_map, cache, None, config)

        create_actions = [a for a in plan.actions if a.action == ActionType.CREATE_ITEM.value]
        created_ids = {a.source_identifier for a in create_actions}
        assert widget.canonical_id not in created_ids
        assert plan.resolved_entities[widget.canonical_id].qid == "Q999"

    def test_add_statement_skipped_when_already_present(self, sample_entities, sample_property_map, tmp_path):
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        for entity in sample_entities:
            cache.set(entity.canonical_id, f"Q{abs(hash(entity.canonical_id)) % 1000}", entity.entity_type, entity.original_id, "x", entity.content_hash, "2026-01-01T00:00:00Z")

        config = PipelineConfig(dry_run=True)
        result = plan_entity_resolution(sample_entities, sample_property_map, cache, None, config)

        thing_qid = result.resolved_entities["http://example.org/onto#thing1"].qid
        result.snapshots[thing_qid] = ItemSnapshot(claims={"P3": ["literature review"]})

        plan_statements(sample_entities, sample_property_map, result, config)
        source_actions = [a for a in result.actions if a.pid == "P3"]
        assert any(a.action == ActionType.SKIP_UNCHANGED.value for a in source_actions)
        assert not any(a.action == ActionType.ADD_STATEMENT.value for a in source_actions)

    def test_unmapped_predicate_produces_skip_action(self, sample_entities, tmp_path):
        mappings = build_property_mappings(
            {"rdf:type": {"pid": "P1", "wikibase_datatype": "wikibase-item"}}
        )
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        config = PipelineConfig(dry_run=True)
        plan = build_synchronization_plan(sample_entities, mappings, cache, None, config)
        assert any(a.action == ActionType.SKIP_UNMAPPED_PROPERTY.value and a.property_name == "hasPart" for a in plan.actions)

    def test_resource_statement_to_new_item_uses_new_item_marker(self, sample_entities, sample_property_map, tmp_path):
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        config = PipelineConfig(dry_run=True)
        plan = build_synchronization_plan(sample_entities, sample_property_map, cache, None, config)
        has_part_actions = [a for a in plan.actions if a.property_name == "hasPart" and a.action == ActionType.ADD_STATEMENT.value]
        assert len(has_part_actions) == 1
        assert has_part_actions[0].new_value == "<NEW_ITEM>"
        assert has_part_actions[0].target_source_identifier == "http://purl.example.org/GADGET_0001"

    def test_owl_thing_is_created_and_used_as_subclass_target(self, sample_entities, sample_property_map, tmp_path):
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        config = PipelineConfig(dry_run=True)
        plan = build_synchronization_plan(sample_entities, sample_property_map, cache, None, config)

        thing_creates = [a for a in plan.actions if a.source_identifier == OWL_THING_CANONICAL_ID and a.action == ActionType.CREATE_ITEM.value]
        assert len(thing_creates) == 1

        # Both sample classes (Widget and GADGET_0001) assert rdfs:subClassOf owl:Thing.
        subclass_actions = [
            a for a in plan.actions
            if a.property_name == "rdfs:subClassOf" and a.action == ActionType.ADD_STATEMENT.value
        ]
        assert len(subclass_actions) == 2
        assert {a.source_identifier for a in subclass_actions} == {
            "http://example.org/onto#Widget",
            "http://purl.example.org/GADGET_0001",
        }
        assert all(a.target_source_identifier == OWL_THING_CANONICAL_ID for a in subclass_actions)
        assert all(a.new_value == "<NEW_ITEM>" for a in subclass_actions)

    def test_resource_valued_statement_mapped_to_url_is_written_verbatim(self, sample_entities, sample_property_map, tmp_path):
        # Regression test: some `wikidata_uri` triples in the real ontology use
        # `rdf:resource="https://www.wikidata.org/wiki/Q123"` instead of a literal.
        # Because wikidata_uri maps to "url" (not "wikibase-item"), that external URI
        # must be written verbatim, not looked up as one of our own entities.
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        config = PipelineConfig(dry_run=True)
        plan = build_synchronization_plan(sample_entities, sample_property_map, cache, None, config)

        wikidata_actions = [a for a in plan.actions if a.property_name == "wikidata_uri"]
        assert len(wikidata_actions) == 1
        assert wikidata_actions[0].action == ActionType.ADD_STATEMENT.value
        assert wikidata_actions[0].new_value == "https://www.wikidata.org/wiki/Q999"
        assert wikidata_actions[0].target_source_identifier == ""

        assert not any(a.action == ActionType.UNRESOLVED_OBJECT.value and a.property_name == "wikidata_uri" for a in plan.actions)

    def test_remote_resolution_uses_one_batched_call_not_per_entity(self, sample_entities, sample_property_map, tmp_path):
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        config = PipelineConfig(dry_run=True, batch_size=50)

        mock_client = MagicMock()
        mock_client.credentials.sparql_url = "https://demo.wikibase.cloud/query/sparql"
        mock_client.find_qids_by_identity_values_batch.return_value = {}

        plan_entity_resolution(sample_entities, sample_property_map, cache, mock_client, config)

        mock_client.find_qids_by_identity_values_batch.assert_called_once()
        called_pid, called_values = mock_client.find_qids_by_identity_values_batch.call_args[0]
        assert called_pid == "P5"  # ontology_iri's configured PID in sample_property_map
        assert len(called_values) == len(sample_entities)
        mock_client.find_qids_by_identity_value.assert_not_called()

    def test_cached_owl_thing_is_not_recreated(self, sample_entities, sample_property_map, tmp_path):
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        thing_entity = next(e for e in sample_entities if e.canonical_id == OWL_THING_CANONICAL_ID)
        cache.set(thing_entity.canonical_id, "Q777", thing_entity.entity_type, thing_entity.original_id, "Thing", thing_entity.content_hash, "2026-01-01T00:00:00Z")

        config = PipelineConfig(dry_run=True)
        plan = build_synchronization_plan(sample_entities, sample_property_map, cache, None, config)

        thing_creates = [a for a in plan.actions if a.source_identifier == OWL_THING_CANONICAL_ID and a.action == ActionType.CREATE_ITEM.value]
        assert thing_creates == []
        assert plan.resolved_entities[OWL_THING_CANONICAL_ID].qid == "Q777"


# ---------------------------------------------------------------------------
# Cache loading / atomic saving / corruption recovery
# ---------------------------------------------------------------------------


class TestEntityLookupCache:
    def test_missing_file_starts_empty(self, tmp_path):
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        assert cache.entities == {}

    def test_atomic_save_and_reload_round_trip(self, tmp_path):
        path = tmp_path / "entity_lookup.json"
        cache = EntityLookupCache.load(path, wikibase_url="https://demo.wikibase.cloud")
        cache.set("id1", "Q1", "OwlClass", "orig1", "label", "hash1", "2026-01-01T00:00:00Z")
        cache.save(last_updated="2026-01-01T00:00:00Z")

        reloaded = EntityLookupCache.load(path, wikibase_url="https://demo.wikibase.cloud")
        assert reloaded.get("id1")["qid"] == "Q1"
        assert reloaded.metadata["wikibase_url"] == "https://demo.wikibase.cloud"

    def test_no_temp_file_left_behind(self, tmp_path):
        path = tmp_path / "entity_lookup.json"
        cache = EntityLookupCache.load(path)
        cache.set("id1", "Q1", "OwlClass", "orig1", "label", "hash1", "2026-01-01T00:00:00Z")
        cache.save(last_updated="2026-01-01T00:00:00Z")
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_malformed_json_is_quarantined_not_crashing(self, tmp_path):
        path = tmp_path / "entity_lookup.json"
        path.write_text("{not valid json", encoding="utf-8")
        cache = EntityLookupCache.load(path)
        assert cache.entities == {}
        quarantined = list(tmp_path.glob("*.corrupt.*"))
        assert len(quarantined) == 1

    def test_duplicate_qid_detection(self, tmp_path):
        cache = EntityLookupCache.load(tmp_path / "entity_lookup.json")
        cache.set("id1", "Q1", "OwlClass", "orig1", "label", "hash1", "2026-01-01T00:00:00Z")
        cache.set("id2", "Q1", "OwlClass", "orig2", "label2", "hash2", "2026-01-01T00:00:00Z")
        duplicates = cache.find_duplicate_qids()
        assert duplicates == {"Q1": ["id1", "id2"]}

    def test_backup_creates_bak_file(self, tmp_path):
        path = tmp_path / "entity_lookup.json"
        cache = EntityLookupCache.load(path)
        cache.set("id1", "Q1", "OwlClass", "orig1", "label", "hash1", "2026-01-01T00:00:00Z")
        cache.save(last_updated="2026-01-01T00:00:00Z")
        backup_path = cache.backup()
        assert backup_path is not None
        assert backup_path.exists()


class TestSynchronizationState:
    def test_fresh_state_when_no_prior_file(self, tmp_path):
        state = SynchronizationState.load_or_start(
            tmp_path / "state.json", run_id="run1", owl_file_hash="h1", config_hash="c1",
            wikibase_url="https://demo", start_time="2026-01-01T00:00:00Z",
        )
        assert state.last_successful_phase == "not_started"

    def test_resumes_matching_incomplete_run(self, tmp_path):
        path = tmp_path / "state.json"
        first = SynchronizationState.load_or_start(
            path, run_id="run1", owl_file_hash="h1", config_hash="c1", wikibase_url="https://demo", start_time="t1"
        )
        first.mark_phase_complete("entity_resolution")
        first.mark_entity_processed("id1")
        first.save()

        second = SynchronizationState.load_or_start(
            path, run_id="run2", owl_file_hash="h1", config_hash="c1", wikibase_url="https://demo", start_time="t2"
        )
        assert second.last_successful_phase == "entity_resolution"
        assert "id1" in second.processed_entity_ids

    def test_does_not_resume_when_owl_hash_differs(self, tmp_path):
        path = tmp_path / "state.json"
        first = SynchronizationState.load_or_start(
            path, run_id="run1", owl_file_hash="h1", config_hash="c1", wikibase_url="https://demo", start_time="t1"
        )
        first.mark_phase_complete("entity_resolution")
        first.save()

        second = SynchronizationState.load_or_start(
            path, run_id="run2", owl_file_hash="h2", config_hash="c1", wikibase_url="https://demo", start_time="t2"
        )
        assert second.last_successful_phase == "not_started"

    def test_completed_run_is_not_resumed(self, tmp_path):
        path = tmp_path / "state.json"
        first = SynchronizationState.load_or_start(
            path, run_id="run1", owl_file_hash="h1", config_hash="c1", wikibase_url="https://demo", start_time="t1"
        )
        first.finish("completed")
        first.save()

        second = SynchronizationState.load_or_start(
            path, run_id="run2", owl_file_hash="h1", config_hash="c1", wikibase_url="https://demo", start_time="t2"
        )
        assert second.run_id == "run2"
        assert second.last_successful_phase == "not_started"


# ---------------------------------------------------------------------------
# Wikibase client: mocked network interactions and dry-run guard
# ---------------------------------------------------------------------------


class TestWikibaseClientMocked:
    def _make_client(self):
        from src.config import WikibaseCredentials

        credentials = WikibaseCredentials(
            wikibase_url="https://demo.wikibase.cloud",
            api_url="https://demo.wikibase.cloud/w/api.php",
            sparql_url="https://demo.wikibase.cloud/query/sparql",
            bot_username="",
            bot_password="",
        )
        config = PipelineConfig(dry_run=True, max_retries=1, sleep_time_seconds=0)
        from src.wikibase_client import WikibaseClient

        return WikibaseClient(credentials, config)

    def test_create_item_raises_in_dry_run(self):
        from src.wikibase_client import DryRunViolationError

        client = self._make_client()
        with pytest.raises(DryRunViolationError):
            client.create_item({"en": "test"}, {}, {})

    def test_add_claim_raises_in_dry_run(self):
        from src.wikibase_client import DryRunViolationError

        client = self._make_client()
        with pytest.raises(DryRunViolationError):
            client.add_claim("Q1", "P1", "string", {"value": "x", "type": "string"})

    @patch("src.wikibase_client.requests.get")
    def test_find_qids_by_identity_value_parses_sparql_json(self, mock_get):
        client = self._make_client()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": {"bindings": [{"item": {"value": "https://demo.wikibase.cloud/entity/Q42"}}]}
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        qids = client.find_qids_by_identity_value("P5", "http://example.org/onto#Widget")
        assert qids == ["Q42"]
        mock_get.assert_called_once()

    @patch("src.wikibase_client.requests.get")
    def test_find_qids_returns_empty_list_on_request_failure(self, mock_get):
        client = self._make_client()
        mock_get.side_effect = Exception("network down")
        qids = client.find_qids_by_identity_value("P5", "http://example.org/onto#Widget")
        assert qids == []

    @patch("src.wikibase_client.requests.get")
    def test_connect_with_bot_credentials_uses_wbi_login_class(self, mock_get):
        # Regression test: wikibaseintegrator 0.12.x renamed the bot-password login
        # class from `wbi_login.LoginBot` to `wbi_login.Login` -- using the wrong
        # name raises AttributeError and silently reports authenticated=False.
        from src.config import WikibaseCredentials

        credentials = WikibaseCredentials(
            wikibase_url="https://demo.wikibase.cloud",
            api_url="https://demo.wikibase.cloud/w/api.php",
            sparql_url="https://demo.wikibase.cloud/query/sparql",
            bot_username="Bot@owlsync",
            bot_password="secret",
        )
        config = PipelineConfig(dry_run=True, max_retries=1, sleep_time_seconds=0)
        from src.wikibase_client import WikibaseClient

        client = WikibaseClient(credentials, config)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.ok = True
        mock_get.return_value = mock_response

        with patch("wikibaseintegrator.wbi_login.Login") as mock_login_cls:
            mock_login_cls.return_value = MagicMock()
            status = client.connect(require_write=True)

        assert mock_login_cls.call_count == 1
        _, call_kwargs = mock_login_cls.call_args
        assert call_kwargs["user"] == "Bot@owlsync"
        assert call_kwargs["password"] == "secret"
        assert call_kwargs["mediawiki_api_url"] == credentials.api_url
        assert status.authenticated is True
        assert status.bot_permissions is True

    @patch("src.wikibase_client.requests.get")
    def test_connect_reports_api_available(self, mock_get):
        client = self._make_client()
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.ok = True
        mock_get.return_value = mock_response

        status = client.connect()
        assert status.api_available is True
        assert status.authenticated is False  # no bot credentials configured

    @patch("src.wikibase_client.requests.get")
    def test_batched_identity_lookup_issues_one_request_for_many_values(self, mock_get):
        client = self._make_client()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": {
                "bindings": [
                    {
                        "value": {"value": "http://example.org/onto#Widget"},
                        "item": {"value": "https://demo.wikibase.cloud/entity/Q1"},
                    },
                    {
                        "value": {"value": "http://example.org/onto#Gadget"},
                        "item": {"value": "https://demo.wikibase.cloud/entity/Q2"},
                    },
                ]
            }
        }
        mock_get.return_value = mock_response

        values = [f"http://example.org/onto#Entity{i}" for i in range(50)]
        values[0] = "http://example.org/onto#Widget"
        values[1] = "http://example.org/onto#Gadget"

        matches = client.find_qids_by_identity_values_batch("P17", values)

        mock_get.assert_called_once()  # one HTTP round-trip regardless of batch size
        assert matches == {
            "http://example.org/onto#Widget": ["Q1"],
            "http://example.org/onto#Gadget": ["Q2"],
        }
        # values with no match are simply absent, not present with an empty list
        assert "http://example.org/onto#Entity5" not in matches

    def test_batched_identity_lookup_empty_values_short_circuits(self):
        client = self._make_client()
        assert client.find_qids_by_identity_values_batch("P17", []) == {}

    @patch("src.wikibase_client.requests.get")
    def test_batched_identity_lookup_returns_empty_on_failure(self, mock_get):
        client = self._make_client()
        mock_get.side_effect = Exception("network down")
        matches = client.find_qids_by_identity_values_batch("P17", ["http://example.org/onto#Widget"])
        assert matches == {}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_config_hash_stable_for_equivalent_config(self):
        a = PipelineConfig(dry_run=True, update_labels=True)
        b = PipelineConfig(dry_run=True, update_labels=True)
        assert a.config_hash() == b.config_hash()

    def test_config_hash_changes_with_policy(self):
        a = PipelineConfig(dry_run=True, remove_obsolete_statements=False)
        b = PipelineConfig(dry_run=True, remove_obsolete_statements=True)
        assert a.config_hash() != b.config_hash()

    def test_property_map_pids_are_well_formed_or_unset(self):
        # PIDs are either None (not yet configured) or a real "Pxxx" id supplied by the
        # user after manually creating the property in Wikibase -- never a guessed/malformed value.
        import re

        for key, entry in PROPERTY_MAP.items():
            pid = entry["pid"]
            assert pid is None or re.fullmatch(r"P\d+", pid), f"PROPERTY_MAP['{key}']['pid'] is malformed: {pid!r}"

    def test_property_map_has_no_duplicate_pids(self):
        pids = [entry["pid"] for entry in PROPERTY_MAP.values() if entry["pid"]]
        assert len(pids) == len(set(pids)), "PROPERTY_MAP assigns the same PID to more than one predicate"

    def test_unused_predicate_remains_unmapped(self):
        # hasInterchangeFormat is declared in the ontology but never used on any individual,
        # so it should stay unmapped until/unless it is actually needed.
        assert PROPERTY_MAP["hasInterchangeFormat"]["pid"] is None


# ---------------------------------------------------------------------------
# Synchronizer (live execution): metadata-update redundancy and enum bug
# ---------------------------------------------------------------------------


class TestSynchronizerMetadataUpdates:
    def test_skips_entities_created_earlier_in_the_same_run(self):
        # Regression test: create_item() already writes an entity's full
        # labels/descriptions/aliases at creation time. Re-applying them via
        # update_metadata() is pure redundant traffic -- and was the reason a
        # real live run hit 349 crashes (see test_update_metadata_uses_... below)
        # on items whose data was already correct on Wikibase.
        plan = MagicMock()
        plan.actions = [
            PlannedActionStub("UPDATE_LABEL", "http://example.org/onto#New", "label@en", "brand new"),
            PlannedActionStub("ADD_ALIAS", "http://example.org/onto#New", "alias@en", "new alias"),
            PlannedActionStub("UPDATE_LABEL", "http://example.org/onto#Existing", "label@en", "changed label"),
        ]
        client = MagicMock()
        result = MagicMock(errors=0, labels_updated=0, descriptions_updated=0, aliases_added=0)
        state = MagicMock()

        _execute_metadata_updates(
            plan,
            client,
            state,
            result,
            qid_by_canonical_id={"http://example.org/onto#New": "Q1", "http://example.org/onto#Existing": "Q2"},
            created_this_run={"http://example.org/onto#New"},
        )

        client.update_metadata.assert_called_once()
        called_qid = client.update_metadata.call_args[0][0]
        assert called_qid == "Q2"  # only the pre-existing entity's metadata is re-applied

    def test_update_metadata_uses_action_if_exists_enum_not_string(self):
        # Regression test: passing the plain string "APPEND" to wikibaseintegrator's
        # Aliases.set(action_if_exists=...) raises
        # `TypeError: unsupported operand type(s) for 'in': 'str' and 'EnumMeta'`
        # because the library asserts `action_if_exists in ActionIfExists` (an Enum).
        from wikibaseintegrator.wbi_enums import ActionIfExists

        from src.config import WikibaseCredentials
        from src.wikibase_client import WikibaseClient

        credentials = WikibaseCredentials(
            wikibase_url="https://demo.wikibase.cloud",
            api_url="https://demo.wikibase.cloud/w/api.php",
            bot_username="Bot@x",
            bot_password="secret",
        )
        config = PipelineConfig(dry_run=False, sleep_time_seconds=0, max_retries=1)
        client = WikibaseClient(credentials, config)

        mock_item = MagicMock()
        mock_wbi = MagicMock()
        mock_wbi.item.get.return_value = mock_item
        client._wbi = mock_wbi
        client._login = MagicMock()

        client.update_metadata("Q1", labels={}, descriptions={}, aliases={"en": ["Alpha", "Beta"]})

        mock_item.aliases.set.assert_called_once()
        call_kwargs = mock_item.aliases.set.call_args.kwargs
        assert call_kwargs["action_if_exists"] == ActionIfExists.APPEND_OR_REPLACE
        assert not isinstance(call_kwargs["action_if_exists"], str)
        mock_item.write.assert_called_once()


class PlannedActionStub:
    """Minimal stand-in for models.PlannedAction with just the fields _execute_metadata_updates reads."""

    def __init__(self, action, source_identifier, property_name, new_value):
        self.action = action
        self.source_identifier = source_identifier
        self.property_name = property_name
        self.new_value = new_value
