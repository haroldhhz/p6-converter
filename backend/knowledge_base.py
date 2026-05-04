"""
Knowledge Base — Azure Blob Storage backed correction store.

Each project gets one JSON blob: {project_code}.json
Schema:
{
  "project_code": "MAA23",
  "last_updated": "2026-05-01T12:00:00Z",
  "corrections": [
    {
      "pdf_activity_name": "L1 Commissioning Start",
      "action": "keep",                        # "keep" | "remove"
      "correct_std_id": "L1CX-S",
      "correct_task_name": "Start L1 Installation & QA/QC (Red Tag)",
      "correct_status": "Not Started",
      "correct_cstr_date": "2026-07-31 00:00"
    }
  ]
}
"""

import json
import os
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_CONTAINER = "p6-corrections"


def _get_client():
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        return None
    try:
        from azure.storage.blob import BlobServiceClient
        return BlobServiceClient.from_connection_string(conn_str)
    except Exception as e:
        logger.warning(f"Knowledge base unavailable: {e}")
        return None


def _ensure_container(service_client) -> bool:
    try:
        service_client.create_container(_CONTAINER)
    except Exception:
        pass  # already exists
    return True


def save_corrections(project_code: str, activities: list[dict]) -> bool:
    """
    Persist planner-corrected activities as the knowledge base for project_code.

    `activities` is the frontend row list (each dict has task_code, task_name,
    status_code, cstr_date, act_start_date, act_end_date, pdf_activity_name, and
    optionally _removed=True for rows the planner deleted).
    """
    client = _get_client()
    if not client:
        logger.warning("AZURE_STORAGE_CONNECTION_STRING not set — KB save skipped")
        return False

    _ensure_container(client)

    corrections = []
    for a in activities:
        pdf_name = a.get("pdf_activity_name") or ""
        if not pdf_name:
            continue  # no PDF name → can't use as a future hint

        if a.get("_removed"):
            corrections.append({
                "pdf_activity_name": pdf_name,
                "action": "remove",
            })
        else:
            corrections.append({
                "pdf_activity_name": pdf_name,
                "action": "keep",
                "correct_std_id": a.get("task_code", ""),
                "correct_task_name": a.get("task_name", ""),
                "correct_status": a.get("status_code", ""),
                "correct_cstr_date": a.get("cstr_date", ""),
            })

    payload = {
        "project_code": project_code.upper(),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "corrections": corrections,
    }

    blob_name = f"{project_code.upper()}.json"
    try:
        blob_client = client.get_blob_client(_CONTAINER, blob_name)
        blob_client.upload_blob(
            json.dumps(payload, indent=2),
            overwrite=True,
            content_settings=_json_content_settings(),
        )
        logger.info(f"KB saved: {blob_name} ({len(corrections)} corrections)")
        return True
    except Exception as e:
        logger.error(f"KB save failed for {project_code}: {e}")
        return False


def get_corrections(project_code: str) -> list[dict]:
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
        logger.info(f"KB loaded: {blob_name} ({len(corrections)} corrections)")
        return corrections
    except Exception:
        return []  # blob not found or parse error — treat as empty KB


def get_kb_metadata(project_code: str) -> Optional[dict]:
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


def list_projects() -> list[str]:
    """Return list of project codes that have a KB entry."""
    client = _get_client()
    if not client:
        return []
    try:
        container_client = client.get_container_client(_CONTAINER)
        return [
            b.name.replace(".json", "")
            for b in container_client.list_blobs()
            if b.name.endswith(".json")
        ]
    except Exception:
        return []


def _json_content_settings():
    from azure.storage.blob import ContentSettings
    return ContentSettings(content_type="application/json")
