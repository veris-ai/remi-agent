# One image recipe for every vendor. Build one image per vendor:
#   docker build --platform linux/amd64 --build-arg VENDOR=openai_realtime -f Dockerfile .
# VENDOR is a folder under vendors/ and an extra in pyproject.toml; only that extra is installed.
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

FROM python:3.12.13-slim-bookworm
ARG VENDOR
RUN test -n "$VENDOR" || (echo "build with --build-arg VENDOR=<openai_realtime|gemini_live|pipecat>" >&2 && false)

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# The source lives at /agent: the bench's default code_path, where candidate reports read it from.
WORKDIR /agent
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project --extra "$VENDOR"
COPY remi_core ./remi_core
COPY vendors ./vendors
RUN uv sync --locked --no-dev --extra "$VENDOR" \
    && useradd --system --uid 10001 --no-create-home remi

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    REMI_VENDOR=$VENDOR \
    PORT=8008
USER remi
EXPOSE 8008
CMD ["python", "-m", "remi_core"]
