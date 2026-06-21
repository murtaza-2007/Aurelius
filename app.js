// ── State ────────────────────────────────────────────────────────────────────
let ws = null;
let nodes = {};
let edges = [];
let centreNode = null;
let pathNodes = new Set();
let simulation = null;
let svgEl, gEl;
let width, height;
let running = false;
let _searchGen = 0;
let scoreCards = {};
let heroDismissed = false;
let paused = false;
let foundPath = [];
let _lastElapsedS = 0;

// ── NEW: autocomplete state (vector search) ───────────────────────────────────
let acDebounceTimers = { start: null, end: null };   // per-field debounce handles
let acActiveIndex = { start: -1, end: -1 };    // keyboard-highlighted row
let acLastResults = { start: [], end: [] };    // last suggestions per field
const AC_DEBOUNCE_MS = 80;    // tight debounce — feel instant, but avoid a request on every single keypress

// ── Rotating placeholder examples (hero inputs) ────────────────────────────────
// Nagpur → Mars is first (shown on launch) and held for 2s; every other pair
// uses the default 1.3s. Per-pair `duration` overrides DEFAULT_PLACEHOLDER_MS;
// a setTimeout chain (not setInterval) is what makes a per-pair duration
// possible — a single fixed-rate interval couldn't give one entry a different
// dwell time than the rest.
const PLACEHOLDER_PAIRS = [
  { pair: ['Nagpur', 'Mars'], duration: 2000 },
  { pair: ['Alexander the Great', 'Jazz music'] },
  { pair: ['Pizza', 'Mount Everest'] },
  { pair: ['Albert Einstein', 'Coffee'] },
  { pair: ['Leonardo da Vinci', 'The Internet'] },
  { pair: ['Cleopatra', 'Bitcoin'] },
  { pair: ['Shakespeare', 'Video games'] },
  { pair: ['Mahatma Gandhi', 'Formula 1'] },
  { pair: ['Vincent van Gogh', 'Climate change'] },
  { pair: ['Genghis Khan', 'Sushi'] },
  { pair: ['The Moon', 'Chess'] },
];
const DEFAULT_PLACEHOLDER_MS = 1300;
let _placeholderIdx = 0;
let _placeholderTimer = null;

function _applyPlaceholder() {
  const startEl = document.getElementById('hero-inp-start');
  const endEl = document.getElementById('hero-inp-end');
  if (!startEl || !endEl) return;
  const [s, e] = PLACEHOLDER_PAIRS[_placeholderIdx].pair;
  startEl.placeholder = `e.g. ${s}`;
  endEl.placeholder = `e.g. ${e}`;
}

function _scheduleNextPlaceholder() {
  const duration = PLACEHOLDER_PAIRS[_placeholderIdx].duration || DEFAULT_PLACEHOLDER_MS;
  _placeholderTimer = setTimeout(() => {
    const startEl = document.getElementById('hero-inp-start');
    const endEl = document.getElementById('hero-inp-end');
    // Don't fight the user — only advance while both fields are empty, so
    // cycling text never replaces something they've actually typed. Focus
    // alone does NOT block rotation: boot() auto-focuses hero-inp-start on
    // every load, so gating on activeElement here would freeze rotation at
    // the first pair for any visitor who hasn't yet clicked away — there's
    // no real cursor/typed content to interrupt in an empty, placeholder-only
    // field regardless of focus.
    const canAdvance = startEl && endEl && !startEl.value && !endEl.value;
    if (canAdvance) {
      _placeholderIdx = (_placeholderIdx + 1) % PLACEHOLDER_PAIRS.length;
      _applyPlaceholder();
    }
    _scheduleNextPlaceholder();
  }, duration);
}

function startPlaceholderRotation() {
  if (_placeholderTimer) return;
  _placeholderIdx = 0;   // Nagpur → Mars first, every time rotation (re)starts
  _applyPlaceholder();
  _scheduleNextPlaceholder();
}

function stopPlaceholderRotation() {
  clearTimeout(_placeholderTimer);
  _placeholderTimer = null;
}

// ── Hero input logic ──────────────────────────────────────────────────────────
function heroInputChanged() {
  const s = document.getElementById('hero-inp-start').value.trim();
  const e = document.getElementById('hero-inp-end').value.trim();

  // Labels light up when filled
  document.getElementById('lbl-start').classList.toggle('filled', s.length > 0);
  document.getElementById('lbl-end').classList.toggle('filled', e.length > 0);

  // Inputs get filled style
  document.getElementById('hero-inp-start').classList.toggle('filled', s.length > 0);
  document.getElementById('hero-inp-end').classList.toggle('filled', e.length > 0);

  // Arrow lights up when both filled
  document.getElementById('hero-arrow').classList.toggle('lit', s.length > 0 && e.length > 0);

  // Button and hint appear when both filled
  const ready = s.length > 0 && e.length > 0;
  document.getElementById('hero-btn').classList.toggle('ready', ready);
  document.getElementById('hero-hint').classList.toggle('visible', ready);
}
// NOTE: autocomplete (vector search) is wired separately via addEventListener
// in the NEW block below — heroInputChanged() above is the original,
// untouched function and does not need to know about autocomplete at all.

function heroKeyDown(e) {
  // NEW: if an autocomplete dropdown is open for this field, arrow keys /
  // Enter operate on the dropdown first. This is purely additive — if no
  // dropdown is open, every branch below falls through to the exact
  // original behaviour at the bottom of this function, untouched.
  const fieldName = e.target.id === 'hero-inp-start' ? 'start' : 'end';
  const dropdown = document.getElementById('ac-dropdown-' + fieldName);
  const isOpen = dropdown && dropdown.classList.contains('visible');

  if (isOpen && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
    e.preventDefault();
    const items = acLastResults[fieldName];
    if (items.length === 0) return;
    const delta = e.key === 'ArrowDown' ? 1 : -1;
    acActiveIndex[fieldName] = (acActiveIndex[fieldName] + delta + items.length) % items.length;
    renderAcDropdown(fieldName);
    return;
  }
  if (isOpen && e.key === 'Enter' && acActiveIndex[fieldName] >= 0) {
    e.preventDefault();
    selectAcSuggestion(fieldName, acLastResults[fieldName][acActiveIndex[fieldName]]);
    return;
  }
  if (isOpen && e.key === 'Escape') {
    closeAcDropdown(fieldName);
    return;
  }

  // ── Original behaviour (unchanged) ──────────────────────────────────────
  if (e.key === 'Enter') {
    const s = document.getElementById('hero-inp-start').value.trim();
    const en = document.getElementById('hero-inp-end').value.trim();
    if (s && en) startSearch();
    else if (!s) document.getElementById('hero-inp-start').focus();
    else document.getElementById('hero-inp-end').focus();
  }
}

