"""Nightly SQLite backup: consistent snapshot via the sqlite3 backup API,
gzipped, dated, pruned after RETAIN_DAYS. Cron (run as the app user):

    10 3 * * * cd /website/box2fit-platform && .venv/bin/python -m scripts.backup_db >> logs/backup.log 2>&1

Backups land in /website/backups (override with BACKUP_DIR env). This
protects against bad deploys, bad deletes and DB corruption; it does NOT
survive the droplet itself dying — pair it with DigitalOcean droplet
backups or an off-box copy for that.
"""
import gzip
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from app import create_app

RETAIN_DAYS = 14


def main() -> None:
    app = create_app()
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    if not uri.startswith("sqlite"):
        sys.exit(f"not a sqlite database ({uri.split(':')[0]}) — use the "
                 "engine's own dump tooling instead")
    db_path = uri.split("///", 1)[1]
    if not Path(db_path).is_absolute():
        db_path = str(Path(app.instance_path) / db_path)
    if not Path(db_path).exists():
        sys.exit(f"database file not found: {db_path}")

    backup_dir = Path(os.environ.get("BACKUP_DIR", "/website/backups"))
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    tmp = backup_dir / f".inprogress-{stamp}.db"
    out = backup_dir / f"box2fit-{stamp}.db.gz"

    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)  # consistent even while the app is writing
    dst.close()
    src.close()

    with open(tmp, "rb") as f_in, gzip.open(out, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp.unlink()

    cutoff = time.time() - RETAIN_DAYS * 86400
    pruned = 0
    for old in backup_dir.glob("box2fit-*.db.gz"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
            pruned += 1

    size_kb = out.stat().st_size // 1024
    print(f"{datetime.now():%Y-%m-%d %H:%M} backup ok: {out.name} "
          f"({size_kb} KB), pruned {pruned}")


if __name__ == "__main__":
    main()
