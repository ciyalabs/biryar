# Install

**Status: placeholder. No container image exists yet.** See
[MANIFEST.md](../../MANIFEST.md) for what still has to be built.

The intended shape, for review rather than use:

```bash
docker compose up
```

Then open `http://localhost:8501`, and work through the setup screen with the
five values from [the SP-API guide](sp-api.md).

Biryar keeps a single SQLite database in a volume you control. There is no
account to create, no server to sign in to, and nothing leaves the machine.
