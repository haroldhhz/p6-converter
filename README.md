# P6 Schedule Converter — Step-by-Step Guide

A web app that takes a **P6 schedule export (PDF)** and an **Attachment 3 standard reference (XLSX)**, then produces a **P6-import-ready XLSX** with corrected Activity IDs, WBS codes, statuses, and dates.

Built with **FastAPI** + **Azure Document Intelligence** + **Azure OpenAI (GPT-4o)**.

---

## Architecture

```
User uploads PDF + XLSX
        │
        ▼
Azure Static Web Apps  (frontend/index.html)
        │
        │  fetch() POST
        ▼
Azure App Service  (FastAPI backend)
        │
        ├── Azure Document Intelligence  →  Parse PDF tables
        ├── Azure OpenAI (GPT-4o)         →  Normalise Activity IDs
        │                                    Confirm activity names
        ├── Python rules engine            →  Build WBS codes
        └── Pandas + openpyxl             →  Write P6-import XLSX
        │
        ▼
User downloads p6_import.xlsx
```

---

## Part 0 — Prerequisites

1. **VS Code** with the **Azure Toolkit** extension installed and signed in
2. **Python 3.10+** installed locally
3. Azure subscription with the following resources created:

| Resource | Type | Why |
|---|---|---|
| `doc-intelligence` | Cognitive Services — Document Intelligence | Parse PDF tables |
| `openai` | Azure OpenAI | Normalise IDs & validate names |
| App Service Plan | App Service Plan | Host the FastAPI backend |
| Static Web App | Azure Static Web Apps | Host the HTML/JS frontend |

4. Clone this repo:
   ```bash
   git clone https://github.com/YOUR_USERNAME/p6-converter.git
   cd p6-converter
   ```

---

## Part 1 — Local Development

### 1.1 Create and activate a virtual environment

```bash
# In the p6-converter/ root
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 1.2 Install dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 1.3 Configure environment variables

```bash
cd backend
cp .env.example .env
```

Edit `.env` and fill in your values:

```env
# Azure Document Intelligence
AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT=https://YOUR_DOC_INT_RESOURCE.cognitiveservices.azure.com/
AZURE_DOCUMENT_INTELLIGENCE_KEY=YOUR_KEY_HERE

# Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://YOUR_OPENAI_RESOURCE.openai.azure.com/
AZURE_OPENAI_KEY=YOUR_KEY_HERE
AZURE_OPENAI_API_VERSION=2024-02-01
AZURE_OPENAI_DEPLOYMENT=gpt-4o

# App
DEFAULT_DATA_DATE=2026-02-28
ALLOWED_ORIGINS=http://localhost:8000
```

> ⚠️ **Never commit `.env` to git.** It is already in `.gitignore`.

### 1.4 Run the FastAPI backend locally

```bash
cd backend
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/docs to see the Swagger UI.

### 1.5 Open the frontend locally

Since the frontend is static HTML, you can just open the file directly:

```bash
# Windows
start frontend/index.html

# macOS
open frontend/index.html

# Linux
xdg-open frontend/index.html
```

Or serve it with any static server, e.g.:

```bash
npx serve frontend --p 3000
```

> In `frontend/app.js`, the `API_BASE` is already set to `http://localhost:8000` for local dev, so no changes needed.

---

## Part 2 — Deploy to Azure

### 2.1 Deploy the Backend to Azure App Service

1. Open the `p6-converter` folder in **VS Code**.
2. Open the **Azure Explorer** (left sidebar → Azure icon).
3. Expand **App Services** → right-click your subscription → **Create New Web App…**
   - **Name**: `p6-converter-api` (or your choice)
   - **Runtime**: `Python 3.11` (or latest)
   - **Region**: your preferred region
   - **App Service Plan**: select your existing plan
4. Right-click the newly created App Service → **Deploy to Web App** → choose the **`backend/`** folder.
5. VS Code will:
   - Detect it's a FastAPI app and set the startup command automatically
   - Zip and push the code to Azure
   - Show a notification when done

