/**
 * Lease Agentic Planning (LAP) — Frontend v7
 *
 * Functions:
 *   1. IMS Generator  — PDF schedule → P6-import XLSX
 *   2. Schedule Risk Manager — Assessment Excel → Acumen Fuse risk register
 *   3. Activity Log (Admin) — Audit trail with filter, pagination, detail view
 *
 * Auth: Microsoft Entra ID SSO via MSAL.js
 */

const API_BASE = "";

// ─── MSAL Config ───────────────────────────────────────────────────────────
// These are injected at build time via Vite env vars.
// For local dev / Azure Static Web Apps, use app settings to expose them.
let MSAL_CLIENT_ID = window.env?.VITE_ENTRA_CLIENT_ID || "";
let MSAL_TENANT_ID = window.env?.VITE_ENTRA_TENANT_ID || "";

// MSAL instance — initialised once on first sign-in attempt
let msalInstance = null;
let msalAccount = null;

// Auth state
let currentUser = null;   // { email, name, roles: [], is_admin, source }
let isAuthenticated = false;
let currentView = "ims";   // "ims" | "risk" | "admin"
let sharedProjectCode = "";

// ─── API auth headers ───────────────────────────────────────────────────────
function authHeaders(extra = {}) {
  const h = { ...extra };
  // Prefer Bearer token if we have a MSAL access token (check FIRST so it's sent even when currentUser is null)
  if (msalAccount && msalAccessToken) {
    h["Authorization"] = `Bearer ${msalAccessToken}`;
  } else if (currentUser?.email) {
    h["X-User-Email"] = currentUser.email;
  }
  return h;
}

let msalAccessToken = "";

// ─── State ─────────────────────────────────────────────────────────────────
let projectResults = [];
let activeIdx = 0;
let visibleSlots = 1;
let editableActivities = [];

const DISPLAY_COLS = [
  ["task_code",          "Activity ID",           true],
  ["pdf_activity_name",  "Original PDF Name",    false],
  ["task_name",          "Activity Name",         true],
  ["status_code",        "Status",                true],
  ["cstr_type",          "Constraint",             true],
  ["cstr_date",          "Constraint Date",       true],
  ["act_start_date",     "Actual Start",          true],
  ["act_end_date",       "Actual Finish",         true],
];

// ─── DOM references ─────────────────────────────────────────────────────────
const form            = document.getElementById("uploadForm");
const submitBtn       = document.getElementById("submitBtn");
const btnText         = document.getElementById("btnText");
const spinner         = document.getElementById("spinner");
const statusBox       = document.getElementById("statusBox");
const addProjectBtn   = document.getElementById("addProjectBtn");
const resultsSection  = document.getElementById("resultsSection");
const projectTabsEl   = document.getElementById("projectTabs");
const resultsPanelEl  = document.getElementById("resultsPanel");
const downloadBtn     = document.getElementById("downloadBtn");
const saveKbBtn       = document.getElementById("saveKbBtn");
const newConversionBtn = document.getElementById("newConversionBtn");

// ─── MSAL helpers ───────────────────────────────────────────────────────────

async function initMsal() {
  if (!MSAL_CLIENT_ID || !MSAL_TENANT_ID) {
    console.warn("MSAL not configured — VITE_ENTRA_CLIENT_ID / VITE_ENTRA_TENANT_ID not set");
    return null;
  }
  if (msalInstance) return msalInstance;

  const { PublicClientApplication } = window.msal;
  msalInstance = new PublicClientApplication({
    auth: {
      clientId: MSAL_CLIENT_ID,
      authority: `https://login.microsoftonline.com/${MSAL_TENANT_ID}`,
      redirectUri: window.location.origin,
    },
    cache: { cacheLocation: "localStorage", storeAuthStateInCookie: false },
  });

  await msalInstance.initialize();
  // Handle redirect response
  await msalInstance.handleRedirectPromise();
  return msalInstance;
}

async function signInWithMSAL() {
  const msal = await initMsal();
  if (!msal) return;

  try {
    const response = await msal.loginPopup({
      scopes: ["User.Read", "email", "profile"],
      prompt: "select_account",
    });
    msalAccount = response.account;
    msalAccessToken = response.accessToken;

    // Fetch full user info from /auth/me
    await fetchAndSetUser();
  } catch (err) {
    console.error("MSAL sign-in failed:", err);
    document.getElementById("signinError").textContent =
      "Sign-in failed. Please try again or use a different account.";
    document.getElementById("signinError").classList.remove("hidden");
  }
}

async function fetchAndSetUser() {
  // Set temp user so authHeaders() can send Bearer token
  currentUser = { email: msalAccount.username, name: msalAccount.name || msalAccount.username, roles: [], is_admin: true, source: "entra" };
  try {
    const res = await fetch(`${API_BASE}/auth/me`, {
      headers: authHeaders(),
    });
    if (!res.ok) {
      // Prefer saved session, fall back to msalAccount
      const saved = getSavedUser();
      if (saved?.email) {
        currentUser = saved;
        isAuthenticated = true;
        showMainApp(currentUser);
      } else if (msalAccount) {
        currentUser = {
          email: msalAccount.username,
          name: msalAccount.name || msalAccount.username,
          roles: [],
          is_admin: true,
          source: "entra",
        };
        isAuthenticated = true;
        showMainApp(currentUser);
      }
      return;
    }
    const data = await res.json();
    currentUser = data;
    isAuthenticated = true;
    showMainApp(currentUser);
  } catch (_) {
    // Don't overwrite a saved session — it's better than a dev fallback
    const saved = getSavedUser();
    if (saved?.email) {
      currentUser = saved;
      isAuthenticated = true;
      showMainApp(currentUser);
    } else if (msalAccount) {
      currentUser = {
        email: msalAccount.username,
        name: msalAccount.name || msalAccount.username,
        roles: [],
        is_admin: true,
        source: "entra",
      };
      isAuthenticated = true;
      showMainApp(currentUser);
    }
  }
}

async function signOut() {
  if (msalInstance && msalAccount) {
    await msalInstance.logoutPopup().catch(() => {});
    msalAccount = null;
    msalAccessToken = "";
  }
  clearUser();
  currentUser = null;
  isAuthenticated = false;
  showSignInPage();
}

// ─── Auth lifecycle ─────────────────────────────────────────────────────────

const STORAGE_KEY = "lap_user";

function getSavedUser() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)); } catch { return null; }
}

function saveUser(user) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(user));
}

function clearUser() {
  localStorage.removeItem(STORAGE_KEY);
}

function showSignInPage() {
  document.getElementById("signinPage").style.display = "flex";
  document.getElementById("mainApp").style.display = "none";
  document.getElementById("signinError").classList.add("hidden");
}

