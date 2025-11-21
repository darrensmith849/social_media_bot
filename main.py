#main.py

"""
Universal Agency Bot (Railway-ready Python service)

This file is a self-contained Python service you can deploy to Railway.
It includes:
  • FastAPI app (health, dry-run preview, manual publish)
  • APScheduler jobs (daily rotation) 
  • Read-only DB ingest (Generic 'clients' table with JSON attributes)
  • Template engine (Jinja2) implementing the 4-1-1 Rule
  • Pluggable publishers (Console + X/Twitter v2 skeleton)
  • Local SQLite state (works on Railway Volume)

-----------------
ENV VARS (Railway project variables)
-----------------
DATABASE_URL=mysql+pymysql://readonly:***@host:3306/dbname
BOT_CLIENTS_SQL=SELECT * FROM bot_clients_v;   # view returning id, name, industry, attributes (json)
TIMEZONE=Africa/Johannesburg
DRY_RUN=true
DAILY_SLOTS="09:00,13:00,17:30"
PER_CLIENT_MONTHLY_CAP=12
ENABLE_X=false
X_BEARER_TOKEN=
"""
from __future__ import annotations
import os
import logging
import hashlib
import random
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta 
from zoneinfo import ZoneInfo
from typing import List, Optional, Dict, Any, Tuple, Callable

import yaml
import requests
from jinja2 import Environment, BaseLoader, StrictUndefined

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from functools import lru_cache

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, Row, make_url

# ------------------
# Logging setup
# ------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("agency-bot")

# ------------------
# Config helpers
# ------------------
TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Johannesburg"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

DATABASE_URL = os.getenv("DATABASE_URL")
STATE_DB_URL = os.getenv("BOT_STATE_DB_URL", "sqlite:////data/bot.db")
CLIENTS_SQL = os.getenv("BOT_CLIENTS_SQL", "SELECT * FROM bot_clients_v;")

DAILY_SLOTS = [s.strip().strip('"').strip("'") for s in os.getenv("DAILY_SLOTS", "09:00,13:00,17:30").split(",") if s.strip()]
POSTS_PER_SLOT = int(os.getenv("POSTS_PER_SLOT", "1"))
PER_CLIENT_MONTHLY_CAP = int(os.getenv("PER_CLIENT_MONTHLY_CAP", "12"))
COOLDOWN_DAYS = int(os.getenv("COOLDOWN_DAYS", "3"))

# Platform toggles
ENABLE_X = os.getenv("ENABLE_X", "false").lower() == "true"
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")

