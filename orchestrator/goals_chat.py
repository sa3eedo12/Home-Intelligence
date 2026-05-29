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
    "create_goal", "check_progress", "list_goals",
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
                f"valid for ~{ttl_min} more minutes). If the new message "
                "is a vague follow-up like 'what would the plan involve', "
                "'tell me more', 'what does it look like', 'how does it "
                "work', treat it as 'explain_plan' on that goal. If they "
                "say 'skip it', 'mute it', 'pause it' without naming a "
                "different goal, apply to this one. Don't override an "
                "obviously-different intent."
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
            "- create_goal: user wants to start a new health goal. "
            "args: {\"title\": str, \"description\": str}.\n"
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
            "{\"intent\": \"general_chat\"}\n"
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
        if intent == "check_progress":
            return await self._handle_check_progress(args, member_id, context)
        if intent == "list_goals":
            return await self._handle_list_goals(member_id), None
        if intent == "explain_plan":
            return await self._handle_explain_plan(args, member_id, context)
        if intent == "skip_workout":
            return await self._handle_skip_workout(args, member_id)
        if intent == "log_workout":
            return await self._handle_log_workout(args, member_id)
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
                    "I will check in daily and nudge you when a workout "
                    "is due. We can refine the plan as we learn what "
                    "works for you."
                ),
                "metric_links": [],
                "workout_budget": None,
                "milestones": [],
            }
        goal_id = await self.goals.create(
            member_id=member_id,
            title=title,
            description=description,
            metric_links=plan.get("metric_links") or [],
            workout_budget=plan.get("workout_budget"),
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
        budget = plan.get("workout_budget") or {}
        per_week = budget.get("required_per_week")
        bits = [
            f"Got it — your new goal is \"{title}\".",
            plan.get("plan_text") or "",
        ]
        if per_week:
            bits.append(
                f"I'll expect about {per_week} workouts a week and check in "
                f"on the day if you haven't logged one yet."
            )
        bits.append(
            "Want the full plan with milestones? Just ask — say \"what "
            "does the plan look like\". Skip a day anytime by saying so; "
            "to silence nags on this goal say \"mute it\"."
        )
        return "\n\n".join(b for b in bits if b), goal_id

    async def _generate_plan(
        self, *, title: str, description: str,
    ) -> dict[str, Any]:
        """One 14b call. Returns plan_text + metric_links + workout_budget
        + milestones. Falls back to an empty plan if parsing fails."""
        system = (
            "You are a calm, practical health coach. The user told you a "
            "goal in plain English. Write a short personal plan and "
            "decide what to track.\n\n"
            "Output a JSON object with these keys:\n"
            "- plan_text: 2 to 4 sentences. Friendly and concrete. Avoid "
            "fitness clichés. Avoid commands like 'do X every day'.\n"
            "- metric_links: a JSON array. Each entry is "
            "{metric, direction, target_per_week?, target?, days_preferred?}.\n"
            "  Allowed metric names: workout, weight, steps, hrv, "
            "resting_heart_rate, sleep_asleep.\n"
            "- workout_budget: object if the goal needs workouts, else null. "
            "Keys: required_per_week:int, flexible_rest_per_week:int, "
            "days_preferred: list of 3-letter day names like 'mon','wed','fri'.\n"
            "- milestones: 1 to 3 entries, each "
            "{due_date: YYYY-MM-DD, target_description: str}.\n\n"
            "Today is " + date.today().isoformat() + ". The user is in "
            "Asia/Dubai timezone.\n"
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
        metric_links = parsed.get("metric_links") or []
        if not isinstance(metric_links, list):
            metric_links = []
        budget = parsed.get("workout_budget")
        if not isinstance(budget, dict):
            budget = None
        milestones = parsed.get("milestones") or []
        if not isinstance(milestones, list):
            milestones = []
        return {
            "plan_text": plan_text,
            "metric_links": metric_links,
            "workout_budget": budget,
            "milestones": milestones,
        }

    # ── Read intents ─────────────────────────────────────────────

    async def _handle_check_progress(
        self, args: dict[str, Any], member_id: int,
        context: dict[str, Any] | None = None,
    ) -> tuple[str, int | None]:
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
        prog = await self.goals.get_progress(int(target_goal["id"])) or {}
        return _format_progress_line(target_goal, prog), int(target_goal["id"])

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
        """Explain the plan for a specific goal: plan_text, weekly
        cadence, milestones. Resolves the goal from explicit 'which'
        first, then conversational context, then the most-recent
        active goal."""
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
        budget = target.get("workout_budget") or {}
        if isinstance(budget, dict) and budget:
            cadence_bits = []
            if budget.get("required_per_week"):
                cadence_bits.append(
                    f"{budget['required_per_week']} workouts per week"
                )
            if budget.get("flexible_rest_per_week"):
                cadence_bits.append(
                    f"up to {budget['flexible_rest_per_week']} flexible "
                    "rest day" +
                    ("" if budget["flexible_rest_per_week"] == 1 else "s")
                )
            if budget.get("days_preferred"):
                cadence_bits.append(
                    "leaning toward " + _humanize_list(
                        [str(d) for d in budget["days_preferred"]]
                    )
                )
            if cadence_bits:
                bits.append("Cadence: " + ", ".join(cadence_bits) + ".")
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
    ) -> tuple[str, int | None]:
        # We log against the chore_log table as a "Laundry load"-style
        # entry? No — workouts are tracked in health_metrics. The user's
        # Apple Watch / HealthKit Auto Export populates that. For now,
        # just acknowledge so the user doesn't feel ignored, and update
        # today's progress as workout_completed=True so the nag stops.
        goals = await self.goals.list_active(member_id=member_id)
        marked = 0
        last_touched = None
        for g in goals:
            if not (g.get("workout_budget") or {}):
                continue
            today = date.today()
            existing = await self.goals.get_progress(int(g["id"]), day=today) or {}
            snapshot = existing.get("metric_snapshots") or {}
            await self.goals.upsert_progress(
                int(g["id"]), day=today,
                metric_snapshots=snapshot,
                on_track_score=existing.get("on_track_score"),
                on_track_label=existing.get("on_track_label"),
                workout_required=bool(existing.get("workout_required") or True),
                workout_completed=True,
                rest_day_excused=bool(existing.get("rest_day_excused") or False),
                note=str(args.get("note") or "logged via chat") or None,
            )
            marked += 1
            last_touched = int(g["id"])
        if marked == 0:
            return ("Nice. I don't have an active workout goal on file, but "
                    "I'll remember you trained today."), None
        return (
            f"Nice — logged today's workout against {marked} goal" +
            ("" if marked == 1 else "s") + ". I'll back off the nags."
        ), last_touched

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
