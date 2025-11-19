from __future__ import annotations

import os
import json
from datetime import datetime
from typing import Optional, Dict, Any, List

import requests

try:
    from openai import OpenAI
except ImportError:  # OpenAI is optional at import time; feature-gated by env.
    OpenAI = None  # type: ignore

import main


logger = main.logger
TZ = main.TZ

School = main.School
kget = main.kget
kset = main.kset
load_templates = main.load_templates
select_template = main.select_template
render_text = main.render_text
pick_media = main.pick_media
publish_text_for_school = main.publish_text_for_school
fetch_schools = main.fetch_schools

TELEGRAM_APPROVAL_ENABLED = main.TELEGRAM_APPROVAL_ENABLED
TELEGRAM_PREVIEW_ON_STARTUP = main.TELEGRAM_PREVIEW_ON_STARTUP
TELEGRAM_BOT_TOKEN = main.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = main.TELEGRAM_CHAT_ID
OPENAI_API_KEY = main.OPENAI_API_KEY
OPENAI_MODEL = main.OPENAI_MODEL

STATE_KEY = "telegram_approval_state"

_client: Optional[OpenAI] = None
if OPENAI_API_KEY and OpenAI is not None:
    _client = OpenAI(api_key=OPENAI_API_KEY)


def _telegram_base_url() -> Optional[str]:
    if not TELEGRAM_BOT_TOKEN:
        return None
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _post_telegram(method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    base = _telegram_base_url()
    if not base:
        logger.warning("Telegram bot token not set; skipping call %s", method)
        return None
    try:
        resp = requests.post(f"{base}/{method}", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.warning("Telegram API error %s: %s", method, data)
        return data
    except Exception as e:
        logger.exception("Telegram API call %s failed: %s", method, e)
        return None


def _get_state() -> Optional[Dict[str, Any]]:
    raw = kget(STATE_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.exception("Failed to decode telegram approval state")
        return None


def _set_state(state: Dict[str, Any]) -> None:
    kset(STATE_KEY, json.dumps(state))


def _clear_state() -> None:
    # Store empty string so kget() returns falsy.
    kset(STATE_KEY, "")


def _find_school(school_id: str) -> Optional[School]:
    for s in fetch_schools():
        if s.id == school_id:
            return s
    return None


def _build_preview_text(s: School, text_body: str) -> str:
    header = f"Next post for {s.name} ({s.city})"
    return f"{header}\n\n{text_body}"


def _approval_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Approve ✅", "callback_data": "approve"},
                {"text": "Regenerate 🔁", "callback_data": "regen"},
                {"text": "Customise ✏️", "callback_data": "custom"},
            ]
        ]
    }


def _send_preview_message(
    s: School,
    text_body: str,
    media_url: Optional[str],
    template_key: str,
    platforms: Optional[List[str]],
    purpose: str,
    record_state: bool,
    source: str,
) -> Optional[Dict[str, Any]]:
    if not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_CHAT_ID not set; cannot send preview message.")
        return None

    payload = {
        "chat_id": int(TELEGRAM_CHAT_ID),
        "text": _build_preview_text(s, text_body),
        "reply_markup": _approval_keyboard(),
    }
    data = _post_telegram("sendMessage", payload)
    if not data or not data.get("ok"):
        return None

    msg = data["result"]
    state: Dict[str, Any] = {
        "status": "pending",
        "school_id": s.id,
        "purpose": purpose,
        "text_body": text_body,
        "media_url": media_url,
        "template_key": template_key,
        "platforms": platforms or [],
        "record_state": record_state,
        "chat_id": msg["chat"]["id"],
        "message_id": msg["message_id"],
        "created_at": datetime.now(TZ).isoformat(),
        "source": source,
    }
    _set_state(state)
    return msg


