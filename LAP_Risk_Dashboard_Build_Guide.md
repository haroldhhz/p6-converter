# LAP Risk Dashboard — Build Guide

## Module: Dynamic Regional Risk Comparison & Cascading Filters

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  DATA INGESTION LAYER (Schedule Risk Manager)               │
│                                                             │
│  User inputs "Project Name Revision" (e.g. "PNQ24 T1")     │
│         │                                                   │
│         ▼                                                   │
│  ┌─────────────────────────────────┐                        │
│  │  AI Location Resolver (Server)  │                        │
│  │  • Step 1: Extract Code         │                        │
│  │  • Step 2: Standard Biz Match   │                        │
│  │  • Step 3: IATA Fallback        │                        │
│  │  • Step 4: Validation Gate      │                        │
│  └──────────────┬──────────────────┘                        │
│                 │                                            │
│                 ▼                                            │
│  Enriched Risk Record saved to Cloud:                       │
│  { ...riskData, Region, SubRegion, Metro, ProjectID }       │
└─────────────────────────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│  RISK DASHBOARD (Read-only Consumer)                        │
│                                                             │
│  Fetches enriched risk data from Knowledge Base / Cloud     │
│  Renders cascading filters + dynamic comparisons            │
└─────────────────────────────────────────────────────────────┘
```

The project code resolver does NOT appear on the dashboard. Resolution happens at ingestion time (when the user clicks "Parse & Score Risks" in the Schedule Risk Manager). The resolved `Region`, `SubRegion`, and `Metro` fields are persisted alongside each risk record in the cloud datastore.

---

## 2. Data Model

### 2.1 Enriched Risk Record (stored in cloud)

```json
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
```

### 2.2 Aggregated View Model (computed client-side)

```typescript
interface LocationAggregate {
  name: string;             // Region, SubRegion, or Metro name
  riskCount: number;        // Total risks in this grouping
  avgProbability: number;   // Mean probability score (1-6)
  avgImpact: number;        // Mean schedule impact score (1-6)
  avgRiskScore: number;     // Mean of (probability × impact) / 6
  criticalCount: number;    // Risks with prob≥5 OR impact≥5
  highCount: number;        // Risks with max(prob,impact) = 4
  projects: string[];       // Unique project codes within
}
```

---

## 3. Project Code Resolution (Ingestion-Time)

### 3.1 When It Runs

The resolver is triggered server-side when the user submits a risk parse job in the Schedule Risk Manager. The `Project Code` field value is sent to the AI model along with the resolution prompt.

### 3.2 Integration Point

```
POST /api/risks/parse
Body: { file, projectCode, tranche, assessmentDate, constructionRegister? }

Server flow:
  1. Parse risks from Excel (existing logic)
  2. Call AI resolver with projectCode → get { Region, SubRegion, Metro, IsStandard }
  3. Attach location fields to every risk record from this parse
  4. Save enriched records to cloud datastore
  5. Return results to client
