"""Write the API's OpenAPI document to a file.

    python scripts/openapi.py web/openapi.json

The front end's types are generated from this, so the two cannot drift: a
route that changes shape changes the document, which changes the generated
types, which fails the front end's type check. Hand-written types would
agree with the API only until someone forgot.

Nothing is connected to. ``app.openapi()`` reads the route table, so this
runs anywhere — including in CI, where there is no reason to start a server
to ask what shape it serves.
"""

from __future__ import annotations

import json
import pathlib
import sys

from biryar.api import create_app
from biryar.config import Settings


def document() -> dict[str, object]:
    """The OpenAPI document this build of the API would serve."""
    # A URL that is never dialled. The app has to be constructed to be
    # asked about its routes, and construction validates settings.
    app = create_app(
        Settings(
            database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
        )
    )
    return dict(app.openapi())


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    destination = pathlib.Path(args[0])
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Sorted and newline-terminated, so regenerating it produces either no
    # diff or a meaningful one — never a reordering.
    destination.write_text(
        json.dumps(document(), indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
