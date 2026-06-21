"""
Aurelius — Wikipedia API client: resolve, links, backlinks, summary,
categories, and junk/date filters.

Module split per architect plan §1. This is a literal relocation of the
Wikipedia-facing functions previously in main.py (lines 99-103, 190-394,
397-402 in the pre-split file) — no behavioral changes.
"""

import asyncio
import re
from typing import Optional
from urllib.parse import unquote

import httpx

from config import WIKI_API, WIKI_HEADERS, MAX_FETCH, MAX_PAGES, SEED_FILE


def make_wiki_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=WIKI_HEADERS, follow_redirects=True,
                             timeout=httpx.Timeout(25.0))


async def wiki_get(client: httpx.AsyncClient, params: dict, retries: int = 3) -> dict:
    for attempt in range(retries + 1):
        try:
            r = await client.get(WIKI_API, params=params)
            if r.status_code == 429:
                await asyncio.sleep(min(2 ** attempt, 8)); continue
            if r.status_code != 200:
                return {}
            return r.json()
        except httpx.TimeoutException:
            if attempt < retries: await asyncio.sleep(1)
        except Exception as e:
            print(f"[wiki] {e}"); return {}
    return {}


def extract_title_from_url(query: str) -> Optional[str]:
    """If query is a Wikipedia URL, extract and decode the article title; else None."""
    m = re.search(r'wikipedia\.org/wiki/([^#?\s]+)', query.strip())
    if not m:
        return None
    return unquote(m.group(1)).replace('_', ' ').strip()


async def resolve_article_title(client: httpx.AsyncClient, query: str) -> Optional[str]:
    query = query.strip()
    url_t = extract_title_from_url(query)
    if url_t: query = url_t

    data = await wiki_get(client, {"action":"query","titles":query,
                                   "redirects":1,"format":"json","utf8":1})
    for pid, page in data.get("query",{}).get("pages",{}).items():
        if pid != "-1":
            print(f"[resolve] Exact ✓ '{page['title']}'")
            return page["title"]

    data = await wiki_get(client, {"action":"opensearch","search":query,
                                   "limit":10,"namespace":0,"format":"json","utf8":1})
    titles = data[1] if isinstance(data, list) and len(data) > 1 else []
    if titles:
        for t in titles:
            if t.lower() == query.lower(): return t
        return titles[0]

    data = await wiki_get(client, {"action":"query","list":"search","srsearch":query,
                                   "srlimit":5,"srprop":"title","format":"json","utf8":1})
    results = data.get("query",{}).get("search",[])
    if results:
        return results[0]["title"]
    return None


_DATE_RE = re.compile(
    r'^\d{1,2} \w+$|^\w+ \d{1,2}$|^\d{4}$|^\d{4} in '
    r'|^\d{1,2}\w* century'
    r'|^(January|February|March|April|May|June|July|August|'
    r'September|October|November|December)$'
    r'|^\d{4}[-–]\d{2,4}$', re.IGNORECASE
)
def _is_date(t: str) -> bool:
    return bool(_DATE_RE.search(t))

_JUNK_TITLES = frozenset({
    "Wayback Machine", "OCLC", "Digital object identifier", "CrossRef",
    "JSTOR", "PubMed", "PubMed Central", "Semantic Scholar", "ArXiv",
    "Bibcode", "CiteSeerX", "S2CID", "Zbl", "MR", "PMC",
})
def _is_junk(t: str) -> bool:
    """Bibliographic citation artifacts: dead ends with 1-3 links each."""
    return t.endswith("(identifier)") or t in _JUNK_TITLES

