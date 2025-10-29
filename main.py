#main.py

"""
SA Private Schools – Social Bot (Railway-ready Python service)

This file is a self-contained Python service you can deploy to Railway.
It includes:
  • FastAPI app (health, dry-run preview, manual publish)
  • APScheduler jobs (daily rotation + upgrade announcements watcher)
  • Read-only DB ingest (Postgres by default, configurable SQL)
  • Template engine (Jinja2) with sensible defaults written to templates.yaml on first run
  • Per-school monthly caps, de-duplication, and fair rotation
  • Pluggable publishers (Console + X/Twitter v2 skeleton). Add more easily (Facebook/Instagram/LinkedIn)
  • Local SQLite state (works on Railway Volume) so your main DB can stay READ ONLY

-----------------
Quick start on Railway
-----------------
1) Create a new service from this file.
2) Add a Railway Volume and mount to `/data` (or use a separate Postgres for state).
3) Set ENV VARS (see ENV VARS section below). Start command:
      uvicorn app:app --host 0.0.0.0 --port ${PORT}
4) Provide a READ-ONLY `DATABASE_URL` to your main school directory DB.
5) (Optional) Create a view `public.bot_schools_v` in your main DB matching the DDL at bottom, or set BOT_SCHOOLS_SQL to your schema.
6) Visit `/dry-run?count=5` to preview posts before enabling real publishing.

-----------------
Requirements (add to requirements.txt)
-----------------
fastapi==0.115.5
uvicorn[standard]==0.32.0
SQLAlchemy==2.0.36
psycopg2-binary==2.9.9
APScheduler==3.10.4
Jinja2==3.1.4
PyYAML==6.0.2
requests==2.32.3
python-dateutil==2.9.0.post0

Python 3.11+ recommended (uses zoneinfo).

-----------------
ENV VARS (Railway project variables)
-----------------
# Core
DATABASE_URL=postgresql+psycopg2://readonly:***@host:5432/dbname
BOT_STATE_DB_URL=sqlite:////data/bot.db   # or postgres URL for state if you prefer
BOT_SCHOOLS_SQL=SELECT * FROM public.bot_schools_v;   # override if your schema differs
TIMEZONE=Africa/Johannesburg
DRY_RUN=true   # when true, only logs posts (still stores state)

# Cadence
DAILY_SLOTS="09:00,13:00,17:30"   # local times
POSTS_PER_SLOT=1
PER_SCHOOL_MONTHLY_CAP=2
COOLDOWN_DAYS=14

# Platform toggles
ENABLE_X=false
# X/Twitter v2 (user-context OAuth token capable of posting)
X_BEARER_TOKEN=   # if set and ENABLE_X=true, will publish real tweets

# Branding and fallbacks
BRAND_NAME="SA Private Schools"
FALLBACK_IMAGE_URL=   # optional branded card URL if school has no media

"""
from __future__ import annotations
import os
import logging
import hashlib
import random
import string
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Dict, Any, Tuple

import yaml
import requests
from jinja2 import Environment, BaseLoader, StrictUndefined

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, Row
from sqlalchemy.exc import SQLAlchemyError

# ------------------
# Logging setup
# ------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("sa-private-schools-bot")

# ------------------
# Config helpers
# ------------------
TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Johannesburg"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
BRAND_NAME = os.getenv("BRAND_NAME", "SA Private Schools")

DATABASE_URL = os.getenv("DATABASE_URL")
STATE_DB_URL = os.getenv("BOT_STATE_DB_URL", "sqlite:////data/bot.db")
SCHOOLS_SQL = os.getenv("BOT_SCHOOLS_SQL", "SELECT * FROM public.bot_schools_v;")

