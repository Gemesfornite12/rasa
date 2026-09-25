"""Firebase-authenticated public gateway for Sara (Rasa).

The Render service exposes only this gateway. Rasa itself runs on loopback,
with its separate RASA_AUTH_TOKEN kept server-side and never shipped in the APK.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_firebase_token

FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "omnistudio-caaf5")
RASA_AUTH_TOKEN = os.environ.get("RASA_AUTH_TOKEN", "").strip()
PUBLIC_PORT = int(os.environ.get("PORT", "10000"))
RASA_HOST = "127.0.0.1"
RASA_PORT = 10001
RASA_URL = f"http://{RASA_HOST}:{RASA_PORT}/webhooks/rest/webhook"
MAX_BODY_BYTES = 16 * 1024
MAX_MESSAGE_CHARS = 4000

if not RASA_AUTH_TOKEN:
    raise RuntimeError("RASA_AUTH_TOKEN is required")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class SaraGatewayHandler(BaseHTTPRequestHandler):
    server_version = "SaraGateway/1.0"

    def _send_json(self, status: int, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in ("/", "/healthz"):
            self._send_json(404, {"error": "not_found"})
            return
        rasa = getattr(self.server, "rasa_process", None)
        if rasa is not None and rasa.poll() is not None:
            self._send_json(503, {"status": "rasa_stopped"})
            return
        self._send_json(200, {"status": "ok", "service": "sara-gateway"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/webhooks/rest/webhook":
            self._send_json(404, {"error": "not_found"})
            return

        auth_header = self.headers.get("Authorization", "")
        scheme, _, firebase_token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not firebase_token.strip():
            self._send_json(401, {"error": "firebase_auth_required"})
            return

        try:
            claims = verify_firebase_token(
                firebase_token.strip(), Request(), audience=FIREBASE_PROJECT_ID
            )
        except Exception:
            # Never return token-validation internals to the client.
            self._send_json(401, {"error": "invalid_firebase_token"})
            return

        uid = claims.get("user_id") or claims.get("sub")
        if not isinstance(uid, str) or not uid or len(uid) > 128:
            self._send_json(401, {"error": "invalid_firebase_identity"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid_content_length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request_too_large"})
            return

        try:
            incoming = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid_json"})
            return
        message = incoming.get("message") if isinstance(incoming, dict) else None
        if not isinstance(message, str) or not message.strip():
            self._send_json(400, {"error": "message_required"})
            return
        message = message.strip()
        if len(message) > MAX_MESSAGE_CHARS:
            self._send_json(413, {"error": "message_too_long"})
            return

        # Ignore any client-provided sender: the Firebase UID owns this tracker.
        upstream_body = _json_bytes({"sender": uid, "message": message})
        upstream = urllib.request.Request(
            RASA_URL,
            data=upstream_body,
            headers={
                "Authorization": f"Bearer {RASA_AUTH_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(upstream, timeout=180) as response:
                result = response.read(MAX_BODY_BYTES * 4)
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(result)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(result)
        except urllib.error.HTTPError as exc:
            self._send_json(exc.code if 400 <= exc.code < 600 else 502, {"error": "sara_request_failed"})
        except Exception:
            self._send_json(502, {"error": "sara_unavailable"})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid logging message bodies or credentials.
        sys.stdout.write("sara-gateway: " + (fmt % args) + "\n")
        sys.stdout.flush()


def main() -> None:
    rasa_command = [
        "rasa", "run", "--enable-api",
        "-i", RASA_HOST,
        "-p", str(RASA_PORT),
        "--credentials", "credentials.yml",
        "-t", RASA_AUTH_TOKEN,
    ]
    rasa_process = subprocess.Popen(rasa_command, cwd="/app")
    server = ThreadingHTTPServer(("0.0.0.0", PUBLIC_PORT), SaraGatewayHandler)
    server.rasa_process = rasa_process  # type: ignore[attr-defined]

    def stop_server(signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()
        if rasa_process.poll() is None:
            rasa_process.terminate()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(f"Sara Firebase gateway listening on 0.0.0.0:{PUBLIC_PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if rasa_process.poll() is None:
            rasa_process.terminate()
            try:
                rasa_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                rasa_process.kill()


if __name__ == "__main__":
    main()
