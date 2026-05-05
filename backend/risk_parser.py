"""
Risk Parser — Schedule Assessment Excel → AI-categorised risk candidates.

Reads each sheet with pandas, converts to structured text, sends to DeepSeek
for parsing and risk categorisation, returns a list of risk candidates.
"""

import io
import json
import logging
import os
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Ordered list used in the AI prompt and for validation
RISK_CATEGORIES = [
    "Schedule Logic",
    "Scope Definition",
    "Schedule Quality",
    "Duration Compression",
    "Close-out Compression",
    "Execution Overlap",
    "Milestone Slippage",
    "Milestone Compliance",
    "Schedule Integrity",
    "Logic Gap",
    "Critical Path Exposure",
    "Scope Granularity",
]

# Sheets we know how to parse — others are included but deprioritised
KNOWN_SHEETS = [
    "Lease Overview",
    "LPSS",
    "Duration Benchmark",
    "Milestones Assessment",
    "Schedule Health",
    "Critical Path Analysis",
    "Scope Check",
]

SYSTEM_PROMPT = """You are a senior schedule risk analyst for large data centre and construction projects.

You will receive the content of a schedule assessment Excel workbook, each sheet separated by a header.
Your task:
1. Identify every finding that indicates a schedule, scope, or quality risk.
2. Assign each finding to exactly one risk category from the list below.
3. CONSOLIDATE AGGRESSIVELY: produce at most ONE risk item per category. All findings of the same category — regardless of tranche, sheet, or row — must be merged into a single risk item. Cite all affected tranches and data points in the evidence field.
   - BAD: "USC missing T5-T6" + "USC missing T7-T8" + "USC missing T9-T10" as 3 separate Milestone Compliance risks
   - BAD: "MEP compression T5" + "CXL4 compression T7" as 2 separate Duration Compression risks
   - GOOD: one Milestone Compliance risk covering T5-T10 with all USC evidence combined
   - GOOD: one Duration Compression risk covering MEP T5 (-48%) and CXL4 T7 (-64%) in the evidence
4. Assign confidence (0.0–1.0): >0.8 = clear numeric evidence; 0.6–0.8 = qualitative finding; <0.6 = inferred.

Risk categories (use these exact strings):
- Schedule Logic: incorrect critical path, wrong sequencing
- Scope Definition: missing scope items, activities not split by hall/trade/system
- Schedule Quality: incomplete schedule, missing activities, poor detail level
- Duration Compression: activity duration significantly below benchmark (>20% shorter)
- Close-out Compression: close-out / CXL4 activities compressed
- Execution Overlap: activity duration significantly above benchmark (inflated / overlapping)
- Milestone Slippage: milestones slipped vs baseline dates
- Milestone Compliance: required milestones missing from schedule
- Schedule Integrity: high float, poor connectivity, disconnected network
- Logic Gap: missing logical dependencies between activity groups
- Critical Path Exposure: zero-float chain or compressed critical path sequence
- Scope Granularity: activities not split finely enough by datahall, trade, or phase

Output: return ONLY a valid JSON array, no markdown, no explanation. Each element:
{
  "source_sheet": "<sheet name(s)>",
  "source_finding": "<concise reference to the specific row/cell that triggered this risk>",
  "category": "<category from list above>",
  "risk_name": "<clear risk description covering all affected tranches, max 120 chars>",
  "type": "Threat",
  "shape": "Triangular",
  "evidence": "<all supporting data combined: numbers, variances, dates, all tranche details>",
  "confidence": <0.0–1.0>,
  "current_probability": "<Very High | High | Medium | Low | Negligible>",
  "current_schedule":    "<Very High | High | Medium | Low | Negligible>"
}

Rules:
SCORING SCALE — use integers 1–6 for all scoring fields:
  1 = Negligible  2 = Very Low  3 = Low  4 = Medium  5 = High  6 = Very High

PROBABILITY SCORING from LPSS sheet (schedule risks only):
  For each LPSS risk row: probability_ratio = row_score / row_weight_pct
  ratio > 0.80 → 1  |  0.60–0.80 → 2  |  0.40–0.60 → 3  |  0.20–0.40 → 4  |  0.10–0.20 → 5  |  < 0.10 → 6
  (Inverted: high ratio = performing well = low risk. High ratio = doing well = low score.)
  When multiple LPSS rows are consolidated, use the maximum ratio across rows.

SCHEDULE IMPACT SCORING from LPSS sheet:
  impact_ratio = row_score / row_weight_pct
  ratio > 0.80 → 1  |  0.60–0.80 → 2  |  0.40–0.60 → 3  |  0.20–0.40 → 4  |  0.10–0.20 → 5  |  < 0.10 → 6

For risks from other sheets (Duration Benchmark, Milestones, Critical Path, etc.):
  Variance > 40% or slippage > 120 days → probability 5, schedule 5
  Variance 20–40% or slippage 60–120 days → 4 / 4
  Variance < 20% or slippage < 60 days → 3 / 3

If a construction risk register is provided, cross-reference similar risk categories to calibrate the 1–6 values — do not override the LPSS formula above.

Add two additional fields to every output item:
  "current_probability": integer 1–6 (1=Negligible … 6=Very High)
  "current_schedule":    integer 1–6 (same scale)

Rules:
- Maximum 12 risk items — one per category at most. If a category has no findings, omit it entirely.
- Every item must have all ten fields (including current_probability and current_schedule).
- Only use category strings from the list above exactly.
- Never create two items with the same category value.
- Do not include opportunities unless clearly indicated in the data.
"""