DAILY_SLOTS = [s.strip() for s in os.getenv("DAILY_SLOTS", "09:00,13:00,17:30").split(",") if s.strip()]
POSTS_PER_SLOT = int(os.getenv("POSTS_PER_SLOT", "1"))
PER_SCHOOL_MONTHLY_CAP = int(os.getenv("PER_SCHOOL_MONTHLY_CAP", "2"))
COOLDOWN_DAYS = int(os.getenv("COOLDOWN_DAYS", "14"))

# Platform toggles
ENABLE_X = os.getenv("ENABLE_X", "false").lower() == "true"
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN")

# Fallback media
FALLBACK_IMAGE_URL = os.getenv("FALLBACK_IMAGE_URL")

# ------------------
# State DB (SQLite by default)
# ------------------
STATE_ENGINE: Engine = create_engine(STATE_DB_URL, future=True)

STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS published_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  template_key TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  external_id TEXT,
  posted_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pp_school_month ON published_posts (school_id, posted_at);

CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""

# ------------------
# Templates bootstrap
# ------------------
TEMPLATES_PATH = "/data/templates.yaml"

DEFAULT_TEMPLATES_YAML = """
post_templates:
  - key: general_spotlight_short
    platforms: [x, facebook, linkedin]
    text: "Featured private school in {{ city }}, {{ province }}: {{ name }} — {{ phases|join('/') }}. Fees {{ fees_min }}–{{ fees_max }} p.a. Enquire: {{ profile_url }}"
  - key: general_spotlight_alt
    platforms: [x, facebook, linkedin]
    text: "Discover {{ name }} ({{ phases|join('/') }}) in {{ city }}. Faith: {{ religion }}. Subjects: {{ subjects|slice(0,3)|join(', ') }}… Details & enquiries: {{ profile_url }}"
  - key: admissions_focus
    platforms: [x, facebook, linkedin]
    text: "Admissions at {{ name }}: {{ admissions_note }} Apply or enquire here: {{ admissions_url or profile_url }}"
  - key: value_highlight
    platforms: [x, facebook, linkedin]
    text: "Why parents choose {{ name }} in {{ city }}: {{ value_points|join(' • ') }}. Fees from {{ fees_min }} p.a. Learn more: {{ profile_url }}"
  - key: religion_subjects
    platforms: [x, facebook, linkedin]
    text: "{{ name }} • {{ religion }} ethos • Popular subjects: {{ subjects|slice(0,5)|join(', ') }}. See fees & admissions: {{ profile_url }}"
  - key: media_spotlight
    platforms: [x, facebook, linkedin]
    text: "Take a look at {{ name }} in {{ city }} — {{ media_caption }}. Explore the school: {{ profile_url }}"
  - key: upgrade_announcement
    platforms: [x, facebook, linkedin]
    text: "🎉 Welcome to our Featured family: {{ name }} in {{ city }}! Featured schools get priority placement and instant parent enquiries. See the profile: {{ profile_url }} #SAPrivateSchools"
  - key: open_day
    platforms: [x, facebook, linkedin]
    text: "Open day at {{ name }}{{ ' on ' + open_day if open_day else '' }}. Book your spot: {{ admissions_url or profile_url }}"
"""

# ------------------
# Data model
# ------------------
@dataclass
class School:
    id: str
    name: str
    city: str
    province: str
    area: Optional[str]
    phases: List[str]
    religion: Optional[str]
    fees_min: Optional[int]
    fees_max: Optional[int]
    admissions_url: Optional[str]
    profile_url: str
    subjects: List[str]
    featured: bool
    upgraded_at: Optional[datetime]
    x_handle: Optional[str]
    facebook_page_id: Optional[str]
    instagram_username: Optional[str]
    linkedin_url: Optional[str]
    logo_url: Optional[str]
    hero_image_url: Optional[str]
    media_approved: bool
    opt_out: bool
    admissions_note: Optional[str] = None
    value_points: List[str] = None
    media_caption: Optional[str] = None
    open_day: Optional[str] = None

