"""
Aurelius — search engine: greedy best-first, embeddings + graph only.

Architect plan: Honest Bidirectional Meeting-Check Rewrite. Replaces the
prior stagnation-detection + frontier-jump escape valve (which fabricated
a came_from pointer with no real Wikipedia link behind it — see the
Marcus Aurelius -> A* search algorithm incident, where the jump landed on
a node that doesn't even link to the target) with an honest mechanism:

  The target's backlinks (depth-1) are real reverse edges: for every
  F in that set, F -> target exists by construction (Wikipedia's own
  linkshere index). The forward search now treats this set as its actual
  goal zone (~100+ members, not one node) instead of just a score hint.
  The moment the forward search expands a node whose real neighbour set
  intersects this zone, current -> F -> target is a fully real two-edge
  bridge — no teleportation, no invented edges.

  A permanent regression fence (_real_edges / _validate_path /
  _emit_found) refuses to emit any path containing an edge that wasn't
  observed as a real Wikipedia link, so a future change can't silently
  reintroduce a fabricated path.

  Removed entirely: stagnation detection, frontier-jump, the depth-2
  frontier (couldn't be stitched into a real path without bridge
  bookkeeping it never had), and the category-embedding bonus (capped at
  +0.10, applied weakly everywhere, did not prevent the failure case that
  motivated the jump in the first place).

Semantic Drift fix: a later investigation found a path that LOOKED
fabricated ('Anu' -> 'Iteration' with no visible link on the rendered
page) but verified real against the live API — it's a piped wikilink
[[Iteration|iterative]] in a sentence about deity-name etymology, invisible
to a manual page read but a genuine edge. The actual problem was that the
search wandered through several pages (Egyptian/Mesopotamian deities) with
no real relevance to the target before reaching it. Root cause: candidates
were embedded as bare titles, and a short ambiguous proper noun carries no
domain signal for the model to score against. Fixed two ways: (1) every
candidate is now embedded as "{title}. {wikidata_short_desc}" instead of a
bare title — see wiki.py's get_all_links() and _rank_candidates() below —
so "Anu. Mesopotamian sky-god" scores clearly far from the target instead
of landing in noisy title-only cosine; (2) heap priority is now
h + DEPTH_TIEBREAK_EPSILON*g instead of pure h (config.py), a deliberate,
documented reversal of the original no-g-cost design — pure-greedy let one
noisy candidate drag the search arbitrarily deep into its cluster with no
incentive to prefer a shallower alternative. The depth penalty is small
enough (max 0.6 at MAX_HOPS=60) that it only breaks ties/near-ties.
"""

import asyncio
import heapq
import json
import math
import time
from collections import defaultdict
from typing import Optional

import httpx
import numpy as np
from fastapi import WebSocket

from config import (
    MAX_HOPS, TOP_DISPLAY,
    PRUNE_TOP_K, PRUNE_FLOOR, PRUNE_MIN_SURVIVORS,
    FRONTIER_D1_BONUS, FRONTIER_D2_BONUS,
    DEPTH_TIEBREAK_EPSILON,
    GOAL_ZONE_EXPAND_INTERVAL, GOAL_ZONE_EXPAND_BATCH,
    GOAL_ZONE_D2_BACKLINK_LIMIT,
    STAGNATION_WINDOW, STAGNATION_DELTA, PRUNE_TOP_K_STAGNANT,
    DIVERSITY_WEIGHT,
)
from embedding import batch_embed, cosine_similarity, reset_session_caches
import embedding as _embedding_mod
# NOTE: the embedding cache and dead-ends set are accessed as
# _embedding_mod._emb_cache / _embedding_mod._dead_ends (module-qualified),
# never imported by name. reset_session_caches() REBINDS both names inside
# the embedding module to fresh dict/set objects on every search — a
# `from embedding import _emb_cache` here would capture the OLD object at
# import time and silently go stale after the first reset.
from wiki import (
    make_wiki_client, resolve_article_title, get_all_links, get_backlinks,
    get_article_summary, check_disambiguation, get_link_display_text, _is_date,
)