# Telegram + AI approval settings
TELEGRAM_APPROVAL_ENABLED = os.getenv("TELEGRAM_APPROVAL_ENABLED", "false").lower() == "true"
TELEGRAM_PREVIEW_ON_STARTUP = os.getenv("TELEGRAM_PREVIEW_ON_STARTUP", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

FALLBACK_IMAGE_URL = os.getenv("FALLBACK_IMAGE_URL")

@dataclass(frozen=True)
class Settings:
    dry_run: bool
    enable_x: bool
    x_bearer_token: Optional[str]

SET = Settings(dry_run=DRY_RUN, enable_x=ENABLE_X, x_bearer_token=X_BEARER_TOKEN)

# ------------------
# State DB
# ------------------
def _create_state_engine(url: str) -> Engine:
    if url.startswith("sqlite:"):
        return create_engine(url, future=True, connect_args={"check_same_thread": False})
    return create_engine(url, future=True, pool_pre_ping=True)

STATE_ENGINE: Engine = _create_state_engine(STATE_DB_URL)

STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS published_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  template_key TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  external_id TEXT,
  posted_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pp_client_month ON published_posts (client_id, posted_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pp_client_platform_text ON published_posts (client_id, platform, text_hash);

CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""

# ------------------
# Templates (The 4-1-1 Implementation)
# ------------------
def _templates_path_from_state(url: str) -> str:
    try:
        if url.startswith("sqlite:"):
            u = make_url(url)
            base = os.path.dirname(u.database) if (u and u.database and u.database != ":memory:") else "/tmp"
        else:
            base = "/tmp"
    except Exception:
        base = "/tmp"
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "templates.yaml")

TEMPLATES_PATH = _templates_path_from_state(STATE_DB_URL)

DEFAULT_TEMPLATES_YAML = """
# The 4-1-1 Rule Categories:
# 4 x Educational/Entertaining (Value)
# 1 x Soft Sell (Trust)
# 1 x Hard Sell (Conversion)

post_templates:
  # --- EDUCATIONAL / VALUE (Indices 0, 1, 2, 3) ---
  - key: edu_tip
    category: educational
    platforms: [x, facebook, linkedin]
    text: "💡 Tip from {{ name }}: {{ attributes.tips|random if attributes.tips else 'Did you know? Consistency is key to success.' }} #{{ industry|replace(' ','') }} #{{ city|replace(' ','') }}"

  - key: edu_mythbuster
    category: educational
    platforms: [x, facebook, linkedin]
    text: "Common myth about {{ industry }}: {{ attributes.myths|random if attributes.myths else 'Most people think it is expensive, but it saves you money in the long run.' }} 🚫 Truth: {{ name }} makes it easy."

  - key: edu_question
    category: educational
    platforms: [x, facebook, linkedin]
    text: "Question for our {{ city }} friends: What is your biggest challenge with {{ industry }} right now? 👇 let us know below!"

  - key: edu_didyouknow
    category: educational
    platforms: [x, facebook, linkedin]
    text: "Did you know? {{ attributes.facts|random if attributes.facts else 'We have been serving ' + city + ' for years.' }} 🌍"

  # --- SOFT SELL (Index 4) ---
  - key: soft_team
    category: soft_sell
    platforms: [x, facebook, linkedin]
    text: "Meet the team behind {{ name }} in {{ city }}! We are passionate about {{ industry }} and helping our community. 👋 {{ attributes.website }}"

  - key: soft_behind_scenes
    category: soft_sell
    platforms: [x, facebook, linkedin]
    text: "Behind the scenes at {{ name }}... We are busy making things happen for our clients today! 🛠️"

  # --- HARD SELL (Index 5) ---
  - key: hard_cta
    category: hard_sell
    platforms: [x, facebook, linkedin]
    text: "Ready to get started? 🚀 Join {{ name }} today. {{ attributes.offer_text or 'Contact us for a quote.' }} 👉 {{ attributes.admissions_url or attributes.website }}"

  - key: hard_urgency
    category: hard_sell
    platforms: [x, facebook, linkedin]
    text: "Don't wait! Slots are filling up at {{ name }}. Secure your spot now: {{ attributes.admissions_url or attributes.website }}"
"""

# ------------------
# Data model
# ------------------
@dataclass
class Client:
    id: str
    name: str
    industry: str
    city: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    # attributes usually contains: website, phone, email, negative_constraints, tone, tips (list), myths (list)
    
    @property
    def media_approved(self) -> bool:
        return self.attributes.get("media_approved", True)

    @property
    def opt_out(self) -> bool:
        return self.attributes.get("opt_out", False)

# ------------------
# Util functions
# ------------------
def ensure_bootstrap() -> None:
    with STATE_ENGINE.begin() as conn:
        for stmt in [s.strip() for s in STATE_SCHEMA_SQL.split(";") if s.strip()]:
            conn.exec_driver_sql(stmt)
    if not os.path.exists(TEMPLATES_PATH):
        os.makedirs(os.path.dirname(TEMPLATES_PATH), exist_ok=True)
        with open(TEMPLATES_PATH, "w", encoding="utf-8") as f:
            f.write(DEFAULT_TEMPLATES_YAML)

def load_templates() -> Dict[str, Any]:
    with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    tpl_map = {}
    for item in data.get("post_templates", []):
        tpl_map[item["key"]] = item
    return tpl_map

def month_bounds(dt: datetime) -> Tuple[datetime, datetime]:
    start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end

def text_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def already_recorded(client_id: str, platform: str, text_body: str) -> bool:
    th = text_hash(text_body)
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM published_posts WHERE client_id=:cid AND platform=:pf AND text_hash=:th LIMIT 1"),
            {"cid": client_id, "pf": platform, "th": th},
        ).fetchone()
    return bool(row)