```

### 3.3 AI Resolver Prompt

The full prompt (provided by the user) implements a 5-step resolution:

| Step | Logic | Priority |
|------|-------|----------|
| 1 | Extract `ProjectID` and `Code` (first 3 alpha chars) | — |
| 2 | Match against Standard Business Code table (10 entries) | HIGHEST |
| 3 | If no Step 2 match → IATA fallback with mandatory anchors | FALLBACK |
| 4 | Validation gate — verify geographic consistency | — |
| 5 | Return strict JSON | — |

**Standard Business Code Table (Step 2):**

| Code | Metro | Region | SubRegion |
|------|-------|--------|-----------|
| HYD | Hyderabad | APAC | APAC India |
| KUL | Kuala Lumpur | APAC | APAC South East Asia |
| MEL | Melbourne | APAC | APAC Australia New Zealand |
| TPE | Taipei | APAC | APAC North East Asia |
| TYO | Tokyo | APAC | APAC North East Asia |
| SEL | Seoul | APAC | APAC North East Asia |
| SYD | Sydney | APAC | APAC Australia New Zealand |
| OSA | Osaka | APAC | APAC North East Asia |
| JHB | Johor Bahru | APAC | APAC South East Asia |
| JNB | Johannesburg | EMEA | EMEA Middle East, Africa, & Emerging |

**Approved Regions:** `APAC`, `EMEA`, `AMER`

**Approved SubRegions:**

| Region | SubRegions |
|--------|-----------|
| AMER | AMER US Canada & LATAM, AMER US Central, AMER US East, AMER US West |
| APAC | APAC Australia New Zealand, APAC India, APAC North East Asia, APAC South East Asia |
| EMEA | EMEA Ireland & Nordics, EMEA Middle East Africa & Emerging, EMEA South & East, EMEA UK, EMEA West & Central |

### 3.4 Caching Strategy

To avoid repeated AI calls for the same code:

```
Cloud lookup table: code_resolutions
┌──────┬──────────────┬────────┬──────────────────────────────┬────────────┐
│ Code │ Metro        │ Region │ SubRegion                    │ IsStandard │
├──────┼──────────────┼────────┼──────────────────────────────┼────────────┤
│ PNQ  │ Pune         │ APAC   │ APAC India                   │ No         │
│ KUL  │ Kuala Lumpur │ APAC   │ APAC South East Asia         │ Yes        │
│ LHR  │ London       │ EMEA   │ EMEA UK                      │ No         │
└──────┴──────────────┴────────┴──────────────────────────────┴────────────┘
```

On each parse request:
1. Check if `code` exists in `code_resolutions`
2. If yes → use cached result (no AI call)
3. If no → call AI resolver → save to `code_resolutions` + attach to risks

---

## 4. Dashboard Cascading Filters

### 4.1 Filter Hierarchy

```
┌────────────────────────────────────────────────────────┐
│  PORTFOLIO (All)                                       │
│    └── REGION filter     [APAC | EMEA | AMER]          │
│          └── SUBREGION filter  [dynamic based on region]│
│                └── METRO filter [dynamic based on sub]  │
└────────────────────────────────────────────────────────┘
```

### 4.2 Filter Component Specification

```
┌─────────────────────────────────────────────────────────────────┐
│  View Level:  [Portfolio ▾]   Region: [All ▾]                   │
│               Subregion: [All ▾]   Metro: [All ▾]               │
└─────────────────────────────────────────────────────────────────┘
```

**Behaviour rules:**

| User Action | Dashboard Response |
|-------------|-------------------|
| View Level = "Portfolio" | Show Region comparison heatmap. Region/Sub/Metro dropdowns disabled. |
| View Level = "Region" + selects a Region | Show SubRegion comparison heatmap for that Region. Sub/Metro populate. |
| View Level = "SubRegion" + selects a SubRegion | Show Metro comparison heatmap for that SubRegion. Metro populates. |
| View Level = "Metro" + selects a Metro | Show per-project detail for that Metro. |

**Dropdown cascade logic:**

```javascript
function onViewLevelChange(level) {
  switch(level) {
    case 'portfolio':
      disableDropdowns(['region','subregion','metro']);
      renderRegionComparison(allRisks);
      break;
    case 'region':
      enableDropdown('region');
      disableDropdowns(['subregion','metro']);
      populateRegionDropdown();  // [APAC, EMEA, AMER]
      break;
    case 'subregion':
      enableDropdowns(['region','subregion']);
      disableDropdown('metro');
      // subregion options depend on selected region
      break;
    case 'metro':
      enableDropdowns(['region','subregion','metro']);
      // metro options depend on selected subregion
      break;
  }
}

function onRegionChange(region) {
  const subs = risks
    .filter(r => r.region === region)
    .map(r => r.subRegion)
    .filter(unique);
  populateSubRegionDropdown(subs);
  resetDropdown('metro');

  if (viewLevel === 'region') {
    renderSubRegionComparison(risks.filter(r => r.region === region));
  }
}

