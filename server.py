"""
Aurelius — FastAPI app: /api/random, /api/health, /ws handler.

Module split per architect plan §1. Owns the FastAPI app instance, CORS
middleware, the startup handler (now delegating model loading to
embedding.load_model() per plan §3), and the WebSocket endpoint that drives
one AStarNavigator session per connection.
"""

import asyncio
import json
import random

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

import embedding
from config import (
    ALLOWED_ORIGINS, MAX_QUERY_LEN,
    MAX_CONCURRENT_SEARCHES, MAX_SEARCH_SECONDS,
)
from search import AStarNavigator
from wiki import _load_seed_titles

# Disable the auto-generated interactive API docs (/docs, /redoc) and the
# OpenAPI schema (/openapi.json) in this public deployment — the frontend
# never uses them, and they only hand an attacker a free map of the API.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# CORS is restricted to the known frontend origins (config.ALLOWED_ORIGINS),
# not "*". This middleware governs the REST endpoints; the WebSocket handshake
# is guarded separately below (Starlette's CORS middleware does not apply to
# WS), so a disallowed website cannot drive either surface from a browser.
# Only GET is used by the REST API — no need to allow write methods.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Defensive response headers for the JSON API (the static frontend gets
    its own, richer set via vercel.json). Cheap and harmless on an API."""
    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp


app.add_middleware(SecurityHeadersMiddleware)

# Bound how many searches run at once. The event loop is single-threaded, so
# a plain counter is race-free; it caps CPU-bound embedding and Wikipedia API
# fan-out from many simultaneous (or maliciously held-open) connections.
_active_searches = 0


@app.on_event("startup")
async def _startup_embed_model():
    await embedding.load_model()


# ══════════════════════════════════════════════════════════════
# REST endpoints (autocomplete lives entirely on the frontend now —
# it calls Wikipedia's opensearch API directly)
# ══════════════════════════════════════════════════════════════

@app.get("/")
async def api_root():
    """
    This backend never serves the frontend (no static files here — see
    CLAUDE.md). Visiting this URL directly is a normal thing for a human
    to try, so return a clear message instead of a bare 404 — the actual
    app is index.html, opened directly or served alongside this on its
    own port.
    """
    return {"status": "Aurelius backend is running.", "frontend": "Open index.html directly in your browser."}


@app.get("/api/random")
async def api_random():
    """
    Powers the "Random" button on the start screen. Picks two distinct
    topics from the curated seed list (common_wiki_searches.txt) — NOT a
    fully random Wikipedia page — so the search always gets a
    well-connected pair of articles to find a path between.
    """
    titles = _load_seed_titles()
    if len(titles) < 2:
        return {"start": "Google", "end": "Mohali"}
    start, end = random.sample(titles, 2)
    return {"start": start, "end": end}


@app.get("/api/health")
async def api_health():
    """
    Polled by the frontend's loading screen on first load. Since FastAPI
    does not accept connections until the startup handler (which loads the
    embedding model) finishes, a successful response here already implies
    the model is ready — there is nothing further to check.
    """
    return {"status": "ready"}


# ══════════════════════════════════════════════════════════════
# WebSocket endpoint
# ══════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global _active_searches

    # Origin guard (CORS middleware does not cover WebSockets). Browsers
    # always send Origin; reject one that isn't allow-listed so another
    # website's JS can't drive this backend from a visitor's browser.
    # Non-browser clients send no Origin and are intentionally allowed —
    # an Origin check only mitigates cross-site browser abuse, not curl.
    origin = ws.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        await ws.close(code=1008)  # policy violation
        return

    await ws.accept()
    navigator: AStarNavigator | None = None
    counted = False
    try:
        raw  = await ws.receive_text()
        data = json.loads(raw)
        if "control" in data:
            return

        start = data.get("start", "").strip()
        end   = data.get("end",   "").strip()
        print(f"\n{'='*60}\n[WS] '{start}' → '{end}'\n{'='*60}")
        if not start or not end:
            await ws.send_text(json.dumps({"event":"error","message":"Need start and end."}))
            return
        # Cap input length: these strings are forwarded to the Wikipedia API
        # and embedded; an oversized payload is only ever abuse.
        if len(start) > MAX_QUERY_LEN or len(end) > MAX_QUERY_LEN:
            await ws.send_text(json.dumps({"event":"error","message":"Article name is too long."}))
            return

        # Concurrency cap — reject rather than pile work onto the free tier.
        if _active_searches >= MAX_CONCURRENT_SEARCHES:
            await ws.send_text(json.dumps(
                {"event":"error","message":"Server is busy right now — please try again in a moment."}))
            return
        _active_searches += 1
        counted = True

        navigator = AStarNavigator(start, end, ws)

        async def listen_controls():
            while True:
                try:
                    msg  = await asyncio.wait_for(ws.receive_text(), timeout=0.5)
                    ctrl = json.loads(msg)
                    if navigator:
                        if ctrl.get("control") == "pause":
                            navigator._paused = True;  print("[WS] Paused")
                        elif ctrl.get("control") == "resume":
                            navigator._paused = False; print("[WS] Resumed")
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    break

        ctrl_task   = asyncio.create_task(listen_controls())
        search_task = asyncio.create_task(navigator.run())
        # Hard wall-clock cap: a client must not be able to hold a search slot
        # open indefinitely (e.g. start a search then sit on {control:"pause"}).
        # If neither task finishes in time, both stay pending and are cancelled.
        done, pending = await asyncio.wait(
            [ctrl_task, search_task],
            timeout=MAX_SEARCH_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

    except WebSocketDisconnect:
        print("[WS] disconnected")
    except Exception as e:
        # Log the real error server-side, but never leak internals (exception
        # text, stack-derived detail) to the client.
        print(f"[WS] {e}")
        try:
            await ws.send_text(json.dumps(
                {"event":"error","message":"Something went wrong on the server."}))
        except Exception:
            pass
    finally:
        if counted:
            _active_searches -= 1
