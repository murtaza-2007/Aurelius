"""
Aurelius — embedding model singleton, batch_embed, cosine, session caches.

Implements architect plan §3 (embedding swap to Qwen3-Embedding-0.6B) and
part of §1 (module split: model singleton + caches live here, owned by the
module that uses them, per the plan's "Module-level globals ... follow
their owning module" instruction).
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Optional

# Once the model has been downloaded once, sentence-transformers/huggingface_hub
# still spend 1-3s on every startup doing a network round-trip to check for
# updates. These must be set before `sentence_transformers` is imported, and
# only skip the check if the model is already cached locally (first run still
# goes online to fetch it).
#
# Architect plan §3: widen the glob from "models--sentence-transformers--*"
# to "models--*" so this still kicks in for the new Qwen3 model (whose cache
# dir is "models--Qwen--Qwen3-Embedding-0.6B", not "models--sentence-transformers--*").
# Still safe — it only activates when *any* cached model exists locally.
_hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
if _hf_cache.exists() and any(_hf_cache.glob("models--*")):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np
from sentence_transformers import SentenceTransformer

from config import EMBED_MODEL_NAME, EMBED_DEVICE, EMBED_BATCH_SIZE

# ══════════════════════════════════════════════════════════════
# MODEL SINGLETON — loaded once at startup, in-process
# ══════════════════════════════════════════════════════════════

_EMBED_MODEL: Optional[SentenceTransformer] = None


async def load_model():
    """
    Startup handler: loads the embedding model once, in-process.

    device="cpu" skips torch's CUDA-availability probe (driver/nvidia-smi
    calls), which otherwise adds 1-2s on machines without a fast GPU setup.

    No silent fallback to MiniLM on failure (architect plan §3) — if the
    download or load fails, this re-raises so FastAPI refuses connections
    rather than degrading silently.
    """
    global _EMBED_MODEL
    # Render's free tier is a single throttled vCPU — torch's default thread
    # pool spawns one thread per logical core it *thinks* it has, each
    # holding its own memory arena. Capping to 1 avoids wasting RAM on
    # threads that have no extra cores to actually run on anyway.
    import torch
    torch.set_num_threads(1)
    print(f"[Embed] Loading sentence-transformers model '{EMBED_MODEL_NAME}'...")
    t0 = time.time()
    _EMBED_MODEL = SentenceTransformer(EMBED_MODEL_NAME, device=EMBED_DEVICE)
    print(f"[Embed] {EMBED_MODEL_NAME} ready "
          f"(dim={_EMBED_MODEL.get_embedding_dimension()}, {time.time()-t0:.1f}s)")


# ══════════════════════════════════════════════════════════════
# SESSION CACHES  (reset each search)
# ══════════════════════════════════════════════════════════════

_emb_cache: dict[str, np.ndarray] = {}   # title → embedding vector
_dead_ends: set[str]              = set() # articles with 0 links


def reset_session_caches():
    global _emb_cache, _dead_ends
    _emb_cache = {}
    _dead_ends = set()


# ══════════════════════════════════════════════════════════════
# VECTOR MATH
# ══════════════════════════════════════════════════════════════

def cosine_similarity(a, b) -> float:
    if a is None or b is None:
        return 0.0
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.size == 0 or b.size == 0:
        return 0.0
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ══════════════════════════════════════════════════════════════
# EMBEDDING  (in-process sentence-transformers, no network calls)
# ══════════════════════════════════════════════════════════════

async def batch_embed(texts: list[str], stats,
                       embed_texts: list[str] | None = None) -> list[np.ndarray]:
    """
    Embed texts via the in-process sentence-transformers model.
    Results are cached per title across the session. Runs the (CPU-bound,
    synchronous) encode call in a thread executor so it never blocks the
    event loop — keeps pause/resume and other WS traffic responsive.

    `embed_texts`, if given, is what actually gets encoded (parallel to
    `texts`, which remains the cache key). Lets callers embed a richer
    string — e.g. "Anu. Mesopotamian sky-god" — while still caching and
    looking up by the bare title "Anu". Bare-title-only embedding can't
    tell a proper noun's actual subject apart from the target's, which is
    what let semantically unrelated link-adjacent pages (mythology,
    scripture, etc.) survive scoring on noisy title-only cosine alone.

    `stats` is a SearchStats instance (search.py); only its embed_calls
    counter is touched here, so no import of search.py is needed (keeps
    embedding.py free of any dependency on search.py).
    """
    if not texts or _EMBED_MODEL is None:
        return [np.array([]) for _ in texts]
    if embed_texts is None:
        embed_texts = texts

    uncached_idx  = [i for i, t in enumerate(texts) if t not in _emb_cache]
    uncached_text = [embed_texts[i] for i in uncached_idx]
    if uncached_text:
        loop = asyncio.get_running_loop()
        embs = await loop.run_in_executor(
            None,
            lambda: _EMBED_MODEL.encode(
                uncached_text, convert_to_numpy=True,
                batch_size=EMBED_BATCH_SIZE, show_progress_bar=False,
            ),
        )
        stats.embed_calls += 1
        for orig_i, emb in zip(uncached_idx, embs):
            _emb_cache[texts[orig_i]] = emb

    return [_emb_cache.get(t, np.array([])) for t in texts]
