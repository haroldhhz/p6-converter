# P6 Schedule Converter — v3.0 Build Guide

## Schedule Risk Manager Integration

**Stack:** Azure Document Intelligence · DeepSeek v4-flash · Azure Blob Storage · Python · pandas · openpyxl

---

## What Changes in v3.0

The P6 Schedule Converter becomes a multi-function platform. A left navigation pane is added to the UI, exposing two top-level functions:

```
┌──────────────────────┬──────────────────────────────────────────────────┐
│                      │                                                  │
│  ◉ IMS Generator     │   [Active function workspace]                    │
│                      │                                                  │
│  ○ Schedule Risk     │   IMS Generator: same as v2.0 — upload PDF,     │
│    Manager           │   extract, match, review, save KB, download XLSX │
│                      │                                                  │
│                      │   Schedule Risk Manager: upload schedule         │
│                      │   assessment Excel, parse, categorize, score,    │
│                      │   review, export Acumen Fuse risk register       │
│                      │                                                  │
└──────────────────────┴──────────────────────────────────────────────────┘
```

| Function | Purpose | Input | Output |
|---|---|---|---|
| **1 — IMS Generator** *(existing v2.0)* | Convert P6/MSP schedule PDF into IMS-ready XLSX | Schedule export PDF | P6-import-ready XLSX (TASK sheet) |
| **2 — Schedule Risk Manager** *(new)* | Parse schedule assessment, categorize risks, score in Acumen Fuse format | IMS schedule assessment Excel + user-submitted scoring knowledge | Acumen Fuse risk register Excel |

Both functions share the same Azure infrastructure, project code conventions, and Knowledge Base storage layer.

---

## Navigation Pane Specification

### Layout

- Fixed left sidebar, always visible, ~240px wide.
- Logo/app title at top: **P6 Schedule Converter**.
- Two nav items below, each with an icon and label.
- Active item highlighted; workspace area on the right switches content without page reload.

### Nav Items

| Order | Label | Icon | Route |
|---|---|---|---|
| 1 | IMS Generator | 📄 | `/app/ims-generator` |
| 2 | Schedule Risk Manager | ⚠️ | `/app/risk-manager` |

### Behaviour

- Default landing: IMS Generator (preserves current user experience).
- Switching functions does not lose in-progress work in the other (state retained in memory).
- Project Code entered in either function is shared — if a user uploads HYD20 in IMS Generator, Schedule Risk Manager pre-fills HYD20.

---

## Function 1 — IMS Generator *(unchanged from v2.0)*

No changes to the existing six-stage pipeline:

```
KB Retrieval → PDF Extraction → AI Data Extraction → Standard Reference Matching → Tranche & ID Assignment → XLSX Generation
```

The SOP, Knowledge Base flywheel, preview/edit workflow, and P6 import process remain exactly as documented in the v2.0 SOP.

---

## Function 2 — Schedule Risk Manager *(new in v3.0)*

### Architecture Overview

The Schedule Risk Manager ingests a multi-sheet schedule assessment Excel, uses AI to organize scattered findings into structured risk items by category, and scores each risk in Acumen Fuse risk register format. Scoring knowledge is supplied by the user — not retrieved from an external search index.

```
Schedule Assessment Excel ─┐
                           ├→ AI Parsing → Categorization → Scoring → Review → Acumen Fuse Risk Register
User-Submitted Knowledge ──┘
```

---

### Scoring Knowledge — User-Submitted

Instead of an automated retrieval layer, the user provides scoring guidance that the AI uses as context when assigning probability and impact. This follows the same pattern as the IMS Generator's Knowledge Base — user-supplied, project-specific, and injected into the AI prompt.

**What the user can submit:**

| Knowledge Type | Example | How the AI Uses It |
|---|---|---|
| **Historical risk register** | A previous Acumen Fuse export for this project or a similar project | Uses as scoring precedent — "MEP compression on HYD18 was scored High/High" |
| **Scoring criteria document** | A table defining what constitutes Negligible vs. Low vs. Medium vs. High vs. Very High for probability, schedule impact, and cost impact | Applies as the scoring rubric for all candidates |
| **Category definitions** | List of risk categories with descriptions and typical score ranges | Uses to validate category assignments and calibrate scores |
| **Project-specific notes** | Free-text instructions (e.g. "CXL4 compression on this site is acceptable — score Low") | Overrides default scoring for specific finding types |

**How it works:**

