"""
Azure AI Client — Document Intelligence + OpenAI wrapper.
Handles PDF parsing, date extraction, and intelligent Activity ID matching.
"""

import os
import re
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Optional, Any, Union
from difflib import SequenceMatcher
from datetime import datetime

import pandas as pd
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeResult
from azure.core.credentials import AzureKeyCredential
from azure.identity import DefaultAzureCredential
from openai import OpenAI
from dotenv import load_dotenv

# Load .env from backend folder
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Global list to store extraction logs for observability
_extraction_logs: list[dict] = []

def get_extraction_logs() -> list[dict]:
    """Get all extraction logs for observability."""
    return _extraction_logs.copy()

def clear_extraction_logs() -> None:
    """Clear all extraction logs."""
    _extraction_logs.clear()

def log_extraction_event(
    event_type: str,
    details: dict,
    pdf_filename: str = "unknown",
    request_id: str = "unknown"
) -> None:
    """Log an extraction event for observability."""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "event_type": event_type,
        "request_id": request_id,
        "pdf_filename": pdf_filename,
        "details": details
    }
    _extraction_logs.append(log_entry)
    logger.info(f"[{event_type}] {details}")


# ── Corporate environment: patch certifi BEFORE any Azure SDK imports ──────
_corporate_cert = Path.home() / "Documents" / "cacert.pem"
if _corporate_cert.exists():
    os.environ["SSL_CERT_FILE"] = str(_corporate_cert)
    os.environ["SSL_CERT_DIR"] = str(_corporate_cert)
    import certifi

    _original_where = certifi.where

    def _patched_where() -> str:
        return str(_corporate_cert)

    certifi.where = _patched_where  # type: ignore[method-assign]
    certifi.__dict__["where"] = _patched_where  # type: ignore[index]


# ─────────────────────────────────────────────
# Document Intelligence Client
# ─────────────────────────────────────────────

def get_doc_intel_client() -> DocumentIntelligenceClient:
    """Returns a DocumentIntelligenceClient using env vars."""
    endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")

    if endpoint and key:
        return DocumentIntelligenceClient(
            endpoint=endpoint,
            credential=AzureKeyCredential(key),
        )

    if not endpoint:
        raise RuntimeError(
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is not set. "
            "Set it in .env or as an App Setting."
        )
    return DocumentIntelligenceClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
    )


def extract_tables_from_pdf(pdf_bytes: bytes, pages: Optional[str] = None) -> list[pd.DataFrame]:
    """
    Uses Azure Document Intelligence 'layout' model to extract all
    tables from a PDF and return them as a list of DataFrames.
    """
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    client = get_doc_intel_client()

    request = AnalyzeDocumentRequest(bytes_source=pdf_bytes)
    page_range = pages if pages else "1-9999"
    poller = client.begin_analyze_document(
        model_id="prebuilt-layout",
        body=request,
        pages=page_range,
    )
    result: AnalyzeResult = poller.result()

    tables: list[pd.DataFrame] = []

    for table in result.tables:
        rows: dict[int, list[str]] = {}
        max_col = 0

        for cell in table.cells:
            row_idx = cell.row_index
            col_idx = cell.column_index
            content = cell.content.strip()

            if row_idx not in rows:
                rows[row_idx] = []
            while len(rows[row_idx]) <= col_idx:
                rows[row_idx].append("")
            rows[row_idx][col_idx] = content
            max_col = max(max_col, col_idx)

        if rows:
            df = pd.DataFrame.from_dict(rows, orient="index")
            df = df.dropna(how="all").reset_index(drop=True)
            df.columns = [str(c).strip() for c in df.columns]
            tables.append(df)

    return tables


def extract_table_data_with_names(pdf_bytes: bytes, pages: Optional[str] = None) -> list[dict]:
    """
    Extract activity ID and name pairs from PDF tables.
    This provides structured data to help AI correctly map IDs to names.

    Returns list of dicts with:
    - activity_id: The Activity ID from the PDF
    - activity_name: The Activity Name from the PDF (or empty if not found)
    - row_data: Full row data as string for reference
    """
    activities: list[dict] = []

    # Single DI call, restricted to the requested page range
    tables = extract_tables_from_pdf(pdf_bytes, pages=pages)
    
    # Keywords to identify Activity ID and Activity Name columns
    id_keywords = ["activity id", "task id", "task_code", "taskcode", "activity"]
    name_keywords = ["activity name", "task name", "task_name", "name", "description"]
    
    for table in tables:
        if table.empty:
            continue
        
        col_lower = {str(c).lower(): c for c in table.columns}
        
        # Find ID column
        id_col = None
        for kw in id_keywords:
            if kw in col_lower:
                id_col = col_lower[kw]
                break
        
        # Find Name column
        name_col = None
        for kw in name_keywords:
            if kw in col_lower:
                name_col = col_lower[kw]
                break
        
        if not id_col:
            continue
        
        # Extract activity data from rows
        for idx, row in table.iterrows():
            act_id = str(row.get(id_col, "")).strip()
            
            # Skip header rows and empty rows
            if not act_id or act_id.lower() in ["activity id", "task id", "", "nan"]:
                continue
            if "activity id" in act_id.lower():
                continue
            
            act_name = str(row.get(name_col, "")).strip() if name_col else ""
            
            # Build full row data for reference
            row_data = " | ".join([str(v).strip() for v in row.values if str(v).strip()])
            
            activities.append({
                "activity_id": act_id,
                "activity_name": act_name,
                "row_data": row_data,
            })
    
    return activities


# ─────────────────────────────────────────────
# OpenAI Client
# ─────────────────────────────────────────────

def get_openai_client() -> OpenAI:
    """Returns an OpenAI client pointed at the Azure AI Foundry endpoint."""
    return OpenAI(
        api_key=os.getenv("AZURE_OPENAI_KEY"),
        base_url=os.getenv("AZURE_OPENAI_ENDPOINT"),
    )


def _get_deployment() -> str:
    """Get the model deployment name from env vars."""
    return os.getenv("AZURE_OPENAI_DEPLOYMENT", "DeepSeek-V4-Flash")


# ─────────────────────────────────────────────
# Date Extraction from PDF using AI
# ─────────────────────────────────────────────

# NOTE: No limit on PDF text - all text is sent to AI for maximum milestone detection