function showMainApp(user) {
  currentUser = user;
  isAuthenticated = true;
  saveUser(user);
  document.getElementById("signinPage").style.display = "none";
  document.getElementById("mainApp").style.display = "block";
  document.getElementById("userEmailDisplay").textContent = user.email;

  // Show Admin nav item only if user has Admin role
  const adminNav = document.getElementById("adminNavGroup");
  if (adminNav) {
    adminNav.style.display = user.is_admin ? "flex" : "none";
  }

  showView(currentView);
}

async function initAuth() {
  // Fetch MSAL config from the server (reads server-side env vars — no rebuild needed)
  try {
    const cfg = await fetch('/auth/config').then(r => r.json());
    if (cfg.msalClientId) MSAL_CLIENT_ID = cfg.msalClientId;
    if (cfg.msalTenantId) MSAL_TENANT_ID = cfg.msalTenantId;
  } catch (_) {}

  // Try silent sign-in first (MSAL token cache)
  if (MSAL_CLIENT_ID && MSAL_TENANT_ID) {
    const msal = await initMsal();
    if (msal) {
      const accounts = msal.getAllAccounts();
      if (accounts.length > 0) {
        msalAccount = accounts[0];
        try {
          const resp = await msal.acquireTokenSilent({ accounts: [msalAccount], scopes: ["User.Read"] });
          msalAccessToken = resp.accessToken;
        } catch (_) {}
        await fetchAndSetUser();
        return;
      }
    }
  }

  // Fallback: saved session
  const user = getSavedUser();
  if (user?.email) {
    currentUser = user;
    isAuthenticated = true;
    showMainApp(user);
    return;
  }

  showSignInPage();

  // Wire up MSAL sign-in button
  const msalBtn = document.getElementById("msalSigninBtn");
  if (msalBtn) {
    msalBtn.addEventListener("click", signInWithMSAL);
  }

  // Legacy email sign-in (shown if MSAL not configured)
  document.getElementById("signinBtn")?.addEventListener("click", handleEmailSignIn);
  document.getElementById("signinEmail")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") handleEmailSignIn();
  });
}

async function handleEmailSignIn() {
  const email = document.getElementById("signinEmail").value.trim().toLowerCase();
  const errorEl = document.getElementById("signinError");
  const btn = document.getElementById("signinBtn");

  if (!email) {
    errorEl.textContent = "Please enter your email address.";
    errorEl.classList.remove("hidden");
    return;
  }
  btn.disabled = true;
  btn.textContent = "Signing in…";
  errorEl.classList.add("hidden");

  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    if (res.ok) {
      const data = await res.json();
      currentUser = { ...data.user, roles: data.user?.roles || [], is_admin: data.user?.is_admin || false, source: "email" };
      saveUser(currentUser);
      showMainApp(currentUser);
    } else {
      const err = await res.json().catch(() => ({}));
      errorEl.textContent = err.detail || "Sign in failed.";
      errorEl.classList.remove("hidden");
    }
  } catch {
    errorEl.textContent = "Network error.";
    errorEl.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Sign In";
  }
}

document.getElementById("signoutBtn")?.addEventListener("click", signOut);

// ─── Page Filter management ─────────────────────────────────────────────────

function setupPageFilterButtons() {
  document.querySelectorAll(".add-page-filter-btn").forEach(btn => {
    const newBtn = btn.cloneNode(true);
    btn.parentNode.replaceChild(newBtn, btn);
  });
  document.querySelectorAll(".add-page-filter-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const container = btn.closest(".page-filters-container");
      const slot = container.dataset.slot;
      const existingRows = container.querySelectorAll(".page-range-input");
      const filterIndex = existingRows.length;
      const newRow = document.createElement("div");
      newRow.className = "page-range-input";
      newRow.innerHTML = `
        <input type="number" name="page_from_${slot}_${filterIndex}" min="1" placeholder="From" />
        <span class="page-range-sep">to</span>
        <input type="number" name="page_to_${slot}_${filterIndex}" min="1" placeholder="To" />
        <button type="button" class="remove-page-filter-btn" title="Remove this range">✕</button>`;
      container.appendChild(newRow);
      newRow.querySelector(".remove-page-filter-btn")?.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        newRow.remove();
      });
    });
  });
}

setupPageFilterButtons();

// ─── Slot management ─────────────────────────────────────────────────────────

addProjectBtn.addEventListener("click", () => {
  if (visibleSlots >= 3) return;
  visibleSlots++;
  const slot = document.querySelector(`.project-slot[data-slot="${visibleSlots}"]`);
  if (slot) slot.classList.remove("hidden");
  document.body.setAttribute("data-visible-slots", visibleSlots);
  if (visibleSlots === 3) addProjectBtn.style.display = "none";
});

document.body.setAttribute("data-visible-slots", "1");

document.querySelectorAll(".remove-slot-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const slotNum = parseInt(btn.dataset.slot);
    const slot = document.querySelector(`.project-slot[data-slot="${slotNum}"]`);
    slot.classList.add("hidden");
    slot.querySelectorAll("input").forEach(inp => { inp.value = ""; });
    for (let s = slotNum + 1; s <= 3; s++) {
      document.querySelector(`.project-slot[data-slot="${s}"]`).classList.add("hidden");
      document.querySelector(`.project-slot[data-slot="${s}"]`).querySelectorAll("input").forEach(inp => { inp.value = ""; });
    }
    visibleSlots = slotNum - 1;
    addProjectBtn.style.display = "";
  });
});

// ─── Helpers ─────────────────────────────────────────────────────────────────

function setLoading(loading) {
  submitBtn.disabled = loading;
  addProjectBtn.disabled = loading;
  btnText.textContent = loading ? "Converting…" : "Convert All";
  spinner.classList.toggle("hidden", !loading);
}

function showStatus(msg, isError = false) {
  statusBox.textContent = msg;
  statusBox.className = `status-box ${isError ? "error" : "success"}`;
  statusBox.classList.remove("hidden");
}

function hideStatus() { statusBox.classList.add("hidden"); }

// ─── Toast ────────────────────────────────────────────────────────────────────
const toastPopup = document.getElementById("toastPopup");
const toastIcon  = document.getElementById("toastIcon");
const toastMsg  = document.getElementById("toastMsg");
let _toastTimer = null;

function showToast(message, isError = false, durationMs = 3000) {
  if (_toastTimer) clearTimeout(_toastTimer);
  toastIcon.textContent = isError ? "❌" : "✅";
  toastMsg.textContent = message;
  toastPopup.className = `toast-popup ${isError ? "error" : "success"}`;
  void toastPopup.offsetWidth;
  toastPopup.classList.add("show");
  _toastTimer = setTimeout(() => {
    toastPopup.classList.remove("show");
    setTimeout(() => { toastPopup.classList.add("hidden"); }, 260);
  }, durationMs);
}

