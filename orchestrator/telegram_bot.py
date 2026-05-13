from __future__ import annotations

import re
from typing import Any

import httpx
from home_agents_sdk.telemetry import get_logger
from redis.asyncio import Redis
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .pending import clear_pending, get_pending, set_pending
from .policy_engine import PolicyEngine
from .router import Router
from .scheduler import Scheduler
from .voice import transcribe
from .workflow import WorkflowEngine

logger = get_logger("telegram_bot")

_CONFIRM_RE = re.compile(r"^\s*(yes|yeah|yep|sure|confirm|do it|ok|okay|y)\b", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^\s*(no|nope|cancel|abort|stop|n)\b", re.IGNORECASE)


def _is_allowed(user_id: int, allowed_ids: set[int]) -> bool:
    return user_id in allowed_ids


def _confirmation_action(text: str) -> str | None:
    if _CONFIRM_RE.search(text):
        return "confirm"
    if _CANCEL_RE.search(text):
        return "cancel"
    return None


def _chat_id(update: Update) -> int | None:
    effective_chat = getattr(update, "effective_chat", None)
    if effective_chat is not None:
        return int(effective_chat.id)
    message = getattr(update, "message", None)
    if message is not None:
        chat_id = getattr(message, "chat_id", None)
        if chat_id is not None:
            return int(chat_id)
        chat = getattr(message, "chat", None)
        if chat is not None and getattr(chat, "id", None) is not None:
            return int(chat.id)
    query = getattr(update, "callback_query", None)
    if query is not None:
        message = getattr(query, "message", None)
        chat_id = getattr(message, "chat_id", None)
        if chat_id is not None:
            return int(chat_id)
        chat = getattr(message, "chat", None)
        if chat is not None and getattr(chat, "id", None) is not None:
            return int(chat.id)
    return None


async def resolve_member(chat_id: int, knowledge_graph: Any | None = None) -> dict[str, Any] | None:
    if knowledge_graph is None:
        return None
    get_member = getattr(knowledge_graph, "get_member_by_chat_id", None)
    if not callable(get_member):
        return None
    try:
        return await get_member(int(chat_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_member_lookup_failed", chat_id=chat_id, error=str(exc))
        return None


def _telegram_first_name(update: Update) -> str:
    user = getattr(update, "effective_user", None)
    if user is None:
        query = getattr(update, "callback_query", None)
        user = getattr(query, "from_user", None) if query is not None else None
    first_name = str(getattr(user, "first_name", "") or "").strip()
    full_name = str(getattr(user, "full_name", "") or "").strip()
    username = str(getattr(user, "username", "") or "").strip()
    return first_name or full_name or username or "this Telegram user"


async def _record_unknown_chat_proposal(
    *,
    update: Update,
    chat_id: int,
    proposal_store: Any | None,
    redis: Redis | None,
) -> None:
    if proposal_store is None:
        return
    cache_key = f"telegram:unknown_chat_proposal:{chat_id}"
    if redis is not None:
        try:
            created = await redis.set(cache_key, "1", ex=7 * 24 * 3600, nx=True)
            if not created:
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram_unknown_chat_cache_failed", chat_id=chat_id, error=str(exc))
    first_name = _telegram_first_name(update)
    title = f"I see a new chat from {first_name} — is this someone in the household?"
    try:
        await proposal_store.add_proposal(
            kind="household_inference",
            title=title,
            rationale=(
                f"Telegram chat_id {chat_id} is allowed but not linked to a household member."
            ),
            evidence_event_ids=[],
            confidence=0.5,
            status="pending",
            delivery_channel="inbox",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram_unknown_chat_proposal_failed", chat_id=chat_id, error=str(exc))


async def _member_for_update(
    update: Update,
    knowledge_graph: Any | None,
    proposal_store: Any | None,
    redis: Redis | None,
) -> dict[str, Any] | None:
    chat_id = _chat_id(update)
    if chat_id is None:
        return None
    member = await resolve_member(chat_id, knowledge_graph)
    if member is None:
        await _record_unknown_chat_proposal(
            update=update,
            chat_id=chat_id,
            proposal_store=proposal_store,
            redis=redis,
        )
    return member


def _member_kwargs(member: dict[str, Any] | None) -> dict[str, Any]:
    if not member:
        return {"member_id": None, "member_name": None}
    try:
        member_id = int(member.get("id"))
    except (TypeError, ValueError):
        member_id = None
    return {"member_id": member_id, "member_name": member.get("name")}


async def _reply(update: Update, text: str) -> None:
    if update.message is not None:
        await update.message.reply_text(text)


def _parse_minutes(raw: str | None, default_minutes: int, max_minutes: int) -> int:
    if raw is None or not raw.isdigit():
        return default_minutes
    return min(max_minutes, max(1, int(raw)))


async def _set_mute(policy_engine: PolicyEngine, key: str, minutes: int) -> int:
    ttl_seconds = minutes * 60
    await policy_engine._redis.set(f"policy:mute:{key}", "1", ex=ttl_seconds)
    return ttl_seconds


def _make_start(allowed_ids: set[int]):
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        await _reply(
            update,
            "👋 Hello! I'm your Home Intelligence assistant."
            " Send me a message or /ask to get started.",
        )

    return start


def _make_help(allowed_ids: set[int]):
    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        text = (
            "Available commands:\n"
            "/start – Welcome message\n"
            "/help – Show this help\n"
            "/status – Show agent status\n"
            "/ask <message> – Route a request\n"
            "/quiet on|off|status – Quiet-hours override\n"
            "/mute <agent|topic> [minutes] – Temporarily mute notifications\n"
            "/unmute <agent|topic> – Remove mute\n"
            "/cancel – Cancel a pending confirmation\n"
            "/jobs – List scheduled jobs\n"
            "/jobs run <id> – Run a job now\n"
            "Or just send a message directly."
        )
        await _reply(update, text)

    return help_cmd


def _make_status(allowed_ids: set[int]):
    async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        registry = context.bot_data.get("registry")
        if registry is not None:
            agents = registry.agents()
            text = f"Registered agents: {', '.join(agents) if agents else 'none'}"
        else:
            text = "Registry not available."
        await _reply(update, text)

    return status_cmd


def _make_ask(
    allowed_ids: set[int],
    router: Router,
    redis: Redis,
    knowledge_graph: Any | None = None,
    proposal_store: Any | None = None,
):
    async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        text = " ".join(context.args or [])
        if not text:
            await _reply(update, "Usage: /ask <your request>")
            return
        user_id = str(update.effective_user.id)
        member = await _member_for_update(update, knowledge_graph, proposal_store, redis)
        result = await router.handle(text, user_id, **_member_kwargs(member))
        await _send_result(update, context, result, redis)

    return ask_cmd


async def _handle_pending_text(update: Update, router: Router, redis: Redis, text: str) -> bool:
    chat_id = _chat_id(update)
    if chat_id is None:
        return False
    pending = await get_pending(redis, chat_id)
    if pending is None:
        return False
    action = _confirmation_action(text)
    if action is None:
        return False

    if action == "cancel":
        await clear_pending(redis, chat_id)
        await _reply(update, "Cancelled.")
        return True

    try:
        result = await router.execute_pending(pending)
        reply_text = result.get("reply", "Done.")
    except Exception as exc:
        logger.warning("pending_text_execute_failed", error=str(exc))
        reply_text = f"Failed: {exc}"
    finally:
        await clear_pending(redis, chat_id)
    await _reply(update, reply_text)
    return True


def _make_text(
    allowed_ids: set[int],
    router: Router,
    redis: Redis,
    knowledge_graph: Any | None = None,
    proposal_store: Any | None = None,
):
    async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        text = update.message.text or ""
        if await _handle_pending_text(update, router, redis, text):
            return
        user_id = str(update.effective_user.id)
        member = await _member_for_update(update, knowledge_graph, proposal_store, redis)
        result = await router.handle(text, user_id, **_member_kwargs(member))
        await _send_result(update, context, result, redis)

    return text_handler


def _make_voice(
    allowed_ids: set[int],
    router: Router,
    redis: Redis,
    knowledge_graph: Any | None = None,
    proposal_store: Any | None = None,
):
    async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        try:
            text = await transcribe(update.message.voice, context.bot)
        except Exception as exc:
            logger.warning("voice_transcribe_failed", error=str(exc))
            await _reply(update, "Sorry, I couldn't transcribe that voice message.")
            return
        user_id = str(update.effective_user.id)
        member = await _member_for_update(update, knowledge_graph, proposal_store, redis)
        result = await router.handle(text, user_id, **_member_kwargs(member))
        await _send_result(update, context, result, redis)

    return voice_handler


def _make_photo(
    allowed_ids: set[int],
    router: Router,
    redis: Redis,
    knowledge_graph: Any | None = None,
    proposal_store: Any | None = None,
):
    async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        caption = update.message.caption or ""
        text = f"Look at this image: {caption}"
        user_id = str(update.effective_user.id)
        member = await _member_for_update(update, knowledge_graph, proposal_store, redis)
        result = await router.handle(text, user_id, **_member_kwargs(member))
        await _send_result(update, context, result, redis)

    return photo_handler


def _make_quiet(allowed_ids: set[int], policy_engine: PolicyEngine):
    async def quiet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        mode = (context.args[0] if context.args else "status").lower()
        overrides = policy_engine.policies.get("manual_overrides", {})
        ttl_minutes = int(overrides.get("ttl_minutes_default", 60))
        ttl_seconds = ttl_minutes * 60

        if mode == "on":
            await policy_engine.set_quiet_override("on", ttl_seconds)
            await _reply(update, f"Quiet hours override set to ON for {ttl_minutes} min.")
            return
        if mode == "off":
            await policy_engine.clear_quiet_override()
            active = await policy_engine.quiet_hours_active()
            await _reply(
                update,
                f"Quiet override cleared. Quiet hours are {'active' if active else 'inactive'}.",
            )
            return
        if mode == "status":
            active = await policy_engine.quiet_hours_active()
            await _reply(update, f"Quiet hours are {'active' if active else 'inactive'}.")
            return

        await _reply(update, "Usage: /quiet on|off|status")

    return quiet_cmd


def _make_mute(allowed_ids: set[int], policy_engine: PolicyEngine):
    async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        if not context.args:
            await _reply(update, "Usage: /mute <agent|topic> [minutes]")
            return
        key = context.args[0]
        overrides = policy_engine.policies.get("manual_overrides", {})
        default_minutes = int(overrides.get("ttl_minutes_default", 60))
        max_minutes = int(overrides.get("max_minutes", 720))
        minutes = _parse_minutes(
            context.args[1] if len(context.args) > 1 else None, default_minutes, max_minutes
        )
        ttl = await _set_mute(policy_engine, key, minutes)
        await _reply(update, f"Muted {key} for {ttl} seconds.")

    return mute_cmd


def _make_unmute(allowed_ids: set[int], policy_engine: PolicyEngine):
    async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        if not context.args:
            await _reply(update, "Usage: /unmute <agent|topic>")
            return
        key = context.args[0]
        await policy_engine._redis.delete(f"policy:mute:{key}")
        await _reply(update, f"Unmuted {key}.")

    return unmute_cmd


def _make_cancel(allowed_ids: set[int], redis: Redis):
    async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        chat_id = _chat_id(update)
        if chat_id is None:
            return
        pending = await get_pending(redis, chat_id)
        await clear_pending(redis, chat_id)
        if pending is None:
            await _reply(update, "No pending action to cancel.")
        else:
            await _reply(update, "Cancelled pending action.")

    return cancel_cmd


def _make_jobs(allowed_ids: set[int], scheduler: Scheduler, admin_base_url: str):
    async def jobs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        if context.args[:1] == ["run"] and len(context.args) > 1:
            job_id = context.args[1]
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(f"{admin_base_url.rstrip('/')}/admin/run-job/{job_id}")
                    resp.raise_for_status()
                await _reply(update, f"Job {job_id} run result: {resp.json()}")
            except Exception as exc:
                await _reply(update, f"Failed to run job {job_id}: {exc}")
            return

        lines = ["Scheduled jobs:"]
        for job in scheduler.list_jobs():
            lines.append(f"- {job.id}: next={job.next_run_time or 'n/a'} status={job.last_status}")
        await _reply(update, "\n".join(lines))

    return jobs_cmd


def _make_callback(
    allowed_ids: set[int], workflow_engine: WorkflowEngine, router: Router, redis: Redis
):
    async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None:
            return
        if not _is_allowed(query.from_user.id, allowed_ids):
            return
        await query.answer()
        data = query.data or ""
        parts = data.split(":", 2)
        if len(parts) < 2:
            return
        action, workflow_id = parts[0], parts[1]

        if action == "cycle":
            await _handle_cycle_callback(update, query, parts, router)
            return

        if action == "clean":
            await _handle_clean_callback(update, query, parts, router)
            return

        if action not in {"confirm", "cancel"}:
            return

        if workflow_id == "pending":
            await _handle_pending_callback(update, query, action, router, redis)
            return

        chat_id = _chat_id(update)
        try:
            user_choice = {"action": action}
            payload = await workflow_engine.resume(workflow_id, user_choice)
            if action == "confirm":
                agent = payload.get("agent")
                capability = payload.get("capability")
                inputs = payload.get("inputs", {})
                if agent and capability:
                    try:
                        result = await router.dispatch(agent, capability, inputs)
                        await workflow_engine.mark_done(workflow_id, result)
                        await query.edit_message_text(f"Done: {result}")
                    except Exception as exc:
                        await workflow_engine.mark_failed(workflow_id, str(exc))
                        await query.edit_message_text(f"Failed: {exc}")
            else:
                await workflow_engine.mark_failed(workflow_id, "Cancelled by user")
                await query.edit_message_text("Cancelled.")
        finally:
            if chat_id is not None:
                await clear_pending(redis, chat_id)

    return callback_handler


async def _handle_cycle_callback(
    update: Update,
    query: Any,
    parts: list[str],
    router: Router,
) -> None:
    """Handle ``cycle:<cycle_load_id>:<label>`` Telegram callbacks.

    Dispatches to ``household_ops.confirm_cycle_load`` and rewrites the
    message to reflect the final state so the buttons disappear.
    """
    if len(parts) < 3:
        await query.edit_message_text("Cycle confirmation expired or invalid.")
        return
    try:
        cycle_load_id = int(parts[1])
    except ValueError:
        await query.edit_message_text("Cycle confirmation has a bad id.")
        return
    label = parts[2]
    chat_id = _chat_id(update)
    if label == "_skip":
        await query.edit_message_text("👌 Skipped.")
        return
    try:
        result = await router.dispatch(
            "household_ops",
            "confirm_cycle_load",
            {"cycle_load_id": cycle_load_id, "label": label, "chat_id": chat_id},
        )
    except Exception as exc:
        logger.warning("cycle_confirm_dispatch_failed", error=str(exc))
        await query.edit_message_text(f"Couldn't save: {exc}")
        return
    inner = result.get("result") if isinstance(result, dict) else None
    if isinstance(inner, dict) and inner.get("ok"):
        await query.edit_message_text(f"✅ Saved: {label}")
    else:
        err = inner.get("error", "unknown error") if isinstance(inner, dict) else "unknown error"
        await query.edit_message_text(f"Couldn't save: {err}")


async def _handle_clean_callback(
    update: Update,
    query: Any,
    parts: list[str],
    router: Router,
) -> None:
    """Handle ``clean:<cleaning_run_id>:<status>`` Telegram callbacks.

    Dispatches to ``household_ops.confirm_cleaning_run`` and rewrites the
    message to reflect the final state so the buttons disappear.
    """
    if len(parts) < 3:
        await query.edit_message_text("Cleaning confirmation expired or invalid.")
        return
    try:
        cleaning_run_id = int(parts[1])
    except ValueError:
        await query.edit_message_text("Cleaning confirmation has a bad id.")
        return
    status = parts[2]
    chat_id = _chat_id(update)
    if status == "_skip":
        await query.edit_message_text("👌 Skipped.")
        return
    try:
        result = await router.dispatch(
            "household_ops",
            "confirm_cleaning_run",
            {"cleaning_run_id": cleaning_run_id, "status": status, "chat_id": chat_id},
        )
    except Exception as exc:
        logger.warning("cleaning_confirm_dispatch_failed", error=str(exc))
        await query.edit_message_text(f"Couldn't save: {exc}")
        return
    inner = result.get("result") if isinstance(result, dict) else None
    if isinstance(inner, dict) and inner.get("ok"):
        await query.edit_message_text(f"✅ Saved: {status}")
    else:
        err = inner.get("error", "unknown error") if isinstance(inner, dict) else "unknown error"
        await query.edit_message_text(f"Couldn't save: {err}")


async def _handle_pending_callback(
    update: Update,
    query: Any,
    action: str,
    router: Router,
    redis: Redis,
) -> None:
    chat_id = _chat_id(update)
    if chat_id is None:
        await query.edit_message_text("No chat context for pending action.")
        return
    pending = await get_pending(redis, chat_id)
    if pending is None:
        await query.edit_message_text("No pending action found or it expired.")
        return
    if action == "cancel":
        await clear_pending(redis, chat_id)
        await query.edit_message_text("Cancelled.")
        return

    try:
        result = await router.execute_pending(pending)
        reply_text = result.get("reply", "Done.")
    except Exception as exc:
        logger.warning("pending_callback_execute_failed", error=str(exc))
        reply_text = f"Failed: {exc}"
    finally:
        await clear_pending(redis, chat_id)
    await query.edit_message_text(reply_text)


async def _send_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, redis: Redis
) -> None:
    if update.message is None:
        return
    reply_text = result.get("reply", "Done.")
    confirm = result.get("confirm")
    if confirm:
        chat_id = _chat_id(update)
        if chat_id is not None:
            await set_pending(redis, chat_id, {**confirm, "prompt_text": reply_text})
        workflow_id = confirm.get("workflow_id") or "pending"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{workflow_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{workflow_id}"),
                ]
            ]
        )
        await update.message.reply_text(reply_text, reply_markup=keyboard)
    else:
        await update.message.reply_text(reply_text)


async def build_telegram_app(
    token: str,
    allowed_ids: set[int],
    router: Router,
    workflow_engine: WorkflowEngine,
    policy_engine: PolicyEngine,
    scheduler: Scheduler,
    admin_base_url: str,
    redis: Redis,
    knowledge_graph: Any | None = None,
    proposal_store: Any | None = None,
) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["registry"] = router._registry
    app.bot_data["redis"] = redis
    app.bot_data["knowledge_graph"] = knowledge_graph
    app.bot_data["proposal_store"] = proposal_store

    app.add_handler(CommandHandler("start", _make_start(allowed_ids)))
    app.add_handler(CommandHandler("help", _make_help(allowed_ids)))
    app.add_handler(CommandHandler("status", _make_status(allowed_ids)))
    app.add_handler(
        CommandHandler(
            "ask",
            _make_ask(allowed_ids, router, redis, knowledge_graph, proposal_store),
        )
    )
    app.add_handler(CommandHandler("quiet", _make_quiet(allowed_ids, policy_engine)))
    app.add_handler(CommandHandler("mute", _make_mute(allowed_ids, policy_engine)))
    app.add_handler(CommandHandler("unmute", _make_unmute(allowed_ids, policy_engine)))
    app.add_handler(CommandHandler("cancel", _make_cancel(allowed_ids, redis)))
    app.add_handler(CommandHandler("jobs", _make_jobs(allowed_ids, scheduler, admin_base_url)))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            _make_text(allowed_ids, router, redis, knowledge_graph, proposal_store),
        )
    )
    app.add_handler(
        MessageHandler(
            filters.VOICE,
            _make_voice(allowed_ids, router, redis, knowledge_graph, proposal_store),
        )
    )
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            _make_photo(allowed_ids, router, redis, knowledge_graph, proposal_store),
        )
    )
    app.add_handler(
        CallbackQueryHandler(_make_callback(allowed_ids, workflow_engine, router, redis))
    )

    return app


def _to_inline_keyboard(keyboard: Any) -> InlineKeyboardMarkup | None:
    """Convert the list-of-lists of ``{text, callback}`` dicts produced by
    reactive triggers / cycle inference into a telegram ``InlineKeyboardMarkup``.

    Returns ``None`` for empty/invalid input so callers can pass it straight
    through to ``send_message(reply_markup=...)``.
    """
    if not keyboard:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for row in keyboard:
        if not isinstance(row, list):
            continue
        buttons: list[InlineKeyboardButton] = []
        for btn in row:
            if not isinstance(btn, dict):
                continue
            text = str(btn.get("text") or "").strip()
            callback = str(btn.get("callback") or "").strip()
            if not text or not callback:
                continue
            buttons.append(InlineKeyboardButton(text=text, callback_data=callback))
        if buttons:
            rows.append(buttons)
    return InlineKeyboardMarkup(rows) if rows else None


async def send(application: Application, chat_id: int, text: str, reply_markup=None) -> None:
    converted = _to_inline_keyboard(reply_markup) if not isinstance(
        reply_markup, InlineKeyboardMarkup
    ) else reply_markup
    await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=converted)