CONSTRUCTION_RISK_PROMPT = """You are parsing a construction risk register Excel workbook.
Extract risk records and return a JSON array.

Each element must have exactly these fields:
{
  "risk_name":           "<short title or name of the risk>",
  "category":            "<risk category as written in the register>",
  "evidence":            "<detailed description or notes about the risk>",
  "current_probability": <integer 1–6 from the probability rating column, or 0 if blank/missing>,
  "current_schedule":    <integer 1–6 from the schedule impact rating column, or 0 if blank/missing>
}

Scoring scale: 1=Negligible, 2=Very Low, 3=Low, 4=Medium, 5=High, 6=Very High.

Rules:
- ONLY include risks whose status column shows "Open" (or equivalent active status). Skip any row where status is "Closed", "Resolved", "Mitigated", "Done", or similar.
- If there is no status column, include all rows that have a risk title or name.
- Read the numeric probability and impact values exactly as they appear in the register (columns labelled "Risk probability rating", "probability", or similar).
- If a probability or impact cell is empty or non-numeric, output 0.
- Output ONLY a valid JSON array. No markdown fences, no explanation.
"""


CONSTRUCTION_RISK_FROM_IMAGE_PROMPT = """You are parsing a construction risk register from a screenshot or image of a risk register slide/page.

Extract risk records and return a JSON array. Focus on the "Open Issues", "Risks", and "Blockers" section if visible.

Each element must have exactly these fields:
{
  "risk_name":           "<short title or name of the risk>",
  "category":            "<max 2 words — derived by summarising the description, e.g. 'Permit Delay', 'Supply Chain', 'Resource Constraint', 'Scope Change', 'Weather Risk'>",
  "description":         "<full description text from the risk register — include all detail>",
  "target_close_date":   "<the target close / target resolution date if visible, e.g. 'May 30, 2026' — leave empty string if not found>",
  "current_probability": <integer 1–6 from probability rating, or 0 if missing/not readable>,
  "current_schedule":     <integer 1–6 from schedule impact, or 0 if missing/not readable>
}

STATUS LOGIC — determine status by comparing dates:
- assessment_date = "{assessment_date}" (the date the user is conducting this assessment)
- If assessment_date < target_close_date → status = "Open"
- If assessment_date >= target_close_date → status = "Closed"
- Only return risks where status = "Open"
- If target_close_date is not visible/readable, include the risk (treat as open)

NOTE: in your JSON output, fields like "target_close_date" will contain literal date strings with {{ }} curly braces — output them exactly as shown.

CATEGORY ASSIGNMENT (max 2 words):
Summarise the risk description into 2 words maximum. Examples:
- "Delay in obtaining building permit from local authority" → "Permit Delay"
- "Critical components on long lead time" → "Supply Chain"
- "Shortage of skilled labour on site" → "Resource Constraint"
- "Scope not fully defined at this stage" → "Scope Change"
- "Adverse weather conditions expected in monsoon season" → "Weather Risk"
- "Design changes requested by client" → "Design Change"
- "Budget overrun on MEP works" → "Budget Risk"

Scoring scale: 1=Negligible, 2=Very Low, 3=Low, 4=Medium, 5=High, 6=Very High.

Rules:
- Extract risks from the "Open Issues", "Risks", or "Blockers" section if present.
- Only include Open risks (apply the date logic above).
- If probability or impact scores are not readable from the image, output 0.
- Output ONLY a valid JSON array. No markdown fences, no explanation.
- The image may be a slide screenshot — look for tables, lists, or structured text.
"""