# ------------------
# DB ingest
# ------------------
MAIN_ENGINE: Optional[Engine] = None

def _sample_clients_for_dry() -> List[Client]:
    return [
        Client(
            id="dry-1",
            name="Joe's Gym",
            industry="Fitness",
            city="Cape Town",
            attributes={
                "website": "https://joesgym.co.za",
                "tips": ["Drink water!", "Never skip leg day."],
                "negative_constraints": "Do not mention steroids. Do not use slang.",
                "tone": "High Energy",
                "offer_text": "Get 50% off your first month!"
            }
        ),
        Client(
            id="dry-2",
            name="Smile Dental",
            industry="Healthcare",
            city="Sandton",
            attributes={
                "website": "https://smiledental.co.za",
                "tips": ["Floss daily.", "Brush twice a day."],
                "negative_constraints": "No blood or scary needles.",
                "tone": "Professional and Gentle"
            }
        )
    ]

def get_main_engine() -> Engine:
    global MAIN_ENGINE
    if MAIN_ENGINE is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        MAIN_ENGINE = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
    return MAIN_ENGINE

def row_to_client(row: Row) -> Client:
    """
    Converts DB row to Client. 
    If 'attributes' column is missing, it packs unknown columns into the dict.
    """
    row_dict = dict(row)
    c_id = str(row_dict.pop("id"))
    c_name = row_dict.pop("name")
    c_city = row_dict.pop("city", "South Africa")
    c_industry = row_dict.pop("industry", "General")
    
    # Extract attributes (handle generic JSON column or pack loose columns)
    attrs = {}
    if "attributes" in row_dict and row_dict["attributes"]:
        if isinstance(row_dict["attributes"], str):
            try:
                attrs = json.loads(row_dict["attributes"])
            except:
                pass
        elif isinstance(row_dict["attributes"], dict):
            attrs = row_dict["attributes"]
    
    # Merge remaining columns into attributes if not already present
    for k, v in row_dict.items():
        if k != "attributes" and k not in attrs:
            attrs[k] = v
            
    return Client(id=c_id, name=c_name, industry=c_industry, city=c_city, attributes=attrs)

def fetch_clients() -> List[Client]:
    if not DATABASE_URL:
        return _sample_clients_for_dry() if DRY_RUN else []
    
    try:
        eng = get_main_engine()
        with eng.begin() as conn:
            rows = conn.execute(text(CLIENTS_SQL)).mappings().all()
            return [row_to_client(r) for r in rows]
    except Exception as e:
        if DRY_RUN:
            logger.warning("Fetch failed (%s); using sample clients.", e)
            return _sample_clients_for_dry()
        logger.exception("Failed to fetch clients: %s", e)
        return []

# ------------------
# 4-1-1 Template Selection
# ------------------
@lru_cache(maxsize=1)
def build_env() -> Environment:
    env = Environment(loader=BaseLoader(), autoescape=False, undefined=StrictUndefined)
    env.filters["random"] = lambda seq: random.choice(seq) if seq else ""
    return env

def select_template(templates: Dict[str, Any], client: Client, monthly_count: int) -> Dict[str, Any]:
    """
    Implements the 4-1-1 Rule based on monthly post count.
    Cycle of 6 posts:
    0, 1, 2, 3 -> Educational
    4 -> Soft Sell
    5 -> Hard Sell
    """
    cycle_index = monthly_count % 6
    
    target_category = "educational"
    if cycle_index == 4:
        target_category = "soft_sell"
    elif cycle_index == 5:
        target_category = "hard_sell"
        
    candidates = [t for t in templates.values() if t.get("category") == target_category]
    
    # Fallback if category missing
    if not candidates:
        candidates = list(templates.values())

    # Deterministic choice based on day/client
    seed = int(datetime.now(TZ).strftime("%Y%m%d")) ^ hash(client.id)
    rng = random.Random(seed)
    return rng.choice(candidates)

