"""
Identifier normalization helpers.

Real-world OWL exports (this pipeline was built against a WebProtege export)
are not always internally consistent about how resources are named. The same
conceptual resource can appear under more than one URI shape, for example:

    #Contribution
    #http://purl.obolibrary.org/obo/BFO_0000015
    http://purl.obolibrary.org/obo/BFO_0000015

The second form is a fragment (``#...``) whose fragment text is itself a full
absolute URI -- this happens when an RDF/XML ``rdf:about`` attribute contains
an absolute URI that an authoring tool mistakenly prefixed with ``#``. After
RDF/XML parsing (which resolves the ``#`` fragment against ``xml:base``), the
result is a URI like::

    http://tib.eu/slr#http://purl.obolibrary.org/obo/BFO_0000015

which does *not* string-match the plain absolute form
``http://purl.obolibrary.org/obo/BFO_0000015`` even though both refer to the
same real-world concept. ``normalize_resource_identifier`` detects this
pattern and recovers the embedded absolute URI so both shapes produce the
same canonical identifier, while ``original_id`` (stored separately on every
:class:`~src.models.OntologyEntity`) always preserves the untouched value for
traceability.
"""

from __future__ import annotations

_URI_SCHEMES = ("http://", "https://")


def normalize_resource_identifier(raw_identifier: str) -> str:
    """Return a canonical identifier suitable for identity matching.

    If ``raw_identifier`` contains an embedded absolute URI *after* its own
    start (e.g. a fragment that is itself a full URI), the embedded URI is
    returned. Otherwise the identifier is returned unchanged.

    Examples
    --------
    >>> normalize_resource_identifier("http://tib.eu/slr#Contribution")
    'http://tib.eu/slr#Contribution'
    >>> normalize_resource_identifier(
    ...     "http://tib.eu/slr#http://purl.obolibrary.org/obo/BFO_0000015"
    ... )
    'http://purl.obolibrary.org/obo/BFO_0000015'
    >>> normalize_resource_identifier("http://purl.obolibrary.org/obo/BFO_0000015")
    'http://purl.obolibrary.org/obo/BFO_0000015'
    """
    text = str(raw_identifier).strip()
    for scheme in _URI_SCHEMES:
        # Search from index 1 so the identifier's own leading scheme (at
        # index 0, the overwhelmingly common case) is never mistaken for an
        # embedded URI -- only a *second*, later occurrence counts.
        embedded_index = text.find(scheme, 1)
        if embedded_index > 0:
            return text[embedded_index:]
    return text


def get_local_name(identifier: str) -> str:
    """Extract a human-oriented local name from a (canonical) identifier.

    Prefers the fragment after the last ``#``; falls back to the last
    ``/``-delimited path segment; falls back to the identifier itself.
    """
    text = str(identifier).strip()
    if "#" in text:
        candidate = text.rsplit("#", 1)[-1]
        if candidate:
            return candidate
    if "/" in text:
        candidate = text.rstrip("/").rsplit("/", 1)[-1]
        if candidate:
            return candidate
    return text


def is_probably_malformed_uri(identifier: str) -> bool:
    """Heuristic: flag identifiers that embed a second absolute URI or scheme.

    Used by preflight validation to surface authoring inconsistencies rather
    than silently normalizing them away without a trace.
    """
    text = str(identifier).strip()
    occurrences = sum(text.count(scheme) for scheme in _URI_SCHEMES)
    return occurrences > 1