def extract_dates_and_data_from_pdf(
    pdf_bytes: bytes,
    standard_df: pd.DataFrame,
    request_id: str = "unknown",
    pdf_filename: str = "unknown.pdf",
    return_debug_info: bool = False,
    user_tranche: str = "T1",
    pages: Optional[str] = None,
    kb_corrections: Optional[list] = None,
) -> list[dict]:
    """
    Uses Azure OpenAI (GPT-5.4) to extract activity data from PDF.
    
    Parameters:
        pdf_bytes: The PDF file bytes
        standard_df: Standard reference DataFrame for matching
        request_id: Unique identifier for this request (for tracing)
        pdf_filename: Name of the PDF file (for logging)
        return_debug_info: If True, returns tuple (data, debug_info)
        user_tranche: User's selected tranche (T1, T2, T3) for phase filtering
    
    Returns:
        List of activity dicts, or tuple (data, debug_info) if return_debug_info=True
    """
    client = get_openai_client()
    deployment = _get_deployment()

    # Build standard reference for matching
    standard_ids = standard_df["task_code"].str.strip().str.upper().tolist()
    standard_names = standard_df["task_name"].str.strip().tolist()
    standard_count = len(standard_ids)
    
    # Build a short reference list
    ref_list = "\n".join(
        f"  {row['task_code']} | {row['task_name']} | {row.get('status_code', 'Unknown')}"
        for _, row in standard_df.iterrows()
    )

    # Extract text from PDF using Document Intelligence
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    doc_client = get_doc_intel_client()
    request = AnalyzeDocumentRequest(bytes_source=pdf_bytes)
    # Use user-specified page range or default to all pages
    page_range = pages if pages else "1-9999"
    poller = doc_client.begin_analyze_document(
        model_id="prebuilt-layout",
        body=request,
        pages=page_range,
    )
    result = poller.result()
    
    # Collect all text content from the PDF
    pdf_text = ""

    # Primary: result.content is the full concatenated document text
    if hasattr(result, 'content') and result.content:
        pdf_text = result.content

    # Always walk per-page lines — catches pages that result.content may have missed
    page_text = ""
    if hasattr(result, 'pages'):
        for page in result.pages:
            if hasattr(page, 'lines'):
                for line in page.lines:
                    if hasattr(line, 'content') and line.content:
                        page_text += line.content + "\n"

    if not pdf_text or len(pdf_text) < 100:
        pdf_text = page_text
    elif page_text and len(page_text) > len(pdf_text) * 0.1:
        # Page-line text has substantial content; append to cover any gaps
        pdf_text = pdf_text + "\n" + page_text

    # Fallback: paragraphs
    if not pdf_text or len(pdf_text) < 100:
        if hasattr(result, 'paragraphs'):
            for para in result.paragraphs:
                if hasattr(para, 'content') and para.content:
                    pdf_text += para.content + "\n"

    # Always append table cell content (activities live in tables)
    for table in result.tables:
        for cell in table.cells:
            if hasattr(cell, 'content') and cell.content:
                pdf_text += cell.content + " "
        pdf_text += "\n"

    # Log extraction start (NO LIMIT - all text sent to AI)
    log_extraction_event(
        event_type="PDF_EXTRACTION_START",
        details={
            "pdf_filename": pdf_filename,
            "pdf_text_length": len(pdf_text),
            "pages_returned": len(result.pages) if hasattr(result, 'pages') and result.pages else 0,
            "text_truncated": False,
            "text_limit": "None (full PDF processed)",
            "standard_reference_count": standard_count,
            "tables_found": len(result.tables),
        },
        pdf_filename=pdf_filename,
        request_id=request_id,
    )

    # Extract structured table data to help AI correctly map IDs to names
    table_activities = extract_table_data_with_names(pdf_bytes, pages)
    table_data_str = ""
    if table_activities:
        table_data_str = "\n\nSTRUCTURED TABLE DATA (use this for accurate ID-to-Name mapping):\n"
        table_data_str += "Format: Activity ID | Activity Name\n"
        for ta in table_activities[:200]:  # Limit to first 200 to avoid token overflow
            act_name = ta["activity_name"] if ta["activity_name"] else "(no name found in table)"
            table_data_str += f"  {ta['activity_id']} | {act_name}\n"
        if len(table_activities) > 200:
            table_data_str += f"  ... and {len(table_activities) - 200} more activities\n"

    # Build knowledge-base hint block from planner-verified corrections
    kb_block = ""
    if kb_corrections:
        keep = [c for c in kb_corrections if c.get("action") == "keep"]
        remove = [c for c in kb_corrections if c.get("action") == "remove"]
        lines = ["PLANNER-VERIFIED CORRECTIONS (from previous runs — apply these exactly):"]
        if keep:
            lines.append("Activities to KEEP and how they were corrected:")
            for c in keep:
                lines.append(
                    f'  PDF name "{c["pdf_activity_name"]}" → '
                    f'standard ID {c.get("correct_std_id","")} | '
                    f'name "{c.get("correct_task_name","")}" | '
                    f'status {c.get("correct_status","")}'
                )
        if remove:
            lines.append("Activities to EXCLUDE (planner removed these — do NOT include them):")
            for c in remove:
                lines.append(f'  "{c["pdf_activity_name"]}"')
        kb_block = "\n".join(lines) + "\n\n"

    system_prompt = f"""You are a P6 schedule data extraction expert. Your job is to extract ALL activities and milestones from this P6 schedule PDF.

CRITICAL: You MUST use the structured table data below to get the correct Activity Name for each Activity ID.
The PDF text extraction may miss or confuse Activity Names, so cross-reference with the table data provided.

{kb_block}For each activity found, extract:
1. Activity ID (from PDF) - REQUIRED
2. Activity Name (from PDF) - REQUIRED, use table data to find the correct name
3. Start Date: Extract the raw date value from the PDF exactly as it appears, preserving any leading/trailing letter 'A' (e.g. "A 01/15/2026" or "01/15/2026A"). Always populate this field when any start date is visible.
4. Finish Date: Extract the raw date value from the PDF exactly as it appears, preserving any leading/trailing letter 'A' (e.g. "A 01/15/2026" or "01/15/2026A"). Always populate this field when any finish date is visible.
5. Percent Complete (0-100)
6. Status (if visible)

P6 ACTUAL DATE CONVENTION: In some P6 schedules (especially those without a Status column), a letter 'A' attached to a date marks it as an "Actual" date — meaning that date has already occurred. Examples: "A 12/15/2025" (prefix), "12/15/2025A" (suffix), "12/15/2025 A" (space suffix). When you see dates with 'A' markers, preserve them verbatim in act_start_date / act_end_date so the system can detect them.

Standard reference milestones for matching (some PDF activities may match these):
{ref_list}

IMPORTANT RULES:
- Extract ALL activities from the PDF - every milestone, every activity
- Do not skip any activities, even if they seem less important
- CRITICAL: For Activity Name, you MUST cross-reference the table data. If the table shows "Contract Award" as the name for ID "CEX-S", use that exact name.
- Common activity names to look for (PDF may use different naming):
  * "Start Mobilisation and site Set-up" → should match "Start on Site (Contractors)"
  * "Contract Execution", "GC Award", "Planning Permit", "Design Complete"
  * "Power On", "Weather Tight", "L1/L2/L3 Commissioning", "RFS"
  * Phase-specific: "-P1", "-P2", "-P3" suffix activities
- For Percent Complete, use integer 0-100
- If an activity cannot be matched to standard reference, still include it (match_type: "no_match")
- Return ONLY valid JSON array, no markdown code fences or other text

Output format (JSON array):
[
  {{
    "pdf_activity_id": "...",
    "pdf_activity_name": "...",
    "act_start_date": "raw date from PDF, e.g. A 12/15/2025 or 12/15/2025A or 2025-12-15",
    "act_end_date": "raw date from PDF, e.g. A 12/15/2025 or 12/15/2025A or 2025-12-15",
    "complete_pct": 0-100,
    "status": "...",
    "matched_std_id": "... or null",
    "matched_std_name": "... or null",
    "name_match_confidence": 0.0-1.0,
    "date_confidence": 0.0-1.0,
    "match_type": "exact_id|exact_name|fuzzy|no_match"
  }}
]"""

    # NO LIMIT - Send all PDF text + table data to AI
    user_prompt = f"Extract ALL activities and milestones from this P6 schedule PDF:\n\n{pdf_text}{table_data_str}"

    # Store debug info
    debug_info = {
        "request_id": request_id,
        "pdf_filename": pdf_filename,
        "pdf_text_length": len(pdf_text),
        "text_sent_to_ai_length": len(pdf_text),
        "text_truncated": False,  # No limit anymore
        "text_limit": "None (full PDF processed)",
        "standard_reference_count": standard_count,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "ai_response_raw": None,
        "ai_response_parsed": None,
        "error": None,
        "activities_extracted": 0,
        "activities_matched": 0,
        "activities_unmatched": 0,
    }

    try:
        log_extraction_event(
            event_type="AI_REQUEST_START",
            details={
                "request_id": request_id,
                "deployment": deployment,
                "text_length_sent": len(pdf_text),
            },
            pdf_filename=pdf_filename,
            request_id=request_id,
        )

        # Retry logic for empty/invalid responses
        max_retries = 2
        raw = None
        for attempt in range(max_retries + 1):
            try:
                response = client.chat.completions.create(
                    model=deployment,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_completion_tokens=128000,
                    temperature=0,
                )
                
                raw = response.choices[0].message.content
                if raw and raw.strip():
                    break  # Got valid response
                    
                logger.warning(f"[{request_id}] Attempt {attempt + 1}: Empty response from AI")
                
            except Exception as retry_err:
                logger.warning(f"[{request_id}] Attempt {attempt + 1} failed: {retry_err}")
                if attempt >= max_retries:
                    raise
        
        # Log raw response for debugging
        debug_info["ai_response_raw"] = raw if raw else "(empty)"
        logger.info(f"[{request_id}] Raw AI response (first 500 chars): {str(raw)[:500] if raw else 'EMPTY'}")
        
        if not raw or not raw.strip():
            error_msg = "AI returned empty response"
            debug_info["error"] = error_msg
            logger.error(f"[{request_id}] {error_msg}")
            log_extraction_event(
                event_type="AI_EXTRACTION_ERROR",
                details={"request_id": request_id, "error": error_msg},
                pdf_filename=pdf_filename,
                request_id=request_id,
            )
            if return_debug_info:
                return [], debug_info
            return []
        
        raw = raw.strip()
        
        # Strip markdown code fences if present
        raw = re.sub(r"^```json\s*", "", raw).replace("```", "").strip()
        
        # If still empty after stripping, return empty
        if not raw:
            error_msg = "AI response was empty after stripping markdown"
            debug_info["error"] = error_msg
            logger.error(f"[{request_id}] {error_msg}")
            if return_debug_info:
                return [], debug_info
            return []
        
        extracted_data = json.loads(raw)
        debug_info["ai_response_parsed"] = extracted_data
        
        # Add page_number to each extracted activity if available
        # (This is set by the caller in extract_dates_and_data_from_pdf_multi)
        for activity in extracted_data:
            if "page_number" not in activity:
                # Will be set by the multi-range processor
                activity["page_number"] = None
        
        # Analyze results
        matched_count = sum(1 for a in extracted_data if a.get("match_type") != "no_match")
        unmatched_count = len(extracted_data) - matched_count
        debug_info["activities_extracted"] = len(extracted_data)
        debug_info["activities_matched"] = matched_count
        debug_info["activities_unmatched"] = unmatched_count

        log_extraction_event(
            event_type="AI_EXTRACTION_COMPLETE",
            details={
                "request_id": request_id,
                "total_activities": len(extracted_data),
                "matched": matched_count,
                "unmatched": unmatched_count,
                "match_types": {
                    "exact_id": sum(1 for a in extracted_data if a.get("match_type") == "exact_id"),
                    "exact_name": sum(1 for a in extracted_data if a.get("match_type") == "exact_name"),
                    "fuzzy": sum(1 for a in extracted_data if a.get("match_type") == "fuzzy"),
                    "no_match": sum(1 for a in extracted_data if a.get("match_type") == "no_match"),
                },
            },
            pdf_filename=pdf_filename,
            request_id=request_id,
        )

        if return_debug_info:
            return extracted_data, debug_info
        return extracted_data
        
    except json.JSONDecodeError as e:
        error_msg = f"JSON decode error: {e}"
        debug_info["error"] = error_msg
        logger.error(f"[{request_id}] {error_msg}")
        logger.error(f"[{request_id}] Raw response was: {str(raw)[:1000] if raw else 'EMPTY'}")
        log_extraction_event(
            event_type="AI_EXTRACTION_ERROR",
            details={"request_id": request_id, "error": error_msg, "raw_preview": str(raw)[:500] if raw else "EMPTY"},
            pdf_filename=pdf_filename,
            request_id=request_id,
        )
        if return_debug_info:
            return [], debug_info
        return []
        
    except Exception as e:
        error_msg = f"AI extraction error: {e}"
        debug_info["error"] = error_msg
        logger.error(f"[{request_id}] {error_msg}")
        
        # Try fallback to table extraction
        logger.info(f"[{request_id}] Attempting fallback to table extraction...")
        try:
            tables = extract_tables_from_pdf(pdf_bytes)
            fallback_activities = _extract_activities_from_tables(tables, standard_df)
            
            if fallback_activities:
                logger.info(f"[{request_id}] Fallback extracted {len(fallback_activities)} activities from tables")
                debug_info["fallback_used"] = True
                debug_info["fallback_activities"] = len(fallback_activities)
                
                if return_debug_info:
                    return fallback_activities, debug_info
                return fallback_activities
        except Exception as fallback_err:
            logger.error(f"[{request_id}] Fallback also failed: {fallback_err}")
        
        log_extraction_event(
            event_type="AI_EXTRACTION_ERROR",
            details={"request_id": request_id, "error": error_msg},
            pdf_filename=pdf_filename,
            request_id=request_id,
        )
        if return_debug_info:
            return [], debug_info
        return []