- User uploads knowledge files (Excel, PDF, or text) via a dedicated **"📚 Upload Scoring Knowledge"** panel in the Risk Manager workspace.
- Files are stored in Azure Blob Storage under `risk-knowledge/{project_code}/`.
- On each run, the app retrieves all knowledge files for the project and injects them into the DeepSeek v4-flash system prompt — same pattern as KB corrections in IMS Generator.
- Knowledge is project-specific. HYD20 scoring knowledge only applies to HYD20 assessments.

---

### Input Format — Schedule Assessment Excel

The ingested file contains schedule analysis data spread across multiple sheets with inconsistent structures. The AI must parse and consolidate findings from all of them.

| Sheet | Content | What the AI Extracts |
|---|---|---|
| **Lease Overview** | Site type, tranche list, MW capacity, LP RFS dates | Project metadata, tranche identifiers, target dates |
| **LPSS (Scorecard)** | Pass/fail criteria with scores, weights, comments | Failed criteria → risk candidates; comments → risk descriptions |
| **Duration Benchmark** | Activity durations vs. benchmarks with % variance per tranche | Variance flags → schedule risk candidates (e.g. MEP -48%, CXL4 -64%) |
| **Milestones Assessment** | Milestone compliance, baseline vs. current vs. previous dates, variances | Slipped milestones → risk candidates; non-compliant items → risk candidates |
| **Schedule Health** | Quality findings (float analysis, logic gaps, missing links) | Each finding → risk candidate |
| **Critical Path Analysis** | CP activities with durations, dates, total slack | Zero/negative float activities → risk candidates; compressed sequences → risk candidates |
| **Scope Check** | Missing scope items, activities not split by hall/trade | Scope gaps → risk candidates |

---

### Output Format — Acumen Fuse Risk Register

Each risk must conform to the Acumen Fuse risk register schema:

| Column Group | Fields |
|---|---|
| **Risk** | Enabled (Yes/No), Absolute Mapping (Yes/No), ID (R1, R2…), Type (Threat/Opportunity), Name, Shape (Uniform/Triangular/Trigen) |
| **Current** | Probability, Schedule (impact), Cost (impact), Score |
| **Mitigation** | Enabled (Yes/No), Description, Duration, Cost |
| **Mitigated** | Probability, Schedule (impact), Cost (impact), Score |
| **Calendar** | Calendar assignment |
| **Other** | No Split flag |

Scoring values: Negligible, Low, Medium, High, Very High — or numeric equivalents depending on Acumen Fuse model configuration.

---

### Stage 1 — Upload & Project Binding

User selects **Schedule Risk Manager** in the nav pane.

Upload form fields:

| Field | Required | Notes |
|---|---|---|
| Schedule Assessment Excel | Yes | .xlsx file |
| Project Code | Yes | Pre-filled if already set in IMS Generator (e.g. HYD20) |
| Tranche(s) | Yes | e.g. T5-T10 |
| Assessment Date | Yes | Date of the schedule review |

Optional: Upload scoring knowledge files via the **"📚 Upload Scoring Knowledge"** panel (if not already uploaded for this project).

On submit, the app creates an assessment record, retrieves any existing knowledge and KB corrections for the project, and begins parsing.

---

### Stage 2 — AI Parsing & Structuring

The schedule assessment Excel is not a clean tabular dataset. Data is spread across 10+ sheets with merged cells, free-text findings, mixed date formats (Excel serial numbers), nested headers, and variance calculations embedded in text paragraphs.

**Parsing strategy per sheet type:**

**Structured sheets** (LPSS, Duration Benchmark, Milestones Assessment) — parse tabular data row by row, identify rows that fail criteria or exceed variance thresholds, extract each finding with its numeric context (e.g. "-48% variance, 8.6 vs 16.7 months benchmark").

**Semi-structured sheets** (Schedule Health, Scope Check) — extract free-text bullet findings, associate each with the relevant tranche and activity type.

**Hierarchical sheets** (Critical Path Analysis) — parse the indented activity tree, identify zero-float and compressed-duration items, map them to risk categories.

**Technology:** Azure Document Intelligence extracts the Excel structure. DeepSeek v4-flash reads the extracted content plus any user-submitted scoring knowledge and returns structured risk candidates as JSON.

---

### Stage 3 — Risk Categorization

The AI assigns each extracted finding to a risk category:

| Source Sheet | Example Finding | Risk Category |
|---|---|---|
| LPSS | Critical path incorrect — only CDU design/procurement in path | Schedule Logic |
| LPSS | Activities not split by hall/colo; missing cabling and piping | Scope Definition |
| LPSS | Schedule float analysis — no improvement on high float | Schedule Quality |
| Duration Benchmark | MEP Works -48% variance (8.6 vs 16.7 months) | Duration Compression |
| Duration Benchmark | CXL4 -64% variance (0.6 vs 1.7 months) | Close-out Compression |
| Duration Benchmark | CXL3 +91% variance (6.2 vs 3.2 months) | Execution Overlap |
| Milestones | L1CX start slipped 171 days from baseline | Milestone Slippage |
| Milestones | Milestone not found in schedule (e.g. USC) | Milestone Compliance |
| Schedule Health | 77% high float — schedule not properly linked | Schedule Integrity |
| Schedule Health | MEP not logically linked to CxL2 activities | Logic Gap |
| Critical Path | Zero-float chain: design → procurement → MEP → RFS | Critical Path Exposure |
| Scope Check | MEP works not split into datahall or trades | Scope Granularity |

**Consolidation:** Where multiple sheets flag the same underlying issue (e.g. Duration Benchmark shows MEP -48% and Schedule Health notes MEP not linked to CxL2), the AI merges them into a single risk candidate with combined evidence.

---

### Stage 4 — Scoring

DeepSeek v4-flash scores each risk candidate using:

1. **User-submitted scoring knowledge** (if available) — historical registers, scoring criteria, project-specific notes injected into the system prompt.
2. **KB corrections from previous runs** (if available) — verified score overrides from past reviews.
3. **Default reasoning** (fallback) — the AI scores based on the severity of the finding, variance magnitude, and general construction risk principles.

For each risk candidate, the AI generates:

| Field | Example |
|---|---|
| **ID** | R1 |
| **Type** | Threat |
| **Name** | MEP duration compressed 48% below benchmark — T5 |
| **Shape** | Triangular |
| **Current Probability** | High |
| **Current Schedule** | High |
| **Current Cost** | Medium |
| **Current Score** | *(computed)* |
| **Mitigation Enabled** | Yes |
| **Mitigation Description** | Resequence MEP to allow CxL2 dependency completion |
| **Mitigation Duration** | 30d |
| **Mitigation Cost** | $50,000 |
| **Mitigated Probability** | Medium |
| **Mitigated Schedule** | Medium |
| **Mitigated Cost** | Low |
| **Mitigated Score** | *(computed)* |
| **Confidence** | 0.78 |

---

### Stage 5 — Preview & Review

Results appear in a preview table (same UI pattern as the IMS Generator preview):

| Column | Content |
|---|---|
| Source | Sheet name and finding reference |
| Category | AI-assigned risk category |
| Risk Name | AI-generated description |
| Type | Threat / Opportunity |
| Shape | Uniform / Triangular / Trigen |
| Probability | Current + Mitigated |
| Schedule Impact | Current + Mitigated |
| Cost Impact | Current + Mitigated |
| Score | Current + Mitigated (computed) |
| Mitigation | Description, Duration, Cost |
| Confidence | AI confidence score |
| Evidence | Expandable panel showing source data |

**Actions available:**

- ✕ Remove a risk candidate.
- Edit any cell directly (scores, category, mitigation text).
- + Add Risk to manually create a risk not detected by the AI.
- Merge two candidates that describe the same underlying issue.

---

### Stage 6 — Save to Knowledge Base

Same flywheel concept as IMS Generator. After reviewing and correcting:

- Click **"💾 Save to Knowledge Base"**.
- Stores all corrections to Azure Blob Storage under `risk-corrections/{project_code}/`.
- Next upload for the same project retrieves these corrections and pre-applies them.

| Correction Type | What it Records | Effect on Next Run |
|---|---|---|
| **KEEP** | Source finding → verified category, scores, mitigation | AI applies the verified scoring for this finding pattern. |
| **REMOVE** | Source finding the reviewer deleted | AI excludes this finding type from future output. |
| **SCORE OVERRIDE** | Reviewer changed probability/impact from AI suggestion | AI uses the reviewer's scores for similar findings. |

---

### Stage 7 — Export Acumen Fuse Risk Register

Click **"Download {ProjectCode}_Risk_Register.xlsx"**.

Output Excel matches the Acumen Fuse import schema:

| Column | Content |
|---|---|
| Enabled | Yes |
| Absolute M | Yes/No |
| ID | R1, R2, R3… |
| Type | Threat / Opportunity |
| Name | Risk description |
| Shape | Uniform / Triangular / Trigen |
| Current Probability | Negligible / Low / Medium / High / Very High |
| Current Schedule | Negligible / Low / Medium / High / Very High |
| Current Cost | Negligible / Low / Medium / High / Very High |
| Current Score | Computed |
| Mitigation Enabled | Yes/No |
| Mitigation Description | Free text |
| Mitigation Duration | e.g. 30d |
| Mitigation Cost | e.g. $50,000 |
| Mitigated Probability | Negligible / Low / Medium / High / Very High |
| Mitigated Schedule | Negligible / Low / Medium / High / Very High |
| Mitigated Cost | Negligible / Low / Medium / High / Very High |
| Mitigated Score | Computed |
| Calendar | Calendar assignment |
| No Split | Yes/No |

File can be imported directly into Acumen Fuse for QSRA.

---

## Shared Infrastructure

Both functions use the same Azure backend. v3.0 extends the existing services:

| Component | IMS Generator (v2.0) | Schedule Risk Manager (v3.0) | Shared? |
|---|---|---|---|
| **Azure Document Intelligence** | Extracts text from PDF schedules | Extracts structure from Excel assessments | Yes — same service |
| **DeepSeek v4-flash** | Extracts activity data, applies KB corrections | Parses findings, categorizes risks, generates scores | Yes — same endpoint, different system prompts |
| **Azure Blob Storage** | `p6-corrections/` (per-project KB) | `risk-corrections/` + `risk-knowledge/` (per-project) | Yes — same account, separate containers |
| **Frontend** | Single-page app at `/app` | Extended with nav pane and second workspace | Extended |

No additional Azure services required. No Dataverse, Power Automate, or Model-driven App.

---

## User Workflow — Schedule Risk Manager

| Step | Action | Outcome |
|---|---|---|
| **1** | Click **Schedule Risk Manager** in the left nav | Workspace switches to risk manager view |
| **2** | *(First time only)* Upload scoring knowledge via **📚 Upload Scoring Knowledge** | Scoring criteria stored for this project |
| **3** | Upload schedule assessment Excel, enter Project Code, Tranche(s), Assessment Date | Assessment record created; AI parsing begins |
| **4** | Wait for parsing + scoring to complete | Preview table appears with categorized, scored risk candidates |
| **5** | Review: edit scores, remove noise, add missing risks, merge duplicates | Corrections reflected in preview |
| **6** | Click **💾 Save to Knowledge Base** | Corrections stored for future runs — flywheel turns |
| **7** | Click **Download {ProjectCode}_Risk_Register.xlsx** | Acumen Fuse-ready risk register downloaded |
| **8** | Import into Acumen Fuse for QSRA | Risk register feeds quantitative schedule risk analysis |

---

## Knowledge Base Flywheel — Schedule Risk Manager

| Cycle | What Happens | Improvement |
|---|---|---|
| **Run 1** | No history. AI parses from scratch, scores using user-submitted knowledge only. Full manual review. | Baseline — all corrections saved. |
| **Run 2** | KB corrections retrieved. Known findings pre-scored. Reviewer focuses on new or changed findings. | Review time drops ~50%. |
| **Run 3+** | Most findings pre-matched and pre-scored. Spot-check only. New staff get expert-level risk registers from day one. | Near-zero review for stable projects. |

The Knowledge Base is project-specific. HYD20 risk corrections only apply to HYD20 assessments.

---

## Recommended Build Order

| Sprint | Deliverables |
|---|---|
| **1** | Left navigation pane, IMS Generator migrated to `/app/ims-generator`, Schedule Risk Manager shell at `/app/risk-manager`, upload form, scoring knowledge upload panel |
| **2** | AI parsing of all sheet types (LPSS, Duration Benchmark, Milestones, Schedule Health, Critical Path, Scope Check), risk categorization, preview table with edit/remove/add/merge |
| **3** | AI scoring with user-submitted knowledge context, Acumen Fuse score generation, confidence display, evidence panel |
| **4** | Knowledge Base save/retrieval for risk corrections (flywheel), Acumen Fuse Excel export, testing with live schedule assessments |

---

## Versioning Strategy

**v2.0** *(current)* — IMS Generator with Knowledge Base & AI Learning Flywheel.

**v3.0 (MVP)** — Navigation pane added. Schedule Risk Manager: Excel upload → AI parsing → risk categorization → scoring (user-submitted knowledge + KB corrections) → review → Acumen Fuse risk register export.

**v4.0** *(future)* — Cross-function intelligence: IMS Generator flags schedule anomalies that auto-populate risk candidates in Schedule Risk Manager. Auto-approve low-risk / high-confidence rows. Progress report PDF ingestion as a third input path.
