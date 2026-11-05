"""A PostgreSQL for the test suite, with no root and no Docker.

Most of this suite needs a real PostgreSQL, because the thing under test
*is* PostgreSQL behaviour: row-level security, ``current_setting``,
``FORCE ROW LEVEL SECURITY``, enum types. A suite that skipped those tests
would report green while proving nothing about the one property the product
most depends on.

That made the tests effectively CI-only for anyone without a database to
hand, which is a bad place to be: a round trip through a runner to find out
whether a policy works is slow enough that it stops being done.

``pgserver`` ships a complete PostgreSQL as a Python wheel, so there is a
third option between "install a system service" and "install Docker". This
script runs one on a loopback port, in a directory under the project, owned
by whoever ran it.

    python scripts/devdb.py start     # prints the URL to export
    python scripts/devdb.py url
    python scripts/devdb.py stop
    python scripts/devdb.py reset     # throw it away and start again

It listens only on 127.0.0.1 and trusts local connections, which is right
for a throwaway development cluster and wrong for anything else. It is not a
way to run Biryar in production; ``docker-compose.yml`` is.

**The bundled server may be a different PostgreSQL major version from the
one CI runs.** Both are printed by ``status`` so the difference is visible
rather than assumed away.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

#: Not 5432, so this cannot collide with a system PostgreSQL someone already
#: runs, and cannot be mistaken for one in a connection string.
PORT = 55432
DATABASE = "biryar_dev"
USER = "postgres"

ROOT = pathlib.Path(__file__).resolve().parents[1]
PGDATA = ROOT / ".devdb"

URL = f"postgresql+psycopg://{USER}@127.0.0.1:{PORT}/{DATABASE}"

EXTRA_CONF = f"""
# Written by scripts/devdb.py. A development cluster, not a deployment.
listen_addresses = '127.0.0.1'
port = {PORT}

# Durability traded for speed. Losing this cluster costs nothing: it is
# rebuilt by `reset`, and every test drops and recreates its schema anyway.
fsync = off
synchronous_commit = off
full_page_writes = off
"""


def _bin() -> pathlib.Path:
    try:
        import pgserver
    except ImportError:
        sys.exit(
            "pgserver is not installed. It ships PostgreSQL as a wheel:\n"
            "    uv pip install -e '.[dev]'"
        )

    binaries = pathlib.Path(pgserver.__file__).parent / "pginstall" / "bin"
    if not (binaries / "pg_ctl").exists():      # pragma: no cover - defensive
        sys.exit(f"pgserver is installed but has no binaries at {binaries}")
    return binaries


def _run(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=check)


def is_running() -> bool:
    binaries = _bin()
    return _run(
        str(binaries / "pg_isready"), "-h", "127.0.0.1", "-p", str(PORT),
        check=False,
    ).returncode == 0


def start() -> None:
    binaries = _bin()

    # Deliberately not "if running, do nothing". The port being busy does
    # not mean this cluster is serving, and it certainly does not mean the
    # database exists — an earlier version returned here and handed back a
    # URL pointing at a database nothing had created, which failed sixty-odd
    # tests with a connection error rather than saying so.
    if not (PGDATA / "PG_VERSION").exists():
        PGDATA.mkdir(parents=True, exist_ok=True)
        print(f"creating a cluster in {PGDATA.relative_to(ROOT)} ...")
        _run(
            str(binaries / "initdb"), "-D", str(PGDATA), "-U", USER,
            "--auth=trust", "--encoding=UTF8",
        )
        (PGDATA / "postgresql.conf").open("a").write(EXTRA_CONF)

    if not is_running():
        _run(
            str(binaries / "pg_ctl"), "-D", str(PGDATA),
            "-l", str(PGDATA / "server.log"), "-w", "start",
        )

    existing = _run(
        str(binaries / "psql"), "-h", "127.0.0.1", "-p", str(PORT), "-U", USER,
        "-tAc", f"SELECT 1 FROM pg_database WHERE datname = '{DATABASE}'",
        "-d", "postgres",
    ).stdout.strip()
    if existing != "1":
        _run(
            str(binaries / "createdb"), "-h", "127.0.0.1", "-p", str(PORT),
            "-U", USER, DATABASE,
        )

    # Prove it, rather than assume the steps above worked.
    check = _run(
        str(binaries / "psql"), "-h", "127.0.0.1", "-p", str(PORT), "-U", USER,
        "-d", DATABASE, "-tAc", "SELECT 1", check=False,
    )
    if check.returncode != 0:
        sys.exit(
            f"the cluster is up but {DATABASE} is not reachable:\n"
            f"{check.stderr.strip()}\n"
            "try: python scripts/devdb.py reset"
        )

    print("ready\n")
    print("export BIRYAR_TEST_DATABASE_URL=" + URL)


def stop() -> None:
    if not (PGDATA / "PG_VERSION").exists():
        print("no cluster to stop")
        return
    binaries = _bin()
    result = _run(
        str(binaries / "pg_ctl"), "-D", str(PGDATA), "-m", "fast", "stop",
        check=False,
    )
    print(result.stdout.strip() or result.stderr.strip() or "stopped")


def reset() -> None:
    """Throw the cluster away. Cheap, and the honest fix for a wedged one."""
    import shutil

    if is_running():
        stop()
    if PGDATA.exists():
        shutil.rmtree(PGDATA)
        print(f"removed {PGDATA.relative_to(ROOT)}")
    start()


def status() -> None:
    running = is_running()
    print(f"cluster : {PGDATA}")
    print(f"running : {running}")
    if running:
        binaries = _bin()
        version = _run(
            str(binaries / "psql"), "-h", "127.0.0.1", "-p", str(PORT),
            "-U", USER, "-d", "postgres", "-tAc", "SHOW server_version",
        ).stdout.strip()
        print(f"version : {version}   (CI runs the version in .github/workflows/ci.yml)")
        print(f"url     : {URL}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=["start", "stop", "reset", "status", "url"],
    )
    command = parser.parse_args().command

    if command == "url":
        print(URL)
    else:
        {"start": start, "stop": stop, "reset": reset, "status": status}[command]()


if __name__ == "__main__":
    main()
