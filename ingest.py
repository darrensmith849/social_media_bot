# ingest.py
import os
import uuid
import json
from typing import List, Optional
from pydantic import BaseModel, Field
from firecrawl import FirecrawlApp  # SDK v1
from sqlalchemy import create_engine, text

# --- CONFIGURATION ---
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL") 

# --- 1. DEFINE THE BRAND DNA SCHEMA ---
# This is the "form" we want the AI to fill out for us.
class BrandDNA(BaseModel):
    company_name: str = Field(description="The official name of the business")
    industry: str = Field(description="A short 2-3 word industry category (e.g., 'Family Dentistry', 'Craft Brewery')")
    city: str = Field(description="The primary city where they operate. Default to 'South Africa' if unclear.")
    tone: str = Field(description="The brand voice adjectives (e.g., 'Professional & Trustworthy', 'Fun & Edgy')")
    negative_constraints: str = Field(description="Topics or words this brand should strictly AVOID based on their vibe (e.g., 'Avoid slang', 'No medical advice').")
    tips: List[str] = Field(description="5 generic, helpful tips related to their industry for social media content.")
    myths: List[str] = Field(description="3 common myths about their industry that they can debunk.")
    hard_sell_offer: Optional[str] = Field(description="A short call-to-action phrase found on the site (e.g., 'Book your free consultation').")

# --- 2. THE INGESTION FUNCTION ---
def onboard_client(url: str):
    print(f"🕵️  Analyzing {url}...")
    
    app = FirecrawlApp(api_key=FIRECRAWL_API_KEY)
    
    # The Magic: Firecrawl scrapes AND analyzes in one step using our Pydantic schema
    data = app.scrape_url(url, {
        'extractorOptions': {
            'extractionSchema': BrandDNA.model_json_schema(),
            'mode': 'llm-extraction'
        }
    })

    # Firecrawl returns the data inside 'llm_extraction'
    result = data.get("llm_extraction")
    if not result:
        print("❌ Failed to extract data.")
        return

    print("✅ Analysis Complete!")
    print(f"   Name: {result['company_name']}")
    print(f"   Industry: {result['industry']}")
    print(f"   Tone: {result['tone']}")
    print(f"   No-Go Zone: {result['negative_constraints']}")

    # --- 3. SAVE TO DATABASE ---
    save_to_db(result, url)

def save_to_db(data: dict, url: str):
    # Create a clean ID (e.g., "Smile_Dental_a1b2")
    slug = data['company_name'].replace(" ", "_").lower()[:15]
    client_id = f"{slug}_{uuid.uuid4().hex[:4]}"

    # Pack the "Brand DNA" into the JSON attributes
    attributes = {
        "website": url,
        "tone": data['tone'],
        "negative_constraints": data['negative_constraints'],
        "tips": data['tips'],
        "myths": data['myths'],
        "offer_text": data['hard_sell_offer'],
        "media_approved": True, # Default to true for now
        "opt_out": False
    }

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        sql = text("""
            INSERT INTO clients (id, name, website, industry, city, attributes)
            VALUES (:id, :name, :website, :industry, :city, :attr)
        """)
        conn.execute(sql, {
            "id": client_id,
            "name": data['company_name'],
            "website": url,
            "industry": data['industry'],
            "city": data['city'],
            "attr": json.dumps(attributes)
        })
    
    print(f"💾 Saved client [{client_id}] to database.")

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    # Example Usage:
    target_url = input("Enter client website URL: ")
    onboard_client(target_url)
