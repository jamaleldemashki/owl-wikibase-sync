"""
Persistent, restart-safe state for the synchronization pipeline.

Two JSON files back the pipeline's memory across runs:

``cache/entity_lookup.json``
    Canonical-ontology-id -> Wikibase QID lookup table. This is what makes
    the pipeline idempotent: before creating an item, Pass A always checks
    this cache (and, failing that, Wikibase itself) so reruns reuse existing
    items instead of duplicating them.

``cache/synchronization_state.json``
    A checkpoint of the current/last run (phase reached, processed entity
    ids, failed/pending actions) so an interrupted run can be diagnosed and
    safely resumed -- a rerun always re-evaluates live Wikibase/cache state
    rather than blindly replaying recorded writes.

Both files are written atomically (write to a temp file in the same
directory, then ``os.replace``) so a crash mid-write cannot leave a
half-written, unparseable file behind.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("owl_wikibase_sync.cache_manager")

CACHE_SCHEMA_VERSION = "1.0"
STATE_SCHEMA_VERSION = "1.0"


class CacheCorruptionError(Exception):
    """Raised when a cache file exists but cannot be parsed as valid JSON."""


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def _load_json_with_recovery(path: Path, empty_default: dict[str, Any]) -> dict[str, Any]:
    """Load JSON from ``path``, quarantining and recovering from corruption.

    A malformed file is renamed to ``<name>.corrupt.<n>`` (never deleted) and
    a fresh default structure is returned so the pipeline can keep running.
    """
    path = Path(path)
    if not path.exists():
        return empty_default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        quarantine_index = 0
        quarantine_path = path.with_suffix(path.suffix + f".corrupt.{quarantine_index}")
        while quarantine_path.exists():
            quarantine_index += 1
            quarantine_path = path.with_suffix(path.suffix + f".corrupt.{quarantine_index}")
        shutil.move(str(path), str(quarantine_path))
        logger.error(
            "Cache file %s was malformed (%s). Quarantined to %s and starting from an empty cache.",
            path,
            exc,
            quarantine_path,
        )
        return empty_default


# ---------------------------------------------------------------------------
# Entity lookup cache
# ---------------------------------------------------------------------------


@dataclass
class EntityLookupCache:
    """In-memory + on-disk canonical-id -> Wikibase-QID lookup table."""

    path: Path
    wikibase_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    entities: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, wikibase_url: str = "") -> "EntityLookupCache":
        empty_default = {
            "metadata": {"schema_version": CACHE_SCHEMA_VERSION, "wikibase_url": wikibase_url, "last_updated": None},
            "entities": {},
        }
        raw = _load_json_with_recovery(Path(path), empty_default)
        metadata = raw.get("metadata", {})
        entities = raw.get("entities", {})

        cache = cls(path=Path(path), wikibase_url=wikibase_url, metadata=metadata, entities=entities)

        cached_url = metadata.get("wikibase_url")
        if wikibase_url and cached_url and cached_url != wikibase_url:
            logger.warning(
                "Entity lookup cache at %s was built for wikibase_url=%s but current config targets %s. "
                "Cached QIDs will not be trusted without verification.",
                path,
                cached_url,
                wikibase_url,
            )
        return cache

    def belongs_to_current_instance(self) -> bool:
        cached_url = self.metadata.get("wikibase_url")
        return not cached_url or not self.wikibase_url or cached_url == self.wikibase_url

    def get(self, canonical_id: str) -> Optional[dict[str, Any]]:
        return self.entities.get(canonical_id)

    def set(
        self,
        canonical_id: str,
        qid: str,
        entity_type: str,
        source_identifier: str,
        label: str,
        content_hash: str,
        synced_at: str,
    ) -> None:
        self.entities[canonical_id] = {
            "qid": qid,
            "entity_type": entity_type,
            "source_identifier": source_identifier,
            "label": label,
            "last_seen_owl_hash": content_hash,
            "last_synced_at": synced_at,
        }

    def find_duplicate_qids(self) -> dict[str, list[str]]:
        """Return {qid: [canonical_ids]} for any QID mapped from more than one canonical id."""
        by_qid: dict[str, list[str]] = {}
        for canonical_id, entry in self.entities.items():
            by_qid.setdefault(entry["qid"], []).append(canonical_id)
        return {qid: ids for qid, ids in by_qid.items() if len(ids) > 1}

    def backup(self) -> Optional[Path]:
        """Copy the current on-disk cache file aside before a major update."""
        if not Path(self.path).exists():
            return None
        backup_path = Path(self.path).with_suffix(Path(self.path).suffix + ".bak")
        shutil.copy2(self.path, backup_path)
        return backup_path

    def save(self, last_updated: str) -> None:
        self.metadata = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "wikibase_url": self.wikibase_url,
            "last_updated": last_updated,
        }
        _atomic_write_json(Path(self.path), {"metadata": self.metadata, "entities": self.entities})

    def as_dataframe_records(self) -> list[dict[str, Any]]:
        return [{"canonical_id": canonical_id, **entry} for canonical_id, entry in self.entities.items()]


# ---------------------------------------------------------------------------
# Synchronization state / checkpoint
# ---------------------------------------------------------------------------


@dataclass
class SynchronizationState:
    """Checkpoint describing the progress of a single synchronization run."""

    path: Path
    run_id: str = ""
    owl_file_hash: str = ""
    config_hash: str = ""
    wikibase_url: str = ""
    start_time: str = ""
    last_successful_phase: str = "not_started"
    processed_entity_ids: list[str] = field(default_factory=list)
    failed_actions: list[dict[str, Any]] = field(default_factory=list)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    completion_status: str = "in_progress"

    @classmethod
    def load_or_start(
        cls, path: Path, run_id: str, owl_file_hash: str, config_hash: str, wikibase_url: str, start_time: str
    ) -> "SynchronizationState":
        raw = _load_json_with_recovery(Path(path), {})
        if raw and raw.get("owl_file_hash") == owl_file_hash and raw.get("config_hash") == config_hash \
                and raw.get("wikibase_url") == wikibase_url and raw.get("completion_status") != "completed":
            logger.info("Resuming prior synchronization state (run_id=%s) that matches current inputs.", raw.get("run_id"))
            return cls(
                path=Path(path),
                run_id=raw.get("run_id", run_id),
                owl_file_hash=raw.get("owl_file_hash", owl_file_hash),
                config_hash=raw.get("config_hash", config_hash),
                wikibase_url=raw.get("wikibase_url", wikibase_url),
                start_time=raw.get("start_time", start_time),
                last_successful_phase=raw.get("last_successful_phase", "not_started"),
                processed_entity_ids=list(raw.get("processed_entity_ids", [])),
                failed_actions=list(raw.get("failed_actions", [])),
                pending_actions=list(raw.get("pending_actions", [])),
                completion_status="in_progress",
            )
        return cls(
            path=Path(path),
            run_id=run_id,
            owl_file_hash=owl_file_hash,
            config_hash=config_hash,
            wikibase_url=wikibase_url,
            start_time=start_time,
            last_successful_phase="not_started",
            processed_entity_ids=[],
            failed_actions=[],
            pending_actions=[],
            completion_status="in_progress",
        )

    def mark_phase_complete(self, phase: str) -> None:
        self.last_successful_phase = phase

    def mark_entity_processed(self, canonical_id: str) -> None:
        if canonical_id not in self.processed_entity_ids:
            self.processed_entity_ids.append(canonical_id)

    def record_failed_action(self, action: dict[str, Any]) -> None:
        self.failed_actions.append(action)

    def finish(self, status: str) -> None:
        self.completion_status = status

    def save(self) -> None:
        _atomic_write_json(
            Path(self.path),
            {
                "schema_version": STATE_SCHEMA_VERSION,
                "run_id": self.run_id,
                "owl_file_hash": self.owl_file_hash,
                "config_hash": self.config_hash,
                "wikibase_url": self.wikibase_url,
                "start_time": self.start_time,
                "last_successful_phase": self.last_successful_phase,
                "processed_entity_ids": self.processed_entity_ids,
                "failed_actions": self.failed_actions,
                "pending_actions": self.pending_actions,
                "completion_status": self.completion_status,
            },
        )