6. **Add App Settings** (environment variables) after deployment:
   - In the Azure Explorer, right-click your App Service → **Upload Settings** or go to the Azure Portal → **Configuration → Application settings** and add:

   | Name | Value |
   |---|---|
   | `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` | `https://YOUR_RESOURCE.cognitiveservices.azure.com/` |
   | `AZURE_DOCUMENT_INTELLIGENCE_KEY` | `YOUR_KEY` |
   | `AZURE_OPENAI_ENDPOINT` | `https://YOUR_RESOURCE.openai.azure.com/` |
   | `AZURE_OPENAI_KEY` | `YOUR_KEY` |
   | `AZURE_OPENAI_API_VERSION` | `2024-02-01` |
   | `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` |
   | `ALLOWED_ORIGINS` | `https://YOUR_STATIC_WEB_APP.sta.azure.com` |
   | `DEFAULT_DATA_DATE` | `2026-02-28` |

   Click **Save** and **restart** the app.

7. Verify: open `https://p6-converter-api.azurewebsites.net/docs` in your browser.

### 2.2 Update the Frontend API URL

Before deploying the frontend, update `frontend/app.js` to point to your deployed backend:

```js
// Replace this line:
: "https://<YOUR_BACKEND_APP>.azurewebsites.net";   // ← replace this

// With your actual URL, e.g.:
: "https://p6-converter-api.azurewebsites.net";
```

Commit and save.

### 2.3 Deploy the Frontend to Azure Static Web Apps

**Option A — Via VS Code Azure Toolkit:**

1. Right-click the **`frontend/`** folder in VS Code.
2. Select **Deploy to Static Web App…**
3. Choose your Azure subscription.
4. Fill in:
   - **Name**: `p6-converter` (or your choice)
   - **Region**: your preferred region
   - **Build preset**: select **"Other"** (no build step needed)
   - **Output location**: `.` (root of frontend folder)
5. Click **Create + Deploy**.
6. After deployment, Azure will give you a URL like:
   `https://p6-converter.sta.azure.com`

**Option B — Via Azure Portal:**

