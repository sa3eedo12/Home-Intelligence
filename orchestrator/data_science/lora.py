from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from home_agents_sdk.telemetry import get_logger

from .common import SingleFlightJob, decode_json

logger = get_logger("orchestrator.data_science.lora")


class LoraTrainingJob(SingleFlightJob):
    def __init__(
        self,
        pool: Any,
        llm: Any | None = None,
        data_dir: str | Path | None = None,
        model_base: str | None = None,
        event_log_store: Any | None = None,
    ) -> None:
        super().__init__(job_name="lora_training", pool=pool, event_log_store=event_log_store)
        self.llm = llm
        self.data_dir = Path(data_dir or os.environ.get("LORA_DATA_DIR") or "data/lora")
        self.model_base = (
            model_base or os.environ.get("HUMANIZER_MODEL") or os.environ.get("DEFAULT_MODEL")
        )

    async def run(self) -> dict[str, Any]:
        return await self._run_singleflight(self._run)

    async def _run(self) -> dict[str, Any]:
        if os.environ.get("LORA_TRAINING_ENABLED", "false").strip().lower() != "true":
            logger.info("lora_training_skipped", reason="disabled")
            await self._insert_run(status="disabled", finished=True)
            return {"status": "disabled"}

        if self.pool is None:
            return {"status": "skipped", "reason": "postgres_unavailable"}

        run_id = await self._insert_run(status="pending", finished=False)
        try:
            examples = await self._load_training_examples()
            training_file = self._training_path()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with training_file.open("w", encoding="utf-8") as fh:
                for example in examples:
                    fh.write(json.dumps(example, ensure_ascii=False, default=str) + "\n")

            # TODO: Unsloth/ROCm training pseudocode for the opt-in implementation:
            # 1. Spawn a dedicated ROCm container with the base model mounted read-only and
            #    this JSONL file mounted as the supervised fine-tuning dataset.
            # 2. Load the base chat model with Unsloth FastLanguageModel.from_pretrained(),
            #    enable LoRA adapters for attention/MLP projection modules, and train for a
            #    small capped number of epochs with low rank (r=8 or r=16) and bf16/fp16 per
            #    GPU support.
            # 3. Save adapters under LORA_DATA_DIR/adapters/<period>, never overwrite the
            #    production model, and register the candidate as a shadow model only.
            # 4. Run ShadowComparison against the current humanizer model; promote only after
            #    explicit user approval and a rollback artifact has been written.
            await self._finish_run(
                run_id,
                status="data_prepared",
                training_file=str(training_file),
            )
            return {
                "status": "data_prepared",
                "training_file": str(training_file),
                "row_count": len(examples),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("lora_training_data_prep_failed", error=str(exc))
            await self._finish_run(run_id, status="failed", error=str(exc))
            return {"status": "failed", "error": str(exc)}

    async def _load_training_examples(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ts, summary, payload
                FROM event_log
                WHERE ts >= now() - interval '7 days'
                  AND (
                    capability ILIKE '%human%'
                    OR payload::text ILIKE '%humanized%'
                    OR payload::text ILIKE '%humanizer%'
                  )
                  AND (
                    payload->>'confirmed' = 'true'
                    OR payload->>'status' IN ('confirmed', 'accepted')
                    OR summary ILIKE '%confirmed%'
                  )
                ORDER BY ts DESC
                LIMIT 500
                """
            )
        examples: list[dict[str, Any]] = []
        for row in rows:
            payload = decode_json(dict(row).get("payload"), {})
            if not isinstance(payload, dict):
                payload = {}
            prompt = (
                payload.get("input")
                or payload.get("prompt")
                or payload.get("source_text")
                or payload.get("text")
                or dict(row).get("summary")
            )
            response = (
                payload.get("humanized_response")
                or payload.get("humanized")
                or payload.get("response")
                or payload.get("output")
            )
            if not prompt or not response:
                continue
            examples.append(
                {
                    "messages": [
                        {"role": "user", "content": str(prompt)},
                        {"role": "assistant", "content": str(response)},
                    ],
                    "metadata": {"event_id": dict(row).get("id"), "source": "event_log"},
                }
            )
        return examples

    def _training_path(self) -> Path:
        now = datetime.now(UTC).isocalendar()
        return self.data_dir / f"training-{now.year:04d}W{now.week:02d}.jsonl"

    async def _insert_run(self, *, status: str, finished: bool) -> int | None:
        if self.pool is None:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO lora_training_runs(status, model_base, finished_at)
                    VALUES ($1, $2, CASE WHEN $3::bool THEN now() ELSE NULL END)
                    RETURNING id
                    """,
                    status,
                    self.model_base,
                    finished,
                )
            return int(row["id"]) if row else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("lora_training_run_insert_failed", error=str(exc))
            return None

    async def _finish_run(
        self,
        run_id: int | None,
        *,
        status: str,
        training_file: str | None = None,
        error: str | None = None,
    ) -> None:
        if self.pool is None or run_id is None:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE lora_training_runs
                    SET finished_at = now(),
                        status = $2,
                        training_file = COALESCE($3, training_file),
                        error = $4
                    WHERE id = $1
                    """,
                    run_id,
                    status,
                    training_file,
                    error,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("lora_training_run_update_failed", error=str(exc))


class ShadowComparison:
    """Placeholder evaluator for LoRA candidates.

    The v1 quality score is intentionally simple: a response is counted as
    higher quality when it is 20-300 words long and includes at least one digit
    or proper noun. It is a specificity proxy until a human evaluation rubric is
    added.
    """

    def __init__(self, pool: Any, llm: Any) -> None:
        self.pool = pool
        self.llm = llm

    async def compare(self, model_a: str, model_b: str, sample_count: int = 30) -> dict[str, Any]:
        if self.pool is None or self.llm is None:
            return {
                "model_a_quality_score": 0.0,
                "model_b_quality_score": 0.0,
                "recommendation": "skipped: comparison dependencies unavailable",
            }
        samples = await self._load_samples(max(1, int(sample_count or 30)))
        if not samples:
            return {
                "model_a_quality_score": 0.0,
                "model_b_quality_score": 0.0,
                "recommendation": "insufficient_samples",
            }

        score_a = await self._score_model(model_a, samples)
        score_b = await self._score_model(model_b, samples)
        if score_b > score_a:
            recommendation = f"keep {model_b} in shadow and request human review"
        elif score_a > score_b:
            recommendation = f"keep {model_a}; {model_b} did not improve the heuristic"
        else:
            recommendation = "tie: collect more samples before changing models"
        return {
            "model_a_quality_score": score_a,
            "model_b_quality_score": score_b,
            "recommendation": recommendation,
        }

    async def _load_samples(self, sample_count: int) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT summary, payload
                FROM event_log
                WHERE capability ILIKE '%human%' OR payload::text ILIKE '%humanizer%'
                ORDER BY ts DESC
                LIMIT $1
                """,
                sample_count,
            )
        samples: list[str] = []
        for row in rows:
            payload = decode_json(dict(row).get("payload"), {})
            if not isinstance(payload, dict):
                payload = {}
            text = payload.get("input") or payload.get("prompt") or dict(row).get("summary")
            if text:
                samples.append(str(text))
        return samples

    async def _score_model(self, model: str, samples: list[str]) -> float:
        passing = 0
        for sample in samples:
            response = await self.llm.chat(
                model=model,
                temperature=0.2,
                messages=[{"role": "user", "content": sample}],
            )
            text = _response_text(response)
            passing += int(_quality_passes(text))
        return round(passing / len(samples), 3)


def _response_text(response: Any) -> str:
    if isinstance(response, dict):
        message = response.get("message")
        if isinstance(message, dict):
            return str(message.get("content") or "")
        return str(response.get("response") or "")
    return str(response or "")


def _quality_passes(text: str) -> bool:
    words = re.findall(r"\b\w+\b", text)
    has_specific_token = bool(re.search(r"\d", text) or re.search(r"\b[A-Z][a-z]{2,}\b", text))
    return 20 <= len(words) <= 300 and has_specific_token