function onSubRegionChange(subRegion) {
  const metros = risks
    .filter(r => r.subRegion === subRegion)
    .map(r => r.metro)
    .filter(unique);
  populateMetroDropdown(metros);

  if (viewLevel === 'subregion') {
    renderMetroComparison(risks.filter(r => r.subRegion === subRegion));
  }
}
```

---

## 5. KPI Summary Cards

### 5.1 Purpose

A row of KPI cards sits **above** the comparison heatmap and provides at-a-glance portfolio health metrics. The cards dynamically update based on the current filter/view level selection — if the user drills into APAC, the KPIs reflect only APAC data.

### 5.2 Visual Specification

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ Total Risks  │  │   Critical   │  │    High      │  │   Medium     │  │     Low      │  │  Avg Score   │ │
│  │     42       │  │      8       │  │     10       │  │     14       │  │     10       │  │     3.4      │ │
│  │ 6 projects   │  │ Escalation   │  │ Monitoring   │  │ Active mgmt  │  │ Std controls │  │  P × I / 6   │ │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### 5.3 KPI Definitions

| KPI | Calculation | Color | Threshold Logic |
|-----|------------|-------|-----------------|
| Total Risks | `count(filteredRisks)` | Accent (indigo) | Always accent |
| Critical | `count(risks where prob ≥ 5 OR impact ≥ 5)` | Red | Always red |
| High | `count(risks where max(prob,impact) = 4 AND prob < 5 AND impact < 5)` | Orange | Always orange |
| Medium | `count(risks where max(prob,impact) = 3 AND prob < 4 AND impact < 4)` | Yellow | Always yellow |
| Low | `count(risks where max(prob,impact) ≤ 2)` | Green | Always green |
| Avg Risk Score | `mean(prob × impact) / 6` for filtered set | Dynamic | Green ≤2, Yellow ≤3.5, Orange ≤4.5, Red >4.5 |

### 5.4 Dynamic Context Label

Each KPI card shows a secondary label that reflects the current scope:

| View Level | Secondary Label Example |
|------------|----------------------|
| Portfolio | "Across 3 regions" |
| Region (APAC) | "Across 4 subregions" |
| SubRegion (APAC India) | "Across 3 metros" |
| Metro (Pune) | "Across 2 projects" |

### 5.5 Computation

```javascript
function computeKPIs(filteredRisks, viewLevel, childGroupField) {
  const total = filteredRisks.length;
  const critical = filteredRisks.filter(r => r.probability >= 5 || r.impact >= 5).length;
  const high = filteredRisks.filter(r => {
    const mx = Math.max(r.probability, r.impact);
    return mx === 4 && r.probability < 5 && r.impact < 5;
  }).length;
  const medium = filteredRisks.filter(r => {
    const mx = Math.max(r.probability, r.impact);
    return mx === 3 && r.probability < 4 && r.impact < 4;
  }).length;
  const low = total - critical - high - medium;

  const avgScore = total > 0
    ? filteredRisks.reduce((sum, r) => sum + (r.probability * r.impact), 0) / total / 6
    : 0;

  // Count child groups for context label
  const childGroups = new Set(filteredRisks.map(r => r[childGroupField])).size;
  const groupLabels = {
    region: `Across ${childGroups} subregion${childGroups !== 1 ? 's' : ''}`,
    subRegion: `Across ${childGroups} metro${childGroups !== 1 ? 's' : ''}`,
    metro: `Across ${childGroups} project${childGroups !== 1 ? 's' : ''}`,
    projectId: `${childGroups} tranche${childGroups !== 1 ? 's' : ''}`
  };

  return {
    total,
    critical,
    high,
    medium,
    low,
    avgScore: avgScore.toFixed(1),
    contextLabel: groupLabels[childGroupField] || `Across ${childGroups} items`
  };
}
```

### 5.6 Colour Logic for Avg Score Card

```javascript
function getAvgScoreColor(score) {
  if (score <= 2.0) return '--color-low';       // green
  if (score <= 3.5) return '--color-moderate';   // yellow
  if (score <= 4.5) return '--color-high';       // orange
  return '--color-critical';                     // red
}
```

### 5.7 Responsive Layout

- Desktop (≥1024px): 6 cards in a single row, equal width
- Tablet (768–1023px): 3 cards per row × 2 rows
- Mobile (<768px): 2 cards per row × 3 rows

```css
.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
```

---

## 6. Dynamic Heatmap Comparisons

### 6.1 Concept

Instead of a traditional P×I heatmap, this section shows **comparative bar/heatmap** of average risk scores across locations at the selected view level. The KPI cards above give the aggregate picture; the comparison rows below break it down by location.

### 6.2 Visual Specification

```
┌─────────────────────────────────────────────────────────┐
│  Average Risk Comparison — by [Region/SubRegion/Metro]  │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │         Avg Prob  │  Avg Impact  │  Avg Score   │    │
│  ├─────────────────────────────────────────────────┤    │
│  │  APAC      ████▌  │   █████▌    │   █████      │    │
│  │  EMEA      ███▌   │   ████▌     │   ████       │    │
│  │  AMER      ███    │   ████      │   ███▌       │    │
│  └─────────────────────────────────────────────────┘    │
│                                                         │
│  Color scale: 1-2 Green | 3-4 Amber | 5-6 Red          │
└─────────────────────────────────────────────────────────┘
```

### 6.3 Computation

```javascript
function computeLocationAggregates(risks, groupByField) {
  // groupByField = 'region' | 'subRegion' | 'metro'
  const groups = {};

  risks.forEach(risk => {
    const key = risk[groupByField];
    if (!groups[key]) {
      groups[key] = { name: key, risks: [], projects: new Set() };
    }
    groups[key].risks.push(risk);
    groups[key].projects.add(risk.projectId);
  });

  return Object.values(groups).map(g => ({
    name: g.name,
    riskCount: g.risks.length,
    projectCount: g.projects.size,
    avgProbability: mean(g.risks.map(r => r.probability)),
    avgImpact: mean(g.risks.map(r => r.impact)),
    avgRiskScore: mean(g.risks.map(r => (r.probability * r.impact) / 6)),
    criticalCount: g.risks.filter(r => r.probability >= 5 || r.impact >= 5).length,
    maxProbability: Math.max(...g.risks.map(r => r.probability)),
    maxImpact: Math.max(...g.risks.map(r => r.impact)),
  }));
}

