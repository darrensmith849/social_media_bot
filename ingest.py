import os
import uuid
import json 
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from firecrawl import FirecrawlApp
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError 

class ContentAtoms(BaseModel):
    story_mission: Optional[str] = Field(default=None)
    services_benefits: Optional[List[str]] = Field(default_factory=list)
    faqs: Optional[List[str]] = Field(default_factory=list)
    stats: Optional[List[str]] = Field(default_factory=list)
    offers: Optional[List[str]] = Field(default_factory=list)

class ProductSpotlight(BaseModel):
    name: str
    short_benefit: str
    url: Optional[str] = None
    image_url: Optional[str] = None
    price_band: Optional[str] = None
    category: Optional[str] = None

class BrandDNA(BaseModel):
    company_name: str
    industry: str
    city: str
    tone: str
    negative_constraints: str
    tips: Optional[List[str]] = Field(default_factory=list)
    myths: Optional[List[str]] = Field(default_factory=list)
    hard_sell_offer: Optional[str] = None
    content_theme: Optional[str] = None
    content_pillars: Optional[List[str]] = Field(default_factory=list)
    suggested_posts_per_week: Optional[int] = None
    content_atoms: Optional[ContentAtoms] = Field(default=None)
    is_ecommerce: Optional[bool] = None
    ecommerce_platform: Optional[str] = None
    product_categories: Optional[List[str]] = Field(default_factory=list)
    product_spotlights: Optional[List[ProductSpotlight]] = Field(default_factory=list)

def build_extraction_urls(root_url: str) -> List[str]:
    base = root_url.rstrip("/")
    candidates = [
        base, f"{base}/*", f"{base}/about", f"{base}/services", 
        f"{base}/pricing", f"{base}/contact"
    ]
    return list(dict.fromkeys(candidates))[:6] # Dedup and limit

def run_ingestion(url: str, api_key: str) -> Dict[str, Any]:
    """Scrapes the site and returns the Brand DNA dict."""
    print(f"🕵️ Analyzing {url}...")
    app = FirecrawlApp(api_key=api_key)
    
    urls = build_extraction_urls(url)
    
    try:
        # Pass arguments as keywords
        response = app.extract(
            urls,
            prompt="Extract brand identity, tone, and content atoms.",
            schema=BrandDNA.model_json_schema()
        )
        
        # --- ROBUST RESPONSE HANDLING ---
        # 1. Unwrap the Object if it has a .data attribute (SDK v1+)
        if hasattr(response, "data"):
            payload = response.data
        else:
            payload = response

        # 2. If it's a list (batch result), take the first item
        if isinstance(payload, list):
            if not payload:
                raise RuntimeError("Empty extraction result")
            payload = payload[0]

        # 3. If it's a Pydantic model (not a dict), convert it
        if hasattr(payload, "model_dump"):
            payload = payload.model_dump()
        elif hasattr(payload, "dict"):
            payload = payload.dict()
            
        # 4. Final safety check: ensure it is a dictionary
        if not isinstance(payload, dict):
             # Last ditch: try __dict__ if it's a generic object
             if hasattr(payload, "__dict__"):
                 payload = payload.__dict__
        
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected response format: {type(payload)}")

        # 5. Unwrap any nested 'data' or 'structured' keys inside the dict
        payload = payload.get("data") or payload.get("structured") or payload

        # Add source for reference
        if isinstance(payload, dict):
            payload["source_pages"] = urls
            
        return payload

    except Exception as e:
        raise RuntimeError(f"Firecrawl extraction failed: {str(e)}")