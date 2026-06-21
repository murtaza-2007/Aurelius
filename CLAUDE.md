# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
# Start the backend (from WikiNav/)
uvicorn main:app --reload --port 8000

# The frontend is a single static page split across index.html / styles.css /
# app.js — open index.html directly in a browser, or serve it alongside the
# backend (FastAPI does not serve static files here; open file:// or use a
# simple HTTP server on the same port via a proxy).
```

No external services are required at runtime. The embedding model
(`sentence-transformers`, `all-MiniLM-L6-v2`) is loaded once, in-process,
at FastAPI startup — there is no LLM, no Ollama, and no other network
dependency besides the Wikipedia API itself.

## Architecture

Backend is split into focused modules; frontend is markup + CSS + JS in
separate files.

### Backend modules

- **`main.py`** — thin entrypoint only: the Windows stdout/stderr UTF-8
  reconfigure (must run before any import that logs non-ASCII arrows) and
  the uvicorn launch. Reads `$PORT` for PaaS hosts (Render etc.), falls
  back to 8000 locally.
- **`config.py`** — every tunable constant, env-overridable where it
  matters (embedding model/device/batch size). Single source of truth —
  nothing in search/embedding/wiki should hardcode a magic number that
  belongs here.
- **`server.py`** — the FastAPI `app`: CORS, the startup handler
  (`embedding.load_model()`), `GET /api/random`, `GET /api/health`, and the
  `/ws` WebSocket endpoint. One `AStarNavigator` instance per connection. A
  parallel `listen_controls()` coroutine on the same socket handles
  `{control: "pause"}` / `{control: "resume"}` messages while the search
  coroutine runs.
- **`search.py`** — `AStarNavigator` + `SearchStats`: the whole search
  pipeline (scoring, pruning, the meeting check, the regression fence).
- **`wiki.py`** — all Wikipedia API access: `resolve_article_title`,
  `get_all_links`, `get_backlinks`, `get_article_summary`,
  `check_disambiguation`, junk/date filters, seed-list loading for the
  Random button.
- **`embedding.py`** — the `sentence-transformers` model singleton,
  `batch_embed`, `cosine_similarity`, and the per-session caches
  (`_emb_cache`, `_dead_ends`, reset via `reset_session_caches()`).

**WebSocket `/ws`**: the frontend sends `{start, end}` once; the backend
streams JSON events (`status`, `resolved`, `node_add`, `expand_node`,
`node_state`, `neighbours`, `found`, `not_found`) as the search progresses.

### `AStarNavigator` (search.py) — honest bidirectional meeting-check search

Despite the class name it runs **greedy best-first search, not A***. Core
pipeline per expansion:
1. **Goal-zone expansion** (every `GOAL_ZONE_EXPAND_INTERVAL=10` steps):
   expand `GOAL_ZONE_EXPAND_BATCH=5` unexpanded d1 members by fetching
   their backlinks, creating a depth-2 goal zone (`_goal_zone_d2`) with
   bridge bookkeeping (`d2_node → d1_bridge` mapping).
2. Fetch all links from the current article (`get_all_links`, up to
   `MAX_FETCH=500`, redirect-resolved to canonical titles via
   `generator=links&redirects=1`). The same call also returns each linked
   page's Wikidata short description (`pageprops=wikibase-shortdesc`,
   bundled into the same request — no extra round-trip).
3. **Immediate-win check**: if the target is directly in the link set,
   emit `found` right away.
4. **D1 meeting check**: if any neighbour is a member of the target's
   depth-1 backlink set (`_goal_zone_d1`), that neighbour is a verified
   bridge — `current → bridge → target` is a fully real two-edge path.
5. **D2 meeting check**: if any neighbour is in `_goal_zone_d2`, then
   `current → d2_node → d1_bridge → target` is a verified three-edge
   path (d2_node links to d1_bridge by construction, d1_bridge links to
   target by construction). All three edges recorded via `_link()`.
6. **Stagnation detection**: if the best h-value hasn't improved by
   `STAGNATION_DELTA=0.02` over a `STAGNATION_WINDOW=8`-step sliding
   window, the search is stagnant. When stagnant: beam widens from 15 to
   `PRUNE_TOP_K_STAGNANT=25`, and a diversity bonus
   (`DIVERSITY_WEIGHT=0.10 × distance from cluster centroid`) is added to
   each candidate's score to escape dense off-topic clusters.
7. Batch-embed uncached candidates in one in-process `sentence-transformers`
   call (`batch_embed`, run off the event loop via a thread executor).
   Candidates are embedded as `"{title}. {wikidata_short_desc}"`, not a
   bare title — see "Semantic drift" below for why.
8. Score each candidate (`_rank_candidates`):
   `cosine(candidate, target) + frontier_bonus + diversity_bonus`.
9. Prune to the top `PRUNE_TOP_K=15` (or 25 when stagnant), dropping
   anything below the raw-cosine floor `PRUNE_FLOOR=0.15` (goal-zone d1
   and d2 members exempt).
10. Push survivors to the heap.

**Heap priority = `h + DEPTH_TIEBREAK_EPSILON * g`** (config.py,
`DEPTH_TIEBREAK_EPSILON = 0.01`). This is a deliberate reversal of an
earlier "pure-h, no g-cost" design — see "Semantic drift" below. The
penalty is small (max 0.6 at `MAX_HOPS=60`); it only breaks ties/near-ties,
it doesn't override a genuinely strong `h` advantage.

**Disambiguation detection**: before the search begins, the resolved
target is checked for `pageprops.disambiguation`. If it's a disambiguation
page (e.g. "A*" with only 3 backlinks), its outbound links are embedded
and compared against the user's query (with a title-prefix affinity bonus)
to auto-redirect to the most relevant real article (e.g. "A* search
algorithm" with 112 backlinks). Uses `check_disambiguation()` in wiki.py.

**Scoring formula** (`_rank_candidates`, no LLM involved anywhere):
- `cosine(candidate_embedding, target_embedding)` — primary signal, raw
  value stashed for the prune floor
- `+0.30` if the candidate is in `_goal_zone_d1` (`FRONTIER_D1_BONUS`)
- `+0.15` if the candidate is in `_goal_zone_d2` (`FRONTIER_D2_BONUS`)
- `+DIVERSITY_WEIGHT × (1 - cosine(candidate, centroid))` when stagnant

**Target goal zone** (the "bidirectional" part): at search init, the
target's depth-1 backlinks are fetched and stored as `_goal_zone_d1`.
Every `GOAL_ZONE_EXPAND_INTERVAL` steps, `_expand_goal_zone()` fetches
backlinks of unexpanded d1 members, populating `_goal_zone_d2` (a dict
mapping `d2_node → d1_bridge`). This creates a growing catchment area
that makes the meeting check increasingly likely to fire, especially for
targets with few initial backlinks.

**Regression fence**: every `came_from` pointer is recorded through
`_link()`, which also adds the edge to `self._real_edges`. `_emit_found()`
calls `_validate_path()` before ever sending a `found` event, refusing to
emit any path containing an edge that wasn't observed as a real Wikipedia
link. This guards against ever again fabricating a path (see "History"
below).

**Session caches** (`_emb_cache`, `_dead_ends`) are module-level dicts in
`embedding.py`, reset at the start of each search via
`reset_session_caches()`. `search.py` accesses them as
`_embedding_mod._emb_cache` / `_embedding_mod._dead_ends` (module-qualified,
never imported by name) since `reset_session_caches()` rebinds both names
to fresh objects every search — a `from embedding import _emb_cache` would
capture the old object and go stale after the first reset.

**REST endpoints**:
- `GET /api/random` — random pair from `common_wiki_searches.txt`, used by
  the "Random" button
- `GET /api/health` — polled by the frontend's loading screen; a successful
  response implies the embedding model finished loading at startup

Autocomplete has no backend involvement — the frontend calls Wikipedia's
`opensearch` API directly.

### Frontend: `index.html` + `styles.css` + `app.js`

`index.html` is markup only; all CSS lives in `styles.css`, all JS in
`app.js` (single non-module `<script src>`, so handlers referenced by
inline `onclick=` attributes must stay plain function declarations on
`window`, not module exports).

Key `app.js` globals:
- `AC_DEBOUNCE_MS = 80` — autocomplete debounce
- `triggerAutocomplete()` — calls Wikipedia's opensearch API directly (CORS-enabled, no backend round-trip)
- `renderAcDropdown()` — builds the suggestion list; uses inline SVG icons (not emoji)
- `connectWS()` — opens the WebSocket, sends `{start, end}`, dispatches incoming events to render functions
- `playHeroEntrance()` — shared landing-page entrance sequence, called from both the initial load and `resetAll()`
- About modal opened by `openAboutModal()` / closed by `closeAboutModal()`

**Title entrance animation** (`#hero-title.entrance`, `styles.css`): a
single CSS keyframe animation (`@keyframes title-entrance`) takes the title
from its landing position, grows it to viewport center, holds, then
returns it to the landing position as the rest of the hero fades in.
Total duration and the hold length are both encoded in the keyframe
percentages — to change the hold duration, adjust both the animation
duration on `#hero-title.entrance` and the keyframe percentages together
(they're coupled: a percentage is a fraction of the total duration).

**Fonts**: Cinzel (Google Fonts, for "AURELIUS" title and topbar) + Outfit (body) + DM Mono (data/scores). All loaded via `<link>` in `<head>`.

## Key constants (config.py)

| Constant | Value | Purpose |
|---|---|---|
| `MAX_HOPS` | 60 | Expansion-count cap |
| `PRUNE_TOP_K` | 15 | Candidates kept per expansion after scoring |
| `PRUNE_FLOOR` | 0.15 | Soft floor on raw cosine-to-target (top-5 + goal-zone members exempt) |
| `PRUNE_MIN_SURVIVORS` | 5 | Always keep at least this many top-ranked candidates even below floor |
| `FRONTIER_D1_BONUS` | 0.30 | Score bonus for depth-1 goal-zone membership |
| `FRONTIER_D2_BONUS` | 0.15 | Score bonus for depth-2 goal-zone membership |
| `DEPTH_TIEBREAK_EPSILON` | 0.01 | Heap priority = h + this × g (see "Semantic drift" below) |
| `GOAL_ZONE_EXPAND_INTERVAL` | 10 | Expand goal zone every N steps |
| `GOAL_ZONE_EXPAND_BATCH` | 5 | D1 members expanded per interval |
| `GOAL_ZONE_D2_BACKLINK_LIMIT` | 100 | Max backlinks fetched per d1 member |
| `STAGNATION_WINDOW` | 8 | Sliding window for h-improvement tracking |
| `STAGNATION_DELTA` | 0.02 | Min improvement to count as progress |
| `PRUNE_TOP_K_STAGNANT` | 25 | Widened beam when stagnant |
| `DIVERSITY_WEIGHT` | 0.10 | Centroid-distance diversity bonus when stagnant |
| `EMBED_BATCH_SIZE` | 64 | Titles per in-process `model.encode()` batch |
| `MAX_FETCH` | 500 | Max links fetched per article |
| `MAX_PAGES` | 6 | Safety cap on pagination pages when hunting a specific goal link |

## Things to know

- `_is_junk()` / `_JUNK_TITLES` (wiki.py) filter Wikipedia maintenance articles, citation-identifier stubs, disambiguation pages, date articles, and list articles before they enter the heap.
- The embedding model (`all-MiniLM-L6-v2`, 384-dim) is loaded once at startup and reused for every embed call. There is no separate autocomplete model and no HNSW index.
- `WIKI_HEADERS["User-Agent"]` must include contact info or Wikimedia's robot policy 403s the request — see config.py.
- `mainpybackup.txt`, `htmlbackup.txt`, `main.py.bak`, `index.html.bak` are manual snapshots — not used by the app.

## History: incidents worth knowing about

- **Fabricated-path bug (fixed)**: an earlier stagnation-detection +
  frontier-jump escape valve set `came_from` pointers with no real
  Wikipedia link behind them (verified live: the jump target had no real
  link from the node it claimed to jump from). Removed entirely and
  replaced with the meeting-check architecture described above, plus the
  permanent `_real_edges` / `_validate_path` regression fence.
- **Semantic drift ("Anu → Iteration" investigation, fixed)**: a path
  through several Egyptian/Mesopotamian deity pages looked fabricated —
  `Anu`'s rendered page has no visible link reading "Iteration" — but
  verified real against the live API: it's a piped wikilink
  `[[Iteration|iterative]]` in a sentence about deity-name etymology,
  invisible to a manual Ctrl+F but a genuine edge. The actual problem was
  the search wandering through several pages with no real relevance to the
  target before reaching it. Root cause: candidates were embedded as bare
  titles, giving the model no domain signal for short/ambiguous proper
  nouns. Fixed by (1) embedding `"{title}. {wikidata_short_desc}"` instead
  of a bare title (the short description is fetched for free, bundled into
  the existing `get_all_links` request via `pageprops=wikibase-shortdesc`),
  and (2) adding `DEPTH_TIEBREAK_EPSILON` to heap priority so one noisy
  candidate can't drag the search arbitrarily deep into an irrelevant
  cluster with no incentive to prefer a shallower alternative. This is a
  deliberate, intentional reversal of the original "heap priority = h only,
  no g-cost" design — if you're tempted to "simplify" the heap key back to
  pure `h`, don't; that's what caused the drift.
- **"A\*" disambiguation failure (fixed, v3)**: searching for "A*" as a
  target resolved to a Wikipedia disambiguation page with only 3 backlinks,
  making it nearly unreachable (60 steps, NOT_FOUND). The real algorithm
  article "A* search algorithm" has 112 backlinks. Fixed by adding
  disambiguation detection (`check_disambiguation` in wiki.py) + automatic
  redirect to the best semantic match among the disambig page's outbound
  links (with a title-prefix affinity bonus for short queries). Also added
  active backward expansion (depth-2 goal zone with bridge bookkeeping) and
  stagnation-based cluster escape (beam widening + diversity bonus) to
  prevent similar structural failures.
- **Prune-floor total wipe-out (fixed, "Cleopatra → Time complexity"
  incident)**: the search expanded Cleopatra once (step 1), pruned ALL
  500 candidates below `PRUNE_FLOOR=0.15` (raw cosine of ancient-history
  links against a CS target is ~0.05–0.12), the heap went empty, and the
  search reported NOT_FOUND after 1 step. Root cause: `PRUNE_FLOOR` was a
  hard kill switch with no safety net — if *every* candidate from an
  expansion fell below the floor, zero survivors meant immediate death
  regardless of `MAX_HOPS`. The same bug class existed for the earlier
  0.22 threshold (documented in config.py), but at 0.15 it only surfaced
  for maximally distant topic pairs. Fixed by adding
  `PRUNE_MIN_SURVIVORS=5`: the top 5 candidates by combined score always
  survive regardless of the floor, so the search can always make progress.
  The floor still filters positions 6+ in the ranked list, keeping its
  original role of trimming low-quality noise.
