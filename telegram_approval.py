#telegram_approval.py
from __future__ import annotations
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
import requests
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

import main

logger = main.logger
TZ = main.TZ
Client = main.Client
kget = main.kget
kset = main.kset
publish_text_for_client = main.publish_text_for_client
create_post_candidate = main.create_post_candidate
get_post_candidate = main.get_post_candidate
update_post_candidate_status = main.update_post_candidate_status
update_post_candidate_metadata = main.update_post_candidate_metadata


TELEGRAM_BOT_TOKEN = main.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = main.TELEGRAM_CHAT_ID
OPENAI_API_KEY = main.OPENAI_API_KEY
OPENAI_MODEL = main.OPENAI_MODEL
STATE_KEY = "telegram_approval_state"

_client = OpenAI(api_key=OPENAI_API_KEY) if (OPENAI_API_KEY and OpenAI) else None

def _telegram_base_url():
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None

def _post_telegram(method: str, payload: Dict[str, Any]):
    base = _telegram_base_url()
    if not base: return None
    try:
        resp = requests.post(f"{base}/{method}", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        logger.error("Telegram error: %s", e)
        return None

def _get_state():
    raw = kget(STATE_KEY)
    return json.loads(raw) if raw else None

def _set_state(state):
    kset(STATE_KEY, json.dumps(state))

def _clear_state():
    kset(STATE_KEY, "")

def _find_client(client_id: str) -> Optional[Client]:
    for c in main.fetch_clients():
        if c.id == client_id: return c
    return None

def _build_preview_text(c: Client, text_body: str, category: Optional[str] = None) -> str:
    header = c.name
    if category:
        header = f"{header} · {category.replace('_', ' ').title()}"
    return f"📋 *{header}*\n\n{text_body}"


def _approval_keyboard(candidate_id: int):
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{candidate_id}"},
            {"text": "❌ Reject",  "callback_data": f"reject:{candidate_id}"},
        ]]
    }


def _generate_ai_post(c: Client, state: Dict[str, Any], custom_prompt: Optional[str] = None) -> Dict[str, Any]:
    """
    Regenerates text using OpenAI, injecting Negative Constraints and Brand Tone.
    """
    base_text = state.get("text_body", "")
    
    if not _client:
        return state # No AI available

    # --- THIS IS THE SAFETY LOGIC ---
    constraints = c.attributes.get("negative_constraints", "None")
    tone = c.attributes.get("tone", "Professional and engaging")
    
    system_msg = (
        f"You are a social media manager for {c.name}, a {c.industry} business in {c.city}. "
        f"TONE: {tone}. "
        f"CONSTRAINT: Do NOT talk about: {constraints}. "
        "Keep posts short (under 280 chars) and punchy. Use British English."
    )
    # --------------------------------

    if custom_prompt:
        user_msg = f"Draft: '{base_text}'. Instruction: {custom_prompt}"
    else:
        user_msg = f"Rewrite this post to be more engaging for {c.industry} clients: '{base_text}'"

    try:
        resp = _client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=200,
            temperature=0.7,
        )
        new_text = resp.choices[0].message.content.strip()
        # Return updated state dict
        new_state = state.copy()
        new_state["text_body"] = new_text
        new_state["source"] = "ai"
        return new_state
    except Exception as e:
        logger.error("AI Gen failed: %s", e)
        return state

def _send_preview_message(
    c: Client,
    candidate_id: int,
    text_body: str,
    media_url: str,
    template_key: str,
    category: Optional[str],
    platforms: List[str],
) -> None:
    """Send the preview to Telegram and attach chat/message ids to the candidate metadata."""
    if not TELEGRAM_CHAT_ID:
        return

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _build_preview_text(c, text_body, category),
        "reply_markup": _approval_keyboard(candidate_id),
        "parse_mode": "Markdown",
    }
    data = _post_telegram("sendMessage", payload)
    if not data or not data.get("ok"):
        return

    msg = data["result"]

    # Persist Telegram message identifiers into candidate metadata
    try:
        update_post_candidate_metadata(
            candidate_id,
            {
                "telegram_chat_id": msg["chat"]["id"],
                "telegram_message_id": msg["message_id"],
            },
        )
    except Exception as e:
        logger.exception("Failed to update candidate %s metadata: %s", candidate_id, e)



