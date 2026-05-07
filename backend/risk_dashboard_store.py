"""
Risk Dashboard Data Store — Stores enriched risk records with location data.

Container: risk-dashboard-data
Blob per project: {project_code}/risks.json

Schema:
{
  "project_code": "PNQ24",
  "tranche": "T1",
  "assessment_date": "2026-04-15",
  "last_updated": "2026-05-05T12:00:00Z",
  "risks": [
    {
      "id": "S-001",
      "type": "S",
      "projectId": "PNQ24",
      "code": "PNQ",
      "tranche": "T1",
      "assessmentDate": "2026-04-15",
      "region": "APAC",
      "subRegion": "APAC India",
      "metro": "Pune",
      "isStandard": "No",
      "name": "Foundation delay due to monsoon",
      "category": "Weather",
      "probability": 5,
      "impact": 5,
      "evidence": "LPSS shows 82% schedule slip risk during Jun-Sep"
    }
  ]
}
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CONTAINER = "risk-dashboard-data"

# Load .env file explicitly so environment variables are available
# This ensures AZURE_STORAGE_CONNECTION_STRING is set regardless of working directory
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path)

# Standard Business Code Table (Step 2 of resolver)
STANDARD_CODES = {
    "HYD": {"metro": "Hyderabad", "region": "APAC", "subRegion": "APAC India"},
    "KUL": {"metro": "Kuala Lumpur", "region": "APAC", "subRegion": "APAC South East Asia"},
    "MEL": {"metro": "Melbourne", "region": "APAC", "subRegion": "APAC Australia New Zealand"},
    "TPE": {"metro": "Taipei", "region": "APAC", "subRegion": "APAC North East Asia"},
    "TYO": {"metro": "Tokyo", "region": "APAC", "subRegion": "APAC North East Asia"},
    "SEL": {"metro": "Seoul", "region": "APAC", "subRegion": "APAC North East Asia"},
    "SYD": {"metro": "Sydney", "region": "APAC", "subRegion": "APAC Australia New Zealand"},
    "OSA": {"metro": "Osaka", "region": "APAC", "subRegion": "APAC North East Asia"},
    "JHB": {"metro": "Johor Bahru", "region": "APAC", "subRegion": "APAC South East Asia"},
    "JNB": {"metro": "Johannesburg", "region": "EMEA", "subRegion": "EMEA Middle East Africa & Emerging"},
}

# Approved regions and sub-regions
APPROVED_REGIONS = ["APAC", "EMEA", "AMER"]
APPROVED_SUBREGIONS = {
    "AMER": [
        "AMER US Canada & LATAM",
        "AMER US Central",
        "AMER US East",
        "AMER US West",
    ],
    "APAC": [
        "APAC Australia New Zealand",
        "APAC India",
        "APAC North East Asia",
        "APAC South East Asia",
    ],
    "EMEA": [
        "EMEA Ireland & Nordics",
        "EMEA Middle East Africa & Emerging",
        "EMEA South & East",
        "EMEA UK",
        "EMEA West & Central",
    ],
}


def _get_client():
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        return None
    try:
        from azure.storage.blob import BlobServiceClient
        return BlobServiceClient.from_connection_string(conn_str)
    except Exception as e:
        logger.warning("Risk dashboard store unavailable: %s", e)
        return None


def _ensure_container(service_client) -> bool:
    try:
        service_client.create_container(_CONTAINER)
    except Exception:
        pass  # already exists
    return True


def _json_content_settings():
    from azure.storage.blob import ContentSettings
    return ContentSettings(content_type="application/json")


def extract_code(project_name: str) -> tuple[str, str]:
    """
    Extract ProjectID and Code from project name.
    Example: "PNQ24 T1-2026.02" -> ("PNQ24", "PNQ")
    """
    import re
    # Remove tranche and date suffix
    cleaned = re.sub(r'\s*T?\d+[-/].*$', '', project_name.strip())
    # Extract 3-letter code (first 3 alpha chars)
    match = re.match(r'^([A-Za-z]{3})', cleaned)
    code = match.group(1).upper() if match else ""
    project_id = cleaned.upper()
    return project_id, code


def resolve_location(project_name: str) -> dict:
    """
    Resolve project name to location data using the 4-step resolver:
    1. Extract ProjectID and Code
    2. Match against Standard Business Code table (HIGHEST priority)
    3. IATA fallback (for non-standard codes)
    4. Validation gate
    """
    project_id, code = extract_code(project_name)
    
    # Step 2: Standard Business Code match (highest priority)
    if code in STANDARD_CODES:
        loc = STANDARD_CODES[code]
        return {
            "projectId": project_id,
            "code": code,
            "metro": loc["metro"],
            "region": loc["region"],
            "subRegion": loc["subRegion"],
            "isStandard": "Yes",
        }
    
    # Step 3: IATA fallback (return raw code as metro for now, will be enriched by AI)
    # The AI resolver will be called via OpenAI for non-standard codes
    return {
        "projectId": project_id,
        "code": code,
        "metro": "",
        "region": "",
        "subRegion": "",
        "isStandard": "No",
    }


def enrich_risk_with_location(risk: dict, location: dict) -> dict:
    """Add location fields to a risk record."""
    return {
        **risk,
        "projectId": location.get("projectId", ""),
        "code": location.get("code", ""),
        "metro": location.get("metro", ""),
        "region": location.get("region", ""),
        "subRegion": location.get("subRegion", ""),
        "isStandard": location.get("isStandard", "No"),
    }


def save_enriched_risks(project_code: str, tranche: str, assessment_date: str, risks: list[dict]) -> bool:
    """
    Persist enriched risk records with location data for the dashboard.
    """
    client = _get_client()
    if not client:
        logger.warning("AZURE_STORAGE_CONNECTION_STRING not set — risk dashboard save skipped")
        return False

    _ensure_container(client)

    payload = {
        "project_code": project_code.upper(),
        "tranche": tranche,
        "assessment_date": assessment_date,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "risks": risks,
    }

    blob_name = f"{project_code.upper()}/risks.json"
    try:
        blob_client = client.get_blob_client(_CONTAINER, blob_name)
        blob_client.upload_blob(
            json.dumps(payload, indent=2),
            overwrite=True,
            content_settings=_json_content_settings(),
        )
        logger.info("Saved %d enriched risks for %s", len(risks), project_code)
        return True
    except Exception as e:
        logger.error("Failed to save enriched risks for %s: %s", project_code, e)
        return False


def _normalize_risk(risk: dict, project_code: str = "", assessment_date: str = "") -> dict:
    """
    Normalize risk record fields for consistent API response.
    Maps Risk Manager fields (current_probability, current_schedule) to dashboard fields (probability, impact).
    Also attaches project_code and assessment_date from parent payload if available.
    """
    normalized = {**risk}
    
    # Map Risk Manager fields to dashboard standard fields
    if "current_probability" in normalized and "probability" not in normalized:
        normalized["probability"] = normalized["current_probability"]
    if "current_schedule" in normalized and "impact" not in normalized:
        normalized["impact"] = normalized["current_schedule"]
    
    # Attach project_code and assessment_date from parent payload
    if project_code and "projectId" not in normalized:
        normalized["projectId"] = project_code
    if assessment_date and "assessmentDate" not in normalized:
        normalized["assessmentDate"] = assessment_date
    
    return normalized


def get_enriched_risks(project_code: Optional[str] = None) -> list[dict]:
    """
    Retrieve all enriched risk records.
    If project_code is provided, returns risks for that project only.
    """
    client = _get_client()
    if not client:
        logger.warning("AZURE_STORAGE_CONNECTION_STRING not set — risk dashboard read skipped")
        return []

    container_client = client.get_container_client(_CONTAINER)
    all_risks = []

    try:
        blobs = container_client.list_blobs(name_starts_with=project_code.upper() + "/" if project_code else "")
        for blob in blobs:
            if not blob.name.endswith("/risks.json"):
                continue
            try:
                blob_client = container_client.get_blob_client(blob.name)
                data = json.loads(blob_client.download_blob().readall())
                # Extract project_code and assessment_date from parent payload
                parent_project = data.get("project_code", "")
                parent_date = data.get("assessment_date", "")
                # Normalize each risk record
                for risk in data.get("risks", []):
                    all_risks.append(_normalize_risk(risk, parent_project, parent_date))
            except Exception as e:
                logger.error("Failed to read blob %s: %s", blob.name, e)
    except Exception as e:
        logger.error("Failed to list blobs in %s: %s", _CONTAINER, e)

    return all_risks


def get_all_risks_for_dashboard() -> dict:
    """
    Get all risks for the dashboard with aggregations.
    Returns:
    {
        "risks": [...],
        "aggregates": {
            "byRegion": [...],
            "bySubRegion": [...],
            "byMetro": [...]
        },
        "filters": {
            "regions": [...],
            "subRegions": [...],
            "metros": [...]
        }
    }
    """
    risks = get_enriched_risks()
    
    # Compute aggregations
    by_region = {}
    by_subregion = {}
    by_metro = {}
    
    for risk in risks:
        region = risk.get("region", "Unknown")
        subregion = risk.get("subRegion", "Unknown")
        metro = risk.get("metro", "Unknown")
        
        # By Region
        if region not in by_region:
            by_region[region] = {"name": region, "risks": [], "projects": set()}
        by_region[region]["risks"].append(risk)
        by_region[region]["projects"].add(risk.get("projectId", ""))
        
        # By SubRegion
        if subregion not in by_subregion:
            by_subregion[subregion] = {"name": subregion, "region": region, "risks": [], "projects": set()}
        by_subregion[subregion]["risks"].append(risk)
        by_subregion[subregion]["projects"].add(risk.get("projectId", ""))
        
        # By Metro
        if metro not in by_metro:
            by_metro[metro] = {"name": metro, "subRegion": subregion, "region": region, "risks": [], "projects": set()}
        by_metro[metro]["risks"].append(risk)
        by_metro[metro]["projects"].add(risk.get("projectId", ""))
    
    def compute_agg(data):
        result = []
        for item in data.values():
            risks_list = item["risks"]
            if not risks_list:
                continue
            probs = [r.get("probability", 0) or 0 for r in risks_list]
            impacts = [r.get("impact", 0) or 0 for r in risks_list]
            scores = [p * i for p, i in zip(probs, impacts)]
            
            result.append({
                "name": item["name"],
                "riskCount": len(risks_list),
                "projectCount": len(item["projects"]),
                "projects": list(item["projects"]),
                "avgProbability": sum(probs) / len(probs) if probs else 0,
                "avgImpact": sum(impacts) / len(impacts) if impacts else 0,
                "avgScore": sum(scores) / len(scores) / 6 if scores else 0,
                "criticalCount": sum(1 for p, i in zip(probs, impacts) if p >= 5 or i >= 5),
            })
        return sorted(result, key=lambda x: x["riskCount"], reverse=True)
    
    return {
        "risks": risks,
        "aggregates": {
            "byRegion": compute_agg(by_region),
            "bySubRegion": compute_agg(by_subregion),
            "byMetro": compute_agg(by_metro),
        },
        "filters": {
            "regions": sorted(set(r.get("region", "") for r in risks if r.get("region"))),
            "subRegions": sorted(set(r.get("subRegion", "") for r in risks if r.get("subRegion"))),
            "metros": sorted(set(r.get("metro", "") for r in risks if r.get("metro"))),
        },
    }


def get_filtered_risks(region: Optional[str] = None, sub_region: Optional[str] = None, 
                       metro: Optional[str] = None, risk_type: Optional[str] = None) -> list[dict]:
    """
    Get filtered risks based on location hierarchy.
    """
    all_risks = get_enriched_risks()
    
    filtered = all_risks
    if region:
        filtered = [r for r in filtered if r.get("region") == region]
    if sub_region:
        filtered = [r for r in filtered if r.get("subRegion") == sub_region]
    if metro:
        filtered = [r for r in filtered if r.get("metro") == metro]
    if risk_type:
        filtered = [r for r in filtered if r.get("type") == risk_type]
    
    return filtered
