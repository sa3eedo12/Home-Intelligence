from __future__ import annotations

import os

from .npu import NPUClient


async def transcribe_voice_note(audio_bytes: bytes, lang: str | None = None) -> str:
    model = os.getenv("STT_MODEL", "distil-whisper-small.en-int8")
    npu_url = os.getenv("LEMONADE_URL", "http://lemonade:8000")
    client = NPUClient(base_url=npu_url)
    return await client.transcribe(model=model, audio_bytes=audio_bytes, lang=lang)
