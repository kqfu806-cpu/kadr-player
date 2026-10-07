from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from backend.config import OLLAMA_EMBED_NUM_CTX, OLLAMA_LLM_NUM_CTX, OLLAMA_VISION_NUM_CTX
from backend.ollama_ai import OllamaClient


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeHttpClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(self.payload)


@pytest.mark.asyncio
async def test_embedding_request_uses_model_context_limit() -> None:
    client = FakeHttpClient({"embedding": [0.1, 0.2]})
    ollama = OllamaClient()
    ollama.status.online = True
    ollama.status.models["nomic-embed-text"] = True

    result = await ollama.embed(client, "test prompt")

    assert result == [0.1, 0.2]
    assert client.calls[0]["json"]["options"]["num_ctx"] == OLLAMA_EMBED_NUM_CTX


@pytest.mark.asyncio
async def test_embedding_batch_uses_embed_endpoint_and_context_limit() -> None:
    client = FakeHttpClient({"embeddings": [[0.1, 0.2], [0.3, 0.4]]})
    ollama = OllamaClient()
    ollama.status.online = True
    ollama.status.models["nomic-embed-text"] = True

    result = await ollama.embed_batch(client, ["first", "second"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert client.calls[0]["url"].endswith("/api/embed")
    assert client.calls[0]["json"]["input"] == ["first", "second"]
    assert client.calls[0]["json"]["options"]["num_ctx"] == OLLAMA_EMBED_NUM_CTX


@pytest.mark.asyncio
async def test_llm_request_uses_model_context_limit() -> None:
    client = FakeHttpClient({"response": "ok"})
    ollama = OllamaClient()
    ollama.status.online = True
    ollama.status.models["deepseek-r1:8b"] = True

    result = await ollama.generate(client, "test prompt")

    assert result == "ok"
    assert client.calls[0]["json"]["options"]["num_ctx"] == OLLAMA_LLM_NUM_CTX


@pytest.mark.asyncio
async def test_vision_request_uses_model_context_limit(tmp_path: Path) -> None:
    image_path = tmp_path / "cover.png"
    Image.new("RGB", (8, 8)).save(image_path)
    client = FakeHttpClient({"response": "ok"})
    ollama = OllamaClient()
    ollama.status.online = True
    ollama.status.models["llava:7b"] = True

    result = await ollama.vision(client, "test prompt", str(image_path))

    assert result == "ok"
    assert client.calls[0]["json"]["options"]["num_ctx"] == OLLAMA_VISION_NUM_CTX