def _extract_activities_from_tables(tables: list[pd.DataFrame], standard_df: pd.DataFrame) -> list[dict]:
    """
    Fallback method to extract activities from PDF tables without AI.
    Uses column detection and basic parsing.
    """
    activities = []
    
    for table in tables:
        # Try to detect activity columns
        col_map = {}
        col_lower = {str(c).lower(): c for c in table.columns}
        
        for our_name, keywords in {
            "activity_id": ["activity id", "task id", "task_code", "taskcode", "activity"],
            "activity_name": ["activity name", "task name", "task_name", "name", "description"],
            "actual_start": ["actual start", "act start", "start date", "start"],
            "actual_finish": ["actual finish", "act finish", "finish date", "finish", "end"],
            "complete_pct": ["% complete", "percent complete", "complete_pct", "%", "complete"],
            "status": ["status", "activity status"],
        }.items():
            for kw in keywords:
                if kw in col_lower:
                    col_map[our_name] = col_lower[kw]
                    break
        
        # Skip tables without ID column
        if "activity_id" not in col_map:
            continue
        
        # Extract activities from table rows
        for idx, row in table.iterrows():
            act_id = str(row.get(col_map["activity_id"], "")).strip()
            
            # Skip header rows and empty rows
            if not act_id or act_id.lower() in ["activity id", "task id", "", "nan"]:
                continue
            if "activity id" in act_id.lower():
                continue
            
            activity = {
                "pdf_activity_id": act_id,
                "pdf_activity_name": str(row.get(col_map.get("activity_name", ""), "")).strip(),
                "act_start_date": str(row.get(col_map.get("actual_start", ""), "")).strip(),
                "act_end_date": str(row.get(col_map.get("actual_finish", ""), "")).strip(),
                "complete_pct": _parse_pct(row.get(col_map.get("complete_pct", ""), 0)),
                "status": str(row.get(col_map.get("status", ""), "")).strip(),
                "matched_std_id": None,
                "matched_std_name": None,
                "name_match_confidence": 0.0,
                "date_confidence": 0.0,
                "match_type": "no_match",
                "source": "table_fallback",
            }
            
            # Try to match activity
            match_result = match_activity(activity["pdf_activity_id"], activity["pdf_activity_name"], standard_df)
            if match_result["match_type"] != "no_match":
                activity["matched_std_id"] = match_result["matched_id"]
                activity["matched_std_name"] = match_result["matched_name"]
                activity["name_match_confidence"] = match_result["match_confidence"]
                activity["match_type"] = match_result["match_type"]
            
            activities.append(activity)
    
    return activities


