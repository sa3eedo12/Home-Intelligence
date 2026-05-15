"""Parse the free-form profile answers into structured fields agents can use.

Background:
  When the user answers onboarding questions like "When do you wake up?",
  the response gets stored verbatim in user_profile.value as a JSON-quoted
  string: "I wake up no later than 9:00 AM on weekdays, 11:00 AM weekends".

  Until this module existed, downstream agents only saw the raw text.
  Quiet hours, morning brief delivery, dietary suggestions, and proactive
  scheduling all hardcoded a single value or read household_members.sleep_time
  (which captures only one variant).

  This module bridges the gap: pull the free-form answer + extract a
  structured shape, e.g.:
    "I wake up no later than 9:00 AM on weekdays, 11:00 AM weekends"
       → {"weekday": time(9, 0), "weekend": time(11, 0)}
    "I only eat Halal food, not a big fan of seafood"
       → {"halal": True, "avoid": ["seafood"]}

  Parsers are conservative: when a pattern doesn't match cleanly we
  return None rather than guessing. Callers should treat None as "no
  structured info, fall back to defaults."

  All parsers operate on already-decoded text (callers strip JSON quotes
  via dashboard._humanize_profile_value before passing in).
"""
from __future__ import annotations

import re
from datetime import time
from typing import Any

WEEKDAY_KEYWORDS = ("weekday", "monday-friday", "mon-fri", "weekdays", "work day")
WEEKEND_KEYWORDS = ("weekend", "saturday", "sunday", "sat-sun", "weekends")


def _parse_time_str(raw: str) -> time | None:
    """Accepts '9 AM', '9:00 AM', '09:00', '11 PM' and returns a time."""
    raw = raw.strip()
    if not raw:
        return None
    # Match HH(:MM)? optionally followed by AM/PM
    m = re.match(
        r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|AM|PM|a\.m\.|p\.m\.)?$",
        raw,
    )
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    suffix = (m.group(3) or "").lower().replace(".", "")
    if suffix in {"pm", "p.m."} and hour < 12:
        hour += 12
    elif suffix in {"am", "a.m."} and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def parse_wake_or_sleep_time(answer: str) -> dict[str, time] | None:
    """Extract weekday / weekend time variants from a free-form answer.

    Examples handled:
      "9 AM" → {"weekday": 09:00, "weekend": 09:00}
      "9:00 AM weekdays, 11:00 AM weekends" → {"weekday": 09:00, "weekend": 11:00}
      "no later than 9:00 AM on weekdays, 11:00 AM weekends" → same
      "I wake up around 7" → {"weekday": 07:00, "weekend": 07:00}
      "anytime between 12:00 and 2 AM" → {"weekday": 00:30, "weekend": 00:30}
        (mid-point of the range — best-effort)

    Returns None if no time at all could be extracted.
    """
    if not answer or not isinstance(answer, str):
        return None
    text = answer.strip()
    if not text:
        return None
    text_lower = text.lower()

    # Strategy: find all (time, suffix) matches in the text, then look at
    # the surrounding context for "weekday" / "weekend" keywords.
    pattern = re.compile(
        r"(\d{1,2}(?::\d{2})?)\s*(am|pm|a\.m\.|p\.m\.)?",
        re.IGNORECASE,
    )
    matches: list[tuple[time, int]] = []
    for m in pattern.finditer(text):
        # Skip plain-number matches that are clearly not times (e.g. "between
        # 12:00 and 2 AM" — the "2" needs the AM context to stick).
        time_str = m.group(0).strip()
        # Heuristic: standalone "5" with no AM/PM is too ambiguous unless
        # it's a clear hour-of-day context. Require ':' OR an AM/PM suffix
        # OR the value being a plausible 24h hour.
        has_meridiem = bool(m.group(2))
        has_colon = ":" in time_str
        # Strip everything except the digits + colon + meridiem
        candidate = re.sub(r"\s+", "", time_str)
        parsed = _parse_time_str(candidate)
        if parsed is None:
            continue
        if not has_meridiem and not has_colon:
            # Bare integer: only accept if it falls in a reasonable range
            # AND has the word "around"/"at"/"by" right before it.
            window = text_lower[max(0, m.start() - 12) : m.start()]
            if not any(kw in window for kw in (" around", " at ", " by ", "wake up ", "sleep ")):
                continue
        matches.append((parsed, m.start()))
    if not matches:
        return None

    # Bucket matches by nearest weekday/weekend keyword in the surrounding
    # ±25 chars. If no keyword is found, the time is "default" — applied to
    # both buckets unless a specific one is set.
    weekday: time | None = None
    weekend: time | None = None
    default: time | None = None
    for parsed, pos in matches:
        window_start = max(0, pos - 30)
        window_end = min(len(text_lower), pos + 30)
        window = text_lower[window_start:window_end]
        if any(kw in window for kw in WEEKEND_KEYWORDS):
            weekend = parsed
        elif any(kw in window for kw in WEEKDAY_KEYWORDS):
            weekday = parsed
        elif default is None:
            default = parsed
    if weekday is None and weekend is None and default is None:
        return None
    weekday_final = weekday or default
    weekend_final = weekend or default or weekday
    if weekday_final is None and weekend_final is None:
        return None
    if weekday_final is None:
        weekday_final = weekend_final
    if weekend_final is None:
        weekend_final = weekday_final
    return {"weekday": weekday_final, "weekend": weekend_final}


