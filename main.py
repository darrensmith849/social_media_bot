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
import uuid
import logging
import hashlib
import random
import json
import html
from dataclasses import dataclass, field
from datetime import datetime, timedelta 
from zoneinfo import ZoneInfo
from typing import List, Optional, Dict, Any, Tuple, Callable


import yaml
import requests
from requests_oauthlib import OAuth1Session
from jinja2 import Environment, BaseLoader, StrictUndefined

from fastapi import FastAPI, Query, Body, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

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
# Global Cache for Dry Run
# ------------------
_DRY_RUN_CACHE = []

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

CREATE TABLE IF NOT EXISTS post_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id TEXT NOT NULL,
  template_key TEXT NOT NULL,
  text_body TEXT NOT NULL,
  media_url TEXT,
  slot_time TIMESTAMP NOT NULL,
  status TEXT NOT NULL,
  platforms TEXT,
  rejection_reason TEXT,
  metadata TEXT,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pc_client_status ON post_candidates (client_id, status);
CREATE INDEX IF NOT EXISTS idx_pc_slot_status ON post_candidates (slot_time, status);

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
    text: |
      {{ content_theme or ("Quick tip from " ~ name) }} 💡

      {% set tip = (attributes.tips|random) if attributes.tips else None %}
      {{ tip or ("Share a practical insight about " ~ industry ~ " that helps your " ~ city ~ " audience.") }}

  - key: edu_mythbuster
    category: educational
    platforms: [x, facebook, linkedin]
    text: |
      {% set myth = (attributes.myths|random) if attributes.myths else None %}
      Myth vs reality in {{ industry }} for {{ city }} 👇

      {% if myth %}
      Myth: {{ myth }}
      Reality: Explain the truth in a friendly, confidence-building way.
      {% else %}
      Bust a common misconception your customers have before they work with you.
      {% endif %}

  - key: edu_pillar_story
    category: educational
    platforms: [x, facebook, linkedin]
    text: |
      {% set pillar = (content_pillars|random) if content_pillars else "Behind the scenes" %}
      {{ pillar }} – from {{ name }} in {{ city }} 👀

      Share a short story or example that brings this pillar to life for your ideal customer.

  - key: edu_faq
    category: educational
    platforms: [x, facebook, linkedin]
    text: |
      {% set faq = (attributes.content_atoms.faqs|random) 
            if attributes.content_atoms and attributes.content_atoms.faqs else None %}
      Common question we get at {{ name }} in {{ city }}:

      {% if faq %}
      {{ faq }}
      {% else %}
      Answer one simple, practical question your {{ city }} clients ask about {{ industry }}.
      {% endif %}

  # --- SOFT SELL (Index 4) ---
  - key: soft_story
    category: soft_sell
    platforms: [x, facebook, linkedin]
    text: |
      Why {{ name }} exists in {{ city }} 💭

      {% if attributes.content_atoms and attributes.content_atoms.story_mission %}
      {{ attributes.content_atoms.story_mission }}
      {% else %}
      Share a short origin story that shows your values and how you help people with {{ industry }}.
      {% endif %}

  - key: soft_social_proof
    category: soft_sell
    platforms: [x, facebook, linkedin]
    text: |
      A quick win from our community ✨

      Share a recent testimonial, review or success story that proves your {{ industry }} work really helps.

  # --- HARD SELL (Index 5) ---
  - key: hard_offer
    category: hard_sell
    platforms: [x, facebook, linkedin]
    text: |
      Ready to take action with {{ name }} in {{ city }}? 💥

      {% if attributes.hard_sell_offer %}
      {{ attributes.hard_sell_offer }}
      {% else %}
      Invite people to book, buy or enquire today – make the next step extremely clear.
      {% endif %}

  - key: hard_ecom_spotlight
    category: hard_sell
    platforms: [x, facebook, instagram]
    text: |
      {% set product = (attributes.product_spotlights|random) if attributes.product_spotlights else None %}
      {% if product %}
      Featured product: {{ product.name }} 🛒

      {{ product.short_benefit }}
      {% if product.url %}Shop now: {{ product.url }}{% endif %}
      {% else %}
      Highlight one hero product or package your {{ industry }} customers love and include a clear call to action.
      {% endif %}
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
    # attributes usually contains:
    #   website, phone, email
    #   tone, negative_constraints, tips (list), myths (list)
    #   content_theme, content_pillars, suggested_posts_per_week
    #   content_atoms, is_ecommerce, ecommerce_platform
    #   product_categories, product_spotlights
    #   cooldown_days, max_posts_per_month
    #   approval_mode, approval_channel, on_approval_timeout
    #   media_approved, opt_out, etc.

    # --- Base flags ---

    @property
    def media_approved(self) -> bool:
        return self.attributes.get("media_approved", True)

    @property
    def opt_out(self) -> bool:
        return self.attributes.get("opt_out", False)

    # --- Brand DNA convenience accessors ---

    @property
    def content_theme(self) -> Optional[str]:
        return self.attributes.get("content_theme")

    @property
    def content_pillars(self) -> List[str]:
        return self.attributes.get("content_pillars") or []

    @property
    def suggested_posts_per_week(self) -> Optional[int]:
        return self.attributes.get("suggested_posts_per_week")

    @property
    def tone(self) -> Optional[str]:
        return self.attributes.get("tone")

    @property
    def negative_constraints(self) -> Optional[str]:
        return self.attributes.get("negative_constraints")

    @property
    def tips(self) -> List[str]:
        return self.attributes.get("tips") or []

    @property
    def myths(self) -> List[str]:
        return self.attributes.get("myths") or []

    @property
    def content_atoms(self) -> Dict[str, Any]:
        """
        Normalise content_atoms to a dict so Jinja usage like
        attributes.content_atoms.story_mission works reliably.
        """
        atoms = self.attributes.get("content_atoms") or {}
        if isinstance(atoms, dict):
            return atoms
        return {}

    # --- Ecommerce helpers ---

    @property
    def is_ecommerce(self) -> bool:
        return bool(self.attributes.get("is_ecommerce"))

    @property
    def ecommerce_platform(self) -> Optional[str]:
        return self.attributes.get("ecommerce_platform")

    @property
    def product_categories(self) -> List[str]:
        return self.attributes.get("product_categories") or []

    @property
    def product_spotlights(self) -> List[Dict[str, Any]]:
        spotlights = self.attributes.get("product_spotlights") or []
        if isinstance(spotlights, list):
            return spotlights
        return []

    # --- Posting rules (per-client) ---

    @property
    def cooldown_days(self) -> int:
        from main import COOLDOWN_DAYS  # avoid circular import at module load
        try:
            return int(self.attributes.get("cooldown_days", COOLDOWN_DAYS))
        except (TypeError, ValueError):
            return COOLDOWN_DAYS

    @property
    def max_posts_per_month(self) -> int:
        from main import PER_CLIENT_MONTHLY_CAP
        try:
            return int(self.attributes.get("max_posts_per_month", PER_CLIENT_MONTHLY_CAP))
        except (TypeError, ValueError):
            return PER_CLIENT_MONTHLY_CAP

    # --- Approval settings (per-client) ---

    @property
    def approval_mode(self) -> str:
        """
        'always', 'first_n', 'auto_with_notifications', 'auto_silent'
        """
        value = self.attributes.get("approval_mode") or "always"
        return str(value).lower()

    @property
    def approval_channel(self) -> str:
        """
        'telegram', 'web', 'both' (currently primarily 'telegram')
        """
        value = self.attributes.get("approval_channel") or "telegram"
        return str(value).lower()

    @property
    def on_approval_timeout(self) -> str:
        """
        'auto_post', 'auto_cancel', 'fallback'
        (hooked in when we wire full timeout logic)
        """
        value = self.attributes.get("on_approval_timeout") or "auto_post"
        return str(value).lower()


    # --- Brand DNA convenience accessors ---

    @property
    def content_theme(self) -> Optional[str]:
        return self.attributes.get("content_theme")

    @property
    def content_pillars(self) -> List[str]:
        return self.attributes.get("content_pillars") or []

    @property
    def suggested_posts_per_week(self) -> Optional[int]:
        return self.attributes.get("suggested_posts_per_week")

    @property
    def negative_constraints(self) -> Optional[str]:
        return self.attributes.get("negative_constraints")

    @property
    def tips(self) -> List[str]:
        return self.attributes.get("tips") or []

    @property
    def myths(self) -> List[str]:
        return self.attributes.get("myths") or []

    @property
    def content_atoms(self) -> Dict[str, Any]:
        # Normalise to a dict to make Jinja rendering easier
        atoms = self.attributes.get("content_atoms") or {}
        if isinstance(atoms, dict):
            return atoms
        return {}

    # --- Ecommerce helpers ---

    @property
    def is_ecommerce(self) -> bool:
        return bool(self.attributes.get("is_ecommerce"))

    @property
    def ecommerce_platform(self) -> Optional[str]:
        return self.attributes.get("ecommerce_platform")

    @property
    def product_categories(self) -> List[str]:
        return self.attributes.get("product_categories") or []

    @property
    def product_spotlights(self) -> List[Dict[str, Any]]:
        # Ensure we always get a list of dicts for Jinja
        spotlights = self.attributes.get("product_spotlights") or []
        if isinstance(spotlights, list):
            return spotlights
        return []

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
# KV helpers (used by Telegram, etc.)
# ------------------
def kget(key: str) -> Optional[str]:
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(
            text("SELECT v FROM kv WHERE k = :k LIMIT 1"),
            {"k": key},
        ).fetchone()
    return row[0] if row else None


