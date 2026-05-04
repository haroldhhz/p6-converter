"""
P6 Schedule Processor — Activity Matching and Date Extraction Pipeline.

Step 1  : Extract dates and data from PDF using AI (GPT-5.4-mini)
Step 2  : Match activities against standard reference (Exact ID → Exact Name → Fuzzy)
Step 3  : Transform Activity ID using user-provided project_code and tranche
Step 4  : Use standard activity names from reference
Step 5  : Populate dates, % complete, and status from PDF

Output  : pandas DataFrame with 13 P6-import columns
"""

import re
import os
import pandas as pd
from collections import defaultdict
from datetime import datetime
from typing import Optional

from backend.ai_client import (
    extract_tables_from_pdf,
    match_activity,
    match_by_id_suffix,
    transform_activity_id,
    extract_tranche_from_pdf_id,
    extract_tranche_and_build_output_id,
    parse_standard_id,
    extract_dates_and_data_from_pdf,
    extract_dates_and_data_from_pdf_multi,
    reset_milestone_tracking,
)

# ─────────────────────────────────────────────────────────────────────────────
# Load embedded standard reference (Attachment 3)
# ─────────────────────────────────────────────────────────────────────────────

def _load_standard() -> pd.DataFrame:
    """Load the embedded standard reference CSV (Attachment 3)."""
    std_path = os.path.join(os.path.dirname(__file__), "standard_data.csv")
    df = pd.read_csv(std_path)
    df["task_code"] = df["task_code"].str.strip().str.upper()
    return df

STANDARD_DF = _load_standard()


# ─────────────────────────────────────────────────────────────────────────────
# Milestone suffixes that trigger tranche-based WBS code (6.X instead of 6)
# ─────────────────────────────────────────────────────────────────────────────

MILESTONE_SUFFIXES = {
    "PRO-F", "TOI-F",           # Fitout Design/Requirements
    "L1CX-S", "L1CX-F",         # L1 Red Tag
    "L2CX-S", "L2CX-F",         # L2 Yellow Tag
    "EAP-F",                    # Early Access
    "L3CX-S", "L3CX-F",         # L3 Green Tag
    "L4CX-S",                   # L4 Blue Tag
    "L5CX-F",                   # L5/IST White Tag
    "SCO-F",                    # Secure Containment
    "FOC-F",                    # Fitout Complete
    "RFS-F",                    # Ready for Service
    "RNI-F",                    # Ready for Network Install
    "POW-F",                    # Power-On (can have multiple tranches)
}

# Milestone types that should only have ONE occurrence (no duplicates)
# Once a match is found for these, subsequent matches are skipped
SINGLETON_MILESTONES = {
    "DES-F",                    # Base Build Design Complete - only one allowed
}


# ─────────────────────────────────────────────────────────────────────────────
# Output column schema (P6 Import) - 13 columns
# ─────────────────────────────────────────────────────────────────────────────

P6_COLUMNS = [
    "task_code",
    "status_code",
    "wbs_id",
    "actv_code_activity_type_id",
    "task_name",
    "cstr_type",
    "cstr_date",
    "act_start_date",
    "act_end_date",
    "delete_record_flag",
]