def render_text(tpl_text: str, client: Client) -> str:
    env = build_env()
    ctx = {
        "name": client.name,
        "city": client.city,
        "industry": client.industry,
        "attributes": client.attributes
    }
    template = env.from_string(tpl_text)
    return template.render(**ctx).strip()

# ------------------
# Publishers
# ------------------
class PublishResult(Dict[str, Any]): pass

class Publisher:
    platform: str = "base"
    def publish(self, text: str, media_url: Optional[str] = None) -> PublishResult:
        raise NotImplementedError

class ConsolePublisher(Publisher):
    platform = "console"
    def publish(self, text: str, media_url: Optional[str] = None) -> PublishResult:
        prefix = "[DRY]" if DRY_RUN else "[LIVE]"
        logger.info("%s %s | %s", prefix, self.platform, text)
        return PublishResult({"platform": self.platform, "id": None, "text": text})

class XPublisher(Publisher):
    platform = "x"
    def __init__(self, bearer: Optional[str]):
        self.bearer = bearer
    def publish(self, text: str, media_url: Optional[str] = None) -> PublishResult:
        MAX = 280
        if len(text) > MAX: text = text[: MAX - 1] + "…"
        if DRY_RUN or not self.bearer:
            logger.info("[DRY] x | %s", text)
            return PublishResult({"platform": self.platform, "id": None, "text": text})
        # Skeleton real publish
        return PublishResult({"platform": self.platform, "id": "12345", "text": text})

def build_publishers() -> list[Publisher]:
    pubs = [ConsolePublisher()]
    if ENABLE_X and SET.x_bearer_token:
        pubs.append(XPublisher(SET.x_bearer_token))
    return pubs

# ------------------
# Core Logic
# ------------------
def already_posted_recently(client_id: str) -> bool:
    cutoff = datetime.now(TZ) - timedelta(days=COOLDOWN_DAYS)
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(text("SELECT 1 FROM published_posts WHERE client_id=:cid AND posted_at>=:cutoff LIMIT 1"), {"cid": client_id, "cutoff": cutoff}).fetchone()
        return bool(row)

def monthly_count(client_id: str, when: datetime) -> int:
    start, end = month_bounds(when)
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(text("SELECT COUNT(*) FROM published_posts WHERE client_id=:cid AND posted_at>=:start AND posted_at<:end"), {"cid": client_id, "start": start, "end": end}).fetchone()
        return int(row[0]) if row else 0

def choose_client_for_slot(clients: List[Client]) -> Optional[Client]:
    now = datetime.now(TZ)
    # Eligibility check
    pool = [c for c in clients if not c.opt_out and c.media_approved and not already_posted_recently(c.id)]
    if not pool: return None
    
    # Cap check
    counts = {c.id: monthly_count(c.id, now) for c in pool}
    pool = [c for c in pool if counts[c.id] < PER_CLIENT_MONTHLY_CAP]
    if not pool: return None
    
    # Prioritize lowest count
    min_c = min(counts[c.id] for c in pool)
    shortlist = [c for c in pool if counts[c.id] == min_c]
    
    rng = random.Random(now.strftime("%Y%m%d"))
    rng.shuffle(shortlist)
    return shortlist[0]

def record_published(client_id: str, platform: str, template_key: str, text_body: str, external_id: Optional[str]) -> None:
    with STATE_ENGINE.begin() as conn:
        conn.execute(text(
            "INSERT INTO published_posts(client_id, platform, template_key, text_hash, external_id, posted_at) VALUES(:cid,:pf,:tk,:th,:eid,:ts)"
        ), {
            "cid": client_id, "pf": platform, "tk": template_key, "th": text_hash(text_body), "eid": external_id, "ts": datetime.now(TZ),
        })