// ── Wikipedia URL extractor ───────────────────────────────────────────────────
function extractTitleFromWikiUrl(input) {
  const match = input.match(/wikipedia\.org\/wiki\/([^#?]+)/);
  if (match) return decodeURIComponent(match[1]).replace(/_/g, ' ');
  return input;
}

// ════════════════════════════════════════════════════════════════════════════
// NEW: Vector-search autocomplete (hooks into the existing hero inputs via
// addEventListener below — does not modify heroInputChanged/heroKeyDown's
// original bodies, see the dedicated NOTE comments near those functions).
// ════════════════════════════════════════════════════════════════════════════

// Single source of truth for the backend origin. Derived from whatever host
// the page itself was loaded from (not hardcoded to 'localhost') so this
// works unmodified whether you open it as localhost, as a LAN IP from
// another device on the same network (backend already binds 0.0.0.0 — see
// main.py), or any other dev hostname — as long as the backend listens on
// the same host, just port 8000. After deploying to separate frontend/
// backend hosts (e.g. Render), replace this line with the fixed backend URL
// (e.g. 'https://aurelius-backend.onrender.com') — the WebSocket URL below
// is derived from it automatically (http→ws, https→wss), so nothing else in
// this file needs to change.
// window.location.hostname is '' when the page is opened as a local file
// (file://index.html, which is how this app is normally launched per
// CLAUDE.md) rather than served over http(s) — fall back to 'localhost' for
// that case so AC_BACKEND never becomes the invalid 'http://:8000'.
const AC_BACKEND = `http://${window.location.hostname || 'localhost'}:8000`;
const AC_BACKEND_WS = AC_BACKEND.replace(/^http/, 'ws');
const WIKI_OPENSEARCH = 'https://en.wikipedia.org/w/api.php';

/**
 * Debounced fetch directly to Wikipedia's opensearch API. Called on every
 * keystroke in a hero input field, but the actual network request is
 * delayed by AC_DEBOUNCE_MS so rapid typing doesn't spam the API. No
 * backend involved — CORS-enabled, instant, and accurate for any article
 * (not limited to a curated seed list).
 */
function triggerAutocomplete(fieldName) {
  const inputId = fieldName === 'start' ? 'hero-inp-start' : 'hero-inp-end';
  const query = document.getElementById(inputId).value.trim();

  if (acDebounceTimers[fieldName]) clearTimeout(acDebounceTimers[fieldName]);

  if (query.length < 2) {
    closeAcDropdown(fieldName);
    return;
  }

  acDebounceTimers[fieldName] = setTimeout(async () => {
    try {
      const params = new URLSearchParams({
        action: 'opensearch', search: query, limit: '5',
        namespace: '0', format: 'json', origin: '*',
      });
      const res = await fetch(`${WIKI_OPENSEARCH}?${params}`);
      if (!res.ok) { closeAcDropdown(fieldName); return; }
      const data = await res.json();
      const suggestions = Array.isArray(data) && data.length > 1 ? data[1] : [];

      // The user may have kept typing while this request was in flight —
      // if the field no longer matches what we searched for, drop the
      // (now-stale) result instead of flashing an outdated dropdown.
      const currentValue = document.getElementById(inputId).value.trim();
      if (currentValue !== query) return;

      acLastResults[fieldName] = suggestions;
      acActiveIndex[fieldName] = -1;
      renderAcDropdown(fieldName);
    } catch (err) {
      // Network hiccup — fail silently, autocomplete is a nice-to-have
      // and must never block the user from just typing a title and
      // pressing Enter as before.
      closeAcDropdown(fieldName);
    }
  }, AC_DEBOUNCE_MS);
}

/** Render the top-5 suggestions below the given hero field. */
function renderAcDropdown(fieldName) {
  const dropdown = document.getElementById('ac-dropdown-' + fieldName);
  const items = acLastResults[fieldName];

  if (!items || items.length === 0) {
    closeAcDropdown(fieldName);
    return;
  }

  dropdown.innerHTML = items.map((title, i) => `
    <div class="ac-item${i === acActiveIndex[fieldName] ? ' ac-active' : ''}"
         data-idx="${i}" data-title="${title.replace(/"/g, '&quot;')}">
      <svg class="ac-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="14" height="14"><path d="M4 2h6l3 3v9H4V2z"/><path d="M10 2v3h3"/><line x1="6" y1="7" x2="11" y2="7"/><line x1="6" y1="10" x2="10" y2="10"/></svg>${title}
    </div>
  `).join('');
  dropdown.classList.add('visible');
  // BUGFIX: previously used inline onclick="selectAcSuggestion('${fieldName}', ${JSON.stringify(title)})"
  // which broke because JSON.stringify's double-quotes collided with the outer HTML
  // attribute's double-quotes, corrupting the attribute and silently failing on click.
  // Event delegation with data-title avoids the quote-collision entirely.
  dropdown.querySelectorAll('.ac-item').forEach(el => {
    el.addEventListener('click', () => selectAcSuggestion(fieldName, el.dataset.title));
  });
}

function closeAcDropdown(fieldName) {
  const dropdown = document.getElementById('ac-dropdown-' + fieldName);
  if (dropdown) {
    dropdown.classList.remove('visible');
    dropdown.innerHTML = '';
  }
  acLastResults[fieldName] = [];
  acActiveIndex[fieldName] = -1;
}

/** Called when the user clicks (or presses Enter on) a suggestion row. */
function selectAcSuggestion(fieldName, title) {
  const inputId = fieldName === 'start' ? 'hero-inp-start' : 'hero-inp-end';
  document.getElementById(inputId).value = title;
  closeAcDropdown(fieldName);
  // Re-run the existing, untouched hero validation logic (lights up the
  // label/arrow/button exactly as if the user had typed this themselves).
  heroInputChanged();
}

// Close dropdowns when clicking anywhere outside them (standard
// autocomplete UX) — purely additive, does not interfere with any
// existing click handlers elsewhere in the app.
document.addEventListener('click', (e) => {
  if (!e.target.closest('.hero-field')) {
    closeAcDropdown('start');
    closeAcDropdown('end');
  }
});

// ════════════════════════════════════════════════════════════════════════════
// NEW: Random button — picks two curated topics via /api/random, then
// reuses the EXISTING startSearch() pipeline untouched (same WebSocket
// flow, same A* navigator on the backend, same rendering code).
// ════════════════════════════════════════════════════════════════════════════
async function useRandomPair() {
  // Per the requirements: only auto-fill if the user hasn't already typed
  // a query themselves — we don't want to clobber a half-typed search.
  const sVal = document.getElementById('hero-inp-start').value.trim();
  const eVal = document.getElementById('hero-inp-end').value.trim();
  if (sVal || eVal) {
    // BUGFIX: setStatus() alone is invisible here — #status-bar lives inside
    // #topbar, which has opacity:0 until the hero is dismissed. Surface the
    // message on the button itself too, since that's always visible.
    setStatus('Clear both fields first to use Random.');
    btn_guard_msg(document.getElementById('hero-random-btn'), 'Clear fields first');
    return;
  }

  const btn = document.getElementById('hero-random-btn');
  btn.classList.add('loading');
  btn.textContent = 'Picking...';

  try {
    const res = await fetch(`${AC_BACKEND}/api/random`);
    if (!res.ok) throw new Error('Random pick failed');
    const data = await res.json();

    document.getElementById('hero-inp-start').value = data.start;
    document.getElementById('hero-inp-end').value = data.end;
    heroInputChanged();   // existing function — lights up labels/arrow/button

    btn.classList.remove('loading');
    btn.textContent = 'Random';

    // Small delay so the user can actually see which two topics were
    // picked before the hero screen animates away.
    setTimeout(() => startSearch(), 600);
  } catch (err) {
    btn.classList.remove('loading');
    setStatus('Could not reach backend for Random pick.');
    // BUGFIX: status bar is invisible on hero screen — show error on button too.
    btn_guard_msg(btn, '⚠️ Backend offline');
  }
}

// Small helper for useRandomPair(): briefly shows an error message directly
// on the button (always visible on hero screen), then restores its label.
function btn_guard_msg(btn, msg) {
  const original = 'Random';
  btn.textContent = msg;
  setTimeout(() => { btn.textContent = original; }, 1800);
}

// ── Dismiss hero, show app ────────────────────────────────────────────────────
function dismissHero(start, end) {
  if (heroDismissed) return;
  heroDismissed = true;
  stopPlaceholderRotation();

  // Copy values to topbar inputs
  document.getElementById('inp-start').value = start;
  document.getElementById('inp-end').value = end;

  // Animate hero out
  const hero = document.getElementById('hero');
  hero.classList.add('hiding');
  setTimeout(() => hero.classList.add('hidden'), 500);

  // Animate topbar in
  document.getElementById('topbar').classList.add('visible');

  // On phones/narrow tablets, default the search-controls topbar and the
  // path/scores/log panel to collapsed the moment the main view opens, so
  // what you see first is the live graph forming the path, not a wall of
  // chrome. Both are reachable again via their handle bars. No-op on
  // desktop/tablet — the CSS that makes .mobile-collapsed do anything only
  // exists under the same max-width:820px breakpoint this check mirrors.
  if (isMobileLayout()) {
    document.getElementById('topbar').classList.add('mobile-collapsed');
    document.getElementById('panel').classList.add('mobile-collapsed');
    _syncMobileHandles();
  }
}

// ── Mobile topbar/panel collapse ──────────────────────────────────────────────
function isMobileLayout() {
  return window.matchMedia('(max-width: 820px)').matches;
}

function toggleMobileTopbar() {
  document.getElementById('topbar').classList.toggle('mobile-collapsed');
  _syncMobileHandles();
}

function toggleMobilePanel() {
  document.getElementById('panel').classList.toggle('mobile-collapsed');
  _syncMobileHandles();
}

// Keeps each handle's chevron direction (and its own .is-collapsed state,
// used purely for the CSS rotate transform) matching what it actually
// controls, since the two can change independently of each other.
function _syncMobileHandles() {
  const topbarCollapsed = document.getElementById('topbar').classList.contains('mobile-collapsed');
  const panelCollapsed = document.getElementById('panel').classList.contains('mobile-collapsed');
  const th = document.getElementById('mobile-topbar-handle');
  const ph = document.getElementById('mobile-panel-handle');
  if (th) th.classList.toggle('is-collapsed', topbarCollapsed);
  if (ph) ph.classList.toggle('is-collapsed', panelCollapsed);

  // Collapsing/expanding either bar resizes #canvas-wrap via CSS transition,
  // but that's an internal layout change, not a viewport resize — it never
  // fires the `resize` listener that normally keeps the d3 force simulation
  // centered. Re-measure once the 0.32s transition (styles.css) finishes, or
  // the graph stays centered on the stale pre-toggle canvas size.
  setTimeout(_resyncCanvasSize, 340);
}

function _resyncCanvasSize() {
  if (!simulation) return;
  const cw = document.getElementById('canvas-wrap');
  if (!cw) return;
  width = cw.clientWidth;
  height = cw.clientHeight;
  simulation.force('center', d3.forceCenter(width / 2, height / 2));
  simulation.alpha(0.3).restart();
}

// ── Search ────────────────────────────────────────────────────────────────────
function startSearch() {
  // Pull from whichever screen is active
  let rawStart, rawEnd;
  if (!heroDismissed) {
    rawStart = document.getElementById('hero-inp-start').value.trim();
    rawEnd = document.getElementById('hero-inp-end').value.trim();
  } else {
    rawStart = document.getElementById('inp-start').value.trim();
    rawEnd = document.getElementById('inp-end').value.trim();
  }

  if (!rawStart || !rawEnd) {
    setStatus('Please enter both a start and end article.');
    return;
  }
  if (running) return;

  const start = extractTitleFromWikiUrl(rawStart);
  const end = extractTitleFromWikiUrl(rawEnd);

  dismissHero(rawStart, rawEnd);
  resetAll(false);
  running = true;
  document.getElementById('btn-search').disabled = true;
  switchTab('log');
  document.getElementById('stats-bar').classList.add('visible');
  document.getElementById('btn-pause-bar').classList.add('visible');
  const stepsLive = document.getElementById('sv-steps-live');
  if (stepsLive) { document.getElementById('sv-steps-count').textContent = '0'; stepsLive.classList.add('visible'); }
  paused = false;
  foundPath = [];

  addLog(`Starting: "${start}" → "${end}"`, 'highlight');
  setStatus('Connecting...');

  const gen = ++_searchGen;
  ws = new WebSocket(`${AC_BACKEND_WS}/ws`);

  ws.onopen = () => {
    if (gen !== _searchGen) return;
    ws.send(JSON.stringify({ start, end }));
    setStatus('Connected. Running A*...');
  };

  ws.onmessage = e => {
    if (gen !== _searchGen) return;
    try { handleMessage(JSON.parse(e.data)); }
    catch (err) { console.error('Parse error:', err); }
  };

  ws.onerror = () => {
    if (gen !== _searchGen) return;
    // Only a connection error mid-search is a real problem. When the search
    // finishes, the backend returns from the WS handler and closes the
    // socket — mobile browsers (Safari/Chrome on iOS especially) surface
    // that normal teardown as an `error` event, which would otherwise fire
    // this scary "is the backend running?" message even though the path was
    // found seconds earlier. `running` is false once found/not_found/error
    // has been handled, so bail out — there's nothing wrong to report.
    if (!running) return;
    setStatus('⚠️  Cannot connect to backend. Is main.py running?');
    addLog('WebSocket error — is the backend running? (python main.py)', 'error');
    document.getElementById('btn-search').disabled = false;
    running = false;
  };

  ws.onclose = () => {
    if (gen !== _searchGen) return;
    if (running) {
      setStatus('Connection closed.');
      running = false;
      document.getElementById('btn-search').disabled = false;
    }
  };
}

function resetAll(showHero = true) {
  if (ws) { ws.close(); ws = null; }
  nodes = {}; edges = []; centreNode = null;
  pathNodes = new Set(); scoreCards = {};
  running = false;
  // BUGFIX: "New Search" lives inside #success-modal itself and calls
  // resetAll(true) directly — without this, the stale "Path Discovered"
  // modal stays visible on top of the freshly-reset hero screen,
  // permanently blocking it until the next search happens to find a path.
  document.getElementById('success-modal').classList.remove('visible');
  document.getElementById('btn-search').disabled = false;
  document.getElementById('path-banner').classList.remove('visible');
  document.getElementById('path-display').innerHTML = '<p style="font-size:13px;color:var(--text2)">Path will appear here as A* explores.</p>';
  document.getElementById('scores-list').innerHTML = '';
  document.getElementById('log').innerHTML = '';
  setStatus('Enter two Wikipedia topics and hit Find Path.');
  document.getElementById('stats-bar').classList.remove('visible');
  _hidePauseBtn();
  document.getElementById('btn-export').classList.remove('visible');
  paused = false;
  foundPath = [];

  if (showHero) {
    heroDismissed = false;
    document.getElementById('topbar').classList.remove('visible');
    document.getElementById('topbar').classList.remove('mobile-collapsed');
    document.getElementById('panel').classList.remove('mobile-collapsed');
    const hero = document.getElementById('hero');
    hero.classList.remove('hiding', 'hidden');
    document.getElementById('hero-inp-start').value = '';
    document.getElementById('hero-inp-end').value = '';
    heroInputChanged();
    resetHeroDisplay();
  }

  initSVG();
}

// ── Init SVG ──────────────────────────────────────────────────────────────────
function initSVG() {
  svgEl = d3.select('#graph');
  width = document.getElementById('canvas-wrap').clientWidth;
  height = document.getElementById('canvas-wrap').clientHeight;

  svgEl.selectAll('*').remove();

  // Define glow filter
  const defs = svgEl.append('defs');
  const glow = defs.append('filter')
    .attr('id', 'glow')
    .attr('x', '-30%')
    .attr('y', '-30%')
    .attr('width', '160%')
    .attr('height', '160%');
  glow.append('feGaussianBlur')
    .attr('stdDeviation', '6')
    .attr('result', 'blur');
  glow.append('feMerge')
    .append('feMergeNode').attr('in', 'blur');
  glow.select('feMerge')
    .append('feMergeNode').attr('in', 'SourceGraphic');

  const zoom = d3.zoom()
    .scaleExtent([0.2, 4])
    .on('zoom', e => gEl.attr('transform', e.transform));
  svgEl.call(zoom);

  gEl = svgEl.append('g').attr('id', 'main-g');
  gEl.append('g').attr('id', 'edge-layer');
  gEl.append('g').attr('id', 'node-layer');
  gEl.append('g').attr('id', 'label-layer');

  simulation = d3.forceSimulation()
    .force('link', d3.forceLink().id(d => d.id).distance(120).strength(0.4))
    // distanceMax caps how far the mutual-repulsion force reaches between any
    // two nodes. Without it, a long-running search (up to 60 expansions, 25
    // new nodes per step) keeps accumulating cumulative repulsion across the
    // WHOLE graph — every node keeps pushing every other node apart forever,
    // so the cluster's total footprint grows without bound as more nodes
    // appear, even though the camera's own zoom level never changes. That
    // unbounded sprawl is what reads as "the page automatically zooms out" —
    // capping the repulsion's reach keeps the graph's footprint roughly
    // stable once nodes are a few hundred px apart, instead of ballooning.
    .force('charge', d3.forceManyBody().strength(-300).distanceMax(420))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide(44))
    .alphaDecay(0.02)
    .on('tick', ticked);
}

// ── D3 tick ───────────────────────────────────────────────────────────────────
function ticked() {
  d3.selectAll('.edge-line')
    .attr('x1', d => (nodes[d.from] || {}).x || 0)
    .attr('y1', d => (nodes[d.from] || {}).y || 0)
    .attr('x2', d => (nodes[d.to] || {}).x || 0)
    .attr('y2', d => (nodes[d.to] || {}).y || 0);

  d3.selectAll('.node-circle')
    .attr('cx', d => d.x)
    .attr('cy', d => d.y);

  d3.selectAll('.node-label-el')
    .attr('x', d => d.x)
    .attr('y', d => d.y + nodeRadius(d) + 15);
}

function nodeRadius(d) {
  if (d.state === 'centre') return 22;
  if (d.state === 'target') return 18;
  if (pathNodes.has(d.id)) return 15;
  return 12;
}

function nodeColor(d) {
  const s = getComputedStyle(document.documentElement);
  if (d.state === 'centre') return s.getPropertyValue('--node-centre').trim();
  if (d.state === 'target') return s.getPropertyValue('--node-target').trim();
  if (d.state === 'closed') return '#374151';
  if (d.state === 'gated') return '#4b2020';
  if (pathNodes.has(d.id)) return foundPath.length ? '#34d399' : s.getPropertyValue('--node-path').trim();
  return s.getPropertyValue('--node-open').trim();
}

function edgeColor(d) {
  if (d.isPath) return foundPath.length ? 'rgba(52,211,153,0.9)' : 'rgba(249,115,22,0.85)';
  const fromNode = nodes[d.from];
  if (fromNode && fromNode.state === 'closed') return 'rgba(124,106,247,0.18)';
  return 'rgba(124,106,247,0.38)';
}
function edgeWidth(d) { return d.isPath ? 3 : 1.2; }

// ── Render ────────────────────────────────────────────────────────────────────
function render() {
  const nodeArr = Object.values(nodes);

  const edgeSel = d3.select('#edge-layer')
    .selectAll('.edge-line')
    .data(edges, d => d.from + '→' + d.to);

  edgeSel.enter().append('line')
    .attr('class', 'edge-line')
    .attr('stroke-opacity', 0)
    .attr('stroke-linecap', 'round')
    .transition().duration(400)
    .attr('stroke-opacity', 1);

  edgeSel.exit().remove();

  d3.selectAll('.edge-line')
    .attr('stroke', edgeColor)
    .attr('stroke-width', edgeWidth)
    .attr('class', d => d.isPath && foundPath.length ? 'edge-line edge-path-found' : 'edge-line');

  const circSel = d3.select('#node-layer')
    .selectAll('.node-circle')
    .data(nodeArr, d => d.id);

  circSel.enter().append('circle')
    .attr('class', 'node-circle')
    .attr('r', 0)
    .attr('fill', nodeColor)
    .attr('stroke', '#0a0a0f')
    .attr('stroke-width', 2.5)
    .style('cursor', 'pointer')
    .on('mouseover', onNodeHover)
    .on('mouseout', onNodeOut)
    .on('click', onNodeClick)
    .call(d3.drag()
      .on('start', dragStart)
      .on('drag', dragged)
      .on('end', dragEnd))
    .transition().duration(350)
    .attr('r', nodeRadius);

  d3.selectAll('.node-circle')
    .attr('class', 'node-circle')
    .attr('fill', nodeColor)
    .style('filter', d => {
      if (d.state === 'centre' || d.state === 'target' || pathNodes.has(d.id)) {
        return 'url(#glow)';
      }
      return 'none';
    })
    .transition().duration(200)
    .attr('r', nodeRadius);

  circSel.exit().transition().duration(200).attr('r', 0).remove();

  const lblSel = d3.select('#label-layer')
    .selectAll('.node-label-el')
    .data(nodeArr, d => d.id);

  lblSel.enter().append('text')
    .attr('class', d => 'node-label-el ' + (d.state === 'centre' ? 'centre-label' : 'node-label'))
    .attr('opacity', 0)
    .transition().duration(400)
    .attr('opacity', d => {
      if (d.state === 'centre' || d.state === 'target') return 1;
      if (pathNodes.has(d.id)) return 0.9;
      return 0.45;
    });

  d3.selectAll('.node-label-el')
    .text(d => truncate(d.id, d.state === 'centre' ? 24 : 18))
    .attr('class', d => 'node-label-el ' + (d.state === 'centre' ? 'centre-label' : 'node-label'))
    .attr('fill', d =>
      d.state === 'centre' ? 'var(--text)' :
        d.state === 'target' ? 'var(--green)' : 'var(--text2)')
    .transition().duration(300)
    .attr('opacity', d => {
      if (d.state === 'centre' || d.state === 'target') return 1;
      if (pathNodes.has(d.id)) return 0.9;
      return 0.42;
    });

  lblSel.exit().remove();

  simulation.nodes(nodeArr);
  simulation.force('link').links(edges.map(e => ({
    source: e.from, target: e.to, isPath: e.isPath
  })));
  simulation.alpha(0.3).restart();
}

// ── Pan to node ───────────────────────────────────────────────────────────────
function panToNode(nodeId) {
  const n = nodes[nodeId];
  if (!n || !n.x) return;
  const cw = document.getElementById('canvas-wrap').clientWidth;
  const ch = document.getElementById('canvas-wrap').clientHeight;
  const t = d3.zoomTransform(svgEl.node());
  const tx = cw / 2 - t.k * n.x;
  const ty = ch / 2 - t.k * n.y;
  svgEl.transition().duration(600).ease(d3.easeCubicInOut)
    .call(d3.zoom().transform, d3.zoomIdentity.translate(tx, ty).scale(t.k));
}

// ── Drag ──────────────────────────────────────────────────────────────────────
function dragStart(e, d) { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; }
function dragged(e, d) { d.fx = e.x; d.fy = e.y; }
function dragEnd(e, d) { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }

// ── Tooltip ───────────────────────────────────────────────────────────────────
function onNodeHover(e, d) {
  const tip = document.getElementById('tooltip');
  document.getElementById('tt-title').textContent = d.id;
  const scores = [];
  if (d.g != null) scores.push(`g(n) = ${d.g}  hops from start`);
  if (d.h != null) scores.push(`h(n) = ${d.h}  heuristic`);
  if (d.f != null) scores.push(`f(n) = ${d.f}  total`);
  document.getElementById('tt-scores').innerHTML = scores.join('<br>');
  tip.style.left = (e.pageX + 14) + 'px';
  tip.style.top = (e.pageY - 32) + 'px';
  tip.classList.add('visible');
}
function onNodeOut() { document.getElementById('tooltip').classList.remove('visible'); }
function onNodeClick(e, d) { panToNode(d.id); }

// ── Helpers ───────────────────────────────────────────────────────────────────
function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + '…' : s; }
function setStatus(msg) { document.getElementById('status-bar').textContent = msg; }

function addLog(msg, type = '') {
  const log = document.getElementById('log');
  const entry = document.createElement('div');
  entry.className = 'log-entry ' + type;
  const time = new Date().toLocaleTimeString('en', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  entry.textContent = `[${time}] ${msg}`;
  log.insertBefore(entry, log.firstChild);
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t, i) => {
    const names = ['path', 'scores', 'log', 'legend'];
    t.classList.toggle('active', names[i] === name);
  });
  document.querySelectorAll('.tab-content').forEach(c => {
    c.classList.toggle('active', c.id === 'tab-' + name);
  });
}

