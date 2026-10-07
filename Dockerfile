# One image recipe for every agent x vendor. Build one image per pair:
#   docker build --platform linux/amd64 --build-arg AGENT=remi --build-arg VENDOR=openai_realtime .
# AGENT is a folder under agents/ and an extra in pyproject.toml; VENDOR is a folder under
# vamos_voice/providers/ and an extra. Only those two extras and that one agent's code are installed.
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

FROM python:3.12.13-slim-bookworm
ARG AGENT
ARG VENDOR
RUN test -n "$AGENT" || (echo "build with --build-arg AGENT=<folder under agents/, e.g. remi>" >&2 && false)
RUN test -n "$VENDOR" || (echo "build with --build-arg VENDOR=<openai_realtime|gemini_live|pipecat>" >&2 && false)

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# The source lives at /agent: the bench's default code_path, where candidate reports read it from.
WORKDIR /agent
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project --extra "$AGENT" --extra "$VENDOR"
COPY vamos_voice ./vamos_voice
COPY agents/__init__.py ./agents/__init__.py
COPY agents/${AGENT} ./agents/${AGENT}
RUN uv sync --locked --no-dev --extra "$AGENT" --extra "$VENDOR" \
    && useradd --system --uid 10001 --no-create-home vamos

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    VAMOS_AGENT=$AGENT \
    VAMOS_VENDOR=$VENDOR \
    PORT=8008
USER vamos
EXPOSE 8008
CMD ["python", "-m", "vamos_voice"]
