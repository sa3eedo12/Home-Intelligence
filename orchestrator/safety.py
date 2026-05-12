from __future__ import annotations

from copy import deepcopy
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

TIERS = ("auto", "suggest", "never")

_SAFE_DEFAULT: dict[str, Any] = {
    "tiers": {
        "auto": {"description": "", "rules": []},
        "suggest": {"description": "Safe default: ask before acting.", "rules": []},
        "never": {"description": "", "rules": []},
    },
    "defaults": {"unmatched_tier": "suggest"},
}


class SafetyPolicy:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        self._data = self._load_policy()

    def classify(
        self,
        agent: str,
        capability: str,
        inputs: dict[str, Any] | None = None,
    ) -> str:
        return str(self.explain(agent, capability, inputs)["tier"])

    def explain(
        self,
        agent: str,
        capability: str,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_inputs = inputs if isinstance(inputs, dict) else {}
        for tier in TIERS:
            tier_cfg = (self._data.get("tiers") or {}).get(tier) or {}
            for rule in tier_cfg.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                if self._rule_matches(rule, agent, capability, clean_inputs):
                    return {
                        "tier": tier,
                        "matched_rule": dict(rule),
                        "reason": self._reason(tier, rule),
                    }

        fallback = str((self._data.get("defaults") or {}).get("unmatched_tier") or "suggest")
        if fallback not in TIERS:
            fallback = "suggest"
        return {
            "tier": fallback,
            "matched_rule": None,
            "reason": f"No safety rule matched; defaulting to {fallback}.",
        }

    def _load_policy(self) -> dict[str, Any]:
        path = self._resolve_path()
        if path is None:
            return deepcopy(_SAFE_DEFAULT)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return deepcopy(_SAFE_DEFAULT)
        if not isinstance(data, dict):
            return deepcopy(_SAFE_DEFAULT)
        data.setdefault("tiers", {})
        data.setdefault("defaults", {"unmatched_tier": "suggest"})
        return data

    def _resolve_path(self) -> Path | None:
        candidates = [self.path]
        if not self.path.is_absolute():
            repo_root = Path(__file__).resolve().parents[1]
            candidates.append(repo_root / self.path)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _rule_matches(
        self,
        rule: dict[str, Any],
        agent: str,
        capability: str,
        inputs: dict[str, Any],
    ) -> bool:
        agent_pattern = str(rule.get("agent") or "*")
        if not fnmatch(agent, agent_pattern):
            return False

        capability_pattern = str(rule.get("capability_pattern") or rule.get("capability") or "*")
        if not fnmatch(capability, capability_pattern):
            return False

        domains = rule.get("domain_in")
        if domains is not None:
            domain = inputs.get("domain")
            if domain is not None and str(domain) not in {str(item) for item in domains}:
                return False
        return True

    def _reason(self, tier: str, rule: dict[str, Any]) -> str:
        capability_pattern = str(rule.get("capability_pattern") or rule.get("capability") or "*")
        target = f"{rule.get('agent', '*')}.{capability_pattern}"
        note = str(rule.get("note") or "").strip()
        suffix = f" ({note})" if note else ""
        return f"Safety policy tier '{tier}' matched {target}{suffix}."