def _edit_preview_message(state: Dict[str, Any]) -> None:
    chat_id = state.get("chat_id")
    message_id = state.get("message_id")
    school_id = state.get("school_id")
    text_body = state.get("text_body", "")

    if not chat_id or not message_id or not school_id:
        return

    s = _find_school(school_id)
    if not s:
        return

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": _build_preview_text(s, text_body),
        "reply_markup": _approval_keyboard(),
    }
    _post_telegram("editMessageText", payload)


def _generate_ai_post(s: School, state: Dict[str, Any], custom_prompt: Optional[str] = None) -> Dict[str, Any]:
    """
    Use OpenAI (if available) to regenerate or customise the text.
    Falls back to the existing text if AI isn't configured.
    """
    base_text = state.get("text_body", "")
    media_url = state.get("media_url")
    template_key = state.get("template_key", "manual")
    platforms = state.get("platforms", [])

    if not _client:
        logger.warning("OpenAI client not configured; skipping AI generation.")
        return {
            "text_body": base_text,
            "media_url": media_url,
            "template_key": template_key,
            "platforms": platforms,
            "source": "template",
        }

    system_msg = (
        "You are a marketing copywriter for South African private schools. "
        "Write short, engaging, parent-friendly posts with clear value. "
        "Use British spelling."
    )

    user_parts = [
        f"School name: {s.name}",
        f"City: {s.city}",
        f"Province: {s.province}",
        "",
    ]

    if custom_prompt:
        user_parts.append("Write a new social media post based on this instruction:")
        user_parts.append(custom_prompt)
    else:
        user_parts.append("Rewrite this draft post in a fresh way, same intent, more compelling:")
        user_parts.append(base_text)

    user_msg = "\n".join(user_parts)

    try:
        resp = _client.chat.completions.create(
            model=OPENAI_MODEL or "gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=300,
            temperature=0.7,
        )
        new_text = (resp.choices[0].message.content or "").strip()
        if not new_text:
            raise ValueError("Empty AI response")

        return {
            "text_body": new_text,
            "media_url": media_url,
            "template_key": template_key,
            "platforms": platforms,
            "source": "ai_custom" if custom_prompt else "ai_regen",
        }
    except Exception as e:
        logger.exception("OpenAI generation failed; falling back to template text: %s", e)
        return {
            "text_body": base_text,
            "media_url": media_url,
            "template_key": template_key,
            "platforms": platforms,
            "source": "template",
        }


def handle_scheduled_post(s: School, purpose: str = "rotation", record_state: bool = True) -> None:
    """
    Entry point from run_rotation_post():
    - If Telegram approval is disabled → publish immediately (old behaviour).
    - If enabled → send preview to Telegram and wait for user approval.
    """
    if not TELEGRAM_APPROVAL_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram approval disabled or misconfigured; publishing directly.")
        main.publish_once(s, purpose=purpose, record_state=record_state)
        return

    state = _get_state()
    if state and state.get("status") in ("pending", "awaiting_custom_prompt"):
        logger.info("Existing approval pending; skipping new scheduled post.")
        return

    # Build a fresh template-based draft for preview.
    templates = load_templates()
    tpl = select_template(templates, s, purpose)
    text_body = render_text(tpl["text"], s)
    media_url = pick_media(s)
    platforms = tpl.get("platforms", [])

    msg = _send_preview_message(
        s=s,
        text_body=text_body,
        media_url=media_url,
        template_key=tpl["key"],
        platforms=platforms,
        purpose=purpose,
        record_state=record_state,
        source="template",
    )

    if not msg:
        # Telegram failed; fall back to immediate publish so schedule keeps working.
        logger.warning("Failed to send Telegram preview; publishing directly.")
        publish_text_for_school(
            s=s,
            text_body=text_body,
            media_url=media_url,
            template_key=tpl["key"],
            platforms=platforms,
            record_state=record_state,
        )