# ------------------
# Util functions
# ------------------
def ensure_bootstrap() -> None:
    """Ensure state tables and default templates exist."""
    with STATE_ENGINE.begin() as conn:
        conn.exec_driver_sql(STATE_SCHEMA_SQL)
    if not os.path.exists(TEMPLATES_PATH):
        os.makedirs(os.path.dirname(TEMPLATES_PATH), exist_ok=True)
        with open(TEMPLATES_PATH, "w", encoding="utf-8") as f:
            f.write(DEFAULT_TEMPLATES_YAML)
        logger.info("Wrote default templates to %s", TEMPLATES_PATH)


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


def kget(key: str) -> Optional[str]:
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(text("SELECT v FROM kv WHERE k=:k"), {"k": key}).fetchone()
        return row[0] if row else None


def kset(key: str, val: str) -> None:
    with STATE_ENGINE.begin() as conn:
        conn.execute(text("INSERT INTO kv(k,v) VALUES(:k,:v) ON CONFLICT(k) DO UPDATE SET v=excluded.v"), {"k": key, "v": val})

# ------------------
# DB ingest (read-only main DB)
# ------------------
MAIN_ENGINE: Optional[Engine] = None


def get_main_engine() -> Engine:
    global MAIN_ENGINE
    if MAIN_ENGINE is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        MAIN_ENGINE = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
    return MAIN_ENGINE


def row_to_school(row: Row) -> School:
    def split_list(val: Optional[str]) -> List[str]:
        if not val:
            return []
        return [x.strip() for x in str(val).replace(";", ",").split(",") if x.strip()]

    return School(
        id=str(row["id"]),
        name=row["name"],
        city=row.get("city") or "",
        province=row.get("province") or "",
        area=row.get("area"),
        phases=split_list(row.get("phases")),
        religion=row.get("religion"),
        fees_min=(int(row["fees_min"]) if row.get("fees_min") is not None else None),
        fees_max=(int(row["fees_max"]) if row.get("fees_max") is not None else None),
        admissions_url=row.get("admissions_url"),
        profile_url=row.get("profile_url") or "",
        subjects=split_list(row.get("subjects")),
        featured=bool(row.get("featured") or row.get("upgraded") or row.get("is_featured")),
        upgraded_at=(row.get("upgraded_at") or row.get("featured_at")),
        x_handle=row.get("x_handle") or row.get("twitter_handle"),
        facebook_page_id=row.get("facebook_page_id"),
        instagram_username=row.get("instagram_username"),
        linkedin_url=row.get("linkedin_url"),
        logo_url=row.get("logo_url"),
        hero_image_url=row.get("hero_image_url"),
        media_approved=bool(row.get("media_approved") if row.get("media_approved") is not None else True),
        opt_out=bool(row.get("opt_out") if row.get("opt_out") is not None else False),
        admissions_note=row.get("admissions_note"),
        value_points=split_list(row.get("value_points")),
        media_caption=row.get("media_caption"),
        open_day=row.get("open_day"),
    )


def fetch_schools() -> List[School]:
    eng = get_main_engine()
    try:
        with eng.begin() as conn:
            rows = conn.execute(text(SCHOOLS_SQL)).mappings().all()
            schools = [row_to_school(r) for r in rows]
            return schools
    except SQLAlchemyError as e:
        logger.exception("Failed to fetch schools: %s", e)
        return []

# ------------------
# Template rendering
# ------------------

def build_env() -> Environment:
    env = Environment(loader=BaseLoader(), autoescape=False, undefined=StrictUndefined)
    env.filters["slice"] = lambda seq, n: list(seq)[:n]
    def join_filter(iterable, sep=", "):
        return sep.join([str(x) for x in iterable])
    env.filters["join"] = join_filter
    return env


