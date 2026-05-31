"""Goals + nag-windows + chores natural-language handler.

This module sits in front of the regular `router.handle` in the
Telegram text path and intercepts messages that are about the user's
health goals, chore tracking, or nag window preferences. If the LLM
intent classifier matches one of the supported intents we handle it
inline; otherwise we return None and the message falls through to the
existing router so weather/news/etc still works.

Design rules:
- Plain conversational English in + out. No slash commands, no JSON
  visible to the user, no markdown widgets.
- Tone: playful, brief, warm but never chatty.
- Member-scoped. Every operation goes against the member resolved from
  the Telegram chat.
- LLM budget per message: one classifier call (8b). Only the
  create_goal intent makes a second 14b call to write the actual plan.
- Short-lived conversational context per member (Redis, 30-min TTL):
  after any handled intent that touches a specific goal we remember
  the goal_id, so a follow-up like "what would the plan involve?"
  resolves to that goal instead of falling through to the generic
  router. Keeps memory tiny — one key per member, no transcript.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

from home_agents_sdk.chore_store import ChoreStore
from home_agents_sdk.health_goals_store import HealthGoalsStore
from home_agents_sdk.llm import OllamaClient
from home_agents_sdk.member_nag_windows_store import MemberNagWindowsStore
from home_agents_sdk.telemetry import get_logger


logger = get_logger("orchestrator.goals_chat")

CLASSIFIER_MODEL_DEFAULT = "qwen3:8b"
PLANNER_MODEL_DEFAULT = "qwen3:14b"

# Per-member context lives in Redis for this many seconds. Long enough
# to cover a natural back-and-forth ("create goal" → "what's the plan?"
# → "skip today") but short enough that a stale context from 2 hours
# ago doesn't accidentally route a fresh question to the wrong goal.
CONTEXT_TTL_SECONDS = 30 * 60
CONTEXT_KEY_PREFIX = "goals_chat:context:"

# Intents the classifier may return. "general_chat" means "not a goals
# topic — pass through to the regular router". The order matters: when
# the LLM is unsure between create_goal and check_progress, we tip
# toward check_progress because create is irreversible (creates a row).
VALID_INTENTS = {
    "create_goal", "refine_goal", "check_progress", "list_goals",
    "explain_plan",
    "skip_workout", "log_workout",
    "mute_goal", "unmute_goal", "abandon_goal", "pause_goal", "resume_goal",
    "set_nag_windows",
    "complete_chore", "list_chores",
    "weekly_review",
    "general_chat",
}


@dataclass(slots=True)
class GoalsHandlerResult:
    """Returned from `try_handle`. `handled=True` means we replied
    inline (text is the reply to send). `handled=False` means the
    caller should fall through to the regular router."""
    handled: bool
    text: str | None = None
    intent: str | None = None
    extras: dict[str, Any] | None = None


class GoalsChatHandler:
    def __init__(
        self,
        *,
        llm: OllamaClient,
        goals_store: HealthGoalsStore,
        chore_store: ChoreStore,
        nag_store: MemberNagWindowsStore,
        redis: Any | None = None,
        classifier_model: str = CLASSIFIER_MODEL_DEFAULT,
        planner_model: str = PLANNER_MODEL_DEFAULT,
    ) -> None:
        self.llm = llm
        self.goals = goals_store
        self.chores = chore_store
        self.nag = nag_store
        self.redis = redis
        self.classifier_model = classifier_model
        self.planner_model = planner_model

    # ── Public entry point ───────────────────────────────────────

    async def try_handle(
        self,
        text: str,
        *,
        member: dict[str, Any] | None,
    ) -> GoalsHandlerResult:
        """Run the classifier. If the message is a goals/chores/nag
        intent, handle it inline and return the reply. Otherwise return
        handled=False and let the regular chat router take over."""
        if not text or not text.strip():
            return GoalsHandlerResult(handled=False)
        if member is None or "id" not in member:
            return GoalsHandlerResult(handled=False)
        member_id = int(member["id"])
        context = await self._load_context(member_id)
        intent_info = await self._classify(text, context=context)
        intent = intent_info.get("intent", "general_chat")
        if intent == "general_chat" or intent not in VALID_INTENTS:
            return GoalsHandlerResult(handled=False, intent=intent)
        try:
            reply, touched_goal_id = await self._dispatch(
                intent=intent, args=intent_info, text=text,
                member_id=member_id, context=context,
            )
        except Exception as exc:
            logger.warning("goals_chat_handler_failed",
                           intent=intent, error=str(exc))
            return GoalsHandlerResult(
                handled=True, intent=intent,
                text=(
                    "I tried to handle that as a goals/chores message but "
                    "something went wrong. Could you try rephrasing?"
                ),
            )
        # Stash whichever goal we just touched so a follow-up question
        # ("what's the plan?") routes back to it.
        if touched_goal_id is not None:
            await self._save_context(
                member_id, last_goal_id=touched_goal_id, last_intent=intent,
            )
        return GoalsHandlerResult(
            handled=True, intent=intent, text=reply,
            extras=intent_info,
        )

    # ── Classifier ───────────────────────────────────────────────

    async def _classify(
        self, text: str, *, context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One LLM call. Returns {intent, args}. The classifier is
        intentionally strict — anything that isn't clearly a goals
        topic falls through to general_chat. When a recent goal
        context is present we feed it to the model so vague follow-up
        questions ('what does the plan look like') route correctly."""
        prompt = self._classifier_prompt(text, context=context)
        try:
            resp = await self.llm.chat(
                messages=[
                    {"role": "system", "content": prompt["system"]},
                    {"role": "user", "content": prompt["user"]},
                ],
                model=self.classifier_model,
                temperature=0.0,
                response_format="json",
                think=False,
            )
        except Exception as exc:
            logger.warning("goals_chat_classifier_failed", error=str(exc))
            return {"intent": "general_chat"}
        content = _extract_chat_content(resp)
        parsed = _parse_json_blob(content) or {}
        intent = str(parsed.get("intent") or "general_chat")
        if intent not in VALID_INTENTS:
            intent = "general_chat"
        parsed["intent"] = intent
        return parsed

    @staticmethod
    def _classifier_prompt(
        text: str, *, context: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        context_block = ""
        if context and context.get("last_goal_title"):
            ttl_min = max(
                0, int((CONTEXT_TTL_SECONDS - (context.get("age_seconds") or 0)) / 60)
            )
            context_block = (
                "\n\nRECENT CONTEXT: The user just interacted with their "
                f"goal titled \"{context['last_goal_title']}\" "
                f"(intent='{context.get('last_intent')}', "
                f"valid for ~{ttl_min} more minutes). "
                "If the new message is a vague follow-up like 'what would "
                "the plan involve', 'tell me more', 'what does it look "
                "like', 'how does it work' — treat it as 'explain_plan' "
                "on that goal. "
                "If they propose a tweak / alternative approach to the "
                "SAME outcome ('I was thinking instead I could…', 'what "
                "if I did X instead', 'change it to…', 'make it 3x a "
                "week', 'shift to Mon/Wed/Fri', 'what do you think about "
                "doing it daily') — treat that as 'refine_goal' on this "
                "existing goal, NOT a new 'create_goal'. Only return "
                "'create_goal' when the new message is clearly a "
                "different outcome (e.g. they just made a pushup goal "
                "and now say 'I also want to lose weight'). "
                "If they say 'skip it', 'mute it', 'pause it' without "
                "naming a different goal, apply to this one. Don't "
                "override an obviously-different intent."
            )
        system = (
            "You classify a single user message into one of a fixed set "
            "of intents about their personal health goals, household "
            "chores, or notification preferences. Be strict — anything "
            "that is not clearly one of the listed intents must be "
            "'general_chat' so the regular assistant handles it.\n\n"
            "Return ONLY a JSON object: "
            "{\"intent\": <name>, ...args}.\n\n"
            "Allowed intents and what to extract:\n"
            "- create_goal: user wants to start a NEW health goal. "
            "args: {\"title\": str, \"description\": str}.\n"
            "- refine_goal: user proposes a different approach / "
            "cadence / day-pattern for an EXISTING goal they just "
            "talked about. args: {\"refinement\": str, \"which\": "
            "str|null}. Use this ONLY when recent context names a "
            "goal — never on a cold message.\n"
            "- check_progress: user asks how they're doing on a goal.\n"
            "- list_goals: user asks 'what goals do I have'.\n"
            "- explain_plan: user asks what the plan for a goal is, what "
            "it involves, what it looks like, how it works. "
            "args: {\"which\": str|null}.\n"
            "- skip_workout: user wants to skip today's workout / take "
            "a rest day. args: {\"reason\": str|null}.\n"
            "- log_workout: user reports they just worked out. "
            "args: {\"note\": str|null}.\n"
            "- mute_goal: stop nagging me about a goal for some time. "
            "args: {\"duration_hours\": int, \"which\": str|null}.\n"
            "- unmute_goal / pause_goal / resume_goal / abandon_goal: "
            "state change on a goal. args: {\"which\": str|null}.\n"
            "- set_nag_windows: user changes when they can be nagged. "
            "args: {\"weekday_start_hour\": int|null, "
            "\"weekday_end_hour\": int|null, "
            "\"weekend_start_hour\": int|null, "
            "\"weekend_end_hour\": int|null}.\n"
            "- complete_chore: user reports finishing a chore. "
            "args: {\"name\": str}.\n"
            "- list_chores: 'what chores are overdue / due today'.\n"
            "- weekly_review: user asks for the weekly summary.\n"
            "- general_chat: anything not in this list.\n\n"
            "Examples:\n"
            "USER: 'I want to work out four times a week'\n"
            "{\"intent\": \"create_goal\", \"title\": \"Work out 4x a week\", "
            "\"description\": \"I want to work out four times a week.\"}\n\n"
            "USER (after just creating a pushup goal): 'I was thinking "
            "doing pushups after every prayer daily, what do you think?'\n"
            "{\"intent\": \"refine_goal\", \"refinement\": \"do pushups "
            "after every prayer, daily cadence\"}\n\n"
            "USER (after creating a goal): 'shift it to Tue/Thu/Sat'\n"
            "{\"intent\": \"refine_goal\", \"refinement\": \"shift to "
            "Tue/Thu/Sat\"}\n\n"
            "USER: 'how am I doing this week'\n"
            "{\"intent\": \"check_progress\"}\n\n"
            "USER: 'what would the plan involve'\n"
            "{\"intent\": \"explain_plan\"}\n\n"
            "USER: 'tell me more about how the plan works'\n"
            "{\"intent\": \"explain_plan\"}\n\n"
            "USER: 'i did a workout just now'\n"
            "{\"intent\": \"log_workout\", \"note\": null}\n\n"
            "USER: 'skip workout today, sick'\n"
            "{\"intent\": \"skip_workout\", \"reason\": \"sick\"}\n\n"
            "USER: 'dont nag me before 6pm on weekdays'\n"
            "{\"intent\": \"set_nag_windows\", "
            "\"weekday_start_hour\": 18}\n\n"
            "USER: 'just vacuumed the living room'\n"
            "{\"intent\": \"complete_chore\", \"name\": \"vacuum\"}\n\n"
            "USER: 'whats the weather'\n"
            "{\"intent\": \"general_chat\"}\n\n"
            + _now_context_line()
            + context_block
        )
        return {"system": system, "user": text.strip()}

    # ── Dispatcher ───────────────────────────────────────────────

    async def _dispatch(
        self, *, intent: str, args: dict[str, Any],
        text: str, member_id: int,
        context: dict[str, Any] | None = None,
    ) -> tuple[str, int | None]:
        """Returns (reply_text, touched_goal_id_or_None). The caller
        uses the goal_id to update the per-member context so a
        follow-up question lands on the same goal."""
        if intent == "create_goal":
            return await self._handle_create_goal(args, text, member_id)
        if intent == "refine_goal":
            return await self._handle_refine_goal(args, text, member_id, context)
        if intent == "check_progress":
            return await self._handle_check_progress(args, member_id, context)
        if intent == "list_goals":
            return await self._handle_list_goals(member_id), None
        if intent == "explain_plan":
            return await self._handle_explain_plan(args, member_id, context)
        if intent == "skip_workout":
            return await self._handle_skip_workout(args, member_id)
        if intent == "log_workout":
            return await self._handle_log_workout(
                args, member_id, text=text, context=context,
            )
        if intent in {"mute_goal", "unmute_goal", "pause_goal", "resume_goal",
                       "abandon_goal"}:
            return await self._handle_goal_state(intent, args, member_id, context)
        if intent == "set_nag_windows":
            return await self._handle_set_nag_windows(args, member_id), None
        if intent == "complete_chore":
            return await self._handle_complete_chore(args, member_id), None
        if intent == "list_chores":
            return await self._handle_list_chores(), None
        if intent == "weekly_review":
            return await self._handle_weekly_review(member_id), None
        return (
            "I picked up something about goals but I don't know how to "
            "handle that specific request yet."
        ), None

    # ── create_goal: LLM plan generation ─────────────────────────

    async def _handle_create_goal(
        self, args: dict[str, Any], text: str, member_id: int,
    ) -> tuple[str, int | None]:
        title = str(args.get("title") or text[:80]).strip()
        description = str(args.get("description") or text).strip()
        try:
            plan = await self._generate_plan(title=title, description=description)
        except Exception as exc:
            logger.warning("goals_chat_planner_failed", error=str(exc))
            plan = {
                "plan_text": (
                    "I'll check in and nudge you as we go. Tell me how "
                    "you'd like me to measure this and I'll set up the "
                    "tracking."
                ),
                "tracker_spec": None,
                "milestones": [],
            }
        goal_id = await self.goals.create(
            member_id=member_id,
            title=title,
            description=description,
            tracker_spec=plan.get("tracker_spec"),
            plan_text=plan.get("plan_text"),
        )
        if goal_id is not None and plan.get("milestones"):
            try:
                await self.goals.update_plan(
                    goal_id, plan_text=plan["plan_text"] or "",
                    milestones=plan["milestones"],
                )
            except Exception as exc:
                logger.warning("goals_chat_milestone_save_failed",
                               error=str(exc))
        bits = [
            f"Got it — your new goal is \"{title}\".",
            plan.get("plan_text") or "",
        ]
        spec = plan.get("tracker_spec") or {}
        trackers = spec.get("trackers") or []
        if trackers:
            tracker_lines = []
            for t in trackers[:4]:
                target = t.get("target")
                unit = t.get("unit") or ""
                label = t.get("label") or t.get("id") or "tracker"
                reset = t.get("reset") or "daily"
                if target is not None:
                    tracker_lines.append(
                        f"{label}: target {target} {unit} per {reset[:-2] if reset.endswith('ly') else reset}".replace(
                            "  ", " "
                        ).strip()
                    )
                else:
                    tracker_lines.append(label)
            bits.append("I'll track: " + "; ".join(tracker_lines) + ".")
        bits.append(
            "Tell me how it goes (e.g. \"did 20 pushups after maghrib\") "
            "and I'll log it. Ask \"how am I doing\" any time. Say "
            "\"mute it\" to stop nags, or describe a change and I'll "
            "rework the plan."
        )
        return "\n\n".join(b for b in bits if b), goal_id

    # ── Refine an existing goal in conversational context ────────

    async def _handle_refine_goal(
        self, args: dict[str, Any], text: str, member_id: int,
        context: dict[str, Any] | None = None,
    ) -> tuple[str, int | None]:
        """User proposed a different approach to a goal we've been
        discussing. Re-run the planner with the existing goal's
        description + the refinement note, then overwrite the plan in
        place (no new health_goals row)."""
        goals = await self.goals.list_active(member_id=member_id)
        if not goals:
            return (
                "There's no active goal to refine right now. Tell me "
                "what you're aiming for and I'll set one up."
            ), None
        which = str(args.get("which") or "").lower()
        target = _match_goal(goals, which)
        if target is None and context and context.get("last_goal_id"):
            target = next(
                (g for g in goals if int(g["id"]) == context["last_goal_id"]),
                None,
            )
        if target is None:
            target = goals[0]
        refinement = str(args.get("refinement") or text).strip()
        try:
            plan = await self._generate_plan(
                title=target["title"],
                description=(
                    f"{target['description']}\n\n"
                    f"REFINEMENT FROM USER: {refinement}"
                ),
            )
        except Exception as exc:
            logger.warning("goals_chat_refine_failed", error=str(exc))
            return (
                "I'd like to rework the plan but the planner is taking too "
                "long right now. Try again in a minute, or tell me exactly "
                "what to change."
            ), int(target["id"])
        try:
            await self.goals.update_plan(
                int(target["id"]),
                plan_text=plan.get("plan_text") or "",
                tracker_spec=plan.get("tracker_spec"),
                milestones=plan.get("milestones") or None,
            )
        except Exception as exc:
            logger.warning("goals_chat_refine_persist_failed", error=str(exc))
            return (
                "I drafted an updated plan but couldn't save it. "
                f"Here's what I had:\n\n{plan.get('plan_text') or ''}"
            ), int(target["id"])
        bits = [
            f"Updated \"{target['title']}\". Here's the revised plan:",
            plan.get("plan_text") or "",
        ]
        spec = plan.get("tracker_spec") or {}
        trackers = spec.get("trackers") or []
        if trackers:
            tracker_lines = []
            for t in trackers[:4]:
                target_v = t.get("target")
                unit = t.get("unit") or ""
                label = t.get("label") or t.get("id") or "tracker"
                reset = t.get("reset") or "daily"
                if target_v is not None:
                    tracker_lines.append(
                        f"{label}: target {target_v} {unit} per {reset[:-2] if reset.endswith('ly') else reset}".replace(
                            "  ", " "
                        ).strip()
                    )
            if tracker_lines:
                bits.append("New tracking: " + "; ".join(tracker_lines) + ".")
        bits.append(
            "Say the word and I'll keep refining it — or \"explain the "
            "plan\" any time you want the full breakdown."
        )
        return "\n\n".join(b for b in bits if b), int(target["id"])

    async def _generate_plan(
        self, *, title: str, description: str,
    ) -> dict[str, Any]:
        """One 14b call. Returns plan_text + tracker_spec + milestones.

        The tracker_spec is the generic structure that the goal_engine
        runtime evaluates against the user's logs. Trackers can be
        counter (sum of deltas in a window) or gauge (most recent
        reported value). Reset windows are daily / weekly / monthly /
        never. New goal shapes — sessions per day, reps per session,
        weight gauges, calorie deficits, water intake, anything — fit
        this shape without code changes."""
        system = (
            "You are a calm, practical health coach. The user told you a "
            "goal in plain English. Write a short personal plan and a "
            "structured tracker spec that the runtime will evaluate.\n\n"
            "Output a JSON object with these keys:\n"
            "- plan_text: 2 to 4 sentences. Friendly and concrete. Avoid "
            "fitness clichés. Avoid commands like 'do X every day'.\n"
            "- tracker_spec: object with these keys:\n"
            "    - trackers: JSON array. Each entry is "
            "{id, label, kind, reset, target, unit, direction}.\n"
            "      id: short snake_case key unique within this goal.\n"
            "      label: human-readable string for messages.\n"
            "      kind: 'counter' (sum of logged deltas in window) "
            "or 'gauge' (most recent reported value).\n"
            "      reset: 'daily' | 'weekly' | 'monthly' | 'never'.\n"
            "      target: number to reach (or stay under for direction=down).\n"
            "      unit: short string like 'set', 'rep', 'kg', 'min'.\n"
            "      direction: 'up' (more is better) | 'down' (less is better).\n"
            "    - completion_rule: optional. {kind: 'all_targets_met', "
            "trackers: [tracker_id, ...]} declares when today counts as "
            "done. Omit for a sensible default (all daily up-trackers met).\n"
            "    - nudge_rule: optional. {kind: 'behind_schedule', "
            "tracker: <id>, after_local_hour: 14, before_local_hour: 22, "
            "max_per_day: 3, min_gap_minutes: 90}. Use kind: 'none' to opt "
            "out of nags. Pick max_per_day + min_gap_minutes to match the "
            "goal's pace: a daily-water-intake goal might want 5 per day "
            "with 90-min gaps; a once-a-week run goal might want 1 per "
            "day with 240-min gaps. Defaults are 3 and 90 if omitted.\n"
            "    - log_hints: optional array helping the log classifier. "
            "Each entry: {if_mentions: [keywords], increment: {tracker_id: number}}.\n"
            "- milestones: 1 to 3 entries, each "
            "{due_date: YYYY-MM-DD, target_description: str}.\n\n"
            "Pick trackers that match HOW the user described the goal. "
            "For 'pushups after every prayer', model both sessions_today "
            "(counter, daily, target 5) and pushups_today (counter, daily, "
            "target should match an early-week starting volume the user "
            "can grow from). For 'lose 5 kg', use a weight gauge plus a "
            "weekly weigh-in counter. For 'sleep 7 hours nightly', a "
            "sleep_minutes gauge reset daily with target 420. "
            "Always include log_hints with the words you expect the user "
            "to use so the log classifier can map their messages to deltas.\n\n"
            f"{_now_context_line()}\n"
            "Return ONLY the JSON object."
        )
        user = f"Title: {title}\nDescription: {description}"
        resp = await self.llm.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=self.planner_model,
            temperature=0.4,
            response_format="json",
            think=False,
            keep_alive=120,
        )
        content = _extract_chat_content(resp)
        parsed = _parse_json_blob(content) or {}
        plan_text = str(parsed.get("plan_text") or "").strip()
        tracker_spec = parsed.get("tracker_spec")
        if not isinstance(tracker_spec, dict):
            tracker_spec = None
        milestones = parsed.get("milestones") or []
        if not isinstance(milestones, list):
            milestones = []
        return {
            "plan_text": plan_text,
            "tracker_spec": tracker_spec,
            "milestones": milestones,
        }

    # ── Read intents ─────────────────────────────────────────────

    async def _handle_check_progress(
        self, args: dict[str, Any], member_id: int,
        context: dict[str, Any] | None = None,
    ) -> tuple[str, int | None]:
        from . import goal_engine

        goals = await self.goals.list_active(member_id=member_id)
        if not goals:
            return (
                "You don't have any active goals right now. If you want "
                "to start one, just tell me what you're aiming for."
            ), None
        which = str(args.get("which") or "").lower()
        target_goal = _match_goal(goals, which)
        if target_goal is None and context and context.get("last_goal_id"):
            target_goal = next(
                (g for g in goals if int(g["id"]) == context["last_goal_id"]),
                None,
            )
        if target_goal is None:
            target_goal = goals[0]
        # Run the generic engine over recent log entries; this is the
        # source of truth, not the cached progress row (which is just
        # a nightly snapshot).
        log_rows = await self.goals.recent_log(int(target_goal["id"]), limit=400)
        if log_rows or target_goal.get("tracker_spec"):
            eval_result = goal_engine.evaluate(
                goal=target_goal, log_rows=log_rows,
            )
            line = goal_engine.format_status_line(eval_result)
            return (
                f"\"{target_goal['title']}\" — {line}",
                int(target_goal["id"]),
            )
        return (
            f"\"{target_goal['title']}\" is active but I don't have any "
            "logs yet. Tell me what you've done and I'll start tracking."
        ), int(target_goal["id"])

    async def _handle_list_goals(self, member_id: int) -> str:
        goals = await self.goals.list_all_for_member(
            member_id, include_archived=False,
        )
        if not goals:
            return "You don't have any goals yet."
        lines = [f"You have {len(goals)} active or paused goal" +
                 ("" if len(goals) == 1 else "s") + ":"]
        for g in goals:
            lines.append(f"- {g['title']} ({g['status']})")
        return "\n".join(lines)

    async def _handle_explain_plan(
        self, args: dict[str, Any], member_id: int,
        context: dict[str, Any] | None = None,
    ) -> tuple[str, int | None]:
        """Explain the plan for a specific goal: plan_text, trackers,
        milestones. Resolves the goal from explicit 'which' first,
        then conversational context, then the most-recent active
        goal."""
        from . import goal_engine

        goals = await self.goals.list_active(member_id=member_id)
        if not goals:
            return (
                "You don't have any active goals yet. Tell me what you "
                "want to work on and I'll write you a plan."
            ), None
        which = str(args.get("which") or "").lower()
        target = _match_goal(goals, which)
        if target is None and context and context.get("last_goal_id"):
            target = next(
                (g for g in goals if int(g["id"]) == context["last_goal_id"]),
                None,
            )
        if target is None:
            target = goals[0]
        milestones = await self.goals.list_milestones(int(target["id"]))
        bits = [f"Here's the plan for \"{target['title']}\":"]
        plan_text = (target.get("plan_text") or "").strip()
        if plan_text:
            bits.append(plan_text)
        else:
            bits.append(
                "I haven't written a detailed plan yet. Tell me a bit more "
                "about how you'd like to approach it and I'll fill it in."
            )
        spec = goal_engine.normalize_spec(target.get("tracker_spec"))
        if spec["trackers"]:
            tracker_lines = ["What I'm tracking:"]
            for t in spec["trackers"]:
                target_v = t.get("target")
                unit = t.get("unit") or ""
                label = t.get("label") or t.get("id") or "tracker"
                reset = t.get("reset") or "daily"
                direction_word = "≤" if t.get("direction") == "down" else "≥"
                if target_v is not None:
                    tracker_lines.append(
                        f"- {label}: {direction_word} {goal_engine._format_value(float(target_v))} "
                        f"{unit} per {reset}".replace("  ", " ").strip()
                    )
                else:
                    tracker_lines.append(f"- {label}")
            bits.append("\n".join(tracker_lines))
            # Also show current state if we have any logs
            log_rows = await self.goals.recent_log(int(target["id"]), limit=400)
            if log_rows:
                ev = goal_engine.evaluate(goal=target, log_rows=log_rows)
                bits.append("Current state: " + goal_engine.format_status_line(ev))
        if milestones:
            ms_lines = ["Milestones:"]
            for ms in milestones[:5]:
                due = ms.get("due_date")
                due_str = due.isoformat() if hasattr(due, "isoformat") else str(due or "")
                ms_lines.append(
                    f"- {due_str}: {ms.get('target_description', '')}"
                )
            bits.append("\n".join(ms_lines))
        bits.append(
            "If the cadence doesn't fit, just say so — \"make it 2 days a "
            "week\" or \"shift to Tue/Thu/Sat\" — and I'll rework it."
        )
        return "\n\n".join(b for b in bits if b), int(target["id"])

    # ── Workout actions ──────────────────────────────────────────

    async def _handle_skip_workout(
        self, args: dict[str, Any], member_id: int,
    ) -> tuple[str, int | None]:
        goals = await self.goals.list_active(member_id=member_id)
        if not goals:
            return (
                "No active goals to skip workouts on. If you want to "
                "track one, tell me what you're aiming for."
            ), None
        reason = str(args.get("reason") or "").strip() or None
        excused = []
        over_budget = []
        for g in goals:
            if not (g.get("workout_budget") or {}):
                continue
            used = await self.goals.excuses_this_week(int(g["id"]))
            budget = (g.get("workout_budget") or {}).get(
                "flexible_rest_per_week", 2
            )
            try:
                budget_int = int(budget)
            except (TypeError, ValueError):
                budget_int = 2
            if used >= budget_int:
                over_budget.append((g["title"], used, budget_int))
                continue
            await self.goals.excuse_today(int(g["id"]), note=reason)
            excused.append(g["title"])
        bits = []
        if excused:
            bits.append(
                "Okay, marking today as a rest day for " +
                _humanize_list(excused) + ". I'll keep quiet on the nags."
            )
        if over_budget:
            details = ", ".join(
                f"{title} (already used {used} of {budget})"
                for title, used, budget in over_budget
            )
            bits.append(
                "You've already hit your flexible-rest budget this week on "
                f"{details}. I'll still mark today excused but the weekly "
                "review will flag it."
            )
            for g in goals:
                if any(g["title"] == title for title, _, _ in over_budget):
                    await self.goals.excuse_today(int(g["id"]), note=reason)
        if not bits:
            bits.append("Nothing to skip — you don't have a workout planned "
                        "for today.")
        last_touched = goals[0]["id"] if goals else None
        return "\n\n".join(bits), int(last_touched) if last_touched else None

    async def _handle_log_workout(
        self, args: dict[str, Any], member_id: int,
        text: str = "",
        context: dict[str, Any] | None = None,
    ) -> tuple[str, int | None]:
        """Generic event logging — the user reports something they did
        ('30 pushups after maghrib', 'ran 5k', 'weighed in 88kg today')
        and a small LLM call maps it to tracker deltas using the
        target goal's spec. Inserts one health_goal_log row, then
        evaluates the goal so we can confirm with the new tracker
        state (e.g. '2 of 5 sets today, three to go')."""
        from . import goal_engine

        goals = await self.goals.list_active(member_id=member_id)
        if not goals:
            return ("Nice. I don't have an active goal on file, but "
                    "I'll remember you did something good today."), None
        target = None
        if context and context.get("last_goal_id"):
            target = next(
                (g for g in goals if int(g["id"]) == context["last_goal_id"]),
                None,
            )
        if target is None:
            target = goals[0]
        spec = goal_engine.normalize_spec(target.get("tracker_spec"))
        if not spec["trackers"]:
            # Self-heal: goals created under the old engine (or where
            # the planner LLM failed at creation) have no trackers.
            # Generate one on the fly rather than telling the user to
            # set it up — they're trying to log activity, not configure
            # software.
            try:
                generated = await self._generate_plan(
                    title=target["title"],
                    description=target.get("description") or target["title"],
                )
            except Exception as exc:
                logger.warning("goals_chat_autoheal_planner_failed",
                               goal_id=target["id"], error=str(exc))
                generated = None
            new_spec = (generated or {}).get("tracker_spec")
            if new_spec and isinstance(new_spec, dict):
                try:
                    await self.goals.update_plan(
                        int(target["id"]),
                        plan_text=(generated.get("plan_text")
                                    or target.get("plan_text") or ""),
                        tracker_spec=new_spec,
                    )
                    # Re-fetch with the new spec inline so the rest of
                    # this call works against it.
                    target = {**target, "tracker_spec": new_spec}
                    spec = goal_engine.normalize_spec(new_spec)
                except Exception as exc:
                    logger.warning("goals_chat_autoheal_persist_failed",
                                   goal_id=target["id"], error=str(exc))
            if not spec["trackers"]:
                return (
                    f"\"{target['title']}\" doesn't have any trackers set up "
                    "yet. Tell me how you'd like me to measure it."
                ), int(target["id"])
        raw_text = (args.get("note") or text or "").strip() or "logged via chat"
        try:
            deltas, event_ts = await self._classify_log_deltas(
                spec=spec, goal_title=target["title"], raw_text=raw_text,
            )
        except Exception as exc:
            logger.warning("goals_chat_log_classify_failed", error=str(exc))
            deltas = _fallback_deltas_from_hints(spec, raw_text)
            event_ts = None
        if not deltas:
            return (
                "I heard you, but I couldn't map that to anything I'm "
                "tracking. Try naming the action (e.g. 'did 20 pushups')."
            ), int(target["id"])
        await self.goals.record_log_event(
            int(target["id"]),
            deltas=deltas,
            raw_text=raw_text,
            member_id=member_id,
            source="telegram",
            ts=event_ts,  # None → store defaults to now()
        )
        # Re-evaluate so the confirmation is grounded in real state.
        log_rows = await self.goals.recent_log(int(target["id"]), limit=200)
        eval_result = goal_engine.evaluate(goal=target, log_rows=log_rows)
        bits = [_humanize_log_delta(deltas, spec, event_ts=event_ts)]
        bits.append(goal_engine.format_status_line(eval_result))
        return " ".join(b for b in bits if b), int(target["id"])

    async def _classify_log_deltas(
        self, *, spec: dict[str, Any], goal_title: str, raw_text: str,
    ) -> tuple[dict[str, float], datetime | None]:
        """Small LLM call: map free-text into per-tracker deltas AND
        deduce when the event happened.

        Returns (deltas, ts_or_none). The LLM is given the current
        local time + timezone so it can resolve phrasings like
        'earlier today', 'yesterday after maghrib', 'this morning' to
        a concrete ISO timestamp. If no time signal is present, the
        ts comes back None and the caller defaults to now()."""
        trackers_brief = "; ".join(
            f"{t['id']} ({t['label']}, {t.get('unit') or 'unit'})"
            for t in spec["trackers"]
        )
        hints = spec.get("log_hints") or []
        hints_text = json.dumps(hints[:6]) if hints else "(none)"
        now_ctx = _now_context_line()
        system = (
            "You convert a user's free-text report of something they did "
            "into per-tracker numeric deltas for one goal, AND deduce when "
            "the event happened. Be conservative: only count what the "
            "user actually said.\n\n"
            "Return ONLY a JSON object with these keys:\n"
            "- deltas: {<tracker_id>: <number>, ...}. Unknown trackers or "
            "ambiguous quantities → omit them. Numbers must be positive "
            "for counters (additions). For gauges, return the latest "
            "reported absolute value.\n"
            "- ts_iso: ISO-8601 datetime with timezone (e.g. "
            "'2026-05-31T13:30:00+04:00') if the user gave a time signal "
            "you can resolve (e.g. 'earlier today', 'yesterday morning', "
            "'after Dhuhr', 'right after I woke up', 'last Friday'). "
            "Use your knowledge of approximate Islamic prayer times for "
            "the given location and date when relevant. Use null when "
            "there's no temporal hint — the caller will default to now.\n"
            "- reasoning_brief: 1 short sentence on how you interpreted "
            "the time signal (or 'none').\n\n"
            f"{now_ctx}\n"
            f"Goal: {goal_title}\n"
            f"Trackers available: {trackers_brief}\n"
            f"Log hints: {hints_text}"
        )
        try:
            resp = await self.llm.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": raw_text},
                ],
                model=self.classifier_model,
                temperature=0.0,
                response_format="json",
                think=False,
            )
        except Exception as exc:
            logger.warning("goals_chat_log_llm_failed", error=str(exc))
            return _fallback_deltas_from_hints(spec, raw_text), None
        content = _extract_chat_content(resp)
        parsed = _parse_json_blob(content) or {}
        valid_ids = {t["id"] for t in spec["trackers"]}
        # Backwards-compat: older prompt returned bare {tracker_id: n}.
        # New shape is {deltas: {...}, ts_iso: ...}.
        if "deltas" in parsed and isinstance(parsed["deltas"], dict):
            deltas_raw = parsed["deltas"]
            ts_iso = parsed.get("ts_iso")
        else:
            deltas_raw = {k: v for k, v in parsed.items()
                          if k not in {"ts_iso", "reasoning_brief"}}
            ts_iso = parsed.get("ts_iso")
        out: dict[str, float] = {}
        for k, v in deltas_raw.items():
            if k not in valid_ids:
                continue
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                continue
        ts = _parse_ts_hint(ts_iso)
        return out, ts

    # ── Goal state ───────────────────────────────────────────────

    async def _handle_goal_state(
        self, intent: str, args: dict[str, Any], member_id: int,
        context: dict[str, Any] | None = None,
    ) -> tuple[str, int | None]:
        goals = await self.goals.list_active(member_id=member_id)
        if not goals:
            return "No active goals to change.", None
        which = str(args.get("which") or "").lower()
        target = _match_goal(goals, which)
        if target is None and context and context.get("last_goal_id"):
            target = next(
                (g for g in goals if int(g["id"]) == context["last_goal_id"]),
                None,
            )
        if target is None:
            target = goals[0]
        gid = int(target["id"])
        title = target["title"]
        if intent == "pause_goal":
            await self.goals.set_status(gid, "paused", note="user requested")
            return f"Paused \"{title}\". Tell me when you want to resume.", gid
        if intent == "resume_goal":
            await self.goals.set_status(gid, "active", note="user requested")
            return f"Resumed \"{title}\".", gid
        if intent == "abandon_goal":
            await self.goals.set_status(gid, "abandoned", note="user requested")
            return (
                f"Done — \"{title}\" is off the active list. No judgment, "
                "we can pick a different one whenever."
            ), gid
        if intent == "mute_goal":
            hours = args.get("duration_hours")
            try:
                hours_int = int(hours)
            except (TypeError, ValueError):
                hours_int = 24
            until = datetime.now(UTC) + timedelta(hours=hours_int)
            await self.goals.set_quiet_until(gid, until=until)
            return (
                f"Muted \"{title}\" for {hours_int} hour" +
                ("" if hours_int == 1 else "s") + "."
            ), gid
        if intent == "unmute_goal":
            await self.goals.set_quiet_until(gid, until=None)
            return f"Unmuted \"{title}\".", gid
        return "Done.", gid

    # ── Nag windows ──────────────────────────────────────────────

    async def _handle_set_nag_windows(
        self, args: dict[str, Any], member_id: int,
    ) -> str:
        kwargs: dict[str, int] = {}
        for key in ("weekday_start_hour", "weekday_end_hour",
                    "weekend_start_hour", "weekend_end_hour"):
            v = args.get(key)
            if v is None:
                continue
            try:
                kwargs[key] = int(v)
            except (TypeError, ValueError):
                continue
        if not kwargs:
            return (
                "I picked up a notification-window change but couldn't tell "
                "what to update. Try something like 'don't nag me before "
                "6pm on weekdays'."
            )
        try:
            updated = await self.nag.set(member_id, **kwargs)
        except ValueError as exc:
            return f"Couldn't update windows — {exc}"
        return (
            "Updated. Weekdays " +
            f"{updated['weekday_start_hour']:02d}:00 to "
            f"{updated['weekday_end_hour']:02d}:00, weekends " +
            f"{updated['weekend_start_hour']:02d}:00 to "
            f"{updated['weekend_end_hour']:02d}:00."
        )

    # ── Chores ───────────────────────────────────────────────────

    async def _handle_complete_chore(
        self, args: dict[str, Any], member_id: int,
    ) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return "Which chore did you finish? Tell me by name."
        tid = await self.chores.log_by_name(
            name, member_id=member_id, source="telegram",
        )
        if tid is None:
            return (
                f"I couldn't match '{name}' to a chore I'm tracking. "
                "You can see the list on the chores dashboard."
            )
        return f"Logged. Nice work."

    async def _handle_list_chores(self) -> str:
        rows = await self.chores.list_status(include_recent=False)
        overdue = [r for r in rows if r.status == "overdue"]
        due_today = [r for r in rows if r.status == "due_today"]
        soon = [r for r in rows if r.status == "soon"]
        if not (overdue or due_today or soon):
            return "Nothing on the chore list right now. Enjoy."
        bits = []
        if overdue:
            bits.append(
                "Overdue: " +
                _humanize_list([f"{r.name} ({r.days_overdue}d late)"
                                 for r in overdue])
            )
        if due_today:
            bits.append("Due today: " + _humanize_list([r.name for r in due_today]))
        if soon:
            bits.append("Coming up: " + _humanize_list([r.name for r in soon]))
        return ". ".join(bits) + "."

    # ── Weekly review ────────────────────────────────────────────

    async def _handle_weekly_review(self, member_id: int) -> str:
        goals = await self.goals.list_active(member_id=member_id)
        if not goals:
            return "No active goals to review."
        lines = []
        for g in goals:
            prog_rows = await self.goals.recent_progress(int(g["id"]), days=7)
            workouts = sum(1 for r in prog_rows if r.get("workout_completed"))
            target = (g.get("workout_budget") or {}).get("required_per_week")
            line = f"- {g['title']}: {workouts} workout" + (
                "" if workouts == 1 else "s") + " this week"
            if target:
                line += f" (target {target})"
            lines.append(line)
        return "Here's the week so far:\n" + "\n".join(lines)

    # ── Conversational context (Redis-backed, 30-min TTL) ────────

    async def _load_context(self, member_id: int) -> dict[str, Any] | None:
        """Read the per-member context blob if present + still valid.
        Augments it with the goal title (cheap lookup) and an
        age_seconds field for the classifier prompt."""
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(CONTEXT_KEY_PREFIX + str(member_id))
        except Exception as exc:
            logger.warning("goals_chat_context_load_failed", error=str(exc))
            return None
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            blob = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(blob, dict):
            return None
        goal_id = blob.get("last_goal_id")
        title = None
        if isinstance(goal_id, int):
            try:
                goal = await self.goals.get(goal_id)
                if goal and goal.get("status") in {"active", "paused"}:
                    title = goal.get("title")
                else:
                    # Stale (goal abandoned / wrong member) — drop the context
                    return None
            except Exception:
                pass
        ts = blob.get("ts")
        age = None
        try:
            if ts:
                age = max(0, int(
                    datetime.now(UTC).timestamp() -
                    datetime.fromisoformat(ts).timestamp()
                ))
        except (TypeError, ValueError):
            age = None
        return {
            "last_goal_id": goal_id,
            "last_goal_title": title,
            "last_intent": blob.get("last_intent"),
            "age_seconds": age,
        }

    async def _save_context(
        self, member_id: int, *, last_goal_id: int, last_intent: str,
    ) -> None:
        """Stash the most-recent touched goal so a follow-up question
        resolves to it. TTL keeps stale context from leaking forward."""
        if self.redis is None:
            return
        blob = {
            "last_goal_id": int(last_goal_id),
            "last_intent": last_intent,
            "ts": datetime.now(UTC).isoformat(),
        }
        try:
            await self.redis.set(
                CONTEXT_KEY_PREFIX + str(member_id),
                json.dumps(blob),
                ex=CONTEXT_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning("goals_chat_context_save_failed", error=str(exc))


# ── Helpers ──────────────────────────────────────────────────────


def _extract_chat_content(resp: dict[str, Any]) -> str:
    """Pull the assistant message text out of an Ollama chat response."""
    msg = resp.get("message") or {}
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
    return ""


def _parse_json_blob(content: str) -> dict[str, Any] | None:
    """Forgiving JSON parser. Strips a code-fence wrapper if the model
    decided to wrap, and falls back to grabbing the first {...} span."""
    if not content:
        return None
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        out = json.loads(stripped)
        return out if isinstance(out, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match:
        try:
            out = json.loads(match.group(0))
            return out if isinstance(out, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


def _match_goal(
    goals: list[dict[str, Any]], hint: str,
) -> dict[str, Any] | None:
    if not hint:
        return None
    h = hint.lower()
    for g in goals:
        if h in str(g.get("title") or "").lower():
            return g
    for g in goals:
        if h in str(g.get("description") or "").lower():
            return g
    return None


def _humanize_list(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _humanize_log_delta(
    deltas: dict[str, float], spec: dict[str, Any],
    *,
    event_ts: datetime | None = None,
) -> str:
    """Render the LLM's parsed deltas as a single confirmation sentence.

    When event_ts is provided and meaningfully different from 'now'
    (more than ~10 min off), include a phrase showing we understood
    the time signal — so the user can trust we logged it correctly."""
    if not deltas:
        return "Logged."
    by_id = {t["id"]: t for t in (spec.get("trackers") or [])}
    bits = []
    for tid, value in deltas.items():
        t = by_id.get(tid, {})
        label = t.get("label") or tid
        unit = t.get("unit") or ""
        v_str = f"{value:.1f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))
        bits.append(f"{v_str} {unit} {label.lower()}".strip().replace("  ", " "))
    when_phrase = ""
    if event_ts is not None:
        now = datetime.now(UTC)
        if abs((now - event_ts.astimezone(UTC)).total_seconds()) > 600:
            local = event_ts.astimezone(ZoneInfo("Asia/Dubai"))
            today_local = datetime.now(ZoneInfo("Asia/Dubai")).date()
            if local.date() == today_local:
                when_phrase = f" earlier today at {local.strftime('%H:%M')}"
            elif (today_local - local.date()).days == 1:
                when_phrase = f" yesterday at {local.strftime('%H:%M')}"
            else:
                when_phrase = f" on {local.strftime('%a %d %b at %H:%M')}"
    return f"Logged{when_phrase}: " + ", ".join(bits) + "."


def _now_context_line() -> str:
    """One-line current-time context for any LLM system prompt.
    Helps the model resolve 'earlier today', 'yesterday', 'after dhuhr',
    'this morning', and pick reasonable due dates."""
    tz = ZoneInfo("Asia/Dubai")
    local = datetime.now(tz)
    return (
        f"It is currently {local.strftime('%A, %d %B %Y, %H:%M')} "
        f"in Asia/Dubai. The user is in the UAE."
    )


def _parse_ts_hint(raw: Any) -> datetime | None:
    """Validate an LLM-supplied ISO timestamp. Rejects anything in the
    future or more than 14 days in the past — those are almost always
    hallucinations. Returns a tz-aware datetime or None."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # Tolerate trailing 'Z'
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        # Assume Dubai-local if the LLM forgot the offset
        ts = ts.replace(tzinfo=ZoneInfo("Asia/Dubai"))
    now = datetime.now(UTC)
    ts_utc = ts.astimezone(UTC)
    # Reject hallucinated futures (allow up to 5 min clock skew)
    if ts_utc > now + timedelta(minutes=5):
        return None
    # Reject anything older than 14 days
    if ts_utc < now - timedelta(days=14):
        return None
    return ts


def _fallback_deltas_from_hints(
    spec: dict[str, Any], raw_text: str,
) -> dict[str, float]:
    """Cheap fallback when the LLM call fails: scan log_hints for
    keyword matches and apply their increments (numeric only)."""
    text_low = raw_text.lower()
    out: dict[str, float] = {}
    for hint in spec.get("log_hints") or []:
        if not isinstance(hint, dict):
            continue
        triggers = hint.get("if_mentions") or []
        if not any(isinstance(t, str) and t.lower() in text_low for t in triggers):
            continue
        increments = hint.get("increment") or {}
        if not isinstance(increments, dict):
            continue
        for tid, val in increments.items():
            try:
                out[tid] = out.get(tid, 0.0) + float(val)
            except (TypeError, ValueError):
                continue
    return out


def _format_progress_line(
    goal: dict[str, Any], prog: dict[str, Any],
) -> str:
    title = goal["title"]
    if not prog:
        return (
            f"\"{title}\" is active but I haven't computed today's "
            "progress yet. The nightly compute lands around 23:30."
        )
    label = prog.get("on_track_label") or "unknown"
    snap = prog.get("metric_snapshots") or {}
    workouts_week = snap.get("workouts_this_week")
    target_week = (goal.get("workout_budget") or {}).get("required_per_week")
    bits = [f"\"{title}\": you're {label.replace('_', ' ')} today."]
    if workouts_week is not None and target_week:
        bits.append(
            f"Workouts this week: {workouts_week} of {target_week}."
        )
    if prog.get("workout_required"):
        if prog.get("workout_completed"):
            bits.append("Today's workout is done. Nice.")
        elif prog.get("rest_day_excused"):
            bits.append("Today's marked as a rest day.")
        else:
            bits.append("Today's workout is still pending.")
    return " ".join(bits)