def extract_construction_risks_via_ai(register_bytes: bytes) -> dict:
    """
    Send the construction risk register to DeepSeek for structured extraction.
    Returns {"risks": [...], "manual_scoring_required": bool}
    """
    from backend.ai_client import get_openai_client, _get_deployment

    # Debug: log file magic bytes to diagnose format issues
    magic = register_bytes[:8]
    logger.info("Construction register file magic bytes: %s (hex: %s)", magic, magic.hex())
    if magic[:4] == b'PK\x03\x04':
        logger.info("File appears to be a valid ZIP-based format (xlsx/docx)")
    elif magic[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        logger.info("File appears to be OLE2 format (xls/doc)")
    else:
        logger.warning("Unknown file format - first 20 bytes: %s", register_bytes[:20].hex())

    sheets = read_excel_sheets(register_bytes, max_rows=1000)
    if not sheets:
        logger.warning("Construction risk register has no readable sheets")
        return {"risks": [], "manual_scoring_required": False}

    # Build text content from all sheets — large slice to capture full register
    parts = ["=== CONSTRUCTION RISK REGISTER ==="]
    for sheet_name, text in sheets.items():
        parts.append(f"\n-- Sheet: {sheet_name} --\n{text[:20000]}")
    content = "\n".join(parts)

    client = get_openai_client()
    deployment = _get_deployment()

    logger.info("Sending construction register to %s for extraction (%d chars)", deployment, len(content))

    response = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": CONSTRUCTION_RISK_PROMPT},
            {"role": "user",   "content": content},
        ],
        max_tokens=16000,
        temperature=0.0,
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        parts_md = raw.split("```")
        raw = parts_md[1] if len(parts_md) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # Robust JSON extraction: find the first `[` and last `]` to handle
    # cases where the AI includes prose before/after the JSON array.
    json_start = raw.find("[")
    json_end = raw.rfind("]")
    if json_start != -1 and json_end != -1 and json_end > json_start:
        raw = raw[json_start : json_end + 1]
        logger.info("Trimmed AI response to JSON array bounds (%d chars)", len(raw))
    else:
        logger.warning("Could not locate JSON array bounds in AI response: %s", raw[:200])

    extracted: list[dict] = json.loads(raw)

    # Merge rows with the same risk_name (case-insensitive) — combine evidence,
    # keep the highest probability and schedule scores seen across duplicates.
    merged: dict[str, dict] = {}
    for item in extracted:
        key = item.get("risk_name", "").strip().lower()
        if not key:
            continue
        prob = int(item.get("current_probability", 0) or 0)
        sched = int(item.get("current_schedule", 0) or 0)
        if key not in merged:
            merged[key] = {
                "risk_name":           item.get("risk_name", "").strip(),
                "category":            item.get("category", "Construction Risk"),
                "evidence":            item.get("evidence", ""),
                "current_probability": prob,
                "current_schedule":    sched,
            }
        else:
            existing = merged[key]
            # Combine evidence snippets (deduplicate)
            ev_parts = [p.strip() for p in existing["evidence"].split(" | ")] if existing["evidence"] else []
            new_ev = item.get("evidence", "").strip()
            if new_ev and new_ev not in ev_parts:
                ev_parts.append(new_ev)
            existing["evidence"] = " | ".join(ev_parts)
            # Keep the highest scores
            if prob > existing["current_probability"]:
                existing["current_probability"] = prob
            if sched > existing["current_schedule"]:
                existing["current_schedule"] = sched

    risks = []
    for m in merged.values():
        risks.append({
            "risk_source":         "construction",
            "source_sheet":        "Risk Register",
            "source_finding":      m["risk_name"],
            "category":            m["category"],
            "risk_name":           m["risk_name"],
            "type":                "Threat",
            "shape":               "Triangular",
            "evidence":            m["evidence"],
            "confidence":          0.95,
            "current_probability": m["current_probability"],
            "current_schedule":    m["current_schedule"],
        })

    manual_scoring_required = any(
        r["current_probability"] == 0 or r["current_schedule"] == 0 for r in risks
    )
    logger.info("Extracted %d construction risks via AI after merge (manual_scoring=%s)", len(risks), manual_scoring_required)
    return {"risks": risks, "manual_scoring_required": manual_scoring_required}


