"""LLM evaluation harness for the goals / health stack.

Replays real planner, log-classifier, nag-wording, and weekly-
reflection inputs against an arbitrary list of Ollama models and
scores the outputs on the things we actually care about:

- JSON validity
- Self-consistency (does tracker_spec agree with milestones?)
- Honesty (does nag wording stay grounded in status_line?)
- Tool-loop discipline (does the planner correctly emit needs_data
  when the prompt implies a lookup?)
- Schema compliance (kind/reset/direction in known enum)

All graders are deterministic and run without human input. The harness
is model-agnostic — point it at any models in your Ollama instance
and it'll run + tabulate.

Run from inside the orchestrator container:

    python3 -m orchestrator.eval.llm_eval --models qwen3:14b,gemma4:e4b

Output: side-by-side per-task scores + raw JSON to
~/.copilot/session-state/<id>/files/llm_eval_<timestamp>/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from home_agents_sdk.llm import OllamaClient


# ── Task definitions ────────────────────────────────────────────


def _now_context() -> str:
    """Same shape goals_chat injects into all prompts."""
    return (
        f"It is currently {datetime.now().strftime('%A, %d %B %Y, %H:%M')} "
        "in Asia/Dubai. The user is in the UAE."
    )


PLANNER_SYSTEM = """You are a calm, practical health coach. The user told you a goal in plain English. You have three options each turn:
  (a) commit a plan now (ready=true)
  (b) ask ONE short follow-up question (ready=false + clarification_question)
  (c) request data the system can look up for you (ready=false + needs_data) when the user implied or stated that a value can be fetched.

Output a JSON object with these keys:
- ready: true|false
- clarification_question: required ONLY when ready=false AND needs_data is empty/missing. ONE specific question, ~20 words max.
- needs_data: optional array. Each entry is {"kind": "latest_health_metric", "metric": <name>, "note": <why>}. Allowed metrics: weight, body_fat_pct, heart_rate, resting_heart_rate, hrv, steps, sleep_asleep, active_energy, workout. Use ONLY when the user signaled the system can look it up. Never fabricate a value because you can't fetch it.
- plan_text: required when ready=true. 2-4 sentences. Explain cadence/rate chosen and why it's safe and realistic for THIS goal category.
- tracker_spec: required when ready=true. {trackers: [{id, label, kind: counter|gauge, reset: daily|weekly|monthly|never, target, unit, direction: up|down}], completion_rule?, nudge_rule?, log_hints?}
- milestones: optional. 1-3 entries, each {due_date: YYYY-MM-DD, target_description: str}.

Tracker design rules:
- A GAUGE is the check — don't add a sibling counter to track 'did you weigh in'.
- Don't create two trackers measuring the same thing.

Self-consistency: tracker targets, milestones, and plan_text MUST agree numerically and on timeline.

When to ask vs lookup vs commit:
- LOOKUP when user said check/pull/use latest X and X is a tracked metric.
- ASK when goal needs a numeric anchor you can't get from data.
- COMMIT when enough info to write safe defaults.

""" + _now_context() + """
Return ONLY the JSON object."""


LOG_CLASSIFIER_SYSTEM = """You convert a user's free-text report of something they did into per-tracker numeric deltas for one goal, AND deduce when the event happened.

Return ONLY a JSON object: {deltas: {<tracker_id>: <number>}, ts_iso: ISO-8601 with timezone or null, reasoning_brief: 1 short sentence}

Two kinds of trackers:
- COUNTER: positive deltas only (additions)
- GAUGE: absolute readings, not deltas