function updatePathPanel(pathArr) {
  const div = document.getElementById('path-display');
  if (!pathArr || pathArr.length === 0) return;
  div.innerHTML = '';
  pathArr.forEach((p, i) => {
    const row = document.createElement('div');
    row.className = 'path-step';
    const dot = document.createElement('div');
    dot.className = 'dot' + (i === 0 ? ' start' : i === pathArr.length - 1 ? ' end' : '');

    const btn = document.createElement('a');
    btn.className = 'hop-btn-link' + (i === 0 ? ' start-hop' : i === pathArr.length - 1 ? ' end-hop' : '');
    btn.href = 'https://en.wikipedia.org/wiki/' + encodeURIComponent(p.replace(/ /g, '_'));
    btn.target = '_blank';
    btn.textContent = p;

    row.appendChild(dot);
    row.appendChild(btn);
    div.appendChild(row);
    if (i < pathArr.length - 1) {
      const arr = document.createElement('div');
      arr.className = 'path-step';
      arr.style.margin = '2px 0 2px 4px';
      arr.innerHTML = '<span class="arrow">↓</span>';
      div.appendChild(arr);
    }
  });
}

function updateScores(nodeData) {
  scoreCards[nodeData.id] = nodeData;
  const list = document.getElementById('scores-list');
  list.innerHTML = '';
  const sorted = Object.values(scoreCards)
    .filter(n => n.f != null)
    .sort((a, b) => a.f - b.f)
    .slice(0, 30);
  sorted.forEach(n => {
    const card = document.createElement('div');
    card.className = 'score-card';
    card.innerHTML = `
      <div class="title">${n.id}</div>
      <div class="score-row"><span>f(n)</span><span>${n.f}</span></div>
      <div class="score-row"><span>g(n)</span><span>${n.g ?? '—'}</span></div>
      <div class="score-row"><span>h(n)</span><span>${n.h ?? '—'}</span></div>
    `;
    list.appendChild(card);
  });
}