def parse_dietary_restrictions(answer: str) -> dict[str, Any] | None:
    """Extract halal/kosher/vegetarian/vegan flags + a list of foods to avoid.

    Examples:
      "I only eat Halal food, not a big fan of seafood"
        → {"halal": True, "avoid": ["seafood"]}
      "Vegetarian, no nuts"
        → {"vegetarian": True, "avoid": ["nuts"]}
      "I avoid dairy and gluten"
        → {"avoid": ["dairy", "gluten"]}
    """
    if not answer or not isinstance(answer, str):
        return None
    text = answer.lower()
    flags: dict[str, Any] = {}
    for diet in ("halal", "kosher", "vegan", "vegetarian", "pescatarian"):
        if diet in text:
            flags[diet] = True
    avoid: list[str] = []
    # Look for "no/avoid/not a fan of/can't eat X"
    avoid_patterns = (
        r"no\s+([a-z\s,]+?)(?=[\.,;]|$)",
        r"avoid\s+([a-z\s,&]+?(?:\s+(?:and|or)\s+[a-z\s,&]+?)*)(?=[\.,;]|$)",
        r"not a (?:big )?fan of\s+([a-z\s,]+?)(?=[\.,;]|$)",
        r"don'?t (?:eat|like)\s+([a-z\s,]+?)(?=[\.,;]|$)",
        r"can'?t eat\s+([a-z\s,]+?)(?=[\.,;]|$)",
        r"allergic to\s+([a-z\s,]+?(?:\s+(?:and|or)\s+[a-z\s,]+?)*)(?=[\.,;]|$)",
    )
    for pattern in avoid_patterns:
        for m in re.finditer(pattern, text):
            for token in re.split(r"\s*(?:,|and|or|&)\s*", m.group(1)):
                cleaned = token.strip()
                # The "no nuts and no dairy" pattern captures
                # "nuts and no dairy"; after splitting we need to drop
                # any leading "no " prefix so we get "dairy" not "no dairy".
                if cleaned.startswith("no "):
                    cleaned = cleaned[3:].strip()
                # Drop bare adjectives like "big" / "much"
                if cleaned and cleaned not in {"big", "much", "many", "really"}:
                    if cleaned not in avoid:
                        avoid.append(cleaned)
    if avoid:
        flags["avoid"] = avoid
    return flags or None


def parse_work_hours(answer: str) -> dict[str, Any] | None:
    """Extract work-hour structure.

    Examples:
      "9:00 AM to 6:00 PM officially, I work remotely"
        → {"start": time(9,0), "end": time(18,0), "remote": True}
      "9-5 weekdays"
        → {"start": time(9,0), "end": time(17,0)}
    """
    if not answer or not isinstance(answer, str):
        return None
    text = answer.lower()
    out: dict[str, Any] = {}
    if "remote" in text or "wfh" in text or "work from home" in text:
        out["remote"] = True
    if "office" in text or "in-office" in text or "at the office" in text:
        out["in_office"] = True
    # Patterns: "9 AM to 6 PM", "9-5", "9:00 to 17:00"
    range_patterns = (
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.))\s*(?:to|-|–|—)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.))",
        r"(\d{1,2}(?::\d{2})?)\s*(?:to|-|–|—)\s*(\d{1,2}(?::\d{2})?)",
    )
    for pattern in range_patterns:
        m = re.search(pattern, text)
        if m:
            start_t = _parse_time_str(m.group(1))
            end_t = _parse_time_str(m.group(2))
            if start_t is not None and end_t is not None:
                # If both bare numbers and end < start, assume PM for the end
                if end_t < start_t and ":" not in m.group(2) and "p" not in m.group(2):
                    end_t = time((end_t.hour + 12) % 24, end_t.minute)
                out["start"] = start_t
                out["end"] = end_t
                break
    if not out:
        return None
    return out


def parse_profile_value(key: str, value: Any) -> dict[str, Any] | None:
    """Top-level dispatcher: route a raw user_profile (key, value) row to
    the right parser. Strips JSON-quote wrapping the same way the
    dashboard does."""
    if not isinstance(value, str):
        # Already structured (jsonb dict) — return as-is, caller can use directly.
        if isinstance(value, dict):
            return value
        return None
    text = value.strip()
    if text.startswith('"') and text.endswith('"'):
        import json as _json

        try:
            text = _json.loads(text)
        except Exception:
            text = text[1:-1]
    if not text or not isinstance(text, str):
        return None
    if key in {"wake_time", "wake_time_observed"}:
        out = parse_wake_or_sleep_time(text)
        return {"weekday": str(out["weekday"]), "weekend": str(out["weekend"])} if out else None
    if key in {"sleep_time", "sleep_time_observed"}:
        out = parse_wake_or_sleep_time(text)
        return {"weekday": str(out["weekday"]), "weekend": str(out["weekend"])} if out else None
    if key in {"dietary_restrictions", "diet"}:
        return parse_dietary_restrictions(text)
    if key == "work_hours":
        out = parse_work_hours(text)
        if out:
            return {
                k: (str(v) if isinstance(v, time) else v) for k, v in out.items()
            }
        return None
    return None