function b64ToBlob(b64) {
  const bytes = atob(b64);
  const arr = new Uint8Array(bytes.length);
  for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
  return new Blob([arr], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ─── Results rendering ───────────────────────────────────────────────────────

function renderTabs() {
  projectTabsEl.innerHTML = projectResults.map((p, i) => {
    const label = p.error ? "ERROR" : p.project_code;
    const count = p.error ? "ERR" : p.row_count;
    return `<button class="tab-btn ${i === activeIdx ? "active" : ""}" data-idx="${i}">${label} <span class="tab-count">${count}</span></button>`;
  }).join("");
  projectTabsEl.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      activeIdx = parseInt(btn.dataset.idx);
      renderTabs();
      renderTable();
      updateDownloadBtn();
    });
  });
}

function escapeHtml(str) {
  if (str == null) return "";
  const div = document.createElement("div");
  div.textContent = String(str);
  return div.innerHTML;
}

function renderTable(reinit = true) {
  const project = projectResults[activeIdx];
  if (!project) { resultsPanelEl.innerHTML = ""; return; }
  if (project.error) {
    resultsPanelEl.innerHTML = `<div class="panel-error"><strong>Error:</strong> ${project.error}</div>`;
    return;
  }
  if (reinit) {
    editableActivities = (project.activities || []).map((a, idx) => ({ ...a, _idx: idx }));
  }
  if (editableActivities.length === 0) {
    resultsPanelEl.innerHTML = `<p class="panel-empty">No matched activities found.</p><button class="add-activity-btn" id="addActivityBtnEmpty">+ Add Activity</button>`;
    setupAddActivityButton();
    return;
  }
  const headerHtml = `<th class="row-actions-header">Actions</th>` + DISPLAY_COLS.map(([, h]) => `<th>${h}</th>`).join("");
  const visible = editableActivities.filter(r => !r._removed);
  const bodyHtml = visible.map((row) => {
    const realIdx = editableActivities.indexOf(row);
    const cells = DISPLAY_COLS.map(([key, , editable]) => {
      const value = row[key] != null ? row[key] : "";
      return editable
        ? `<td><input type="text" class="editable-cell" data-key="${key}" data-idx="${realIdx}" value="${escapeHtml(value)}" /></td>`
        : `<td>${escapeHtml(value)}</td>`;
    }).join("");
    return `<tr data-row-idx="${realIdx}"><td class="row-actions"><button class="remove-row-btn" data-idx="${realIdx}" title="Remove row">✕</button></td>${cells}</tr>`;
  }).join("");
  const kbBadge = project.kb_corrections_used > 0
    ? `<span class="kb-badge" title="Last saved">📚 ${project.kb_corrections_used} KB corrections</span>` : "";
  resultsPanelEl.innerHTML = `
    <div class="results-toolbar">
      <span class="panel-count">${visible.length} activities — ${project.filename}</span>
      ${kbBadge}
      <button class="add-activity-btn" id="addActivityBtn">+ Add Activity</button>
    </div>
    <div class="results-table-wrap">
      <table class="results-table">
        <thead><tr>${headerHtml}</tr></thead>
        <tbody id="resultsTableBody">${bodyHtml}</tbody>
      </table>
    </div>`;
  setupRemoveRowButtons();
  setupAddActivityButton();
  setupCellChangeHandlers();
}

function setupRemoveRowButtons() {
  document.querySelectorAll(".remove-row-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const idx = parseInt(btn.dataset.idx);
      editableActivities[idx]._removed = true;
      const scrollY = window.scrollY;
      renderTable(false);
      window.scrollTo(0, scrollY);
    });
  });
}

function setupAddActivityButton() {
  const btn = document.getElementById("addActivityBtn") || document.getElementById("addActivityBtnEmpty");
  if (btn) btn.addEventListener("click", addNewActivityRow);
}

function setupCellChangeHandlers() {
  document.querySelectorAll(".editable-cell").forEach(input => {
    input.addEventListener("change", () => {
      editableActivities[parseInt(input.dataset.idx)][input.dataset.key] = input.value;
    });
    input.addEventListener("input", () => {
      editableActivities[parseInt(input.dataset.idx)][input.dataset.key] = input.value;
    });
  });
}

function addNewActivityRow() {
  editableActivities.push({
    _idx: editableActivities.length, task_code: "", task_name: "",
    status_code: "Not Started", cstr_type: "", cstr_date: "",
    act_start_date: "", act_end_date: "",
  });
  renderTable(false);
  requestAnimationFrame(() => {
    document.querySelector(".results-table-wrap")?.scrollTo(0, 9999);
    const lastInput = document.querySelector("#resultsTableBody tr:last-child input");
    if (lastInput) lastInput.focus();
  });
}

function updateDownloadBtn() {
  const p = projectResults[activeIdx];
  downloadBtn.textContent = `⬇ Download ${p?.project_code ?? ""}.xlsx`;
  downloadBtn.disabled = !p || !!p.error;
}

// ─── Download ───────────────────────────────────────────────────────────────

downloadBtn.addEventListener("click", async () => {
  const p = projectResults[activeIdx];
  if (!p || p.error) return;
  try {
    const res = await fetch(`${API_BASE}/download-edited/`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ project_code: p.project_code, activities: editableActivities }),
    });
    if (!res.ok) {
      let errMsg = `Server error ${res.status}`;
      try {
        const errData = await res.json();
        errMsg = typeof errData?.detail === 'string' ? errData.detail : errMsg;
      } catch (_) {}
      throw new Error(errMsg);
    }
    const data = await res.json();
    if (data.xlsx_b64) {
      triggerDownload(b64ToBlob(data.xlsx_b64), `${p.project_code}_p6_import.xlsx`);
      showStatus(`✅ Downloaded ${p.project_code}_p6_import.xlsx`);
    }
  } catch (err) {
    showStatus(`❌ ${err.message}`, true);
  }
});

// ─── Save to KB ────────────────────────────────────────────────────────────

saveKbBtn.addEventListener("click", async () => {
  const p = projectResults[activeIdx];
  if (!p || p.error) return;
  saveKbBtn.disabled = true;
  saveKbBtn.textContent = "💾 Saving…";
  try {
    const res = await fetch(`${API_BASE}/knowledge-base/save/`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ project_code: p.project_code, activities: editableActivities }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Server error");
    }
    const data = await res.json();
    showToast(`Knowledge base saved for ${p.project_code}! ${data.corrections_saved} corrections.`, false, 4000);
    saveKbBtn.textContent = "✅ Saved";
    setTimeout(() => { saveKbBtn.textContent = "💾 Save to Knowledge Base"; saveKbBtn.disabled = false; }, 4000);
  } catch (err) {
    showToast(`KB save failed: ${err.message}`, true, 5000);
    saveKbBtn.textContent = "💾 Save to Knowledge Base";
    saveKbBtn.disabled = false;
  }
});

// ─── New Conversion ─────────────────────────────────────────────────────────

newConversionBtn.addEventListener("click", () => {
  projectResults = []; activeIdx = 0; editableActivities = [];
  form.reset();
  for (let s = 2; s <= 3; s++) document.querySelector(`.project-slot[data-slot="${s}"]`).classList.add("hidden");
  visibleSlots = 1; document.body.setAttribute("data-visible-slots", "1");
  addProjectBtn.style.display = "";
  form.classList.remove("hidden"); resultsSection.classList.add("hidden");
  hideStatus(); setLoading(false);
});

