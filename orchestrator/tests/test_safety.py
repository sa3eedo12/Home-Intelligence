from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.safety import SafetyPolicy

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def safety() -> SafetyPolicy:
    return SafetyPolicy(ROOT / "policies" / "safety.yaml")


@pytest.mark.parametrize(
    ("agent", "capability", "inputs", "tier"),
    [
        ("home_automation", "set_scene", {"scene_name": "Evening"}, "auto"),
        (
            "home_automation",
            "call_service_in_area",
            {"area": "Kitchen", "domain": "light", "service": "turn_off"},
            "auto",
        ),
        (
            "home_automation",
            "call_service",
            {"domain": "climate", "service": "set_temperature"},
            "suggest",
        ),
        (
            "home_automation",
            "call_service",
            {"domain": "lock", "service": "unlock"},
            "never",
        ),
        ("billing", "submit_payment", {}, "never"),
        ("some_agent", "unknown_capability", {}, "suggest"),
    ],
)
def test_classify_table(
    safety: SafetyPolicy,
    agent: str,
    capability: str,
    inputs: dict,
    tier: str,
) -> None:
    assert safety.classify(agent, capability, inputs) == tier


def test_domain_rule_matches_without_domain_refinement(safety: SafetyPolicy) -> None:
    explanation = safety.explain("home_automation", "call_service", {})

    assert explanation["tier"] == "suggest"
    assert explanation["matched_rule"]["domain_in"] == ["climate", "media_player", "vacuum"]


def test_domain_rule_rejects_non_matching_domain_then_falls_back(safety: SafetyPolicy) -> None:
    explanation = safety.explain(
        "home_automation",
        "call_service",
        {"domain": "light", "service": "turn_on"},
    )

    assert explanation["tier"] == "suggest"
    assert explanation["matched_rule"] is None
    assert "defaulting" in explanation["reason"]


def test_missing_policy_file_defaults_to_suggest(tmp_path: Path) -> None:
    policy = SafetyPolicy(tmp_path / "missing.yaml")

    assert policy.classify("home_automation", "call_service", {"domain": "lock"}) == "suggest"
    assert policy.explain("x", "y")["matched_rule"] is None