function mean(arr) {
  return arr.length ? arr.reduce((s, v) => s + v, 0) / arr.length : 0;
}
```

### 6.4 Rendering Rules

| View Level | Heatmap Shows | Rows | Columns |
|------------|--------------|------|---------|
| Portfolio | Region comparison | APAC, EMEA, AMER (up to 3 rows) | Avg Prob, Avg Impact, Avg Score, Risk Count, Critical Count |
| Region (e.g. APAC selected) | SubRegion comparison | All subregions within APAC | Same columns |
| SubRegion (e.g. APAC India selected) | Metro comparison | All metros within APAC India | Same columns |
| Metro (e.g. Pune selected) | Project comparison | All projects within Pune | Same columns |

### 6.5 Color Encoding

Each cell is colored based on its value relative to the 1–6 scale:

```javascript
function getHeatColor(value, maxScale = 6) {
  const ratio = value / maxScale;
  if (ratio <= 0.33) return 'var(--color-low)';      // #22c55e (green)
  if (ratio <= 0.50) return 'var(--color-moderate)';  // #eab308 (yellow)
  if (ratio <= 0.67) return 'var(--color-medium)';    // #f97316 (orange)
  if (ratio <= 0.83) return 'var(--color-high)';      // #ef4444 (red)
  return 'var(--color-critical)';                     // #dc2626 (dark red)
}
```

---

## 7. Regional Overview (Dynamic View Level)

### 7.1 Behaviour

The Regional Overview section below the heatmap dynamically adjusts its display based on the current view level:

| View Level | Regional Overview Shows |
|------------|----------------------|
| Portfolio | Expandable cards for each Region, showing total risks + top risk categories |
| Region | Cards for each SubRegion within the selected Region, with project counts |
| SubRegion | Cards for each Metro, showing individual projects and risk summaries |
| Metro | Full risk table for the selected Metro (same as Schedule Risk Manager output) |

### 7.2 Card Component Specification

```
┌────────────────────────────────────────────────┐
│  APAC India                          12 risks  │
│  ──────────────────────────────────────────── │
│  Projects: PNQ24, BOM25, HYD24                 │
│  Avg Score: 3.8  │  Critical: 3  │  High: 5   │
│                                                │
│  Top Categories:                               │
│    Weather ████████  (4)                       │
│    Procurement ████  (3)                       │
│    Geotechnical ███  (2)                       │
└────────────────────────────────────────────────┘
```

### 7.3 Drill-Down Interaction

Clicking a card at any level drills down to the next level:

```javascript
function onCardClick(item, currentLevel) {
  switch(currentLevel) {
    case 'portfolio':
      // Clicked a Region card → switch to Region view
      setViewLevel('region');
      setRegionFilter(item.name);
      break;
    case 'region':
      // Clicked a SubRegion card → switch to SubRegion view
      setViewLevel('subregion');
      setSubRegionFilter(item.name);
      break;
    case 'subregion':
      // Clicked a Metro card → switch to Metro view
      setViewLevel('metro');
      setMetroFilter(item.name);
      break;
    case 'metro':
      // Clicked a Project → open risk detail modal
      openProjectDetail(item.projectId);
      break;
  }
  renderAll();
}
```

### 7.4 Breadcrumb Navigation

A breadcrumb trail shows the current drill-down path and allows quick navigation back:

```
Portfolio  >  APAC  >  APAC India  >  Pune
[click any segment to jump back to that level]
```

```html
<div class="breadcrumb">
  <span class="crumb clickable" onclick="jumpTo('portfolio')">Portfolio</span>
  <span class="crumb-sep">›</span>
  <span class="crumb clickable" onclick="jumpTo('region')">APAC</span>
  <span class="crumb-sep">›</span>
  <span class="crumb clickable" onclick="jumpTo('subregion')">APAC India</span>
  <span class="crumb-sep">›</span>
  <span class="crumb current">Pune</span>
