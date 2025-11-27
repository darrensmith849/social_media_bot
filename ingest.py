# ingest.py

from dotenv import load_dotenv
load_dotenv()                   

import os
import uuid
import json 
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from firecrawl import FirecrawlApp
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError 

# --- CONFIGURATION ---
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") 

# --- 1. DEFINE THE BRAND DNA SCHEMA ---
# NOTE: List fields (tips, myths) are Optional with default_factory=list
# so extraction doesn't die if the AI can't find enough items.

class ContentAtoms(BaseModel):
    """Reusable content atoms we can recombine into posts."""

    story_mission: Optional[str] = Field(
        default=None,
        description="1–2 sentence brand story or mission that can be reused in intros.",
    )
    services_benefits: Optional[List[str]] = Field(
        default_factory=list,
        description="Bullet-friendly list of services and their key benefits.",
    )
    faqs: Optional[List[str]] = Field(
        default_factory=list,
        description="Short FAQ-style lines we can turn into Q&A posts.",
    )
    stats: Optional[List[str]] = Field(
        default_factory=list,
        description="Proof points, numbers, or stats we can reuse across posts.",
    )
    offers: Optional[List[str]] = Field(
        default_factory=list,
        description="Current offers, promos, or hooks that work well for hard-sell posts.",
    )


class ProductSpotlight(BaseModel):
    """Lightweight product representation for ecommerce content."""

    name: str = Field(description="Name of the product.")
    short_benefit: str = Field(
        description="Short benefit-driven sentence highlighting why this product matters."
    )
    url: Optional[str] = Field(
        default=None,
        description="URL of the product page.",
    )
    image_url: Optional[str] = Field(
        default=None,
        description="URL of a representative product image.",
    )
    price_band: Optional[str] = Field(
        default=None,
        description="Optional price band label (e.g., 'budget', 'mid-range', 'premium').",
    )
    category: Optional[str] = Field(
        default=None,
        description="High-level category/collection this product belongs to.",
    )


class BrandDNA(BaseModel):
    company_name: str = Field(description="The official name of the business.")
    industry: str = Field(
        description="A short 2–3 word industry category (e.g., 'Family Dentistry', 'Craft Brewery')."
    )
    city: str = Field(
        description="The primary city where they operate. Default to 'South Africa' if unclear."
    )
    tone: str = Field(
        description="The brand voice adjectives (e.g., 'Professional & Trustworthy', 'Fun & Edgy')."
    )

    # Core constraints & reusable ideas
    negative_constraints: str = Field(
        description=(
            "Topics or phrases this brand should strictly AVOID based on their vibe "
            "(e.g., 'Avoid slang', 'No medical advice')."
        )
    )
    tips: Optional[List[str]] = Field(
        default_factory=list,
        description="5 generic, helpful tips related to their industry for social media content.",
    )
    myths: Optional[List[str]] = Field(
        default_factory=list,
        description="3 common myths about their industry that they can debunk.",
    )
    hard_sell_offer: Optional[str] = Field(
        default=None,
        description=(
            "A short, punchy hard-sell offer phrase found on the site "
            "(e.g., 'Book your free consultation')."
        ),
    )

    # High-level content theme & pillars + suggested cadence
    content_theme: Optional[str] = Field(
        default=None,
        description=(
            "A short phrase describing the core content theme "
            "(e.g., 'Empowering busy parents to stay fit')."
        ),
    )
    content_pillars: Optional[List[str]] = Field(
        default_factory=list,
        description="3–5 content pillars (e.g., 'Education', 'Testimonials', 'Behind the scenes', 'Offers').",
    )
    suggested_posts_per_week: Optional[int] = Field(
        default=None,
        description="Suggested posting frequency per week based on brand size and activity.",
    )

    # Aggregated content atoms (from page-level content)
    content_atoms: Optional[ContentAtoms] = Field(
        default=None,
        description=(
            "Reusable content atoms (story, services, FAQs, stats, offers) aggregated "
            "from the most important pages."
        ),
    )

    # Ecommerce-specific signals
    is_ecommerce: Optional[bool] = Field(
        default=None,
        description="True if this is primarily an online shop / ecommerce brand.",
    )
    ecommerce_platform: Optional[str] = Field(
        default=None,
        description="Detected ecommerce platform, e.g. 'Shopify', 'WooCommerce', 'Wix', 'Custom', etc.",
    )
    product_categories: Optional[List[str]] = Field(
        default_factory=list,
        description="High-level product categories / collections suitable for softer spotlight posts.",
    )
    product_spotlights: Optional[List[ProductSpotlight]] = Field(
        default_factory=list,
        description=(
            "A small pool of spotlight products (name, benefit, URL, image, price band) "
            "for hard-sell or promo content."
        ),
    )



# --- 2. THE INGESTION FUNCTION ---

def build_extraction_urls(root_url: str) -> List[str]:
    """
    Build a small, opinionated list of URLs to feed into Firecrawl.extract.

    Strategy:
    - Always include the homepage.
    - Include high-signal static pages: about, services, pricing, contact, team.
    - Include common ecommerce slugs if present (shop, products, collections, store).
    - Optionally include a wildcard URL so Firecrawl can explore navigation menus.
    - De-duplicate and cap to a sensible number of pages (5–10).
    """
    base = root_url.rstrip("/")

    candidates = [
        # Core
        base,
        # Wildcard for Firecrawl to follow internal links (acts like "navigation aware" crawling)
        f"{base}/*",
        # Brand/credibility pages
        f"{base}/about",
        f"{base}/about-us",
        f"{base}/who-we-are",
        # Services / benefits
        f"{base}/services",
        f"{base}/our-services",
        f"{base}/what-we-do",
        # Team / trust
        f"{base}/team",
        f"{base}/meet-the-team",
        # Pricing / offer clarity
        f"{base}/pricing",
        f"{base}/plans",
        # Contact / conversion
        f"{base}/contact",
        f"{base}/contact-us",
        # Ecommerce-oriented paths
        f"{base}/shop",
        f"{base}/store",
        f"{base}/products",
        f"{base}/collections",
        f"{base}/catalog",
    ]

    # De-duplicate while preserving order and cap length
    seen: set[str] = set()
    urls: List[str] = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            urls.append(u)
        if len(urls) >= 10:
            break

    return urls