async def get_all_links(client: httpx.AsyncClient, title: str,
                        end_title: str = "",
                        target_neighbours: set | None = None
                        ) -> tuple[list[str], dict[str, str]]:
    """
    Fetch ALL links via pagination (up to MAX_FETCH), redirect-resolved to
    canonical titles. Returns (links, short_descriptions).

    Uses generator=links (not prop=links) + redirects=1 so that a link
    written as e.g. "A* algorithm" in the wikitext comes back as its
    canonical target "A* search algorithm" — MediaWiki's `redirects`
    parameter resolves redirects in generator output, not just in the
    `titles` input. Without this, the search graph carries raw,
    possibly-redirect link titles, which silently breaks every canonical-
    title comparison downstream (direct-hit check, the target-frontier
    meeting check, de-duplication) — e.g. a redirect that happens to read
    as semantically close to the target can look like a near-miss "hit"
    when it never actually appears in the target's real backlink set.

    Also requests prop=pageprops with ppprop=wikibase-shortdesc in the SAME
    call (no extra round-trip) — Wikidata's one-line human-curated short
    description (e.g. "Mesopotamian sky-god" for Anu). Bare-title embeddings
    can't distinguish a proper noun's domain, which let semantically
    unrelated but link-adjacent pages (Egyptian/Mesopotamian deities,
    surahs) survive the prune floor on noisy title-only cosine. Embedding
    "{title}. {short_desc}" instead gives the model the actual subject
    matter to score against — see search.py's _rank_candidates.

    Goal link is always included first if found.
    Target neighbours always get priority slots.
    Date/year stubs filtered unless list would be empty.

    BUGFIX: when hunting for end_title, the old exit condition
    (len(all_links) >= MAX_FETCH) stopped pagination after roughly the
    first alphabetical page of a large article, so a goal link sorting
    late in the alphabet (e.g. "Pakistan" on India's 1000+-link page)
    was never found even though it's a direct 1-hop link. We now keep
    paginating past MAX_FETCH while specifically searching for a goal,
    bounded by MAX_PAGES as a safety cap. Calls with no end_title (e.g.
    pre-seeding target neighbours) are unaffected — same stop condition
    as before.
    """
    all_links:  list[str] = []
    date_links: list[str] = []
    goal_link:  str | None = None
    short_desc: dict[str, str] = {}
    end_lower = end_title.lower() if end_title else ""
    params = {
        "action": "query", "generator": "links", "redirects": 1,
        "titles": title, "prop": "info|pageprops", "ppprop": "wikibase-shortdesc",
        "gpllimit": 500, "gplnamespace": 0, "format": "json", "utf8": 1,
    }
    pages_fetched = 0
    while True:
        data = await wiki_get(client, params)
        pages_fetched += 1
        for page in data.get("query", {}).get("pages", {}).values():
            if "missing" in page:
                continue   # link target doesn't exist (red link)
            t = page["title"]
            sd = page.get("pageprops", {}).get("wikibase-shortdesc")
            if sd:
                short_desc[t] = sd
            if end_lower and t.lower() == end_lower:
                goal_link = t; continue
            if _is_junk(t):
                continue
            if _is_date(t):
                date_links.append(t)
            else:
                all_links.append(t)
        cont = data.get("continue", {})
        if "gplcontinue" not in cont:
            break
        if goal_link:
            break   # found it — no need to keep paginating
        if end_lower:
            # Actively hunting a specific goal: keep going past MAX_FETCH,
            # bounded only by MAX_PAGES (safety cap on API calls).
            if pages_fetched >= MAX_PAGES:
                break
        else:
            # No specific goal (e.g. pre-seeding neighbours): original behavior.
            if len(all_links) + len(date_links) >= MAX_FETCH:
                break
        params["gplcontinue"] = cont["gplcontinue"]

    result: list[str] = []
    if goal_link:
        result.append(goal_link)

    tn = target_neighbours or set()
    bridges = [l for l in all_links if l in tn]
    rest    = [l for l in all_links if l not in tn]
    result += bridges
    result += rest
    if len(result) < 5:
        result += date_links[:max(0, 5 - len(result))]

    return result[:MAX_FETCH], short_desc

