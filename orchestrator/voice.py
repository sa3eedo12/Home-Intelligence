from __future__ import annotations

import time

from home_agents_sdk import stt
from home_agents_sdk.telemetry import get_logger

logger = get_logger("voice")


async def transcribe(voice_msg: object, bot: object) -> str:
    """Download Telegram voice note and transcribe via NPU Whisper."""
    file = await bot.get_file(voice_msg.file_id)  # type: ignore[attr-defined]
    audio_bytes = bytes(await file.download_as_bytearray())
    start = time.monotonic()
    transcript = await stt.transcribe_voice_note(audio_bytes)
    elapsed = time.monotonic() - start
    logger.info("voice_transcribed", bytes=len(audio_bytes), duration_s=round(elapsed, 2))
    return transcript