</div>
```

---

## 8. API Endpoints

### 8.1 Dashboard Data Fetch

```
GET /api/dashboard/risks
Query params:
  ?region=APAC              (optional filter)
  &subRegion=APAC India     (optional filter)
  &metro=Pune               (optional filter)
  &type=S|C                 (optional: schedule or construction)

Response:
{
  "risks": [ ...enriched risk records... ],
  "aggregates": {
    "byRegion": [ { name, riskCount, avgProbability, avgImpact, ... } ],
    "bySubRegion": [ ... ],
    "byMetro": [ ... ]
  },
  "filters": {
    "regions": ["APAC", "EMEA", "AMER"],
    "subRegions": ["APAC India", "APAC South East Asia", ...],
    "metros": ["Pune", "Kuala Lumpur", ...]
  }
}
```

### 8.2 Code Resolution (called at ingestion)

```
POST /api/resolve-location
Body: { "projectNameRevision": "PNQ24 T1-2026.02" }

Response:
{
  "ProjectID": "PNQ24",
  "Code": "PNQ",
  "Region": "APAC",
  "Metro": "Pune",
  "SubRegion": "APAC India",
  "IsStandard": "No"
}
```

---

## 9. State Management

### 9.1 Dashboard State Object

```javascript
const dashboardState = {
  // Current view level
  viewLevel: 'portfolio',  // 'portfolio' | 'region' | 'subregion' | 'metro'

  // Active filters
  filters: {
    region: null,           // null = all
    subRegion: null,
    metro: null,
    riskType: null,         // null | 'S' | 'C'
  },

  // Breadcrumb trail
  breadcrumb: ['Portfolio'],

  // Loaded data
  allRisks: [],
  filteredRisks: [],
  aggregates: [],
};
```

### 9.2 State Transitions

```javascript
function applyFilters() {
  let risks = dashboardState.allRisks;

  if (dashboardState.filters.region) {
    risks = risks.filter(r => r.region === dashboardState.filters.region);
  }
  if (dashboardState.filters.subRegion) {
    risks = risks.filter(r => r.subRegion === dashboardState.filters.subRegion);
  }
  if (dashboardState.filters.metro) {
    risks = risks.filter(r => r.metro === dashboardState.filters.metro);
  }
  if (dashboardState.filters.riskType) {
    risks = risks.filter(r => r.type === dashboardState.filters.riskType);
  }

  dashboardState.filteredRisks = risks;

  // Compute aggregates based on current view level
  const groupField = {
    portfolio: 'region',
    region: 'subRegion',
    subregion: 'metro',
    metro: 'projectId'
  }[dashboardState.viewLevel];

  dashboardState.aggregates = computeLocationAggregates(risks, groupField);

  renderHeatmapComparison();
  renderRegionalOverview();
  renderBreadcrumb();
  updateDropdownOptions();
}
```

---

## 10. Component Hierarchy

```
RiskDashboardPage
├── BreadcrumbNav
├── FilterBar
│   ├── ViewLevelDropdown
│   ├── RegionDropdown
│   ├── SubRegionDropdown
│   ├── MetroDropdown
│   └── RiskTypeChips (All / Schedule / Construction)
├── KPISummaryRow                          ◄── NEW: Section 5
│   ├── KPICard (Total Risks)    — accent
│   ├── KPICard (Critical 5-6)   — red
│   ├── KPICard (High 4)         — orange
│   ├── KPICard (Medium 3)       — yellow
│   ├── KPICard (Low 1-2)        — green
│   └── KPICard (Avg Score)      — dynamic color
├── ComparisonHeatmap
│   ├── HeatmapHeader (dynamic title based on view level)
│   ├── HeatmapRows[] (one per location at current level)
│   │   ├── LocationLabel
│   │   ├── AvgProbabilityCell
│   │   ├── AvgImpactCell
│   │   ├── AvgScoreCell
│   │   ├── RiskCountBadge
│   │   └── CriticalBadge
│   └── ColorScaleLegend
└── RegionalOverview
    ├── OverviewHeader (dynamic title)
    └── LocationCards[] (clickable, drill-down)
        ├── CardTitle + Badge
        ├── ProjectList
        ├── RiskSummaryBar
        └── TopCategoriesMini