function showPathBanner(pathArr) {
  const banner = document.getElementById('path-banner');
  banner.innerHTML = '';
  banner.classList.add('visible');
  const lbl = document.createElement('span');
  lbl.style.cssText = 'font-size:13px;color:var(--text2);margin-right:12px;white-space:nowrap;font-weight:600';
  lbl.textContent = `✓ Path (${pathArr.length - 1} hops):`;
  banner.appendChild(lbl);
  pathArr.forEach((p, i) => {
    const btn = document.createElement('a');
    btn.className = 'hop-btn-link' + (i === 0 ? ' start-hop' : i === pathArr.length - 1 ? ' end-hop' : '');
    btn.style.flex = 'none';
    btn.href = 'https://en.wikipedia.org/wiki/' + encodeURIComponent(p.replace(/ /g, '_'));
    btn.target = '_blank';
    btn.textContent = p;
    banner.appendChild(btn);
    if (i < pathArr.length - 1) {
      const arr = document.createElement('span');
      arr.className = 'banner-arrow';
      arr.textContent = '→';
      banner.appendChild(arr);
    }
  });
}

// ── WebSocket message handler ─────────────────────────────────────────────────
function handleMessage(msg) {
  switch (msg.event) {

    case 'status':
      setStatus(msg.message);
      addLog(msg.message);
      break;

    case 'resolved':
      addLog(`Resolved: "${msg.start}" → "${msg.end}"`, 'highlight');
      setStatus(`Navigating: ${msg.start} → ${msg.end}`);
      break;

    case 'node_add': {
      nodes[msg.id] = {
        id: msg.id, g: msg.g, h: msg.h, f: msg.f,
        state: msg.state,
        x: width / 2 + (Math.random() - .5) * 120,
        y: height / 2 + (Math.random() - .5) * 120,
        vx: 0, vy: 0,
      };
      updateScores(msg);
      render();
      break;
    }

    case 'expand_node': {
      if (centreNode && centreNode !== msg.id) {
        if (nodes[centreNode]) nodes[centreNode].state = 'open';
      }
      centreNode = msg.id;
      if (nodes[msg.id]) {
        nodes[msg.id].state = 'centre';
        nodes[msg.id].g = msg.g;
        nodes[msg.id].h = msg.h;
        nodes[msg.id].f = msg.f;
      }

      pathNodes = new Set(msg.path_so_far || []);
      edges.forEach(e => {
        const pArr = msg.path_so_far || [];
        e.isPath = false;
        for (let i = 0; i < pArr.length - 1; i++) {
          if ((e.from === pArr[i] && e.to === pArr[i + 1]) ||
            (e.to === pArr[i] && e.from === pArr[i + 1])) {
            e.isPath = true;
          }
        }
      });

      updatePathPanel(msg.path_so_far);
      updateScores(msg);
      addLog(`Expanding: ${msg.id}  f=${msg.f}`, 'highlight');
      if (msg.stats) _updateStats(msg.stats);
      render();
      setTimeout(() => panToNode(msg.id), 200);
      break;
    }

    case 'node_state': {
      if (nodes[msg.id]) nodes[msg.id].state = msg.state;
      render();
      break;
    }

    case 'neighbours': {
      msg.nodes.forEach(n => {
        if (!nodes[n.id]) {
          const cn = nodes[msg.centre] || { x: width / 2, y: height / 2 };
          const angle = Math.random() * 2 * Math.PI;
          const dist = 90 + Math.random() * 70;
          nodes[n.id] = {
            id: n.id, g: n.g, h: n.h, f: n.f,
            state: n.state,
            x: cn.x + Math.cos(angle) * dist,
            y: cn.y + Math.sin(angle) * dist,
            vx: 0, vy: 0,
          };
        } else {
          nodes[n.id].g = n.g;
          nodes[n.id].h = n.h;
          nodes[n.id].f = n.f;
          if (nodes[n.id].state !== 'target' && nodes[n.id].state !== 'centre') {
            nodes[n.id].state = n.state;
          }
        }
        updateScores(n);
      });

      msg.edges.forEach(e => {
        const exists = edges.find(ex => ex.from === e.from && ex.to === e.to);
        if (!exists) edges.push({ from: e.from, to: e.to, isPath: false });
      });

      render();
      break;
    }

    case 'found': {
      const path = msg.path;
      pathNodes = new Set(path);

      // Synthesize missing nodes: the meeting check (d1/d2) skips the
      // neighbours event, so bridge nodes may not exist in the frontend.
      path.forEach((id, i) => {
        if (!nodes[id]) {
          const prev = i > 0 ? nodes[path[i - 1]] : null;
          nodes[id] = {
            id, g: i, h: 0, f: i,
            state: 'path',
            x: (prev ? prev.x : width / 2) + (Math.random() - .5) * 100,
            y: (prev ? prev.y : height / 2) + (Math.random() - .5) * 100,
            vx: 0, vy: 0,
          };
        }
      });

      path.forEach(id => { if (nodes[id]) nodes[id].state = 'path'; });
      if (nodes[path[0]]) nodes[path[0]].state = 'start';
      if (nodes[path[path.length - 1]]) nodes[path[path.length - 1]].state = 'target';
      edges.forEach(e => {
        e.isPath = false;
        for (let i = 0; i < path.length - 1; i++) {
          if ((e.from === path[i] && e.to === path[i + 1]) ||
            (e.to === path[i] && e.from === path[i + 1])) {
            e.isPath = true;
          }
        }
      });
      for (let i = 0; i < path.length - 1; i++) {
        const a = path[i], b = path[i + 1];
        const exists = edges.find(e =>
          (e.from === a && e.to === b) || (e.from === b && e.to === a));
        if (exists) {
          exists.isPath = true;
        } else {
          edges.push({ from: a, to: b, isPath: true });
        }
      }
      updatePathPanel(path);
      showPathBanner(path);
      addLog(`✓ Found! ${path.join(' → ')}  (${msg.total_hops} hops, ${msg.steps} steps)`, 'success');
      setStatus(`Done! Path found in ${msg.total_hops} hops.`);
      render();
      document.getElementById('btn-search').disabled = false;
      running = false;
      foundPath = path;
      _hidePauseBtn();
      document.getElementById('btn-export').classList.add('visible');
      if (msg.stats) _updateStats(msg.stats);
      animatePath(path, msg.steps, msg.total_hops, msg.display_texts);
      break;
    }

    case 'not_found': {
      addLog(`✗ ${msg.message}`, 'error');
      setStatus(msg.message);
      document.getElementById('btn-search').disabled = false;
      running = false;
      _hidePauseBtn();
      if (msg.stats) _updateStats(msg.stats);
      break;
    }

    case 'stats': {
      _updateStats(msg);
      break;
    }


    case 'error': {
      addLog(`Error: ${msg.message}`, 'error');
      setStatus(`Error: ${msg.message}`);
      document.getElementById('btn-search').disabled = false;
      running = false;
      _hidePauseBtn();
      break;
    }
  }
}