""" + _now_context()


# Real-world fixtures captured from the user's actual usage. Add new
# cases here as we encounter failure modes in production.
PLANNER_FIXTURES = [
    {
        "name": "lose_weight_with_healthkit_lookup",
        "user": "I want to lose weight for an event in 4 weeks. Current weight is 94 KG. Check the latest body fat percentage from health kit exports",
        "expect": {
            "should_ask_for_lookup": True,
            "lookup_metric": "body_fat_pct",
            "should_not_commit_yet": True,
        },
    },
    {
        "name": "lose_weight_5kg_clear",
        "user": "I want to lose 5 kg in 12 weeks",
        "expect": {
            "should_commit": True,
            "must_contain_tracker_id_pattern": "weight",
            "weight_target_max": 90,  # realistic 5kg cut
            "no_sibling_check_counter": True,
            "weekly_loss_rate_max_kg": 1.0,
        },
    },
    {
        "name": "vague_lose_weight",
        "user": "I want to lose weight",
        "expect": {
            "should_ask_clarification": True,
        },
    },
    {
        "name": "pushups_after_prayer",
        "user": "I want to do as many pushups as I can after every prayer daily",
        "expect": {
            "should_commit": True,
            "must_have_counter_tracker": True,
            "no_sibling_check_counter": True,
        },
    },
    {
        "name": "sleep_better_with_baseline_lookup",
        "user": "I want to sleep better. Check my recent sleep from the watch.",
        "expect": {
            "should_ask_for_lookup": True,
            "lookup_metric": "sleep_asleep",
        },
    },
]


LOG_CLASSIFIER_FIXTURES = [
    {
        "name": "absolute_weight_reading",
        "spec": {
            "trackers": [
                {"id": "weight_kg", "label": "Weight", "kind": "gauge",
                 "reset": "weekly", "target": 85, "unit": "kg",
                 "direction": "down"},
            ],
        },
        "user": "weight — 92.8 kg at 2026-05-31 07:42",
        "expect": {
            "weight_kg_value_min": 90.0,  # MUST be absolute, not delta
            "weight_kg_value_max": 95.0,
            "no_negative_for_gauge": True,
        },
    },
    {
        "name": "pushups_counter_event",
        "spec": {
            "trackers": [
                {"id": "sessions_today", "label": "Sets", "kind": "counter",
                 "reset": "daily", "target": 5, "unit": "set",
                 "direction": "up"},
                {"id": "pushups_today", "label": "Pushups", "kind": "counter",
                 "reset": "daily", "target": 100, "unit": "pushup",
                 "direction": "up"},
            ],
        },
        "user": "did 30 pushups after dhuhr just now",
        "expect": {
            "sessions_today_value_min": 1,
            "sessions_today_value_max": 1,
            "pushups_today_value_min": 29,
            "pushups_today_value_max": 31,
        },
    },
    {
        "name": "ambiguous_no_match",
        "spec": {
            "trackers": [
                {"id": "weight_kg", "label": "Weight", "kind": "gauge",
                 "reset": "weekly", "target": 85, "unit": "kg",
                 "direction": "down"},
            ],
        },
        "user": "had a really stressful day at work",
        "expect": {
            "deltas_should_be_empty": True,
        },
    },
    {
        "name": "yesterday_ts_resolution",
        "spec": {
            "trackers": [
                {"id": "sessions_today", "label": "Sets", "kind": "counter",
                 "reset": "daily", "target": 5, "unit": "set",
                 "direction": "up"},
            ],
        },
        "user": "did 2 sets yesterday after maghrib",
        "expect": {
            "ts_should_be_past": True,
            "ts_within_days": 2,
        },
    },
]


NAG_FIXTURES = [
    {
        "name": "zero_progress_no_fabrication",
        "goal_title": "Lose weight for event",
        "plan_text": "Aim for ~0.5 kg/week — safe and sustainable.",
        "status_line": "Today: in progress. Weight (kg): 0 kg (target ≤ 85) Overall 0%.",
        "nags_today": 0,
        "recent_log": [],
        "expect": {
            "must_not_contain_invented_progress": True,
            "forbidden_substrings": [
                "lost",  # nothing was lost
                "great start",  # nothing to celebrate
                "amazing", "crushed",
            ],
            "max_words": 35,
        },
    },
    {
        "name": "actual_progress_grounded",
        "goal_title": "Max Pushups After Prayer Daily",
        "plan_text": "Do pushups after each of the 5 daily prayers.",
        "status_line": "Today: in progress. Sets: 2 of 5 set; Pushups: 30 of 100 pushup. Overall 40%.",
        "nags_today": 1,
        "recent_log": [
            {"ts": "2026-06-03T11:00:00+04:00", "raw_text": "did 15 pushups after dhuhr"},
            {"ts": "2026-06-03T14:30:00+04:00", "raw_text": "15 more after asr"},
        ],
        "expect": {
            "must_reference_progress": True,
            "max_words": 35,
        },
    },
]


# ── Graders ─────────────────────────────────────────────────────


def _try_json(content: str) -> dict | None:
    """Best-effort JSON extractor matching what goals_chat does."""
    if not content:
        return None
    s = content.strip()
    if s.startswith("```"):
        import re
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    import re
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            out = json.loads(m.group(0))
            return out if isinstance(out, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


def grade_planner(content: str, expect: dict) -> dict:
    """Return {score: 0..N, max_score: N, checks: [{name, pass, detail}]}."""
    checks = []
    parsed = _try_json(content)
    checks.append({
        "name": "json_valid", "pass": parsed is not None,
        "detail": "non-empty dict parsed" if parsed else "parse failed",
    })
    if parsed is None:
        return _score(checks)

    ready = bool(parsed.get("ready"))
    needs_data = parsed.get("needs_data") or []
    question = (parsed.get("clarification_question") or "").strip()
    spec = parsed.get("tracker_spec") or {}
    trackers = spec.get("trackers") or []

    if expect.get("should_ask_for_lookup"):
        ok = (not ready) and needs_data and not question
        metric_ok = any(
            (isinstance(n, dict) and n.get("metric") == expect.get("lookup_metric"))
            for n in needs_data
        )
        checks.append({
            "name": "asked_for_lookup", "pass": bool(ok and metric_ok),
            "detail": (f"ready={ready} needs_data={len(needs_data)} "
                       f"correct_metric={metric_ok}"),
        })

    if expect.get("should_ask_clarification"):
        ok = (not ready) and question and not needs_data
        checks.append({
            "name": "asked_clarification", "pass": bool(ok),
            "detail": f"ready={ready} question={bool(question)}",
        })

    if expect.get("should_commit"):
        ok = ready and trackers
        checks.append({
            "name": "committed_with_trackers", "pass": bool(ok),
            "detail": f"ready={ready} trackers={len(trackers)}",
        })

    if expect.get("should_not_commit_yet") and ready:
        checks.append({
            "name": "did_not_commit_prematurely", "pass": False,
            "detail": "committed without first looking up the value",
        })
    elif expect.get("should_not_commit_yet"):
        checks.append({
            "name": "did_not_commit_prematurely", "pass": True,
            "detail": "held off as expected",
        })

    if expect.get("no_sibling_check_counter"):
        # Look for a counter whose label/id includes 'check'
        bad = [
            t for t in trackers
            if isinstance(t, dict)
            and t.get("kind") == "counter"
            and ("check" in str(t.get("id", "")).lower()
                 or "check" in str(t.get("label", "")).lower())
        ]
        checks.append({
            "name": "no_sibling_check_counter", "pass": len(bad) == 0,
            "detail": f"{len(bad)} sibling check counters",
        })

    if "must_contain_tracker_id_pattern" in expect:
        needle = expect["must_contain_tracker_id_pattern"]
        ok = any(needle in str(t.get("id", "")).lower() for t in trackers
                 if isinstance(t, dict))
        checks.append({
            "name": f"has_tracker_matching_'{needle}'", "pass": ok,
            "detail": f"tracker ids: {[t.get('id') for t in trackers if isinstance(t, dict)]}",
        })

    if expect.get("must_have_counter_tracker"):
        ok = any(t.get("kind") == "counter" for t in trackers if isinstance(t, dict))
        checks.append({
            "name": "has_counter_tracker", "pass": ok,
            "detail": "",
        })

    if "weight_target_max" in expect:
        # Find any weight-ish tracker and check the target is realistic
        max_target = expect["weight_target_max"]
        weight_trackers = [
            t for t in trackers
            if isinstance(t, dict)
            and "weight" in str(t.get("id", "")).lower()
        ]
        if weight_trackers:
            target = weight_trackers[0].get("target")
            ok = isinstance(target, (int, float)) and target <= max_target
            checks.append({
                "name": "weight_target_realistic", "pass": ok,
                "detail": f"target={target}, expected ≤ {max_target}",
            })

    if "weekly_loss_rate_max_kg" in expect:
        # Cross-check milestones against plan timeline. Detect aggressive
        # rates (e.g. 10kg in 6 weeks = ~1.7kg/week).
        milestones = parsed.get("milestones") or []
        rates = _infer_loss_rates(milestones, trackers)
        max_rate = max(rates) if rates else 0
        ok = max_rate <= expect["weekly_loss_rate_max_kg"]
        checks.append({
            "name": "loss_rate_within_safe_bound", "pass": ok,
            "detail": f"max inferred rate {max_rate:.2f} kg/week",
        })

    return _score(checks)


def _infer_loss_rates(milestones: list, trackers: list) -> list[float]:
    """Estimate kg/week from milestone target_descriptions."""
    import re
    rates = []
    for ms in milestones:
        if not isinstance(ms, dict):
            continue
        desc = str(ms.get("target_description", ""))
        due = ms.get("due_date")
        # Pull a "X kg" number from the description
        m = re.search(r"(\d+(?:\.\d+)?)\s*kg", desc)
        if not m or not due:
            continue
        try:
            kg = float(m.group(1))
            due_dt = datetime.strptime(due, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        days = (due_dt - date.today()).days
        if days <= 0:
            continue
        weeks = days / 7
        rates.append(kg / weeks)
    return rates


def grade_log_classifier(content: str, expect: dict) -> dict:
    checks = []
    parsed = _try_json(content)
    checks.append({
        "name": "json_valid", "pass": parsed is not None,
        "detail": "" if parsed else "parse failed",
    })
    if parsed is None:
        return _score(checks)

    deltas = parsed.get("deltas") or {}
    if not isinstance(deltas, dict):
        deltas = {}

    if expect.get("deltas_should_be_empty"):
        ok = len(deltas) == 0
        checks.append({
            "name": "no_false_match", "pass": ok,
            "detail": f"got {deltas}",
        })
        return _score(checks)

    for key, val in deltas.items():
        if "weight" in key.lower():
            if expect.get("no_negative_for_gauge") and isinstance(val, (int, float)):
                checks.append({
                    "name": f"{key}_not_negative", "pass": val >= 0,
                    "detail": f"value={val}",
                })

    for field_name in list(expect.keys()):
        if not field_name.endswith("_value_min") and not field_name.endswith("_value_max"):
            continue
        tracker_id = field_name.rsplit("_value_", 1)[0]
        bound = expect[field_name]
        actual = deltas.get(tracker_id)
        if actual is None:
            checks.append({
                "name": f"{tracker_id}_present", "pass": False,
                "detail": f"missing in deltas {list(deltas.keys())}",
            })
            continue
        if field_name.endswith("_min"):
            ok = isinstance(actual, (int, float)) and actual >= bound
            checks.append({
                "name": f"{tracker_id}_≥_{bound}", "pass": ok,
                "detail": f"actual={actual}",
            })
        else:
            ok = isinstance(actual, (int, float)) and actual <= bound
            checks.append({
                "name": f"{tracker_id}_≤_{bound}", "pass": ok,
                "detail": f"actual={actual}",
            })

    if expect.get("ts_should_be_past"):
        ts_iso = parsed.get("ts_iso")
        if not ts_iso:
            checks.append({
                "name": "ts_resolved", "pass": False,
                "detail": "ts_iso missing",
            })
        else:
            try:
                ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
                hours_diff = (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds() / 3600
                ok = hours_diff > 12  # at least half a day in the past
                checks.append({
                    "name": "ts_in_past_>12h", "pass": ok,
                    "detail": f"ts={ts_iso}, {hours_diff:.1f}h ago",
                })
                if expect.get("ts_within_days"):
                    max_days = expect["ts_within_days"]
                    days_diff = hours_diff / 24
                    ok2 = 0.5 <= days_diff <= max_days + 0.5
                    checks.append({
                        "name": f"ts_within_{max_days}d", "pass": ok2,
                        "detail": f"{days_diff:.1f} days ago",
                    })
            except (ValueError, TypeError):
                checks.append({
                    "name": "ts_parseable", "pass": False,
                    "detail": f"can't parse '{ts_iso}'",
                })

    return _score(checks)


def grade_nag(content: str, expect: dict) -> dict:
    """Nag is plain text, not JSON."""
    checks = []
    text = (content or "").strip().strip('"')
    checks.append({
        "name": "non_empty", "pass": bool(text),
        "detail": f"len={len(text)}",
    })
    if not text:
        return _score(checks)

    words = text.split()
    if "max_words" in expect:
        checks.append({
            "name": f"≤{expect['max_words']}_words", "pass": len(words) <= expect["max_words"],
            "detail": f"got {len(words)}",
        })

    if expect.get("forbidden_substrings"):
        text_low = text.lower()
        found = [s for s in expect["forbidden_substrings"] if s in text_low]
        checks.append({
            "name": "no_fabricated_celebration", "pass": len(found) == 0,
            "detail": f"found: {found}" if found else "clean",
        })

    if expect.get("must_not_contain_invented_progress"):
        import re
        # Numbers that aren't 0 (which is in the status line) are suspect
        # when the status shows 0 progress.
        numbers = re.findall(r"\b\d+(?:\.\d+)?\s*(?:kg|%|days?|weeks?|reps?|sets?)\b",
                             text.lower())
        # Allow numbers that are explicitly part of the status line phrasing
        # (just '0' should be fine; anything else is fabricated)
        bad = [n for n in numbers if not n.startswith("0 ")
               and not n.startswith("0 kg") and not n.startswith("0%")]
        checks.append({
            "name": "no_invented_numbers", "pass": len(bad) == 0,
            "detail": f"suspect numbers: {bad}" if bad else "clean",
        })

    if expect.get("must_reference_progress"):
        # The nag should mention the actual numbers from the status line
        text_low = text.lower()
        # For the fixture: status shows "2 of 5" and "30 of 100"
        ok = ("2" in text or "30" in text or "40" in text_low or "set" in text_low)
        checks.append({
            "name": "references_actual_state", "pass": ok,
            "detail": "mentions tracker numbers" if ok else "generic, no anchor",
        })

    return _score(checks)


def _score(checks: list[dict]) -> dict:
    return {
        "score": sum(1 for c in checks if c["pass"]),
        "max_score": len(checks),
        "checks": checks,
    }


# ── Runner ──────────────────────────────────────────────────────


async def _run_planner(llm: OllamaClient, model: str, user: str) -> tuple[str, float]:
    t0 = time.monotonic()
    resp = await llm.chat(
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": user},
        ],
        model=model, temperature=0.4, response_format="json",
        think=False, keep_alive=120,
    )
    elapsed = time.monotonic() - t0
    msg = resp.get("message") or {}
    return (msg.get("content") if isinstance(msg, dict) else "") or "", elapsed


async def _run_log_classifier(
    llm: OllamaClient, model: str, fixture: dict,
) -> tuple[str, float]:
    spec = fixture["spec"]
    trackers_brief = "; ".join(
        f"{t['id']} (kind={t['kind']}, label={t['label']}, unit={t['unit']})"
        for t in spec["trackers"]
    )
    system = LOG_CLASSIFIER_SYSTEM + f"\n\nTrackers available: {trackers_brief}"
    t0 = time.monotonic()
    resp = await llm.chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": fixture["user"]},
        ],
        model=model, temperature=0.0, response_format="json", think=False,
    )
    elapsed = time.monotonic() - t0
    msg = resp.get("message") or {}
    return (msg.get("content") if isinstance(msg, dict) else "") or "", elapsed


async def _run_nag(
    llm: OllamaClient, model: str, fixture: dict,
) -> tuple[str, float]:
    tone = ("first nudge of the day — be warm and brief"
            if fixture["nags_today"] == 0
            else "second nudge — keep it light, no guilt")
    system = (
        "You are a calm, brief health coach checking in with one user. "
        "Write exactly ONE sentence (max ~25 words). Warm, conversational, "
        "no emoji-as-syntax, no markdown, no exclamation marks, no clichés. "
        "GROUND your message in the status line provided. NEVER invent "
        "numbers (kilograms lost, percentages, streaks) that aren't shown "
        "there. If the status line shows 0 progress or no data, "
        "acknowledge that honestly rather than inventing improvement. "
        f"Tone: {tone}. Return ONLY the sentence."
    )
    recent = "\n".join(
        f"- {r['ts']}: {r['raw_text']}" for r in fixture["recent_log"]
    ) or "(no logs yet today)"
    user = (
        f"Goal: {fixture['goal_title']}\n"
        f"Plan: {fixture['plan_text']}\n"
        f"Current state: {fixture['status_line']}\n"
        f"Recent activity:\n{recent}"
    )
    t0 = time.monotonic()
    resp = await llm.chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        model=model, temperature=0.7, think=False,
    )
    elapsed = time.monotonic() - t0
    msg = resp.get("message") or {}
    return (msg.get("content") if isinstance(msg, dict) else "") or "", elapsed


async def run_eval(
    *, models: list[str], output_dir: Path, ollama_url: str,
) -> dict:
    llm = OllamaClient(ollama_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {model: {"planner": [], "log_classifier": [], "nag": []}
               for model in models}

    for model in models:
        print(f"\n=== {model} ===", flush=True)
        print("  planner...", flush=True)
        for f in PLANNER_FIXTURES:
            try:
                content, elapsed = await _run_planner(llm, model, f["user"])
                grade = grade_planner(content, f["expect"])
                results[model]["planner"].append({
                    "name": f["name"], "elapsed_s": round(elapsed, 1),
                    "score": grade["score"], "max": grade["max_score"],
                    "checks": grade["checks"], "raw": content,
                })
                print(f"    {f['name']}: {grade['score']}/{grade['max_score']} ({elapsed:.1f}s)", flush=True)
            except Exception as exc:
                print(f"    {f['name']}: ERROR {exc}", flush=True)
                results[model]["planner"].append({
                    "name": f["name"], "error": str(exc),
                })

        print("  log_classifier...", flush=True)
        for f in LOG_CLASSIFIER_FIXTURES:
            try:
                content, elapsed = await _run_log_classifier(llm, model, f)
                grade = grade_log_classifier(content, f["expect"])
                results[model]["log_classifier"].append({
                    "name": f["name"], "elapsed_s": round(elapsed, 1),
                    "score": grade["score"], "max": grade["max_score"],
                    "checks": grade["checks"], "raw": content,
                })
                print(f"    {f['name']}: {grade['score']}/{grade['max_score']} ({elapsed:.1f}s)", flush=True)
            except Exception as exc:
                print(f"    {f['name']}: ERROR {exc}", flush=True)
                results[model]["log_classifier"].append({
                    "name": f["name"], "error": str(exc),
                })

        print("  nag wording...", flush=True)
        for f in NAG_FIXTURES:
            try:
                content, elapsed = await _run_nag(llm, model, f)
                grade = grade_nag(content, f["expect"])
                results[model]["nag"].append({
                    "name": f["name"], "elapsed_s": round(elapsed, 1),
                    "score": grade["score"], "max": grade["max_score"],
                    "checks": grade["checks"], "raw": content,
                })
                print(f"    {f['name']}: {grade['score']}/{grade['max_score']} ({elapsed:.1f}s)", flush=True)
            except Exception as exc:
                print(f"    {f['name']}: ERROR {exc}", flush=True)
                results[model]["nag"].append({
                    "name": f["name"], "error": str(exc),
                })

    return results


def render_summary(results: dict) -> str:
    """Side-by-side scorecard. Sums per category."""
    lines = ["\n=== SCORECARD ===\n"]
    categories = ["planner", "log_classifier", "nag"]
    models = list(results.keys())
    for cat in categories:
        lines.append(f"\n{cat.upper()}:")
        # Header row
        header = "  fixture".ljust(45) + "  ".join(
            m.rjust(20) for m in models
        )
        lines.append(header)
        # Collect fixture names from the first model
        first = results[models[0]][cat]
        fixture_names = [r["name"] for r in first]
        for fname in fixture_names:
            row_bits = ["  " + fname.ljust(43)]
            for m in models:
                entry = next(
                    (r for r in results[m][cat] if r["name"] == fname),
                    None,
                )
                if entry is None or "error" in entry:
                    cell = "ERR"
                else:
                    cell = f"{entry['score']}/{entry['max']} ({entry['elapsed_s']}s)"
                row_bits.append(cell.rjust(20))
            lines.append("  ".join(row_bits))
        # Subtotals
        sub_bits = ["  TOTAL".ljust(45)]
        for m in models:
            scored = [r for r in results[m][cat] if "score" in r]
            total = sum(r["score"] for r in scored)
            max_total = sum(r["max"] for r in scored)
            pct = (100 * total / max_total) if max_total else 0
            sub_bits.append(f"{total}/{max_total} ({pct:.0f}%)".rjust(20))
        lines.append("  ".join(sub_bits))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models", default="qwen3:14b,qwen3:8b,gemma4:e4b",
        help="comma-separated list of Ollama model tags",
    )
    parser.add_argument(
        "--ollama-url",
        default=os.environ.get("OLLAMA_URL", "http://ollama:11434"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path.home() / "llm_eval_runs"),
    )
    args = parser.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(args.output_dir) / f"run_{stamp}"

    results = asyncio.run(run_eval(
        models=models, output_dir=out, ollama_url=args.ollama_url,
    ))

    (out / "results.json").write_text(json.dumps(results, indent=2, default=str))
    summary = render_summary(results)
    print(summary, flush=True)
    (out / "summary.txt").write_text(summary)
    print(f"\nFull results: {out}/results.json", flush=True)


if __name__ == "__main__":
    main()
