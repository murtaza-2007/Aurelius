"""
Aurelius — central configuration.

All tunables live here so they can be adjusted (or env-overridden) without
touching the search/embedding/wiki/server logic. See CLAUDE.md and the
architect plan (Production Refactor + Algorithm + UI Pass, §2) for context.
"""

import os
from pathlib import Path

# ── Embedding model ──────────────────────────────────────────────────────
# Default is all-MiniLM-L6-v2: 80 MB / 384-dim, fits free-tier deployment
# (Render free web service) without OOMing or blowing the cold-start budget.
# Qwen/Qwen3-Embedding-0.6B (plan §3) is a better-quality drop-in if you're
# running on a host with more RAM — set AURELIUS_EMBED_MODEL to switch.
EMBED_MODEL_NAME    = os.getenv("AURELIUS_EMBED_MODEL", "all-MiniLM-L6-v2")
EMBED_DEVICE        = os.getenv("AURELIUS_EMBED_DEVICE", "cpu")
EMBED_BATCH_SIZE    = int(os.getenv("AURELIUS_EMBED_BATCH", "64"))   # smaller for 0.6B vs 128 for MiniLM

# ── Wikipedia ────────────────────────────────────────────────────────────
WIKI_API            = "https://en.wikipedia.org/w/api.php"
MAX_FETCH           = 500   # max links fetched from Wikipedia (paginated)
MAX_PAGES           = 6     # safety cap on pagination pages when hunting for a specific goal link
# Wikimedia's robot policy (https://w.wiki/4wJS) 403s requests whose
# User-Agent has no contact info — this was dropped during the module split
# and the bare "Aurelius-WikiNavigator/7.0" string started getting blocked.
WIKI_HEADERS = {
    "User-Agent": "Aurelius-WikiNavigator/7.0 (educational project; contact: murtaza.vali.ug25@plaksha.edu.in)",
    "Accept": "application/json",
}

# ── Search ───────────────────────────────────────────────────────────────
MAX_HOPS            = 60    # enforced as expansion-count cap, not path-length cap (see search.py)
TOP_DISPLAY         = 25    # max neighbours sent to frontend per step

# ── Scoring ──────────────────────────────────────────────────────────────
# Architect plan (Honest Bidirectional Meeting-Check Rewrite): the target's
# backlinks (depth-1) are now the actual goal zone, not just a score hint —
# see search.py's meeting check. Depth-2 frontier, its bonus, and the
# category bonus were removed: none of them could contribute a real,
# verifiable edge, and depth-2 specifically could not be stitched into a
# real path without bridge bookkeeping it never had. The stagnation/
# frontier-jump escape valve was removed entirely — it fabricated a
# came_from pointer with no corresponding Wikipedia link (see the Marcus
# Aurelius -> A* search algorithm incident), which is no longer needed now
# that the meeting check gives the search an honest way to converge.
PRUNE_TOP_K         = 15    # keep this many candidates per expansion after scoring
# NOTE: tried raising this to 0.22 as part of the Semantic Drift fix (see
# search.py docstring) on the theory that richer "{title}. {short_desc}"
# embeddings would make raw cosine trustworthy enough to support a higher
# floor. Verified live against Marcus Aurelius -> A* search algorithm: the
# best of Marcus Aurelius's 500 direct links scores only ~0.186 raw cosine
# to the (also-enriched) target embedding — there is no semantically close
# 1-hop neighbour for a niche CS topic from a Roman-emperor article, full
# stop, regardless of embedding quality. 0.22 killed the search at step 1
# (0/500 candidates survived). Kept at 0.15. The actual drift fix is the
# embedding enrichment improving *relative* ranking among survivors, plus
# DEPTH_TIEBREAK_EPSILON preventing runaway commitment to one irrelevant
# cluster — not an absolute floor, which hard multi-hop cases can't clear
# early on by construction.
PRUNE_FLOOR         = 0.15  # soft floor on raw cosine-to-target; frontier members exempt
PRUNE_MIN_SURVIVORS = 5     # ALWAYS keep at least this many top-ranked candidates per step,
                            # even if they fall below PRUNE_FLOOR — prevents the search from
                            # killing itself at step 1 for semantically distant pairs
                            # (Cleopatra → Time complexity: 0/500 candidates cleared 0.15,
                            # search died immediately)
FRONTIER_D1_BONUS   = 0.30  # candidate is a direct backlink of the target (goal-zone member)

# Depth tie-breaker (Semantic Drift fix): heap priority is h + DEPTH_EPSILON*g
# instead of pure h. This is a deliberate, documented reversal of the prior
# "heap priority = h only, no g-cost" design (see CLAUDE.md) — pure-greedy
# let one noisy high-scoring title (e.g. a proper noun with no domain
# context) drag the search arbitrarily deep into its cluster with nothing
# to prefer a shallower, more recently-improving alternative. 0.01 is small
# enough that it only breaks ties/near-ties; it doesn't override a genuinely
# strong h advantage at any reasonable depth (60 hops -> max 0.6 penalty).
DEPTH_TIEBREAK_EPSILON = 0.01

# ── Active Backward Expansion (goal-zone depth-2) ───────────────────────
GOAL_ZONE_EXPAND_INTERVAL   = 10    # expand goal zone every N steps
GOAL_ZONE_EXPAND_BATCH      = 5     # d1 members to expand per interval
GOAL_ZONE_D2_BACKLINK_LIMIT = 100   # max backlinks fetched per d1 member
FRONTIER_D2_BONUS           = 0.15  # score bonus for d2 goal-zone members (half of d1's 0.30)

# ── Stagnation Detection / Cluster Escape ────────────────────────────────
STAGNATION_WINDOW       = 8     # sliding window size for h-improvement tracking
STAGNATION_DELTA        = 0.02  # min improvement over window to count as progress
PRUNE_TOP_K_STAGNANT    = 25    # widened beam when stagnant (vs 15 normal)
DIVERSITY_WEIGHT        = 0.10  # weight for centroid-distance diversity bonus

# ── Files ────────────────────────────────────────────────────────────────
_THIS_DIR           = Path(__file__).resolve().parent
SEED_FILE           = _THIS_DIR / "common_wiki_searches.txt"