// ── Stats update ─────────────────────────────────────────────────────────────
function _updateStats(s) {
  if (!s) return;
  _lastElapsedS = s.elapsed_s ?? 0;
  const cnt = document.getElementById('sv-steps-count');
  if (cnt) cnt.textContent = (s.nodes_visited ?? 0).toLocaleString();
}

// ── Pause / Resume ────────────────────────────────────────────────────────────
function togglePause() {
  paused = !paused;
  const btn = document.getElementById('btn-pause-bar');
  if (paused) {
    btn.textContent = '▶ Resume';
    btn.classList.add('paused');
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ control: 'pause' }));
  } else {
    btn.textContent = '⏸ Pause';
    btn.classList.remove('paused');
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ control: 'resume' }));
  }
}

function _hidePauseBtn() {
  paused = false;
  const btn = document.getElementById('btn-pause-bar');
  btn.classList.remove('visible', 'paused');
  btn.textContent = '⏸ Pause';
  const sl = document.getElementById('sv-steps-live');
  if (sl) sl.classList.remove('visible');
}

// ── Export path ───────────────────────────────────────────────────────────────
function exportPath() {
  if (!foundPath.length) return;
  const lines = [
    'Aurelius — Path Export',
    '='.repeat(50),
    '',
    `Path (${foundPath.length - 1} hops):`,
    '',
    ...foundPath.map((p, i) => {
      const url = 'https://en.wikipedia.org/wiki/' + encodeURIComponent(p.replace(/ /g, '_'));
      return `${i + 1}. ${p}\n   ${url}`;
    }),
    '',
    `Exported: ${new Date().toLocaleString()}`,
  ];
  const blob = new Blob([lines.join('\n')], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'aurelius-path.txt';
  a.click();
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.code === 'Space') { e.preventDefault(); if (running) togglePause(); }
  if (e.code === 'KeyR') { resetAll(); }
  if (e.code === 'KeyE') { exportPath(); }
  if (e.key === '?') { openAboutModal(); }
});

