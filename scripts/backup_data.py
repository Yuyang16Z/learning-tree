"""Back up the configured SQLite file using SQLite's online backup API."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def backup(source: Path, destination: Path) -> Path | None:
    source = source.resolve()
    if not source.exists():
        return None
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(destination, 0o700)
    target = destination / f"{source.stem}-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src,
        sqlite3.connect(target) as dst,
    ):
        src.backup(dst)
    if os.name != "nt":
        os.chmod(target, 0o600)
    return target


def main() -> None:
    # Resolve relative .env/database paths consistently, even when called elsewhere.
    os.chdir(ROOT)
    os.umask(0o077)
    from sqlalchemy.engine import make_url

    from app.config import settings

    url = make_url(settings.database_url)
    if url.drivername.split("+")[0] != "sqlite" or not url.database or url.database == ":memory:":
        raise SystemExit("Backup requires a configured SQLite file.")
    result = backup(Path(url.database), ROOT / ".backups")
    print(
        f"Backup saved: .backups/{result.name}"
        if result
        else "First start: a new local database will be created."
    )


if __name__ == "__main__":
    main()
