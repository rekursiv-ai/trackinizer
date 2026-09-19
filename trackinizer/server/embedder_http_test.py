"""``HttpEmbedder`` against a mocked endpoint.

Every case runs through an ``httpx2.MockTransport`` injected by patching the
module-level ``_http_client`` helper -- the pattern ``api/oauth_routes.py``
established -- so the suite never makes a network call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import json

import httpx2
import pytest

from trackinizer.server import embedder_http
from trackinizer.server.embedder import StubEmbedder
from trackinizer.server.embedder_http import HttpEmbedder
from trackinizer.types.errors import ConflictError


if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager


def _ok_body(vector: list[float]) -> dict[str, object]:
    """Return an OpenAI-shaped embeddings response."""
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": vector}],
        "model": "test-model",
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


def _patched(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> AbstractContextManager[object]:
    """Patch ``_http_client`` to route through ``handler``."""
    transport = httpx2.MockTransport(handler)

    def build(*, timeout_seconds: float = 30.0) -> httpx2.AsyncClient:
        del timeout_seconds
        return httpx2.AsyncClient(transport=transport)

    return patch.object(embedder_http, "_http_client", build)


def _embedder(
    *,
    url: str = "https://example.invalid/v1",
    model: str = "test-model",
    api_key: str | None = None,
    name: str | None = None,
) -> HttpEmbedder:
    """Build an embedder pointed at a host that cannot resolve.

    Every request is intercepted by ``_patched``, so the URL only has to be
    well-formed -- ``.invalid`` is reserved by RFC 2606 precisely so a leaked
    request fails fast instead of reaching a real host.
    """
    return HttpEmbedder(url=url, model=model, api_key=api_key, name=name)


@pytest.mark.asyncio
async def test_returns_a_normalized_vector_of_the_expected_width() -> None:
    """The happy path: an OpenAI-shaped body becomes a unit vector."""
    raw = [3.0, 4.0] + [0.0] * (StubEmbedder.dim - 2)  # norm 5, so easy to check

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json=_ok_body(raw))

    with _patched(handler):
        vector = await _embedder().embed("momentum decays")

    assert len(vector) == StubEmbedder.dim
    assert vector[0] == pytest.approx(0.6)
    assert vector[1] == pytest.approx(0.8)
    assert sum(x * x for x in vector) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_sends_the_openai_request_shape() -> None:
    """Input, model, and the requested width must all reach the endpoint."""
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        # ``json.loads`` is typed ``Any``; cast rather than annotate, so the
        # narrowing is explicit and the strict typecheckers stay quiet.
        body = cast("dict[str, object]", json.loads(request.content))
        seen.update(body)
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        return httpx2.Response(
            200,
            json=_ok_body([1.0] + [0.0] * (StubEmbedder.dim - 1)),
        )

    with _patched(handler):
        await _embedder(api_key="sk-test").embed("a claim")

    assert seen["input"] == "a claim"
    assert seen["model"] == "test-model"
    assert seen["dimensions"] == StubEmbedder.dim
    assert seen["path"] == "/v1/embeddings"
    assert seen["auth"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_omits_authorization_when_no_key_is_configured() -> None:
    """A local endpoint (Ollama, vLLM) needs no credential."""
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx2.Response(
            200,
            json=_ok_body([1.0] + [0.0] * (StubEmbedder.dim - 1)),
        )

    with _patched(handler):
        await _embedder().embed("a claim")

    assert seen["auth"] is None


@pytest.mark.asyncio
async def test_a_wrong_width_is_reported_not_stored() -> None:
    """An endpoint that ignores ``dimensions`` must fail loudly.

    Otherwise the mismatch surfaces much later as a pgvector dimension error
    inside a submit's transaction.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json=_ok_body([0.1] * 768))

    with _patched(handler), pytest.raises(ConflictError, match="768 dimensions"):
        await _embedder().embed("a claim")


@pytest.mark.asyncio
async def test_a_zero_vector_is_rejected() -> None:
    """Zero cannot be normalized and is never a valid embedding."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json=_ok_body([0.0] * StubEmbedder.dim))

    with _patched(handler), pytest.raises(ConflictError, match="zero vector"):
        await _embedder().embed("a claim")


@pytest.mark.asyncio
async def test_a_non_200_carries_the_status_and_body() -> None:
    """The operator needs to see what the endpoint actually said."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(401, text="invalid api key")

    with _patched(handler), pytest.raises(ConflictError, match="401") as caught:
        await _embedder().embed("a claim")

    assert "invalid api key" in str(caught.value)


@pytest.mark.asyncio
async def test_a_transport_failure_is_a_conflict_not_a_raw_httpx_error() -> None:
    """Callers handle ConflictError; they should not have to know httpx2."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        raise httpx2.ConnectError("connection refused")

    with _patched(handler), pytest.raises(ConflictError, match="failed"):
        await _embedder().embed("a claim")


@pytest.mark.asyncio
async def test_a_malformed_body_is_reported() -> None:
    """A JSON body without the expected shape is a configuration fault."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json={"object": "list", "data": []})

    with _patched(handler), pytest.raises(ConflictError, match="no data entries"):
        await _embedder().embed("a claim")


def test_declares_itself_semantic_and_remote() -> None:
    """``find_similar`` gates on the first; the boot backfill on the second."""
    embedder = _embedder()

    assert embedder.is_semantic is True
    assert embedder.is_remote is True


def test_name_defaults_to_the_model_and_can_be_overridden() -> None:
    """``name`` labels a vector space, so it must be settable independently."""
    assert _embedder().name == "test-model"
    assert _embedder(name="bge-384-v1").name == "bge-384-v1"


def test_blank_url_or_model_fails_at_construction() -> None:
    """Better than discovering it on the first submit."""
    with pytest.raises(ConflictError, match="url"):
        _embedder(url="   ")
    with pytest.raises(ConflictError, match="model"):
        _embedder(model="")


@pytest.mark.asyncio
async def test_a_trailing_slash_on_the_url_does_not_double_up() -> None:
    """``.../v1/`` and ``.../v1`` must produce the same request path."""
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        return httpx2.Response(
            200,
            json=_ok_body([1.0] + [0.0] * (StubEmbedder.dim - 1)),
        )

    with _patched(handler):
        await _embedder(url="https://example.invalid/v1/").embed("a claim")

    assert seen["path"] == "/v1/embeddings"
