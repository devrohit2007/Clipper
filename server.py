#!/usr/bin/env python3
"""Local web app for the clipping agent.

    python server.py                    # http://localhost:8000
    python server.py --port 8080
    python server.py --set-password     # protect the app with a password
    python server.py --host 0.0.0.0     # reachable from your Wi-Fi (requires a password)
"""
from __future__ import annotations

import argparse
import getpass
import os
import signal
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from modules.store import Store  # noqa: E402
from modules.utilities import load_env_file, setup_logging, log  # noqa: E402
from modules.webapp import App, Server, hash_password  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Clipper local web app")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default=os.environ.get("DATA_DIR", os.path.join(ROOT, "data")))
    ap.add_argument("--env-file", default=os.path.join(ROOT, ".env"))
    ap.add_argument("--set-password", action="store_true", help="set or change the UI password, then exit")
    ap.add_argument("--clear-password", action="store_true", help="remove the UI password, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    load_env_file(a.env_file)
    os.chdir(ROOT)                      # relative paths in .env (output, client_secret.json) resolve here
    if a.set_password or a.clear_password:
        store = Store(os.path.join(a.data_dir, "clipper.db"))
        if a.clear_password:
            store.kv_del("auth")
            print("Password removed.")
            return 0
        p1, p2 = getpass.getpass("New password: "), getpass.getpass("Repeat it: ")
        if p1 != p2 or len(p1) < 6:
            print("Passwords must match and be at least 6 characters.")
            return 2
        store.kv_set("auth", hash_password(p1))
        print("Password saved.")
        return 0

    setup_logging(os.path.join(a.data_dir, "server.log"), a.verbose)
    app = App(a.data_dir, os.path.join(ROOT, "ui"), a.host, a.port)
    if app.lan and not app.store.kv_get("auth"):
        print("Listening on the network needs a password. Run: python server.py --set-password")
        return 2
    try:
        httpd = Server((a.host, a.port), app)
    except OSError as e:
        print(f"Cannot start on {a.host}:{a.port}: {e}. Try --port 8080.")
        return 2
    app.start_worker()

    def shutdown(*_):
        print("\nStopping...")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    print(f"Clipper is running: http://localhost:{a.port}")
    if app.store.kv_get("auth") is None:
        print("Tip: protect it with a password: python server.py --set-password")
    try:
        httpd.serve_forever()
    finally:
        app.stop()
        httpd.server_close()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