def handle_scheduled_post(c: Client, record_state: bool = True):
    """
    Create a post_candidate and either:
    - auto-publish (global toggle or approval_mode='auto_silent'), or
    - send to Telegram for approval.
    """
    # Generate initial draft using 4-1-1 templates
    templates = main.load_templates()
    count = main.monthly_count(c.id, datetime.now(TZ))
    tpl = main.select_template(templates, c, count)

    text_body = main.render_text(tpl["text"], c)
    media_url = c.attributes.get("hero_image_url") or main.FALLBACK_IMAGE_URL
    platforms = tpl.get("platforms", [])
    category = tpl.get("category")
    slot_time = datetime.now(TZ)

    # Create candidate row
    candidate_id = create_post_candidate(
        client_id=c.id,
        template_key=tpl["key"],
        text_body=text_body,
        media_url=media_url,
        platforms=platforms,
        slot_time=slot_time,
        status="PENDING",
        metadata={
            "category": category,
            "record_state": bool(record_state),
        },
    )

    # Per-client approval mode
    approval_mode = (c.attributes.get("approval_mode") or "always").lower()

    # Global toggle OFF or client in auto_silent -> publish immediately
    if not main.TELEGRAM_APPROVAL_ENABLED or approval_mode in ("auto_silent",):
        publish_text_for_client(
            c,
            text_body,
            media_url,
            tpl["key"],
            platforms,
            record_state=record_state,
        )
        try:
            update_post_candidate_status(candidate_id, "APPROVED")
        except Exception as e:
            logger.exception("Failed to update candidate %s after auto publish: %s", candidate_id, e)
        return

    # Otherwise: send to Telegram for approval
    _send_preview_message(
        c,
        candidate_id,
        text_body,
        media_url,
        tpl["key"],
        category,
        platforms,
    )



# --- Callback Handlers ---

def _edit_message(state):
    c = _find_client(state["client_id"])
    _post_telegram("editMessageText", {
        "chat_id": state["chat_id"],
        "message_id": state["message_id"],
        "text": _build_preview_text(c, state["text_body"]),
        "reply_markup": _approval_keyboard(),
        "parse_mode": "Markdown"
    })

def handle_telegram_update(update: Dict[str, Any]):
    if "callback_query" in update:
        cb = update["callback_query"]
        data = (cb.get("data") or "").strip()

        _post_telegram("answerCallbackQuery", {"callback_query_id": cb["id"]})

        # Expect "action:candidate_id"
        try:
            action, id_str = data.split(":", 1)
            candidate_id = int(id_str)
        except Exception:
            return

        candidate = get_post_candidate(candidate_id)
        if not candidate:
            # Candidate gone or expired
            return

        if candidate.get("status") != "PENDING":
            # Already processed (approved/rejected/timeout)
            return

        c = _find_client(candidate["client_id"])
        if not c:
            update_post_candidate_status(candidate_id, "CANCELLED")
            return

        meta = candidate.get("metadata") or {}
        record_state = bool(meta.get("record_state", True))
        platforms = candidate.get("platforms") or []

        chat_id = meta.get("telegram_chat_id") or cb["message"]["chat"]["id"]
        message_id = meta.get("telegram_message_id") or cb["message"]["message_id"]

        if action == "approve":
            publish_text_for_client(
                c,
                candidate["text_body"],
                candidate["media_url"],
                candidate["template_key"],
                platforms,
                record_state=record_state,
            )
            update_post_candidate_status(candidate_id, "APPROVED")

            # Remove buttons and confirm
            _post_telegram(
                "editMessageReplyMarkup",
                {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
            )
            _post_telegram(
                "sendMessage",
                {"chat_id": chat_id, "text": "✅ Approved and scheduled."},
            )

        elif action == "reject":
            update_post_candidate_status(candidate_id, "REJECTED")

            _post_telegram(
                "editMessageReplyMarkup",
                {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
            )
            _post_telegram(
                "sendMessage",
                {"chat_id": chat_id, "text": "❌ Rejected. We'll skip this one."},
            )

    # For now we ignore plain text messages here (no custom/regen flow).
