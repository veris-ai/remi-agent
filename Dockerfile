# One image recipe for every agent x vendor. Build one image per pair:
#   docker build --platform linux/amd64 --build-arg AGENT=remi --build-arg VENDOR=openai_realtime .
# AGENT is a folder under vamos_agents/ and an extra in pyproject.toml; VENDOR is a folder under
# vamos_voice/providers/ and an extra. Only those two extras and that one agent's code are installed.
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

# Vendor sidecar binaries, fetched only for the vendor that runs one; every other image copies an
# empty folder. livekit: the LiveKit server the provider starts on loopback (pinned + checksummed).
FROM python:3.12.13-slim-bookworm AS sidecars
ARG VENDOR
ARG TARGETARCH
ARG LIVEKIT_SERVER_VERSION=1.13.9
ARG LIVEKIT_SERVER_SHA256_AMD64=0b7fa208b662d09cfdeae8c06cf4c481aead0556086558b48250501e2e2d6e20
ARG LIVEKIT_SERVER_SHA256_ARM64=c5e4e5a6d6cf0a449651aa3178532ecc7a2bf2502538396ebe6e51ff13424056
RUN mkdir -p /sidecars && if [ "$VENDOR" = "livekit" ]; then \
      if [ "$TARGETARCH" = "arm64" ]; then arch=arm64 sha="$LIVEKIT_SERVER_SHA256_ARM64"; \
      else arch=amd64 sha="$LIVEKIT_SERVER_SHA256_AMD64"; fi \
      && python -c "import urllib.request, sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/lk.tgz')" \
           "https://github.com/livekit/livekit/releases/download/v${LIVEKIT_SERVER_VERSION}/livekit_${LIVEKIT_SERVER_VERSION}_linux_${arch}.tar.gz" \
      && echo "$sha  /tmp/lk.tgz" | sha256sum -c - \
      && tar -xzf /tmp/lk.tgz -C /sidecars livekit-server && rm /tmp/lk.tgz; \
    fi

FROM python:3.12.13-slim-bookworm
ARG AGENT
ARG VENDOR
RUN test -n "$AGENT" || (echo "build with --build-arg AGENT=<folder under vamos_agents/, e.g. remi>" >&2 && false)
RUN test -n "$VENDOR" || (echo "build with --build-arg VENDOR=<a folder under vamos_voice/providers/, e.g. openai_realtime>" >&2 && false)
COPY --from=sidecars /sidecars/ /usr/local/bin/

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
COPY vamos_agents/__init__.py ./vamos_agents/__init__.py
COPY vamos_agents/${AGENT} ./vamos_agents/${AGENT}
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