def _sheet_to_text(df: pd.DataFrame, max_rows: int = 120) -> str:
    """Convert a DataFrame to clean pipe-separated text, skipping blank rows."""
    df = df.fillna("")
    # Drop rows that are entirely blank
    df = df[df.apply(lambda r: any(str(v).strip() for v in r), axis=1)]
    df = df.head(max_rows)
    lines = []
    for _, row in df.iterrows():
        parts = [str(v).strip() for v in row if str(v).strip()]
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)


def read_excel_sheets(file_bytes: bytes, max_rows: int = 120) -> "dict[str, str]":
    """
    Read all sheets from an Excel file and convert to text.
    Tries openpyxl → xlrd → HTML fallback to handle .xls files that are
    actually XLSX or HTML disguised with a .xls extension.
    max_rows controls how many data rows are included per sheet.
    """
    buf = io.BytesIO(file_bytes)

    # Try proper Excel engines first
    for engine in ("openpyxl", "xlrd", None):
        try:
            buf.seek(0)
            kwargs = {"engine": engine} if engine else {}
            xls = pd.ExcelFile(buf, **kwargs)
            result = {}
            ordered = [s for s in KNOWN_SHEETS if s in xls.sheet_names]
            others  = [s for s in xls.sheet_names if s not in KNOWN_SHEETS]
            for sheet_name in ordered + others:
                try:
                    df = pd.read_excel(xls, sheet_name=sheet_name, header=None)
                    text = _sheet_to_text(df, max_rows=max_rows)
                    if text.strip():
                        result[sheet_name] = text
                except Exception:
                    pass
            if result:
                return result
        except Exception:
            continue

    # Fallback 1: direct xlrd.open_workbook (handles BIFF4 and other edge cases)
    try:
        import xlrd as _xlrd
        buf.seek(0)
        book = _xlrd.open_workbook(file_contents=buf.read())
        result = {}
        for sname in book.sheet_names():
            sh = book.sheet_by_name(sname)
            lines = []
            for rx in range(min(sh.nrows, max_rows)):
                parts = [str(sh.cell_value(rx, cx)).strip()
                         for cx in range(sh.ncols)
                         if str(sh.cell_value(rx, cx)).strip()]
                if parts:
                    lines.append(" | ".join(parts))
            if lines:
                result[sname] = "\n".join(lines)
        if result:
            logger.info("Read file via direct xlrd (%d sheets)", len(result))
            return result
    except Exception as e:
        logger.warning("Direct xlrd failed: %s", e)

    # Fallback 2: olefile — extract raw BIFF stream from OLE2 container directly,
    # bypassing xlrd's own OLE2 parser which fails on some non-standard structures.
    try:
        import olefile as _olefile
        import xlrd as _xlrd2
        buf.seek(0)
        ole = _olefile.OleFileIO(buf)
        workbook_data = None
        for entry in ole.listdir(streams=True, storages=False):
            name = entry[-1].lower()
            if name in ("workbook", "book"):
                workbook_data = ole.openstream(entry).read()
                break
        if workbook_data:
            book = _xlrd2.open_workbook(file_contents=workbook_data)
            result = {}
            for sname in book.sheet_names():
                sh = book.sheet_by_name(sname)
                lines = []
                for rx in range(min(sh.nrows, max_rows)):
                    parts = [str(sh.cell_value(rx, cx)).strip()
                             for cx in range(sh.ncols)
                             if str(sh.cell_value(rx, cx)).strip()]
                    if parts:
                        lines.append(" | ".join(parts))
                if lines:
                    result[sname] = "\n".join(lines)
            if result:
                logger.info("Read file via olefile+xlrd (%d sheets)", len(result))
                return result
    except Exception as e:
        logger.warning("olefile+xlrd fallback failed: %s", e)

    # Fallback 3: XLSB format (binary Excel)
    try:
        import pyxlsb
        buf.seek(0)
        result = {}
        with pyxlsb.open_workbook(buf) as wb:
            for sname in wb.sheets:
                lines = []
                with wb.get_sheet(sname) as sheet:
                    for row in list(sheet.rows())[:max_rows]:
                        parts = [str(c.v or "").strip() for c in row if str(c.v or "").strip()]
                        if parts:
                            lines.append(" | ".join(parts))
                if lines:
                    result[sname] = "\n".join(lines)
        if result:
            logger.info("Read file as XLSB (%d sheets)", len(result))
            return result
    except Exception as e:
        logger.warning("pyxlsb failed: %s", e)

    # Fallback 4: HTML-based XLS
    try:
        buf.seek(0)
        tables = pd.read_html(buf, flavor="lxml")
        result = {}
        for i, df in enumerate(tables):
            text = _sheet_to_text(df.fillna("").astype(str), max_rows=max_rows)
            if text.strip():
                result[f"Sheet{i + 1}"] = text
        if result:
            logger.info("Read file as HTML table (%d table(s))", len(result))
            return result
    except Exception as e:
        logger.warning("HTML fallback also failed: %s", e)

    return {}