# P6 column header row (what you see in Excel)
P6_HEADERS = [
    "Activity ID",
    "Activity Status",
    "WBS Code",
    "Activity Type",
    "Activity Name",
    "Primary Constraint",
    "Primary Constraint Date",
    "Actual Start",
    "Actual Finish",
    "Delete This Row",
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_milestone_activity(acronym_suffix: str) -> bool:
    """Check if the activity is a milestone that triggers tranche-based WBS."""
    return acronym_suffix.upper() in MILESTONE_SUFFIXES


def extract_activity_type(task_code: str) -> str:
    """
    Extract activity type from task_code.
    - If ends with -S (e.g., -S, -S10, -S-P1) → "Start Milestone"
    - If ends with -F (e.g., -F, -F5, -F-P13) → "Finish Milestone"
    - Otherwise → ""
    """
    if not task_code:
        return ""
    
    # Normalize to uppercase
    code = task_code.strip().upper()
    
    # Check if ends with -S or -F (can have additional suffixes after)
    if code.endswith("-S"):
        return "Start Milestone"
    elif code.endswith("-F"):
        return "Finish Milestone"
    
    return ""


def get_primary_constraint(activity_type: str) -> str:
    """Get primary constraint type based on activity type."""
    if activity_type == "Start Milestone":
        return "Start On or After"
    elif activity_type == "Finish Milestone":
        return "Finish On or After"
    return ""


def extract_data_date(raw_df: pd.DataFrame, fallback: str = "2026-02-28") -> str:
    """
    Try to extract the IMS data date from the first rows / columns of
    the raw PDF table. Falls back to DEFAULT_DATA_DATE env var or param.
    """
    for col in raw_df.columns:
        for val in raw_df[col].astype(str):
            m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", val)
            if m:
                return m.group(1)

    env_date = os.getenv("DEFAULT_DATA_DATE")
    return env_date or fallback


def build_wbs_id(project_code: str, tranche: str, data_date: str, is_milestone: bool = False) -> str:
    """
    Format: {project_code}_IMS_{data_date}_UD.6[.N]

    - For milestones in MILESTONE_SUFFIXES (L1CX-S, L2CX-S, etc.): UD.6.{tranche}
    - For other activities (FND-S, FND-F, etc.): UD.6

    Uses the full project_code (e.g. BKK20, not truncated to BKK).
    """
    tranche_num = tranche.strip().upper().replace("T", "").lstrip("0") or "1"
    base = f"{project_code.upper()}_IMS_{data_date}_UD.1"
    if is_milestone:
        return f"{base}.{tranche_num}"
    return base


def build_wbs_path(tranche: str, is_milestone: bool = False) -> str:
    """
    Build WBS Path (last portion of WBS code).
    For milestone activities: use tranche decimal (6.1, 6.2)
    For non-milestone activities: use 6
    """
    if not is_milestone:
        return "6"
    
    tranche_num = tranche.strip().upper().replace("T", "").lstrip("0") or "1"
    return f"6.{tranche_num}"


def parse_activity_id(raw_id: str) -> tuple[str, str, str]:
    """
    Lightweight parse: returns (project_code, tranche, acronym+suffix).
    Used for WBS extraction before AI correction.
    """
    # Try hyphenated form first: PNQ-26-01-CEX-S
    parts = raw_id.strip().replace(" ", "").split("-")
    if len(parts) >= 3:
        project = parts[0]
        tranche = parts[1]
        acronym_suffix = "-".join(parts[2:])
        return project, tranche, acronym_suffix

    # Try compact form: PNQ2601CEXS
    m = re.match(
        r"([A-Z]{3})(\d{2})(\d{2})([A-Z].*)",
        raw_id.strip(),
        re.IGNORECASE,
    )
    if m:
        return m.group(1).upper(), m.group(2), m.group(4).upper()

    return raw_id, "01", ""


def format_date(val, fmt: str = "%m/%d/%Y %H:%M") -> str:
    """Try to normalise a date value to MM/DD/YYYY HH:MM format."""
    if pd.isna(val) or str(val).strip() in ("", "None", "NaT"):
        return ""
    s = str(val).strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            dt = datetime.strptime(s, pattern)
            return dt.strftime(fmt)
        except ValueError:
            pass
    return s


def normalize_date_for_output(date_str: str) -> str:
    """
    Normalize date string to MM/DD/YYYY HH:MM format.
    Handles various input formats including YYYY-MM-DD.
    """
    if not date_str or date_str.strip() in ("", "None", "NaT", "null"):
        return ""
    
    s = date_str.strip()
    
    # Try parsing various date formats
    # Try various date formats including M/D/YY (2-digit year) and M/D/YYYY (1-digit month/day)
    for pattern in (
        "%Y-%m-%d", "%Y/%m/%d",
        "%m/%d/%Y", "%d/%m/%Y",
        "%m/%d/%y",  # M/D/YY format (e.g., 12/2/25 or 12/02/25)
        "%m/%d/%y %H:%M",  # M/D/YY with time (e.g., 12/2/25 00:00)
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, pattern)
            return dt.strftime("%m/%d/%Y %H:%M")
        except ValueError:
            pass
    
    # If already in correct format, return as is
    return s


def auto_detect_columns(df: pd.DataFrame) -> dict[str, str]:
    """
    Map raw PDF column headers to our known field names.
    Returns a dict: {our_field: raw_column_name}
    """
    mapping: dict[str, str] = {}
    col_lower = {str(c).lower(): c for c in df.columns}

    for our_name, keywords in {
        "activity_id": ["activity id", "task id", "task_code", "taskcode"],
        "activity_name": ["activity name", "task name", "task_name"],
        "activity_status": ["activity status", "status", "status_code"],
        "complete_pct": ["% complete", "percent complete", "complete_pct", "activity %"],
        "actual_start": ["actual start", "act start", "start date"],
        "actual_finish": ["actual finish", "act finish", "finish date"],
    }.items():
        for kw in keywords:
            if kw in col_lower:
                mapping[our_name] = col_lower[kw]
                break
    return mapping


def extract_status_from_pdf(status_str: str, complete_pct: Optional[int] = None) -> str:
    """
    Normalize status string from PDF to standard values.
    Priority: 1) explicit status, 2) % complete, 3) no data available
    
    Parameters:
        status_str: Activity status string from PDF
        complete_pct: Percent complete (0-100), used as fallback
    """
    # First: check explicit status - ONLY Completed or Not Started allowed
    if status_str and status_str.strip().lower() not in ["", "nan", "none"]:
        s = status_str.strip().lower()
        if s in ["completed", "finish", "f", "100%", "complete", "done"]:
            return "Completed"
        # "In Progress", "On Hold" etc. will fall through to % complete check
        if s in ["not started", "ns", "pending", "planned", "scheduled"]:
            return "Not Started"
    
    # Second: check % complete (0% = Not Started, 100% = Completed)
    if complete_pct is not None:
        if complete_pct == 100:
            return "Completed"
        elif complete_pct == 0:
            return "Not Started"
        # Any intermediate % (1-99) maps to Not Started
        return "Not Started"
    
    # Third: no data available
    return "No data available for determining status"


# ─────────────────────────────────────────────────────────────────────────────
# Main Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def process_schedule(
    raw_df: pd.DataFrame,
    standard_df: pd.DataFrame,
    project_code: str,
    tranche: str,
    data_date: Optional[str] = None,
) -> pd.DataFrame:
    """
    Execute the processing pipeline on a raw PDF-extracted DataFrame.

    Parameters
    ----------
    raw_df       : DataFrame extracted from the PDF via Document Intelligence
    standard_df  : DataFrame loaded from Attachment 3 (standard reference)
                   Must contain columns: task_code, task_name, status_code
    project_code : User-provided project code (e.g., "KUL24")
    tranche      : User-provided tranche (e.g., "T2" or "02")
    data_date    : IMS date string (YYYY-MM-DD). Auto-detected if not provided.

    Returns
    -------
    DataFrame with the 13 P6 import columns ready to be saved as .xlsx
    """
    # ── Pre-flight ──────────────────────────────────────────────────────────
    if data_date is None:
        data_date = extract_data_date(raw_df) or "2026-02-28"

    col_map = auto_detect_columns(raw_df)
    get_col = lambda name: col_map.get(name)

    # Build standard lookup structures
    standard_ids = standard_df["task_code"].str.strip().str.upper().tolist()
    std_by_id = standard_df.set_index("task_code")[["task_name", "status_code"]].to_dict("index")

    rows: list[dict] = []
    skipped_count = 0
    matched_count = 0

    for _, src in raw_df.iterrows():
        # ── Get raw data from PDF ────────────────────────────────────────────
        raw_id_col = get_col("activity_id")
        raw_id = str(src[raw_id_col]).strip() if raw_id_col else ""
        
        raw_name_col = get_col("activity_name")
        raw_name = str(src[raw_name_col]).strip() if raw_name_col else ""
        
        start_col = get_col("actual_start")
        act_start_date = normalize_date_for_output(str(src[start_col])) if start_col else ""
        
        finish_col = get_col("actual_finish")
        act_end_date = normalize_date_for_output(str(src[finish_col])) if finish_col else ""
        
        status_col = get_col("activity_status")
        pdf_status = str(src[status_col]).strip() if status_col else ""
        
        complete_col = get_col("complete_pct")
        complete_pct = int(src[complete_col]) if complete_col else None

        # ── Step 1: Match activity against standard reference ───────────────
        match_result = match_activity(raw_id, raw_name, standard_df)
        
        matched_id = match_result["matched_id"]
        matched_name = match_result["matched_name"]
        match_type = match_result["match_type"]

        # ── If no match found, skip this row entirely ────────────────────────
        if match_type == "no_match":
            skipped_count += 1
            continue

        matched_count += 1

        # ── Step 2: Transform Activity ID using user input ───────────────────
        transformed_id = transform_activity_id(matched_id, project_code, tranche)
        
        # Extract activity type from transformed ID
        activity_type = extract_activity_type(transformed_id)
        
        # Parse standard ID for milestone check
        _, _, acronym_suffix = parse_standard_id(matched_id)
        is_milestone = is_milestone_activity(acronym_suffix)

        # ── Step 3: Get status from standard or PDF ─────────────────────
        std_entry = std_by_id.get(matched_id, {})
        std_status = std_entry.get("status_code", "Not Started")
        
        if pdf_status and pdf_status.lower() not in ["", "nan", "none"]:
            activity_status = extract_status_from_pdf(pdf_status, complete_pct)
        else:
            activity_status = extract_status_from_pdf("", complete_pct)

        # ── Step 4: Build WBS ID ──────────────────────────────────────────
        wbs_id = build_wbs_id(project_code, tranche, data_date, is_milestone)

        # ── Step 5: Get Primary Constraint ─────────────────────────────
        primary_constraint = get_primary_constraint(activity_type)
        
        # Primary Constraint Date - ALWAYS present for milestones
        if activity_type == "Start Milestone":
            cstr_date = act_start_date or ""
        elif activity_type == "Finish Milestone":
            cstr_date = act_end_date or ""
        else:
            cstr_date = ""

        # ── Step 6: Actual Start/Finish - ONLY if status=Completed
        if activity_status == "Completed":
            if activity_type == "Start Milestone":
                actual_start = act_start_date
                actual_finish = actual_start  # set equal so P6 doesn't auto-fill with data date
            elif activity_type == "Finish Milestone":
                actual_finish = act_end_date
                actual_start = actual_finish  # set equal so P6 doesn't auto-fill with data date
            else:
                actual_start = act_start_date
                actual_finish = act_end_date
        else:
            actual_start = ""
            actual_finish = ""

        # ── Step 7: Use standard activity name ──────────────────────────────
        task_name = matched_name or std_entry.get("task_name", raw_name)

        rows.append(
            {
                "task_code": transformed_id,
                "status_code": activity_status,
                "wbs_id": wbs_id,
                "actv_code_activity_type_id": "",
                "task_name": task_name,
                "cstr_type": primary_constraint,
                "cstr_date": cstr_date,
                "act_start_date": actual_start,
                "act_end_date": actual_finish,
                "delete_record_flag": "",
            }
        )

    # Log statistics
    print(f"Processing complete: {matched_count} matched, {skipped_count} skipped (no match found)")

    result_df = pd.DataFrame(rows, columns=P6_COLUMNS)
    return result_df


