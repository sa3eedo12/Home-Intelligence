from __future__ import annotations

import pytest

from orchestrator.app import TELEGRAM_PLACEHOLDER_TOKEN, telegram_enabled


@pytest.mark.parametrize(
    "token",
    [
        "",
        TELEGRAM_PLACEHOLDER_TOKEN,
    ],
)
def test_disabled_for_unset_and_placeholder_tokens(token: str) -> None:
    """A blank token or the .env.example placeholder must disable Telegram.

    The placeholder previously reached Application.initialize(), which calls
    getMe and raises InvalidToken, crash-looping the orchestrator on any
    unconfigured deploy.
    """
    assert telegram_enabled(token) is False


def test_enabled_for_a_real_token() -> None:
    assert telegram_enabled("123456:AAHrealtokenvalue") is True