def parse_assessment(
    file_bytes: bytes,
    project_code: str,
    tranches: str,
    assessment_date: str,
    risk_register_bytes: Optional[bytes] = None,
    kb_corrections: Optional[list] = None,
) -> dict:
    """
    Parse a schedule assessment Excel and return AI-categorised risk candidates.
    Each candidate is a dict ready for the frontend preview table.
    kb_corrections: list of dicts from risk_knowledge_base.get_risk_corrections()
    """
    from backend.ai_client import get_openai_client, _get_deployment

    # ── 1. Extract construction risks from register (before AI call) ─────────
    construction_risks: list[dict] = []
    manual_scoring_required = False

    if risk_register_bytes:
        # Detect PNG by magic bytes
        if risk_register_bytes[:4] == b'\x89PNG':
            logger.info("Detected PNG risk register — using image extraction path")
            cr = extract_construction_risks_from_image(risk_register_bytes, assessment_date)
        else:
            cr = extract_construction_risks_via_ai(risk_register_bytes)
        construction_risks = cr["risks"]
        manual_scoring_required = cr["manual_scoring_required"]

    # ── 2. Parse schedule risks from assessment Excel via AI ──────────────────
    sheets = read_excel_sheets(file_bytes)
    if not sheets:
        raise ValueError("No readable sheets found in the uploaded Excel file.")

    parts = [f"PROJECT: {project_code.upper()}   TRANCHES: {tranches}   DATE: {assessment_date}\n"]

    # Inject KB corrections so the AI pre-applies verified scores
    if kb_corrections:
        keep = [c for c in kb_corrections if c.get("action") != "remove"]
        remove = [c for c in kb_corrections if c.get("action") == "remove"]
        kb_lines = ["=== KNOWLEDGE BASE CORRECTIONS FROM PREVIOUS REVIEWS ===",
                    "Apply these verified scores for matching finding patterns and "
                    "exclude any findings marked REMOVE:\n"]
        for c in keep:
            kb_lines.append(
                f"  [KEEP] {c.get('category','')} — \"{c.get('source_finding','')}\" "
                f"→ Probability {c.get('current_probability',0)}, "
                f"Schedule {c.get('current_schedule',0)}"
            )
        for c in remove:
            kb_lines.append(f"  [REMOVE] \"{c.get('source_finding','')}\" — exclude this finding type")
        parts.insert(0, "\n".join(kb_lines) + "\n")
        logger.info("Injected %d KB corrections (%d keep, %d remove) into prompt",
                    len(kb_corrections), len(keep), len(remove))

    for sheet_name, text in sheets.items():
        parts.append(f"\n=== {sheet_name} ===\n{text[:3000]}")

    content = "\n".join(parts)
    client = get_openai_client()
    deployment = _get_deployment()

    logger.info("Sending assessment to %s for parsing (%d chars)", deployment, len(content))

    response = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        max_tokens=6000,
        temperature=0.1,
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        parts_md = raw.split("```")
        raw = parts_md[1] if len(parts_md) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # Robust JSON extraction: find the first `[` and last `]` to handle
    # cases where the AI includes prose before/after the JSON array.
    json_start = raw.find("[")
    json_end = raw.rfind("]")
    if json_start != -1 and json_end != -1 and json_end > json_start:
        raw = raw[json_start : json_end + 1]
        logger.info("Trimmed schedule AI response to JSON array bounds (%d chars)", len(raw))
    else:
        logger.warning("Could not locate JSON array bounds in schedule AI response: %s", raw[:200])

    schedule_candidates: list[dict] = json.loads(raw)
    schedule_candidates = _consolidate_duplicates(schedule_candidates)

    # ── 3. Normalise schedule risks ────────────────────────────────────────────
    def _blank_scoring_fields(c: dict):
        c["current_cost"] = ""
        c["current_score"] = ""
        c["mitigation_description"] = ""
        c["mitigation_duration"] = ""
        c["mitigation_cost"] = ""
        c["mitigated_probability"] = ""
        c["mitigated_schedule"] = ""
        c["mitigated_cost"] = ""
        c["mitigated_score"] = ""
        c["_removed"] = False

    for i, c in enumerate(schedule_candidates):
        c["id"] = f"S{i + 1}"
        c["risk_source"] = "schedule"
        c.setdefault("source_sheet", "")
        c.setdefault("source_finding", "")
        c.setdefault("category", "")
        c.setdefault("risk_name", "")
        c.setdefault("type", "Threat")
        c.setdefault("shape", "Triangular")
        c.setdefault("evidence", "")
        c.setdefault("confidence", 0.0)
        c.setdefault("current_probability", 0)
        c.setdefault("current_schedule", 0)
        _blank_scoring_fields(c)
        c["_idx"] = i

    # ── 4. Normalise construction risks ────────────────────────────────────────
    offset = len(schedule_candidates)
    for i, c in enumerate(construction_risks):
        c["id"] = f"C{i + 1}"
        _blank_scoring_fields(c)
        c["_idx"] = offset + i

    all_risks = schedule_candidates + construction_risks

    logger.info(
        "Total risks for %s: %d schedule + %d construction",
        project_code, len(schedule_candidates), len(construction_risks),
    )
    return {"risks": all_risks, "manual_scoring_required": manual_scoring_required}


