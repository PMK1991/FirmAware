"""Azure AI Search as the evidence index, with Foundry embeddings for it.

The local index embeds 377 chunks with BGE-small on every start: a minute of
CPU and a gigabyte of torch. Here the chunks are embedded once, with
text-embedding-3-small on the Foundry resource, and live in a Search index; a
query costs one embedding call and one search call, and nothing heavy loads.

Everything is keyless. Tokens come from DefaultAzureCredential (`az login` on a
laptop, a managed identity in Azure), and the only SDK is azure-identity,
imported when a token is first needed, as io.py does for storage. Requests are
plain HTTP to the documented REST API, so no Search SDK is involved.

Promotion follows the plan: build a new timestamped index, grade it with the
eval, and only then repoint the alias the readers use. Rollback is repointing
it back. Aliases exist only in the preview REST version, so that is the one
used throughout; the GA 2024-07-01 version rejects /aliases outright.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np

SEARCH_API = "2025-05-01-preview"
SEARCH_SCOPE = "https://search.azure.com/.default"
FOUNDRY_SCOPE = "https://cognitiveservices.azure.com/.default"
DEFAULT_ALIAS = "firmaware-corpus"
EMBEDDING_DIMENSIONS = 1536  # text-embedding-3-small
EMBED_BATCH = 64
UPLOAD_BATCH = 100
# Every field of a chunk record except its vector, so a hit from Search has
# exactly the shape the routes, the gate and the page read from LocalIndex.
CHUNK_FIELDS = [
    "chunk_id", "doc_id", "chunk_index", "chunk_kind", "document_type",
    "retrieval_mode", "vendor", "vendors", "evidence_strength", "provenance",
    "source_format", "filename", "text",
]


class SearchError(RuntimeError):
    """A refused or failed call to Search or Foundry, with the service's reason."""


def _tokens(scope: str) -> Callable[[], str]:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    return get_bearer_token_provider(DefaultAzureCredential(), scope)


def _call(url: str, token: Callable[[], str], body: Any = None, method: str = "GET") -> Any:
    request = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method=method,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise SearchError(f"{method} {url.split('?')[0]} -> {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise SearchError(f"could not reach {url.split('?')[0]}: {error}") from None
    return json.loads(payload) if payload else {}


def foundry_embedder(endpoint: str, deployment: str) -> Callable[[Sequence[str]], np.ndarray]:
    """text-embedding-3-small on Foundry, batched; vectors arrive unit-length."""
    token = _tokens(FOUNDRY_SCOPE)
    url = endpoint.rstrip("/") + "/openai/v1/embeddings"

    def embed(texts: Sequence[str]) -> np.ndarray:
        vectors: list[list[float]] = []
        texts = list(texts)
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start : start + EMBED_BATCH]
            data = _call(url, token, {"model": deployment, "input": batch}, "POST")["data"]
            vectors += [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]
        return np.asarray(vectors, dtype=np.float32)

    return embed


def index_definition(name: str, dimensions: int = EMBEDDING_DIMENSIONS) -> dict[str, Any]:
    def text(field: str, **flags: bool) -> dict[str, Any]:
        return {"name": field, "type": "Edm.String", **flags}

    return {
        "name": name,
        "fields": [
            text("chunk_id", key=True, filterable=True),
            text("doc_id", filterable=True),
            {"name": "chunk_index", "type": "Edm.Int32", "filterable": True, "sortable": True},
            text("chunk_kind", filterable=True),
            text("document_type", filterable=True, facetable=True),
            text("retrieval_mode", filterable=True),
            text("vendor", filterable=True),
            {"name": "vendors", "type": "Collection(Edm.String)", "filterable": True},
            text("evidence_strength", filterable=True),
            text("provenance", filterable=True),
            text("source_format", filterable=True),
            text("filename"),
            text("text", searchable=True),
            {
                "name": "vector",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "dimensions": dimensions,
                "vectorSearchProfile": "cosine",
            },
        ],
        "vectorSearch": {
            "algorithms": [{"name": "hnsw", "kind": "hnsw",
                            "hnswParameters": {"metric": "cosine"}}],
            "profiles": [{"name": "cosine", "algorithm": "hnsw"}],
        },
    }


