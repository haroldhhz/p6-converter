# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the backend

```bash
# From repo root, activate venv first
source venv/bin/activate

# Run dev server (serves both API and frontend at http://localhost:8000)
uvicorn backend.main:app --reload --port 8000
```

The frontend is mounted at `/app` (served as static files from `frontend/`). The API docs are at `http://localhost:8000/docs`.

On Windows with a corporate SSL proxy, use `start_server.bat` which patches the cert bundle.

## Environment setup

```bash
cp backend/.env.example backend/.env
# Edit backend/.env with real Azure credentials
```

Required `.env` vars:
- `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` / `AZURE_DOCUMENT_INTELLIGENCE_KEY`
- `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_KEY` / `AZURE_OPENAI_DEPLOYMENT`
- `ENTRA_TENANT_ID` / `ENTRA_CLIENT_ID` — Microsoft Entra ID app registration
- `ALLOWED_EMAILS` — comma-separated list of permitted sign-in emails

Localhost (`:8000`) bypasses authentication entirely — no credentials needed for local dev.

## Architecture

**Two workflows, one FastAPI backend:**

### IMS Generator (P6 Schedule Converter)
PDF schedule + Attachment 3 standard XLSX → P6-import XLSX

Pipeline (all in `backend/`):
1. `ai_client.py` — Azure Document Intelligence extracts tables from the PDF
2. `ai_client.py` — Azure OpenAI normalises raw Activity IDs (e.g. `BKK2001FOCF` → `BKK20-01-FOC-F`)
3. `processor.py` — Fuzzy-matches normalised IDs against the standard reference, builds hierarchical WBS codes with tranche suffixes, extracts dates/status, writes the final 13-column P6-import XLSX via pandas/openpyxl

### Schedule Risk Manager
Assessment Excel → scored risk candidates → Acumen Fuse risk register XLSX

Pipeline:
1. `risk_parser.py` — Parses the assessment Excel, sends rows to Azure OpenAI for categorisation across 12 predefined risk categories
2. `main.py` — Returns structured JSON; frontend renders editable table
3. User reviews/edits → downloads register or saves to knowledge base (`risk_knowledge_base.py`)

### Key backend modules

| Module | Role |
|---|---|
| `main.py` | FastAPI app, all routes, CORS, static file mount |
| `processor.py` | Core IMS pipeline: matching, WBS building, XLSX output |
| `ai_client.py` | Azure Document Intelligence + OpenAI wrappers, PDF table extraction, AI activity normalisation |
| `auth_middleware.py` | Entra ID JWT validation + RBAC, email header fallback, localhost bypass |
| `activity_store.py` | SQLite audit log (`activity.db`) — one row per workflow run, 90-day retention |
| `knowledge_base.py` | Azure Blob Storage–backed per-project correction history (IMS) |
| `risk_parser.py` | Risk assessment Excel parsing and AI categorisation |
| `risk_knowledge_base.py` | Same pattern as `knowledge_base.py` for risk corrections |

### Frontend

Single-page app (`frontend/`) — plain HTML/JS with no build step.

- `index.html` — full page markup (sign-in card + main app with 3 views: IMS, Risk, Admin)
- `app.js` — all client logic: MSAL auth, API calls, state, rendering
- `styles.css` — all styling

**MSAL config**: `app.js` fetches `/auth/config` at startup to get `ENTRA_CLIENT_ID`/`ENTRA_TENANT_ID` from the server (avoids needing a build step to inject env vars). The CDN script is `@azure/msal-browser` loaded from jsDelivr — use the `lib/msal-browser.min.js` path (the `dist/` path is ESM-only and has no UMD global).

### Auth flow

- Sign-in: MSAL popup → JWT Bearer token on all API requests
- Backend validates JWT signature + issuer + audience in `auth_middleware.py`
- Admin role: controlled by Entra ID app role (`ADMIN_ROLE_NAME` env var, default `Admin`)
- The Azure app registration **must** have redirect URIs registered under the **Single-Page Application** platform (not Web) to allow PKCE flow from the browser

### Deployment

- Backend: Azure App Service (Python 3.12)
- Frontend: Azure Static Web Apps (routing in `frontend/azure-static-web-apps.config.json`)
- Storage: Azure Blob Storage (knowledge bases), SQLite file (activity log, local to App Service)
# General Guideline

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.