def extract_construction_risks_from_image(image_bytes: bytes, assessment_date: str) -> dict:
    """
    Extract construction risks from a PNG screenshot of a risk register.
    Uses Azure Document Intelligence to extract text from the image,
    then sends to DeepSeek for structured extraction with date-driven status logic.
    Returns {"risks": [...], "manual_scoring_required": bool}
    """
    from backend.ai_client import get_doc_intel_client, get_openai_client, _get_deployment
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

    # Extract text from PNG using Document Intelligence layout model
    client = get_doc_intel_client()
    request = AnalyzeDocumentRequest(bytes_source=image_bytes)
    poller = client.begin_analyze_document(
        model_id="prebuilt-layout",
        body=request,
    )
    result = poller.result()

    # Build text content from document analysis
    text_parts = []
    if hasattr(result, "content") and result.content:
        text_parts.append(result.content)

    # Also collect per-page text
    if hasattr(result, "pages"):
        for page in result.pages:
            if hasattr(page, "lines"):
                for line in page.lines:
                    if hasattr(line, "content") and line.content:
                        text_parts.append(line.content)
            if hasattr(page, "paragraphs"):
                for para in page.paragraphs:
                    if hasattr(para, "content") and para.content:
                        text_parts.append(para.content)

    image_text = "\n".join(text_parts)
    logger.info("Extracted %d chars from PNG via Document Intelligence", len(image_text))

    if not image_text.strip():
        logger.warning("No text extracted from PNG image")
        return {"risks": [], "manual_scoring_required": False}

    # Build system prompt with assessment_date injected
    system_prompt = CONSTRUCTION_RISK_FROM_IMAGE_PROMPT.replace("{assessment_date}", assessment_date)

    # Send to DeepSeek for structured extraction
    ai_client = get_openai_client()
    deployment = _get_deployment()

    logger.info("Sending PNG extracted text to %s for risk extraction (%d chars)",
                 deployment, len(image_text))

    response = ai_client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": image_text},
        ],
        max_tokens=8000,
        temperature=0.0,
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        parts_md = raw.split("```")
        raw = parts_md[1] if len(parts_md) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # Robust JSON extraction: find the first `[` and last `]` to handle
    # cases where the AI includes prose before/after the JSON array.
    json_start = raw.find("[")
    json_end = raw.rfind("]")
    if json_start != -1 and json_end != -1 and json_end > json_start:
        raw = raw[json_start : json_end + 1]
        logger.info("Trimmed PNG AI response to JSON array bounds (%d chars)", len(raw))
    else:
        logger.warning("Could not locate JSON array bounds in PNG AI response: %s", raw[:200])

    # Validate trimmed result is a reasonable length before parsing
    if len(raw) < 20:
        logger.warning(
            "Trimmed JSON from PNG AI response is suspiciously short (%d chars). "
            "Full raw response: %s",
            len(raw), raw[:500]
        )
        return {"risks": [], "manual_scoring_required": False}

    # Parse with explicit error handling — log raw response for debugging
    try:
        extracted: list[dict] = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(
            "Failed to parse JSON from PNG AI response (pos %d, col %d): %s. "
            "Raw response (first 1000 chars): %s",
            e.pos, e.lineno if hasattr(e, "lineno") else -1, e.msg, raw[:1000]
        )
        return {"risks": [], "manual_scoring_required": False}

    # Normalise to standard risk format
    risks = []
    for item in extracted:
        # The AI may return "description" or "evidence" field from image
        description = item.get("description", "") or item.get("evidence", "")
        risks.append({
            "risk_source":         "construction",
            "source_sheet":        "PNG Image",
            "source_finding":      item.get("risk_name", ""),
            "category":            item.get("category", "Construction Risk"),
            "risk_name":           item.get("risk_name", ""),
            "type":                "Threat",
            "shape":               "Triangular",
            "evidence":            description,
            "confidence":          0.85,
            "current_probability": int(item.get("current_probability", 0) or 0),
            "current_schedule":    int(item.get("current_schedule", 0) or 0),
        })

    manual_scoring_required = any(
        r["current_probability"] == 0 or r["current_schedule"] == 0 for r in risks
    )
    logger.info("Extracted %d risks from PNG image (manual_scoring=%s)",
                len(risks), manual_scoring_required)
    return {"risks": risks, "manual_scoring_required": manual_scoring_required}