class SearchAdmin:
    """Build, promote and prune the corpus indexes behind one alias."""

    def __init__(self, endpoint: str, alias: str = DEFAULT_ALIAS) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.alias = alias
        self._token = _tokens(SEARCH_SCOPE)

    def _url(self, path: str) -> str:
        return f"{self.endpoint}/{path}?api-version={SEARCH_API}"

    def build(self, chunks: list[dict], embed: Callable[[Sequence[str]], np.ndarray]) -> str:
        """A new, unpromoted index holding every chunk and its vector."""
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        name = f"{self.alias}-{stamp}"
        vectors = embed([chunk["text"] for chunk in chunks])
        _call(self._url(f"indexes/{name}"), self._token,
              index_definition(name, vectors.shape[1]), "PUT")
        documents = [
            {"@search.action": "upload", **{f: chunk.get(f) for f in CHUNK_FIELDS},
             "vector": vector.tolist()}
            for chunk, vector in zip(chunks, vectors)
        ]
        for start in range(0, len(documents), UPLOAD_BATCH):
            result = _call(self._url(f"indexes/{name}/docs/index"), self._token,
                           {"value": documents[start : start + UPLOAD_BATCH]}, "POST")
            failed = [item["key"] for item in result.get("value", []) if not item.get("status")]
            if failed:
                raise SearchError(f"{name}: {len(failed)} chunk(s) not indexed: {failed[:5]}")
        return name

    def current(self) -> str | None:
        try:
            alias = _call(self._url(f"aliases/{self.alias}"), self._token)
        except SearchError as error:
            if "-> 404" in str(error):
                return None
            raise
        return alias["indexes"][0]

    def promote(self, index: str) -> str | None:
        """Point the alias at `index`; returns what it pointed at before."""
        previous = self.current()
        _call(self._url(f"aliases/{self.alias}"), self._token,
              {"name": self.alias, "indexes": [index]}, "PUT")
        return previous

    def indexes(self) -> list[str]:
        names = [i["name"] for i in _call(self._url("indexes"), self._token)["value"]]
        return sorted(n for n in names if n.startswith(f"{self.alias}-"))

    def prune(self, keep: int = 2) -> list[str]:
        """Delete all but the newest `keep` indexes, never the one the alias serves.

        The Free tier holds three indexes; keeping two leaves room to build the
        next while the live one and its predecessor (the rollback) both remain.
        """
        live = self.current()
        removable = [n for n in self.indexes() if n != live][: max(0, len(self.indexes()) - keep)]
        for name in removable:
            _call(self._url(f"indexes/{name}"), self._token, method="DELETE")
        return removable


class AzureSearchBackend:
    """The SearchBackend the routes use, served from an Azure AI Search index."""

    def __init__(self, endpoint: str, index: str,
                 embed: Callable[[Sequence[str]], np.ndarray]) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.index = index
        self._embed = embed
        self._token = _tokens(SEARCH_SCOPE)
        self._query: tuple[str, list[float]] | None = None
        self._documents: dict[str, list[dict]] = {}

    def _search(self, body: dict[str, Any]) -> list[dict]:
        url = f"{self.endpoint}/indexes/{urllib.parse.quote(self.index)}/docs/search?api-version={SEARCH_API}"
        return _call(url, self._token, {"select": ",".join(CHUNK_FIELDS), **body}, "POST")["value"]

    def _query_vector(self, query: str) -> list[float]:
        # One embedding per query, however many vector routes use it.
        if self._query is None or self._query[0] != query:
            self._query = (query, self._embed([query])[0].tolist())
        return self._query[1]

    def search(self, query: str, doc_ids: frozenset[str], k: int) -> list[tuple[dict, float]]:
        if not doc_ids or k <= 0:
            return []
        ids = "|".join(sorted(doc_ids))
        hits = self._search({
            "top": k,
            "filter": f"search.in(doc_id, '{ids}', '|')",
            # Filter first, then rank: a post-filter would rank the whole corpus
            # and could return fewer than k of the documents this route allows.
            "vectorFilterMode": "preFilter",
            "vectorQueries": [{"kind": "vector", "vector": self._query_vector(query),
                               "fields": "vector", "k": k}],
        })
        return [({f: hit.get(f) for f in CHUNK_FIELDS}, float(hit["@search.score"])) for hit in hits]

    def chunks_of(self, doc_id: str) -> list[dict]:
        # Documents only change when a new index is promoted, so each is
        # fetched once per backend; the vectorless routes ask for the same few.
        if doc_id not in self._documents:
            hits = self._search({"search": "*", "filter": f"doc_id eq '{doc_id}'",
                                 "orderby": "chunk_index asc", "top": 1000})
            self._documents[doc_id] = [{f: hit.get(f) for f in CHUNK_FIELDS} for hit in hits]
        return self._documents[doc_id]


def backend_from_env() -> AzureSearchBackend | None:
    """The Search backend when .env configures one; None means use the local index."""
    search = os.getenv("FIRMAWARE_SEARCH_ENDPOINT")
    foundry = os.getenv("FIRMAWARE_FOUNDRY_ENDPOINT")
    embedding = os.getenv("FIRMAWARE_FOUNDRY_EMBEDDING_DEPLOYMENT")
    if not (search and foundry and embedding):
        return None
    alias = os.getenv("FIRMAWARE_SEARCH_INDEX", DEFAULT_ALIAS)
    return AzureSearchBackend(search, alias, foundry_embedder(foundry, embedding))
