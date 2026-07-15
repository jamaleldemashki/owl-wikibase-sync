"""
Deterministic hashing utilities used for change detection.

Hashes are an *optimization*, not the sole correctness mechanism: they let
the planner skip expensive comparison work for entities that provably have
not changed since the last successful synchronization. The pipeline never
relies on a hash match alone to decide that Wikibase state is correct -- see
``sync_planner.py`` for how hashes are combined with cache/live lookups.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def compute_file_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute a stable SHA-256 hash of a file's raw bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    """Serialize a value to JSON with sorted keys and no whitespace ambiguity."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def compute_dict_hash(value: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hash of a JSON-serializable mapping."""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def compute_entity_hash(
    canonical_id: str,
    entity_type: str,
    labels: dict[str, str],
    descriptions: dict[str, str],
    aliases: dict[str, list[str]],
    statements: list[dict[str, Any]],
) -> str:
    """Compute a deterministic content hash for an :class:`OntologyEntity`.

    The hash covers everything the pipeline could write to Wikibase for this
    entity (identity, labels, descriptions, aliases, outgoing statements).
    Two runs over an unchanged OWL file must produce identical hashes.
    """
    payload = {
        "canonical_id": canonical_id,
        "entity_type": entity_type,
        "labels": labels,
        "descriptions": descriptions,
        "aliases": {lang: sorted(values) for lang, values in aliases.items()},
        "statements": sorted(
            (_canonical_json(stmt) for stmt in statements),
        ),
    }
    return compute_dict_hash(payload)


def compute_statement_hash(
    subject_canonical_id: str,
    predicate_local_name: str,
    value_kind: str,
    normalized_value: str,
    literal_datatype: str | None,
    language: str | None,
) -> str:
    """Compute a deterministic hash identifying a single desired statement."""
    payload = {
        "subject": subject_canonical_id,
        "predicate": predicate_local_name,
        "value_kind": value_kind,
        "value": normalized_value,
        "datatype": literal_datatype or "",
        "language": language or "",
    }
    return compute_dict_hash(payload)


def compute_config_hash(config_values: dict[str, Any]) -> str:
    """Compute a deterministic hash of the synchronization-relevant configuration.

    Used by the checkpoint/state file to detect when a rerun uses different
    policy settings than a prior in-progress run, which is useful context
    when deciding whether to trust partially completed work.
    """
    return compute_dict_hash(config_values)