// ─── Form submit ─────────────────────────────────────────────────────────────

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData();
  let taskCount = 0;

  for (let slot = 1; slot <= 3; slot++) {
    const slotEl = document.querySelector(`.project-slot[data-slot="${slot}"]`);
    if (slotEl.classList.contains("hidden")) continue;
    const fileInput = slotEl.querySelector(`input[name="schedule_file_${slot}"]`);
    const codeInput = slotEl.querySelector(`input[name="project_code_${slot}"]`);
    const trancheInput = slotEl.querySelector(`input[name="tranche_${slot}"]`);
    const dateInput = slotEl.querySelector(`input[name="data_date_${slot}"]`);
    const file = fileInput?.files?.[0];
    const code = codeInput?.value?.trim();
    if (!file || !code) {
      if (slot === 1) { showStatus("Please select a PDF and project code for Project 1.", true); return; }
      continue;
    }
    fd.append(`schedule_file_${slot}`, file);
    fd.append(`project_code_${slot}`, code);
    fd.append(`tranche_${slot}`, trancheInput?.value?.trim() || "01");
    if (dateInput?.value) fd.append(`data_date_${slot}`, dateInput.value);
    const pageContainer = slotEl.querySelector(".page-filters-container");
    if (pageContainer) {
      const pageRanges = [];
      pageContainer.querySelectorAll(".page-range-input").forEach(row => {
        const from = row.querySelector(`input[name^="page_from_${slot}_"]`)?.value?.trim();
        const to = row.querySelector(`input[name^="page_to_${slot}_"]`)?.value?.trim();
        if (from && to) pageRanges.push(`${from}-${to}`);
      });
      if (pageRanges.length > 0) fd.append(`pages_${slot}`, pageRanges.join(","));
    }
    taskCount++;
  }

  if (taskCount === 0) { showStatus("Select at least one PDF with a project code.", true); return; }

  setLoading(true);
  showStatus(`Processing ${taskCount} project${taskCount > 1 ? "s" : ""} in parallel…`);

  try {
    const res = await fetch(`${API_BASE}/batch-convert/`, {
      method: "POST",
      headers: authHeaders(),
      body: fd,
    });
    if (!res.ok) {
      let errMsg = `Server error ${res.status}`;
      try {
        const errData = await res.json();
        errMsg = typeof errData?.detail === 'string' ? errData.detail : errMsg;
      } catch (_) {}
      throw new Error(errMsg);
    }
    const data = await res.json();
    projectResults = data.projects || [];
    activeIdx = 0;
    setLoading(false);
    form.classList.add("hidden");
    resultsSection.classList.remove("hidden");
    renderTabs(); renderTable(); updateDownloadBtn(); hideStatus();
  } catch (err) {
    showStatus(`❌ ${err.message}`, true);
    setLoading(false);
  }
});

// ─── Navigation ─────────────────────────────────────────────────────────────

function showView(viewId) {
  currentView = viewId;
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
  const view = document.getElementById(`view-${viewId}`);
  if (view) view.classList.add("active");
  const navItem = document.querySelector(`.nav-item[data-view="${viewId}"]`);
  if (navItem) navItem.classList.add("active");
  if (viewId === "risk" && sharedProjectCode) {
    const rpc = document.getElementById("riskProjectCode");
    if (rpc && !rpc.value) rpc.value = sharedProjectCode;
  }
  if (viewId === "admin") loadActivityLog();
}

document.querySelectorAll(".nav-item").forEach(item => {
  item.addEventListener("click", () => showView(item.dataset.view));
});

document.addEventListener("input", (e) => {
  if (e.target?.name === "project_code_1") sharedProjectCode = e.target.value.trim();
});

// ─── Risk Manager ────────────────────────────────────────────────────────────

const riskUploadForm = document.getElementById("riskUploadForm");
const riskSubmitBtn = document.getElementById("riskSubmitBtn");
const riskBtnText = document.getElementById("riskBtnText");
const riskSpinner = document.getElementById("riskSpinner");
const riskStatusBox = document.getElementById("riskStatusBox");
const riskResultsSection = document.getElementById("riskResultsSection");
const riskResultsPlaceholder = document.getElementById("riskResultsPlaceholder");
const riskTableBody = document.getElementById("riskTableBody");
const mergeRiskBtn = document.getElementById("mergeRiskBtn");

let riskCandidates = [];
let riskRegisterFile = null;