def _parse_pct(val) -> int:
    """Parse percentage value to integer."""
    if pd.isna(val) or val == "":
        return 0
    s = str(val).replace("%", "").replace(",", "").strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def get_extraction_debug_info(pdf_bytes: bytes, standard_df: pd.DataFrame) -> dict:
    """
    Get detailed debug information about PDF extraction.
    Use this to understand how AI extracted data from the PDF.
    """
    import uuid
    request_id = str(uuid.uuid4())[:8]
    _, debug_info = extract_dates_and_data_from_pdf(
        pdf_bytes=pdf_bytes,
        standard_df=standard_df,
        request_id=request_id,
        pdf_filename="debug.pdf",
        return_debug_info=True,
    )
    return debug_info


# ─────────────────────────────────────────────────────────────────────────────
# Parallel Page Range Processing
# ─────────────────────────────────────────────────────────────────────────────

def extract_dates_and_data_from_pdf_multi(
    pdf_bytes: bytes,
    standard_df: pd.DataFrame,
    request_id: str = "unknown",
    pdf_filename: str = "unknown.pdf",
    return_debug_info: bool = False,
    user_tranche: str = "T1",
    pages: Optional[str] = None,
    kb_corrections: Optional[list] = None,
) -> Union[tuple[list[dict], dict], list[dict]]:
    """
    Extract dates and data from multiple page ranges in parallel.
    
    This function parses comma-separated page ranges (e.g., "1-2,19-20") and
    processes each range in parallel, then combines all results.
    
    Parameters:
        pdf_bytes: The PDF file bytes
        standard_df: Standard reference DataFrame for matching
        request_id: Unique identifier for this request (for tracing)
        pdf_filename: Name of the PDF file (for logging)
        return_debug_info: If True, returns tuple (data, debug_info)
        user_tranche: User's selected tranche (T1, T2, T3) for phase filtering
        pages: Comma-separated page ranges (e.g., "1-2,19-20") or single range (e.g., "1-5")
    
    Returns:
        List of activity dicts (combined from all page ranges), or tuple (data, debug_info) if return_debug_info=True
    """
    import concurrent.futures
    import threading
    
    # Parse page ranges
    if not pages:
        # No specific pages, process all
        return extract_dates_and_data_from_pdf(
            pdf_bytes=pdf_bytes,
            standard_df=standard_df,
            request_id=request_id,
            pdf_filename=pdf_filename,
            return_debug_info=return_debug_info,
            user_tranche=user_tranche,
            pages=None,
            kb_corrections=kb_corrections,
        )

    # Split by comma to get individual ranges
    page_ranges = [r.strip() for r in pages.split(",") if r.strip()]

    if len(page_ranges) == 1:
        # Single range, no need for parallel processing
        return extract_dates_and_data_from_pdf(
            pdf_bytes=pdf_bytes,
            standard_df=standard_df,
            request_id=request_id,
            pdf_filename=pdf_filename,
            return_debug_info=return_debug_info,
            user_tranche=user_tranche,
            pages=page_ranges[0],
            kb_corrections=kb_corrections,
        )
    
    # Multiple ranges - process in parallel
    logger.info(f"[{request_id}] Processing {len(page_ranges)} page ranges in parallel: {page_ranges}")
    
    # Thread-safe accumulator for results
    all_results: list[dict] = []
    results_lock = threading.Lock()
    
    def process_single_range(page_range: str) -> list[dict]:
        """Process a single page range and return extracted activities with page number."""
        import uuid
        range_request_id = f"{request_id}-{page_range.replace('-', '_')}"
        
        # Extract start page number for sorting
        start_page = int(page_range.split('-')[0]) if page_range else 0
        
        try:
            result = extract_dates_and_data_from_pdf(
                pdf_bytes=pdf_bytes,
                standard_df=standard_df,
                request_id=range_request_id,
                pdf_filename=pdf_filename,
                return_debug_info=False,
                user_tranche=user_tranche,
                pages=page_range,
                kb_corrections=kb_corrections,
            )
            # Add page_number to each activity for sorting
            for activity in result:
                activity["page_number"] = start_page
            logger.info(f"[{request_id}] Page range {page_range}: extracted {len(result)} activities")
            return result
        except Exception as e:
            logger.error(f"[{request_id}] Error processing page range {page_range}: {e}")
            return []
    
    # Process all ranges in parallel using ThreadPoolExecutor
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(page_ranges)) as executor:
        futures = {executor.submit(process_single_range, pr): pr for pr in page_ranges}
        
        for future in concurrent.futures.as_completed(futures):
            page_range = futures[future]
            try:
                result = future.result()
                with results_lock:
                    all_results.extend(result)
            except Exception as e:
                logger.error(f"[{request_id}] Future error for page range {page_range}: {e}")
    
    logger.info(f"[{request_id}] Parallel extraction complete: {len(all_results)} total activities from {len(page_ranges)} ranges")
    
    if return_debug_info:
        # Build combined debug info
        combined_debug = {
            "request_id": request_id,
            "pdf_filename": pdf_filename,
            "page_ranges_processed": page_ranges,
            "total_activities_extracted": len(all_results),
            "parallel_processing": True,
        }
        return all_results, combined_debug
    
    return all_results


# ─────────────────────────────────────────────
# Matching Functions
# ─────────────────────────────────────────────

def normalize_activity_id(activity_id: str) -> str:
    """Normalize activity ID for comparison."""
    if not activity_id:
        return ""
    return activity_id.strip().upper().replace(" ", "").replace("_", "")


def normalize_activity_name(name: str) -> str:
    """Normalize activity name for comparison."""
    if not name:
        return ""
    return name.strip().lower()


