"""
PostgreSQL Database Reader - Auto-discovery version
Run: python db_reader.py
Open: http://localhost:5000
"""

from flask import Flask, jsonify, Response, request
import psycopg2
import json
import os

# Reads the same ignored .env as chatbox.py, so no password sits in the source.
from chatbox import _load_dotenv

_load_dotenv()

app = Flask(__name__)

DB_CONFIG = {
    "host": os.environ.get("PGHOST", "localhost"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "database": os.environ.get("PGDATABASE", "postgres"),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", "")
}


def get_connection(dbname=None):
    cfg = dict(DB_CONFIG)
    if dbname:
        cfg["database"] = dbname
    return psycopg2.connect(**cfg)


def serialize_row(row, columns):
    result = {}
    for col, val in zip(columns, row):
        try:
            json.dumps(val)
            result[col] = val
        except (TypeError, ValueError):
            result[col] = str(val)
    return result


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Database Viewer</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:wght@300;400;600;800&display=swap" rel="stylesheet">
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css" rel="stylesheet">
<style>
  :root {
    --bg:#0c0f14;--bg-card:#141820;--bg-card-hover:#1a2030;
    --fg:#e8ecf1;--fg-muted:#6b7a8d;--accent:#00d68f;
    --accent-dim:rgba(0,214,143,0.12);--accent-glow:rgba(0,214,143,0.25);
    --warning:#ffaa00;--danger:#ff4757;--border:#1e2636;
    --border-accent:rgba(0,214,143,0.3);--radius:10px;
    --font-ui:'DM Sans',sans-serif;--font-mono:'JetBrains Mono',monospace;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:var(--font-ui);background:var(--bg);color:var(--fg);min-height:100vh;overflow-x:hidden}
  body::before{content:'';position:fixed;top:-30%;left:-20%;width:70vw;height:70vw;background:radial-gradient(circle,rgba(0,214,143,0.06) 0%,transparent 70%);pointer-events:none;z-index:0}
  body::after{content:'';position:fixed;bottom:-40%;right:-15%;width:60vw;height:60vw;background:radial-gradient(circle,rgba(0,150,255,0.04) 0%,transparent 70%);pointer-events:none;z-index:0}
  .app{position:relative;z-index:1;max-width:1440px;margin:0 auto;padding:32px 24px}

  .top-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:16px}
  .top-bar h1{font-size:1.6rem;font-weight:800;letter-spacing:-0.5px;display:flex;align-items:center;gap:12px}
  .top-bar h1 .iw{width:38px;height:38px;background:var(--accent-dim);border:1px solid var(--border-accent);border-radius:9px;display:flex;align-items:center;justify-content:center;color:var(--accent);font-size:1rem}
  .db-status{display:flex;align-items:center;gap:8px;font-size:0.82rem;color:var(--fg-muted);background:var(--bg-card);border:1px solid var(--border);border-radius:20px;padding:6px 16px 6px 10px}
  .db-status .dot{width:8px;height:8px;border-radius:50%;background:var(--fg-muted);transition:background 0.3s}
  .db-status .dot.ok{background:var(--accent);box-shadow:0 0 6px var(--accent-glow)}
  .db-status .dot.err{background:var(--danger);box-shadow:0 0 6px rgba(255,71,87,0.3)}

  .selector-row{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;align-items:stretch}
  .selector-box{flex:1;min-width:240px;background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:14px 18px;display:flex;flex-direction:column;gap:8px}
  .selector-box label{font-size:0.78rem;font-weight:600;color:var(--fg-muted);text-transform:uppercase;letter-spacing:0.5px;display:flex;align-items:center;gap:6px}
  .selector-box label i{color:var(--accent);font-size:0.75rem}
  .selector-box select{padding:10px 36px 10px 14px;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--fg);font-family:var(--font-mono);font-size:0.86rem;outline:none;cursor:pointer;transition:border-color 0.2s;appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%236b7a8d' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center}
  .selector-box select:focus{border-color:var(--accent)}
  .selector-box select option{background:var(--bg-card);color:var(--fg)}
  .hint-text{font-size:0.76rem;color:var(--warning);font-style:italic}
  .hint-text.ok{color:var(--accent);font-style:normal;font-weight:600}

  /* Table list */
  .table-list{display:flex;flex-direction:column;gap:6px;margin-bottom:20px}
  .tl-item{display:flex;align-items:center;gap:14px;padding:14px 20px;background:var(--bg-card);border:1.5px solid var(--border);border-radius:var(--radius);cursor:pointer;transition:all 0.2s}
  .tl-item:hover{border-color:var(--border-accent);background:var(--bg-card-hover);transform:translateX(4px)}
  .tl-item.active{border-color:var(--accent);box-shadow:0 0 16px var(--accent-glow)}
  .tl-item .tl-schema{font-size:0.72rem;padding:3px 8px;border-radius:4px;background:rgba(255,170,0,0.1);color:var(--warning);font-family:var(--font-mono);font-weight:600}
  .tl-item .tl-name{font-family:var(--font-mono);font-size:0.95rem;font-weight:600;flex:1}
  .tl-item .tl-count{font-size:0.78rem;color:var(--fg-muted);font-family:var(--font-mono)}
  .tl-item .tl-count b{color:var(--fg)}
  .tl-item .tl-arrow{color:var(--fg-muted);font-size:0.8rem;transition:color 0.2s}
  .tl-item:hover .tl-arrow{color:var(--accent)}

  .toolbar{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
  .search-box{flex:1;min-width:200px;position:relative}
  .search-box i{position:absolute;left:14px;top:50%;transform:translateY(-50%);color:var(--fg-muted);font-size:0.85rem}
  .search-box input{width:100%;padding:10px 14px 10px 40px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;color:var(--fg);font-family:var(--font-ui);font-size:0.88rem;outline:none;transition:border-color 0.2s}
  .search-box input:focus{border-color:var(--accent)}
  .search-box input::placeholder{color:var(--fg-muted)}
  .btn{padding:10px 18px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);color:var(--fg);font-family:var(--font-ui);font-size:0.85rem;cursor:pointer;display:flex;align-items:center;gap:7px;transition:all 0.2s;white-space:nowrap}
  .btn:hover{border-color:var(--fg-muted);background:var(--bg-card-hover)}
  .btn-accent{background:var(--accent);color:#0c0f14;border-color:var(--accent);font-weight:600}
  .btn-accent:hover{background:#00c080;border-color:#00c080}
  .info-chips{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .chip{font-size:0.78rem;padding:5px 12px;border-radius:6px;background:var(--bg-card);border:1px solid var(--border);color:var(--fg-muted);font-family:var(--font-mono)}
  .chip b{color:var(--fg);font-weight:600}

  .table-wrap{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;margin-bottom:24px}
  .table-scroll{overflow-x:auto;max-height:60vh;overflow-y:auto}
  .table-scroll::-webkit-scrollbar{width:6px;height:6px}
  .table-scroll::-webkit-scrollbar-track{background:transparent}
  .table-scroll::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
  table{width:100%;border-collapse:collapse;font-size:0.84rem}
  thead{position:sticky;top:0;z-index:2}
  th{background:#181e2a;color:var(--fg-muted);font-weight:600;text-align:left;padding:12px 16px;border-bottom:1.5px solid var(--border);white-space:nowrap;font-size:0.78rem;text-transform:uppercase;letter-spacing:0.5px;font-family:var(--font-mono);cursor:pointer;user-select:none;transition:color 0.2s}
  th:hover{color:var(--accent)}
  th .sort-icon{margin-left:4px;font-size:0.65rem;opacity:0.4}
  th.sorted .sort-icon{opacity:1;color:var(--accent)}
  td{padding:10px 16px;border-bottom:1px solid var(--border);color:var(--fg);font-family:var(--font-mono);font-size:0.82rem;white-space:nowrap;max-width:300px;overflow:hidden;text-overflow:ellipsis}
  tr{transition:background 0.15s}
  tbody tr:hover{background:rgba(0,214,143,0.04)}
  td.null-val{color:var(--fg-muted);font-style:italic}
  td.geo-val{color:var(--warning)}
  .pagination{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-top:1px solid var(--border);font-size:0.82rem;color:var(--fg-muted);flex-wrap:wrap;gap:10px}
  .pagination .page-btns{display:flex;gap:4px}
  .page-btn{width:34px;height:34px;display:flex;align-items:center;justify-content:center;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--fg-muted);cursor:pointer;font-size:0.82rem;transition:all 0.2s}
  .page-btn:hover{border-color:var(--fg-muted);color:var(--fg)}
  .page-btn.active{background:var(--accent);color:#0c0f14;border-color:var(--accent);font-weight:700}
  .page-btn:disabled{opacity:0.3;cursor:not-allowed}

  .loading-overlay{display:none;position:absolute;inset:0;background:rgba(12,15,20,0.85);z-index:10;align-items:center;justify-content:center;flex-direction:column;gap:16px;border-radius:var(--radius)}
  .loading-overlay.show{display:flex}
  .spinner{width:36px;height:36px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin 0.8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .loading-overlay span{font-size:0.88rem;color:var(--fg-muted)}

  .empty-state,.error-state{text-align:center;padding:60px 20px;color:var(--fg-muted)}
  .empty-state i,.error-state i{font-size:2.5rem;margin-bottom:16px;display:block}
  .error-state i{color:var(--danger)}
  .error-state .error-msg{margin-top:12px;padding:14px 20px;background:rgba(255,71,87,0.08);border:1px solid rgba(255,71,87,0.2);border-radius:8px;color:var(--danger);font-family:var(--font-mono);font-size:0.8rem;text-align:left;max-width:600px;margin-left:auto;margin-right:auto;word-break:break-all}

  .toast-container{position:fixed;top:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px}
  .toast{padding:12px 20px;border-radius:8px;font-size:0.84rem;color:var(--fg);background:var(--bg-card);border:1px solid var(--border);box-shadow:0 8px 30px rgba(0,0,0,0.4);animation:slideIn 0.3s ease;display:flex;align-items:center;gap:10px}
  .toast.success{border-color:var(--accent)}.toast.success i{color:var(--accent)}
  .toast.error{border-color:var(--danger)}.toast.error i{color:var(--danger)}
  .toast.info{border-color:var(--warning)}.toast.info i{color:var(--warning)}
  @keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}

  .guide{text-align:center;padding:60px 20px;color:var(--fg-muted)}
  .guide i{font-size:3rem;margin-bottom:20px;display:block;color:var(--border)}
  .guide p{font-size:1rem;line-height:1.7}

  .no-tables{text-align:center;padding:40px 20px;color:var(--fg-muted);font-size:0.9rem}

  @media(max-width:640px){.app{padding:16px 12px}.top-bar h1{font-size:1.2rem}.selector-row{flex-direction:column}th,td{padding:8px 10px;font-size:0.76rem}}
</style>
</head>
<body>
<div class="toast-container" id="toastContainer"></div>
<div class="app">
  <div class="top-bar">
    <h1><span class="iw"><i class="fas fa-database"></i></span> Database Viewer</h1>
    <div class="db-status"><span class="dot" id="statusDot"></span><span id="statusText">Checking...</span></div>
  </div>

  <div class="selector-row">
    <div class="selector-box">
      <label><i class="fas fa-server"></i> Database</label>
      <select id="dbSelect" onchange="onDbChange()"><option value="">Loading...</option></select>
    </div>
    <div class="selector-box" style="flex:2">
      <label><i class="fas fa-table"></i> Tables in this database</label>
      <span class="hint-text" id="tableHint">Select a database first</span>
    </div>
  </div>

  <div id="tableListArea"></div>

  <div class="toolbar" id="toolbar" style="display:none">
    <div class="search-box"><i class="fas fa-search"></i><input type="text" id="searchInput" placeholder="Search all columns..." oninput="onSearch()"></div>
    <div class="info-chips" id="infoChips"></div>
    <button class="btn" onclick="exportCSV()"><i class="fas fa-download"></i> Export CSV</button>
    <button class="btn btn-accent" onclick="refreshData()"><i class="fas fa-sync-alt"></i> Refresh</button>
  </div>

  <div class="table-wrap" id="tableWrap" style="display:none;position:relative">
    <div class="loading-overlay" id="loadingOverlay"><div class="spinner"></div><span>Loading data...</span></div>
    <div id="tableContent"></div>
  </div>

  <div class="guide" id="guideState">
    <i class="fas fa-layer-group"></i>
    <p>Select a database from the dropdown above<br>then click a table to view its data</p>
  </div>
</div>

<script>
let currentTable=null,currentSchema='public',currentDb=null;
let allData=[],filteredData=[],columns=[];
let currentPage=1;const pageSize=50;let sortCol=null,sortDir='asc';

document.addEventListener('DOMContentLoaded',()=>loadDatabases());

async function loadDatabases(){
  const dot=document.getElementById('statusDot'),text=document.getElementById('statusText'),sel=document.getElementById('dbSelect');
  try{
    const r=await fetch('/api/databases'),d=await r.json();
    if(d.error)throw new Error(d.error);
    dot.className='dot ok';text.textContent='Connected \u00b7 localhost:5432';
    const dbs=d.databases||[];sel.innerHTML='';
    if(!dbs.length){sel.innerHTML='<option value="">No databases found</option>';return;}
    const ph=document.createElement('option');ph.value='';ph.textContent='-- Select a database ('+dbs.length+' found) --';sel.appendChild(ph);
    dbs.forEach(db=>{const o=document.createElement('option');o.value=db;o.textContent=db;sel.appendChild(o);});
    // Auto-scan for our target tables
    try{
      const sr=await fetch('/api/scan'),sd=await sr.json();
      if(sd.found_in){sel.value=sd.found_in;onDbChange();showToast('info','Auto-detected tables in: '+sd.found_in);}
    }catch(e){}
  }catch(e){
    dot.className='dot err';text.textContent='Connection failed';
    sel.innerHTML='<option value="">Failed to connect</option>';
    showToast('error','Cannot connect: '+e.message);
  }
}

async function onDbChange(){
  const db=document.getElementById('dbSelect').value;
  currentDb=db;currentTable=null;
  document.getElementById('toolbar').style.display='none';
  document.getElementById('tableWrap').style.display='none';
  document.getElementById('guideState').style.display='block';
  document.getElementById('tableListArea').innerHTML='';
  document.getElementById('tableHint').textContent='Loading tables...';
  document.getElementById('tableHint').className='hint-text';
  if(!db){document.getElementById('tableHint').textContent='Select a database first';return;}
  try{
    const r=await fetch('/api/tables?db='+encodeURIComponent(db)),d=await r.json();
    if(d.error)throw new Error(d.error);
    const tables=d.tables||[];
    const hint=document.getElementById('tableHint');
    if(!tables.length){hint.textContent='No user tables found in this database';hint.className='hint-text';return;}
    hint.textContent=tables.length+' table(s) found';hint.className='hint-text ok';
    // Check if our targets are here
    const names=tables.map(t=>t.name);
    const targets=['rainfall_station','subbasin_gis'].filter(t=>names.includes(t));
    if(targets.length)hint.textContent+=' \u2014 includes: '+targets.join(', ');

    let html='<div class="table-list">';
    tables.forEach(t=>{
      const isTarget=targets.includes(t.name);
      html+='<div class="tl-item" onclick="selectTable(\''+esc(t.schema)+'\',\''+esc(t.name)+'\')">';
      html+='<span class="tl-schema">'+esc(t.schema)+'</span>';
      html+='<span class="tl-name">'+(isTarget?'<i class="fas fa-star" style="color:var(--accent);font-size:0.7rem;margin-right:6px"></i>':'')+esc(t.name)+'</span>';
      html+='<span class="tl-count"><b>'+t.rows+'</b> rows</span>';
      html+='<span class="tl-arrow"><i class="fas fa-chevron-right"></i></span>';
      html+='</div>';
    });
    html+='</div>';
    document.getElementById('tableListArea').innerHTML=html;
  }catch(e){
    document.getElementById('tableHint').textContent='Error: '+e.message;
    document.getElementById('tableHint').className='hint-text';
    showToast('error','Failed to list tables: '+e.message);
  }
}

async function selectTable(schema,name){
  currentSchema=schema;currentTable=name;
  currentPage=1;sortCol=null;sortDir='asc';
  document.getElementById('searchInput').value='';
  document.querySelectorAll('.tl-item').forEach(el=>{
    el.classList.toggle('active',el.querySelector('.tl-name').textContent.trim()===name);
  });
  document.getElementById('toolbar').style.display='flex';
  document.getElementById('tableWrap').style.display='block';
  document.getElementById('guideState').style.display='none';
  await loadData();
}

async function loadData(){
  showLoading(true);
  try{
    const r=await fetch('/api/data?db='+encodeURIComponent(currentDb)+'&schema='+encodeURIComponent(currentSchema)+'&table='+encodeURIComponent(currentTable));
    const d=await r.json();if(d.error)throw new Error(d.error);
    columns=d.columns||[];allData=d.rows||[];filteredData=[...allData];
    showLoading(false);renderTable();
    showToast('success','Loaded '+allData.length+' records from '+currentSchema+'.'+currentTable);
  }catch(e){showLoading(false);renderError(e.message);showToast('error','Load failed: '+e.message);}
}

async function refreshData(){if(!currentTable)return;await loadData();}

function onSearch(){
  const q=document.getElementById('searchInput').value.toLowerCase().trim();
  filteredData=q?allData.filter(r=>columns.some(c=>{const v=r[c];return v!==null&&v!==undefined&&String(v).toLowerCase().includes(q);})):[...allData];
  currentPage=1;renderTable();
}

function toggleSort(col){
  if(sortCol===col)sortDir=sortDir==='asc'?'desc':'asc';else{sortCol=col;sortDir='asc';}
  filteredData.sort((a,b)=>{
    let va=a[col],vb=b[col];if(va==null)va='';if(vb==null)vb='';
    if(typeof va==='number'&&typeof vb==='number')return sortDir==='asc'?va-vb:vb-va;
    va=String(va).toLowerCase();vb=String(vb).toLowerCase();
    if(va<vb)return sortDir==='asc'?-1:1;if(va>vb)return sortDir==='asc'?1:-1;return 0;
  });renderTable();
}

function renderTable(){
  const ct=document.getElementById('tableContent'),ch=document.getElementById('infoChips');
  if(!filteredData.length&&allData.length){ct.innerHTML='<div class="empty-state"><i class="fas fa-search"></i><p>No matching results</p></div>';ch.innerHTML='';return;}
  if(!filteredData.length){ct.innerHTML='<div class="empty-state"><i class="fas fa-inbox"></i><p>No data in this table</p></div>';ch.innerHTML='';return;}
  const tp=Math.ceil(filteredData.length/pageSize);if(currentPage>tp)currentPage=tp;
  const s=(currentPage-1)*pageSize,pd=filteredData.slice(s,s+pageSize);
  ch.innerHTML='<span class="chip">DB: <b>'+esc(currentDb)+'</b></span><span class="chip"><b>'+filteredData.length+'</b> rows</span><span class="chip"><b>'+columns.length+'</b> cols</span>';
  const gk=['geom','geometry','wkb','wkt','shape','polygon','point','line','multipolygon'];
  const gc=columns.filter(c=>gk.some(k=>c.toLowerCase().includes(k)));
  let h='<div class="table-scroll"><table><thead><tr><th>#</th>';
  columns.forEach(c=>{const is=sortCol===c,ic=is?(sortDir==='asc'?'fa-sort-up':'fa-sort-down'):'fa-sort';
    h+='<th class="'+(is?'sorted':'')+'" onclick="toggleSort(\''+c.replace(/'/g,"\\'")+'\')">'+esc(c)+' <i class="fas '+ic+' sort-icon"></i></th>';});
  h+='</tr></thead><tbody>';
  pd.forEach((r,i)=>{h+='<tr><td style="color:var(--fg-muted)">'+(s+i+1)+'</td>';
    columns.forEach(c=>{const v=r[c];
      if(v==null)h+='<td class="null-val">NULL</td>';
      else if(gc.includes(c)){const sv=String(v),sh=sv.length>60?sv.substring(0,60)+'...':sv;h+='<td class="geo-val" title="'+esc(sv)+'">'+esc(sh)+'</td>';}
      else{const d=String(v).length>120?String(v).substring(0,120)+'...':String(v);h+='<td title="'+esc(String(v))+'">'+esc(d)+'</td>';}
    });h+='</tr>';});
  h+='</tbody></table></div>';
  h+='<div class="pagination"><span>Rows '+(s+1)+'-'+Math.min(s+pageSize,filteredData.length)+' of '+filteredData.length+'</span><div class="page-btns">';
  h+='<button class="page-btn" onclick="goPage('+(currentPage-1)+')"'+(currentPage<=1?' disabled':'')+'><i class="fas fa-chevron-left"></i></button>';
  getPg(currentPage,tp).forEach(p=>{if(p==='...')h+='<span class="page-btn" style="cursor:default;border:none">...</span>';
    else h+='<button class="page-btn'+(p===currentPage?' active':'')+'" onclick="goPage('+p+')">'+p+'</button>';});
  h+='<button class="page-btn" onclick="goPage('+(currentPage+1)+')"'+(currentPage>=tp?' disabled':'')+'><i class="fas fa-chevron-right"></i></button></div></div>';
  ct.innerHTML=h;
}

function getPg(c,t){if(t<=7)return Array.from({length:t},(_,i)=>i+1);const p=[];
  if(c<=4){for(let i=1;i<=5;i++)p.push(i);p.push('...',t);}
  else if(c>=t-3){p.push(1,'...');for(let i=t-4;i<=t;i++)p.push(i);}
  else p.push(1,'...',c-1,c,c+1,'...',t);return p;}

function goPage(p){const t=Math.ceil(filteredData.length/pageSize);if(p<1||p>t)return;currentPage=p;renderTable();document.querySelector('.table-scroll')?.scrollTo({top:0,behavior:'smooth'});}

function renderError(m){document.getElementById('tableContent').innerHTML='<div class="error-state"><i class="fas fa-exclamation-triangle"></i><p>Failed to load data</p><div class="error-msg">'+esc(m)+'</div></div>';}

function exportCSV(){
  if(!filteredData.length){showToast('error','No data to export');return;}
  const bom='\uFEFF';let csv=bom+columns.map(c=>'"'+c.replace(/"/g,'""')+'"').join(',')+'\n';
  filteredData.forEach(r=>{csv+=columns.map(c=>{const v=r[c];return v==null?'':'"'+String(v).replace(/"/g,'""')+'"';}).join(',')+'\n';});
  const b=new Blob([csv],{type:'text/csv;charset=utf-8;'}),u=URL.createObjectURL(b),a=document.createElement('a');
  a.href=u;a.download=currentTable+'_export.csv';a.click();URL.revokeObjectURL(u);
  showToast('success','Exported '+filteredData.length+' records');
}

function showLoading(s){document.getElementById('loadingOverlay').classList.toggle('show',s);}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function showToast(t,m){
  const c=document.getElementById('toastContainer'),e=document.createElement('div');e.className='toast '+t;
  const ic=t==='success'?'fa-check-circle':t==='info'?'fa-info-circle':'fa-times-circle';
  e.innerHTML='<i class="fas '+ic+'"></i><span>'+esc(m)+'</span>';c.appendChild(e);
  setTimeout(()=>{e.style.opacity='0';e.style.transform='translateX(100%)';e.style.transition='all 0.3s';setTimeout(()=>e.remove(),300);},4000);
}
</script>
</body>
</html>"""


@app.route('/')
def index():
    return Response(HTML_PAGE, mimetype='text/html; charset=utf-8')


@app.route('/api/databases')
def list_databases():
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT datname FROM pg_database
            WHERE datistemplate = false
            ORDER BY datname
        """)
        dbs = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        return jsonify({"databases": dbs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/scan')
def scan_tables():
    """Scan ALL databases to find which one has our target tables"""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
        all_dbs = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()

        targets = ['rainfall_station', 'subbasin_gis']

        for db in all_dbs:
            try:
                tc = get_connection(db)
                tcur = tc.cursor()
                tcur.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_name IN %s
                """, (tuple(targets),))
                found = [row[0] for row in tcur.fetchall()]
                tcur.close()
                tc.close()
                if found:
                    return jsonify({"found_in": db, "tables": found})
            except Exception:
                continue

        return jsonify({"found_in": None})
    except Exception as e:
        return jsonify({"found_in": None, "error": str(e)})


@app.route('/api/tables')
def list_tables():
    """List ALL user tables in the selected database"""
    db = request.args.get('db', 'postgres')
    try:
        conn = get_connection(db)
        cur = conn.cursor()
        cur.execute("""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
              AND table_type = 'BASE TABLE'
            ORDER BY table_schema, table_name
        """)
        raw = cur.fetchall()
        tables = []
        for schema, name in raw:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{name}"')
                count = cur.fetchone()[0]
            except Exception:
                count = -1
            tables.append({"schema": schema, "name": name, "rows": count})
        cur.close()
        conn.close()
        return jsonify({"tables": tables})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/data')
def get_data():
    """Read all data from any table in any schema"""
    db = request.args.get('db', 'postgres')
    schema = request.args.get('schema', 'public')
    table = request.args.get('table', '')

    if not table:
        return jsonify({"error": "No table specified"}), 400

    # Basic name safety check
    for name in [schema, table]:
        if not name.replace('_', '').replace(' ', '').isalnum():
            return jsonify({"error": f"Invalid name: {name}"}), 400

    try:
        conn = get_connection(db)
        cur = conn.cursor()

        full_table = f'"{schema}"."{table}"'
        cur.execute(f'SELECT * FROM {full_table} LIMIT 0')
        col_names = [desc[0] for desc in cur.description]

        cur.execute(f'SELECT * FROM {full_table}')
        rows = [serialize_row(row, col_names) for row in cur.fetchall()]

        cur.close()
        conn.close()
        return jsonify({"columns": col_names, "rows": rows, "total": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    print("\n  +----------------------------------------------+")
    print("  |   Database Viewer is running                |")
    print("  |   Open: http://localhost:5000               |")
    print("  |   Press Ctrl+C to stop                      |")
    print("  +----------------------------------------------+\n")
    app.run(host='0.0.0.0', port=5000, debug=False)