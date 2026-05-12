from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.data_science.lora import LoraTrainingJob, ShadowComparison
from tests.data_science_fakes import FakePool

TEST_DATA_ROOT = Path(__file__).resolve().parents[1] / ".test-data"
LORA_DIR = TEST_DATA_ROOT / "lora"


def _file_exists(path: str) -> bool:
    return Path(path).exists()


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


class LoraConn:
    def __init__(self) -> None:
        self.fetchrow = AsyncMock(return_value={"id": 1})
        self.fetch = AsyncMock(
            return_value=[
                {
                    "id": 10,
                    "summary": "confirmed rewrite",
                    "payload": {
                        "input": "Tell me about the washer cycle",
                        "humanized_response": "The washer finished at 7:15 and is ready to unload.",
                        "confirmed": "true",
                    },
                }
            ]
        )
        self.execute = AsyncMock(return_value="UPDATE 1")


@pytest.mark.asyncio
async def test_lora_training_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("LORA_TRAINING_ENABLED", raising=False)
    event_log = SimpleNamespace(record_event=AsyncMock())

    result = await LoraTrainingJob(FakePool(LoraConn()), event_log_store=event_log).run()

    assert result == {"status": "disabled", "duration_seconds": result["duration_seconds"]}
    event_log.record_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_lora_training_enabled_prepares_jsonl(monkeypatch) -> None:
    monkeypatch.setenv("LORA_TRAINING_ENABLED", "true")
    data_dir = LORA_DIR
    shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)
    event_log = SimpleNamespace(record_event=AsyncMock())

    result = await LoraTrainingJob(
        FakePool(LoraConn()), data_dir=data_dir, event_log_store=event_log
    ).run()

    try:
        assert result["status"] == "data_prepared"
        assert result["row_count"] == 1
        training_file = result["training_file"]
        assert await asyncio.to_thread(_file_exists, training_file)
        content = await asyncio.to_thread(_read_text, training_file)
        assert "washer finished" in content
    finally:
        shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)


@pytest.mark.asyncio
async def test_shadow_comparison_scores_placeholder_quality() -> None:
    conn = LoraConn()
    llm = SimpleNamespace(
        chat=AsyncMock(
            side_effect=[
                {
                    "message": {
                        "content": (
                            "Washer cycle 42 finished in Dubai with a clear next step for "
                            "Saeed today, including a reminder to unload laundry before "
                            "8 tonight and check detergent tomorrow."
                        )
                    }
                },
                {"message": {"content": "ok"}},
            ]
        )
    )

    result = await ShadowComparison(FakePool(conn), llm).compare(
        "model-a", "model-b", sample_count=1
    )

    assert result["model_a_quality_score"] == 1.0
    assert result["model_b_quality_score"] == 0.0
    assert "model-a" in result["recommendation"]
