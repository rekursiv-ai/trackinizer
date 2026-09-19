"""A real embedder that speaks the OpenAI-compatible ``/v1/embeddings`` API.

Why HTTP rather than an in-process model: ``pyproject.toml`` asks for no new
dependencies, and a model library (torch, onnxruntime, sentence-transformers)
would be a large one. ``httpx2`` is already a core dependency and
``api/oauth_routes.py`` already makes outbound calls with it, so this adds
capability without adding anything to install.

Why the OpenAI shape specifically: it is the de facto standard for embedding
endpoints, spoken by OpenAI, Jina, Together, vLLM, LM Studio and **Ollama**.
One implementation therefore covers hosted and fully-local deployments alike --
pointing ``--embedder-url`` at a local Ollama keeps trackinizer offline and
key-free, which matters for a tool whose default substrate is an in-process
database.

Not the default. ``StubEmbedder`` remains what ships, so an install with no
configuration behaves exactly as before: no network, no key, no download.
"""

from __future__ import annotations

from typing import Final, cast

import httpx2

from trackinizer.server.embedder import StubEmbedder
from trackinizer.types.errors import ConflictError


__all__ = [
    "HttpEmbedder",
]


# Long enough for a cold local model to load, short enough that a wedged
# endpoint fails a submit instead of hanging it. Submits embed OUTSIDE their
# transaction (``store/submit.py``, ``store/edit.py``), so a slow response
# delays one request rather than holding a row lock.
DEFAULT_TIMEOUT_SECONDS: Final = 30.0


# Module-level so a test can monkey-patch one helper and inject an
# ``httpx2.MockTransport``, the pattern ``api/oauth_routes.py`` established.
def _http_client(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> httpx2.AsyncClient:
    """Build the client used for embedding round-trips."""
    return httpx2.AsyncClient(timeout=timeout_seconds)


class HttpEmbedder:
    """Embed text via an OpenAI-compatible ``/v1/embeddings`` endpoint.

    ``name`` is what lands in ``inquiry_embeddings.model``, so it identifies a
    vector space, not just a vendor. It defaults to the remote model id and
    should be changed whenever anything that alters the geometry changes --
    a different model, a different dimension count -- because
    ``Store.find_similar`` scopes every query to one ``name`` and mixing two
    geometries under one label would compare incomparable vectors.

    ``dimensions`` is sent to the endpoint rather than truncated locally:
    models in the ``text-embedding-3`` family and Jina v3 reduce by a learned
    projection, which preserves more than slicing the tail off a vector does.
    An endpoint that ignores the field (many local servers do) must therefore
    be paired with a model whose native width already matches, which
    :meth:`embed` verifies on every response rather than trusting.
    """

    is_semantic = True

    # Read by ``Store._backfill_embeddings``, which runs inside bootstrap's
    # advisory-locked transaction: a remote embedder would turn every boot
    # into N network round-trips, so the bulk pass is opt-in there.
    is_remote = True

    def __init__(
        self,
        *,
        url: str,
        model: str,
        api_key: str | None = None,
        name: str | None = None,
        dim: int = StubEmbedder.dim,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not url.strip():
            raise ConflictError("embedder url must not be blank")
        if not model.strip():
            raise ConflictError("embedder model must not be blank")
        self._url = url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self.name = name or model
        self.dim = dim

    async def embed(self, text: str) -> list[float]:
        """Return ``text`` as a unit vector of length :attr:`dim`.

        Raises:
          ConflictError: The endpoint failed, returned an unreadable body, or
            returned a vector of the wrong width. Every case is a
            configuration fault the operator must see, not something to paper
            over with a zero vector -- a silently wrong vector would make
            search quietly bad rather than visibly broken.

        """
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, object] = {
            "input": text,
            "model": self._model,
            "encoding_format": "float",
            "dimensions": self.dim,
        }
        async with _http_client(timeout_seconds=self._timeout_seconds) as client:
            try:
                response = await client.post(
                    f"{self._url}/embeddings",
                    json=payload,
                    headers=headers,
                )
            except httpx2.HTTPError as exc:
                raise ConflictError(
                    f"embedding request to {self._url} failed: {exc}",
                ) from exc
        if response.status_code != 200:
            raise ConflictError(
                f"embedding endpoint {self._url} returned "
                f"{response.status_code}: {response.text[:200]}",
            )
        return self._vector_from(response)

    def _vector_from(self, response: httpx2.Response) -> list[float]:
        """Pull the vector out of an OpenAI-shaped body, verifying its width."""
        try:
            body = response.json()
        except ValueError as exc:
            raise ConflictError(
                f"embedding endpoint {self._url} returned a non-JSON body",
            ) from exc
        if not isinstance(body, dict):
            raise ConflictError(
                f"embedding endpoint {self._url} returned {type(body).__name__}, "
                "expected an object",
            )
        # response.json() is typed Any (basedpyright cannot know the shape of
        # someone else's HTTP response), so every access below is cast rather
        # than left to propagate Unknown through isinstance narrowing alone.
        body_obj = cast("dict[str, object]", body)
        data = body_obj.get("data")
        if not isinstance(data, list) or not data:
            raise ConflictError(
                f"embedding endpoint {self._url} returned no data entries",
            )
        data = cast("list[object]", data)
        first = data[0]
        if not isinstance(first, dict):
            raise ConflictError(
                f"embedding endpoint {self._url} returned no embedding vector",
            )
        first_obj = cast("dict[str, object]", first)
        raw = first_obj.get("embedding")
        if not isinstance(raw, list):
            raise ConflictError(
                f"embedding endpoint {self._url} returned no embedding vector",
            )
        raw = cast("list[object]", raw)
        # Width is checked here, not at construction: an endpoint may accept
        # the ``dimensions`` field and ignore it, which would otherwise surface
        # much later as a pgvector dimension error mid-transaction.
        if len(raw) != self.dim:
            raise ConflictError(
                f"embedding endpoint {self._url} returned {len(raw)} dimensions, "
                f"expected {self.dim}; pick a model of that width or one that "
                "honours the 'dimensions' request field",
            )
        try:
            vector = [float(cast("int | float | str", x)) for x in raw]
        except (TypeError, ValueError) as exc:
            raise ConflictError(
                f"embedding endpoint {self._url} returned a non-numeric vector",
            ) from exc
        # Normalize so cosine distance and inner product agree, and so vectors
        # from an endpoint that does not normalize are comparable with ones
        # that do. A zero vector cannot be normalized and is never a valid
        # embedding, so it is reported rather than divided by one.
        norm = sum(x * x for x in vector) ** 0.5
        if norm == 0.0:
            raise ConflictError(
                f"embedding endpoint {self._url} returned a zero vector",
            )
        return [x / norm for x in vector]
