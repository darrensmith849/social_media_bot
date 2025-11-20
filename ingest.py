# ingest.py

from dotenv import load_dotenv
load_dotenv(override=True)

import os
import uuid
import json
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from firecrawl import Firecrawl
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError 

# --- CONFIGURATION ---
from sqlalchemy.engine import make_url  # add this near the top with other imports

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")

_raw_db_url = (os.getenv("DATABASE_URL") or "").strip()
if _raw_db_url:
    try:
        url_obj = make_url(_raw_db_url)
        db_name = (url_obj.database or "").replace("\n", "").replace("\r", "").strip()
        url_obj = url_obj.set(database=db_name)
        DATABASE_URL = str(url_obj)
        print(f"Using DATABASE_URL={DATABASE_URL!r} (database={db_name!r})")
    except Exception as e:
        print(f"WARNING: Failed to parse DATABASE_URL {_raw_db_url!r}: {e}")
        DATABASE_URL = _raw_db_url
else:
    DATABASE_URL = None
    print("WARNING: DATABASE_URL is not set in ingest.py")

# --- 1. DEFINE THE BRAND DNA SCHEMA ---
# NOTE: List fields (tips, myths) are now Optional with default_factory=list.
# This prevents the extraction from failing if the AI can't find enough content.
class BrandDNA(BaseModel):
    company_name: str = Field(description="The official name of the business")
    industry: str = Field(description="A short 2-3 word industry category (e.g., 'Family Dentistry', 'Craft Brewery')")
    city: str = Field(description="The primary city where they operate. Default to 'South Africa' if unclear.")
    tone: str = Field(description="The brand voice adjectives (e.g., 'Professional & Trustworthy', 'Fun & Edgy')")
    negative_constraints: str = Field(description="Topics or words this brand should strictly AVOID based on their vibe (e.g., 'Avoid slang', 'No medical advice').")
    tips: Optional[List[str]] = Field(default_factory=list, description="5 generic, helpful tips related to their industry for social media content.")
    myths: Optional[List[str]] = Field(default_factory=list, description="3 common myths about their industry that they can debunk.")
    hard_sell_offer: Optional[str] = Field(description="A short call-to-action phrase found on the site (e.g., 'Book your free consultation').")

# --- 2. THE INGESTION FUNCTION ---
def onboard_client(url: str):
    print(f"🕵️  Analyzing {url}...")
    
    if not FIRECRAWL_API_KEY:
        print("❌ Error: FIRECRAWL_API_KEY not found in .env file.")
        return

    # Use the current Firecrawl SDK class
    firecrawl = Firecrawl(api_key=FIRECRAWL_API_KEY)

    try:
        # Call Firecrawl Extract with your BrandDNA schema
        res = firecrawl.extract(
            urls=[url],
            prompt="Extract the brand identity, tone, constraints, and details from this website.",
            schema=BrandDNA.model_json_schema(),
        )

        # --- Normalise the response into a plain dict we can work with ---

        # Many SDK versions return a response object with a .data attribute
        payload = getattr(res, "data", None) or res

        result: Dict[str, Any] = {}

        # Case 1: payload is already the BrandDNA-ish dict
        if isinstance(payload, dict):
            # If it already looks like our schema (company_name etc.), just use it
            if "company_name" in payload or "industry" in payload:
                result = payload
            # Case 2: { "success": true, "data": { ... } }
            elif isinstance(payload.get("data"), dict):
                result = payload["data"]
            # Case 3: { "results": [ { "data": {...} } ] }
            elif isinstance(payload.get("results"), list) and payload["results"]:
                first = payload["results"][0]
                if isinstance(first, dict):
                    if isinstance(first.get("data"), dict):
                        result = first["data"]
                    else:
                        result = first

        # Case 4: some older shapes return a list
        elif isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                if isinstance(first.get("data"), dict):
                    result = first["data"]
                else:
                    result = first

        # Final sanity check: if we still don't have something usable, stop gracefully
        if not isinstance(result, dict) or not result:
            print("❌ Extraction returned empty or poorly structured data from API.")
            print("   Raw Firecrawl response (truncated):")
            print(str(res)[:1000])
            return

    except Exception as e:
        print(f"❌ Extraction failed. Error: {e}")
        return

    # --- Verification and Save ---
    if not result.get("company_name"):
        print("❌ AI failed to identify the company name (a mandatory field). Aborting save.")
        print("   Full extracted payload (truncated):")
        print(str(result)[:1000])
        return

    print("✅ Analysis Complete!")
    print(f"   Name: {result['company_name']}")
    print(f"   Industry: {result.get('industry', 'N/A')}")
    print(f"   Tone: {result.get('tone', 'N/A')}")
    print(f"   No-Go Zone: {result.get('negative_constraints', 'None specified')}")

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
        "tone": data.get('tone'),
        "negative_constraints": data.get('negative_constraints'),
        "tips": data.get('tips'),
        "myths": data.get('myths'),
        "hard_sell_offer": data.get('hard_sell_offer'), 
        "media_approved": True, 
        "opt_out": False
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
