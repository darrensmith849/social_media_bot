#telegram_approval.py
from __future__ import annotations
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
import requests
from sqlalchemy import text  # NEW: for direct KV access

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

import main

logger = main.logger
TZ = main.TZ
Client = main.Client

# Prefer kget/kset from main; fall back to in-memory KV so imports never break.
if hasattr(main, "kget") and hasattr(main, "kset"):
    kget = main.kget
    kset = main.kset
else:
    _mem_kv: Dict[str, str] = {}

    def kget(key: str) -> Optional[str]:
        return _mem_kv.get(key)

    def kset(key: str, value: Optional[str]) -> None:
        if value:
            _mem_kv[key] = value
        else:
            _mem_kv.pop(key, None)

publish_text_for_client = main.publish_text_for_client

def kget(key: str) -> Optional[str]:
    """
    Read a value from the KV table in the shared state DB.
    Returns None if the state DB is not available or key missing.
    """
    if STATE_ENGINE is None:
        return None
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(text("SELECT v FROM kv WHERE k = :k"), {"k": key}).first()
        return row[0] if row else None

def kset(key: str, value: Optional[str]) -> None:
    """
    Write a value to the KV table.
    If value is None, the key is deleted.
    """
    if STATE_ENGINE is None:
        return
    with STATE_ENGINE.begin() as conn:
        conn.execute(text("DELETE FROM kv WHERE k = :k"), {"k": key})
        if value is not None:
            conn.execute(
                text("INSERT INTO kv (k, v) VALUES (:k, :v)"),
                {"k": key, "v": value},
            )

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
    # --- Inline button callbacks (Approve / Regenerate / Customise) ---
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb.get("data")
        state = _get_state()

        # Acknowledge the button press so Telegram stops the "loading" spinner
        _post_telegram("answerCallbackQuery", {"callback_query_id": cb["id"]})

        # If we don't have a pending draft, ignore the callback
        if not state or state.get("status") != "pending":
            return

        c = _find_client(state["client_id"])
        if not c:
            _post_telegram(
                "sendMessage",
                {
                    "chat_id": state.get("chat_id", TELEGRAM_CHAT_ID),
                    "text": "Client for this draft was not found anymore.",
                },
            )
            _clear_state()
            return

        if data == "approve":
            # Publish to the configured platforms
            main.publish_text_for_client(
                c,
                state["text_body"],
                state["media_url"],
                state["template_key"],
                state["platforms"],
                state["record_state"],
            )
            _post_telegram(
                "editMessageText",
                {
                    "chat_id": state["chat_id"],
                    "message_id": state["message_id"],
                    "text": f"✅ Published for *{c.name}*",
                    "parse_mode": "Markdown",
                },
            )
            _clear_state()

        elif data == "regen":
            # Regenerate using AI, keep it in 'pending' state
            new_state = _generate_ai_post(c, state)
            new_state["status"] = "pending"
            _set_state(new_state)
            _edit_message(new_state)

        elif data == "custom":
            # Next text message from you will be treated as custom instructions
            state["status"] = "awaiting_custom"
            _set_state(state)
            _post_telegram(
                "sendMessage",
                {
                    "chat_id": state["chat_id"],
                    "text": "Send your custom instructions for this post.",
                },
            )

    # --- Normal messages (commands, custom text, etc.) ---
    elif "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text") or ""
        state = _get_state()

        # Normalise first token so we can recognise /next-post, /next_post, /next-post@BotName, etc.
        command = ""
        if text.startswith("/"):
            command = text.split()[0].split("@")[0].lower()

        # 1) /next-post command: start a small flow to ask for client ID
        if command in ("/next-post", "/next_post"):
            _set_state(
                {
                    "status": "awaiting_client_id",
                    "chat_id": chat_id,
                }
            )
            _post_telegram(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Please send the client ID for the next post (for example: six_sigma_south_7626).",
                },
            )
            return

        # 2) After /next-post, treat the next message as the client ID
        if state and state.get("status") == "awaiting_client_id" and text:
            client_id = text.strip()
            c = _find_client(client_id)

            if not c:
                _post_telegram(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": f"Client ID '{client_id}' was not found. Please paste the exact ID from the database.",
                    },
                )
                # Keep status as 'awaiting_client_id' so you can retry
                return

            # Reuse the existing scheduled-post logic, but for this specific client only.
            # Because TELEGRAM_APPROVAL_ENABLED is true, this will send you a preview
            # with Approve / Regenerate / Customise buttons instead of auto-posting.
            handle_scheduled_post(c, record_state=True)
            return

        # 3) Custom instructions for the current draft
        if state and state.get("status") == "awaiting_custom" and text:
            c = _find_client(state["client_id"])
            if not c:
                _post_telegram(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": "Client for this draft was not found anymore.",
                    },
                )
                _clear_state()
                return

            new_state = _generate_ai_post(c, state, custom_prompt=text)
            new_state["status"] = "pending"
            _set_state(new_state)
            _edit_message(new_state)
            # Confirm receipt
            _post_telegram(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Updated draft 👆",
                },
            )