function swapInputs() {
  const s = document.getElementById('inp-start');
  const en = document.getElementById('inp-end');
  if (!s || !en) return;
  [s.value, en.value] = [en.value, s.value];
}

function logoClick() {
  resetAll(true);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener('load', () => {
  initSVG();
  // Focus first hero input
  document.getElementById('hero-inp-start').focus();

  window.addEventListener('resize', () => {
    width = document.getElementById('canvas-wrap').clientWidth;
    height = document.getElementById('canvas-wrap').clientHeight;
    if (simulation) simulation.force('center', d3.forceCenter(width / 2, height / 2));
  });

  // NEW: wire vector-search autocomplete onto the two hero inputs. This is
  // a separate addEventListener (not the existing oninput="heroInputChanged()"
  // attribute already on these elements) so both the original validation
  // logic and the new autocomplete logic run independently, side by side,
  // every time the user types — neither one touches or depends on the other.
  document.getElementById('hero-inp-start')
    .addEventListener('input', () => triggerAutocomplete('start'));
  document.getElementById('hero-inp-end')
    .addEventListener('input', () => triggerAutocomplete('end'));
});

function animatePath(path, steps, hops, displayTexts) {
  const delay = 160;
  path.forEach((nodeId, i) => {
    setTimeout(() => {
      d3.selectAll('.node-circle')
        .filter(d => d.id === nodeId)
        .transition().duration(120)
        .attr('r', d => nodeRadius(d) * 1.7)
        .attr('fill', '#34d399')
        .transition().duration(260)
        .attr('r', d => nodeRadius(d))
        .attr('fill', nodeColor);
    }, i * delay);
  });
  setTimeout(() => openSuccessModal(path, steps, hops, displayTexts), path.length * delay + 480);
}

function openSuccessModal(path, steps, hops, displayTexts) {
  document.getElementById('modal-hops').textContent = hops;
  document.getElementById('modal-steps').textContent = steps;
  const elapsed = _lastElapsedS;
  document.getElementById('modal-elapsed').textContent = elapsed >= 60
    ? Math.floor(elapsed / 60) + 'm ' + (elapsed % 60) + 's'
    : elapsed + 's';

  const container = document.getElementById('modal-path-container');
  container.innerHTML = '';
  path.forEach((p, i) => {
    const btn = document.createElement('a');
    btn.className = 'hop-btn' + (i === 0 ? ' start-hop' : i === path.length - 1 ? ' end-hop' : '');
    btn.href = 'https://en.wikipedia.org/wiki/' + encodeURIComponent(p.replace(/ /g, '_'));
    btn.target = '_blank';
    btn.innerHTML = `
      <span>${p}</span>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="margin-left:4px;opacity:0.7">
        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
        <polyline points="15 3 21 3 21 9"></polyline>
        <line x1="10" y1="14" x2="21" y2="3"></line>
      </svg>`;
    container.appendChild(btn);
    if (i < path.length - 1) {
      const arr = document.createElement('span');
      arr.className = 'modal-arrow';
      arr.textContent = '→';
      const piped = displayTexts && displayTexts[i];
      if (piped) {
        arr.classList.add('piped-edge');
        arr.title = `Shown as "${piped}" in the article text`;
        const note = document.createElement('span');
        note.className = 'modal-piped-note';
        note.textContent = `"${piped}"`;
        arr.appendChild(note);
      }
      container.appendChild(arr);
    }
  });

  document.getElementById('modal-copy-btn').textContent = 'Copy Path';
  document.getElementById('success-modal').classList.add('visible');
}

function closeSuccessModal(e) {
  if (!e || e.target === document.getElementById('success-modal') || e.target.tagName === 'BUTTON') {
    document.getElementById('success-modal').classList.remove('visible');
  }
}

function copyPath() {
  if (!foundPath.length) return;
  const text = foundPath.join(' → ');
  const btn = document.getElementById('modal-copy-btn');
  navigator.clipboard.writeText(text).then(() => {
    if (btn) { btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = 'Copy Path'; }, 1600); }
  }).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    if (btn) { btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = 'Copy Path'; }, 1600); }
  });
}