// ── Form submission ─
riskUploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  riskSubmitBtn.disabled = true;
  riskBtnText.textContent = "Parsing…";
  riskSpinner.classList.remove("hidden");
  riskStatusBox.classList.add("hidden");
  riskResultsSection.classList.add("hidden");
  riskResultsPlaceholder.classList.remove("hidden");

  try {
    const fd = new FormData(riskUploadForm);
    const regFile = document.getElementById("knowledgeFiles")?.files?.[0] || riskRegisterFile;
    if (regFile) fd.append("risk_register_file", regFile);

    const res = await fetch(`${API_BASE}/risk-manager/parse/`, {
      method: "POST",
      headers: authHeaders(),
      body: fd,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Server error ${res.status}`);
    }
    const data = await res.json();
    riskCandidates = (data.risks || []).map((r, i) => ({ ...r, _idx: i, _removed: false }));
    document.getElementById("riskResultsTitle").textContent =
      `Risk Candidates — ${data.project_code}${data.tranches ? " · " + data.tranches : ""}`;
    document.getElementById("riskCountBadge").textContent = `${riskCandidates.length} risks identified`;
    riskResultsPlaceholder.classList.add("hidden");
    riskResultsSection.classList.remove("hidden");
    renderRiskTable();
    if (data.manual_scoring_required) openScoringModal();
  } catch (err) {
    showRiskStatus(`❌ ${err.message}`, true);
    riskResultsPlaceholder.classList.remove("hidden");
  } finally {
    riskSubmitBtn.disabled = false;
    riskBtnText.textContent = "Parse & Score Risks";
    riskSpinner.classList.add("hidden");
  }
});

// ── Scoring scale ─────────────────────────────────────────────────────────────

const SCORE_LABELS = { 1:"Negligible",2:"Very Low",3:"Low",4:"Medium",5:"High",6:"Very High" };
const SCORE_COLOURS = { "Very High":"#dc2626","High":"#ea580c","Medium":"#d97706","Low":"#16a34a","Very Low":"#4ade80","Negligible":"#94a3b8" };
const SCORE_OPTIONS = [1,2,3,4,5,6].map(n => `<option value="${n}">${n} — ${SCORE_LABELS[n]}</option>`).join("");
const SCHEDULE_OPTIONS = [1,2,3,4,5,6].map(n => `<option value="${n}">${n} — ${SCORE_LABELS[n]}</option>`).join("");
function scheduleSelectHtml(val, idx) {
  return `<select class="score-select score-inline" data-idx="${idx}" data-field="current_schedule"><option value="0">— select —</option>${SCHEDULE_OPTIONS}</select>`
    .replace(`value="${val || 0}"`, `value="${val || 0}" selected`);
}


function calcAcumenRating(p, s) {
  const score = (p || 0) * (s || 0);
  if (!score) return { label:"—", score:0 };
  if (score >= 20) return { label:"Very High", score };
  if (score >= 12) return { label:"High", score };
  if (score >= 5)  return { label:"Medium", score };
  if (score >= 2)  return { label:"Low", score };
  return { label:"Negligible", score };
}

function scoreBadgeHtml(numVal) {
  if (!numVal) return '<span style="color:#94a3b8;font-size:0.78rem">—</span>';
  const label = SCORE_LABELS[numVal] || String(numVal);
  const colour = SCORE_COLOURS[label] || "#64748b";
  return `<span style="background:${colour}20;color:${colour};font-size:0.72rem;font-weight:700;padding:0.18rem 0.5rem;border-radius:4px;white-space:nowrap">${numVal} · ${escapeHtml(label)}</span>`;
}

function ratingBadgeHtml(label, score) {
  if (score === 0 || score === '' || score === undefined) return '<span style="color:#94a3b8;font-size:0.78rem">—</span>';
  const colour = SCORE_COLOURS[label] || "#64748b";
  return `<span style="background:${colour};color:#fff;font-size:0.75rem;font-weight:700;padding:0.25rem 0.6rem;border-radius:4px;white-space:nowrap">${escapeHtml(label)}&nbsp;<small style="opacity:0.8">(${score})</small></span>`;
}

function confidenceHtml(val) {
  const pct = Math.round((val || 0) * 100);
  const cls = pct >= 80 ? "high" : pct >= 60 ? "medium" : "low";
  return `<div class="confidence-bar"><span class="confidence-val ${cls}">${pct}%</span></div>`;
}

function categoryBadgeHtml(cat) {
  return `<span class="category-badge">${escapeHtml(cat || "")}</span>`;
}

function scoreSelectHtml(field, val, idx) {
  return `<select class="score-select score-inline" data-idx="${idx}" data-field="${field}"><option value="0">— select —</option>${SCORE_OPTIONS}</select>`
    .replace(`value="${val || 0}"`, `value="${val || 0}" selected`);
}

// ── Table rendering ──────────────────────────────────────────────────────────

function renderRiskTable() {
  const visible = riskCandidates.filter(r => !r._removed);
  document.getElementById("riskCountBadge").textContent = `${visible.length} risk${visible.length !== 1 ? "s" : ""}`;
  const rows = [];
  visible.forEach(r => {
    const rating = calcAcumenRating(r.current_probability, r.current_schedule);
    rows.push(`
      <tr data-idx="${r._idx}" class="risk-main-row ${r._selected ? "selected" : ""}">
        <td class="col-check"><input type="checkbox" class="risk-row-check" data-idx="${r._idx}" ${r._selected ? "checked" : ""}></td>
        <td class="col-actions"><button class="expand-evidence-btn" data-idx="${r._idx}" title="Show evidence">▶</button><button class="remove-risk-row-btn" data-idx="${r._idx}" title="Remove">✕</button></td>
        <td class="col-id">${escapeHtml(r.id || "")}<span class="source-tag ${r.risk_source === 'construction' ? 'source-const' : 'source-sched'}">${r.risk_source === 'construction' ? 'C' : 'S'}</span></td>
        <td class="col-category">${categoryBadgeHtml(r.category)}</td>
        <td class="col-name" contenteditable="true" data-idx="${r._idx}" data-field="risk_name">${escapeHtml(r.risk_name || "")}</td>
        <td class="col-type"><select class="type-select" data-idx="${r._idx}" data-field="type"><option ${r.type === "Threat" ? "selected" : ""}>Threat</option><option ${r.type === "Opportunity" ? "selected" : ""}>Opportunity</option></select></td>
        <td class="col-source">${escapeHtml(r.source_sheet || "")}${r.source_finding ? "<br><small>" + escapeHtml(r.source_finding) + "</small>" : ""}</td>
        <td class="col-confidence">${confidenceHtml(r.confidence)}</td>
        <td class="col-score">${scoreSelectHtml("current_probability", r.current_probability, r._idx)}</td>
        <td class="col-score">${scheduleSelectHtml(r.current_schedule, r._idx)}</td>
        <td class="col-rating" style="text-align:center;font-weight:700;color:#1a3c6e">${(r.current_probability || 0) * (r.current_schedule || 0) || "-"}</td>
      </tr>
      <tr class="evidence-detail-row hidden" data-for="${r._idx}">
        <td colspan="11" class="evidence-detail-cell">
          <div class="evidence-panel">
            <div class="evidence-section"><span class="evidence-label">📋 Source</span><span>${escapeHtml(r.source_sheet || "—")}</span></div>
            <div class="evidence-section"><span class="evidence-label">🔍 Evidence</span><span>${escapeHtml(r.evidence || "No evidence recorded.")}</span></div>
            <div class="evidence-section"><span class="evidence-label">🎯 Confidence</span><span>${Math.round((r.confidence || 0) * 100)}%</span></div>
          </div>
        </td>
      </tr>`);
  });
  riskTableBody.innerHTML = rows.join("");

  riskTableBody.querySelectorAll(".remove-risk-row-btn").forEach(btn => {
    btn.addEventListener("click", () => { riskCandidates[parseInt(btn.dataset.idx)]._removed = true; renderRiskTable(); });
  });
  riskTableBody.querySelectorAll(".expand-evidence-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const idx = btn.dataset.idx;
      const detailRow = riskTableBody.querySelector(`.evidence-detail-row[data-for="${idx}"]`);
      const isOpen = !detailRow.classList.contains("hidden");
      detailRow.classList.toggle("hidden", isOpen);
      btn.textContent = isOpen ? "▶" : "▼";
    });
  });
  riskTableBody.querySelectorAll("td[contenteditable]").forEach(cell => {
    cell.addEventListener("blur", () => { riskCandidates[parseInt(cell.dataset.idx)][cell.dataset.field] = cell.textContent.trim(); });
  });
  riskTableBody.querySelectorAll(".type-select").forEach(sel => {
    sel.addEventListener("change", () => { riskCandidates[parseInt(sel.dataset.idx)].type = sel.value; });
  });
  riskTableBody.querySelectorAll(".score-inline").forEach(sel => {
    sel.addEventListener("change", () => {
      const idx = parseInt(sel.dataset.idx);
      riskCandidates[idx][sel.dataset.field] = parseInt(sel.value) || 0;
      const mainRow = riskTableBody.querySelector(`.risk-main-row[data-idx="${idx}"]`);
      if (mainRow) {
        const r = riskCandidates[idx];
        mainRow.querySelector(".col-rating").textContent = ((r.current_probability || 0) * (r.current_schedule || 0)) || "-";
      }
    });
  });
  riskTableBody.querySelectorAll(".risk-row-check").forEach(cb => {
    cb.addEventListener("change", () => {
      const idx = parseInt(cb.dataset.idx);
      riskCandidates[idx]._selected = cb.checked;
      cb.closest("tr").classList.toggle("selected", cb.checked);
      updateMergeBtn();
    });
  });
  const selectAll = document.getElementById("riskSelectAll");
  if (selectAll) {
    selectAll.checked = false;
    selectAll.onchange = () => { riskCandidates.filter(r => !r._removed).forEach(r => { r._selected = selectAll.checked; }); renderRiskTable(); };
  }
}

function updateMergeBtn() {
  mergeRiskBtn.disabled = riskCandidates.filter(r => !r._removed && r._selected).length !== 2;
}

// ── Add risk row ─────────────────────────────────────────────────────────────
document.getElementById("addRiskBtn").addEventListener("click", () => {
  riskCandidates.push({
    _idx: riskCandidates.length, _removed: false, _selected: false,
    id: `R${riskCandidates.filter(r => !r._removed).length + 1}`,
    source_sheet: "", source_finding: "", category: "", risk_name: "", type: "Threat", shape: "Triangular",
    evidence: "", confidence: 0, current_probability: "", current_schedule: "", current_cost: "", current_score: "",
    mitigation_description: "", mitigation_duration: "", mitigation_cost: "",
    mitigated_probability: "", mitigated_schedule: "", mitigated_cost: "", mitigated_score: "",
  });
  riskResultsSection.classList.remove("hidden");
  riskResultsPlaceholder.classList.add("hidden");
  renderRiskTable();
});

// ── Merge ──────────────────────────────────────────────────────────────────
mergeRiskBtn.addEventListener("click", () => {
  const selected = riskCandidates.filter(r => !r._removed && r._selected);
  if (selected.length !== 2) return;
  const [a, b] = selected;
  riskCandidates[a._idx].evidence = [a.evidence, b.evidence].filter(Boolean).join(" | ");
  riskCandidates[a._idx].source_finding = [a.source_finding, b.source_finding].filter(Boolean).join("; ");
  riskCandidates[a._idx].confidence = Math.max(a.confidence || 0, b.confidence || 0);
  riskCandidates[a._idx]._selected = false;
  riskCandidates[b._idx]._removed = true;
  showToast("Risks merged.", false);
  renderRiskTable();
});

// ── Download risk register ─────────────────────────────────────────────────────
document.getElementById("downloadRiskBtn").addEventListener("click", async () => {
  const active = riskCandidates.filter(r => !r._removed);
  if (!active.length) { showRiskStatus("❌ No risks to export.", true); return; }
  const projectCode = (document.getElementById("riskProjectCode").value || "").trim().toUpperCase() || "RISK";
  try {
    const res = await fetch(`${API_BASE}/risk-manager/download/`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ project_code: projectCode, risks: active }),
    });
    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || "Server error"); }
    const data = await res.json();
    triggerDownload(b64ToBlob(data.xlsx_b64), `${projectCode}_Risk_Register.xlsx`);
    showRiskStatus(`✅ Downloaded ${projectCode}_Risk_Register.xlsx`);
  } catch (err) { showRiskStatus(`❌ ${err.message}`, true); }
});

// ── Save Risk KB ─────────────────────────────────────────────────────────────
document.getElementById("saveRiskKbBtn").addEventListener("click", async () => {
  const projectCode = (document.getElementById("riskProjectCode").value || "").trim().toUpperCase();
  if (!projectCode) { showRiskStatus("❌ Enter a project code first.", true); return; }
  const btn = document.getElementById("saveRiskKbBtn");
  btn.disabled = true; btn.textContent = "💾 Saving…";
  try {
    const res = await fetch(`${API_BASE}/risk-manager/kb/save/`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ project_code: projectCode, risks: riskCandidates }),
    });
    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || "Server error"); }
    const data = await res.json();
    showToast(`KB saved for ${projectCode}! ${data.corrections_saved} corrections.`, false, 4000);
    btn.textContent = "✅ Saved";
    setTimeout(() => { btn.textContent = "💾 Save to Knowledge Base"; btn.disabled = false; }, 4000);
  } catch (err) {
    showToast(`KB save failed: ${err.message}`, true, 5000);
    btn.textContent = "💾 Save to Knowledge Base"; btn.disabled = false;
  }
});

document.getElementById("knowledgeFiles").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  riskRegisterFile = file;
  const list = document.getElementById("knowledgeFileList");
  const statusEl = document.getElementById("knowledgeStatus");
  list.innerHTML = `<div class="knowledge-file-item">📄 <span>${escapeHtml(file.name)}</span></div>`;
  statusEl.textContent = "File selected — will be sent with the next parse run.";
  statusEl.className = "knowledge-status success";
  statusEl.classList.remove("hidden");
});

function showRiskStatus(msg, isError = false) {
  riskStatusBox.textContent = msg;
  riskStatusBox.className = `status-box ${isError ? "error" : "success"}`;
  riskStatusBox.classList.remove("hidden");
}

// ─── Scoring Modal ─────────────────────────────────────────────────────────

const scoringModal = document.getElementById("scoringModal");
const scoringTbody = document.getElementById("scoringTableBody");

function openScoringModal() {
  const visible = riskCandidates.filter(r => !r._removed && r.risk_source === "construction" && (!r.current_probability || !r.current_schedule));
  if (!visible.length) return;
  scoringTbody.innerHTML = visible.map(r => `
    <tr data-idx="${r._idx}">
      <td>${escapeHtml(r.id)}</td><td>${categoryBadgeHtml(r.category)}</td>
      <td style="max-width:240px;font-size:0.82rem">${escapeHtml(r.risk_name)}</td>
      <td><select class="score-select modal-score-sel" data-idx="${r._idx}" data-field="current_probability"><option value="0">— select —</option>${SCORE_OPTIONS}</select></td>
      <td><select class="score-select modal-score-sel" data-idx="${r._idx}" data-field="current_schedule"><option value="0">— select —</option>${SCHEDULE_OPTIONS}</select></td>
    </tr>`).join("");
  visible.forEach(r => {
    scoringTbody.querySelectorAll(`[data-idx="${r._idx}"]`).forEach(sel => {
      const val = r[sel.dataset.field] || 0;
      if (val) sel.value = String(val);
    });
  });
  scoringModal.classList.remove("hidden");
}

function closeScoringModal() { scoringModal.classList.add("hidden"); }
document.getElementById("closeScoringModal").addEventListener("click", closeScoringModal);
scoringModal.addEventListener("click", (e) => { if (e.target === scoringModal) closeScoringModal(); });

document.getElementById("applyBatchScores").addEventListener("click", () => {
  const bProb = parseInt(document.getElementById("batchProbability").value) || 0;
  const bImpact = parseInt(document.getElementById("batchImpact").value) || 0;
  scoringTbody.querySelectorAll(".modal-score-sel").forEach(sel => {
    const val = sel.dataset.field === "current_probability" ? bProb : bImpact;
    if (val) { sel.value = String(val); riskCandidates[parseInt(sel.dataset.idx)][sel.dataset.field] = val; }
  });
});

document.getElementById("applyScoringBtn").addEventListener("click", () => {
  scoringTbody.querySelectorAll(".modal-score-sel").forEach(sel => {
    riskCandidates[parseInt(sel.dataset.idx)][sel.dataset.field] = parseInt(sel.value) || 0;
  });
  closeScoringModal();
  renderRiskTable();
});

// ─────────────────────────────────────────────────────────────────────────────
// ACTIVITY LOG (Admin)
// ─────────────────────────────────────────────────────────────────────────────

let activityEvents = [];
let activityPage = 1;
const ACTIVITY_PAGE_SIZE = 50;
let activitySortCol = "triggered_at";
let activitySortDir = "desc";

function loadActivityLog() {
  fetchActivityEvents({ page: activityPage });
}

async function fetchActivityEvents({ page = 1, filters = {} } = {}) {
  const params = new URLSearchParams({ page, limit: ACTIVITY_PAGE_SIZE });
  if (filters.user) params.set("user_email", filters.user);
  if (filters.function) params.set("function_name", filters.function);
  if (filters.date_from) params.set("date_from", filters.date_from);
  if (filters.date_to) params.set("date_to", filters.date_to);
  if (filters.status) params.set("status", filters.status);
  if (filters.project_code) params.set("project_code", filters.project_code);
  params.set("sort_col", activitySortCol);
  params.set("sort_dir", activitySortDir);

  try {
    const res = await fetch(`${API_BASE}/api/admin/events?${params}`, { headers: authHeaders() });
    if (!res.ok) {
      const msg = res.status === 403 ? "Admin access required." : "Failed to load activity log.";
      document.getElementById("activityTableBody").innerHTML =
        `<tr><td colspan="7" class="empty-row">${msg}</td></tr>`;
      return;
    }
    const data = await res.json();
    activityEvents = data.events || [];
    activityPage = data.page || 1;
    renderActivityTable(data.total || 0);
  } catch {
    document.getElementById("activityTableBody").innerHTML =
      `<tr><td colspan="7" class="empty-row">Failed to load activity log.</td></tr>`;
  }
}

function renderActivityTable(total) {
  const tbody = document.getElementById("activityTableBody");
  if (!activityEvents.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-row">No activity records found.</td></tr>`;
    updatePagination(total);
    return;
  }
  const rows = activityEvents.map(ev => {
    const duration = ev.duration_ms ? `${Math.round(ev.duration_ms / 1000)}s` : "—";
    const status_cls = ev.status === "success" ? "status-success" : ev.status === "partial" ? "status-partial" : "status-failed";
    const status_label = (ev.status || "").charAt(0).toUpperCase() + (ev.status || "").slice(1);
    const fn_label = (ev.roles || []).join(", ") || (ev.function_name === "ims_generator" ? "IMS Generator" : ev.function_name || "—");
    const triggered = ev.triggered_at ? new Date(ev.triggered_at < 1e12 ? ev.triggered_at * 1000 : ev.triggered_at).toLocaleString() : "—";
    return `<tr data-event-id="${ev.event_id}">
      <td>${escapeHtml(ev.user_display_name || ev.user_email || "")}</td>
      <td>${fn_label}</td>
      <td>${escapeHtml(ev.project_code || "")}${ev.tranche ? " · " + escapeHtml(ev.tranche) : ""}</td>
      <td>${triggered}</td>
      <td>${duration}</td>
      <td><span class="status-badge ${status_cls}">${status_label}</span></td>
      <td><button class="view-detail-btn" data-id="${ev.event_id}">View</button></td>
    </tr>`;
  }).join("");
  tbody.innerHTML = rows;

  tbody.querySelectorAll(".view-detail-btn").forEach(btn => {
    btn.addEventListener("click", () => openActivityDetail(btn.dataset.id));
  });

  updatePagination(total);
}

function updatePagination(total) {
  const totalPages = Math.ceil(total / ACTIVITY_PAGE_SIZE) || 1;
  document.getElementById("pageInfo").textContent = `Page ${activityPage} of ${totalPages}`;
  document.getElementById("prevPage").disabled = activityPage <= 1;
  document.getElementById("nextPage").disabled = activityPage >= totalPages;
}

function getActivityFilters() {
  return {
    user: document.getElementById("filterUser")?.value.trim(),
    function: document.getElementById("filterFunction")?.value,
    date_from: document.getElementById("filterDateFrom")?.value,
    date_to: document.getElementById("filterDateTo")?.value,
    status: document.getElementById("filterStatus")?.value,
    project_code: document.getElementById("filterProject")?.value.trim(),
  };
}

// Filter apply/reset
document.getElementById("applyFilters")?.addEventListener("click", () => {
  activityPage = 1;
  fetchActivityEvents({ page: 1, filters: getActivityFilters() });
});

document.getElementById("resetFilters")?.addEventListener("click", () => {
  ["filterUser","filterDateFrom","filterDateTo","filterProject"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
  ["filterFunction","filterStatus"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = "";
  });
  activityPage = 1;
  fetchActivityEvents({ page: 1, filters: {} });
});

// Pagination
document.getElementById("prevPage")?.addEventListener("click", () => {
  if (activityPage > 1) { activityPage--; fetchActivityEvents({ page: activityPage, filters: getActivityFilters() }); }
});
document.getElementById("nextPage")?.addEventListener("click", () => {
  activityPage++; fetchActivityEvents({ page: activityPage, filters: getActivityFilters() });
});

// CSV export
document.getElementById("exportCsv")?.addEventListener("click", () => {
  if (!activityEvents.length) return;
  const headers = ["User","Function","Project","Tranche","Triggered At","Duration (s)","Status","Error"];
  const rows = activityEvents.map(ev => [
    ev.user_email || "",
    ev.function_name || "",
    ev.project_code || "",
    ev.tranche || "",
    ev.triggered_at || "",
    ev.duration_ms ? (ev.duration_ms / 1000).toFixed(1) : "",
    ev.status || "",
    (ev.error_message || "").replace(/"/g, '""'),
  ]);
  const csv = [headers, ...rows].map(r => r.map(c => `"${c}"`).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  triggerDownload(blob, `activity-log-${new Date().toISOString().slice(0,10)}.csv`);
});

// Activity detail modal
async function openActivityDetail(eventId) {
  const modal = document.getElementById("activityDetailModal");
  const body = document.getElementById("activityDetailBody");
  const title = document.getElementById("activityDetailTitle");
  body.innerHTML = "<p>Loading…</p>";
  title.textContent = "Activity Detail";
  modal.classList.remove("hidden");

  try {
    const res = await fetch(`${API_BASE}/api/admin/events/${eventId}`, { headers: authHeaders() });
    if (!res.ok) { body.innerHTML = "<p>Failed to load detail.</p>"; return; }
    const ev = await res.json();
    title.textContent = `${ev.function_name === "ims_generator" ? "IMS Generator" : "Risk Manager"} — ${ev.project_code || ""}`;
    body.innerHTML = renderActivityDetailHtml(ev);
  } catch {
    body.innerHTML = "<p>Failed to load detail.</p>";
  }
}

function renderActivityDetailHtml(ev) {
  const triggered = ev.triggered_at ? new Date(ev.triggered_at < 1e12 ? ev.triggered_at * 1000 : ev.triggered_at).toLocaleString() : "—";
  const completed = ev.completed_at ? new Date(ev.completed_at < 1e12 ? ev.completed_at * 1000 : ev.completed_at).toLocaleString() : "—";
  const duration = ev.duration_ms ? `${Math.round(ev.duration_ms / 1000)}s` : "—";
  const status_cls = ev.status === "success" ? "status-success" : ev.status === "partial" ? "status-partial" : "status-failed";
  const inputFiles = (ev.input_files || []).map(f => `<li>${escapeHtml(f.filename || "")} <small>(${f.size || "?"} bytes, SHA-256: ${(f.sha256 || "").slice(0,12)}…)</small></li>`).join("");

  const rs = ev.result_summary || {};
  const summaryHtml = rs.risk_count != null
    ? `<div class="detail-row"><span class="detail-label">Risk Count</span><span>${rs.risk_count}</span></div>
       <div class="detail-row"><span class="detail-label">Manual Scoring</span><span>${rs.manual_scoring_required ? "Required" : "Not required"}</span></div>`
    : `<pre class="detail-summary">${escapeHtml(JSON.stringify(rs, null, 2))}</pre>`;

  const risks = rs.risks || [];
  const riskTableHtml = risks.length > 0 ? `
    <div class="detail-section detail-full-width">
      <h4>Risk Register (${risks.length} risk${risks.length !== 1 ? "s" : ""})</h4>
      <div class="detail-risk-table-wrap">
        <table class="detail-risk-table">
          <thead>
            <tr>
              <th>ID</th><th>Category</th><th>Risk Name</th><th>Type</th>
              <th>Source</th><th>Confidence</th><th>Probability</th><th>Schedule Impact</th><th>Risk Score</th>
            </tr>
          </thead>
          <tbody>
            ${risks.map(r => {
              const prob = r.current_probability || 0;
              const sched = r.current_schedule || 0;
              const score = prob && sched ? prob * sched : "—";
              const probLabel = prob ? `${prob} — ${SCORE_LABELS[prob] || ""}` : "—";
              const schedLabel = sched ? `${sched} — ${SCORE_LABELS[sched] || ""}` : "—";
              const confPct = r.confidence != null ? Math.round((r.confidence || 0) * 100) + "%" : "—";
              return `<tr>
                <td>${escapeHtml(r.id || "")}</td>
                <td>${escapeHtml(r.category || "")}</td>
                <td>${escapeHtml(r.risk_name || "")}</td>
                <td>${escapeHtml(r.type || "")}</td>
                <td>${escapeHtml(r.source_sheet || "")}</td>
                <td style="text-align:center">${confPct}</td>
                <td>${probLabel}</td>
                <td>${schedLabel}</td>
                <td style="font-weight:700;text-align:center">${score}</td>
              </tr>`;
            }).join("")}
          </tbody>
        </table>
      </div>
    </div>` : "";

  // Build activities table for IMS Generator runs
  const projectDetails = rs.project_details || [];
  const imsTableHtml = projectDetails.length > 0 ? projectDetails.map(proj => {
    const activities = proj.activities || [];
    if (!activities.length) return "";
    return `
      <div class="detail-section detail-full-width">
        <h4>Generated Activities — ${escapeHtml(proj.project_code || "")} (${proj.row_count || 0} total, showing ${activities.length})</h4>
        <div class="detail-activities-table-wrap">
          <table class="detail-activities-table">
            <thead>
              <tr>
                <th>Activity ID</th><th>Original PDF Name</th><th>Activity Name</th><th>Status</th><th>Constraint</th><th>Constraint Date</th>
              </tr>
            </thead>
            <tbody>
              ${activities.map(a => `<tr>
                <td>${escapeHtml(a.task_code || "")}</td>
                <td>${escapeHtml(a.pdf_activity_name || "")}</td>
                <td>${escapeHtml(a.task_name || "")}</td>
                <td>${escapeHtml(a.status_code || "")}</td>
                <td>${escapeHtml(a.cstr_type || "")}</td>
                <td>${escapeHtml(a.cstr_date || "")}</td>
              </tr>`).join("")}
            </tbody>
          </table>
        </div>
      </div>`;
  }).join("") : "";

  return `<div class="activity-detail-grid">
    <div class="detail-section">
      <h4>Run Information</h4>
      <div class="detail-row"><span class="detail-label">User</span><span>${escapeHtml(ev.user_display_name || ev.user_email || "")}</span></div>
      <div class="detail-row"><span class="detail-label">Email</span><span>${escapeHtml(ev.user_email || "")}</span></div>
      <div class="detail-row"><span class="detail-label">Function</span><span>${escapeHtml(ev.function_name || "")}</span></div>
      <div class="detail-row"><span class="detail-label">Status</span><span class="status-badge ${status_cls}">${(ev.status || "").charAt(0).toUpperCase() + (ev.status || "").slice(1)}</span></div>
      <div class="detail-row"><span class="detail-label">Project / Tranche</span><span>${escapeHtml(ev.project_code || "")}${ev.tranche ? " · " + escapeHtml(ev.tranche) : ""}</span></div>
      <div class="detail-row"><span class="detail-label">Triggered At</span><span>${triggered}</span></div>
      <div class="detail-row"><span class="detail-label">Completed At</span><span>${completed}</span></div>
      <div class="detail-row"><span class="detail-label">Duration</span><span>${duration}</span></div>
    </div>
    <div class="detail-right-col">
      <div class="detail-section">
        <h4>Input Files</h4>
        ${inputFiles ? `<ul class="detail-file-list">${inputFiles}</ul>` : "<p>No input files recorded.</p>"}
      </div>
      <div class="detail-section">
        <h4>Result Summary</h4>
        ${summaryHtml}
      </div>
    </div>
    ${imsTableHtml}
    ${riskTableHtml}
    ${ev.error_message ? `<div class="detail-section detail-full-width"><h4>Error</h4><div class="detail-error">${escapeHtml(ev.error_message)}</div></div>` : ""}
  </div>`;
}

document.getElementById("closeActivityDetail")?.addEventListener("click", () => {
  document.getElementById("activityDetailModal").classList.add("hidden");
});
document.getElementById("activityDetailModal")?.addEventListener("click", (e) => {
  if (e.target === document.getElementById("activityDetailModal")) {
    document.getElementById("activityDetailModal").classList.add("hidden");
  }
});

// ─── Initialize ─────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", initAuth);
