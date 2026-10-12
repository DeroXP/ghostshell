# syntax=docker/dockerfile:1
# Vispora-Cloud — the image Railway builds and runs (railway.json → DOCKERFILE).
#
# Two stages: `builder` installs the locked dependency set into a virtualenv;
# `runtime` carries only that virtualenv and the application source, and runs
# as an unprivileged user.  Nothing in the final image can compile, fetch
# packages, or write outside /tmp.
#
# The base image is pinned by digest — the tag is a comment for humans and for
# Dependabot (docker ecosystem), which bumps tag and digest together.  Both
# FROM lines must stay identical.

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
# --require-hashes: every wheel must match the hash recorded by pip-compile.
# --no-deps: the compiled file already lists the full transitive closure, so
#            pip must not go looking for anything that is not in it.
RUN pip install --require-hashes --no-deps -r requirements.txt


FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH"

# Fixed UID/GID so file ownership is predictable; no home, no shell.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home \
            --home-dir /nonexistent --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app . .

USER app
EXPOSE 8000

# Railway uses railway.json's healthcheckPath; this one is for docker compose.
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=2)" || exit 1

# `sh -c` so Railway's $PORT expands; `exec` makes uvicorn PID 1 so SIGTERM on
# redeploy reaches it and the graceful-shutdown window drains in-flight requests.
#   --proxy-headers / --forwarded-allow-ips='*'  trust X-Forwarded-Proto from
#        Railway's edge (only the edge can reach this port).  The client IP
#        used for rate limiting is resolved separately — see middleware.py.
#   --no-server-header   don't advertise the server software.
#   WEB_CONCURRENCY      uvicorn reads it for --workers (default 1).
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --proxy-headers --forwarded-allow-ips='*' --no-server-header --timeout-keep-alive 15 --timeout-graceful-shutdown 20"]
