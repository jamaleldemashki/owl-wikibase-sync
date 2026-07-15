"""
Central configuration for the OWL-to-Wikibase synchronization pipeline.

Everything that controls *how* a run behaves (paths, policies, retry
behavior, language settings) lives in :class:`PipelineConfig`. Everything
that is secret (Wikibase URL/credentials) lives in :class:`WikibaseCredentials`
and is loaded exclusively from environment variables / a ``.env`` file --
never hardcoded and never logged.

The ``PROPERTY_MAP`` in this module is the single place where OWL predicates
are wired to real Wikibase property IDs (PIDs). The pipeline never invents a
PID: entries default to ``pid=None`` and must be filled in manually after the
corresponding property has been created in the target Wikibase instance
(see README.md, "Property Creation and Mapping").
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from .hashing import compute_config_hash
from .models import PropertyMapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Property mapping
# ---------------------------------------------------------------------------
#
# Keys are the *local name* of the OWL predicate (see
# ``src.identifiers.get_local_name``) except for two synthetic keys:
#   - "rdf:type"        -> becomes an "instance of" statement
#   - "rdfs:subClassOf" -> becomes a "subclass of" statement
#
# Fill in "pid" with the real Wikibase property ID once the property has
# been created manually in the target Wikibase instance. Leave it as None
# until then -- the pipeline treats None as "not yet mapped" and will report
# (never silently skip, unless STOP_ON_UNRESOLVED_PROPERTY policy allows it)
# any OWL statement that uses an unmapped predicate.
PROPERTY_MAP: dict[str, dict[str, Optional[str]]] = {
    "rdf:type": {
        "pid": "P16",
        "wikibase_datatype": "wikibase-item",
        "role": "instance_of",
    },
    "rdfs:subClassOf": {
        "pid": "P18",
        "wikibase_datatype": "wikibase-item",
        "role": "subclass_of",
    },
    "source": {
        "pid": "P2",
        "wikibase_datatype": "string",
        "role": None,
    },
    "wikidata_uri": {
        "pid": "P3",
        "wikibase_datatype": "url",
        "role": None,
    },
    "orkg_id": {
        "pid": "P1",
        "wikibase_datatype": "external-id",
        "role": None,
    },
    "ontology_iri": {
        "pid": "P17",
        "wikibase_datatype": "url",
        "role": "identity",
    },
    "hasDataFormatSpecification": {
        "pid": "P4",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "hasDataItem": {
        "pid": "P5",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "hasDataModel": {
        "pid": "P6",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "hasInterchangeFormat": {
        "pid": None,
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "hasProcess": {
        "pid": "P7",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "hasSoftware": {
        "pid": "P8",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "mentions": {
        "pid": "P9",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    # WebProtege-generated opaque object properties. Their local names are
    # not human-readable, but Section 6 of the notebook resolves each one's
    # declared rdfs:label via `build_property_label_index` for display --
    # the comments below record that resolved label for maintainers reading
    # this file directly.
    "R7gBWBDDiQ4kgN0BrfJykA8": {  # label: "hasFollowingStep"
        "pid": "P10",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "RBl7XaioQSg3g21Wr88BAf7": {  # label: "part of"
        "pid": "P11",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "RBzBtdfpmBBJgBJZp2NcA9": {  # label: "hasVModelStep"
        "pid": "P12",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "RDE3vWiCDYtK2KIzOlv11fe": {  # label: "hasPrecedingStep"
        "pid": "P13",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "RDNNO7VH5G3XaRgYYmBlhYr": {  # label: "has part(s)"
        "pid": "P14",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
    "RKIYE9OXNe7ee11d5apc5e": {  # label: "hasCorrespondingStep"
        "pid": "P15",
        "wikibase_datatype": "wikibase-item",
        "role": None,
    },
}
"""Baseline property map covering predicates known to appear in
``slr_reviewed.owl``, with PIDs filled in for the Wikibase properties created
manually in the target instance (P1-P18). ``hasInterchangeFormat`` and the
opaque annotation property ``R7NnYCnkiXns2ntVncqu1F0`` (declared label
"instance of", distinct from the ``rdf:type`` -> P16 mapping above) remain
unmapped: the former is declared in the ontology but never used on any
individual, and no Wikibase property has been created for the latter yet --
Section 6/8 of the notebook will keep reporting it until one is."""


def build_property_mappings(property_map: dict[str, dict[str, Optional[str]]]) -> dict[str, PropertyMapping]:
    """Convert the raw ``PROPERTY_MAP`` dict into typed :class:`PropertyMapping` objects."""
    return {
        predicate: PropertyMapping(
            source_predicate=predicate,
            pid=entry.get("pid"),
            wikibase_datatype=entry.get("wikibase_datatype"),
            role=entry.get("role"),
        )
        for predicate, entry in property_map.items()
    }


# ---------------------------------------------------------------------------
# Pipeline configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineConfig:
    """All non-secret configuration for a synchronization run."""

    # Paths
    owl_file_path: Path = PROJECT_ROOT / "data" / "slr_reviewed.owl"
    cache_dir: Path = PROJECT_ROOT / "cache"
    report_dir: Path = PROJECT_ROOT / "reports"
    log_dir: Path = PROJECT_ROOT / "logs"

    # Safety / execution mode
    dry_run: bool = True

    # Traffic control and reliability
    sleep_time_seconds: float = 0.5
    max_retries: int = 5
    backoff_multiplier: float = 2.0
    request_timeout_seconds: int = 30
    batch_size: int = 50

    # Language handling
    default_language: str = "en"
    supported_languages: tuple[str, ...] = ("en", "de")

    # Synchronization policy
    update_labels: bool = True
    update_descriptions: bool = True
    update_aliases: bool = True
    update_statements: bool = True

    remove_obsolete_aliases: bool = False
    remove_obsolete_statements: bool = False
    delete_missing_items: bool = False

    metadata_sync_mode: str = "merge"  # "merge" | "replace_managed_values" | "report_only"

    stop_on_unresolved_property: bool = False
    stop_on_validation_error: bool = True

    # Ontology-specific metadata predicate recognition
    alias_local_names: tuple[str, ...] = ("aliase", "alias", "aliases")

    # Identity property used to look up existing Wikibase items
    identity_property_key: str = "ontology_iri"

    pipeline_version: str = "1.0.0"

    def sync_relevant_dict(self) -> dict:
        """Subset of configuration that affects synchronization *behavior*.

        Used to compute a config hash stored in the checkpoint file so a
        rerun can detect "the OWL file is unchanged but the policy changed"
        and treat cached completion state accordingly.
        """
        return {
            "dry_run": self.dry_run,
            "update_labels": self.update_labels,
            "update_descriptions": self.update_descriptions,
            "update_aliases": self.update_aliases,
            "update_statements": self.update_statements,
            "remove_obsolete_aliases": self.remove_obsolete_aliases,
            "remove_obsolete_statements": self.remove_obsolete_statements,
            "delete_missing_items": self.delete_missing_items,
            "metadata_sync_mode": self.metadata_sync_mode,
            "default_language": self.default_language,
            "supported_languages": list(self.supported_languages),
            "identity_property_key": self.identity_property_key,
        }

    def config_hash(self) -> str:
        return compute_config_hash(self.sync_relevant_dict())

    def with_overrides(self, **overrides) -> "PipelineConfig":
        """Return a copy of this config with the given fields overridden."""
        return replace(self, **overrides)

    def ensure_directories(self) -> None:
        for directory in (self.cache_dir, self.report_dir, self.log_dir):
            Path(directory).mkdir(parents=True, exist_ok=True)


def load_pipeline_config(**overrides) -> PipelineConfig:
    """Build the default :class:`PipelineConfig`, applying any overrides.

    Overrides are typically supplied from the notebook's Section 3
    (Central Configuration) cell so all run-affecting values are visible and
    editable in one place, as required by the project specification.
    """
    return PipelineConfig(**overrides)


# ---------------------------------------------------------------------------
# Secrets / credentials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WikibaseCredentials:
    """Wikibase connection details, loaded exclusively from the environment.

    Never print or log ``bot_password`` in full. :meth:`describe_safe`
    returns a redacted summary suitable for logging.
    """

    wikibase_url: str = ""
    api_url: str = ""
    sparql_url: str = ""
    bot_username: str = ""
    bot_password: str = ""

    @property
    def is_complete(self) -> bool:
        return bool(self.wikibase_url and self.api_url and self.bot_username and self.bot_password)

    @property
    def has_read_endpoints(self) -> bool:
        """True when enough information is present for read-only/preflight use."""
        return bool(self.wikibase_url and self.api_url)

    def describe_safe(self) -> dict[str, str]:
        return {
            "wikibase_url": self.wikibase_url or "<not set>",
            "api_url": self.api_url or "<not set>",
            "sparql_url": self.sparql_url or "<not set>",
            "bot_username": self.bot_username or "<not set>",
            "bot_password": "<redacted>" if self.bot_password else "<not set>",
        }


def load_wikibase_credentials(env_file: Optional[Path] = None) -> WikibaseCredentials:
    """Load Wikibase credentials from environment variables.

    Reads ``env_file`` (default: ``<project root>/.env``) via python-dotenv
    if present, then reads from ``os.environ``. Missing values are left as
    empty strings rather than raising, so read-only/dry-run notebook cells
    can run before credentials are configured.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=env_file or (PROJECT_ROOT / ".env"), override=False)
    except ImportError:  # pragma: no cover - python-dotenv is a hard dependency in requirements.txt
        pass

    return WikibaseCredentials(
        wikibase_url=os.environ.get("WIKIBASE_URL", "").strip(),
        api_url=os.environ.get("WIKIBASE_API_URL", "").strip(),
        sparql_url=os.environ.get("WIKIBASE_SPARQL_URL", "").strip(),
        bot_username=os.environ.get("WIKIBASE_BOT_USERNAME", "").strip(),
        bot_password=os.environ.get("WIKIBASE_BOT_PASSWORD", "").strip(),
    )