```

---

## 11. Implementation Checklist

### Backend

- [ ] Add `region`, `subRegion`, `metro`, `isStandard` columns to risk records table
- [ ] Create `code_resolutions` cache table
- [ ] Implement `/api/resolve-location` endpoint with the AI resolver prompt
- [ ] Modify `/api/risks/parse` to call resolver and enrich records before saving
- [ ] Implement `/api/dashboard/risks` with aggregation and filter support
- [ ] Backfill existing Knowledge Base records with location data

### Frontend

- [ ] Add "Risk Dashboard" nav item to sidebar
- [ ] Build `FilterBar` component with cascading dropdown logic
- [ ] Build `BreadcrumbNav` component
- [ ] Build `ComparisonHeatmap` component with dynamic row rendering
- [ ] Build `RegionalOverview` with clickable cards and drill-down
- [ ] Build `StatsRow` that updates based on filtered data
- [ ] Implement state management for view level transitions
- [ ] Add loading states and empty states for each view level
- [ ] Add animation transitions between view levels

### Testing

- [ ] Verify PNQ → Pune / APAC India / APAC resolution
- [ ] Verify KUL → Kuala Lumpur / APAC South East Asia / APAC (Standard match, not IATA)
- [ ] Verify cascading filter disables correct dropdowns
- [ ] Verify drill-down navigation updates all components
- [ ] Verify breadcrumb back-navigation resets state correctly
- [ ] Verify aggregates recalculate on filter change
- [ ] Test with 0 risks in a location (empty state)
- [ ] Test with single risk per location (edge case for averages)

---

## 12. Accepted Regions & SubRegions Reference

This is the authoritative taxonomy. The dashboard dropdowns must use these exact strings:

```
AMER
├── AMER US Canada & LATAM
├── AMER US Central
├── AMER US East
└── AMER US West

APAC
├── APAC Australia New Zealand
├── APAC India
├── APAC North East Asia
└── APAC South East Asia

EMEA
├── EMEA Ireland & Nordics
├── EMEA Middle East Africa & Emerging
├── EMEA South & East
├── EMEA UK
└── EMEA West & Central
```

---

*End of Build Guide*