function openAboutModal() {
  document.getElementById('about-modal').classList.add('visible');
}

function closeAboutModal(e) {
  if (!e || e.target === document.getElementById('about-modal') || e.target.tagName === 'BUTTON') {
    document.getElementById('about-modal').classList.remove('visible');
  }
}

// ════════════════════════════════════════════════════════════════════════════
// Loading screen + landing entrance sequence.
// Polls /api/health until the backend's embedding model is ready (the
// server doesn't accept connections until startup finishes loading it, so
// a single successful response already means "ready" — no extra check
// needed). Once ready: fade out the loading screen and trigger the title's
// 3-stage entrance animation — landing position -> grow to viewport center
// -> hold -> return to landing position. The rest of the hero
// (.hero-reveal elements) and the hero background (#hero-bg-mask) fade in
// together exactly when that animation finishes, driven by the
// animationend event rather than a magic setTimeout that would drift out
// of sync if the CSS timing is ever tuned.
// ════════════════════════════════════════════════════════════════════════════
const HEALTH_POLL_MS = 350;

async function initLoadingSequence() {
  const loadingScreen = document.getElementById('loading-screen');

  while (true) {
    try {
      const res = await fetch(`${AC_BACKEND}/api/health`);
      if (res.ok) break;
    } catch (err) {
      // Backend not reachable yet — keep polling silently.
    }
    await new Promise(r => setTimeout(r, HEALTH_POLL_MS));
  }

  loadingScreen.classList.add('fade-out');
  setTimeout(() => loadingScreen.classList.add('hidden'), 550);

  playHeroEntrance();
}