def select_template(templates: Dict[str, Any], school: School, purpose: str = "rotation") -> Dict[str, Any]:
    """Choose a template. For rotation, avoid using upgrade_announcement."""
    candidates = [t for t in templates.values() if (purpose == "rotation" and t["key"] != "upgrade_announcement") or (purpose == "upgrade" and t["key"] == "upgrade_announcement")]
    # Deterministic-ish choice per day & school to reduce repeats
    seed = int(datetime.now(TZ).strftime("%Y%m%d")) + hash(school.id)
    random.seed(seed)
    return random.choice(candidates)


def render_text(tpl_text: str, school: School) -> str:
    env = build_env()
    ctx = {
        "name": school.name,
        "city": school.city,
        "province": school.province,
        "area": school.area or "",
        "phases": school.phases or [],
        "religion": school.religion or "",
        "fees_min": school.fees_min if school.fees_min is not None else "N/A",
        "fees_max": school.fees_max if school.fees_max is not None else "N/A",
        "admissions_url": school.admissions_url,
        "profile_url": school.profile_url,
        "subjects": school.subjects or [],
        "admissions_note": school.admissions_note or "Now enrolling – enquire today.",
        "value_points": school.value_points or ["Individual attention", "Strong academics", "Caring ethos"],
        "media_caption": school.media_caption or "campus life & facilities",
        "open_day": school.open_day,
    }
    template = env.from_string(tpl_text)
    return template.render(**ctx).strip()

# ------------------
# Publishers
# ------------------
class PublishResult(Dict[str, Any]):
    pass


class Publisher:
    platform: str = "base"

    def publish(self, text: str, media_url: Optional[str] = None) -> PublishResult:
        raise NotImplementedError


class ConsolePublisher(Publisher):
    platform = "console"

    def publish(self, text: str, media_url: Optional[str] = None) -> PublishResult:
        logger.info("[DRY] %s | %s%s", self.platform, text, f" [media={media_url}]" if media_url else "")
        return PublishResult({"platform": self.platform, "id": None, "text": text})


class XPublisher(Publisher):
    platform = "x"

    def __init__(self, bearer: Optional[str]):
        self.bearer = bearer

    def publish(self, text: str, media_url: Optional[str] = None) -> PublishResult:
        if DRY_RUN or not self.bearer:
            logger.info("[DRY] x | %s", text)
            return PublishResult({"platform": self.platform, "id": None, "text": text})
        # Note: This is a minimal v2 endpoint call. You must supply a valid user-context token.
        url = "https://api.twitter.com/2/tweets"
        headers = {"Authorization": f"Bearer {self.bearer}", "Content-Type": "application/json"}
        payload = {"text": text}
        # Media uploading is more involved (separate upload). Omitted in skeleton.
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"X publish failed: {r.status_code} {r.text}")
        data = r.json()
        tweet_id = data.get("data", {}).get("id")
        logger.info("Tweeted id=%s", tweet_id)
        return PublishResult({"platform": self.platform, "id": tweet_id, "text": text})


def build_publishers() -> List[Publisher]:
    pubs: List[Publisher] = []
    # Always include console logger for visibility
    pubs.append(ConsolePublisher())
    if ENABLE_X:
        pubs.append(XPublisher(X_BEARER_TOKEN))
    return pubs

# ------------------
# Business rules (caps, cooldowns, eligibility)
# ------------------

def already_posted_recently(school_id: str, cooldown_days: int = COOLDOWN_DAYS) -> bool:
    cutoff = datetime.now(TZ) - timedelta(days=cooldown_days)
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(text("SELECT 1 FROM published_posts WHERE school_id=:sid AND posted_at>=:cutoff LIMIT 1"), {"sid": school_id, "cutoff": cutoff}).fetchone()
        return bool(row)


def monthly_count(school_id: str, when: datetime) -> int:
    start, end = month_bounds(when)
    with STATE_ENGINE.begin() as conn:
        row = conn.execute(text("SELECT COUNT(*) FROM published_posts WHERE school_id=:sid AND posted_at>=:start AND posted_at<:end"), {"sid": school_id, "start": start, "end": end}).fetchone()
        return int(row[0]) if row else 0


