---
title: Aurelius
emoji: 🧭
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# Aurelius

Aurelius finds the hidden path between any two Wikipedia articles — no LLM,
just on-device embeddings and graph search — and animates the live search as
a growing, force-directed graph.

The frontend is a single static page (`index.html` / `styles.css` / `app.js`)
deployed on Vercel. The backend is this FastAPI service: an in-process
`sentence-transformers` model (`all-MiniLM-L6-v2`) plus a greedy best-first
search over Wikipedia's real link graph, streamed to the browser over a
WebSocket.

> The YAML block at the top of this file is
> [Hugging Face Spaces](https://huggingface.co/docs/hub/spaces-config-reference)
> configuration — it tells Spaces to build the `Dockerfile` and route traffic
> to port 7860. It renders as a small table on GitHub and is otherwise
> harmless there.

## Running locally

```bash
# Backend (from the repo root)
uvicorn main:app --reload --port 8000

# Frontend: open index.html directly in a browser. For local/LAN dev, point
# AC_BACKEND in app.js at your local backend; for production it points at the
# deployed backend URL.
```

## Deploying the backend

This backend needs an **always-on container host** (it holds the model in
memory and serves a persistent WebSocket) — *not* a serverless/edge platform.
The included `Dockerfile` runs as-is on Hugging Face Spaces (Docker SDK),
Fly.io, Railway, or Google Cloud Run.

## License

MIT — see [LICENSE](LICENSE).