def _consolidate_duplicates(candidates: list[dict]) -> list[dict]:
    """
    Safety net: merge any remaining risks that share the same category into one item.
    One category = one risk, regardless of source sheet or tranche.
    """
    from collections import defaultdict

    groups: defaultdict[str, list[dict]] = defaultdict(list)
    # Preserve insertion order via a separate list
    seen_categories: list[str] = []
    for c in candidates:
        cat = c.get("category", "")
        if cat not in groups:
            seen_categories.append(cat)
        groups[cat].append(c)

    merged = []
    for cat in seen_categories:
        group = groups[cat]
        if len(group) == 1:
            merged.append(group[0])
            continue

        # Sort by confidence descending — highest-quality item leads the merged entry
        group.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        lead = dict(group[0])

        # Combine all unique evidence snippets
        all_evidence = [g.get("evidence", "") for g in group if g.get("evidence", "")]
        lead["evidence"] = " | ".join(dict.fromkeys(all_evidence))

        # Combine all source findings
        all_findings = [g.get("source_finding", "") for g in group if g.get("source_finding", "")]
        lead["source_finding"] = "; ".join(dict.fromkeys(all_findings))

        # List all unique source sheets
        all_sheets = [g.get("source_sheet", "") for g in group if g.get("source_sheet", "")]
        lead["source_sheet"] = ", ".join(dict.fromkeys(all_sheets))

        lead["confidence"] = max(g.get("confidence", 0) for g in group)

        logger.info("Consolidated %d '%s' risks into 1", len(group), cat)
        merged.append(lead)

    return merged
