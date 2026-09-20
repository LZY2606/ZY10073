"""``python -m citeweave --host ... --port ...`` entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import DB
from .web.server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citeweave",
                                     description="time-valid citation workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5207)
    parser.add_argument("--db", default=str(Path.cwd() / "citeweave.sqlite3"),
                        help="SQLite database path")
    parser.add_argument("--sample", help="optional sample directory to seed")
    args = parser.parse_args(argv)

    db = DB(args.db, recover_on_open=True)
    abandoned = int(db.meta_get("last_recovered_count", "0") or 0)
    if abandoned:
        print(f"[recover] removed {abandoned} abandoned staged import(s)",
              file=sys.stderr)
    if args.sample:
        from .sample import seed_directory
        count = seed_directory(db, Path(args.sample))
        print(f"[seed] {count} version(s) imported from {args.sample}",
              file=sys.stderr)

    httpd = serve(db, args.host, args.port)
    print(f"CiteWeave listening on http://{args.host}:{args.port}",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