_CONFIDENCE_THRESHOLD = 0.8  # Minimum confidence to accept any match


def process_schedule_with_ai_extraction(
    pdf_bytes: bytes,
    standard_df: pd.DataFrame,
    project_code: str,
    tranche: str,
    data_date: Optional[str] = None,
    pages: Optional[str] = None,
    kb_corrections: Optional[list] = None,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    """
    Alternative pipeline using AI to extract dates directly from PDF text.

    Returns a tuple of:
    - DataFrame with only MATCHED activities (for XLSX output)
    - List of ALL extracted activities with original columns only (for debug/display)

    Unmatched activities and matches below 80% confidence are skipped in the
    XLSX output but included in the full extraction list for display.

    Activities matching the same milestone type (same acronym suffix, e.g. RFS-F)
    are grouped together, sorted by date (earliest first), and assigned sequential
    tranches. Activities sharing the same date share a tranche; collisions are
    resolved by keeping the higher confidence match.
    """
    if data_date is None:
        data_date = "2026-02-28"

    reset_milestone_tracking()

    # ── Step 1: Extract all data from PDF using AI ──────────────────────────
    # Use parallel processing for multiple page ranges (e.g., "1-2,19-20")
    extracted_data = extract_dates_and_data_from_pdf_multi(
        pdf_bytes,
        standard_df,
        user_tranche=tranche,
        pages=pages,
        kb_corrections=kb_corrections,
    )

    # Build full activity list for debug display (original columns only - unchanged)
    all_activities_for_debug = []
    std_by_id = standard_df.set_index("task_code")[["task_name", "status_code"]].to_dict("index")

    # Process each extracted activity for debug list
    for activity in extracted_data:
        # Use `or ""` so AI-returned null values don't propagate as None
        pdf_id = activity.get("pdf_activity_id") or ""
        pdf_name = activity.get("pdf_activity_name") or ""
        act_start_date = normalize_date_for_output(activity.get("act_start_date") or "")
        act_end_date = normalize_date_for_output(activity.get("act_end_date") or "")
        pdf_status = activity.get("status") or ""
        ai_match_type = activity.get("match_type") or "no_match"
        ai_matched_id = activity.get("matched_std_id") or None
        ai_matched_name = activity.get("matched_std_name") or ""
        ai_confidence = float(activity.get("match_confidence") or activity.get("name_match_confidence") or 0.0)

        # Determine final match status using same logic as XLSX output
        suffix_id, suffix_name, suffix_conf = match_by_id_suffix(pdf_id, standard_df, project_code=project_code)
        if suffix_id:
            final_matched_id = suffix_id
            final_matched_name = suffix_name
            final_confidence = suffix_conf
            final_match_type = "suffix_match"
        elif ai_match_type != "no_match" and ai_matched_id and ai_confidence >= _CONFIDENCE_THRESHOLD:
            final_matched_id = ai_matched_id
            final_matched_name = ai_matched_name
            final_confidence = ai_confidence
            final_match_type = ai_match_type
        else:
            match_result = match_activity(pdf_id, pdf_name, standard_df, user_tranche=tranche)
            if match_result["match_type"] == "no_match" or match_result["match_confidence"] < _CONFIDENCE_THRESHOLD:
                final_matched_id = None
                final_matched_name = None
                final_confidence = 0.0
                final_match_type = "no_match"
            else:
                final_matched_id = match_result["matched_id"]
                final_matched_name = match_result["matched_name"]
                final_confidence = match_result["match_confidence"]
                final_match_type = match_result["match_type"]

        # Add to debug list with all info (original columns only)
        # CRITICAL: Only include activities where we have the original PDF name
        # If pdf_activity_name is empty, the match is NOT trustworthy (Option C)
        if not pdf_name or pdf_name.strip() == "" or pdf_name.lower() == "none":
            # Log this for observability
            if final_matched_id:
                # This is a matched activity but without verifiable PDF name
                print(f"  [UNTRUSTWORTHY] {pdf_id} matched to {final_matched_id} but pdf_activity_name is empty - skipping from preview")
            # Skip this activity - don't add to preview results
            continue
        
        std_entry = std_by_id.get(final_matched_id, {}) if final_matched_id else {}
        debug_activity = {
            "pdf_activity_id": pdf_id,
            "pdf_activity_name": pdf_name,
            "act_start_date": act_start_date,
            "act_end_date": act_end_date,
            "complete_pct": activity.get("complete_pct", 0),
            "status": pdf_status,
            "matched_std_id": final_matched_id or None,
            "matched_std_name": final_matched_name or std_entry.get("task_name", ""),
            "name_match_confidence": final_confidence,
            "date_confidence": 0.8 if act_start_date else 0.0,
            "match_type": final_match_type,
        }
        all_activities_for_debug.append(debug_activity)

    if not extracted_data:
        print("Warning: No data extracted from PDF by AI")
        return pd.DataFrame(columns=P6_COLUMNS), []

    base_tranche_num = int(tranche.strip().upper().replace("T", "").lstrip("0") or "1")
    year_match = re.search(r'(\d{2})', project_code)
    year = year_match.group(1) if year_match else "24"

    # ── Step 2: Match all activities, collect intermediates ─────────────────
    intermediate: list[dict] = []
    unmatched_count = 0
    
    # Track singleton milestones (only one occurrence allowed)
    matched_singletons: set[str] = set()

    for activity in extracted_data:
        # Use `or ""` so AI-returned null values don't propagate as None
        pdf_id = activity.get("pdf_activity_id") or ""
        pdf_name = activity.get("pdf_activity_name") or ""
        act_start_date = normalize_date_for_output(activity.get("act_start_date") or "")
        act_end_date = normalize_date_for_output(activity.get("act_end_date") or "")
        pdf_status = activity.get("status") or ""

        # Skip project-specific MIL-* activities
        if re.match(r'^MIL-', pdf_id, re.IGNORECASE):
            unmatched_count += 1
            continue

        ai_matched_id = activity.get("matched_std_id") or None
        ai_matched_name = activity.get("matched_std_name") or ""
        ai_confidence = float(activity.get("match_confidence") or activity.get("name_match_confidence") or 0.0)
        ai_match_type = activity.get("match_type") or "no_match"

        # 1. ID-suffix match
        suffix_id, suffix_name, suffix_conf = match_by_id_suffix(pdf_id, standard_df, project_code=project_code)
        if suffix_id:
            matched_id, matched_name, confidence = suffix_id, suffix_name, suffix_conf
        # 2. AI match if it meets the confidence threshold
        elif ai_match_type != "no_match" and ai_matched_id and ai_confidence >= _CONFIDENCE_THRESHOLD:
            matched_id, matched_name, confidence = ai_matched_id, ai_matched_name, ai_confidence
        # 3. Local fuzzy match fallback
        else:
            match_result = match_activity(pdf_id, pdf_name, standard_df, user_tranche=tranche)
            if match_result["match_type"] == "no_match" or match_result["match_confidence"] < _CONFIDENCE_THRESHOLD:
                unmatched_count += 1
                continue
            matched_id = match_result["matched_id"]
            matched_name = match_result["matched_name"]
            confidence = match_result["match_confidence"]

        _, _, acronym_suffix = parse_standard_id(matched_id)

        # Check for singleton milestones (only one occurrence allowed)
        if acronym_suffix.upper() in SINGLETON_MILESTONES:
            if acronym_suffix.upper() in matched_singletons:
                # Skip duplicate singleton milestone
                print(f"Skipping duplicate singleton milestone: {acronym_suffix} (already matched)")
                continue
            matched_singletons.add(acronym_suffix.upper())

        # Extract tranche from PDF ID for use in output
        pdf_tranche = extract_tranche_from_pdf_id(pdf_id)

        intermediate.append({
            "pdf_id": pdf_id,
            "pdf_activity_name": pdf_name,  # carry through for frontend display
            "matched_std_id": matched_id,
            "matched_name": matched_name,
            "acronym_suffix": acronym_suffix,
            "confidence": confidence,
            "act_start_date": act_start_date,
            "act_end_date": act_end_date,
            "pdf_status": pdf_status,
            "pdf_tranche": pdf_tranche,
        })

    # ── Step 3: Group by acronym_suffix, assign tranches ordered by page number then date ─────
    def _parse_date(date_str: str) -> datetime:
        """Parse date string to datetime, return max if unparseable."""
        for fmt in ("%m/%d/%Y %H:%M", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                pass
        return datetime.max

    def _sort_key(item: dict) -> tuple:
        """
        Sort by page number first, then by date.
        Activities with page_number=None are sorted last within their date group.
        """
        # Page number (lower is first, None goes last)
        page_num = item.get("page_number")
        page_sort = page_num if page_num is not None else 999999
        
        # Date (lower dates first)
        date_str = item["act_start_date"] or item["act_end_date"] or ""
        date_sort = _parse_date(date_str)
        
        return (page_sort, date_sort)

    groups: dict[str, list[dict]] = defaultdict(list)
    for item in intermediate:
        groups[item["acronym_suffix"]].append(item)

    rows: list[dict] = []

    # Extract metro code (first 3 letters of project_code)
    metro_code = project_code[:3].upper()
    
    # Track which PDF IDs we've already processed to avoid duplicates
    processed_pdf_ids: set[str] = set()
    
    for acronym_suffix, items in groups.items():
        sorted_items = sorted(items, key=_sort_key)

        date_to_tranche: dict[datetime, int] = {}
        next_tranche = base_tranche_num
        best: dict[str, dict] = {}

        for item in sorted_items:
            # Skip if this PDF ID was already processed (prevents duplicate matches from overwriting)
            pdf_id = item.get("pdf_id", "")
            if pdf_id and pdf_id in processed_pdf_ids:
                continue
            processed_pdf_ids.add(pdf_id)
            
            # Use page number + date as the unique key for tranche assignment
            page_num = item.get("page_number")
            date_str = item["act_start_date"] or item["act_end_date"] or ""
            date_key = (page_num if page_num is not None else 999999, _parse_date(date_str))
            
            # Use tranche from PDF ID if available, otherwise use sequential tranche
            pdf_tranche = item.get("pdf_tranche")
            if pdf_tranche:
                # Use the tranche extracted from PDF ID
                tranche_num = int(pdf_tranche)
            else:
                # Use sequential tranche assignment
                if date_key not in date_to_tranche:
                    date_to_tranche[date_key] = next_tranche
                    next_tranche += 1
                tranche_num = date_to_tranche[date_key]

            # Use metro code for activity ID: {metro}-{year}-{tranche}-{acronym_suffix}
            task_code = f"{metro_code}-{year}-{tranche_num:02d}-{acronym_suffix}"
            if task_code not in best or item["confidence"] > best[task_code]["confidence"]:
                best[task_code] = item

        for task_code, item in best.items():
            std_entry = std_by_id.get(item["matched_std_id"], {})
            pdf_status = item["pdf_status"]
            
            # Determine activity status - use complete_pct as fallback
            complete_pct = item.get("complete_pct")
            if pdf_status and pdf_status.lower() not in ["", "nan", "none"]:
                activity_status = extract_status_from_pdf(pdf_status, complete_pct)
            else:
                activity_status = extract_status_from_pdf("", complete_pct)
            
            # Extract activity type from task code
            activity_type = extract_activity_type(task_code)
            
            # Check if this is a milestone activity
            is_milestone = is_milestone_activity(acronym_suffix)

            # Build WBS ID — for milestones use the activity's own tranche from task_code
            # (e.g. BKK-22-02-RFS-F → tranche "02" → UD.1.2), not the form-level tranche
            task_parts = task_code.split('-')
            activity_tranche = task_parts[2] if len(task_parts) >= 3 else tranche
            wbs_id = build_wbs_id(project_code, activity_tranche, data_date, is_milestone)

            # Primary constraint type
            primary_constraint = get_primary_constraint(activity_type)

            # Primary Constraint Date — use whichever date the PDF provided
            if activity_type == "Start Milestone":
                cstr_date = item["act_start_date"] or item["act_end_date"] or ""
            elif activity_type == "Finish Milestone":
                cstr_date = item["act_end_date"] or item["act_start_date"] or ""
            else:
                cstr_date = item["act_start_date"] or item["act_end_date"] or ""

            # Actual Start/Finish — only for completed activities
            if activity_status == "Completed":
                if activity_type == "Start Milestone":
                    actual_start  = item["act_start_date"] or cstr_date
                    actual_finish = actual_start  # set equal so P6 doesn't auto-fill with data date
                elif activity_type == "Finish Milestone":
                    actual_finish = item["act_end_date"] or cstr_date
                    actual_start  = actual_finish  # set equal so P6 doesn't auto-fill with data date
                else:
                    actual_start  = item["act_start_date"] or cstr_date
                    actual_finish = item["act_end_date"] or cstr_date
            else:
                actual_start  = ""
                actual_finish = ""

            rows.append({
                "task_code":                  task_code,
                "status_code":                activity_status,
                "wbs_id":                     wbs_id,
                "actv_code_activity_type_id": "",
                "task_name":                  item["matched_name"] or std_entry.get("task_name", ""),
                "cstr_type":                  primary_constraint,
                "cstr_date":                  cstr_date,
                "act_start_date":             actual_start,
                "act_end_date":               actual_finish,
                "delete_record_flag":         "",
                "pdf_activity_name":          item.get("pdf_activity_name", ""),
            })

    print(f"AI Extraction complete: {len(rows)} output rows, {unmatched_count} unmatched/low-confidence (skipped)")
    # rows contains pdf_activity_name for the frontend preview; the DataFrame filters to P6_COLUMNS only
    return pd.DataFrame(rows, columns=P6_COLUMNS), all_activities_for_debug, rows


def save_p6_xlsx(df: pd.DataFrame, path: str) -> None:
    """
    Save the result DataFrame as an Excel file with:
      - Sheet "TASK": Row 1 (codes), Row 2 (headers), Row 3+ (data)
    This matches the exact format needed for P6 import.
    """
    df_out = df.copy()

    # P6 requires date columns to be Excel datetime serial values, not text strings.
    # Re-parse our formatted strings back to datetime objects so openpyxl writes
    # proper date cells. Empty strings become NaT → blank cell.
    date_cols = ["cstr_date", "act_start_date", "act_end_date"]
    for col in date_cols:
        if col in df_out.columns:
            df_out[col] = pd.to_datetime(df_out[col], format="%m/%d/%Y %H:%M", errors="coerce")

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        # TASK sheet only — omitting USERDATA avoids P6's "user not found" warning.
        # Row 1: internal field names (read by P6 importer)
        pd.DataFrame([P6_COLUMNS]).to_excel(
            writer, sheet_name="TASK", index=False, header=False, startrow=0
        )
        # Row 2: display captions (shown in P6 column headers)
        pd.DataFrame([P6_HEADERS]).to_excel(
            writer, sheet_name="TASK", index=False, header=False, startrow=1
        )
        # Row 3+: data
        df_out.to_excel(writer, sheet_name="TASK", index=False, header=False, startrow=2)