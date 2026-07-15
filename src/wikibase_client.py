"""
Wikibase connectivity layer: authentication, read-only lookups, item
creation, and claim writes -- all wrapped with retry/backoff and structured
logging. This is the only module in the pipeline allowed to perform network
I/O against Wikibase.

Design notes
------------
* Identity lookups use SPARQL (``find_qids_by_identity_value``) against the
  configured identity property, never label search -- see design decision #6
  in the README ("labels are not used as the primary duplicate key").
* Every write method raises :class:`DryRunViolationError` if called while
  ``config.dry_run`` is True, so a bug in planning code can never cause an
  accidental live write.
* Retries are implemented with ``tenacity`` and configured from
  :class:`~src.config.PipelineConfig` (``max_retries``, ``backoff_multiplier``,
  ``sleep_time_seconds``) so retry behavior is centrally controlled rather
  than hardcoded per call site.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import PipelineConfig, WikibaseCredentials

logger = logging.getLogger("owl_wikibase_sync.wikibase_client")

USER_AGENT = "owl-wikibase-sync/1.0 (https://github.com/; contact via .env WIKIBASE_BOT_USERNAME)"


class DryRunViolationError(RuntimeError):
    """Raised if a write method is invoked while the pipeline is in dry-run mode."""


class WikibaseConnectionError(RuntimeError):
    """Raised when the Wikibase API/SPARQL endpoint is unreachable or rejects auth."""


class RetryableTransportError(RuntimeError):
    """Wraps transient HTTP errors (429, 5xx, timeouts) so tenacity can retry them."""


@dataclass
class ConnectionStatus:
    api_available: bool = False
    authenticated: bool = False
    bot_permissions: bool = False
    sparql_available: bool = False
    wikibase_url: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ready_for_dry_run(self) -> bool:
        return self.api_available

    @property
    def ready_for_live_writes(self) -> bool:
        return self.api_available and self.authenticated and self.bot_permissions


def _entity_uri(wikibase_url: str, qid: str) -> str:
    return f"{wikibase_url.rstrip('/')}/entity/{qid}"


def _direct_predicate_uri(wikibase_url: str, pid: str) -> str:
    return f"{wikibase_url.rstrip('/')}/prop/direct/{pid}"


def _escape_sparql_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class WikibaseClient:
    """Thin, retrying wrapper around ``wikibaseintegrator`` and the SPARQL endpoint."""

    def __init__(self, credentials: WikibaseCredentials, config: PipelineConfig):
        self.credentials = credentials
        self.config = config
        self._login = None
        self._wbi = None
        self.api_call_count = 0
        self.retry_count = 0

    # -- retry helper ------------------------------------------------------

    def _run_with_retries(self, description: str, fn, *args, **kwargs):
        retrying = Retrying(
            stop=stop_after_attempt(max(1, self.config.max_retries)),
            wait=wait_exponential(multiplier=self.config.sleep_time_seconds, exp_base=self.config.backoff_multiplier),
            retry=retry_if_exception_type((RetryableTransportError, requests.exceptions.RequestException)),
            reraise=True,
        )
        attempt_number = 0
        for attempt in retrying:
            attempt_number += 1
            with attempt:
                if attempt_number > 1:
                    self.retry_count += 1
                    logger.warning("Retrying %s (attempt %d/%d)", description, attempt_number, self.config.max_retries)
                self.api_call_count += 1
                return fn(*args, **kwargs)
        raise WikibaseConnectionError(f"Exhausted retries for {description}")  # pragma: no cover - Retrying always raises/returns

    # -- connection / auth ---------------------------------------------------

    def connect(self, require_write: bool = False) -> ConnectionStatus:
        """Verify API availability, authentication, bot permissions, and SPARQL access.

        Dry-run planning only needs ``api_available``; live synchronization
        requires ``ready_for_live_writes``.
        """
        status = ConnectionStatus(wikibase_url=self.credentials.wikibase_url)

        if not self.credentials.has_read_endpoints:
            status.errors.append("WIKIBASE_URL / WIKIBASE_API_URL are not configured in .env")
            return status

        try:
            response = self._run_with_retries(
                "API availability check",
                requests.get,
                self.credentials.api_url,
                params={"action": "query", "meta": "siteinfo", "format": "json"},
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            status.api_available = True
        except Exception as exc:  # noqa: BLE001 - reported as a structured connection error, not raised
            status.errors.append(f"API not reachable: {exc}")
            return status

        if self.credentials.bot_username and self.credentials.bot_password:
            try:
                from wikibaseintegrator import wbi_config, wbi_login

                wbi_config.config["MEDIAWIKI_API_URL"] = self.credentials.api_url
                wbi_config.config["USER_AGENT"] = USER_AGENT
                self._login = wbi_login.Login(
                    user=self.credentials.bot_username,
                    password=self.credentials.bot_password,
                    mediawiki_api_url=self.credentials.api_url,
                    user_agent=USER_AGENT,
                )
                status.authenticated = True
                status.bot_permissions = True  # Login raises LoginError on failure; success implies edit rights
            except Exception as exc:  # noqa: BLE001
                status.errors.append(f"Authentication failed: {exc}")
        elif require_write:
            status.errors.append("Bot credentials are not configured; live writes are not possible.")

        if self.credentials.sparql_url:
            try:
                response = self._run_with_retries(
                    "SPARQL availability check",
                    requests.get,
                    self.credentials.sparql_url,
                    params={"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1", "format": "json"},
                    headers={"Accept": "application/sparql-results+json"},
                    timeout=self.config.request_timeout_seconds,
                )
                status.sparql_available = response.ok
            except Exception as exc:  # noqa: BLE001
                status.errors.append(f"SPARQL endpoint not reachable: {exc}")

        return status

    def _get_wbi(self):
        if self._wbi is None:
            from wikibaseintegrator import WikibaseIntegrator, wbi_config

            wbi_config.config["MEDIAWIKI_API_URL"] = self.credentials.api_url
            wbi_config.config["USER_AGENT"] = USER_AGENT
            self._wbi = WikibaseIntegrator(login=self._login)
        return self._wbi

    # -- read-only lookups -----------------------------------------------------

    def find_qids_by_identity_value(self, pid: str, value: str) -> list[str]:
        """Look up items whose identity property (e.g. ``ontology_iri``) equals ``value``.

        This is the *only* sanctioned duplicate-detection mechanism (besides
        the local cache) -- label search is never used for identity.
        """
        if not self.credentials.sparql_url:
            logger.warning("SPARQL endpoint not configured; cannot perform remote identity lookup for pid=%s", pid)
            return []

        predicate_uri = _direct_predicate_uri(self.credentials.wikibase_url, pid)
        escaped_value = _escape_sparql_literal(value)
        query = f'SELECT ?item WHERE {{ ?item <{predicate_uri}> "{escaped_value}" }}'

        def _query():
            response = requests.get(
                self.credentials.sparql_url,
                params={"query": query, "format": "json"},
                headers={"Accept": "application/sparql-results+json"},
                timeout=self.config.request_timeout_seconds,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableTransportError(f"SPARQL endpoint returned {response.status_code}")
            response.raise_for_status()
            return response.json()

        try:
            result = self._run_with_retries(f"SPARQL identity lookup ({pid})", _query)
        except Exception as exc:  # noqa: BLE001
            logger.error("SPARQL identity lookup failed for pid=%s value=%s: %s", pid, value, exc)
            return []

        bindings = result.get("results", {}).get("bindings", [])
        qids = []
        for binding in bindings:
            uri = binding.get("item", {}).get("value", "")
            qid = uri.rsplit("/", 1)[-1]
            if qid:
                qids.append(qid)
        return qids

    def find_qids_by_identity_values_batch(self, pid: str, values: list[str]) -> dict[str, list[str]]:
        """Look up many identity values in a single SPARQL query using a VALUES clause.

        This is what Pass A actually uses for entity resolution (chunked by
        ``config.batch_size``): a first run over hundreds of entities issuing
        one HTTP round-trip *per entity* is impractically slow against a real
        endpoint. Batching keeps traffic to roughly
        ``len(values) / config.batch_size`` requests instead of
        ``len(values)`` (see README, "Traffic Control and Reliability").

        Returns a ``{value: [qid, ...]}`` mapping; values with zero matches
        are simply absent from the returned dict (callers should treat a
        missing key as "no match found").
        """
        if not values:
            return {}
        if not self.credentials.sparql_url:
            logger.warning("SPARQL endpoint not configured; cannot perform batched identity lookup for pid=%s", pid)
            return {}

        predicate_uri = _direct_predicate_uri(self.credentials.wikibase_url, pid)
        values_clause = " ".join(f'"{_escape_sparql_literal(v)}"' for v in values)
        query = (
            f"SELECT ?item ?value WHERE {{ VALUES ?value {{ {values_clause} }} "
            f"?item <{predicate_uri}> ?value . }}"
        )

        def _query():
            response = requests.get(
                self.credentials.sparql_url,
                params={"query": query, "format": "json"},
                headers={"Accept": "application/sparql-results+json"},
                timeout=self.config.request_timeout_seconds,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableTransportError(f"SPARQL endpoint returned {response.status_code}")
            response.raise_for_status()
            return response.json()

        try:
            result = self._run_with_retries(f"SPARQL batched identity lookup ({pid}, {len(values)} values)", _query)
        except Exception as exc:  # noqa: BLE001
            logger.error("Batched SPARQL identity lookup failed for pid=%s (%d values): %s", pid, len(values), exc)
            return {}

        matches: dict[str, list[str]] = {}
        for binding in result.get("results", {}).get("bindings", []):
            value = binding.get("value", {}).get("value", "")
            uri = binding.get("item", {}).get("value", "")
            qid = uri.rsplit("/", 1)[-1]
            if value and qid:
                matches.setdefault(value, []).append(qid)
        return matches

    def get_item(self, qid: str):
        """Fetch a live ``ItemEntity`` for claim/label comparison."""
        wbi = self._get_wbi()
        return self._run_with_retries(f"get_item({qid})", wbi.item.get, entity_id=qid)

    def get_existing_claim_values(self, qid: str) -> dict[str, list[str]]:
        """Return {pid: [comparable_value_strings]} for an existing item's claims."""
        item = self.get_item(qid)
        snapshot: dict[str, list[str]] = {}
        for pid, claim_list in item.claims.get_json().items():
            values = []
            for claim in claim_list:
                mainsnak = claim.get("mainsnak", {})
                datavalue = mainsnak.get("datavalue", {})
                value = datavalue.get("value")
                if isinstance(value, dict):
                    values.append(value.get("id") or value.get("text") or value.get("time") or str(value))
                else:
                    values.append(str(value))
            snapshot[pid] = values
        return snapshot

    # -- writes ------------------------------------------------------------

    def _guard_dry_run(self, action: str) -> None:
        if self.config.dry_run:
            raise DryRunViolationError(f"Refusing to execute '{action}': pipeline is in DRY_RUN mode.")

    def create_item(
        self,
        labels: dict[str, str],
        descriptions: dict[str, str],
        aliases: dict[str, list[str]],
    ) -> str:
        """Create a new Wikibase item and return its QID. Never called in dry-run mode."""
        self._guard_dry_run("create_item")
        wbi = self._get_wbi()

        def _create():
            item = wbi.item.new()
            for language, text in labels.items():
                item.labels.set(language, text)
            for language, text in descriptions.items():
                item.descriptions.set(language, text)
            for language, values in aliases.items():
                item.aliases.set(language, values)
            written = item.write()
            return written.id

        qid = self._run_with_retries("create_item", _create)
        time.sleep(self.config.sleep_time_seconds)
        return qid

    def update_metadata(
        self,
        qid: str,
        labels: dict[str, str],
        descriptions: dict[str, str],
        aliases: dict[str, list[str]],
    ) -> None:
        """Write labels/descriptions/aliases onto an existing item.

        Callers (``synchronizer.py``) are responsible for applying the
        configured ``METADATA_SYNC_MODE`` policy *before* calling this method
        -- by the time this runs, ``labels``/``descriptions``/``aliases``
        already contain exactly the values that should be written.
        """
        self._guard_dry_run("update_metadata")
        wbi = self._get_wbi()

        def _update():
            from wikibaseintegrator.wbi_enums import ActionIfExists

            item = wbi.item.get(entity_id=qid)
            for language, text in labels.items():
                item.labels.set(language, text)
            for language, text in descriptions.items():
                item.descriptions.set(language, text)
            for language, values in aliases.items():
                # APPEND_OR_REPLACE is safe here because the planner has already
                # diffed against the item's existing aliases (see sync_planner.py)
                # -- everything in `values` is a genuinely new alias to add.
                item.aliases.set(language, values, action_if_exists=ActionIfExists.APPEND_OR_REPLACE)
            item.write()

        self._run_with_retries(f"update_metadata({qid})", _update)
        time.sleep(self.config.sleep_time_seconds)

    def add_claim(self, qid: str, pid: str, wikibase_datatype: str, datavalue: dict[str, Any]) -> None:
        """Add a single claim to an existing item using an already-built datavalue."""
        self._guard_dry_run("add_claim")
        wbi = self._get_wbi()

        def _add():
            from wikibaseintegrator import datatypes as wbi_datatypes

            item = wbi.item.get(entity_id=qid)
            claim = self._build_claim(wbi_datatypes, pid, wikibase_datatype, datavalue)
            item.claims.add(claim)
            item.write()

        self._run_with_retries(f"add_claim({qid},{pid})", _add)
        time.sleep(self.config.sleep_time_seconds)

    @staticmethod
    def _build_claim(wbi_datatypes, pid: str, wikibase_datatype: str, datavalue: dict[str, Any]):
        value = datavalue["value"]
        if wikibase_datatype == "wikibase-item":
            return wbi_datatypes.Item(prop_nr=pid, value=value["id"])
        if wikibase_datatype == "string":
            return wbi_datatypes.String(prop_nr=pid, value=value)
        if wikibase_datatype == "external-id":
            return wbi_datatypes.ExternalID(prop_nr=pid, value=value)
        if wikibase_datatype == "url":
            return wbi_datatypes.URL(prop_nr=pid, value=value)
        if wikibase_datatype == "commonsMedia":
            return wbi_datatypes.CommonsMedia(prop_nr=pid, value=value)
        if wikibase_datatype == "monolingualtext":
            return wbi_datatypes.MonolingualText(prop_nr=pid, text=value["text"], language=value["language"])
        if wikibase_datatype == "time":
            return wbi_datatypes.Time(
                prop_nr=pid,
                time=value["time"],
                precision=value["precision"],
                before=value["before"],
                after=value["after"],
                timezone=value["timezone"],
                calendarmodel=value["calendarmodel"],
            )
        if wikibase_datatype == "quantity":
            return wbi_datatypes.Quantity(prop_nr=pid, amount=value["amount"], unit=value.get("unit"))
        raise ValueError(f"Unsupported wikibase_datatype for claim construction: {wikibase_datatype}")

    def get_traffic_stats(self) -> dict[str, int]:
        return {"api_calls": self.api_call_count, "retries": self.retry_count}
