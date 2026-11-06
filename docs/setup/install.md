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

Then create the schema, make your account, and start:

```bash
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m biryar.init --email you@example.com
docker compose up -d
```

The second command prints a generated password. **Write it down — it is
shown once** and is not recoverable afterwards. Pass `--password` instead
if you would rather choose one, though that puts it in your shell history.

Biryar is at <http://localhost:8000>, and that account can sign in.

Migrations are a separate step on purpose. If containers ran them at
startup, two starting together would both try, and a schema change that
fails halfway is not something to find out about from a restart loop.

Creating the account is a command rather than a sign-up page because a
sign-up page on a self-hosted install is a door onto the internet whose
only job is to be used once, and whoever reaches it first becomes the
owner. Running it from the shell means the person who gets the account is
the person who can already read the database.

## Connecting Amazon

For a self-hosted install there is **nothing to put in `.env`**. You
register a private application, authorise it against your own account in
the Solution Provider Portal, and paste the refresh token it gives you into
Biryar, which seals it.

The OAuth settings (`BIRYAR_AMAZON_APPLICATION_ID` and the LWA credentials)
are for running Biryar *for other sellers*. Leave them empty: a private
application has no OAuth configuration, so that route answers `MD9100` no
matter what you put there. Biryar says the button is unavailable rather
than offering a flow that cannot finish.

See [Getting your SP-API keys](sp-api.md), which walks the private-
application path with screenshots.

## Seeing real figures before registering anything

You do not need an Amazon application to find out whether this is worth
your time. Sign in, and on the front page drop in a settlement report:
**Reports → Payments → All Statements** in Seller Central, downloaded as
**Flat File V2**.

Biryar reads it and shows you the period's revenue, what Amazon kept, and —
once you have loaded costs — what was actually left. **Nothing is stored.**
The file is read, the figures are shown, and none of it is kept, which also
means it cannot be double-counted against the feed later.

Two things it will tell you that are worth understanding:

- **Whether the file adds up.** The report states the payout Amazon made.
  Biryar adds up every amount in the file and compares. If they disagree it
  says so and tells you not to trust the figures, rather than showing a
  confident number read from a file it misunderstood.
- **Which fees it did not recognise.** Amazon publishes this report's
  column names but not the vocabulary of its fee descriptions, so a seller
  in an unfamiliar category will have some Biryar has never seen. They are
  listed, and they are still counted in the total — never silently dropped.

This is the fastest route to a real number, and it is the only one that
needs nothing from Amazon but a download.

## Getting to a profit figure

Connecting Amazon gets you **gross contribution** — revenue less what Amazon
and the carrier took. That needs nothing from you and appears as soon as the
first fetch finishes.

**Net profit needs one more thing from you: what the units cost.** Amazon
does not know, so Biryar will not guess. Import a cost file — one CSV row
per invoice line — and the figure appears:

```
vendor,doc_number,doc_date,name,qty,unit_price,sku,shipping,prices_include_vat,vat_rate
acme,INV-1001,2026-09-30,Widget Blue,5,8.40,MY-SKU-1,3.95,false,0.19
```

The `sku` column is your own Amazon SKU, and it is the one that matters:
Amazon's financial records name your SKU and nothing your supplier prints,
so a cost filed under an EAN is a correct number that no sale will find. The
import tells you about any row it could not read and any cost it could not
key to a SKU, rather than leaving you to wonder why a figure did not move.

Until every sold unit has a cost, net profit is left blank rather than
counting the unpriced ones as free, and the screen names the SKUs that are
missing one. See [How the fees are measured](../methodology/fees.md) for why
that distinction is the whole point.

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