async def get_backlinks(client: httpx.AsyncClient, title: str,
                        limit: int = MAX_FETCH) -> list[str]:
    """
    Fetch articles that link TO `title` (Wikipedia prop=linkshere) — the
    correct direction for a forward-search bridge set.

    get_all_links() returns what `title` points to. That is NOT useful as
    a "one hop from target" bridge set, because Wikipedia links are not
    reciprocal: target -> A does not imply A -> target. A backlink (B ->
    target) is a guaranteed bridge — if the forward search ever reaches B,
    expanding it is certain to find `target` in B's own outbound links.
    Using outbound links here was sending the search on real-but-useless
    detours (e.g. the "Star Wars" article links to "Star Destroyer", which
    in turn links to real-world "Capital ship"/"Destroyer" articles for
    etymology — those score high on the old outbound-based frontier but
    don't link back to anything Star Wars-related themselves).
    """
    all_links:  list[str] = []
    date_links: list[str] = []
    params = {
        "action": "query", "titles": title, "prop": "linkshere",
        "lhlimit": 500, "lhnamespace": 0, "format": "json", "utf8": 1,
    }
    pages_fetched = 0
    while True:
        data = await wiki_get(client, params)
        pages_fetched += 1
        for page in data.get("query", {}).get("pages", {}).values():
            for link in page.get("linkshere", []):
                t = link["title"]
                if _is_junk(t):
                    continue
                if _is_date(t):
                    date_links.append(t)
                else:
                    all_links.append(t)
        cont = data.get("continue", {})
        if "lhcontinue" not in cont:
            break
        if len(all_links) + len(date_links) >= limit or pages_fetched >= MAX_PAGES:
            break
        params["lhcontinue"] = cont["lhcontinue"]
    return (all_links + date_links)[:limit]

async def check_disambiguation(client: httpx.AsyncClient, title: str) -> bool:
    data = await wiki_get(client, {
        "action": "query", "titles": title,
        "prop": "pageprops", "ppprop": "disambiguation",
        "format": "json", "utf8": 1,
    })
    for page in data.get("query", {}).get("pages", {}).values():
        if "disambiguation" in page.get("pageprops", {}):
            return True
    return False


async def get_link_display_text(client: httpx.AsyncClient, source_title: str,
                                  target_title: str) -> Optional[str]:
    """
    Best-effort lookup of the visible text a wikilink uses on `source_title`
    when it points to `target_title`, e.g. `[[Amber Road|routes]]` -> "routes".

    Exists because get_all_links() (and therefore every edge in the search
    graph) is built from MediaWiki's `links` API, which returns the
    canonical link TARGET, never the piped display text. That's correct for
    graph traversal, but it means a perfectly real edge can look fabricated
    to a human skimming the rendered page for the target's title (e.g.
    "Western world" -> "Amber Road": the link is real, but the article
    displays it as "routes", so Ctrl+F for "Amber Road" finds nothing). See
    CLAUDE.md's "Semantic drift" history entry for the earlier instance of
    this same confusion ('Anu' -> 'Iteration').

    Returns None if the link isn't piped, the display text matches the
    title anyway, or the link couldn't be located in raw wikitext (e.g. it
    comes from a transcluded template) — callers should treat None as
    "nothing extra to show," not an error.
    """
    data = await wiki_get(client, {
        "action": "parse", "page": source_title, "prop": "wikitext",
        "format": "json", "utf8": 1,
    })
    wikitext = data.get("parse", {}).get("wikitext", {}).get("*", "")
    if not wikitext:
        return None

    # Build the pattern from escaped words joined by a flexible separator —
    # escaping the title whole and then substituting spaces for [ _]+
    # doesn't work because re.escape() itself backslash-escapes the space,
    # leaving a stray "\" that turns the substituted "[ _]+" into a literal
    # bracket. Escaping word-by-word and joining avoids that entirely.
    words = re.split(r"[ _]+", target_title)
    target_pattern = r"[ _]+".join(re.escape(w) for w in words)
    regex = re.compile(r"\[\[\s*" + target_pattern + r"\s*(?:\|([^\]]+))?\]\]", re.IGNORECASE)
    match = regex.search(wikitext)
    if not match or not match.group(1):
        return None

    display = re.sub(r"'{2,}", "", match.group(1)).strip()
    if not display or display.lower().replace("_", " ") == target_title.lower().replace("_", " "):
        return None
    return display


async def get_article_summary(client: httpx.AsyncClient, title: str) -> str:
    data = await wiki_get(client, {"action":"query","titles":title,"prop":"extracts",
                                   "exintro":True,"explaintext":True,
                                   "exsentences":2,"format":"json","utf8":1})
    for page in data.get("query",{}).get("pages",{}).values():
        return page.get("extract","")[:220]
    return ""

def _load_seed_titles() -> list[str]:
    """Read the curated topic list used by the Random button, one title per line."""
    if not SEED_FILE.exists():
        return []
    with open(SEED_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]