def eligible_for_rotation(s: School) -> bool:
    return s.featured and not s.opt_out and s.media_approved and s.profile_url

# ------------------
# Core posting pipeline
# ------------------

def choose_school_for_slot(schools: List[School]) -> Optional[School]:
    """Fair rotation: filter eligible, remove cooldown, prioritise lowest monthly count, then random."""
    now = datetime.now(TZ)
    pool = [s for s in schools if eligible_for_rotation(s) and not already_posted_recently(s.id)]
    if not pool:
        return None
    scored = [(monthly_count(s.id, now), s) for s in pool]
    min_count = min(c for c, _ in scored)
    shortlist = [s for c, s in scored if c == min_count and monthly_count(s.id, now) < PER_SCHOOL_MONTHLY_CAP]
    if not shortlist:
        return None
    random.shuffle(shortlist)
    return shortlist[0]


def pick_media(s: School) -> Optional[str]:
    return s.hero_image_url or s.logo_url or FALLBACK_IMAGE_URL


def record_published(school_id: str, platform: str, template_key: str, text_body: str, external_id: Optional[str]) -> None:
    with STATE_ENGINE.begin() as conn:
        conn.execute(text(
            "INSERT INTO published_posts(school_id, platform, template_key, text_hash, external_id, posted_at) VALUES(:sid,:pf,:tk,:th,:eid,:ts)"
        ), {
            "sid": school_id,
            "pf": platform,
            "tk": template_key,
            "th": text_hash(text_body),
            "eid": external_id,
            "ts": datetime.now(TZ),
        })


def publish_once(s: School, purpose: str = "rotation") -> List[PublishResult]:
    templates = load_templates()
    tpl = select_template(templates, s, purpose)
    text_body = render_text(tpl["text"], s)
    media_url = pick_media(s)
    results: List[PublishResult] = []
    for pub in build_publishers():
        # Only post to platforms present in template's platform list, except console.
        if isinstance(pub, ConsolePublisher) or pub.platform in tpl.get("platforms", []):
            try:
                res = pub.publish(text_body, media_url)
                results.append(res)
                record_published(s.id, pub.platform, tpl["key"], text_body, res.get("id"))
            except Exception as e:
                logger.exception("Failed to publish to %s for school %s: %s", pub.platform, s.name, e)
    return results

# ------------------
# Schedules
# ------------------
SCHED = BackgroundScheduler(timezone=str(TZ))


def schedule_today_slots():
    now = datetime.now(TZ)
    for slot_str in DAILY_SLOTS:
        try:
            hh, mm = [int(x) for x in slot_str.split(":", 1)]
        except ValueError:
            logger.warning("Invalid DAILY_SLOTS entry: %s", slot_str)
            continue
        slot_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot_dt <= now:
            slot_dt += timedelta(days=1)  # next day if time has passed
        for i in range(max(1, POSTS_PER_SLOT)):
            jitter_minutes = random.randint(0, 25)  # avoid looking botty
            run_at = slot_dt + timedelta(minutes=jitter_minutes + i * 5)
            SCHED.add_job(run_rotation_post, DateTrigger(run_date=run_at))
            logger.info("Scheduled rotation post at %s", run_at)


def run_rotation_post():
    schools = fetch_schools()
    choice = choose_school_for_slot(schools)
    if not choice:
        logger.info("No eligible school for this slot.")
        return
    publish_once(choice, purpose="rotation")


