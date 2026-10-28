FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
ENV LANGGRAPH_STRICT_MSGPACK=true
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PYTHON=/usr/local/bin/python UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv UV_CACHE_DIR=/app/.cache/uv TMPDIR=/workspace/.cache/insightpilot-tmp XDG_CACHE_HOME=/workspace/.cache/insightpilot TIKTOKEN_CACHE_DIR=/workspace/.cache/insightpilot/tiktoken
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.16 && groupadd --gid 10001 insightpilot && useradd --uid 10001 --gid 10001 --no-create-home insightpilot
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY migrations /app/migrations
COPY fixtures /app/fixtures
COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev --no-editable && mkdir -p /workspace/.data/insightpilot /workspace/.cache/insightpilot-tmp /workspace/.cache/insightpilot/tiktoken && chown -R 10001:10001 /workspace
USER 10001:10001
CMD ["/app/.venv/bin/python","-m","insightpilot.cli","api","--workspace-root","/workspace","--host","0.0.0.0"]
