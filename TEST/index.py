"""
PostgreSQL Database Reader
Reads rainfall_station and subbasin_gis tables
Run: python db_reader.py
Open: http://localhost:5000
"""

from flask import Flask, jsonify, Response
import psycopg2
import json
import os

# Reads the same ignored .env as chatbox.py, so no password sits in the source.
from chatbox import _load_dotenv

_load_dotenv()

app = Flask(__name__)

# ========== Database Connection Config ==========
DB_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "database": os.environ.get("PGDATABASE", "postgres"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", "")
}


def get_connection():
    """Get a database connection"""
    return psycopg2.connect(**DB_CONFIG)


def serialize_row(row, columns):
    """Convert a database row to a dict, handling non-serializable types"""
    result = {}
    for col, val in zip(columns, row):
        try:
            json.dumps(val)
            result[col] = val
        except (TypeError, ValueError):
            result[col] = str(val)
    return result


# ========== HTML Page ==========
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Database Viewer - Rainfall & Subbasin</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:wght@300;400;600;800&display=swap" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
<style>
  :root {
    --bg: #0c0f14;
    --bg-card: #141820;
    --bg-card-hover: #1a2030;
    --fg: #e8ecf1;
    --fg-muted: #6b7a8d;
    --accent: #00d68f;
    --accent-dim: rgba(0,214,143,0.12);
    --accent-glow: rgba(0,214,143,0.25);
    --warning: #ffaa00;
    --danger: #ff4757;
    --border: #1e2636;
    --border-accent: rgba(0,214,143,0.3);
    --radius: 10px;
    --font-ui: 'DM Sans', sans-serif;
    --font-mono: 'JetBrains Mono', monospace;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: var(--font-ui);
    background: var(--bg);
    color: var(--fg);
    min-height: 100vh;
    overflow-x: hidden;
  }

  body::before {
    content: '';
    position: fixed;
    top: -30%; left: -20%;
    width: 70vw; height: 70vw;
    background: radial-gradient(circle, rgba(0,214,143,0.06) 0%, transparent 70%);
    pointer-events: none;
    z-index: 0;
  }
  body::after {
    content: '';
    position: fixed;
    bottom: -40%; right: -15%;
    width: 60vw; height: 60vw;
    background: radial-gradient(circle, rgba(0,150,255,0.04) 0%, transparent 70%);
    pointer-events: none;
    z-index: 0;
  }

  .app { position: relative; z-index: 1; max-width: 1400px; margin: 0 auto; padding: 32px 24px; }

  /* Top Bar */
  .top-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 28px;
    flex-wrap: wrap;
    gap: 16px;
  }
  .top-bar h1 {
    font-size: 1.6rem;
    font-weight: 800;
    letter-spacing: -0.5px;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .top-bar h1 .icon-wrap {
    width: 38px; height: 38px;
    background: var(--accent-dim);
    border: 1px solid var(--border-accent);
    border-radius: 9px;
    display: flex; align-items: center; justify-content: center;
    color: var(--accent);
    font-size: 1rem;
  }
  .db-status {
    display: flex; align-items: center; gap: 8px;
    font-size: 0.82rem;
    color: var(--fg-muted);
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 6px 16px 6px 10px;
  }
  .db-status .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--fg-muted);
    transition: background 0.3s;
  }
  .db-status .dot.ok { background: var(--accent); box-shadow: 0 0 6px var(--accent-glow); }
  .db-status .dot.err { background: var(--danger); box-shadow: 0 0 6px rgba(255,71,87,0.3); }

  /* Table Selection Cards */
  .table-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .table-card {
    background: var(--bg-card);
    border: 1.5px solid var(--border);
    border-radius: var(--radius);
    padding: 22px 24px;
    cursor: pointer;
    transition: all 0.25s ease;
    position: relative;
    overflow: hidden;
  }
  .table-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: transparent;
    transition: background 0.25s;
  }
  .table-card:hover {
    border-color: var(--border-accent);
    background: var(--bg-card-hover);
    transform: translateY(-2px);
  }
  .table-card.active {
    border-color: var(--accent);
    box-shadow: 0 0 20px var(--accent-glow), inset 0 0 20px rgba(0,214,143,0.03);
  }
  .table-card.active::before { background: var(--accent); }
  .table-card .card-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 10px;
  }
  .table-card .card-header h2 {
    font-size: 1.05rem;
    font-weight: 600;
    font-family: var(--font-mono);
  }
  .table-card .card-header .badge {
    font-size: 0.72rem;
    padding: 3px 10px;
    border-radius: 12px;
    background: var(--accent-dim);
    color: var(--accent);
    font-weight: 600;
    font-family: var(--font-mono);
  }
  .table-card .card-desc {
    font-size: 0.82rem;
    color: var(--fg-muted);
    line-height: 1.5;
  }

  /* Toolbar */
  .toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }
  .search-box {
    flex: 1;
    min-width: 200px;
    position: relative;
  }
  .search-box i {
    position: absolute;
    left: 14px; top: 50%; transform: translateY(-50%);
    color: var(--fg-muted);
    font-size: 0.85rem;
  }
  .search-box input {
    width: 100%;
    padding: 10px 14px 10px 40px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--fg);
    font-family: var(--font-ui);
    font-size: 0.88rem;
    outline: none;
    transition: border-color 0.2s;
  }
  .search-box input:focus { border-color: var(--accent); }
  .search-box input::placeholder { color: var(--fg-muted); }

  .btn {
    padding: 10px 18px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--bg-card);
    color: var(--fg);
    font-family: var(--font-ui);
    font-size: 0.85rem;
    cursor: pointer;
    display: flex; align-items: center; gap: 7px;
    transition: all 0.2s;
    white-space: nowrap;
  }
  .btn:hover { border-color: var(--fg-muted); background: var(--bg-card-hover); }
  .btn-accent {
    background: var(--accent);
    color: #0c0f14;
    border-color: var(--accent);
    font-weight: 600;
  }
  .btn-accent:hover { background: #00c080; border-color: #00c080; }

  .info-chips {
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  }
  .chip {
    font-size: 0.78rem;
    padding: 5px 12px;
    border-radius: 6px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--fg-muted);
    font-family: var(--font-mono);
  }
  .chip b { color: var(--fg); font-weight: 600; }

  /* Data Table Container */
  .table-wrap {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    margin-bottom: 24px;
  }
  .table-scroll {
    overflow-x: auto;
    max-height: 60vh;
    overflow-y: auto;
  }
  .table-scroll::-webkit-scrollbar { width: 6px; height: 6px; }
  .table-scroll::-webkit-scrollbar-track { background: transparent; }
  .table-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  .table-scroll::-webkit-scrollbar-thumb:hover { background: var(--fg-muted); }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.84rem;
  }
  thead {
    position: sticky;
    top: 0;
    z-index: 2;
  }
  th {
    background: #181e2a;
    color: var(--fg-muted);
    font-weight: 600;
    text-align: left;
    padding: 12px 16px;
    border-bottom: 1.5px solid var(--border);
    white-space: nowrap;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-family: var(--font-mono);
    cursor: pointer;
    user-select: none;
    transition: color 0.2s;
  }
  th:hover { color: var(--accent); }
  th .sort-icon { margin-left: 4px; font-size: 0.65rem; opacity: 0.4; }
  th.sorted .sort-icon { opacity: 1; color: var(--accent); }

  td {
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    color: var(--fg);
    font-family: var(--font-mono);
    font-size: 0.82rem;
    white-space: nowrap;
    max-width: 300px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  tr { transition: background 0.15s; }
  tbody tr:hover { background: rgba(0,214,143,0.04); }
  td.null-val { color: var(--fg-muted); font-style: italic; }
  td.geo-val { color: var(--warning); }

  /* Pagination */
  .pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 20px;
    border-top: 1px solid var(--border);
    font-size: 0.82rem;
    color: var(--fg-muted);
    flex-wrap: wrap;
    gap: 10px;
  }
  .pagination .page-btns {
    display: flex; gap: 4px;
  }
  .page-btn {
    width: 34px; height: 34px;
    display: flex; align-items: center; justify-content: center;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--fg-muted);
    cursor: pointer;
    font-size: 0.82rem;
    transition: all 0.2s;
  }
  .page-btn:hover { border-color: var(--fg-muted); color: var(--fg); }
  .page-btn.active { background: var(--accent); color: #0c0f14; border-color: var(--accent); font-weight: 700; }
  .page-btn:disabled { opacity: 0.3; cursor: not-allowed; }

  /* Loading Overlay */
  .loading-overlay {
    display: none;
    position: absolute;
    inset: 0;
    background: rgba(12,15,20,0.85);
    z-index: 10;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 16px;
    border-radius: var(--radius);
  }
  .loading-overlay.show { display: flex; }
  .spinner {
    width: 36px; height: 36px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading-overlay span { font-size: 0.88rem; color: var(--fg-muted); }

  /* Empty & Error States */
  .empty-state, .error-state {
    text-align: center;
    padding: 60px 20px;
    color: var(--fg-muted);
  }
  .empty-state i, .error-state i { font-size: 2.5rem; margin-bottom: 16px; display: block; }
  .error-state i { color: var(--danger); }
  .error-state .error-msg {
    margin-top: 12px;
    padding: 14px 20px;
    background: rgba(255,71,87,0.08);
    border: 1px solid rgba(255,71,87,0.2);
    border-radius: 8px;
    color: var(--danger);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    text-align: left;
    max-width: 600px;
    margin-left: auto;
    margin-right: auto;
    word-break: break-all;
  }

  /* Toast Notifications */
  .toast-container {
    position: fixed;
    top: 20px; right: 20px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .toast {
    padding: 12px 20px;
    border-radius: 8px;
    font-size: 0.84rem;
    color: var(--fg);
    background: var(--bg-card);
    border: 1px solid var(--border);
    box-shadow: 0 8px 30px rgba(0,0,0,0.4);
    animation: slideIn 0.3s ease;
    display: flex; align-items: center; gap: 10px;
  }
  .toast.success { border-color: var(--accent); }
  .toast.success i { color: var(--accent); }
  .toast.error { border-color: var(--danger); }
  .toast.error i { color: var(--danger); }
  @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

  /* Initial Guide */
  .guide {
    text-align: center;
    padding: 80px 20px;
    color: var(--fg-muted);
  }
  .guide i { font-size: 3rem; margin-bottom: 20px; display: block; color: var(--border); }
  .guide p { font-size: 1rem; line-height: 1.7; }

  /* Responsive */
  @media (max-width: 640px) {
    .app { padding: 16px 12px; }
    .top-bar h1 { font-size: 1.2rem; }
    .table-cards { grid-template-columns: 1fr; }
    th, td { padding: 8px 10px; font-size: 0.76rem; }
  }
</style>
</head>
<body>

<div class="toast-container" id="toastContainer"></div>

<div class="app">
  <!-- Top Bar -->
  <div class="top-bar">
    <h1>
      <span class="icon-wrap"><i class="fas fa-database"></i></span>
      Database Viewer
    </h1>
    <div class="db-status">
      <span class="dot" id="statusDot"></span>
      <span id="statusText">Checking...</span>
    </div>
  </div>

  <!-- Table Selection Cards -->
  <div class="table-cards">
    <div class="table-card" id="cardRainfall" onclick="selectTable('rainfall_station')">
      <div class="card-header">
        <h2>rainfall_station</h2>
        <span class="badge" id="badgeRainfall">--</span>
      </div>
      <div class="card-desc">Rainfall station data with location coordinates and precipitation records</div>
    </div>
    <div class="table-card" id="cardSubbasin" onclick="selectTable('subbasin_gis')">
      <div class="card-header">
        <h2>subbasin_gis</h2>
        <span class="badge" id="badgeSubbasin">--</span>
      </div>
      <div class="card-desc">Subbasin GIS data containing watershed geometry and spatial attributes</div>
    </div>
  </div>

  <!-- Toolbar -->
  <div class="toolbar" id="toolbar" style="display:none;">
    <div class="search-box">
      <i class="fas fa-search"></i>
      <input type="text" id="searchInput" placeholder="Search all columns..." oninput="onSearch()">
    </div>
    <div class="info-chips" id="infoChips"></div>
    <button class="btn" onclick="exportCSV()"><i class="fas fa-download"></i> Export CSV</button>
    <button class="btn btn-accent" onclick="refreshData()"><i class="fas fa-sync-alt"></i> Refresh</button>
  </div>

  <!-- Data Area -->
  <div class="table-wrap" id="tableWrap" style="display:none; position:relative;">
    <div class="loading-overlay" id="loadingOverlay">
      <div class="spinner"></div>
      <span>Loading data...</span>
    </div>
    <div id="tableContent"></div>
  </div>

  <!-- Initial Guide -->
  <div class="guide" id="guideState">
    <i class="fas fa-hand-pointer"></i>
    <p>Click a card above to select a table to view</p>
  </div>
</div>

<script>
  /* ====== Global State ====== */
  let currentTable = null;
  let allData = [];
  let filteredData = [];
  let columns = [];
  let currentPage = 1;
  const pageSize = 50;
  let sortCol = null;
  let sortDir = 'asc';

  /* ====== Init ====== */
  document.addEventListener('DOMContentLoaded', () => {
    checkConnection();
  });

  /* ====== Check DB Connection ====== */
  async function checkConnection() {
    const dot = document.getElementById('statusDot');
    const text = document.getElementById('statusText');
    try {
      const res = await fetch('/api/ping');
      const data = await res.json();
      if (data.ok) {
        dot.className = 'dot ok';
        text.textContent = 'Connected \u00b7 localhost:5432';
        loadTableCounts();
      } else {
        throw new Error(data.error || 'Connection failed');
      }
    } catch (e) {
      dot.className = 'dot err';
      text.textContent = 'Connection failed';
      showToast('error', 'Cannot connect to database: ' + e.message);
    }
  }

  async function loadTableCounts() {
    try {
      const res = await fetch('/api/table_counts');
      const data = await res.json();
      if (data.rainfall_station !== undefined) {
        document.getElementById('badgeRainfall').textContent = data.rainfall_station + ' rows';
      }
      if (data.subbasin_gis !== undefined) {
        document.getElementById('badgeSubbasin').textContent = data.subbasin_gis + ' rows';
      }
    } catch(e) { /* silent fail */ }
  }

  /* ====== Select Table ====== */
  async function selectTable(tableName) {
    currentTable = tableName;
    currentPage = 1;
    sortCol = null;
    sortDir = 'asc';
    document.getElementById('searchInput').value = '';

    document.getElementById('cardRainfall').classList.toggle('active', tableName === 'rainfall_station');
    document.getElementById('cardSubbasin').classList.toggle('active', tableName === 'subbasin_gis');

    document.getElementById('toolbar').style.display = 'flex';
    document.getElementById('tableWrap').style.display = 'block';
    document.getElementById('guideState').style.display = 'none';

    await loadData();
  }

  /* ====== Load Data ====== */
  async function loadData() {
    showLoading(true);
    try {
      const res = await fetch('/api/table/' + encodeURIComponent(currentTable));
      const data = await res.json();
      if (data.error) throw new Error(data.error);

      columns = data.columns || [];
      allData = data.rows || [];
      filteredData = [...allData];
      showLoading(false);
      renderTable();
      showToast('success', 'Loaded ' + allData.length + ' records');
    } catch (e) {
      showLoading(false);
      renderError(e.message);
      showToast('error', 'Load failed: ' + e.message);
    }
  }

  async function refreshData() {
    if (!currentTable) return;
    await loadData();
  }

  /* ====== Search ====== */
  function onSearch() {
    const q = document.getElementById('searchInput').value.toLowerCase().trim();
    if (!q) {
      filteredData = [...allData];
    } else {
      filteredData = allData.filter(row =>
        columns.some(col => {
          const val = row[col];
          return val !== null && val !== undefined && String(val).toLowerCase().includes(q);
        })
      );
    }
    currentPage = 1;
    renderTable();
  }

  /* ====== Sort ====== */
  function toggleSort(colName) {
    if (sortCol === colName) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortCol = colName;
      sortDir = 'asc';
    }
    filteredData.sort((a, b) => {
      let va = a[colName], vb = b[colName];
      if (va === null || va === undefined) va = '';
      if (vb === null || vb === undefined) vb = '';
      if (typeof va === 'number' && typeof vb === 'number') {
        return sortDir === 'asc' ? va - vb : vb - va;
      }
      va = String(va).toLowerCase();
      vb = String(vb).toLowerCase();
      if (va < vb) return sortDir === 'asc' ? -1 : 1;
      if (va > vb) return sortDir === 'asc' ? 1 : -1;
      return 0;
    });
    renderTable();
  }

  /* ====== Render Table ====== */
  function renderTable() {
    const container = document.getElementById('tableContent');
    const chips = document.getElementById('infoChips');

    if (filteredData.length === 0 && allData.length > 0) {
      container.innerHTML = '<div class="empty-state"><i class="fas fa-search"></i><p>No matching results found</p></div>';
      chips.innerHTML = '';
      return;
    }
    if (filteredData.length === 0) {
      container.innerHTML = '<div class="empty-state"><i class="fas fa-inbox"></i><p>This table has no data</p></div>';
      chips.innerHTML = '';
      return;
    }

    const totalPages = Math.ceil(filteredData.length / pageSize);
    if (currentPage > totalPages) currentPage = totalPages;
    const start = (currentPage - 1) * pageSize;
    const pageData = filteredData.slice(start, start + pageSize);

    chips.innerHTML = '<span class="chip"><b>' + filteredData.length + '</b> rows</span>' +
                      '<span class="chip"><b>' + columns.length + '</b> columns</span>';

    // Detect geometry columns
    const geoKeywords = ['geom', 'geometry', 'wkb', 'wkt', 'shape', 'polygon', 'point', 'line', 'multipolygon'];
    const geoCols = columns.filter(c => geoKeywords.some(k => c.toLowerCase().includes(k)));

    // Build header
    let html = '<div class="table-scroll"><table><thead><tr>';
    html += '<th>#</th>';
    columns.forEach(col => {
      const isSorted = sortCol === col;
      const icon = isSorted ? (sortDir === 'asc' ? 'fa-sort-up' : 'fa-sort-down') : 'fa-sort';
      html += '<th class="' + (isSorted ? 'sorted' : '') + '" onclick="toggleSort(\'' + col.replace(/'/g, "\\'") + '\')">' +
              escapeHtml(col) + ' <i class="fas ' + icon + ' sort-icon"></i></th>';
    });
    html += '</tr></thead><tbody>';

    // Build rows
    pageData.forEach((row, i) => {
      html += '<tr>';
      html += '<td style="color:var(--fg-muted)">' + (start + i + 1) + '</td>';
      columns.forEach(col => {
        const val = row[col];
        if (val === null || val === undefined) {
          html += '<td class="null-val">NULL</td>';
        } else if (geoCols.includes(col)) {
          const strVal = String(val);
          const short = strVal.length > 60 ? strVal.substring(0, 60) + '...' : strVal;
          html += '<td class="geo-val" title="' + escapeHtml(strVal) + '">' + escapeHtml(short) + '</td>';
        } else {
          const display = String(val).length > 120 ? String(val).substring(0, 120) + '...' : String(val);
          html += '<td title="' + escapeHtml(String(val)) + '">' + escapeHtml(display) + '</td>';
        }
      });
      html += '</tr>';
    });

    html += '</tbody></table></div>';

    // Pagination
    html += '<div class="pagination">';
    html += '<span>Rows ' + (start + 1) + '-' + Math.min(start + pageSize, filteredData.length) + ' of ' + filteredData.length + '</span>';
    html += '<div class="page-btns">';
    html += '<button class="page-btn" onclick="goPage(' + (currentPage - 1) + ')"' + (currentPage <= 1 ? ' disabled' : '') + '><i class="fas fa-chevron-left"></i></button>';

    const pages = getPageNumbers(currentPage, totalPages);
    pages.forEach(p => {
      if (p === '...') {
        html += '<span class="page-btn" style="cursor:default;border:none;">...</span>';
      } else {
        html += '<button class="page-btn' + (p === currentPage ? ' active' : '') + '" onclick="goPage(' + p + ')">' + p + '</button>';
      }
    });

    html += '<button class="page-btn" onclick="goPage(' + (currentPage + 1) + ')"' + (currentPage >= totalPages ? ' disabled' : '') + '><i class="fas fa-chevron-right"></i></button>';
    html += '</div></div>';

    container.innerHTML = html;
  }

  function getPageNumbers(current, total) {
    if (total <= 7) return Array.from({length: total}, (_, i) => i + 1);
    const pages = [];
    if (current <= 4) {
      for (let i = 1; i <= 5; i++) pages.push(i);
      pages.push('...', total);
    } else if (current >= total - 3) {
      pages.push(1, '...');
      for (let i = total - 4; i <= total; i++) pages.push(i);
    } else {
      pages.push(1, '...', current - 1, current, current + 1, '...', total);
    }
    return pages;
  }

  function goPage(p) {
    const totalPages = Math.ceil(filteredData.length / pageSize);
    if (p < 1 || p > totalPages) return;
    currentPage = p;
    renderTable();
    document.querySelector('.table-scroll')?.scrollTo({top: 0, behavior: 'smooth'});
  }

  /* ====== Render Error ====== */
  function renderError(msg) {
    document.getElementById('tableContent').innerHTML =
      '<div class="error-state">' +
      '<i class="fas fa-exclamation-triangle"></i>' +
      '<p>Failed to load data</p>' +
      '<div class="error-msg">' + escapeHtml(msg) + '</div>' +
      '</div>';
  }

  /* ====== Export CSV ====== */
  function exportCSV() {
    if (!filteredData.length) { showToast('error', 'No data to export'); return; }
    const bom = '\uFEFF';
    let csv = bom + columns.map(c => '"' + c.replace(/"/g, '""') + '"').join(',') + '\n';
    filteredData.forEach(row => {
      csv += columns.map(c => {
        const v = row[c];
        if (v === null || v === undefined) return '';
        return '"' + String(v).replace(/"/g, '""') + '"';
      }).join(',') + '\n';
    });
    const blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = currentTable + '_export.csv';
    a.click();
    URL.revokeObjectURL(url);
    showToast('success', 'Exported ' + filteredData.length + ' records');
  }

  /* ====== Utilities ====== */
  function showLoading(show) {
    document.getElementById('loadingOverlay').classList.toggle('show', show);
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function showToast(type, msg) {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = 'toast ' + type;
    const icon = type === 'success' ? 'fa-check-circle' : 'fa-times-circle';
    toast.innerHTML = '<i class="fas ' + icon + '"></i><span>' + escapeHtml(msg) + '</span>';
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(100%)';
      toast.style.transition = 'all 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 3500);
  }
</script>
</body>
</html>"""


# ========== API Routes ==========

@app.route('/')
def index():
    """Serve the frontend page"""
    return Response(HTML_PAGE, mimetype='text/html; charset=utf-8')


@app.route('/api/ping')
def ping():
    """Test database connection"""
    try:
        conn = get_connection()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route('/api/table_counts')
def table_counts():
    """Get row counts for both tables"""
    result = {}
    tables = ['rainfall_station', 'subbasin_gis']
    try:
        conn = get_connection()
        cur = conn.cursor()
        for t in tables:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                result[t] = cur.fetchone()[0]
            except Exception:
                result[t] = None
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(result)


@app.route('/api/table/<table_name>')
def get_table(table_name):
    """Read all data from the specified table"""
    allowed = {'rainfall_station', 'subbasin_gis'}
    if table_name not in allowed:
        return jsonify({"error": f"Access denied for table '{table_name}'. Allowed: {', '.join(allowed)}"}), 403

    try:
        conn = get_connection()
        cur = conn.cursor()

        # Get column names first
        cur.execute(f'SELECT * FROM "{table_name}" LIMIT 0')
        col_names = [desc[0] for desc in cur.description]

        # Fetch all rows
        cur.execute(f'SELECT * FROM "{table_name}"')
        rows = [serialize_row(row, col_names) for row in cur.fetchall()]

        cur.close()
        conn.close()

        return jsonify({"columns": col_names, "rows": rows, "total": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ========== Start ==========
if __name__ == '__main__':
    print("\n  +----------------------------------------------+")
    print("  |   Database Viewer is running                |")
    print("  |   Open: http://localhost:5000               |")
    print("  |   Press Ctrl+C to stop                      |")
    print("  +----------------------------------------------+\n")
    app.run(host='0.0.0.0', port=5000, debug=False)