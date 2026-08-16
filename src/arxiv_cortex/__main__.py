from __future__ import annotations

import argparse

from waitress import serve

from arxiv_cortex import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Arxiv Cortex local server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    app = create_app({"SCHEDULER_ENABLED": True})
    serve(app, host=args.host, port=args.port, threads=4)


if __name__ == "__main__":
    main()
