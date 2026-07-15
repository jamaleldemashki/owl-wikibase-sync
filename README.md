# owl-wikibase-sync

A FAIR-oriented, idempotent, restartable pipeline that imports and
synchronizes an OWL ontology into a Wikibase instance. The primary
deliverable is `notebooks/owl_to_wikibase_sync.ipynb`; reusable logic lives
in `src/` so it is testable and usable outside the notebook.

## Overview

Given an OWL/RDF file (`data/slr_reviewed.owl` by default), this pipeline:

1. Parses the graph directly with `rdflib` (no CSV intermediate).
2. Classifies every resource and extracts labels, descriptions, aliases,
   and outgoing statements.
3. Validates a user-maintained OWL-predicate &rarr; Wikibase-property
   mapping (`PROPERTY_MAP`) and the ontology's data quality.
4. Builds one synchronization plan (resolve/create items, then add
   statements) used identically by dry-run and live execution.
5. In dry-run mode (default), shows and exports the plan without writing
   anything to Wikibase.
6. In live mode, executes that plan, updates a persistent cache immediately
   after every write, and produces post-run validation + reports.

The main use case is importing a research/domain ontology produced by a
tool like WebProtege into a Wikibase instance, and keeping that Wikibase
data in sync as the ontology evolves over time -- without ever creating
duplicate items or losing manually-added Wikibase content.

## Architecture

| Module | Responsibility |
|---|---|
| `src/identifiers.py` | Canonical identifier normalization, local-name extraction |
| `src/ontology_parser.py` | rdflib graph &rarr; `OntologyEntity`/`OntologyStatement` (the internal entity model) |
| `src/config.py` | `PipelineConfig`, `PROPERTY_MAP`, credential loading |
| `src/datatype_conversion.py` | OWL value &rarr; Wikibase datavalue conversion, value normalization for comparison |
| `src/cache_manager.py` | `EntityLookupCache` (id &rarr; QID) and `SynchronizationState` (run checkpoint), both atomic on disk |
| `src/validators.py` | Preflight (property-map + data-quality) and post-synchronization validation |
| `src/wikibase_client.py` | All Wikibase network I/O: auth, SPARQL identity lookup, item/claim writes, retry/backoff |
| `src/sync_planner.py` | Two-pass planner producing `PlannedAction` rows; shared by dry-run and live mode |
| `src/synchronizer.py` | Executes a plan live; the only other module allowed to write to Wikibase |
| `src/reporting.py` | Statistics computation and all `reports/*.json`/`.csv` writers |
| `src/logging_setup.py` | Structured logging to notebook output + `logs/pipeline.log` |
| `src/models.py` | Typed dataclasses shared across every stage |

Data flows one direction: OWL graph &rarr; entities &rarr; plan &rarr;
(dry-run report) or (live execution &rarr; cache/state updates &rarr;
post-sync validation &rarr; reports).