// Drives the hero title's 3-stage entrance animation. Called ONLY on first
// load (initLoadingSequence(), below) — this is a one-time first-impression
// flourish, not something to replay on every return to the hero screen.
//
// BUGFIX: this used to also run from resetAll() on every Reset/"New Search",
// per its own now-removed claim that doing so kept the experience
// "consistent." It didn't — the function re-opaques the full-screen
// #hero-bg-mask and re-hides every .hero-reveal element (inputs, tagline,
// random button) for the FULL ~4.3s animation duration before its
// `animationend` handler reveals them again. Replaying that on every reset
// meant the entire screen went solid-background with nothing visible or
// interactive for several seconds — reported as "the page going blank" when
// tapping New Search, especially noticeable on mobile. resetAll() now calls
// the lightweight resetHeroDisplay() below instead, which shows everything
// immediately with no replay.
// Each call:
//   1. Re-hides the rest of the hero (.hero-reveal) and re-opaques the
//      background mask (#hero-bg-mask) — necessary since this is the very
//      first paint and both start in their "revealed" CSS state otherwise.
//   2. Measures the title's actual landing position and computes the
//      exact pixel offset needed to land it on the true viewport center
//      (a fixed vh value over/undershoots depending on monitor height —
//      the title's landing position is a fixed-px distance from center,
//      not a viewport-relative one).
//   3. Forces a reflow and re-adds the `entrance` class so the CSS
//      animation restarts cleanly even if it was already played once.
function playHeroEntrance() {
  const heroTitle = document.getElementById('hero-title');
  const mask = document.getElementById('hero-bg-mask');

  document.querySelectorAll('.hero-reveal').forEach(el => el.classList.remove('show'));
  if (mask) mask.classList.remove('hide');

  heroTitle.classList.remove('entrance');
  const rect = heroTitle.getBoundingClientRect();
  const dy = window.innerHeight / 2 - (rect.top + rect.height / 2);
  // The grown stage's transform is `scale(ENTRANCE_SCALE) translateY(...)` —
  // translateY is applied first and then amplified by the scale that wraps
  // it, so the on-screen displacement ends up as dy * ENTRANCE_SCALE, not
  // dy. Pre-dividing here is what makes the title land exactly on center
  // instead of overshooting by 45%. Must match the scale() value in the
  // title-entrance keyframes (styles.css).
  const ENTRANCE_SCALE = 1.45;
  heroTitle.style.setProperty('--center-dy', `${Math.round(dy / ENTRANCE_SCALE)}px`);

  void heroTitle.offsetWidth; // force reflow so the animation restarts from 0%

  heroTitle.classList.add('entrance');
  heroTitle.addEventListener('animationend', () => {
    document.querySelectorAll('.hero-reveal').forEach(el => el.classList.add('show'));
    if (mask) mask.classList.add('hide');
    startPlaceholderRotation();
    // Show onboarding on first visit only
    if (!localStorage.getItem('aurelius_seen_onboarding')) {
      showOnboarding();
    }
  }, { once: true });
}

// Lightweight hero reset for return visits (Reset / New Search) — shows
// everything immediately instead of replaying playHeroEntrance()'s ~4.3s
// title-grow-to-center-and-back sequence with its full-screen mask. See the
// BUGFIX note on playHeroEntrance() above for why that replay was wrong here.
function resetHeroDisplay() {
  const heroTitle = document.getElementById('hero-title');
  const mask = document.getElementById('hero-bg-mask');
  heroTitle.classList.remove('entrance');
  heroTitle.style.opacity = '1';
  heroTitle.style.transform = 'scale(1)';
  if (mask) mask.classList.add('hide');
  document.querySelectorAll('.hero-reveal').forEach(el => el.classList.add('show'));
  startPlaceholderRotation();
}

function showOnboarding() {
  const modal = document.getElementById('onboarding-modal');
  modal.classList.add('visible');
}

function closeOnboarding() {
  const modal = document.getElementById('onboarding-modal');
  modal.classList.remove('visible');
  localStorage.setItem('aurelius_seen_onboarding', 'true');
}

initLoadingSequence();
