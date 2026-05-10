from __future__ import annotations

from home_agents_sdk.telemetry import get_logger
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .router import Router
from .voice import transcribe
from .workflow import WorkflowEngine

logger = get_logger("telegram_bot")


def _is_allowed(user_id: int, allowed_ids: set[int]) -> bool:
    return user_id in allowed_ids


def _make_start(allowed_ids: set[int]):
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        await update.message.reply_text(
            "👋 Hello! I'm your Home Intelligence assistant."
            " Send me a message or /ask to get started."
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
            "Or just send a message directly."
        )
        await update.message.reply_text(text)

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
        await update.message.reply_text(text)

    return status_cmd


def _make_ask(allowed_ids: set[int], router: Router):
    async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        text = " ".join(context.args or [])
        if not text:
            await update.message.reply_text("Usage: /ask <your request>")
            return
        user_id = str(update.effective_user.id)
        result = await router.handle(text, user_id)
        await _send_result(update, context, result)

    return ask_cmd


def _make_text(allowed_ids: set[int], router: Router):
    async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        text = update.message.text or ""
        user_id = str(update.effective_user.id)
        result = await router.handle(text, user_id)
        await _send_result(update, context, result)

    return text_handler


def _make_voice(allowed_ids: set[int], router: Router):
    async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        try:
            text = await transcribe(update.message.voice, context.bot)
        except Exception as exc:
            logger.warning("voice_transcribe_failed", error=str(exc))
            await update.message.reply_text("Sorry, I couldn't transcribe that voice message.")
            return
        user_id = str(update.effective_user.id)
        result = await router.handle(text, user_id)
        await _send_result(update, context, result)

    return voice_handler


def _make_photo(allowed_ids: set[int], router: Router):
    async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or not _is_allowed(update.effective_user.id, allowed_ids):
            return
        caption = update.message.caption or ""
        text = f"Look at this image: {caption}"
        user_id = str(update.effective_user.id)
        result = await router.handle(text, user_id)
        await _send_result(update, context, result)

    return photo_handler


def _make_callback(allowed_ids: set[int], workflow_engine: WorkflowEngine, router: Router):
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
        user_choice = {"action": action}
        payload = await workflow_engine.resume(workflow_id, user_choice)
        if action == "confirm":
            agent = payload.get("agent")
            capability = payload.get("capability")
            inputs = payload.get("inputs", {})
            if agent and capability:
                try:
                    result = await router._registry.dispatch(agent, capability, inputs)
                    await workflow_engine.mark_done(workflow_id, result)
                    await query.edit_message_text(f"Done: {result}")
                except Exception as exc:
                    await workflow_engine.mark_failed(workflow_id, str(exc))
                    await query.edit_message_text(f"Failed: {exc}")
        else:
            await workflow_engine.mark_failed(workflow_id, "Cancelled by user")
            await query.edit_message_text("Cancelled.")

    return callback_handler


async def _send_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict
) -> None:
    reply_text = result.get("reply", "Done.")
    confirm = result.get("confirm")
    if confirm:
        workflow_id = confirm.get("workflow_id") or "pending"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{workflow_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{workflow_id}"),
            ]
        ])
        await update.message.reply_text(reply_text, reply_markup=keyboard)
    else:
        await update.message.reply_text(reply_text)


async def build_telegram_app(
    token: str,
    allowed_ids: set[int],
    router: Router,
    workflow_engine: WorkflowEngine,
) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["registry"] = router._registry

    app.add_handler(CommandHandler("start", _make_start(allowed_ids)))
    app.add_handler(CommandHandler("help", _make_help(allowed_ids)))
    app.add_handler(CommandHandler("status", _make_status(allowed_ids)))
    app.add_handler(CommandHandler("ask", _make_ask(allowed_ids, router)))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, _make_text(allowed_ids, router)
    ))
    app.add_handler(MessageHandler(filters.VOICE, _make_voice(allowed_ids, router)))
    app.add_handler(MessageHandler(filters.PHOTO, _make_photo(allowed_ids, router)))
    app.add_handler(CallbackQueryHandler(_make_callback(allowed_ids, workflow_engine, router)))

    return app


async def send(application: Application, chat_id: int, text: str, reply_markup=None) -> None:
    await application.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
