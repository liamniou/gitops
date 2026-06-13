#!/usr/bin/env python3
"""Simple HTTP healthcheck server for media mount verification."""

import os
from http.server import HTTPServer, BaseHTTPRequestHandler

MEDIA_PATH = os.environ.get("MEDIA_PATH", "/media")
PORT = int(os.environ.get("PORT", "8080"))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self.check_media()
        else:
            self.send_error(404)

    def check_media(self):
        try:
            if not os.path.ismount(MEDIA_PATH) and not os.path.isdir(MEDIA_PATH):
                self.respond(503, "FAIL: /media is not mounted")
                return

            contents = os.listdir(MEDIA_PATH)
            if not contents:
                self.respond(503, "FAIL: /media is empty")
                return

            self.respond(200, f"OK: /media contains {len(contents)} items")

        except Exception as e:
            self.respond(503, f"FAIL: {e}")

    def respond(self, code, message):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"{message}\n".encode())

    def log_message(self, format, *args):
        pass  # Suppress logging


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"Healthcheck server listening on port {PORT}, checking {MEDIA_PATH}")
    server.serve_forever()
