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

def _build_preview_text(c: Client, text_body: str) -> str:
    return f"🏢 *{c.name}* ({c.industry})\n📍 {c.city}\n\n{text_body}"

def _approval_keyboard():
    return {"inline_keyboard": [[
        {"text": "Approve ✅", "callback_data": "approve"},
        {"text": "Regenerate 🔁", "callback_data": "regen"},
        {"text": "Customise ✏️", "callback_data": "custom"},
    ]]} 

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

def _send_preview_message(c: Client, text_body: str, media_url: Optional[str], template_key: str, platforms: List[str], record_state: bool):
    if not TELEGRAM_CHAT_ID: return
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": _build_preview_text(c, text_body),
        "reply_markup": _approval_keyboard(),
        "parse_mode": "Markdown"
    }
    data = _post_telegram("sendMessage", payload)
    if not data or not data.get("ok"): return

    msg = data["result"]
    _set_state({
        "status": "pending",
        "client_id": c.id,
        "text_body": text_body,
        "media_url": media_url,
        "template_key": template_key,
        "platforms": platforms,
        "record_state": record_state,
        "chat_id": msg["chat"]["id"],
        "message_id": msg["message_id"],
    })

def handle_scheduled_post(c: Client, record_state: bool = True):
    if not main.TELEGRAM_APPROVAL_ENABLED:
        main.publish_once(c, record_state=record_state)
        return

    # Generate initial draft using 4-1-1 templates
    templates = main.load_templates()
    count = main.monthly_count(c.id, datetime.now(TZ))
    tpl = main.select_template(templates, c, count)
    text_body = main.render_text(tpl["text"], c)
    media_url = c.attributes.get("hero_image_url") or main.FALLBACK_IMAGE_URL
    
    _send_preview_message(c, text_body, media_url, tpl["key"], tpl.get("platforms", []), record_state)

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
        data = cb.get("data")
        state = _get_state()
        
        _post_telegram("answerCallbackQuery", {"callback_query_id": cb["id"]})
        
        if not state or state.get("status") != "pending": return

        c = _find_client(state["client_id"])
        
        if data == "approve":
            main.publish_text_for_client(c, state["text_body"], state["media_url"], state["template_key"], state["platforms"], state["record_state"])
            _post_telegram("editMessageText", {
                "chat_id": state["chat_id"], "message_id": state["message_id"],
                "text": f"✅ Published for *{c.name}*", "parse_mode": "Markdown"
            })
            _clear_state()
            
        elif data == "regen":
            new_state = _generate_ai_post(c, state)
            _set_state(new_state)
            _edit_message(new_state)
            
        elif data == "custom":
            state["status"] = "awaiting_custom"
            _set_state(state)
            _post_telegram("sendMessage", {"chat_id": state["chat_id"], "text": "Reply with your instructions:"})

    elif "message" in update:
        msg = update["message"]
        text = msg.get("text")
        state = _get_state()
        
        if state and state.get("status") == "awaiting_custom" and text:
            c = _find_client(state["client_id"])
            new_state = _generate_ai_post(c, state, custom_prompt=text)
            new_state["status"] = "pending"
            _set_state(new_state)
            _edit_message(new_state)
            # Confirm receipt
            _post_telegram("sendMessage", {"chat_id": msg["chat"]["id"], "text": "Updated draft 👆"})
