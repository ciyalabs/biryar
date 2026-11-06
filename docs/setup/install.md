---
title: Install
---

# Install

Biryar runs as two processes against one PostgreSQL: an API, which also
serves the interface, and a worker that does the fetching. The compose file
runs all three.

## What you need

Docker, and an Amazon application if you want to connect a seller account.
Nothing else — no Python, no Node, no database to install.

## Run it

```bash
git clone https://github.com/ciyalabs/biryar.git
cd biryar
cp .env.example .env
```

Generate a sealing key and put it in `.env`:

```bash
docker compose run --rm api python -c \
  "from biryar.crypto import Keyring; print('v1:' + Keyring.generate_key())"
```

This key encrypts every Amazon credential Biryar stores. **Keep a copy
somewhere that is not this machine.** Losing it means reconnecting every
account; leaking it means every credential it sealed is compromised.

Then create the schema and start:

```bash
docker compose run --rm api alembic upgrade head
docker compose up -d
```

Biryar is at <http://localhost:8000>.

Migrations are a separate step on purpose. If containers ran them at
startup, two starting together would both try, and a schema change that
fails halfway is not something to find out about from a restart loop.

## Connecting Amazon

Until `BIRYAR_AMAZON_APPLICATION_ID` and the LWA credentials are set, Biryar
runs but no account can be connected, and the connect button says so rather
than failing partway through. See [Getting your SP-API
keys](/setup/sp-api/).

## Upgrading

```bash
git pull
docker compose build
docker compose run --rm api alembic upgrade head
docker compose up -d
```

Every migration has a tested downgrade, so a bad upgrade is recoverable:

```bash
docker compose run --rm api alembic downgrade -1
```

## Rotating the sealing key

Put the new key first and keep the old one:

```
BIRYAR_SECRET_KEY=v2:<new>,v1:<old>
```

Credentials move to the new key as each tenant is next used. Nothing has to
be re-encrypted in one go, and nothing stops working while it happens. Drop
the old key once every credential has moved.