def publish_text_for_client(c: Client, text_body: str, media_url: Optional[str], template_key: str, platforms: Optional[List[str]] = None, record_state: bool = True) -> List[PublishResult]:
    results = []
    for pub in build_publishers():
        if isinstance(pub, ConsolePublisher) or not platforms or pub.platform in platforms:
            try:
                if record_state and already_recorded(c.id, pub.platform, text_body):
                    continue
                res = pub.publish(text_body, media_url)
                results.append(res)
                if record_state:
                    record_published(c.id, pub.platform, template_key, text_body, res.get("id"))
            except Exception as e:
                logger.exception("Failed to publish to %s", pub.platform)
    return results

def publish_once(c: Client, record_state: bool = True) -> List[PublishResult]:
    templates = load_templates()
    current_count = monthly_count(c.id, datetime.now(TZ))
    tpl = select_template(templates, c, current_count)
    
    text_body = render_text(tpl["text"], c)
    media_url = c.attributes.get("hero_image_url") or c.attributes.get("logo_url") or FALLBACK_IMAGE_URL
    platforms = tpl.get("platforms", [])
    
    return publish_text_for_client(c, text_body, media_url, tpl["key"], platforms, record_state)

# ------------------
# Schedules
# ------------------
SCHED = BackgroundScheduler(timezone=str(TZ))

def run_rotation_post(record_state: bool = True):
    clients = fetch_clients()
    choice = choose_client_for_slot(clients)
    if not choice:
        logger.info("No eligible client for this slot.")
        return

    try:
        from telegram_approval import handle_scheduled_post
        handle_scheduled_post(choice, record_state=record_state)
    except ImportError:
        publish_once(choice, record_state=record_state)

def schedule_today_slots():
    now = datetime.now(TZ)
    for slot_str in DAILY_SLOTS:
        try:
            hh, mm = [int(x) for x in slot_str.split(":", 1)]
        except: continue
        slot_dt = now.replace(hour=hh, minute=mm, second=0)
        if slot_dt <= now: slot_dt += timedelta(days=1)
        for i in range(max(1, POSTS_PER_SLOT)):
            run_at = slot_dt + timedelta(minutes=random.randint(0, 25) + i * 5)
            SCHED.add_job(run_rotation_post, DateTrigger(run_date=run_at))
            logger.info("Scheduled post at %s", run_at)

# ------------------
# FastAPI
# ------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_bootstrap()
    schedule_today_slots()
    SCHED.add_job(schedule_today_slots, "cron", hour=0, minute=5)
    SCHED.start()
    if DRY_RUN or TELEGRAM_PREVIEW_ON_STARTUP:
        try:
            run_rotation_post(record_state=False)
        except: pass
    yield
    SCHED.shutdown(wait=False)

app = FastAPI(title="Universal Agency Bot", lifespan=lifespan)

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(TZ).isoformat(), "dry_run": DRY_RUN}

@app.get("/dry-run")
def dry_run(count: int = Query(3)):
    clients = fetch_clients()
    random.shuffle(clients)
    templates = load_templates()
    items = []
    for c in clients[:count]:
        # Mock count to see variety
        m_count = random.randint(0, 10)
        tpl = select_template(templates, c, m_count)
        items.append({
            "client": c.name,
            "industry": c.industry,
            "template": tpl["key"],
            "category": tpl.get("category"),
            "text": render_text(tpl["text"], c),
        })
    return {"posts": items}

@app.post("/telegram/webhook")
async def telegram_webhook(update: Dict[str, Any]):
    try:
        from telegram_approval import handle_telegram_update
        handle_telegram_update(update)
    except Exception as e:
        logger.exception("Webhook failed: %s", e)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