def run_upgrade_watcher():
    """Every 5 minutes: detect new upgrades and announce immediately."""
    schools = [s for s in fetch_schools() if s.featured and not s.opt_out]
    if not schools:
        return
    last_seen_iso = kget("last_seen_upgrade_ts")
    last_seen = datetime.fromisoformat(last_seen_iso) if last_seen_iso else datetime.now(TZ) - timedelta(days=7)
    new_upgrades = [s for s in schools if s.upgraded_at and s.upgraded_at.replace(tzinfo=TZ) > last_seen]
    if not new_upgrades:
        return
    new_upgrades.sort(key=lambda s: s.upgraded_at)
    for s in new_upgrades:
        logger.info("Announcing new Featured school: %s", s.name)
        publish_once(s, purpose="upgrade")
        kset("last_seen_upgrade_ts", s.upgraded_at.replace(tzinfo=TZ).isoformat())

# ------------------
# FastAPI app
# ------------------
app = FastAPI(title="SA Private Schools – Social Bot")


@app.on_event("startup")
def on_startup():
    ensure_bootstrap()
    schedule_today_slots()
    # Rolling schedules
    SCHED.add_job(schedule_today_slots, "cron", hour=0, minute=5)  # schedule next day just after midnight
    SCHED.add_job(run_upgrade_watcher, "interval", minutes=5)
    SCHED.start()
    logger.info("Bot started. DRY_RUN=%s, TZ=%s", DRY_RUN, TZ)


@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(TZ).isoformat(), "dry_run": DRY_RUN}


@app.get("/dry-run")
def dry_run(count: int = Query(3, ge=1, le=20)):
    schools = [s for s in fetch_schools() if eligible_for_rotation(s)]
    if not schools:
        return JSONResponse({"posts": []})
    random.shuffle(schools)
    templates = load_templates()
    items = []
    for s in schools[:count]:
        tpl = select_template(templates, s, purpose="rotation")
        text_body = render_text(tpl["text"], s)
        items.append({
            "school": s.name,
            "template": tpl["key"],
            "text": text_body,
            "media": pick_media(s),
        })
    return {"posts": items}


@app.post("/publish/now")
def publish_now(school_id: str):
    schools = fetch_schools()
    match = next((s for s in schools if s.id == school_id), None)
    if not match:
        return JSONResponse(status_code=404, content={"error": "School not found"})
    results = publish_once(match, purpose="rotation")
    return {"published": results}


# ------------------
# Optional: SQL view DDL (run in your main DB, not here)
# ------------------
DDL_EXAMPLE = r"""
-- Recommended view: unify the fields the bot reads
CREATE OR REPLACE VIEW public.bot_schools_v AS
SELECT
  s.id,
  s.name,
  s.city,
  s.province,
  s.area,
  s.phases,                -- comma-separated list or array cast to text
  s.religion,
  s.fees_min,
  s.fees_max,
  s.admissions_url,
  s.profile_url,           -- your SA Private Schools profile link with UTM if desired
  s.subjects,              -- comma-separated list
  s.featured,              -- boolean: upgraded/featured
  s.upgraded_at,
  s.x_handle,
  s.facebook_page_id,
  s.instagram_username,
  s.linkedin_url,
  s.logo_url,
  s.hero_image_url,
  COALESCE(s.media_approved, TRUE) AS media_approved,
  COALESCE(s.opt_out, FALSE) AS opt_out,
  s.admissions_note,
  s.value_points,
  s.media_caption,
  s.open_day
FROM public.schools s
WHERE s.is_private = TRUE;  -- adjust to your schema
"""

# ------------------
# Notes & Next steps
# ------------------
"""
• To enable real posting to X, set ENABLE_X=true and X_BEARER_TOKEN to a valid user-context token.
• Add Facebook/Instagram/LinkedIn publishers using their SDKs/Graph API – the pipeline is already pluggable.
• If your schema differs, either create the view above or override BOT_SCHOOLS_SQL with your own SELECT.
• POPIA: Only content from approved fields is posted; use media_approved/opt_out to enforce per-school consent.
• Similar School Enquiries: not needed here; this bot only reads – no writes to your main DB.
• For image cards, consider a separate microservice or add Pillow-based card generation later.
"""
