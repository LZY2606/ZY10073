from __future__ import annotations

import argparse
from pathlib import Path

from .server import create_server
from .storage import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the CiteWeave local workbench")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5207)
    parser.add_argument("--db", default=".citeweave/citeweave.db")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.db != ":memory:":
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    server = create_server(args.host, args.port, store)
    print(f"CiteWeave running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
