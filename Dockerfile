# Aurelius backend — container image.
#
# Works on any always-on container host (Hugging Face Spaces, Fly.io,
# Railway, Google Cloud Run). NOT for serverless/edge platforms (Vercel,
# Netlify, Firebase Functions) — the app holds a ~400 MB ML model in
# memory for its whole lifetime and serves a persistent WebSocket, neither
# of which a cold-starting serverless function can do.

FROM python:3.11-slim

# --- CPU-only PyTorch FIRST ------------------------------------------------
# Installing torch from the default index pulls ~2 GB of NVIDIA CUDA wheels
# that are useless on a CPU host (and OOM/bloat the build). Pinning the CPU
# index gets the ~190 MB CPU build instead. Must come BEFORE requirements so
# sentence-transformers finds torch already satisfied and doesn't re-resolve
# it from the GPU index.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# --- App dependencies ------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Non-root user (Hugging Face Spaces runs containers as UID 1000) -------
# A writable HOME is required so sentence-transformers can download the
# model into ~/.cache/huggingface on first boot. Running as root on Spaces
# makes that cache dir unwritable and the model load fails.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR /home/user/app
COPY --chown=user . .

# Hugging Face Spaces routes to port 7860 by default (app_port in README).
# main.py reads $PORT, so setting it here makes the same image bind 7860 on
# Spaces and whatever $PORT other hosts inject.
ENV PORT=7860
EXPOSE 7860

CMD ["python", "main.py"]