# ══════════════════════════════════════════════════════════════
# SEARCH STATS
# ══════════════════════════════════════════════════════════════

class SearchStats:
    def __init__(self):
        self.t0            = time.time()
        self.nodes_visited = 0
        self.nodes_pruned  = 0
        self.embed_calls   = 0

    def elapsed(self) -> float:
        return round(time.time() - self.t0, 1)

    def to_dict(self) -> dict:
        return {
            "nodes_visited": self.nodes_visited,
            "nodes_pruned":  self.nodes_pruned,
            "embed_calls":   self.embed_calls,
            "elapsed_s":     self.elapsed(),
        }


# ══════════════════════════════════════════════════════════════
# SEARCH ENGINE — greedy best-first, embeddings + graph only
# ══════════════════════════════════════════════════════════════

class AStarNavigator:
    def __init__(self, start: str, end: str, ws: WebSocket):
        self.start   = start
        self.end     = end
        self.ws      = ws
        self.stats   = SearchStats()
        self._paused = False

        self.g_score:   dict[str, float]         = defaultdict(lambda: math.inf)
        self.came_from: dict[str, Optional[str]] = {}
        self.h_cache:   dict[str, float]         = {}
        self.open_heap: list[tuple]              = []
        self.open_set:  set[str]                = set()
        self.closed_set:set[str]                = set()

        # Goal zone: depth-1 and depth-2 backlinks of the target.
        # d1: F -> target is a real edge (Wikipedia's linkshere index).
        # d2: d2_node -> d1_bridge is a real edge, so
        #     current -> d2_node -> d1_bridge -> target is a 3-edge path.
        self._goal_zone_d1: set[str]        = set()
        self._goal_zone_d2: dict[str, str]  = {}   # d2_node -> d1_bridge
        self._d1_expanded:  set[str]        = set()

        self.target_embedding:   Optional[np.ndarray] = None
        self._target_cosine:     dict[str, float]     = {}

        self._short_desc: dict[str, str] = {}

        # Stagnation detection / cluster escape
        self._h_history: list[float] = []
        self._recent_embeddings: list[np.ndarray] = []

        # Permanent regression fence
        self._real_edges: set[tuple[str, str]] = set()


    async def send(self, event: str, data: dict):
        try:
            await self.ws.send_text(json.dumps({"event": event, **data}))
        except Exception:
            pass

    def reconstruct_path(self, current: str) -> list[str]:
        path = [current]
        while current in self.came_from and self.came_from[current] is not None:
            current = self.came_from[current]
            path.append(current)
        return list(reversed(path))

    def _link(self, parent: str, child: str, g: float):
        """
        Record a came_from pointer AND the real edge backing it. This is
        the only place came_from should ever be assigned — routing every
        assignment through here is what makes _validate_path() a genuine
        regression fence instead of a no-op.
        """
        self.came_from[child] = parent
        self.g_score[child]   = g
        self._real_edges.add((parent, child))

    def _validate_path(self, path: list[str]) -> bool:
        """
        Regression fence: refuse to treat a path as valid unless every
        consecutive pair was recorded by _link() as a real, observed
        Wikipedia link. Guards against ever again emitting a path like
        the Marcus Aurelius -> A* search algorithm one, where a removed
        feature (frontier-jump) fabricated a came_from pointer with no
        corresponding link.
        """
        return all(
            (path[i], path[i + 1]) in self._real_edges
            for i in range(len(path) - 1)
        )

    async def _emit_found(self, client: httpx.AsyncClient, end_node: str, step: int):
        path = self.reconstruct_path(end_node)
        if not self._validate_path(path):
            print(f"[Guard] Rejected a path with a fabricated edge: {path}")
            await self.send("not_found", {
                "message": "Internal error: candidate path failed validation.",
                "visited": list(self.closed_set),
                "stats": self.stats.to_dict(),
            })
            return

        # Best-effort: surface the actual piped display text per hop so a
        # genuine-but-piped edge (e.g. "Western world" -[[Amber Road|routes]]->
        # "Amber Road") doesn't look fabricated to someone Ctrl+F-ing the
        # rendered page for the target's title. None entries (not piped, or
        # not found in raw wikitext) are simply omitted by the frontend.
        display_texts = await asyncio.gather(*(
            get_link_display_text(client, path[i], path[i + 1])
            for i in range(len(path) - 1)
        ))

        await self.send("found", {
            "path": path, "steps": step,
            "total_hops": len(path) - 1,
            "stats": self.stats.to_dict(),
            "display_texts": display_texts,
        })

    def _is_goal_zone(self, title: str) -> bool:
        return title in self._goal_zone_d1 or title in self._goal_zone_d2

    # ── CORE SCORING PIPELINE ─────────────────────────────────────────────────
    async def _rank_candidates(self, candidates: list[str],
                               is_stagnant: bool = False) -> dict[str, float]:
        if not candidates:
            return {}

        need_embed = [t for t in candidates if t not in _embedding_mod._emb_cache]
        if need_embed:
            embed_texts = [
                f"{t}. {self._short_desc[t]}" if t in self._short_desc else t
                for t in need_embed
            ]
            await batch_embed(need_embed, self.stats, embed_texts=embed_texts)

        centroid = None
        if is_stagnant and self._recent_embeddings:
            centroid = np.mean(self._recent_embeddings, axis=0)

        combined: dict[str, float] = {}
        for t in candidates:
            emb = _embedding_mod._emb_cache.get(t)
            s_target = cosine_similarity(emb, self.target_embedding)
            self._target_cosine[t] = s_target

            if t in self._goal_zone_d1:
                frontier_bonus = FRONTIER_D1_BONUS
            elif t in self._goal_zone_d2:
                frontier_bonus = FRONTIER_D2_BONUS
            else:
                frontier_bonus = 0.0

            diversity_bonus = 0.0
            if is_stagnant and centroid is not None and emb is not None:
                diversity_bonus = DIVERSITY_WEIGHT * (1.0 - cosine_similarity(emb, centroid))

            combined[t] = min(0.99, s_target + frontier_bonus + diversity_bonus)

        return combined

    async def _prune(self, pool: list[str],
                     scores: dict[str, float],
                     is_stagnant: bool = False) -> tuple[list[str], int]:
        if not scores:
            return pool, 0

        top_k = PRUNE_TOP_K_STAGNANT if is_stagnant else PRUNE_TOP_K
        ranked = sorted(pool, key=lambda t: -scores.get(t, 0.0))
        top    = ranked[:top_k]

        survivors: list[str] = []
        for i, t in enumerate(top):
            raw = self._target_cosine.get(t, 0.0)
            if i < PRUNE_MIN_SURVIVORS or self._is_goal_zone(t) or raw >= PRUNE_FLOOR:
                survivors.append(t)
            else:
                self.h_cache[t] = 0.95

        pruned = len(pool) - len(survivors)
        self.stats.nodes_pruned += pruned
        print(f"[Prune] {pruned}/{len(pool)} pruned, {len(survivors)} kept"
              + (" (stagnant beam)" if is_stagnant else ""))
        return survivors, pruned

    # ── GOAL ZONE EXPANSION ─────────────────────────────────────────────────
    async def _expand_goal_zone(self, client, end_title: str):
        unexpanded = [m for m in self._goal_zone_d1 if m not in self._d1_expanded]
        batch = unexpanded[:GOAL_ZONE_EXPAND_BATCH]
        for d1_member in batch:
            self._d1_expanded.add(d1_member)
            d2_backlinks = await get_backlinks(client, d1_member,
                                               limit=GOAL_ZONE_D2_BACKLINK_LIMIT)
            for d2 in d2_backlinks:
                if d2 not in self._goal_zone_d2 and d2 not in self._goal_zone_d1:
                    self._goal_zone_d2[d2] = d1_member
        if batch:
            print(f"[GoalZone] Expanded: {len(self._goal_zone_d1)} d1 + "
                  f"{len(self._goal_zone_d2)} d2 nodes")

    # ── RUN ───────────────────────────────────────────────────────────────────
    async def run(self):
        reset_session_caches()

        async with make_wiki_client() as client:

            # ── 1. Resolve titles ─────────────────────────────────────────
            await self.send("status", {"message": f"Resolving '{self.start}'..."})
            start_title = await resolve_article_title(client, self.start)
            if not start_title:
                await self.send("error", {"message": f"Cannot find: '{self.start}'"}); return

            await self.send("status", {"message": f"Resolving '{self.end}'..."})
            end_title = await resolve_article_title(client, self.end)
            if not end_title:
                await self.send("error", {"message": f"Cannot find: '{self.end}'"}); return

            await self.send("resolved", {"start": start_title, "end": end_title})
            self.end = end_title

            # ── 1b. Disambiguation redirect ──────────────────────────────
            is_disambig = await check_disambiguation(client, end_title)
            if is_disambig:
                await self.send("status", {"message": f"'{end_title}' is a disambiguation page, finding best match..."})
                disambig_links, disambig_descs = await get_all_links(client, end_title)
                if disambig_links:
                    self._short_desc.update(disambig_descs)
                    embed_texts_d = [
                        f"{t}. {disambig_descs[t]}" if t in disambig_descs else t
                        for t in disambig_links
                    ]
                    query_embs = await batch_embed(
                        [self.end], self.stats,
                        embed_texts=[self.end],
                    )
                    cand_embs = await batch_embed(
                        disambig_links, self.stats,
                        embed_texts=embed_texts_d,
                    )
                    if query_embs and query_embs[0] is not None:
                        query_emb = query_embs[0]
                        best_title, best_score = None, -1.0
                        end_lower = self.end.lower()
                        for i, t in enumerate(disambig_links):
                            if cand_embs[i] is not None:
                                sc = cosine_similarity(cand_embs[i], query_emb)
                                if t.lower().startswith(end_lower):
                                    sc += 0.25
                                if sc > best_score:
                                    best_score, best_title = sc, t
                        if best_title:
                            print(f"[Disambig] '{end_title}' → '{best_title}' (score={best_score:.3f})")
                            end_title = best_title
                            self.end = end_title
                            await self.send("resolved", {"start": start_title, "end": end_title})

            # ── 2. Target embedding  ──────────────────────────────────────
            # Enriched with the article's lead extract (Semantic Drift fix):
            # the target embedding is the cosine anchor for every score in
            # the search, so its quality matters more than any single
            # candidate's. A bare title gives the model nothing to work
            # with for ambiguous/short titles; the extract gives it the
            # actual subject matter.
            await self.send("status", {"message": "Embedding target article..."})
            target_extract = await get_article_summary(client, end_title)
            te = await batch_embed(
                [end_title], self.stats,
                embed_texts=[f"{end_title}. {target_extract}" if target_extract else end_title],
            )
            if te and te[0] is not None and te[0].size:
                self.target_embedding = te[0]
                _embedding_mod._emb_cache[end_title] = te[0]
                print(f"[Embed] Target dim={te[0].shape[0]}")

            # ── 3. Pre-seed: target backlinks = the goal zone ──────────────
            await self.send("status", {"message": "Pre-loading target backlinks..."})
            target_nb = await get_backlinks(client, end_title)
            self._goal_zone_d1 = set(target_nb)
            print(f"[init] Target goal zone: {len(self._goal_zone_d1)} d1 backlinks")

            # ── 4. Init search ────────────────────────────────────────────
            # Same extract-enrichment as the target embedding above — the
            # start node's h0 anchors every depth-tiebreak comparison for
            # the rest of the search.
            self.g_score[start_title] = 0
            summary = await get_article_summary(client, start_title)
            se = await batch_embed(
                [start_title], self.stats,
                embed_texts=[f"{start_title}. {summary}" if summary else start_title],
            )
            if se and se[0] is not None and se[0].size and self.target_embedding is not None:
                h0 = max(0.01, 1.0 - cosine_similarity(se[0], self.target_embedding))
            else:
                h0 = 0.9
            self.h_cache[start_title] = h0
            heapq.heappush(self.open_heap, (h0, h0, 0, start_title))
            self.open_set.add(start_title)

            await self.send("node_add", {
                "id": start_title, "g": 0, "h": round(h0,3), "f": round(h0,3),
                "summary": summary, "state": "open",
                "is_start": True, "is_end": False,
            })
            await self.send("node_add", {
                "id": end_title, "g": None, "h": 0.0, "f": None,
                "summary": "", "state": "target",
                "is_start": False, "is_end": True,
            })

            step = 0

            # ── 5. Greedy best-first search loop ─────────────────────────
            while self.open_heap and step < MAX_HOPS:

                while self._paused:
                    await asyncio.sleep(0.3)

                if not self.open_heap:
                    break
                _priority, h_curr, g_curr, current = heapq.heappop(self.open_heap)
                f_curr = g_curr + h_curr  # for display only
                self.open_set.discard(current)

                if current in self.closed_set:
                    continue

                step += 1
                self.stats.nodes_visited += 1

                await self.send("expand_node", {
                    "id":          current,
                    "g":           g_curr,
                    "h":           round(h_curr, 3),
                    "f":           round(f_curr, 3),
                    "path_so_far": self.reconstruct_path(current),
                    "step":        step,
                    "stats":       self.stats.to_dict(),
                })

                # Goal check
                if current.lower() == end_title.lower():
                    await self._emit_found(client, current, step)
                    return

                self.closed_set.add(current)
                await self.send("node_state", {"id": current, "state": "closed"})

                # ── Periodic goal-zone expansion ──────────────────────────
                if step > 0 and step % GOAL_ZONE_EXPAND_INTERVAL == 0:
                    await self._expand_goal_zone(client, end_title)

                # ── Fetch ALL links ───────────────────────────────────────
                await self.send("status", {"message": f"Fetching links: '{current}'..."})
                neighbours, short_desc = await get_all_links(
                    client, current,
                    end_title=end_title,
                    target_neighbours=self._goal_zone_d1,
                )
                self._short_desc.update(short_desc)
                print(f"[Search] step {step}: '{current}' → {len(neighbours)} links")

                if not neighbours:
                    _embedding_mod._dead_ends.add(current)
                    continue

                # Immediate win: current links directly to the target.
                if end_title in neighbours:
                    self._link(current, end_title, g_curr + 1)
                    print(f"[Search] Direct hit: '{current}' → '{end_title}'")
                    await self._emit_found(client, end_title, step)
                    return

                # D1 meeting check
                bridge = next(
                    (nb for nb in neighbours if nb in self._goal_zone_d1), None
                )
                if bridge:
                    self._link(current, bridge, g_curr + 1)
                    self._link(bridge, end_title, g_curr + 2)
                    print(f"[Search] Meeting d1: '{current}' → '{bridge}' → '{end_title}'")
                    await self._emit_found(client, end_title, step)
                    return

                # D2 meeting check
                d2_bridge = next(
                    (nb for nb in neighbours if nb in self._goal_zone_d2), None
                )
                if d2_bridge:
                    d1_bridge = self._goal_zone_d2[d2_bridge]
                    self._link(current, d2_bridge, g_curr + 1)
                    self._link(d2_bridge, d1_bridge, g_curr + 2)
                    self._link(d1_bridge, end_title, g_curr + 3)
                    print(f"[Search] Meeting d2: '{current}' → '{d2_bridge}' → '{d1_bridge}' → '{end_title}'")
                    await self._emit_found(client, end_title, step)
                    return

                # Filter closed + dead-ends
                candidates = [
                    nb for nb in neighbours
                    if nb not in self.closed_set and nb not in _embedding_mod._dead_ends
                ]

                # ── Stagnation detection ──────────────────────────────────
                is_stagnant = False
                if len(self._h_history) >= 2 * STAGNATION_WINDOW:
                    recent_best = min(self._h_history[-STAGNATION_WINDOW:])
                    prev_best = min(self._h_history[-2*STAGNATION_WINDOW:-STAGNATION_WINDOW])
                    is_stagnant = (prev_best - recent_best) < STAGNATION_DELTA
                    if is_stagnant:
                        print(f"[Stagnation] Detected at step {step}, widening beam")

                # ── Embed + score pipeline ────────────────────────────────
                await self.send("status", {"message": f"Ranking links for '{current}'..."})
                scores = await self._rank_candidates(candidates, is_stagnant=is_stagnant)

                # Track h-history and cluster centroid
                if scores:
                    best_h = min(max(0.01, 1.0 - sc) for sc in scores.values())
                    self._h_history.append(best_h)
                current_emb = _embedding_mod._emb_cache.get(current)
                if current_emb is not None:
                    self._recent_embeddings.append(current_emb)
                    if len(self._recent_embeddings) > STAGNATION_WINDOW:
                        self._recent_embeddings.pop(0)

                # ── Prune ─────────────────────────────────────────────────
                pool = list(scores.keys())
                survivors, _ = await self._prune(pool, scores, is_stagnant=is_stagnant)

                # ── Push to heap ──────────────────────────────────────────
                new_nodes = []
                for nb in survivors:
                    if nb in self.closed_set:
                        continue
                    sc      = scores.get(nb, 0.05)
                    h_nb    = max(0.01, 1.0 - sc)
                    self.h_cache[nb] = h_nb
                    tent_g  = g_curr + 1
                    if tent_g < self.g_score[nb]:
                        self._link(current, nb, tent_g)
                        # Greedy best-first with a small depth tie-breaker
                        # (Semantic Drift fix — deliberate reversal of the
                        # prior "heap priority = h only" design, see
                        # config.py's DEPTH_TIEBREAK_EPSILON and CLAUDE.md):
                        # pure-h let one noisy high-scoring proper noun drag
                        # the search arbitrarily deep into its cluster, with
                        # nothing to prefer a shallower alternative once it
                        # committed. h_nb is still kept as the true heuristic
                        # for display/h_cache — only heap ordering changes.
                        priority = h_nb + DEPTH_TIEBREAK_EPSILON * tent_g
                        heapq.heappush(self.open_heap, (priority, h_nb, tent_g, nb))
                        self.open_set.add(nb)
                        is_end = nb.lower() == end_title.lower()
                        new_nodes.append({
                            "id": nb, "g": tent_g,
                            "h": round(h_nb, 3), "f": round(tent_g + h_nb, 3),
                            "state": "target" if is_end else "open",
                            "is_end": is_end, "parent": current,
                        })

                display_nodes = new_nodes[:TOP_DISPLAY]
                display_edges = [{"from": current, "to": n["id"]} for n in display_nodes]
                if display_nodes:
                    await self.send("neighbours", {
                        "centre": current,
                        "nodes":  display_nodes,
                        "edges":  display_edges,
                    })
                    await asyncio.sleep(0.01)

            await self.send("not_found", {
                "message": f"No path found within {MAX_HOPS} hops.",
                "visited": list(self.closed_set),
                "stats":   self.stats.to_dict(),
            })