def kset(key: str, value: str) -> None:
    with STATE_ENGINE.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO kv(k, v) VALUES (:k, :v) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v"
            ),
            {"k": key, "v": value},
        )


# ------------------
# Post candidate helpers (drafts)
# ------------------
def create_post_candidate(
    client_id: str,
    template_key: str,
    text_body: str,
    media_url: Optional[str],
    platforms: Optional[List[str]],
    slot_time: datetime,
    status: str = "PENDING",
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Create a draft candidate row and return its id.
    """
    now = datetime.now(TZ)
    platforms_json = json.dumps(platforms) if platforms else None
    meta_json = json.dumps(metadata or {})
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO post_candidates("
                "client_id, template_key, text_body, media_url, slot_time, "
                "status, platforms, rejection_reason, metadata, created_at, updated_at"
                ") VALUES (:cid, :tk, :tb, :mu, :slot, :st, :pf, :rr, :md, :ts, :ts) "
                "RETURNING id"
            ),
            {
                "cid": client_id,
                "tk": template_key,
                "tb": text_body,
                "mu": media_url,
                "slot": slot_time,
                "st": status,
                "pf": platforms_json,
                "rr": None,
                "md": meta_json,
                "ts": now,
            },
        ).fetchone()
    return int(row[0])


def get_post_candidate(candidate_id: int) -> Optional[Dict[str, Any]]:
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM post_candidates WHERE id = :id"),
            {"id": candidate_id},
        ).mappings().first()
    if not row:
        return None

    data = dict(row)

    # Decode JSON-like fields
    platforms_raw = data.get("platforms")
    if isinstance(platforms_raw, str):
        try:
            data["platforms"] = json.loads(platforms_raw) or []
        except Exception:
            data["platforms"] = []
    else:
        data["platforms"] = platforms_raw or []

    meta_raw = data.get("metadata")
    if isinstance(meta_raw, str):
        try:
            data["metadata"] = json.loads(meta_raw) or {}
        except Exception:
            data["metadata"] = {}
    else:
        data["metadata"] = meta_raw or {}

    return data


def ensure_main_db_schema():
    """Creates the clients table and view if they don't exist."""
    if not DATABASE_URL: return
    
    # CRITICAL FIX: Ensure we use the PyMySQL driver
    url = DATABASE_URL.replace("mysql://", "mysql+pymysql://")
    
    try:
        eng = create_engine(url, pool_pre_ping=True)
        with eng.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS clients (
                    id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    website VARCHAR(255),
                    industry VARCHAR(100),
                    city VARCHAR(100),
                    attributes JSON, 
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """))
            conn.execute(text("""
                CREATE OR REPLACE VIEW bot_clients_v AS
                SELECT id, name, industry, city, attributes
                FROM clients;
            """))
            logger.info("Main DB schema initialized.")
    except Exception as e:
        logger.error(f"Failed to init Main DB: {e}")


def seed_demo_data():
    """Populates the DB with demo clients if it is empty."""
    if not DATABASE_URL: return
    
    # CRITICAL: Use PyMySQL protocol
    url = DATABASE_URL.replace("mysql://", "mysql+pymysql://")
    
    try:
        eng = create_engine(url, pool_pre_ping=True)
        with eng.begin() as conn:
            # Check if empty
            count = conn.execute(text("SELECT COUNT(*) FROM clients")).scalar()
            if count > 0:
                logger.info("Database already has data. Skipping seed.")
                return

            logger.info("Seeding database with demo clients...")
            
            # Use parameterized query to avoid SQLAlchemy text() parsing issues with colons in JSON
            rows = [
                {
                    "id": "joes_gym_1",
                    "name": "Joe's Gym", 
                    "industry": "Fitness", 
                    "city": "Cape Town",
                    "attributes": json.dumps({
                        "website": "https://joesgym.co.za", 
                        "hero_image_url": "https://images.unsplash.com/photo-1534438327276-14e5300c3a48?q=80&w=1470&auto=format&fit=crop",
                        "tone": "High Energy", 
                        "tips": ["Drink water!", "Never skip leg day"], 
                        "myths": ["Carbs are bad"], 
                        "content_atoms": {"story_mission": "We started in 2010 to help people get fit."}
                    })
                },
                {
                    "id": "smile_dental_1", 
                    "name": "Smile Dental", 
                    "industry": "Healthcare", 
                    "city": "Sandton", 
                    "attributes": json.dumps({
                        "website": "https://smiledental.co.za", 
                        "hero_image_url": "https://images.unsplash.com/photo-1606811841689-23dfddce3e95?q=80&w=1374&auto=format&fit=crop",
                        "tone": "Professional", 
                        "tips": ["Floss daily", "Brush twice"], 
                        "myths": ["Sugar is fine"], 
                        "content_atoms": {"story_mission": "Creating smiles since 1995."}
                    })
                }
            ]
            
            stmt = text("""
                INSERT INTO clients (id, name, industry, city, attributes) 
                VALUES (:id, :name, :industry, :city, :attributes)
            """)
            
            conn.execute(stmt, rows)
            logger.info("Seeding complete!")
            
    except Exception as e:
        logger.error(f"Failed to seed DB: {e}")


def update_post_candidate_status(
    candidate_id: int,
    status: str,
    rejection_reason: Optional[str] = None,
) -> None:
    with STATE_ENGINE.begin() as conn:
        conn.execute(
            text(
                "UPDATE post_candidates "
                "SET status = :st, "
                "    rejection_reason = COALESCE(:rr, rejection_reason), "
                "    updated_at = :ts "
                "WHERE id = :id"
            ),
            {
                "id": candidate_id,
                "st": status,
                "rr": rejection_reason,
                "ts": datetime.now(TZ),
            },
        )


def update_post_candidate_metadata(candidate_id: int, updates: Dict[str, Any]) -> None:
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(
            text("SELECT metadata FROM post_candidates WHERE id = :id"),
            {"id": candidate_id},
        ).fetchone()
        if not row:
            return

        raw = row[0] or "{}"
        try:
            meta = json.loads(raw) or {}
        except Exception:
            meta = {}

        meta.update(updates or {})

        conn.execute(
            text(
                "UPDATE post_candidates "
                "SET metadata = :md, updated_at = :ts "
                "WHERE id = :id"
            ),
            {
                "id": candidate_id,
                "md": json.dumps(meta),
                "ts": datetime.now(TZ),
            },
        )


# ------------------
# DB ingest
# ------------------
MAIN_ENGINE: Optional[Engine] = None
_DRY_RUN_CACHE: Optional[List[Client]] = None


def _sample_clients_for_dry() -> List[Client]:
    global _DRY_RUN_CACHE
    if _DRY_RUN_CACHE:
        return _DRY_RUN_CACHE
        
    # Initialize Cache if empty
    _DRY_RUN_CACHE = [
        Client(
            id="dry-1",
            name="Joe's Gym",
            industry="Fitness",
            city="Cape Town",
            attributes={
                "website": "https://joesgym.co.za",
                "tips": ["Drink water!", "Never skip leg day."],
                "negative_constraints": "Do not mention steroids.",
                "tone": "High Energy",
                # Add empty placeholders so Pre-flight check fails initially (for testing)
                "myths": [], 
                "content_atoms": {}
            }
        ),
        Client(
            id="dry-2",
            name="Smile Dental",
            industry="Healthcare",
            city="Sandton",
            attributes={
                "website": "https://smiledental.co.za",
                "tips": [], # Intentionally empty for testing
                "myths": [],
                "negative_constraints": "No blood or scary needles.",
                "tone": "Professional and Gentle"
            }
        )
    ]
    return _DRY_RUN_CACHE

def get_main_engine() -> Engine:
    global MAIN_ENGINE
    if MAIN_ENGINE is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        
        # CRITICAL FIX: Ensure we use the PyMySQL driver
        real_url = DATABASE_URL.replace("mysql://", "mysql+pymysql://")
        
        MAIN_ENGINE = create_engine(real_url, future=True, pool_pre_ping=True)
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


def recent_template_keys(client_id: str, limit: int = 8) -> List[str]:
    """
    Return the most recent template keys used for this client, newest first.
    Used to avoid hammering the same template repeatedly.
    """
    with STATE_ENGINE.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT template_key "
                "FROM published_posts "
                "WHERE client_id = :cid "
                "ORDER BY posted_at DESC "
                "LIMIT :lim"
            ),
            {"cid": client_id, "lim": limit},
        ).fetchall()
    return [r[0] for r in rows]


def select_template(templates: Dict[str, Any], client: Client, monthly_count: int) -> Dict[str, Any]:
    """
    Implements the 4-1-1 Rule based on monthly post count.

    Cycle of 6 posts:
    0,1,2,3 -> educational
    4       -> soft_sell
    5       -> hard_sell
    """
    cycle_index = monthly_count % 6

    if cycle_index == 4:
        target_category = "soft_sell"
    elif cycle_index == 5:
        target_category = "hard_sell"
    else:
        target_category = "educational"

    # Start with templates in the target category
    candidates = [t for t in templates.values() if t.get("category") == target_category]

    # Fallback if category missing
    if not candidates:
        candidates = list(templates.values())

    # Light diversity: avoid hammering the same template key
    try:
        recent_keys = recent_template_keys(client.id, limit=8)
    except Exception:
        recent_keys = []

    # Avoid the last 3 keys if we have alternatives
    recent_avoid = set(recent_keys[:3])
    fresh = [t for t in candidates if t.get("key") not in recent_avoid]
    pool = fresh or candidates

    # Deterministic-ish choice based on day/client
    seed = int(datetime.now(TZ).strftime("%Y%m%d")) ^ hash(client.id)
    rng = random.Random(seed)
    return rng.choice(pool)


def render_text(tpl_text: str, client: Client) -> str:
    env = build_env()
    attrs = client.attributes or {}
    ctx = {
        "name": client.name,
        "city": client.city,
        "industry": client.industry,
        "attributes": attrs,
        # Shortcuts for brand DNA
        "content_theme": attrs.get("content_theme"),
        "content_pillars": attrs.get("content_pillars") or [],
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
    def __init__(self, client_config: Dict[str, Any]):
        # We now look for keys inside the CLIENT'S attributes, not the global env
        self.consumer_key = os.getenv("X_CONSUMER_KEY") # The App Key is global (Yours)
        self.consumer_secret = os.getenv("X_CONSUMER_SECRET") # The App Secret is global (Yours)
        
        # These are specific to the CLIENT
        self.access_token = client_config.get("x_access_token")
        self.access_token_secret = client_config.get("x_access_token_secret")

    def publish(self, text: str, media_url: Optional[str] = None) -> PublishResult:
        MAX = 280
        if len(text) > MAX: text = text[: MAX - 1] + "…"
        
        # If running dry run or missing keys, just log it
        if DRY_RUN or not (self.consumer_key and self.access_token):
            logger.info("[DRY] x | %s (No tokens found for this client)", text)
            return PublishResult({"platform": self.platform, "id": None, "text": text})

        try:
            twitter = OAuth1Session(
                self.consumer_key,
                client_secret=self.consumer_secret,
                resource_owner_key=self.access_token,
                resource_owner_secret=self.access_token_secret,
            )
            payload = {"text": text}
            response = twitter.post("https://api.twitter.com/2/tweets", json=payload)

            if response.status_code != 201:
                logger.error("X Post Failed: %s", response.text)
                return PublishResult({"platform": self.platform, "id": None, "text": text, "error": response.text})

            data = response.json()
            tweet_id = data.get("data", {}).get("id")
            return PublishResult({"platform": self.platform, "id": tweet_id, "text": text})
        except Exception as e:
            logger.error("X Exception: %s", e)
            return PublishResult({"platform": self.platform, "id": None, "text": text, "error": str(e)})

class FacebookPublisher(Publisher):
    platform = "facebook"
    def __init__(self, client_config: Dict[str, Any]):
        self.page_id = client_config.get("facebook_page_id")
        self.access_token = client_config.get("facebook_page_token")

    def publish(self, text: str, media_url: Optional[str] = None) -> PublishResult:
        if DRY_RUN or not (self.page_id and self.access_token):
            logger.info("[DRY] facebook | %s", text)
            return PublishResult({"platform": self.platform, "id": None, "text": text})

        try:
            url = f"https://graph.facebook.com/v18.0/{self.page_id}/feed"
            payload = {"message": text, "access_token": self.access_token}
            
            resp = requests.post(url, data=payload)
            if resp.status_code != 200:
                return PublishResult({"platform": self.platform, "id": None, "text": text, "error": resp.text})
            
            data = resp.json()
            return PublishResult({"platform": self.platform, "id": data.get("id"), "text": text})
        except Exception as e:
            logger.error("Facebook Post Failed: %s", e)
            return PublishResult({"platform": self.platform, "id": None, "text": text, "error": str(e)})


def build_publishers(client: Client) -> list[Publisher]:
    pubs = [ConsolePublisher()]
    
    attrs = client.attributes or {}
    
    # X (Twitter)
    if ENABLE_X and attrs.get("x_access_token"):
        pubs.append(XPublisher(attrs))
        
    # Facebook
    if attrs.get("facebook_page_token"):
        pubs.append(FacebookPublisher(attrs))

    return pubs

# ------------------
# Core Logic
# ------------------
def already_posted_recently(client_id: str, cooldown_days: int) -> bool:
    cutoff = datetime.now(TZ) - timedelta(days=cooldown_days)
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM published_posts WHERE client_id=:cid AND posted_at>=:cutoff LIMIT 1"),
            {"cid": client_id, "cutoff": cutoff},
        ).fetchone()
        return bool(row)

def monthly_count(client_id: str, when: datetime) -> int:
    start, end = month_bounds(when)
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM published_posts "
                "WHERE client_id=:cid AND posted_at>=:start AND posted_at<:end"
            ),
            {"cid": client_id, "start": start, "end": end},
        ).fetchone()
    return int(row[0]) if row else 0


def compute_rejection_patterns(
    window_days: int = 30,
    client_id: Optional[str] = None,
    min_rejections_per_template: int = 2,
) -> Dict[str, Any]:
    """Analyse REJECTED post_candidates and surface pattern suggestions.

    Returns a JSON-serialisable dict with:
      - total_rejections
      - templates: list of {template_key, total_rejections, reason_counts}
      - negative_constraints_suggestions
      - tone_suggestions
    """
    now = datetime.now(TZ)
    start = now - timedelta(days=window_days)

    try:
        conditions = ["status = 'REJECTED'", "updated_at >= :start"]
        params: Dict[str, Any] = {"start": start}

        if client_id:
            conditions.append("client_id = :cid")
            params["cid"] = client_id

        where_clause = " AND ".join(conditions)
        sql = text(
            "SELECT client_id, template_key, rejection_reason "
            "FROM post_candidates "
            f"WHERE {where_clause}"
        )

        with STATE_ENGINE.begin() as conn:
            rows = conn.execute(sql, params).mappings().all()
    except Exception as e:
        logger.exception("Pattern learner query failed: %s", e)
        return {
            "generated_at": now.isoformat(),
            "window_days": window_days,
            "client_id": client_id,
            "total_rejections": 0,
            "templates": [],
            "negative_constraints_suggestions": [],
            "tone_suggestions": [],
        }

    if not rows:
        return {
            "generated_at": now.isoformat(),
            "window_days": window_days,
            "client_id": client_id,
            "total_rejections": 0,
            "templates": [],
            "negative_constraints_suggestions": [],
            "tone_suggestions": [],
        }

    from collections import Counter, defaultdict

    per_template: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        tpl = r["template_key"]
        reason = (r.get("rejection_reason") or "").strip()
        per_template[tpl].append(reason)

    def bucket_reason(reason: str) -> str:
        if not reason:
            return "unspecified"
        low = reason.lower()
        if "salesy" in low or "pushy" in low or "hard sell" in low:
            return "too_salesy"
        if "tone" in low or "voice" in low or "formal" in low or "casual" in low:
            return "wrong_tone"
        if "off-topic" in low or "off topic" in low or "irrelevant" in low:
            return "off_topic"
        if "long" in low or "wordy" in low:
            return "too_long"
        if "short" in low or "thin" in low:
            return "too_short"
        if "repeat" in low or "repetitive" in low or "boring" in low:
            return "repetitive"
        return "other"

    templates_summary: List[Dict[str, Any]] = []
    negative_constraints_suggestions: set = set()
    tone_suggestions: set = set()

    for tpl_key, reasons in per_template.items():
        if len(reasons) < min_rejections_per_template:
            continue
        buckets = [bucket_reason(r) for r in reasons]
        counts = Counter(buckets)
        total = len(reasons)

        templates_summary.append(
            {
                "template_key": tpl_key,
                "total_rejections": total,
                "reason_counts": dict(counts),
            }
        )

        top_reason, _ = counts.most_common(1)[0]

        if top_reason == "too_salesy":
            negative_constraints_suggestions.add(
                "Avoid overly pushy or hard-sell language in everyday posts."
            )
        if top_reason == "wrong_tone":
            tone_suggestions.add(
                "Adjust the tone to be closer to how the client naturally speaks (less hype, more plain language)."
            )
        if top_reason == "off_topic":
            negative_constraints_suggestions.add(
                "Avoid posts that drift away from the brand's core services or audience problems."
            )
        if top_reason == "repetitive":
            negative_constraints_suggestions.add(
                "Avoid repeating the same hook or promise across multiple posts in a short period."
            )

    return {
        "generated_at": now.isoformat(),
        "window_days": window_days,
        "client_id": client_id,
        "total_rejections": len(rows),
        "templates": templates_summary,
        "negative_constraints_suggestions": sorted(negative_constraints_suggestions),
        "tone_suggestions": sorted(tone_suggestions),
    }


def run_rejection_pattern_learner(window_days: int = 30) -> None:
    """Periodic job that logs pattern suggestions globally and per client."""
    global_summary = compute_rejection_patterns(window_days=window_days, client_id=None)
    logger.info("Global rejection patterns: %s", json.dumps(global_summary))

    # Per-client summaries for clients with recent rejections
    now = datetime.now(TZ)
    start = now - timedelta(days=window_days)
    try:
        with STATE_ENGINE.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT client_id "
                    "FROM post_candidates "
                    "WHERE status = 'REJECTED' AND updated_at >= :start"
                ),
                {"start": start},
            ).fetchall()
    except Exception as e:
        logger.exception("Pattern learner client discovery failed: %s", e)
        return

    for (client_id,) in rows:
        summary = compute_rejection_patterns(window_days=window_days, client_id=client_id)
        logger.info("Rejection patterns for client %s: %s", client_id, json.dumps(summary))


def choose_client_for_slot(clients: List[Client]) -> Optional[Client]:
    now = datetime.now(TZ)

    # Eligibility check (per-client cooldown & opt-outs)
    eligible: List[Client] = []
    for c in clients:
        if c.opt_out or not c.media_approved:
            continue
        if not already_posted_recently(c.id, c.cooldown_days):
            continue
        # NOTE: already_posted_recently returns True if we *have* a recent post,
        # so we only keep clients that are NOT blocked by cooldown.
        eligible.append(c)

    if not eligible:
        logger.info("No eligible clients after cooldown/opt-out filtering.")
        return None
    
    # Cap check (per-client monthly caps)
    counts: Dict[str, int] = {c.id: monthly_count(c.id, now) for c in eligible}
    caps: Dict[str, int] = {c.id: c.max_posts_per_month for c in eligible}

    pool = [c for c in eligible if counts[c.id] < caps[c.id]]
    if not pool:
        logger.info("No eligible clients after monthly cap filtering.")
        return None
    
    # Prioritise lowest count
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
    for pub in build_publishers(c):
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
    """
    Pick a client for this slot and create/send a candidate.
    """
    clients = fetch_clients()
    choice = choose_client_for_slot(clients)
    if not choice:
        logger.info("No eligible client for this slot.")
        return

    try:
        from telegram_approval import handle_scheduled_post
        handle_scheduled_post(choice, record_state=record_state)
    except ImportError:
        logger.info("telegram_approval not available, publishing directly.")
        publish_once(choice, record_state=record_state)
    except Exception:
        logger.exception("handle_scheduled_post failed", exc_info=True)


def run_approval_timeouts(grace_minutes: int = 10) -> None:
    """
    Sweep PENDING post_candidates whose slot_time is older than `grace_minutes`
    and apply per-client timeout behaviour:

    - on_approval_timeout = 'auto_post'   -> auto-approve + publish
    - on_approval_timeout = 'auto_cancel' -> mark TIMEOUT, skip
    - on_approval_timeout = 'fallback'    -> mark TIMEOUT, publish a safe evergreen via publish_once()
    """
    now = datetime.now(TZ)
    cutoff = now - timedelta(minutes=grace_minutes)

    with STATE_ENGINE.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, client_id, template_key, text_body, media_url, platforms "
                "FROM post_candidates "
                "WHERE status = 'PENDING' AND slot_time <= :cutoff"
            ),
            {"cutoff": cutoff},
        ).mappings().all()

    if not rows:
        return

    clients_by_id = {c.id: c for c in fetch_clients()}

    for row in rows:
        candidate_id = row["id"]
        client = clients_by_id.get(row["client_id"])
        if not client:
            update_post_candidate_status(candidate_id, "TIMEOUT")
            logger.info("Candidate %s timed out (client missing).", candidate_id)
            continue

        timeout_mode = (client.attributes.get("on_approval_timeout") or "auto_post").lower()

        platforms = row.get("platforms")
        if isinstance(platforms, str):
            try:
                platforms = json.loads(platforms) or []
            except Exception:
                platforms = []
        else:
            platforms = platforms or []

        if timeout_mode == "auto_cancel":
            update_post_candidate_status(candidate_id, "TIMEOUT")
            logger.info("Candidate %s auto-cancelled on timeout.", candidate_id)

        elif timeout_mode == "fallback":
            update_post_candidate_status(candidate_id, "TIMEOUT")
            logger.info("Candidate %s timed out; publishing fallback for client %s.", candidate_id, client.id)
            try:
                publish_once(client, record_state=True)
            except Exception:
                logger.exception("Fallback publish failed for client %s", client.id)

        else:  # 'auto_post' (default)
            logger.info("Candidate %s auto-posting on timeout.", candidate_id)
            try:
                publish_text_for_client(
                    client,
                    row["text_body"],
                    row["media_url"],
                    row["template_key"],
                    platforms,
                    record_state=True,
                )
                update_post_candidate_status(candidate_id, "APPROVED")
            except Exception:
                logger.exception("Auto-post on timeout failed for candidate %s", candidate_id)



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
    ensure_main_db_schema()
    seed_demo_data()
    schedule_today_slots()
    SCHED.add_job(schedule_today_slots, "cron", hour=0, minute=5)
    # Periodic pattern learner over rejected posts (once a day)
    SCHED.add_job(run_rejection_pattern_learner, "cron", hour=3, minute=0)
    SCHED.start()

    if DRY_RUN or TELEGRAM_PREVIEW_ON_STARTUP:

        logger.info(
            "Running startup preview: DRY_RUN=%s, TELEGRAM_PREVIEW_ON_STARTUP=%s, TELEGRAM_APPROVAL_ENABLED=%s",
            DRY_RUN,
            TELEGRAM_PREVIEW_ON_STARTUP,
            TELEGRAM_APPROVAL_ENABLED,
        )
        try:
            # record_state=False so this doesn't pollute monthly caps / cooldown logic
            run_rotation_post(record_state=False)
        except Exception:
            logger.exception("Startup preview failed")

    try:
        yield
    finally:
        SCHED.shutdown(wait=False)



app = FastAPI(title="Universal Agency Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# SECURITY WARNING: In production, change "super-secret-key" to a real random string!
app.add_middleware(SessionMiddleware, secret_key=os.getenv("API_SECRET_KEY", "super-secret-key"))
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(TZ).isoformat(), "dry_run": DRY_RUN}

@app.get("/debug-env")
def debug_env():
    # Helper to mask values for security
    def mask(val):
        if not val: return "MISSING"
        return val[:4] + "***" + val[-4:] if len(val) > 8 else "***"

    # List specific keys we care about
    keys_to_check = [
        "FIRECRAWL_API_KEY",
        "DATABASE_URL", 
        "OPENAI_API_KEY"
    ]
    
    # Metadata to identify the running environment
    meta_keys = [
        "RAILWAY_ENVIRONMENT_NAME",
        "RAILWAY_GIT_BRANCH",
        "RAILWAY_SERVICE_NAME"
    ]

    return {
        "metadata": {k: os.getenv(k) for k in meta_keys},
        "all_keys": list(os.environ.keys()),
        "values": {k: mask(os.getenv(k)) for k in keys_to_check}
    }

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


@app.get("/rejections", response_class=HTMLResponse)
def view_rejections(
    client_id: Optional[str] = Query(None),
    reason: Optional[str] = Query(None),
    template_key: Optional[str] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """Simple HTML view of rejected post candidates, globally or per client."""

    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None

    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)

    conditions = ["status = 'REJECTED'"]
    params: Dict[str, Any] = {"limit": limit}

    if client_id:
        conditions.append("client_id = :client_id")
        params["client_id"] = client_id
    if template_key:
        conditions.append("template_key = :template_key")
        params["template_key"] = template_key
    if reason:
        conditions.append("rejection_reason LIKE :reason")
        params["reason"] = f"%{reason}%"
    if start_dt:
        conditions.append("updated_at >= :start")
        params["start"] = start_dt
    if end_dt:
        conditions.append("updated_at <= :end")
        params["end"] = end_dt

    where_clause = " AND ".join(conditions)

    try:
        with STATE_ENGINE.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT client_id, template_key, text_body, rejection_reason, slot_time, updated_at "
                    "FROM post_candidates "
                    f"WHERE {where_clause} "
                    "ORDER BY updated_at DESC "
                    "LIMIT :limit"
                ),
                params,
            ).mappings().all()
    except Exception as e:
        logger.exception("Failed to render /rejections view: %s", e)
        return HTMLResponse(
            f"<html><body><h1>Error</h1><p>{html.escape(str(e))}</p></body></html>"
        )

    clients_by_id = {c.id: c for c in fetch_clients()}

    parts = [
        "<html><head><title>Rejected posts</title></head><body>",
        "<h1>Rejected posts</h1>",
    ]

    if client_id:
        client = clients_by_id.get(client_id)
        label = client.name if client else client_id
        parts.append(f"<p>Client: {html.escape(str(label))}</p>")

    if not rows:
        parts.append("<p>No rejected posts found for the selected filters.</p>")
        parts.append("</body></html>")
        return "".join(parts)

    parts.append("<table border='1' cellspacing='0' cellpadding='4'>")
    parts.append(
        "<tr><th>Client</th><th>Template</th><th>Reason</th><th>Snippet</th><th>Slot time</th><th>Updated at</th></tr>"
    )

    for r in rows:
        client = clients_by_id.get(r["client_id"])
        client_label = client.name if client else r["client_id"]
        body = r.get("text_body") or ""
        snippet = body[:180] + ("…" if len(body) > 180 else "")

        parts.append(
            "<tr>"
            f"<td>{html.escape(str(client_label))}</td>"
            f"<td>{html.escape(str(r['template_key']))}</td>"
            f"<td>{html.escape(str(r.get('rejection_reason') or ''))}</td>"
            f"<td>{html.escape(snippet)}</td>"
            f"<td>{html.escape(str(r.get('slot_time') or ''))}</td>"
            f"<td>{html.escape(str(r.get('updated_at') or ''))}</td>"
            "</tr>"
        )

    parts.append("</table></body></html>")
    return "".join(parts)


@app.get("/rejections/patterns")
def get_rejection_patterns(
    client_id: Optional[str] = Query(None),
    window_days: int = Query(30, ge=1, le=365),
):
    """Return rejection pattern suggestions for the last N days."""
    summary = compute_rejection_patterns(window_days=window_days, client_id=client_id)
    return summary



@app.post("/api/clients/{client_id}/attributes/merge")
def merge_client_attributes(client_id: str, overrides: Dict[str, Any] = Body(...)):
    """Merge attributes. Prefers Real DB if available."""
    
    # 1. REAL DB SAVE (Priority)
    if DATABASE_URL:
        try:
            # Fix protocol just in case
            url = DATABASE_URL.replace("mysql://", "mysql+pymysql://")
            eng = create_engine(url, pool_pre_ping=True)
            
            with eng.begin() as conn:
                row = conn.execute(
                    text("SELECT attributes FROM clients WHERE id = :id"),
                    {"id": client_id}
                ).fetchone()

                if not row:
                    # If not in DB, maybe it's a dry-run sample we want to "promote" to real DB?
                    # For now, just fail or handle gracefully.
                    return {"ok": False, "error": "Client not found in Database"}

                current_raw = row[0]
                current_attrs = {}
                if isinstance(current_raw, dict):
                    current_attrs = current_raw
                elif isinstance(current_raw, str):
                    try: current_attrs = json.loads(current_raw)
                    except: current_attrs = {}
                
                merged = {**current_attrs, **overrides}
                
                conn.execute(
                    text("UPDATE clients SET attributes = :attr WHERE id = :id"),
                    {"id": client_id, "attr": json.dumps(merged)}
                )
            return {"ok": True, "client_id": client_id, "storage": "database"}
        except Exception as e:
            logger.exception("DB Save Failed")
            raise HTTPException(status_code=500, detail=f"DB Error: {str(e)}")

    # 2. IN-MEMORY FALLBACK (Only if no DB is connected)
    logger.info(f"[Memory] Updating client {client_id}")
    clients = fetch_clients()
    target = next((c for c in clients if c.id == client_id), None)
    if target:
        target.attributes.update(overrides)
        return {"ok": True, "client_id": client_id, "storage": "memory"}
    
    raise HTTPException(status_code=404, detail="Client not found")


@app.post("/telegram/webhook")
async def telegram_webhook(update: Dict[str, Any]):
    try:
        from telegram_approval import handle_telegram_update
        handle_telegram_update(update)
    except Exception as e:
        logger.exception("Webhook failed: %s", e)
    return {"ok": True}

# ------------------
# OAuth / Connect Routes
# ------------------

# In-memory store for request tokens (step 1 of OAuth 1.0a)
# In production, use Redis. For a single-instance bot, a dict is fine.
oauth_tokens = {} 

@app.get("/auth/{platform}/login")
def auth_login(platform: str, client_id: str, request: Request):
    # --- 1. DRY RUN / MOCK MODE ---
    if DRY_RUN:
        logger.info(f"DRY RUN: Mocking login for {platform} client {client_id}")
        
        # Fake tokens
        fake_update = {}
        if platform == "x":
            fake_update = {"x_access_token": "dry_run_fake_token"}
        elif platform == "linkedin":
            fake_update = {"linkedin_access_token": "dry_run_fake_token"}
        elif platform == "facebook":
            fake_update = {
                "facebook_page_token": "dry_run_fake_token",
                "facebook_page_name": "Dry Run Page",
                "facebook_page_id": "dry_run_123"
            }
        
        # Save fake tokens directly to DB
        eng = get_main_engine()
        with eng.begin() as conn:
             row = conn.execute(text("SELECT attributes FROM clients WHERE id = :id"), {"id": client_id}).fetchone()
             if not row: return HTMLResponse("Client not found", status_code=404)
             
             attrs = {}
             if row[0]:
                 try: attrs = json.loads(row[0])
                 except: pass
            
             attrs.update(fake_update)
             
             conn.execute(
                 text("UPDATE clients SET attributes = :attr WHERE id = :id"),
                 {"id": client_id, "attr": json.dumps(attrs)}
             )
        
        # Redirect back to settings immediately
        frontend_url = "https://postify.co.za" 
        return RedirectResponse(f"{frontend_url}/clients/{client_id}/settings")

    # --- 2. REAL OAUTH LOGIC (Existing code below) ---
    if platform == "linkedin":
        client_key = os.getenv("LINKEDIN_CLIENT_ID")
        if not client_key:
             return HTMLResponse("Server Error: LINKEDIN_CLIENT_ID not set.", status_code=500)
        
        request.session["connecting_client_id"] = client_id
        base_url = str(request.base_url).rstrip("/")
        callback_uri = f"{base_url}/auth/linkedin/callback"
        
        state = hashlib.sha256(os.urandom(1024)).hexdigest()
        request.session["oauth_state"] = state
        
        params = {
            "response_type": "code",
            "client_id": client_key,
            "redirect_uri": callback_uri,
            "state": state,
            "scope": "openid profile email w_member_social"
        }
        from urllib.parse import urlencode
        url = f"https://www.linkedin.com/oauth/v2/authorization?{urlencode(params)}"
        return RedirectResponse(url)

    if platform == "facebook":
        app_id = os.getenv("FACEBOOK_APP_ID")
        if not app_id:
             return HTMLResponse("Server Error: FACEBOOK_APP_ID not set.", status_code=500)
        
        request.session["connecting_client_id"] = client_id
        base_url = str(request.base_url).rstrip("/")
        callback_uri = f"{base_url}/auth/facebook/callback"
        
        state = hashlib.sha256(os.urandom(1024)).hexdigest()
        request.session["oauth_state"] = state
        
        scope = "pages_manage_posts,pages_read_engagement,public_profile"
        
        url = (
            f"https://www.facebook.com/v18.0/dialog/oauth?"
            f"client_id={app_id}&redirect_uri={callback_uri}&state={state}&scope={scope}"
        )
        return RedirectResponse(url)

    if platform == "x":
        consumer_key = os.getenv("X_CONSUMER_KEY")
        consumer_secret = os.getenv("X_CONSUMER_SECRET")
        if not consumer_key or not consumer_secret:
             return HTMLResponse("Server Error: X_CONSUMER_KEY not set.", status_code=500)

        try:
            oauth = OAuth1Session(consumer_key, client_secret=consumer_secret)
            base_url = str(request.base_url).rstrip("/")
            callback_uri = f"{base_url}/auth/x/callback?origin_client_id={client_id}"
            
            fetch_response = oauth.fetch_request_token(f"https://api.twitter.com/oauth/request_token?oauth_callback={callback_uri}")
            resource_owner_key = fetch_response.get("oauth_token")
            resource_owner_secret = fetch_response.get("oauth_token_secret")
            
            oauth_tokens[resource_owner_key] = {
                "secret": resource_owner_secret,
                "client_id": client_id
            }
            
            authorization_url = oauth.authorization_url("https://api.twitter.com/oauth/authorize")
            return RedirectResponse(authorization_url)
        except Exception as e:
            logger.error("X Auth Start Failed: %s", e)
            return HTMLResponse(f"Failed to start X login: {e}", status_code=500)

    return HTMLResponse(f"Platform {platform} not supported", status_code=400)


@app.get("/auth/x/callback")
def auth_callback_x(
    oauth_token: str, 
    oauth_verifier: str, 
    origin_client_id: str, 
    request: Request
):
    """
    User is back from Twitter. We swap the temp token for a permanent one.
    """
    # 1. Retrieve the temp secret we saved earlier
    temp_data = oauth_tokens.get(oauth_token)
    if not temp_data:
        return HTMLResponse("Error: Session expired or invalid token. Try again.", status_code=400)
    
    del oauth_tokens[oauth_token] # Clean up
    
    resource_owner_secret = temp_data["secret"]
    client_id = temp_data["client_id"]
    
    # Double check client_id matches (security)
    if client_id != origin_client_id:
        return HTMLResponse("Error: Client ID mismatch.", status_code=400)

    consumer_key = os.getenv("X_CONSUMER_KEY")
    consumer_secret = os.getenv("X_CONSUMER_SECRET")

    # 2. Swap for Permanent Access Token
    try:
        oauth = OAuth1Session(
            consumer_key,
            client_secret=consumer_secret,
            resource_owner_key=oauth_token,
            resource_owner_secret=resource_owner_secret,
            verifier=oauth_verifier,
        )
        tokens = oauth.fetch_access_token("https://api.twitter.com/oauth/access_token")
        
        final_key = tokens.get("oauth_token")
        final_secret = tokens.get("oauth_token_secret")
        
        # 3. Save to Database (Merge into attributes)
        # Use get_main_engine() to ensure we write to the correct MySQL DB
        eng = get_main_engine()
        with eng.begin() as conn:
             row = conn.execute(
                 text("SELECT attributes FROM clients WHERE id = :id"), 
                 {"id": client_id}
             ).fetchone()
             
             if not row:
                 return HTMLResponse("Client not found in DB.", status_code=404)
             
             attrs = {}
             if row[0]:
                 try: attrs = json.loads(row[0])
                 except: pass
            
             # Update
             attrs["x_access_token"] = final_key
             attrs["x_access_token_secret"] = final_secret
             
             # Write back
             conn.execute(
                 text("UPDATE clients SET attributes = :attr WHERE id = :id"),
                 {"id": client_id, "attr": json.dumps(attrs)}
             )

        # 4. Success Page
        frontend_url = "https://postify.co.za" 
        return RedirectResponse(f"{frontend_url}/clients/{client_id}/settings")

    except Exception as e:
        logger.exception("X Auth Callback Failed")
        return HTMLResponse(f"Error saving X token: {e}", status_code=500)

@app.get("/auth/linkedin/callback")
def auth_callback_linkedin(code: str, state: str, request: Request):
    client_id = request.session.get("connecting_client_id")
    saved_state = request.session.get("oauth_state")
    
    if not client_id or state != saved_state:
        return HTMLResponse("Error: Session invalid or state mismatch.", status_code=400)

    client_key = os.getenv("LINKEDIN_CLIENT_ID")
    client_secret = os.getenv("LINKEDIN_CLIENT_SECRET")
    
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/linkedin/callback"
    
    token_url = "https://www.linkedin.com/oauth/v2/accessToken"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_key,
        "client_secret": client_secret
    }
    
    try:
        resp = requests.post(token_url, data=payload)
        data = resp.json()
        
        access_token = data.get("access_token")
        if not access_token:
             return HTMLResponse(f"LinkedIn Error: {data}", status_code=400)
             
        eng = get_main_engine()
        with eng.begin() as conn:
             row = conn.execute(
                 text("SELECT attributes FROM clients WHERE id = :id"), 
                 {"id": client_id}
             ).fetchone()
             
             if not row: return HTMLResponse("Client not found.", status_code=404)
             
             attrs = {}
             if row[0]:
                 try: attrs = json.loads(row[0])
                 except: pass
            
             attrs["linkedin_access_token"] = access_token
             conn.execute(
                 text("UPDATE clients SET attributes = :attr WHERE id = :id"),
                 {"id": client_id, "attr": json.dumps(attrs)}
             )
        
        frontend_url = "https://postify.co.za" 
        return RedirectResponse(f"{frontend_url}/clients/{client_id}/settings")
        
    except Exception as e:
        logger.exception("LinkedIn Callback Failed")
        return HTMLResponse(f"Error: {e}", status_code=500)


@app.get("/auth/facebook/callback")
def auth_callback_facebook(code: str, state: str, request: Request):
    client_id = request.session.get("connecting_client_id")
    saved_state = request.session.get("oauth_state")
    
    if not client_id or state != saved_state:
        return HTMLResponse("Error: Session invalid or state mismatch.", status_code=400)

    app_id = os.getenv("FACEBOOK_APP_ID")
    app_secret = os.getenv("FACEBOOK_APP_SECRET")
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/auth/facebook/callback"

    # 1. Exchange Code for Short-Lived User Token
    token_url = (
        f"https://graph.facebook.com/v18.0/oauth/access_token?"
        f"client_id={app_id}&redirect_uri={redirect_uri}&client_secret={app_secret}&code={code}"
    )
    resp = requests.get(token_url)
    data = resp.json()
    short_token = data.get("access_token")
    if not short_token:
        return HTMLResponse(f"Facebook Token Error: {data}", status_code=400)

    # 2. Exchange for Long-Lived User Token
    long_url = (
        f"https://graph.facebook.com/v18.0/oauth/access_token?"
        f"grant_type=fb_exchange_token&client_id={app_id}&client_secret={app_secret}&fb_exchange_token={short_token}"
    )
    long_resp = requests.get(long_url)
    long_data = long_resp.json()
    user_token = long_data.get("access_token", short_token)

    # 3. Fetch List of Pages this user manages
    pages_url = f"https://graph.facebook.com/v18.0/me/accounts?access_token={user_token}"
    pages_resp = requests.get(pages_url)
    pages_data = pages_resp.json().get("data", [])

    # 4. Save User Token + Page Candidates to DB
    eng = get_main_engine()
    with eng.begin() as conn:
         row = conn.execute(text("SELECT attributes FROM clients WHERE id = :id"), {"id": client_id}).fetchone()
         attrs = {}
         if row and row[0]:
             try: attrs = json.loads(row[0])
             except: pass
         
         attrs["facebook_candidates"] = [
             {"name": p["name"], "id": p["id"], "access_token": p["access_token"]} 
             for p in pages_data
         ]
         attrs["facebook_user_token"] = user_token
         
         conn.execute(
             text("UPDATE clients SET attributes = :attr WHERE id = :id"),
             {"id": client_id, "attr": json.dumps(attrs)}
         )

    frontend_url = "https://postify.co.za"
    return RedirectResponse(f"{frontend_url}/clients/{client_id}/settings")



# ------------------
# API Endpoints (The Bridge)
# ------------------

@app.get("/api/clients")
def api_list_clients():
    """Return a list of all clients for the dashboard."""
    clients = fetch_clients()
    # Convert to simple dicts
    return {"clients": [
        {
            "id": c.id, 
            "name": c.name, 
            "industry": c.industry, 
            "city": c.city, 
            "attributes": c.attributes
        } 
        for c in clients
    ]}

@app.get("/api/clients/{client_id}")
def api_get_client(client_id: str):
    """Return a single client's details."""
    clients = fetch_clients()
    client = next((c for c in clients if c.id == client_id), None)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return {
        "id": client.id, 
        "name": client.name, 
        "industry": client.industry, 
        "city": client.city, 
        "attributes": client.attributes
    }

@app.get("/api/clients/{client_id}/candidates")
def api_list_candidates(client_id: str, status: str = "PENDING"):
    """Return pending posts for approval."""
    with STATE_ENGINE.begin() as conn:
        rows = conn.execute(
            text("SELECT * FROM post_candidates WHERE client_id=:cid AND status=:st ORDER BY slot_time ASC"),
            {"cid": client_id, "st": status}
        ).mappings().all()
    
    return {"candidates": [dict(r) for r in rows]}

@app.post("/api/candidates/{candidate_id}/approve")
def api_approve_candidate(candidate_id: int):
    """Approve a post and publish it immediately."""
    candidate = get_post_candidate(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    client = next((c for c in fetch_clients() if c.id == candidate["client_id"]), None)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # Publish
    publish_text_for_client(
        client, 
        candidate["text_body"], 
        candidate["media_url"], 
        candidate["template_key"], 
        candidate["platforms"], 
        record_state=True
    )
    
    # Update Status
    update_post_candidate_status(candidate_id, "APPROVED")
    return {"ok": True}

@app.post("/api/candidates/{candidate_id}/reject")
def api_reject_candidate(candidate_id: int, payload: Dict[str, Any] = Body(...)):
    """Reject a post."""
    reason = payload.get("reason")
    update_post_candidate_status(candidate_id, "REJECTED", rejection_reason=reason)
    return {"ok": True}


@app.post("/api/clients/{client_id}/generate")
def api_generate_post(client_id: str):
    """Manually trigger the bot to write a draft post, with data validation."""
    # 1. Fetch Client
    clients = fetch_clients()
    client = next((c for c in clients if c.id == client_id), None)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    # 2. Pre-flight Data Check
    # We require specific attributes to be present for the templates to work.
    missing_fields = []
    attrs = client.attributes or {}
    
    # Check for core Brand DNA elements
    if not attrs.get("tips"): missing_fields.append("Tips")
    if not attrs.get("myths"): missing_fields.append("Myths")
    if not attrs.get("tone"): missing_fields.append("Tone of Voice")
    
    # Check for content atoms (often used in 'Soft Sell' or 'Educational' templates)
    if not attrs.get("content_atoms"): missing_fields.append("Content Atoms (Mission, FAQs, etc.)")
    
    if missing_fields:
        # Return 200 but with ok=False so frontend handles it gracefully
        return {
            "ok": False, 
            "error": "Missing required Brand DNA data.", 
            "missing": missing_fields
        }

    try:
        # 3. Generate Draft
        templates = load_templates()
        # Ensure we use the relaxed env builder if possible, but the check above is the main guard
        env = build_env() 
        
        count = monthly_count(client.id, datetime.now(TZ))
        tpl = select_template(templates, client, count)
        
        text_body = render_text(tpl["text"], client)
        media_url = client.attributes.get("hero_image_url") or FALLBACK_IMAGE_URL
        platforms = tpl.get("platforms", [])
        
        create_post_candidate(
            client_id=client.id,
            template_key=tpl["key"],
            text_body=text_body,
            media_url=media_url,
            platforms=platforms,
            slot_time=datetime.now(TZ),
            status="PENDING",
            metadata={"source": "manual_magic_button"}
        )
        
        return {"ok": True, "message": "Draft generated!"}
        
    except Exception as e:
        logger.exception("Manual generation failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/onboard")
def api_onboard_client(payload: Dict[str, str] = Body(...)):
    url = payload.get("url")
    if not url:
        raise HTTPException(400, "Missing URL")
        
    fc_key = os.getenv("FIRECRAWL_API_KEY")
    if not fc_key:
        raise HTTPException(500, "Server missing Firecrawl API Key")

    try:
        from ingest import run_ingestion
        data = run_ingestion(url, fc_key)
        
        # Save to DB
        client_id = save_ingested_client(data, url)
        return {"ok": True, "client_id": client_id}
        
    except Exception as e:
        logger.exception("Onboarding failed")
        raise HTTPException(500, str(e))

def save_ingested_client(data: dict, url: str) -> str:
    name = data.get('company_name', 'Unknown')
    # Create a safe slug
    slug = name.replace(" ", "_").lower()
    # Remove any non-alphanumeric chars to be safe
    slug = "".join(c for c in slug if c.isalnum() or c == "_")[:15]
    
    client_id = f"{slug}_{uuid.uuid4().hex[:4]}"
    
    # Map ALL BrandDNA fields to attributes
    attributes = {
        "website": url,
        "hero_image_url": f"https://www.google.com/s2/favicons?domain={url}&sz=256",
        
        # Core Identity
        "tone": data.get("tone"),
        "negative_constraints": data.get("negative_constraints"),
        "content_theme": data.get("content_theme"),
        "content_pillars": data.get("content_pillars") or [],
        "suggested_posts_per_week": data.get("suggested_posts_per_week"),
        
        # Content Atoms
        "tips": data.get("tips") or [],
        "myths": data.get("myths") or [],
        "content_atoms": data.get("content_atoms") or {},
        
        # Ecommerce Data
        "is_ecommerce": data.get("is_ecommerce"),
        "ecommerce_platform": data.get("ecommerce_platform"),
        "product_categories": data.get("product_categories") or [],
        "product_spotlights": data.get("product_spotlights") or [],
    }
    
    eng = get_main_engine()
    with eng.begin() as conn:
        conn.execute(text("""
            INSERT INTO clients (id, name, industry, city, attributes)
            VALUES (:id, :name, :ind, :city, :attr)
        """), {
            "id": client_id,
            "name": name,
            "ind": data.get("industry", "General"),
            "city": data.get("city", "Internet"),
            "attr": json.dumps(attributes)
        })
    return client_id


if __name__ == "__main__":
    # If running directly, just start the server
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)



