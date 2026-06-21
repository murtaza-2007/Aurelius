"""
Aurelius — Wikipedia Rabbit-Hole Navigator Backend
Architecture: embeddings + graph search only. No LLM, no external services.

Thin entrypoint per architect plan §1 — everything else lives in
config.py, embedding.py, wiki.py, search.py, server.py. This file keeps
only the Windows stdout encoding fix (must run before any import that
might log non-ASCII, e.g. the arrow characters in search.py's log lines)
and the uvicorn launch.
"""

import sys

# Windows redirects stdout to cp1252 when it's not an interactive console
# (e.g. piped to a log file), which raises on the arrow/emoji characters
# used in log lines below and would otherwise crash mid-search.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import os  # noqa: E402

from server import app  # noqa: E402 (must follow the stdout fix above)

if __name__ == "__main__":
    import uvicorn
    # Render (and most PaaS hosts) inject a dynamic $PORT and require the
    # app to bind to it; 8000 is just the local-dev fallback.
    port = int(os.getenv("PORT", "8000"))
    print(f"\n🌐 Aurelius backend → http://localhost:{port}")
    print("   Open index.html in your browser\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
