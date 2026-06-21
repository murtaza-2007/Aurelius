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

import embedding
from search import AStarNavigator
from wiki import _load_seed_titles

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
    await ws.accept()
    navigator: AStarNavigator | None = None
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
        done, pending = await asyncio.wait(
            [ctrl_task, search_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()

    except WebSocketDisconnect:
        print("[WS] disconnected")
    except Exception as e:
        print(f"[WS] {e}")
        try:
            await ws.send_text(json.dumps({"event":"error","message":str(e)}))
        except Exception:
            pass
