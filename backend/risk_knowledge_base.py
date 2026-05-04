"""
Risk Knowledge Base — Azure Blob Storage backed correction store for Schedule Risk Manager.

Container: risk-corrections
Blob per project: {project_code}.json

Schema:
{
  "project_code": "HYD20",
  "last_updated": "2026-05-04T09:00:00Z",
  "corrections": [
    {
      "source_finding": "MEP Works duration compressed...",
      "category": "Duration Compression",
      "risk_name": "MEP duration below benchmark",
      "action": "keep",          # "keep" | "remove"
      "current_probability": 5,
      "current_schedule": 5
    }
  ]
}
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_CONTAINER = "risk-corrections"


def _get_client():
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        return None
    try:
        from azure.storage.blob import BlobServiceClient
        return BlobServiceClient.from_connection_string(conn_str)
    except Exception as e:
        logger.warning("Risk KB unavailable: %s", e)
        return None


def _ensure_container(service_client) -> None:
    try:
        service_client.create_container(_CONTAINER)
    except Exception:
        pass  # already exists


def _json_content_settings():
    from azure.storage.blob import ContentSettings
    return ContentSettings(content_type="application/json")


def save_risk_corrections(project_code: str, risks: list[dict]) -> bool:
    """
    Persist reviewed risk candidates as the knowledge base for project_code.
    Both kept and removed risks are stored so future runs can pre-apply corrections.
    """
    client = _get_client()
    if not client:
        logger.warning("AZURE_STORAGE_CONNECTION_STRING not set — risk KB save skipped")
        return False

    _ensure_container(client)

    corrections = []
    for r in risks:
        finding = (r.get("source_finding") or r.get("risk_name") or "").strip()
        if not finding:
            continue
        prob  = r.get("current_probability", 0)
        sched = r.get("current_schedule", 0)
        corrections.append({
            "source_finding":      finding,
            "category":            r.get("category", ""),
            "risk_name":           r.get("risk_name", ""),
            "action":              "remove" if r.get("_removed") else "keep",
            "current_probability": int(prob)  if str(prob).isdigit()  else 0,
            "current_schedule":    int(sched) if str(sched).isdigit() else 0,
        })

    payload = {
        "project_code": project_code.upper(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "corrections":  corrections,
    }

    blob_name = f"{project_code.upper()}.json"
    try:
        blob_client = client.get_blob_client(_CONTAINER, blob_name)
        blob_client.upload_blob(
            json.dumps(payload, indent=2),
            overwrite=True,
            content_settings=_json_content_settings(),
        )
        logger.info("Risk KB saved: %s (%d corrections)", blob_name, len(corrections))
        return True
    except Exception as e:
        logger.error("Risk KB save failed for %s: %s", project_code, e)
        return False


def get_risk_corrections(project_code: str) -> list[dict]:
    """Return correction list for project_code, or [] if none found."""
    client = _get_client()
    if not client:
        return []

    blob_name = f"{project_code.upper()}.json"
    try:
        blob_client = client.get_blob_client(_CONTAINER, blob_name)
        data = blob_client.download_blob().readall()
        payload = json.loads(data)
        corrections = payload.get("corrections", [])
        logger.info("Risk KB loaded: %s (%d corrections)", blob_name, len(corrections))
        return corrections
    except Exception:
        return []


def get_risk_kb_metadata(project_code: str) -> Optional[dict]:
    """Return {last_updated, count} for a project, or None if no KB exists."""
    client = _get_client()
    if not client:
        return None

    blob_name = f"{project_code.upper()}.json"
    try:
        blob_client = client.get_blob_client(_CONTAINER, blob_name)
        data = blob_client.download_blob().readall()
        payload = json.loads(data)
        return {
            "last_updated": payload.get("last_updated"),
            "count": len(payload.get("corrections", [])),
        }
    except Exception:
        return None
