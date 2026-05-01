# SAI control plane — public starter image.
#
# Two-stage build:
#
#   1. `builder`   compiles wheels for our Python deps. Keeps the runtime
#                  layer small by discarding compilation artifacts.
#   2. `runtime`   the actual image. Has Python, our app, and just enough
#                  OS tooling for the container to be useful (curl for
#                  healthchecks, git for `sai-overlay merge` semantics,
#                  bash so the operator can `docker exec -it` in to debug).
#
# What this image does NOT do:
#
#   - Ship Ollama. Use the docker-compose.yml at the repo root, which
#     wires up the official `ollama/ollama` image as a sidecar and
#     points SAI's local_llm tier at `http://ollama:11434`. That image
#     already handles GPU passthrough on Linux + Mac.
#
#   - Ship the operator's private overlay. The container runs the public
#     starter as-is. To run with a private overlay:
#       docker run --rm \
#         -v "/path/to/your/private/SAI:/private:ro" \
#         -v "$HOME/.config/sai:/runtime-config:ro" \
#         -v "$HOME/Library/Application Support/SAI:/state" \
#         -e SAI_PRIVATE=/private \
#         sai:latest
#
#   - Bake operator secrets in. Tokens come at run time via env or
#     mounted files; image stays generic.

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install build-only dependencies. python-slim has neither gcc nor the
# libssl headers some wheels need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the dependency manifest first. This layer caches as long as
# pyproject.toml is unchanged — code changes don't trigger a re-resolve.
COPY pyproject.toml README.md ./
RUN mkdir -p app && touch app/__init__.py
RUN pip install --upgrade pip && pip install --prefix=/install .

# ─── runtime image ─────────────────────────────────────────────────────────

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/install/bin:$PATH \
    PYTHONPATH=/workspace \
    SAI_LOCAL_LLM_HOST=http://ollama:11434

# Runtime-only OS tooling. curl: healthchecks. git: cutover script.
# bash: interactive `docker exec -it` debugging. ca-certificates:
# outbound HTTPS to Slack/OpenAI/Gmail.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# Bring the resolved deps over from the builder stage.
COPY --from=builder /install /install

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY app ./app
COPY prompts ./prompts
COPY policies ./policies
COPY workflows ./workflows
COPY registry ./registry
COPY docs ./docs
COPY scripts ./scripts
COPY tests ./tests

# Install the project itself so console scripts (sai-api, sai-overlay,
# sai-verify) end up on PATH. Reuses the wheels from the builder layer.
RUN pip install --no-deps --prefix=/install .

# Default state/log/config paths inside the container. Overridable via
# env at runtime; production usage typically mounts host volumes here.
ENV SAI_STATE_DIR=/state \
    SAI_LOGS_DIR=/logs \
    SAI_TOKENS_DIR=/tokens \
    SAI_RUNTIME_DIR=/sai-runtime

VOLUME ["/state", "/logs", "/tokens", "/sai-runtime"]

EXPOSE 8000

# Liveness check. The control-plane FastAPI exposes a / route that
# returns the dashboard; a 200 here is enough to call the container
# healthy. The compose file uses this for ollama-first ordering.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/ >/dev/null || exit 1

# Default command: serve the control plane. Override on the docker run
# command line to invoke other entrypoints (e.g. `python -m
# scripts.<one-shot>`).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