def token_sort_ratio(s1: str, s2: str) -> float:
    """Calculate token sort ratio for fuzzy matching."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, s1.lower().split(), s2.lower().split()).ratio()


def exact_match_on_id(pdf_id: str, standard_ids: list[str]) -> tuple[Optional[str], float]:
    """
    Try exact match on activity ID.
    Returns (matched_id, confidence) or (None, 0.0)
    """
    pdf_id_normalized = normalize_activity_id(pdf_id)
    
    for std_id in standard_ids:
        if normalize_activity_id(std_id) == pdf_id_normalized:
            return std_id, 1.0
    
    return None, 0.0


def exact_match_on_name(pdf_name: str, standard_df: pd.DataFrame) -> tuple[Optional[str], Optional[str], float]:
    """
    Try exact match on activity name.
    Returns (matched_id, matched_name, confidence) or (None, None, 0.0)
    """
    pdf_name_normalized = normalize_activity_name(pdf_name)
    
    for _, row in standard_df.iterrows():
        std_name_normalized = normalize_activity_name(row["task_name"])
        if std_name_normalized == pdf_name_normalized:
            return row["task_code"], row["task_name"], 1.0
    
    return None, None, 0.0


# Comprehensive synonyms mapping for ALL milestones from Lease Schedule Specification
# Covers pages 6-8 of the specification

_NAME_SYNONYMS = {
    # Contract/Milestones (CEX)
    "contract execution": "contract execution",
    "lease execution": "contract execution",
    "signature": "contract execution",
    
    # Land/Site Purchase (LSP)
    "land purchase": "land site purchase",
    "site purchase": "land site purchase",
    "land lease": "land site purchase",
    "acquisition": "land site purchase",
    
    # Planning Permit (PLA)
    "planning permit": "planning permit",
    "planning approval": "planning permit",
    "permit approval": "planning permit",
    "authority approval": "planning permit",
    
    # Utility Supply (USC)
    "utility supply": "utility supply",
    "utility contract": "utility supply",
    "electricity contract": "utility supply",
    "water electricity": "utility supply",
    "power contract": "utility supply",
    
    # GC Contract Award (GCA)
    "gc award": "gc contract award",
    "contract award": "gc contract award",
    "general contractor": "gc contract award",
    "contractor award": "gc contract award",
    
    # Design (DES)
    "design complete": "base build design complete",
    "base build design": "base build design",
    "ifc": "base build design",
    "design approval": "base build design",
    
    # Long Lead Equipment (LLE)
    "long lead": "long lead equipment",
    "equipment order": "long lead equipment",
    "procurement": "long lead equipment",
    "orders placed": "long lead equipment",
    
    # Start on Site (SOS) - Critical milestone
    "mobilisation": "start on site",
    "mobilization": "start on site",
    "site setup": "start on site",
    "site set-up": "start on site",
    "site establishment": "start on site",
    "construction start": "start on site",
    "commence construction": "start on site",
    "start construction": "start on site",
    "commence work": "start on site",
    
    # Foundations (FND)
    "foundation": "foundation",
    "piling": "piling",
    "excavation": "foundation",
    "foundations start": "foundation start",
    
    # Underground Utilities (UTL)
    "underground": "underground utilities",
    "utility": "utility",
    "underground utility": "underground utilities",
    "services": "utility",
    "ug utility": "underground utilities",
    
    # Superstructure (SUP)
    "superstructure": "superstructure",
    "structural": "superstructure",
    "steel": "superstructure",
    "structure start": "superstructure start",
    "structure complete": "superstructure complete",
    
    # Slab on Grade (SOG)
    "slab on grade": "slab on grade",
    "slab grade": "slab on grade",
    "ground slab": "slab on grade",
    "concrete slab": "slab on grade",
    
    # Slab on Deck (SOD)
    "slab on deck": "slab on deck",
    "deck slab": "slab on deck",
    "elevated slab": "slab on deck",
    
    # Power-On (POW)
    "power on": "utility power on",
    "power available": "utility power on",
    "energize": "utility power on",
    "power energ": "utility power on",
    "utility power": "utility power on",
    "mep": "utility power on",
    
    # Weather Tight (DRY)
    "weather tight": "weather tight",
    "weathertight": "weather tight",
    "building sealed": "weather tight",
    "envelope": "weather tight",
    
    # L1 Commissioning (Red Tag)
    "l1": "l1 installation",
    "red tag": "l1 installation",
    "qa qc": "l1 installation",
    "installation start": "l1 installation",
    "l1 start": "l1 installation",
    "commission start": "l1 installation",
    
    # L2 Commissioning (Yellow Tag)
    "l2": "l2 commissioning",
    "yellow tag": "l2 commissioning",
    "ready to energize": "l2 commissioning",
    "energize ready": "l2 commissioning",
    
    # Early Access (EAP)
    "early access": "early access",
    "access provided": "early access",
    "msft access": "early access",
    
    # L3 Commissioning (Green Tag)
    "l3": "l3 commissioning",
    "green tag": "l3 commissioning",
    "testing commissioning": "l3 commissioning",
    "l3 start": "l3 commissioning",
    
    # L5/IST (White Tag)
    "l5": "l5 ist",
    "white tag": "l5 ist",
    "ist": "l5 ist",
    "integration": "l5 ist",
    "complete l5": "l5 ist",
    
    # Ready for Service (RFS)
    "ready for service": "ready for service",
    "rfs": "ready for service",
    "service ready": "ready for service",
    "complete commissioning": "ready for service",
    
    # Ready for Network Install (RNI)
    "ready for network": "ready for network install",
    "network install": "ready for network install",
    "rni": "ready for network install",
    
    # Fitout Design (PRO)
    "fitout design": "fitout design",
    "fit-out design": "fitout design",
    
    # A2 Design Requirements (TOI)
    "a2 design": "a2 design requirements",
    "design requirements": "a2 design requirements",
    "technical open": "a2 design requirements",
    "toi": "a2 design requirements",
    
    # Secure Containment (SCO)
    "secure containment": "secure containment",
    "containment": "secure containment",
    
    # Fitout Complete (FOC)
    "fitout complete": "fitout complete",
    "fit-out complete": "fitout complete",
    "fitout finish": "fitout complete",
    
    # MSFT Forecast (MRFS)
    "msft forecast": "msft forecast",
    "forecast rfs": "msft forecast",
    
    # MEP - Mechanical, Electrical, Plumbing (distinguished from U/G Utilities)
    "mep complete": "mep complete",
    "mep completion": "mep complete",
    "mechanical electrical plumbing": "mep complete",
    "mep installation": "mep complete",
    "mep install": "mep complete",
    "mechanical completion": "mep complete",
    "electrical completion": "mep complete",
    "plumbing completion": "mep complete",
    "hvac": "mep complete",
    "mechanical installation": "mep complete",
    "electrical installation": "mep complete",
    "plumbing installation": "mep complete",
}

def expand_synonyms(text: str) -> set[str]:
    """Expand text with synonyms for better matching."""
    words = set(text.lower().split())
    expanded = set(words)
    for word in words:
        # Direct synonym replacement
        if word in _NAME_SYNONYMS:
            expanded.update(_NAME_SYNONYMS[word].split())
        # Partial matching for compound words
        for key, value in _NAME_SYNONYMS.items():
            if key in word:
                expanded.update(value.split())
    return expanded


# Gate-only aliases for cases where the PDF uses a completely different word than the standard.
# Kept intentionally small — the gate derives key terms from the standard names dynamically.
_GATE_ALIASES: dict[str, list[str]] = {
    "contractors": ["construction", "mobili"],   # SOS-S "Start on Site (Contractors)"
    "ist":         ["commission", "substantial"], # L5CX-F "Complete L5/IST"
    "piling":      ["foundation", "excavation"], # FND-S "Foundations Start (including piling)"
}

_METRO_CODE_PATTERN = re.compile(r'[A-Z]{2,4}-\d{2}(?:-\d{2})?(?:-[A-Z]{2,5})?', re.IGNORECASE)

def _strip_metro_codes(text: str) -> str:
    """Remove metro/project codes like PNQ-26-01, QTS-1030 from text before word matching."""
    return _METRO_CODE_PATTERN.sub(' ', text)

def _normalize_for_matching(text: str) -> str:
    """
    Aggressively normalize text for matching.
    Handles typos, missing letters, common misspellings.
    """
    text = text.lower().strip()
    
    # Remove common separators
    text = text.replace("-", " ").replace("_", " ").replace("/", " ").replace("\\", " ")
    
    # Fix common typos and misspellings
    corrections = {
        "moblisation": "mobilisation",
        "mobilization": "mobilisation",
        "moblization": "mobilisation",
        "mobiliz": "mobilisation",
        "mobliz": "mobilisation",
        "site setup": "site set up",
        "site set-up": "site set up",
        "setup": "set up",
        "onsite": "on site",
        "on site": "on site",
        "rfs": "ready for service",
        "commision": "commission",
        "commisson": "commission",
        "contstruction": "construction",
        "constuction": "construction",
        "superstructure": "superstructure",
        "weathertight": "weather tight",
        "weatheright": "weather tight",
        "power on": "power on",
        "poweron": "power on",
    }
    
    for wrong, correct in corrections.items():
        text = text.replace(wrong, correct)
    
    # Remove common words that don't help matching
    remove_words = ["the", "a", "an", "of", "to", "and", "for", "phase", "p", "wbs"]
    for word in remove_words:
        text = re.sub(r'\b' + word + r'\b', '', text)
    
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text


def _get_distinctive_terms(std_name: str, word_freq: Counter, common_threshold: float) -> list[str]:
    """
    Return words from std_name sorted by rarity across standard activities (most distinctive first).
    Excludes function words and words that appear in more than common_threshold activities
    (e.g. "complete", "start" appear everywhere and are not useful discriminators).
    Punctuation is stripped so "(Contractors)" becomes "contractors".
    """
    _FUNCTION_WORDS = frozenset({
        "the", "a", "an", "of", "to", "and", "or", "for", "in", "by",
        "at", "with", "on", "as", "&", "including", "applicable",
        # "site" is too ambiguous (construction site vs. building area vs. delivery location)
        # — SOS-S already has "contractors" as a more distinctive gate term.
        "site",
    })
    seen: set[str] = set()
    words: list[str] = []
    for raw in _normalize_for_matching(std_name).split():
        # Strip surrounding punctuation so "(contractors)" → "contractors"
        w = re.sub(r'^[^a-z0-9]+|[^a-z0-9]+$', '', raw.lower())
        if not w or w in _FUNCTION_WORDS or len(w) < 2 or w in seen:
            continue
        # Use raw word's freq first; fall back to cleaned word's freq
        freq = word_freq.get(raw, word_freq.get(w, 0))
        if freq > common_threshold:
            continue
        seen.add(w)
        words.append(w)
    words.sort(key=lambda w: word_freq.get(w, 0))
    return words


def _gate_passes(pdf_name_lower: str, std_name: str, word_freq: Counter, common_threshold: float) -> bool:
    """
    Return True if pdf_name contains at least one distinctive keyword from std_name.
    Skips scoring entirely for candidates with no semantic overlap.
    """
    for term in _get_distinctive_terms(std_name, word_freq, common_threshold)[:3]:
        aliases = _GATE_ALIASES.get(term, [term])
        if any(alias in pdf_name_lower for alias in aliases):
            return True
    return False


def match_by_id_suffix(pdf_id: str, standard_df: pd.DataFrame) -> tuple[Optional[str], Optional[str], float]:
    """
    Match a PDF activity ID to the standard reference by its acronym suffix.

    COLO activity IDs encode the milestone type in their last two hyphen-parts:
      FRA-32-SITE-CEX-S      → suffix CEX-S  → PNQ-26-01-CEX-S
      FRA-06-01-L1CX-S-P1   → strip -P1 → suffix L1CX-S → PNQ-26-01-L1CX-S
      FRA-06-01-L5CX-F-P13  → two-digit phase = RFS → suffix RFS-F → PNQ-26-01-RFS-F

    If project_code is provided, only match IDs that start with that prefix (case-insensitive).
    If no project code match found but suffix matches, returns the match with lower confidence (0.7)
    to allow name-based matching as fallback.

    Returns (matched_id, matched_name, confidence) on success, (None, None, 0.0) otherwise.
    """
    if not pdf_id or "-" not in pdf_id:
        return None, None, 0.0

    # Two-digit phase suffix (e.g. -P13, -P23, -P33) signals an RFS milestone
    rfs_pattern = re.search(r'-P(\d{2,})$', pdf_id, re.IGNORECASE)
    if rfs_pattern:
        target_suffix = "RFS-F"
    else:
        # Strip single-digit phase suffix (-P1, -P2, -P3) if present
        clean_id = re.sub(r'-P\d$', '', pdf_id, flags=re.IGNORECASE)
        parts = clean_id.strip().split('-')
        if len(parts) < 2:
            return None, None, 0.0
        target_suffix = '-'.join(parts[-2:])   # e.g. CEX-S, L1CX-S, EAP-F

    target_suffix_upper = target_suffix.upper()

    # Suffix-only match: the standard always uses PNQ as its project code, while real
    # project PDFs use different metro codes (BKK, CBR, MEL, etc.).  Matching on the
    # standard's project prefix would always fail, so we match purely on milestone suffix.
    for _, row in standard_df.iterrows():
        std_parts = row["task_code"].strip().split('-')
        if len(std_parts) >= 2:
            std_suffix = '-'.join(std_parts[-2:]).upper()
            if std_suffix == target_suffix_upper:
                return row["task_code"], row["task_name"], 0.95

    return None, None, 0.0


def fuzzy_match(
    pdf_id: str,
    pdf_name: str,
    standard_df: pd.DataFrame,
    threshold: float = 0.7,
    require_dual_match: bool = False,
) -> tuple[Optional[str], Optional[str], float]:
    """
    Try fuzzy match on activity ID or name.
    Returns (matched_id, matched_name, confidence)
    
    Enhanced matching for ALL milestones from Lease Schedule Specification (pages 6-8):
    Handles typos, misspellings, and partial matches.
    
    Priority logic:
    - If both ID AND name match well → highest priority (dual-match bonus)
    - If only name matches (no ID similarity) → only accept if score >= 0.7 and no better option
    
    Args:
        threshold: Minimum score to accept a match (default 0.7)
        require_dual_match: If True, only accept matches where both ID and name contribute
    """
    pdf_id_normalized = normalize_activity_id(pdf_id)
    pdf_name_normalized = _normalize_for_matching(pdf_name)

    # Pre-compute word frequency across all standard names (drives the distinctive-term gate)
    all_std_words: list[str] = []
    for name in standard_df["task_name"]:
        all_std_words.extend(_normalize_for_matching(name).split())
    word_freq = Counter(all_std_words)
    # Words appearing in >20% of standard activities are too common to be distinctive
    common_threshold = len(standard_df) * 0.20

    # Expand PDF name with synonyms for better matching
    pdf_name_words = expand_synonyms(pdf_name_normalized)

    candidates: list[tuple[str, str, float, float, float, float, float]] = []  # (std_id, std_name, id_score, name_score, combined, keyword_score, overlap_score)

    for _, row in standard_df.iterrows():
        std_id = row["task_code"]
        std_name = row["task_name"]

        # Gate: skip candidates whose distinctive key terms don't appear in the PDF name.
        # This prevents false-positive matches caused by common words like "complete" or "start".
        if not _gate_passes(pdf_name_normalized, std_name, word_freq, common_threshold):
            candidates.append((std_id, std_name, 0.0, 0.0, 0.0, 0.0, 0.0))
            continue

        # Calculate ID similarity
        std_id_normalized = normalize_activity_id(std_id)
        id_score = SequenceMatcher(None, pdf_id_normalized, std_id_normalized).ratio()

        # Calculate name similarity with multiple methods
        std_name_normalized = _normalize_for_matching(std_name)
        std_name_words = expand_synonyms(std_name_normalized)

        # Method 1: Token sort ratio
        token_score = token_sort_ratio(pdf_name_normalized, std_name_normalized)

        # Method 2: Word overlap (important for milestone matching)
        if pdf_name_words and std_name_words:
            overlap = len(pdf_name_words & std_name_words)
            max_words = max(len(pdf_name_words), len(std_name_words))
            overlap_score = overlap / max_words if max_words > 0 else 0
        else:
            overlap_score = 0

        # Method 3: Check if key milestone keywords match
        keyword_score = 0

        pdf_lower = pdf_name_normalized.lower()
        std_lower = std_name_normalized.lower()

        # All milestone keywords from Lease Schedule Spec pages 6-8
        key_keywords = {
            # Contract/Milestones
            "contract": ["contract", "award", "execution", "signature", "lease"],
            "land": ["land", "site", "purchase", "acquisition", "lease"],
            "planning": ["planning", "permit", "authority", "approval"],
            "utility": ["utility", "power", "electrical", "water", "electricity", "energ"],
            "design": ["design", "ifc", "drawing", "base build"],
            "equipment": ["equipment", "procurement", "long lead", "order"],
            # Construction phases
            "start": ["start", "commence", "begin", "mobilisation", "mobilis", "mobiliz"],
            "site": ["site", "onsite", "on site", "construction"],
            "foundation": ["foundation", "piling", "excavation"],
            "underground": ["underground", "ug", "utility", "services", "fiber", "connectivity", "drainage"],
            "superstructure": ["superstructure", "structural", "steel", "structure"],
            "slab": ["slab", "concrete", "grade", "deck"],
            "power": ["power", "energ", "available", "mep"],
            "weather": ["weather", "tight", "envelope", "sealed"],
            # Commissioning
            "commissioning": ["commission", "cx", "commissioning", "testing"],
            "l1": ["l1", "red tag", "qa qc", "installation"],
            "l2": ["l2", "yellow tag", "energize", "ready"],
            "l3": ["l3", "green tag", "testing"],
            "l5": ["l5", "white tag", "ist", "integration"],
            # MEP - Mechanical, Electrical, Plumbing (distinguished from underground)
            "mep": ["mep", "mechanical", "electrical", "plumbing", "hvac", "mep complete", "mep completion"],
            # Access/Service
            "access": ["access", "early", "provided"],
            "service": ["service", "rfs", "ready"],
            "network": ["network", "rni", "install"],
            # Fitout
            "fitout": ["fitout", "fit-out", "finish", "fixture"],
            "containment": ["containment", "secure", "security"],
            "forecast": ["forecast", "msft", "mrfs"],
        }

        for _, synonyms in key_keywords.items():
            for syn in synonyms:
                if syn in pdf_lower and syn in std_lower:
                    keyword_score += 0.15
                elif syn in pdf_lower or syn in std_lower:
                    keyword_score += 0.05

        # Name score = weighted combination of token + overlap + keyword
        name_score = (token_score * 0.25) + (overlap_score * 0.30) + (keyword_score * 0.30)

        # Combined score (used when both ID and name contribute)
        combined_score = (id_score * 0.15) + (name_score * 0.85)

        candidates.append((std_id, std_name, id_score, name_score, combined_score, keyword_score, overlap_score))
    
    # Sort by: 1) dual-match bonus, 2) combined score, 3) name score
    # Dual-match = both id_score > 0.2 AND name_score > 0.5
    def sort_key(c):
        std_id, std_name, id_score, name_score, combined, _kw, _ov = c
        is_dual_match = (id_score > 0.2 and name_score > 0.5)
        # Prioritize dual matches, then by combined score
        return (1 if is_dual_match else 0, combined, name_score)

    # Find best match using two passes:
    # Pass 1 — Overlap shortcut for QTS/DFW-style IDs where id_score is always low.
    #   Uses min-denominator stripped overlap so short standard names aren't drowned
    #   out by long PDF names (e.g. "DH 1100 Early Access (9/1/25)..." vs "Early Access Provided").
    pdf_name_stripped = _normalize_for_matching(_strip_metro_codes(pdf_name))

    for std_id, std_name, id_score, name_score, combined, kw_score, ov_score in sorted(
        candidates, key=lambda c: c[3], reverse=True
    ):
        if id_score < 0.45 and name_score > 0.0:
            # Compute stripped-word overlap (metro codes removed from both sides)
            std_name_stripped_words = set(_normalize_for_matching(_strip_metro_codes(std_name)).split())
            pdf_name_stripped_words = set(pdf_name_stripped.split())
            if pdf_name_stripped_words and std_name_stripped_words:
                stripped_overlap = len(pdf_name_stripped_words & std_name_stripped_words)
                # Use min denominator: measures coverage of the shorter name's words
                stripped_min = min(len(pdf_name_stripped_words), len(std_name_stripped_words))
                stripped_ov = stripped_overlap / stripped_min if stripped_min > 0 else 0.0
            else:
                stripped_ov = ov_score

            # Condition A: moderate keyword signal + good stripped-word overlap
            if kw_score >= 0.15 and stripped_ov >= 0.20:
                return std_id, std_name, max(name_score, 0.65)

            # Condition B: strong keyword signal + modest raw overlap
            if kw_score >= 0.40 and ov_score >= 0.20:
                return std_id, std_name, max(name_score, 0.65)

    # Pass 2 — Standard combined/name score selection (sorted order)
    # Sort candidates (highest priority first)
    candidates.sort(key=sort_key, reverse=True)

    for std_id, std_name, id_score, name_score, combined, *_ in candidates:
        # If require_dual_match, only accept if both ID and name contribute meaningfully
        if require_dual_match:
            if id_score <= 0.2 or name_score <= 0.5:
                continue

        if id_score > 0.2 and name_score > 0.5:
            # Dual match: ID and name both contribute — use combined score
            if combined >= threshold:
                return std_id, std_name, combined
        else:
            # Standard name-only threshold for recognisable COLO IDs
            if name_score >= threshold:
                return std_id, std_name, name_score

    return None, None, 0.0


# Track used milestone names for duplicate detection
_used_milestone_names: set[str] = set()

def reset_milestone_tracking():
    """Reset the milestone tracking for a new PDF processing session."""
    global _used_milestone_names
    _used_milestone_names = set()


def get_duplicate_tranche_suffix(standard_milestone_name: str, user_tranche: str) -> str:
    """
    For duplicate milestones (same name appears multiple times in PDF),
    assign sequential tranche numbers: 02, 03, 04...
    
    Args:
        standard_milestone_name: The matched standard milestone name (e.g., "Ready for Service (RFS)")
        user_tranche: User's selected tranche (e.g., "T1", "T2", "1", "2")
    
    Returns:
        Tranche suffix for the activity ID (e.g., "-02", "-03", "-04")
    """
    global _used_milestone_names
    
    # Normalize the milestone name for tracking
    name_key = standard_milestone_name.strip().lower()
    
    # Count how many times this name has been used
    used_count = sum(1 for used_name in _used_milestone_names if used_name == name_key)
    
    # Add this usage
    _used_milestone_names.add(name_key)
    
    # Calculate the tranche number
    # If user selected T1 (1), first duplicate gets 02, second gets 03
    # If user selected T2 (2), first duplicate gets 03, second gets 04
    user_tranche_num = int(user_tranche.strip().upper().replace("T", "").lstrip("0") or "1")
    new_tranche_num = user_tranche_num + used_count + 1  # +1 because first is already user tranche
    
    return f"{new_tranche_num:02d}"


def match_activity(
    pdf_activity_id: str,
    pdf_activity_name: str,
    standard_df: pd.DataFrame,
    user_tranche: str = "T1",
) -> dict:
    """
    Match a PDF activity against the standard reference.
    Priority: Exact ID > Name Match (when ID fails) > Fuzzy Match
    
    For duplicate milestones (same name appears multiple times in PDF),
    the tranche number is incremented: 02, 03, 04...
    
    Returns dict with:
        - matched_id: Standard activity ID or None
        - matched_name: Standard activity name or None
        - match_confidence: 0.0-1.0
        - match_type: "exact_id" | "exact_name" | "name_fallback" | "fuzzy" | "no_match"
        - duplicate_tranche: Tranche suffix for duplicates (e.g., "-02") or None
    """
    standard_ids = standard_df["task_code"].str.strip().str.upper().tolist()
    
    # Step 1: Exact match on ID
    matched_id, confidence = exact_match_on_id(pdf_activity_id, standard_ids)
    if matched_id:
        matched_name = standard_df[standard_df["task_code"] == matched_id]["task_name"].iloc[0]
        duplicate_tranche = None  # No duplicate handling for exact ID matches
        
        # Check if this ID would cause a duplicate (same ID appears again)
        # For now, just track it normally
        
        return {
            "matched_id": matched_id,
            "matched_name": matched_name,
            "match_confidence": confidence,
            "match_type": "exact_id",
            "duplicate_tranche": duplicate_tranche,
        }
    
    # Step 2: Try exact match on name FIRST (before fuzzy ID matching)
    # This handles cases like "L5CX-F-P13 Phase 1 - RFS" where ID doesn't match but name does
    matched_id, matched_name, name_confidence = exact_match_on_name(pdf_activity_name, standard_df)
    if matched_id:
        # Check for duplicate milestone name
        name_key = matched_name.strip().lower()
        if name_key in _used_milestone_names:
            # This is a duplicate - assign next tranche
            duplicate_tranche = get_duplicate_tranche_suffix(matched_name, user_tranche)
        else:
            _used_milestone_names.add(name_key)
            duplicate_tranche = None
        
        return {
            "matched_id": matched_id,
            "matched_name": matched_name,
            "match_confidence": name_confidence,
            "match_type": "exact_name",
            "duplicate_tranche": duplicate_tranche,
        }
    
    # Step 3: Fuzzy match on ID or name
    matched_id, matched_name, fuzzy_confidence = fuzzy_match(pdf_activity_id, pdf_activity_name, standard_df)
    if matched_id:
        # Check for duplicate milestone name
        name_key = matched_name.strip().lower()
        if name_key in _used_milestone_names:
            # This is a duplicate - assign next tranche
            duplicate_tranche = get_duplicate_tranche_suffix(matched_name, user_tranche)
        else:
            _used_milestone_names.add(name_key)
            duplicate_tranche = None
        
        return {
            "matched_id": matched_id,
            "matched_name": matched_name,
            "match_confidence": fuzzy_confidence,
            "match_type": "fuzzy",
            "duplicate_tranche": duplicate_tranche,
        }
    
    # No match found
    return {
        "matched_id": None,
        "matched_name": None,
        "match_confidence": 0.0,
        "match_type": "no_match",
        "duplicate_tranche": None,
    }


# ─────────────────────────────────────────────
# Tranche Extraction from PDF Activity ID
# ─────────────────────────────────────────────

def extract_tranche_from_pdf_id(pdf_id: str) -> Optional[str]:
    """
    Extract tranche number from PDF activity ID.
    
    The tranche is always in the 3rd hyphen-separated position (e.g., "01", "02", "03").
    
    Examples:
    - "WAW-21-03-L1CX-S" → "03" (3rd position)
    - "FRA-06-01-L1CX-S" → "01" (3rd position)
    - "FRA-06-01-CEX-S-P1" → "01" (3rd position, -P1 is phase suffix)
    
    Returns None if no tranche found.
    """
    if not pdf_id:
        return None
    
    # Split by hyphen and get the 3rd element (index 2)
    parts = pdf_id.split('-')
    if len(parts) >= 3:
        # The tranche is always in the 3rd position (index 2)
        tranche = parts[2]
        # Validate it's a two-digit number starting with 0
        if re.match(r'^0\d$', tranche):
            return tranche
    
    return None


def extract_tranche_and_build_output_id(
    pdf_id: str,
    standard_id: str,
    project_code: str,
) -> tuple[str, Optional[str]]:
    """
    Build the output activity ID using tranche from PDF ID.
    
    If PDF ID contains a tranche (e.g., "03"), use it.
    Otherwise, use default tranche from standard ID or "01".
    
    Returns: (output_activity_id, extracted_tranche)
    """
    # Extract tranche from PDF ID
    extracted_tranche = extract_tranche_from_pdf_id(pdf_id)
    
    # Parse standard ID to get acronym_suffix
    _, _, acronym_suffix = parse_standard_id(standard_id)
    
    # Extract metro code and year from project code
    metro_code = project_code[:3].upper()
    year_match = re.search(r'(\d{2})', project_code)
    year = year_match.group(1) if year_match else "24"
    
    # Use extracted tranche or default to "01"
    tranche = extracted_tranche if extracted_tranche else "01"
    
    # Build output activity ID
    output_id = f"{metro_code}-{year}-{tranche}-{acronym_suffix}"
    
    return output_id, extracted_tranche


# ─────────────────────────────────────────────
# Activity ID Transformation
# ─────────────────────────────────────────────

def parse_standard_id(standard_id: str) -> tuple[str, str, str]:
    """
    Parse a standard activity ID into components.
    Format: PROJECT-YY-TR-ACRONYM-SUFFIX (e.g., PNQ-26-01-CEX-S)
    Returns: (project_code, year_tranche, acronym_suffix)
    """
    parts = standard_id.strip().replace(" ", "").split("-")
    if len(parts) >= 4:
        project_code = parts[0]  # e.g., "PNQ"
        year_tranche = parts[1] + "-" + parts[2]  # e.g., "26-01"
        acronym_suffix = "-".join(parts[3:])  # e.g., "CEX-S"
        return project_code, year_tranche, acronym_suffix
    
    return "", "", standard_id


def transform_activity_id(
    standard_id: str,
    project_code: str,
    tranche: str,
) -> str:
    """
    Transform a standard activity ID using user-provided project code and tranche.
    
    Example:
        Standard ID: PNQ-26-01-CEX-S
        Project Code: BKK20
        Tranche: 01
        
        Result: BKK-20-01-CEX-S
    """
    # Parse the standard ID to extract the acronym_suffix
    _, _, acronym_suffix = parse_standard_id(standard_id)
    
    # Extract first 3 letters for metro code (e.g., "BKK20" -> "BKK")
    metro_code = project_code[:3].upper()
    
    # Extract year from project code (e.g., "BKK20" -> "20")
    year_match = re.search(r'(\d{2})', project_code)
    year = year_match.group(1) if year_match else "24"
    
    # Normalize tranche (T2 -> 02, 2 -> 02, etc.)
    tranche_clean = tranche.strip().upper().replace("T", "").zfill(2)
    
    # Build the new activity ID: {metro}-{year}-{tranche}-{acronym_suffix}
    new_id = f"{metro_code}-{year}-{tranche_clean}-{acronym_suffix}"
    return new_id


# ─────────────────────────────────────────────
# Confirm Activity Name (Legacy - kept for compatibility)
# ─────────────────────────────────────────────

def confirm_activity_name(
    raw_name: str,
    activity_id: str,
    standard_df: pd.DataFrame,
) -> dict:
    """
    Legacy function - use match_activity() instead.
    """
    match_result = match_activity(activity_id, raw_name, standard_df)
    return {
        "confirmed": match_result["match_type"] != "no_match",
        "suggested_name": match_result["matched_name"] or raw_name,
        "confidence": match_result["match_confidence"],
    }


# ─────────────────────────────────────────────
# Correct Activity ID (Legacy - kept for compatibility)
# ─────────────────────────────────────────────

def correct_activity_id(raw_id: str, standard_ids: list[str]) -> str:
    """
    Legacy function - use match_activity() instead.
    """
    matched_id, confidence = exact_match_on_id(raw_id, standard_ids)
    if matched_id:
        return matched_id
    
    # Try fuzzy
    from difflib import get_close_matches
    matches = get_close_matches(raw_id, standard_ids, n=1, cutoff=0.6)
    return matches[0] if matches else raw_id
