# One image, two things to run: the API and the worker.
#
# They share everything except the command, so shipping them as two images
# would mean two builds that must stay in step and a way to tell when they
# are not. The compose file runs the same image twice.
#
# The front end is built here rather than committed. A dist/ in version
# control is a build output that someone will eventually forget to
# regenerate, and the symptom is a UI that silently lags the API it talks to.

# -- the front end -----------------------------------------------------------
FROM node:22.23-slim AS web

WORKDIR /web

# Dependencies first, so a change to the application does not re-resolve
# them. npm ci rather than install: it uses the lockfile exactly and fails
# if package.json disagrees, so an image cannot be built from a dependency
# tree that was never tested.
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/ ./
RUN npm run build


# -- the application ---------------------------------------------------------
FROM python:3.12-slim AS app

# Dependencies are resolved against the lockfile by uv, which is both faster
# than pip here and the same tool the project is developed with.
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN uv venv /opt/venv

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --python /opt/venv/bin/python --no-cache .

COPY alembic.ini ./
COPY scripts/ ./scripts/
COPY --from=web /web/dist /app/web/dist

# Not root. A process that never needs to write outside its own temp space
# has no reason to be able to.
RUN useradd --create-home --uid 10001 biryar \
 && chown -R biryar:biryar /app
USER biryar

ENV BIRYAR_STATIC_DIR=/app/web/dist

EXPOSE 8000

# The API by default. The worker is the same image with:
#     docker run ... python -m biryar.worker
#
# No migrations on startup: two containers starting together would both run
# them, and a schema change that goes wrong halfway is not something to
# discover from a crash loop. `alembic upgrade head` is a deliberate step.
CMD ["uvicorn", "--factory", "biryar.api.app:create_app", \
     "--host", "0.0.0.0", "--port", "8000"]