def onboard_client(url: str):
    print(f"🕵️  Analyzing {url}...")
    
    if not FIRECRAWL_API_KEY:
        print("❌ Error: FIRECRAWL_API_KEY not found in .env file.")
        return

    # Use the current Firecrawl SDK class
    firecrawl = Firecrawl(api_key=FIRECRAWL_API_KEY)

    # Build a small set of high-signal URLs to extract from
    urls_to_extract = build_extraction_urls(url)
    print("🌐 Extracting from pages:")
    for u in urls_to_extract:
        print(f"   - {u}")

    try:
        # Call Firecrawl Extract with your BrandDNA schema over multiple pages.
        # The current SDK expects: extract(urls: List[str], options: Dict[str, Any])
        res = firecrawl.extract(
            urls_to_extract,
            {
                "prompt": (
                    "Extract the brand identity, tone, constraints, ecommerce details, "
                    "and reusable content atoms from this website."
                ),
                "schema": BrandDNA.model_json_schema(),
            },
        )

        # Normalise the response into a list of results
        if isinstance(res, dict) and isinstance(res.get("data"), list):
            data_list = res["data"]
        elif isinstance(res, list):
            # Some deployments return a bare list already
            data_list = res
        else:
            raise ValueError(f"Unexpected extract() response type: {type(res)!r}")

        if not data_list:
            raise ValueError("Extraction returned no data from Firecrawl.")

        # Typical item shape: {"url": "...", "data": {...BrandDNA...}, ...}
        first = data_list[0]
        payload = first.get("data") or first.get("structured") or first

        if not isinstance(payload, dict):
            raise ValueError("First extract() item did not contain a structured 'data' dict.")

        result: Dict[str, Any] = payload

    except Exception as e:
        # Catch Firecrawl and parsing issues and fail cleanly
        print(f"❌ Extraction failed. Error: {e}")
        return


    # --- Verification and Save ---
    if not result.get("company_name"):
        print("❌ AI failed to identify the company name (a mandatory field). Aborting save.")
        print("   Full extracted payload (truncated):")
        print(str(result)[:1000])
        return

    # Attach the pages we actually used for extraction so we can inspect later
    result["source_pages"] = urls_to_extract

    print("✅ Analysis Complete!")
    print(f"   Name: {result['company_name']}")
    print(f"   Industry: {result.get('industry', 'N/A')}")
    print(f"   Tone: {result.get('tone', 'N/A')}")
    print(f"   No-Go Zone: {result.get('negative_constraints', 'None specified')}")
    print("   Source pages used:")
    for u in urls_to_extract:
        print(f"   - {u}")

    # --- SAVE TO DATABASE ---
    save_to_db(result, url)


def save_to_db(data: dict, url: str):
    # Create a clean ID (e.g., "smile_dental_a1b2")
    company_name = data.get('company_name', 'unknown')
    slug = company_name.replace(" ", "_").lower()[:15]
    client_id = f"{slug}_{uuid.uuid4().hex[:4]}"

    # Pack the "Brand DNA" into the JSON attributes (use .get() for safety)
    attributes = {
        "website": url,
        "tone": data.get("tone"),
        "negative_constraints": data.get("negative_constraints"),
        "tips": data.get("tips"),
        "myths": data.get("myths"),
        "hard_sell_offer": data.get("hard_sell_offer"),
        "media_approved": True,
        "opt_out": False,

        # Pages we actually fed into / extracted from
        "source_pages": data.get("source_pages") or [],

        # Content brain: theme, pillars, cadence
        "content_theme": data.get("content_theme"),
        "content_pillars": data.get("content_pillars") or [],
        "suggested_posts_per_week": data.get("suggested_posts_per_week"),

        # Aggregated content atoms
        "content_atoms": data.get("content_atoms") or {},

        # Ecommerce flags & lightweight catalog
        "is_ecommerce": data.get("is_ecommerce"),
        "ecommerce_platform": data.get("ecommerce_platform"),
        "product_categories": data.get("product_categories") or [],
        "product_spotlights": data.get("product_spotlights") or [],
    }




    if not DATABASE_URL:
        print("❌ ERROR: DATABASE_URL is not set. Cannot save to database.")
        return
        
    try:
        engine = create_engine(DATABASE_URL)
        with engine.begin() as conn:
            sql = text("""
                INSERT INTO clients (id, name, website, industry, city, attributes)
                VALUES (:id, :name, :website, :industry, :city, :attr)
            """)
            conn.execute(sql, {
                "id": client_id,
                "name": company_name,
                "website": url,
                "industry": data.get('industry', 'General'),
                "city": data.get('city', 'South Africa'),
                "attr": json.dumps(attributes)
            })
        
        print(f"💾 Saved client [{client_id}] to database.")
    except SQLAlchemyError as e:
        print(f"❌ DATABASE ERROR: Failed to connect or execute SQL. Did you run database_setup.sql? Error: {e}")


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    # Example Usage:
    target_url = input("Enter client website URL: ")
    onboard_client(target_url)