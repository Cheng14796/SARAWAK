/* Database Chat - front end.
 *
 * No inline handlers anywhere: every click goes through one delegated
 * listener, which is what lets the page ship a Content-Security-Policy that
 * forbids inline script instead of waving it through.
 */
(function () {
  'use strict';

  var ALLOW_RAW_SQL = document.body.dataset.rawSql === '1';
  var STORE_PREFIX = 'dbchat.turns.';
  var STORE_DB = 'dbchat.db';
  var STORE_THEME = 'dbchat.theme';
  var MAX_TURNS = 40;

  var curDb = null;
  var curTables = [];
  var busy = false;
  var turns = [];            // what gets replayed after a refresh
  var mapSeq = 0;
  var maps = {};             // mapId -> Leaflet instance, so a redraw resizes

  var inp = document.getElementById('inp');
  var msgBox = document.getElementById('msgBox');
  var dbSel = document.getElementById('dbSel');
  var expBtn = document.getElementById('expBtn');
  var sendBtn = document.getElementById('sendBtn');
  var connSt = document.getElementById('connSt');

  // ---------- small helpers ----------

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s === null || s === undefined ? '' : String(s);
    return d.innerHTML;
  }

  // Attribute values need more than element escaping: a quoted identifier in
  // the SQL would otherwise close the attribute early and lose the rest of it.
  function escAttr(s) {
    return esc(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function icon(name, cls) {
    return '<svg class="i ' + (cls || '') + '" aria-hidden="true"><use href="#ic-' + name + '"/></svg>';
  }

  function fmt(t) {
    return esc(t)
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^*\n]+)\*/g, '<em>$1</em>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/^\s*-\s+/gm, '&bull; ')
      .replace(/\n/g, '<br>');
  }

  function store(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* private mode, full disk */ }
  }

  function read(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  // ---------- theme ----------

  function applyTheme(mode) {
    document.documentElement.setAttribute('data-theme', mode || '');
    var dark = mode === 'dark' ||
      (!mode && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.getElementById('themeBtn').innerHTML = icon(dark ? 'sun' : 'moon');
  }

  applyTheme(read(STORE_THEME) || '');

  document.getElementById('themeBtn').addEventListener('click', function () {
    var now = document.documentElement.getAttribute('data-theme');
    var dark = now === 'dark' ||
      (!now && window.matchMedia('(prefers-color-scheme: dark)').matches);
    var next = dark ? 'light' : 'dark';
    store(STORE_THEME, next);
    applyTheme(next);
  });

  // ---------- transcript persistence ----------

  function saveTurns() {
    if (!curDb) return;
    store(STORE_PREFIX + curDb, JSON.stringify(turns.slice(-MAX_TURNS)));
  }

  function loadTurns(db) {
    var raw = read(STORE_PREFIX + db);
    if (!raw) return [];
    try {
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) { return []; }
  }

  function pushTurn(turn) {
    turns.push(turn);
    if (turns.length > MAX_TURNS) turns = turns.slice(-MAX_TURNS);
    saveTurns();
  }

  // ---------- message plumbing ----------

  function addMsg(role, html) {
    var div = document.createElement('div');
    div.className = 'mg ' + role;
    div.innerHTML = '<div class="av">' + icon(role === 'ur' ? 'user' : 'bot') +
      '</div><div class="bb">' + html + '</div>';
    msgBox.appendChild(div);
    msgBox.scrollTop = msgBox.scrollHeight;
    return div;
  }

  function errHtml(message) {
    return '<div class="er">' + icon('alert') + '<span>' + esc(message) + '</span></div>';
  }

  function showTyping() {
    var div = document.createElement('div');
    div.className = 'mg sy';
    div.id = 'typEl';
    div.innerHTML = '<div class="av">' + icon('bot') +
      '</div><div class="bb"><div class="tp-ind"><span></span><span></span><span></span></div></div>';
    msgBox.appendChild(div);
    msgBox.scrollTop = msgBox.scrollHeight;
  }

  function hideTyping() {
    var el = document.getElementById('typEl');
    if (el) el.remove();
  }

  function dropWelcome() {
    var w = document.getElementById('welcome');
    if (w) w.remove();
  }

  // ---------- rendering an answer ----------

  function buildTbl(data, hl) {
    var cols = data.columns || [];
    var rows = (data.rows || []).slice(0, 100);
    var hv = hl ? String(hl).toLowerCase() : null;
    var h = '<div class="tw"><table class="tb"><thead><tr>';
    cols.forEach(function (c) { h += '<th>' + esc(c) + '</th>'; });
    h += '</tr></thead><tbody>';
    rows.forEach(function (row) {
      h += '<tr>';
      cols.forEach(function (c) {
        var v = row[c];
        if (v === null || v === undefined) {
          h += '<td class="nl">NULL</td>';
        } else {
          var s = String(v);
          var isH = hv && s.toLowerCase().indexOf(hv) !== -1;
          h += '<td title="' + escAttr(s) + '"' + (isH ? ' class="hl"' : '') + '>' +
            esc(s.length > 120 ? s.substring(0, 120) + '...' : s) + '</td>';
        }
      });
      h += '</tr>';
    });
    h += '</tbody></table></div><div class="rc">Showing ' + rows.length + ' of ' +
      (data.rows || []).length + ' rows';
    if (hv) h += ' &middot; matched: <span class="muted">"' + esc(hl) + '"</span>';
    h += '</div>';
    return h;
  }

  function tableHasGeom(name) {
    var want = String(name).toLowerCase();
    for (var i = 0; i < curTables.length; i++) {
      if (curTables[i].table.toLowerCase() === want) return !!curTables[i].geometry;
    }
    return false;
  }

  function btn(iconName, label, attrs) {
    var a = '';
    Object.keys(attrs).forEach(function (k) {
      a += ' data-' + k + '="' + escAttr(attrs[k]) + '"';
    });
    return '<button class="xb"' + a + '>' + icon(iconName) + label + '</button>';
  }

  // Which coordinate system a shapefile is written in. Only shapefiles get the
  // choice: GeoJSON is specified as WGS84 lon/lat, so projected coordinates in
  // one would be malformed. The list comes from the server, which reads it out
  // of the bundled GIS skill's verified EPSG table.
  var crsOptions = [];

  function loadCrs() {
    fetch('/api/crs').then(function (r) { return r.json(); }).then(function (d) {
      crsOptions = d.crs || [];
    }).catch(function () { crsOptions = []; });
  }

  function crsPicker() {
    if (crsOptions.length < 2) return '';
    var h = '<label class="crs"><span class="crs-l">shapefile CRS</span>' +
      '<select class="crs-pick">';
    crsOptions.forEach(function (c) {
      h += '<option value="' + escAttr(c.epsg) + '">' + esc(c.label) +
        (c.note ? ' - ' + esc(c.note) : '') + '</option>';
    });
    return h + '</select></label>';
  }

  function answerTools(payload) {
    var sql = payload.sql;
    if (!sql) return '';
    var base = { sql: sql, sig: payload.sig || '' };
    function withFmt(f) { return { act: 'export', fmt: f, sql: base.sql, sig: base.sig }; }

    var h = '<div class="xp"><span class="xp-l">Download</span>' +
      btn('file', 'CSV', withFmt('csv')) +
      btn('code', 'JSON', withFmt('json'));
    if (payload.mappable) {
      h += btn('map', 'GeoJSON', withFmt('geojson')) +
        btn('layers', 'Shapefile', withFmt('shp')) + crsPicker();
    }
    h += btn('copy', 'Copy', { act: 'copy' });
    if (payload.mappable) {
      h += btn('map', 'Show on map', { act: 'map', sql: base.sql, sig: base.sig });
    }
    h += '</div>';
    if (payload.mappable) {
      h += '<div class="mapwrap"><div class="mapbox"></div><div class="mapnote"></div></div>';
    }
    return h;
  }

  function answerHtml(payload) {
    var h = '';
    if (payload.reply) h += fmt(payload.reply);
    if (payload.data && payload.data.rows && payload.data.rows.length) {
      h += buildTbl(payload.data, payload.highlight);
      h += answerTools(payload);
    }
    if (payload.sql) {
      h += '<div class="stg"><button data-act="sql-toggle">View SQL</button>' +
        '<div class="stg-hid">' + esc(payload.sql) + '</div></div>';
    }
    return h;
  }

  // ---------- schema card ----------

  function schemaHtml(tables) {
    if (!tables || !tables.length) {
      return '<p>Database <strong>' + esc(curDb) + '</strong> is empty - it has no tables yet, ' +
        'so there is nothing to ask about.</p><p class="muted">Import your data into it ' +
        '(QGIS <em>Export &rarr; PostGIS</em>, <code>shp2pgsql</code>, or <code>ogr2ogr</code>), ' +
        'then pick the database again to reload.</p>';
    }
    var h = '<p>Database <strong>' + esc(curDb) + '</strong> is ready. Ask in plain English - ' +
      'no SQL, and typos are fine.<br>You get a short answer; say <strong>"show them"</strong> ' +
      'when you want the full table, or <strong>"what about Miri"</strong> to carry on.</p>';
    h += '<div class="scb"><div class="scb-h">What is in here</div><div class="scb-b">';
    tables.forEach(function (t) {
      h += '<div class="scb-t"><span class="tn">' + esc(t.table) + '</span> <span class="rn">(' +
        esc(t.rows) + ' rows)</span>' + (t.geometry ? ' <span class="ct">mappable</span>' : '') + '<br>';
      t.columns.forEach(function (c) {
        h += '&nbsp;&nbsp;<span class="cn">' + esc(c.name) + '</span> <span class="ct">' +
          esc(c.type) + '</span><br>';
      });
      var f = t.filterable || {};
      Object.keys(f).forEach(function (k) {
        var v = f[k];
        h += '&nbsp;&nbsp;<span class="ct">you can ask by</span> <span class="cn">' + esc(k) +
          '</span><span class="ct">: ' + esc(v.slice(0, 6).join(', ')) +
          (v.length > 6 ? ', ...' : '') + '</span><br>';
      });
      h += '</div>';
    });
    h += '</div></div><p>Try asking:</p><div class="qa">';

    var qs = [];
    tables.forEach(function (t) {
      qs.push(['table', 'Show ' + t.table, 'show all ' + t.table]);
      var f = t.filterable || {};
      var keys = Object.keys(f);
      if (keys.length) {
        var k = keys[0], v = f[k][0];
        qs.push(['filter', t.table + ' in ' + v, t.table + ' in ' + v]);
        qs.push(['chart', 'Count per ' + k, 'how many ' + t.table + ' per ' + k]);
        qs.push(['award', 'Which ' + k + ' has most', 'which ' + k + ' has the most ' + t.table]);
        qs.push(['list', 'List all ' + k, 'list all ' + k]);
      } else {
        qs.push(['hash', 'Count ' + t.table, 'how many ' + t.table]);
      }
    });
    qs.slice(0, 12).forEach(function (x) {
      h += '<button class="qb" data-act="quick" data-q="' + escAttr(x[2]) + '">' +
        icon(x[0]) + ' ' + esc(x[1]) + '</button>';
    });
    return h + '</div>';
  }

  function exportPanelHtml() {
    var h = '<p>Download from <strong>' + esc(curDb) +
      '</strong> - the whole table, in the format you want.</p>';
    curTables.forEach(function (t) {
      h += '<div class="xrow"><span class="xn">' + esc(t.table) +
        ' <span class="muted">(' + esc(t.rows) + ' rows)</span></span>' +
        btn('file', 'CSV', { act: 'export', fmt: 'csv', table: t.table }) +
        btn('code', 'JSON', { act: 'export', fmt: 'json', table: t.table });
      if (t.geometry) {
        h += btn('map', 'GeoJSON', { act: 'export', fmt: 'geojson', table: t.table }) +
          btn('layers', 'Shapefile', { act: 'export', fmt: 'shp', table: t.table }) +
          crsPicker() +
          btn('map', 'Show on map', { act: 'map', table: t.table });
      }
      h += '</div>';
      if (t.geometry) {
        h += '<div class="mapwrap"><div class="mapbox"></div><div class="mapnote"></div></div>';
      }
    });
    h += '<p class="muted small">Shapefile arrives as a .zip - .shp, .shx, .dbf and .prj ' +
      'together, ready to unzip straight into QGIS.<br>Only want part of it? Ask a question ' +
      'first - every answer comes with its own download buttons.</p>';
    return h;
  }

  // ---------- replaying a saved conversation ----------

  function renderTurn(turn) {
    if (turn.t === 'q') return addMsg('ur', '<p>' + esc(turn.text).replace(/\n/g, '<br>') + '</p>');
    if (turn.t === 'a') return addMsg('sy', answerHtml(turn.payload));
    if (turn.t === 'err') return addMsg('sy', errHtml(turn.text));
    if (turn.t === 'schema') return addMsg('sy', schemaHtml(curTables));
    if (turn.t === 'export') return addMsg('sy', exportPanelHtml());
    return null;
  }

  function replay() {
    msgBox.innerHTML = '';
    turns.forEach(renderTurn);
    msgBox.scrollTop = msgBox.scrollHeight;
  }

  // ---------- databases and schema ----------

  function setConnected(db) {
    connSt.className = db ? 'pl' : 'pl off';
    connSt.innerHTML = '<span class="dt"></span>' + esc(db || 'No database');
  }

  function loadDbs() {
    fetch('/api/databases').then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) throw new Error(d.error);
      var dbs = d.databases || [];
      dbSel.innerHTML = '';
      var ph = document.createElement('option');
      ph.value = '';
      ph.textContent = dbs.length === 1 ? dbs[0] : '-- Select database --';
      dbSel.appendChild(ph);
      dbs.forEach(function (db) {
        var o = document.createElement('option');
        o.value = db;
        o.textContent = db;
        dbSel.appendChild(o);
      });
      // One database is not a choice - open it. Otherwise reopen the last one.
      var want = dbs.length === 1 ? dbs[0] : read(STORE_DB);
      if (want && dbs.indexOf(want) !== -1) {
        dbSel.value = want;
        selectDb(want, true);
      }
    }).catch(function () {
      dbSel.innerHTML = '<option value="">Database unavailable</option>';
      dropWelcome();
      addMsg('sy', errHtml('Cannot reach the database right now. Please try again in a minute.'));
    });
  }

  function selectDb(db, restoring) {
    curDb = db;
    curTables = [];
    maps = {};
    sendBtn.disabled = !db;
    expBtn.disabled = true;
    setConnected(db);

    if (!db) {
      inp.placeholder = 'Select a database first...';
      turns = [];
      msgBox.innerHTML = '';
      return;
    }
    store(STORE_DB, db);
    inp.placeholder = 'Ask anything about your data...';
    turns = restoring ? loadTurns(db) : [];
    msgBox.innerHTML = '';
    showTyping();

    fetch('/api/schema?db=' + encodeURIComponent(db))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.error) throw new Error(d.error);
        hideTyping();
        curTables = d.tables || [];
        expBtn.disabled = !curTables.length;
        if (turns.length) {
          replay();
        } else {
          pushTurn({ t: 'schema' });
          addMsg('sy', schemaHtml(curTables));
        }
      })
      .catch(function (e) {
        hideTyping();
        addMsg('sy', errHtml(e.message));
      });
  }

  // ---------- asking ----------

  function send(text) {
    var txt = (text === undefined ? inp.value : text).trim();
    if (!txt || busy || !curDb) return;
    dropWelcome();
    pushTurn({ t: 'q', text: txt });
    addMsg('ur', '<p>' + esc(txt).replace(/\n/g, '<br>') + '</p>');
    inp.value = '';
    inp.style.height = 'auto';
    busy = true;
    sendBtn.disabled = true;
    showTyping();

    fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ database: curDb, question: txt })
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, body: d }; });
    }).then(function (res) {
      hideTyping();
      var d = res.body;
      if (d.error) {
        pushTurn({ t: 'err', text: d.error });
        addMsg('sy', errHtml(d.error));
      } else {
        pushTurn({ t: 'a', payload: d });
        addMsg('sy', answerHtml(d));
      }
    }).catch(function () {
      hideTyping();
      var msg = 'The connection dropped before an answer came back. Please try again.';
      pushTurn({ t: 'err', text: msg });
      addMsg('sy', errHtml(msg));
    }).then(function () {
      busy = false;
      sendBtn.disabled = !curDb;
      inp.focus();
    });
  }

  // ---------- downloads ----------

  function saveBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function runExport(button) {
    var params = { db: curDb, fmt: button.dataset.fmt };
    if (button.dataset.table) params.table = button.dataset.table;
    if (button.dataset.sql) {
      params.sql = button.dataset.sql;
      params.sig = button.dataset.sig || '';
    }
    if (params.fmt === 'shp' && button.parentNode) {
      var pick = button.parentNode.querySelector('.crs-pick');
      if (pick && pick.value && pick.value !== '4326') params.crs = pick.value;
    }
    var qs = Object.keys(params).map(function (k) {
      return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
    }).join('&');

    var label = button.innerHTML;
    button.disabled = true;
    button.innerHTML = icon('spin', 'spin');

    fetch('/api/export?' + qs).then(function (r) {
      // The status is the only signal: a successful JSON export and a JSON
      // refusal have the same content type.
      if (!r.ok) {
        return r.text().then(function (t) {
          var msg = 'Export failed';
          try { msg = JSON.parse(t).error || msg; } catch (err) { /* not JSON */ }
          throw new Error(msg);
        });
      }
      var name = 'export';
      var m = /filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '');
      if (m) name = m[1];
      return r.blob().then(function (b) { saveBlob(b, name); });
    }).catch(function (e) {
      addMsg('sy', errHtml(e.message));
    }).then(function () {
      button.disabled = false;
      button.innerHTML = label;
    });
  }

  // ---------- copying ----------

  function tableText(wrapper) {
    var table = wrapper.querySelector('table.tb');
    if (!table) return '';
    return Array.prototype.map.call(table.querySelectorAll('tr'), function (tr) {
      return Array.prototype.map.call(tr.querySelectorAll('th,td'), function (cell) {
        // The cell is truncated for the eye; the title holds the whole value.
        return cell.getAttribute('title') || cell.textContent;
      }).join('\t');
    }).join('\n');
  }

  function copyTable(button) {
    var text = tableText(button.closest('.bb'));
    if (!text) return;
    var done = function () {
      var label = button.innerHTML;
      button.innerHTML = icon('check') + 'Copied';
      setTimeout(function () { button.innerHTML = label; }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () { fallbackCopy(text, done); });
    } else {
      fallbackCopy(text, done);
    }
  }

  function fallbackCopy(text, done) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', 'readonly');
    ta.className = 'skip';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { /* nothing else to try */ }
    ta.remove();
  }

  // ---------- the map ----------

  function drawMap(button) {
    var wrap = button.closest('.bb').querySelector('.mapwrap');
    if (!wrap) return;
    var open = wrap.classList.toggle('open');
    button.classList.toggle('on', open);
    if (!open) return;

    var box = wrap.querySelector('.mapbox');
    var note = wrap.querySelector('.mapnote');
    if (box.dataset.mapId) {
      maps[box.dataset.mapId].invalidateSize();
      return;
    }

    var params = { db: curDb };
    if (button.dataset.table) params.table = button.dataset.table;
    if (button.dataset.sql) {
      params.sql = button.dataset.sql;
      params.sig = button.dataset.sig || '';
    }
    var qs = Object.keys(params).map(function (k) {
      return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
    }).join('&');

    note.textContent = 'Loading the shapes...';
    fetch('/api/geojson?' + qs).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, body: d }; });
    }).then(function (res) {
      if (!res.ok || res.body.error) throw new Error(res.body.error || 'Map failed');
      var fc = res.body;
      if (!fc.features.length) {
        note.textContent = 'Nothing to draw - these rows have no geometry.';
        return;
      }

      var id = 'map' + (++mapSeq);
      box.id = id;
      box.dataset.mapId = id;
      var map = L.map(id, { scrollWheelZoom: false });
      maps[id] = map;

      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: 'Map data &copy; OpenStreetMap contributors'
      }).addTo(map);

      var accent = getComputedStyle(document.documentElement).getPropertyValue('--ac').trim() || '#00875a';
      var layer = L.geoJSON(fc, {
        style: { color: accent, weight: 2, fillOpacity: 0.15 },
        pointToLayer: function (feature, latlng) {
          return L.circleMarker(latlng, {
            radius: 5, color: accent, weight: 2, fillOpacity: 0.7
          });
        },
        onEachFeature: function (feature, lyr) {
          var props = feature.properties || {};
          var rows = Object.keys(props).slice(0, 12).map(function (k) {
            return '<b>' + esc(k) + '</b>: ' + esc(props[k]);
          }).join('<br>');
          if (rows) lyr.bindPopup(rows);
        }
      }).addTo(map);

      map.fitBounds(layer.getBounds(), { padding: [20, 20] });
      note.textContent = fc.features.length + ' feature(s) drawn' +
        (fc.truncated ? ', capped at ' + fc.limit + ' - narrow the question for the rest' : '') +
        '. Shapes are simplified for speed; downloads keep full detail.';
    }).catch(function (e) {
      note.textContent = e.message;
    });
  }

  // ---------- one listener for the whole page ----------

  document.addEventListener('click', function (ev) {
    var el = ev.target.closest('[data-act]');
    if (!el) return;
    var act = el.dataset.act;
    if (act === 'quick') {
      send(el.dataset.q);
    } else if (act === 'sql-toggle') {
      var panel = el.nextElementSibling;
      var open = panel.classList.toggle('open');
      el.textContent = open ? 'Hide SQL' : 'View SQL';
    } else if (act === 'export') {
      runExport(el);
    } else if (act === 'copy') {
      copyTable(el);
    } else if (act === 'map') {
      drawMap(el);
    }
  });

  dbSel.addEventListener('change', function () { selectDb(dbSel.value, false); });

  sendBtn.addEventListener('click', function () { send(); });

  inp.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });

  inp.addEventListener('input', function () {
    inp.style.height = 'auto';
    inp.style.height = Math.min(inp.scrollHeight, 120) + 'px';
  });

  expBtn.addEventListener('click', function () {
    if (!curDb || !curTables.length) return;
    dropWelcome();
    pushTurn({ t: 'export' });
    addMsg('sy', exportPanelHtml());
  });

  document.getElementById('clearBtn').addEventListener('click', function () {
    turns = [];
    saveTurns();
    maps = {};
    msgBox.innerHTML = '';
    if (curDb) {
      pushTurn({ t: 'schema' });
      addMsg('sy', schemaHtml(curTables));
    }
    inp.focus();
  });

  if (ALLOW_RAW_SQL) {
    document.querySelector('.ip-h').textContent =
      'Enter to send, Shift+Enter for a new line. Typed SELECT statements work too.';
  }

  loadDbs();
  loadCrs();
  inp.focus();
})();