**`owl:Thing` handling**: `slr_reviewed.owl` asserts every class as
`rdfs:subClassOf owl:Thing`, but `owl:Thing` itself is never declared with
its own `rdf:type owl:Class` triple (it's OWL's implicit root), so it would
otherwise never get a Wikibase item and every class's "subclass of"
statement would be left unresolved. `src/ontology_parser.py::collect_syncable_entities`
detects this and synthesizes a single `OntologyEntity` for `owl:Thing`
(canonical id `http://www.w3.org/2002/07/owl#Thing`, label "Thing") whenever
the graph actually references it. It then flows through the pipeline like
any other class: Pass A creates it once and caches its QID under that
canonical id, so reruns reuse the cached item instead of recreating it, and
every class's `rdfs:subClassOf` statement resolves to a real target. This is
scoped specifically to `owl:Thing` -- the pipeline does not invent a
hierarchy root for ontologies that don't already reference it.

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real Wikibase credentials
jupyter notebook notebooks/owl_to_wikibase_sync.ipynb
```

## Configuration

All run-affecting settings live in one place: the `CONFIG = PipelineConfig(...)`
cell in Section 3 of the notebook (backed by `src/config.py::PipelineConfig`).
This includes paths (`owl_file_path`, `cache_dir`, `report_dir`, `log_dir`),
`dry_run`, retry/traffic-control settings (`sleep_time_seconds`,
`max_retries`, `backoff_multiplier`, `request_timeout_seconds`,
`batch_size`), language settings (`default_language`,
`supported_languages`), and synchronization policy flags
(`update_labels`/`update_descriptions`/`update_aliases`/`update_statements`,
`remove_obsolete_aliases`/`remove_obsolete_statements`/`delete_missing_items`,
`metadata_sync_mode`, `stop_on_unresolved_property`,
`stop_on_validation_error`).

Wikibase endpoints and credentials are the one thing kept *out* of
`PipelineConfig` and out of the notebook entirely: they are loaded from
`.env` by `load_wikibase_credentials()` (Section 9) and never hardcoded or
logged in full.

`metadata_sync_mode` supports:
- `merge` (default): add missing labels/descriptions/aliases, never remove existing values.
- `replace_managed_values`: reserved for future authoritative-replacement behavior; not yet wired into the synchronizer.
- `report_only`: reserved for a future audit-only mode.

## Property Creation and Mapping

Wikibase properties are **created manually** in the target instance
(`Special:NewProperty`) -- this pipeline never invents a PID. After creating
a property, add its PID and matching Wikibase datatype to `PROPERTY_MAP` in
`src/config.py` (or override it in the notebook's Section 4 cell before
running validation). Getting the datatype right matters: a resource-valued
OWL statement mapped to `wikibase-item` needs its target to resolve to a
real item, while the same URI mapped to `url`/`string`/`external-id` is
stored as text. Section 4 and Section 8 of the notebook validate the map
before any Wikibase call is made and report: discovered vs. mapped vs.
unmapped predicates, duplicate PID assignments, missing datatypes, and
predicate/datatype incompatibilities (e.g. a resource-valued predicate
mapped to `quantity`).

## First Run

1. Run Sections 1-2 (setup) and Section 5-7 (parse the OWL file, inspect
   ontology statistics).
2. Review `reports/ontology_statistics.json` and the unmapped-predicates
   table in Section 6.
3. Create the needed Wikibase properties, then fill `PROPERTY_MAP` PIDs.
4. Re-run Section 4 and Section 8 (preflight validation) until there are no
   `BLOCKING` findings you don't understand.
5. Run through Section 12-13 with `DRY_RUN = True` (the default) and inspect
   `reports/dry_run_plan.csv`.
6. Only once the plan looks correct, set `CONFIG.dry_run = False` in Section
   3, re-run from Section 9 onward, and let Section 15 execute live.
7. Inspect `reports/validation_report.json` and
   `reports/synchronization_report.json` (Sections 16-17).

## Repeated Runs

Idempotency comes from **stable canonical ontology identifiers**, never
labels (see `src/identifiers.py::normalize_resource_identifier`). Before
creating an item, the planner checks the local cache
(`cache/entity_lookup.json`), then -- if a PID is configured for
`ontology_iri` and a SPARQL endpoint is available -- queries Wikibase for an
item with that identity value. Cache-miss entities are looked up in batches
of `CONFIG.batch_size` (one SPARQL `VALUES` query per batch, via
`WikibaseClient.find_qids_by_identity_values_batch`) rather than one query
per entity -- on a first run over ~1000 entities this is the difference
between ~20 requests and ~1000. Re-running against an unchanged OWL file
produces an all-`SKIP_UNCHANGED` plan. Re-running after edits only plans the
actual deltas: new entities become `CREATE_ITEM`, changed labels become
`UPDATE_LABEL`, new statements become `ADD_STATEMENT`, and everything else
is skipped. Content hashes (`OntologyEntity.content_hash`,
`src/hashing.py`) are stored per entity as an optimization but are never the
sole correctness mechanism -- the planner still diffs against live/cached
state.

## Interruption Recovery

Every successful Wikibase write in `src/synchronizer.py` is immediately
followed by an atomic cache save and a `cache/synchronization_state.json`
checkpoint update. If the notebook kernel dies, the network drops, or you
interrupt a live run, simply re-run the notebook: `SynchronizationState.load_or_start`
resumes a prior in-progress run only if the OWL file hash, config hash, and
Wikibase URL all still match; otherwise it starts fresh. Either way, the
planner re-evaluates current cache/live state before writing anything --
it never blindly replays a recorded action list.

## Cache Management

`cache/entity_lookup.json` schema:

```json
{
  "metadata": {"schema_version": "1.0", "wikibase_url": "...", "last_updated": "ISO_TIMESTAMP"},
  "entities": {
    "<canonical_ontology_id>": {
      "qid": "Q123",
      "entity_type": "NamedIndividual",
      "source_identifier": "<original OWL id>",
      "label": "...",
      "last_seen_owl_hash": "<sha256>",
      "last_synced_at": "ISO_TIMESTAMP"
    }
  }
}
```

Writes are atomic (temp file + `os.replace`). A malformed cache file is
never silently deleted: it is quarantined to `entity_lookup.json.corrupt.N`
and the pipeline starts from an empty cache, logging the recovery. Section
10 backs up the cache (`.bak` file) before a run begins making changes.
`EntityLookupCache.belongs_to_current_instance()` warns if the cache's
recorded `wikibase_url` does not match the instance you are currently
targeting -- cached QIDs from a different instance are never blindly
trusted. Rebuilding the cache from scratch is safe (just delete
`cache/entity_lookup.json`): the next run will re-resolve every entity via
Wikibase lookup (if configured) or recreate items it cannot find, so only do
this if you are sure no matching items already exist, to avoid duplicates.

**Decision**: `cache/*.json` is gitignored by default (see `.gitignore`).
It is operational state tied to one Wikibase instance and one machine/run
history, not source code -- committing it would let it drift from reality
and leak QIDs from a possibly-private instance. If you need to share cache
state across a team (e.g. a shared staging Wikibase), commit a specific
snapshot deliberately and document its `wikibase_url` in the commit message.

## Update and Deletion Policy

Defaults are conservative and intentionally cannot delete anything:
`REMOVE_OBSOLETE_ALIASES`, `REMOVE_OBSOLETE_STATEMENTS`, and
`DELETE_MISSING_ITEMS` all default to `False`. The pipeline adds and updates
labels/descriptions/aliases/statements but never removes Wikibase content
automatically. An entity absent from a newer OWL file is never deleted from
Wikibase. The data model (`ActionType.UPDATE_STATEMENT`) leaves room to add
managed-deletion support later, but it is not enabled by default and should
only be turned on deliberately, after confirming the dry-run report shows
exactly the intended removals.

## Adapting to Other Ontologies

To point this pipeline at a different OWL file or Wikibase instance:

- `CONFIG.owl_file_path` (Section 3): path to the new file.
- `.env`: new `WIKIBASE_URL`/`WIKIBASE_API_URL`/`WIKIBASE_SPARQL_URL`/bot credentials.
- `PROPERTY_MAP` (`src/config.py` or Section 4): rebuild it for the new
  ontology's predicates -- Section 6/8 will report every predicate that
  needs a mapping.
- `CONFIG.alias_local_names` / `ParserConfig.description_predicates`
  (`src/ontology_parser.py`): adjust if the new ontology uses different
  annotation-property conventions for aliases/descriptions.
- `CONFIG.supported_languages` / `CONFIG.default_language`.
- `CONFIG.identity_property_key`: change if the new ontology's stable
  identifier should be stored/looked-up under a different property than
  `ontology_iri`.
- `CONFIG` policy flags (`update_*`, `remove_obsolete_*`,
  `delete_missing_items`, `metadata_sync_mode`) for the new deployment's
  synchronization policy.

## Troubleshooting

- **Invalid credentials**: Section 9's connection test reports
  `authenticated=False` with the underlying error in `errors`; dry-run
  planning still works without valid credentials.
- **Missing PIDs**: Section 8 reports `missing_pid` (ERROR) for every
  predicate used in the OWL file whose `PROPERTY_MAP` entry has `pid=None`.
- **Incorrect Wikibase datatypes**: Section 8 reports `incompatible_datatype`
  when a resource-valued predicate is mapped to a datatype that cannot hold
  a reference (e.g. `time`, `quantity`, `monolingualtext`).
- **Unknown OWL predicates**: Section 6 lists unmapped predicates with their
  resolved human-readable label (from the property's own `rdfs:label`) when
  available, which helps identify opaque WebProtege-style predicate ids.
- **Unresolved object resources**: shown as `UNRESOLVED_OBJECT` rows in the
  dry-run plan and `reports/unresolved_resources.csv`.
- **Duplicate identifiers**: `duplicate_canonical_id` (preflight, BLOCKING)
  and `duplicate_qid_mapping` (post-sync, BLOCKING) findings.
- **Malformed cache**: automatically quarantined; see "Cache Management" above.
- **Rate limiting / transient errors**: handled by `tenacity`-based retry
  with exponential backoff in `src/wikibase_client.py`, controlled by
  `MAX_RETRIES`/`BACKOFF_MULTIPLIER`/`SLEEP_TIME_SECONDS`.
- **Interrupted runs**: see "Interruption Recovery" above -- just re-run.
- **SPARQL endpoint delays**: if `WIKIBASE_SPARQL_URL` is slow/unavailable,
  `connect()` reports `sparql_available=False` and the planner falls back to
  cache-only entity resolution rather than blocking.
- **Placeholder literal values**: `reports/dry_run_plan.csv`/`unresolved_resources.csv`
  may show `ERROR` rows like `conversion failed: not_a_uri:'None'` -- this
  means the OWL file itself contains a literal placeholder (commonly the text
  `None`) where the authoring tool left a field unset rather than omitting
  the triple. `slr_reviewed.owl` has ~309 such `wikidata_uri` values. Section
  8 also surfaces these proactively as `invalid_url_value` findings. The
  pipeline reports and skips these rather than writing garbage text to
  Wikibase; fix them at the source (or extend `PROPERTY_MAP`/the parser with
  a placeholder-value allowlist) if you want them cleaned up automatically.

## Security

`.env` (real credentials) and `cache/*.json` (which can reveal internal
QIDs and the target Wikibase URL) are gitignored and must never be
committed. `WikibaseCredentials.describe_safe()` and the logging
configuration are designed so passwords are never printed or written to
`logs/pipeline.log`. Only `.env.example` (placeholders) is tracked.

## Limitations

- Statement qualifiers and references are modeled (`src/models.py`) but not
  yet populated by the planner.
- No in-place statement value replacement (`UPDATE_STATEMENT`) -- only
  additive `ADD_STATEMENT`; obsolete-value cleanup is gated behind the
  (currently inert-by-default) `REMOVE_OBSOLETE_STATEMENTS` flag.
- Remote duplicate detection requires both a SPARQL endpoint and a PID for
  the identity property; without either, only cache-known entities are
  deduplicated against Wikibase.
- Blank-node-valued statements are detected and reported, never
  synchronized (blank nodes have no stable identity to key a Wikibase item
  on).
- Tested against `data/slr_reviewed.owl` (WebProtege RDF/XML export,
  ~14.5k triples); other RDF serializations are supported by
  `load_ontology_graph`'s format auto-detection but have not been
  exercised end-to-end by this repository's test suite.