1. Go to [portal.azure.com](https://portal.azure.com) → **Create a Resource** → search **Static Web Apps**.
2. Fill in:
   - **Name**: `p6-converter`
   - **Publish**: Static App
   - **Region**: your region
   - **Plan**: Free (or Standard)
3. On the "Deployment details" tab:
   - **Source**: GitHub (or manual zip)
4. On the "Build details" tab:
   - **Build preset**: Other
   - **App artifact location**: `/`
5. Click **Review + Create** → **Create**.
6. After creation, go to the Static Web App → **Deployment tokens** → copy the token and use it in GitHub Actions or push your code directly.

### 2.4 Allow the Backend to Accept Requests from the Frontend

In the Azure Portal → your **App Service** → **Configuration → CORS**:
- Add the Static Web App URL (e.g. `https://p6-converter.sta.azure.com`)
- Remove any `*` wildcard if present for production.

### 2.5 Verify End-to-End

1. Open your Static Web App URL in a browser.
2. Upload a P6 schedule PDF and the Attachment 3 standard XLSX.
3. Click **Convert & Download XLSX**.
4. Open the downloaded `.xlsx` in Excel — you should see all 8 columns filled in:
   - `task_code`, `status_code`, `wbs_id`, `task_name`, `complete_pct`,
   - `act_start_date`, `act_end_date`, `delete_record_flag`
5. Import the XLSX back into P6 to verify compatibility.

---

## Part 3 — Processing Pipeline Reference

| Step | What happens |
|---|---|
| **1 — Parse PDF** | Azure Document Intelligence extracts all tables from the PDF using the `prebuilt-layout` model |
| **2 — Normalise Activity ID** | Azure OpenAI (GPT-4o) strips spaces, fixes typos, enforces `-S`/`-F` suffix, normalises `BKK2001FOCF` → `BKK20-01-FOC-F` |
| **3 — Populate Status** | Looks up the corrected Activity ID in Attachment 3 to get the expected status (`Completed`, `Not Started`, etc.) |
| **4 — Build WBS ID** | Python rules: `{ProjectCode}_IMS_{DataDate}_UD.6` + tranche suffix (e.g. `UD.6.2` for tranche 2) |
| **5 — Validate Activity Name** | Azure OpenAI (GPT-4o) confirms the activity name matches the standard reference |
| **6 — Extract Dates & %** | Pulls `Actual Start`, `Actual Finish`, `% Complete` from the PDF columns |

---

## Part 4 — Updating the Standard Reference

The app accepts **any** Attachment 3 Excel file as long as it has at minimum:
- `task_code` — the standard Activity ID (e.g. `PNQ-26-01-CEX-S`)
- `task_name` — the standard Activity Name (e.g. `Contract Execution`)
- `status_code` — (optional) the standard status; defaults to `"Not Started"`

The standard file can be a different project (e.g. BKK20 instead of PNQ26) — the AI will adapt accordingly.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT is not set` | Add the env var to `.env` (local) or App Settings (Azure) |
| `No tables found in the PDF` | The PDF may be image-based; use Document Intelligence `prebuilt-layout` which supports embedded images, or pre-OCR the PDF |
| CORS error in browser console | Add the Static Web App URL to `ALLOWED_ORIGINS` in App Settings and to the CORS settings in the Azure Portal |
| `gpt-4o deployment not found` | Check that your Azure OpenAI resource has GPT-4o deployed and the deployment name matches `AZURE_OPENAI_DEPLOYMENT` in your settings |
| XLSX downloads but P6 rejects it | Make sure the standard XLSX has `task_code` and `task_name` columns — see Part 4 above |
| `Authentication required` error | Make sure your email is in `ALLOWED_EMAILS`. If running locally (localhost), authentication is bypassed for development. |
| `Access denied` error (403) | Your email is signed in but not in the allowed list. Contact the admin to add your email. |

---

## Part 5 — Authentication Setup

The app uses **Azure AD (Microsoft Entra ID)** for authentication. Only users with authorized emails can access the application.

### 5.1 Allowed Users

Access is controlled by the `ALLOWED_EMAILS` environment variable. By default, the following emails are authorized:

- `haroldhuang@microsoft.com`
- `jonathan.heng@linesight.com`
- `emmberger@microsoft.com`
- `kpawaskar@microsoft.com`
- `gdubois@microsoft.com`

To add or remove users, update the `ALLOWED_EMAILS` environment variable in the backend settings.

### 5.2 Local Development

When running locally (http://localhost:8000), authentication is **automatically bypassed** so you can test without signing in.

### 5.3 Azure Portal Configuration

To enable Azure AD authentication on Azure Static Web Apps:

1. Go to your **Azure Static Web App** in the Azure Portal
2. Navigate to **Authentication** (under Settings)
3. Click **Add identity provider**
4. Select **Microsoft** (Azure AD / Entra ID)
5. Follow the prompts to create or select an existing App Registration
6. Set the **Callback URL** to your Static Web App URL
7. Click **Add**

### 5.4 Backend Configuration

After enabling authentication, add the `ALLOWED_EMAILS` environment variable to your App Service:

| Name | Value |
|---|---|
| `ALLOWED_EMAILS` | `haroldhuang@microsoft.com,jonathan.heng@linesight.com,emmberger@microsoft.com,kpawaskar@microsoft.com,gdubois@microsoft.com` |

### 5.5 How It Works

1. Users are redirected to Microsoft's login page when accessing the app
2. After signing in, the backend validates the user's email against the allowlist
3. If the email is not in the list, the user receives an "Access denied" error
4. Authenticated and authorized users can use the full application functionality

---

## File Structure

```
p6-converter/
├── backend/
│   ├── main.py                         # FastAPI server
│   ├── processor.py                    # 5-step processing pipeline
│   ├── ai_client.py                   # Azure DI + OpenAI wrapper
│   ├── requirements.txt               # Python dependencies
│   ├── .env.example                   # Environment variable template
│   └── host.json                      # Azure Functions/App Service config
├── frontend/
│   ├── index.html                     # Upload UI
│   ├── app.js                         # Fetch client
│   ├── styles.css                     # Styles
│   └── azure-static-web-apps.config.json
├── .gitignore
└── README.md                          # This file
```
