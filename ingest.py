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

    app = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
    
    # Using app.extract() with direct arguments (schema=, prompt=)
    try:
        data_list = app.extract(
            urls=[url],
            schema=BrandDNA.model_json_schema(),
            prompt="Extract the brand identity, tone, constraints, and details from this website."
        )
        
        # Check if data was returned and is correctly structured
        if not data_list or not isinstance(data_list, list) or not data_list[0].get("data"):
            raise ValueError("Extraction returned empty or poorly structured data from API.")

        # The result is nested under 'data' in the response list
        result: Dict[str, Any] = data_list[0].get("data") or {}
        
    except Exception as e:
        # Catching the previous errors (params, attribute) more generically
        print(f"❌ Extraction failed. Error: {e}")
        return

    # --- Verification and Save ---
    
    if not result.get("company_name"):
        print("❌ AI failed to identify the company name (a mandatory field). Aborting save.")
        return

    print("✅ Analysis Complete!")
    print(f"   Name: {result['company_name']}")
    print(f"   Industry: {result.get('industry', 'N/A')}")
    print(f"   Tone: {result.get('tone', 'N/A')}")
    print(f"   No-Go Zone: {result.get('negative_constraints', 'None specified')}")

    # --- 3. SAVE TO DATABASE ---
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
