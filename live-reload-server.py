#!/usr/bin/env python3
"""Static file server with a tiny Server-Sent Events live reload endpoint."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


class LiveReloadHandler(SimpleHTTPRequestHandler):
    watched_file: Path
    root_dir: Path

    def do_GET(self) -> None:
        if self.path.startswith("/__live_reload/events"):
            self._serve_events()
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        clean = posixpath.normpath(unquote(parsed.path))
        parts = [part for part in clean.split("/") if part and part not in (os.curdir, os.pardir)]
        target = self.root_dir
        for part in parts:
            target = target / part
        return str(target)

    def log_message(self, format: str, *args: object) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {self.address_string()} {format % args}")

    def _serve_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_mtime = self._mtime()
        while True:
            time.sleep(0.8)
            current_mtime = self._mtime()
            if current_mtime != last_mtime:
                last_mtime = current_mtime
                payload = json.dumps({"mtime": current_mtime})
                try:
                    self.wfile.write(f"event: reload\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

    def _mtime(self) -> float:
        try:
            return self.watched_file.stat().st_mtime
        except FileNotFoundError:
            return 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a static file tree with live reload.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--root", default=".")
    parser.add_argument("--watch", required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    watched = (root / args.watch).resolve()
    if not watched.exists():
        raise SystemExit(f"watched file does not exist: {watched}")

    LiveReloadHandler.root_dir = root
    LiveReloadHandler.watched_file = watched

    server = ThreadingHTTPServer((args.host, args.port), LiveReloadHandler)
    print(f"Serving {root}")
    print(f"Watching {watched}")
    print(f"Open http://{args.host}:{args.port}/{watched.name}")
    server.serve_forever()


if __name__ == "__main__":
    main()
