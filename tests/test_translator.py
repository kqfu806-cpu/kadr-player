from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from backend import translator


@pytest.mark.asyncio
async def test_english_biography_is_translated_and_cached(tmp_path: Path) -> None:
    source = (
        "The artist is a musician and songwriter known for several albums. "
        "They began performing in London and released their first record in 2010."
    )

    class Model:
        calls = 0

        async def generate(self, _client, prompt: str, **_kwargs) -> str:
            self.calls += 1
            assert source in prompt
            return "Исполнитель — музыкант и автор песен."

    model = Model()
    async with httpx.AsyncClient() as client:
        first = await translator.translate_biography(
            source, client, model, cache_dir=tmp_path  # type: ignore[arg-type]
        )
        second = await translator.translate_biography(
            source, client, model, cache_dir=tmp_path  # type: ignore[arg-type]
        )

    assert first == second == "Исполнитель — музыкант и автор песен."
    assert model.calls == 1
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.asyncio
async def test_translation_preserves_original_when_model_unavailable(tmp_path: Path) -> None:
    source = (
        "The artist is a musician and songwriter known for several albums. "
        "They began performing in London and released their first record in 2010."
    )

    class OfflineModel:
        async def generate(self, *_args, **_kwargs) -> None:
            return None

    async with httpx.AsyncClient() as client:
        result = await translator.translate_biography(
            source, client, OfflineModel(), cache_dir=tmp_path  # type: ignore[arg-type]
        )

    assert result == source
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_non_english_biography_does_not_call_model(tmp_path: Path) -> None:
    class OfflineModel:
        async def generate(self, *_args, **_kwargs) -> None:
            raise AssertionError("Russian text should not be sent for translation")

    async with httpx.AsyncClient() as client:
        result = await translator.translate_biography(
            "Музыкальный исполнитель из Минска.",
            client,
            OfflineModel(),  # type: ignore[arg-type]
            cache_dir=tmp_path,
        )
    assert result == "Музыкальный исполнитель из Минска."