def _handle_callback_approve(callback: Dict[str, Any]) -> None:
    _post_telegram("answerCallbackQuery", {"callback_query_id": callback.get("id")})

    state = _get_state()
    if not state or state.get("status") not in ("pending", "awaiting_custom_prompt"):
        logger.info("No pending approval state to approve.")
        return

    s = _find_school(state.get("school_id", ""))
    if not s:
        logger.warning("Approval state refers to unknown school: %s", state)
        return

    results = publish_text_for_school(
        s=s,
        text_body=state.get("text_body", ""),
        media_url=state.get("media_url"),
        template_key=state.get("template_key", "manual"),
        platforms=state.get("platforms", []),
        record_state=state.get("record_state", True),
    )

    msg = callback.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")

    if chat_id and message_id:
        ack_text = _build_preview_text(s, state.get("text_body", "")) + "\n\n✅ Approved & published."
        _post_telegram(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": ack_text,
            },
        )

    logger.info("Publish results: %s", results)
    _clear_state()


def _handle_callback_regen(callback: Dict[str, Any]) -> None:
    _post_telegram("answerCallbackQuery", {"callback_query_id": callback.get("id")})

    state = _get_state()
    if not state or state.get("status") not in ("pending", "awaiting_custom_prompt"):
        logger.info("No pending approval state to regenerate.")
        return

    s = _find_school(state.get("school_id", ""))
    if not s:
        logger.warning("Regen state refers to unknown school: %s", state)
        return

    new_bits = _generate_ai_post(s, state, custom_prompt=None)
    state["text_body"] = new_bits["text_body"]
    state["media_url"] = new_bits["media_url"]
    state["template_key"] = new_bits["template_key"]
    state["platforms"] = new_bits["platforms"]
    state["source"] = new_bits["source"]
    state["status"] = "pending"
    _set_state(state)

    _edit_preview_message(state)


def _handle_callback_custom(callback: Dict[str, Any]) -> None:
    _post_telegram("answerCallbackQuery", {"callback_query_id": callback.get("id")})

    state = _get_state()
    if not state:
        logger.info("No approval state to customise.")
        return

    state["status"] = "awaiting_custom_prompt"
    _set_state(state)

    msg = callback.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id:
        _post_telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "What would you like for your next post to be about?",
            },
        )


def _handle_text_message(message: Dict[str, Any]) -> None:
    text = message.get("text") or ""
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return

    state = _get_state()
    if not state or state.get("status") != "awaiting_custom_prompt":
        # Not in a custom prompt flow; ignore.
        return

    if chat_id != state.get("chat_id"):
        # Ignore messages from other chats.
        return

    s = _find_school(state.get("school_id", ""))
    if not s:
        logger.warning("Custom prompt state refers to unknown school: %s", state)
        return

    if not _client:
        _post_telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Customise is unavailable because OpenAI is not configured.",
            },
        )
        state["status"] = "pending"
        _set_state(state)
        return

    new_bits = _generate_ai_post(s, state, custom_prompt=text) 
    state["text_body"] = new_bits["text_body"]
    state["media_url"] = new_bits["media_url"]
    state["template_key"] = new_bits["template_key"]
    state["platforms"] = new_bits["platforms"]
    state["source"] = new_bits["source"]
    state["status"] = "pending"
    _set_state(state)

    # Send a fresh preview message with the customised post.
    _send_preview_message(
        s=s,
        text_body=state["text_body"],
        media_url=state["media_url"],
        template_key=state["template_key"],
        platforms=state["platforms"],
        purpose=state.get("purpose", "rotation"),
        record_state=state.get("record_state", True),
        source=state["source"],
    )


def handle_telegram_update(update: Dict[str, Any]) -> None:
    """
    Entry point from FastAPI webhook:
    - callback_query → buttons
    - message → custom prompt reply
    """
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb.get("data")
        if data == "approve":
            _handle_callback_approve(cb)
        elif data == "regen":
            _handle_callback_regen(cb)
        elif data == "custom":
            _handle_callback_custom(cb)
        else:
            _post_telegram("answerCallbackQuery", {"callback_query_id": cb.get("id")})
        return

    if "message" in update:
        _handle_text_message(update["message"])
