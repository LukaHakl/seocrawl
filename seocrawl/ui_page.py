"""The single-page app served by :mod:`seocrawl.ui`.

Kept as one self-contained string: no build step, no asset pipeline, no CDN.
The page is served from localhost and talks to the small JSON API in ``ui.py``.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>seocrawl</title>
<style>
:root{color-scheme:light dark;
 --bg:#fbfbfc; --panel:#fff; --fg:#16181d; --muted:#666e7a; --line:#e4e7ec;
 --accent:#1f3864; --accent-fg:#fff; --focus:#3b6bb5;
 --high:#c0392b; --high-bg:#fdecea; --med:#a86b12; --med-bg:#fdf4e3;
 --low:#2f6f4f; --low-bg:#eaf5ef; --ok:#2f6f4f;}
@media (prefers-color-scheme:dark){:root{
 --bg:#101317; --panel:#171b21; --fg:#e7e9ee; --muted:#98a1ae; --line:#272c34;
 --accent:#8ab4f8; --accent-fg:#0d1117; --focus:#8ab4f8;
 --high:#ff8f84; --high-bg:#3a1f1d; --med:#ffc86b; --med-bg:#3a2f18;
 --low:#8fd3ad; --low-bg:#1b2f26; --ok:#8fd3ad;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 80px}
header{display:flex;align-items:baseline;gap:12px;margin-bottom:4px}
h1{font-size:20px;margin:0;letter-spacing:-.01em}
.tag{font-size:12px;color:var(--muted);border:1px solid var(--line);
 border-radius:999px;padding:2px 9px}
.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:20px}
.tab{background:none;border:0;border-bottom:2px solid transparent;color:var(--muted);
 font:inherit;font-weight:600;padding:9px 14px;cursor:pointer}
.tab[aria-selected=true]{color:var(--fg);border-bottom-color:var(--accent)}
.tab:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}
label{display:block;font-size:12px;font-weight:600;color:var(--muted);
 text-transform:uppercase;letter-spacing:.04em;margin-bottom:5px}
input[type=text],input[type=number],input[type=file],select{
 width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:7px;
 background:var(--bg);color:var(--fg);font:inherit}
input:focus,select:focus{outline:2px solid var(--focus);outline-offset:-1px;border-color:transparent}
.hint{font-size:12px;color:var(--muted);margin-top:5px;font-weight:400;text-transform:none;letter-spacing:0}
.checks{display:flex;flex-wrap:wrap;gap:16px;margin-top:16px}
.checks label{display:flex;align-items:center;gap:7px;text-transform:none;
 letter-spacing:0;font-size:14px;font-weight:400;color:var(--fg);margin:0;cursor:pointer}
.row{display:flex;gap:10px;align-items:center;margin-top:20px;flex-wrap:wrap}
button.go,a.go{background:var(--accent);color:var(--accent-fg);border:0;border-radius:8px;
 padding:11px 20px;font:inherit;font-weight:650;cursor:pointer;
 text-decoration:none;display:inline-block;line-height:1.2}
a.go:hover{filter:brightness(1.08)}
button.go:disabled{opacity:.45;cursor:not-allowed}
button.ghost{background:none;color:var(--fg);border:1px solid var(--line);
 border-radius:8px;padding:10px 16px;font:inherit;cursor:pointer}
button:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.status{margin-top:22px}
.bar{height:7px;background:var(--line);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent);width:0;
 transition:width .4s ease}
.bar.indet>i{width:35%;animation:slide 1.3s ease-in-out infinite}
@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
.statline{display:flex;justify-content:space-between;gap:12px;font-size:13px;
 color:var(--muted);margin-top:9px}
.statline b{color:var(--fg);font-variant-numeric:tabular-nums}
.cur{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
 color:var(--muted);margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(105px,1fr));
 gap:10px;margin:20px 0 4px}
.stat{background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:11px 13px}
.stat b{display:block;font-size:21px;font-variant-numeric:tabular-nums}
.stat span{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.f{border:1px solid var(--line);border-left-width:4px;border-radius:8px;
 padding:13px 15px;margin:9px 0;background:var(--panel)}
.f.HIGH{border-left-color:var(--high)} .f.MED{border-left-color:var(--med)}
.f.LOW{border-left-color:var(--low)} .f.GAP{border-left-color:var(--high)}
.badge{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.05em;
 padding:2px 7px;border-radius:4px;vertical-align:1px}
.badge.HIGH,.badge.GAP{background:var(--high-bg);color:var(--high)}
.badge.MED{background:var(--med-bg);color:var(--med)}
.badge.LOW{background:var(--low-bg);color:var(--low)}
.f h3{display:inline;font-size:15px;margin:0 0 0 9px;font-weight:650}
.f .n{float:right;color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums}
.f p{margin:9px 0 0;font-size:13.5px}
.f .why{color:var(--muted);font-size:13px;margin-top:7px}
.f details{margin-top:8px;font-size:12.5px}
.f summary{cursor:pointer;color:var(--accent)}
.f ul{margin:6px 0 0;padding-left:18px;max-height:150px;overflow:auto}
.f li{word-break:break-all;margin:2px 0;color:var(--muted)}
.err{background:var(--high-bg);color:var(--high);border-radius:8px;padding:12px 14px;
 margin-top:16px;font-size:13.5px;white-space:pre-wrap}
.note{background:var(--med-bg);color:var(--med);border-radius:8px;padding:11px 14px;
 margin-top:16px;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
th{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
td button,td a{background:none;border:0;color:var(--accent);font:inherit;cursor:pointer;
 padding:0;text-decoration:none}
td a:hover,td button:hover{text-decoration:underline}
.hidden{display:none}
</style>
</head>
<body>
<div class="wrap">
  <header><h1>seocrawl</h1><span class="tag" id="ver">local</span></header>
  <p class="sub">Crawls run on this machine. Nothing is uploaded anywhere.</p>

  <div class="tabs" role="tablist">
    <button class="tab" role="tab" aria-selected="true" data-tab="crawl">Crawl a site</button>
    <button class="tab" role="tab" aria-selected="false" data-tab="keywords">Keyword map</button>
    <button class="tab" role="tab" aria-selected="false" data-tab="reports">Reports</button>
  </div>

  <!-- CRAWL -->
  <section class="panel" id="tab-crawl">
    <div class="grid">
      <div style="grid-column:1/-1">
        <label for="url">Site URL</label>
        <input type="text" id="url" placeholder="https://example.com" autocomplete="off">
      </div>
      <div>
        <label for="maxurls">Max URLs</label>
        <input type="number" id="maxurls" min="0" placeholder="all">
        <div class="hint">Blank crawls the whole site.</div>
      </div>
      <div>
        <label for="delay">Delay (seconds)</label>
        <input type="number" id="delay" value="0.7" min="0" step="0.1">
        <div class="hint">Politeness. Don't go below 0.5.</div>
      </div>
      <div>
        <label for="workers">Workers</label>
        <input type="number" id="workers" value="4" min="1" max="16">
      </div>
      <div>
        <label for="imgsample">Image sample / template</label>
        <input type="number" id="imgsample" value="25" min="0">
        <div class="hint">0 sizes every page (much slower).</div>
      </div>
    </div>
    <div class="checks">
      <label><input type="checkbox" id="spell" checked> Spellcheck</label>
      <label><input type="checkbox" id="psi"> PageSpeed Insights</label>
      <label><input type="checkbox" id="recrawl" checked> Ignore cache</label>
    </div>
    <div id="psikeywrap" class="hidden" style="margin-top:14px">
      <label for="psikey">PSI API key</label>
      <input type="text" id="psikey" placeholder="or set PSI_API_KEY">
    </div>
    <div class="row">
      <button class="go" id="start">Start crawl</button>
      <button class="ghost hidden" id="stop">Stop</button>
    </div>
  </section>

  <!-- KEYWORDS -->
  <section class="panel hidden" id="tab-keywords">
    <div class="grid">
      <div style="grid-column:1/-1">
        <label for="gkp">Keyword Planner export</label>
        <input type="file" id="gkp" accept=".csv,.tsv,.txt">
        <div class="hint">The UTF-16 tab file Google gives you. Read as-is.</div>
      </div>
      <div style="grid-column:1/-1">
        <label for="crawlpick">Join against</label>
        <select id="crawlpick"><option value="">Loading reports...</option></select>
      </div>
      <div><label for="contains">Contains</label>
        <input type="text" id="contains" placeholder="wallet, belt">
        <div class="hint">Comma list, OR-joined.</div></div>
      <div><label for="notcontains">Doesn't contain</label>
        <input type="text" id="notcontains" placeholder="code, coupon"></div>
      <div><label for="brand">Brand terms</label>
        <input type="text" id="brand" placeholder="acme, acme leather"></div>
      <div><label for="minvol">Min volume</label>
        <input type="number" id="minvol" value="0" min="0"></div>
    </div>
    <div class="checks">
      <label><input type="checkbox" id="idf" checked> Weight distinguishing tokens</label>
    </div>
    <div class="hint" style="margin-top:6px">
      On a single-niche site every page says the same nouns; without this, generic
      terms match everything and the gap list comes back empty.
    </div>
    <div class="row"><button class="go" id="startkw">Build keyword map</button></div>
    <div class="note">GKP volumes are ranges for non-spending accounts and
      geo-dependent — treat as relative sizes, not truth. GSC data supersedes
      this file the day you get access.</div>
  </section>

  <!-- REPORTS -->
  <section class="panel hidden" id="tab-reports">
    <table><thead><tr><th>Report</th><th>Created</th><th>Size</th><th></th></tr></thead>
      <tbody id="reportrows"><tr><td colspan="4">Loading...</td></tr></tbody></table>
  </section>

  <!-- STATUS -->
  <div class="status hidden" id="status">
    <div class="bar" id="bar"><i></i></div>
    <div class="statline">
      <span id="msg">Working...</span>
      <span><b id="count">0</b> pages · <b id="time">0s</b></span>
    </div>
    <div class="cur" id="cur"></div>
    <div id="stats" class="stats hidden"></div>
    <div id="funnel" class="hint hidden" style="margin-top:10px"></div>
    <div id="outputs" class="row hidden"></div>
    <div id="err" class="err hidden"></div>
    <div id="findings"></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t);
  if (c) n.className = c; if (x !== undefined) n.textContent = x; return n; };

/* tabs */
document.querySelectorAll('.tab').forEach(tab => tab.onclick = () => {
  document.querySelectorAll('.tab').forEach(t =>
    t.setAttribute('aria-selected', String(t === tab)));
  ['crawl','keywords','reports'].forEach(name =>
    $('#tab-' + name).classList.toggle('hidden', name !== tab.dataset.tab));
  if (tab.dataset.tab !== 'crawl') loadReports();
});
$('#psi').onchange = e => $('#psikeywrap').classList.toggle('hidden', !e.target.checked);

/* reports */
async function loadReports() {
  const data = await (await fetch('/api/reports')).json();
  const body = $('#reportrows'); body.innerHTML = '';
  const picker = $('#crawlpick'); picker.innerHTML = '';

  if (!data.reports.length) {
    body.appendChild(el('tr')).appendChild(el('td', '', 'No reports yet — run a crawl.'))
       .colSpan = 4;
    picker.appendChild(el('option', '', 'No reports yet'));
    return;
  }
  data.reports.forEach(r => {
    const tr = el('tr');
    tr.appendChild(el('td', '', r.name));
    tr.appendChild(el('td', '', r.when));
    tr.appendChild(el('td', '', r.size));
    const td = el('td');
    const download = el('a', '', 'Download');
    download.href = r.download;
    download.setAttribute('download', '');
    td.appendChild(download);
    td.appendChild(document.createTextNode('  '));
    const reveal = el('button', '', 'Show in folder');
    reveal.onclick = () => openFile(r.path);
    td.appendChild(reveal);
    tr.appendChild(td);
    body.appendChild(tr);

    const option = el('option', '', r.name);
    option.value = r.path;
    picker.appendChild(option);
  });
}

async function openFile(path) {
  const res = await fetch('/api/open', {method:'POST', body: JSON.stringify({path})});
  const data = await res.json();
  if (data.error) { showNote(data.error, true); return; }
  if (data.revealed) showNote(data.note + '  ' + data.path);
}

/* The one place the UI admits something did not go as asked. */
function showNote(text, isError) {
  let note = document.querySelector('#opennote');
  if (!note) {
    note = el('div');
    note.id = 'opennote';
    $('#status').appendChild(note);
  }
  note.className = isError ? 'err' : 'note';
  note.textContent = text;
  note.classList.remove('hidden');
}

/* start */
$('#start').onclick = async () => {
  const body = {
    url: $('#url').value,
    max_urls: Number($('#maxurls').value) || 0,
    delay: Number($('#delay').value),
    workers: Number($('#workers').value),
    image_sample: Number($('#imgsample').value),
    spellcheck: $('#spell').checked,
    psi: $('#psi').checked,
    psi_key: $('#psikey').value,
    recrawl: $('#recrawl').checked,
  };
  if (!body.url.trim()) { alert('Enter a site URL.'); return; }
  reset();
  const data = await (await fetch('/api/crawl',
    {method:'POST', body: JSON.stringify(body)})).json();
  if (data.error) { showError(data.error); return; }
  poll();
};

$('#startkw').onclick = async () => {
  const file = $('#gkp').files[0];
  if (!file) { alert('Choose a Keyword Planner export.'); return; }
  if (!$('#crawlpick').value) { alert('Pick a report to join against.'); return; }

  const params = new URLSearchParams({
    crawl: $('#crawlpick').value,
    contains: $('#contains').value,
    not_contains: $('#notcontains').value,
    brand: $('#brand').value,
    min_volume: $('#minvol').value || '0',
    use_idf: $('#idf').checked ? '1' : '0',
  });
  reset();
  const data = await (await fetch('/api/keywords?' + params,
    {method:'POST', body: await file.arrayBuffer()})).json();
  if (data.error) { showError(data.error); return; }
  poll();
};

$('#stop').onclick = async () => {
  $('#stop').disabled = true;
  await fetch('/api/stop', {method:'POST'});
};

function reset() {
  $('#status').classList.remove('hidden');
  ['stats','outputs','err','funnel','opennote'].forEach(i => {
    const node = $('#' + i); if (node) node.classList.add('hidden');
  });
  $('#findings').innerHTML = '';
  $('#start').disabled = $('#startkw').disabled = true;
  $('#stop').disabled = false;
  $('#stop').classList.remove('hidden');
  $('#bar').classList.add('indet');
  $('#bar').firstElementChild.style.width = '';
}

function showError(message) {
  $('#err').textContent = message;
  $('#err').classList.remove('hidden');
  finish();
}

function finish() {
  $('#start').disabled = $('#startkw').disabled = false;
  $('#stop').classList.add('hidden');
  $('#bar').classList.remove('indet');
}

let timer = null;
function poll() {
  clearInterval(timer);
  timer = setInterval(tick, 700);
  tick();
}

async function tick() {
  const job = await (await fetch('/api/status')).json();
  $('#msg').textContent = job.message || job.state;
  $('#count').textContent = job.done.toLocaleString();
  $('#time').textContent = job.elapsed < 90
    ? Math.round(job.elapsed) + 's'
    : Math.floor(job.elapsed / 60) + 'm ' + Math.round(job.elapsed % 60) + 's';
  $('#cur').textContent = job.current || '';

  if (job.total && job.done && job.state === 'running') {
    $('#bar').classList.remove('indet');
    $('#bar').firstElementChild.style.width =
      Math.min(100, 100 * job.done / job.total) + '%';
  }

  if (job.state === 'running') return;

  clearInterval(timer);
  finish();
  $('#bar').firstElementChild.style.width = '100%';

  if (job.state === 'error') { showError(job.error); return; }
  if (job.state === 'stopped') {
    $('#msg').textContent = 'Stopped early — report written from what was crawled.';
  }

  if (job.funnel) {
    $('#funnel').textContent = 'Funnel: ' + job.funnel;
    $('#funnel').classList.remove('hidden');
  }
  renderStats(job.stats);
  renderOutputs(job.outputs);
  renderFindings(job.findings);
  loadReports();
}

function renderStats(stats) {
  const box = $('#stats');
  box.innerHTML = '';
  const entries = Object.entries(stats || {});
  if (!entries.length) return;
  entries.forEach(([key, value]) => {
    const card = el('div', 'stat');
    card.appendChild(el('b', '', Number(value).toLocaleString()));
    card.appendChild(el('span', '', key));
    box.appendChild(card);
  });
  box.classList.remove('hidden');
}

function renderOutputs(outputs) {
  const box = $('#outputs');
  box.innerHTML = '';
  (outputs || []).forEach(out => {
    const link = el('a', 'go');
    link.textContent = (out.download ? 'Download ' : 'View ') + out.label;
    link.href = out.download || out.view;
    link.title = out.path;
    if (out.download) link.setAttribute('download', '');
    else link.target = '_blank';
    box.appendChild(link);
  });
  if ((outputs || []).length) {
    const path = el('div', 'hint', 'Saved to ' + outputs[0].path);
    path.style.flexBasis = '100%';
    box.appendChild(path);
  }
  if ((outputs || []).length) box.classList.remove('hidden');
}

function renderFindings(findings) {
  const box = $('#findings');
  box.innerHTML = '';
  (findings || []).forEach(f => {
    const card = el('div', 'f ' + f.severity);
    card.appendChild(el('span', 'badge ' + f.severity, f.severity));
    card.appendChild(el('h3', '', f.issue));
    card.appendChild(el('span', 'n', f.count + (f.severity === 'GAP' ? ' kws' : ' URLs')));
    card.appendChild(el('p', '', f.detail));
    card.appendChild(el('p', 'why', f.why));
    if (f.urls && f.urls.length) {
      const details = el('details');
      details.appendChild(el('summary', '', 'Examples'));
      const list = el('ul');
      f.urls.forEach(u => list.appendChild(el('li', '', u)));
      details.appendChild(list);
      card.appendChild(details);
    }
    box.appendChild(card);
  });
}

loadReports();
/* Pick up whatever the server is doing, or has just done.
   A crawl can run for twenty minutes, so closing the tab and coming back must
   show the result rather than an empty page. */
fetch('/api/status').then(r => r.json()).then(job => {
  if (job.state === 'idle') return;
  reset();
  if (job.state === 'running') { poll(); } else { tick(); }
});
</script>
</body>
</html>
"""
