"""Firebase-authenticated gateway for Sara (Rasa).

Only this gateway is exposed by Render. Rasa listens on loopback, and its
RASA_AUTH_TOKEN is held exclusively in the Render environment.
"""

from __future__ import annotations

import json
import jwt
import os
import signal
import subprocess
import sys
import threading
import time
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
RASA_ROOT_URL = f"http://{RASA_HOST}:{RASA_PORT}/"
MAX_BODY_BYTES = 16 * 1024
MAX_MESSAGE_CHARS = 4000
MAX_UPSTREAM_BYTES = 64 * 1024

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
        ready = getattr(self.server, "rasa_ready", None)
        if rasa is None or rasa.poll() is not None:
            self._send_json(503, {"status": "rasa_stopped"})
            return
        if ready is None or not ready.is_set():
            self._send_json(503, {"status": "rasa_starting"})
            return
        self._send_json(200, {"status": "ok", "service": "sara-gateway"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/webhooks/rest/webhook":
            self._send_json(404, {"error": "not_found"})
            return

        auth_header = self.headers.get("Authorization", "")
        scheme, separator, firebase_token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not separator or not firebase_token.strip():
            self._send_json(401, {"error": "firebase_auth_required"})
            return
        try:
            claims = verify_firebase_token(
                firebase_token.strip(), Request(), audience=FIREBASE_PROJECT_ID
            )
        except Exception:
            # Do not expose token-validation internals to clients or logs.
            self._send_json(401, {"error": "invalid_firebase_token"})
            return

        uid = claims.get("user_id") or claims.get("sub")
        if not isinstance(uid, str) or not uid or len(uid) > 128:
            self._send_json(401, {"error": "invalid_firebase_identity"})
            return

        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length or "0")
        except ValueError:
            self._send_json(400, {"error": "invalid_content_length"})
            return
        if length <= 0:
            self._send_json(400, {"error": "empty_request"})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request_too_large"})
            return
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            self._send_json(400, {"error": "unsupported_transfer_encoding"})
            return

        try:
            raw_body = self.rfile.read(length)
            if len(raw_body) != length:
                self._send_json(400, {"error": "incomplete_request"})
                return
            incoming = json.loads(raw_body)
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

        # Keep the public port available while Rasa loads its model. A first
        # request may wait for the model rather than failing during cold start.
        ready = getattr(self.server, "rasa_ready", None)
        if ready is None or not ready.wait(timeout=210):
            rasa = getattr(self.server, "rasa_process", None)
            error = "sara_stopped" if rasa is None or rasa.poll() is not None else "sara_starting"
            self._send_json(503, {"error": error})
            return

        # Never trust a client-provided sender: the verified Firebase UID owns
        # this Rasa tracker, providing tracker isolation per signed-in account.
        upstream_body = _json_bytes({"sender": uid, "message": message})
        now = int(time.time())
        rasa_jwt = jwt.encode(
            {
                "user": {"username": uid, "role": "user"},
                "iat": now,
                "exp": now + 300,
            },
            RASA_AUTH_TOKEN,
            algorithm="HS256",
        )
        upstream = urllib.request.Request(
            RASA_URL,
            data=upstream_body,
            headers={
                "Authorization": f"Bearer {rasa_jwt}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(upstream, timeout=180) as response:
                result = response.read(MAX_UPSTREAM_BYTES + 1)
                if len(result) > MAX_UPSTREAM_BYTES:
                    self._send_json(502, {"error": "sara_response_too_large"})
                    return
                # Ensure upstream returned valid JSON before forwarding it.
                json.loads(result)
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(result)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(result)
        except urllib.error.HTTPError as exc:
            self._send_json(502, {"error": "sara_request_failed"})
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            self._send_json(502, {"error": "sara_unavailable"})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Default HTTP logs include only method/path/status, never body/headers.
        sys.stdout.write("sara-gateway: " + (fmt % args) + "\n")
        sys.stdout.flush()


def main() -> None:
    rasa_command = [
        "rasa", "run", "--enable-api",
        "-i", RASA_HOST,
        "-p", str(RASA_PORT),
        "--credentials", "credentials.yml",
        "--jwt-secret", RASA_AUTH_TOKEN,
        "--jwt-method", "HS256",
    ]
    rasa_process = subprocess.Popen(rasa_command, cwd="/app")

    # Bind Render's public port immediately so the platform can detect it even
    # while Rasa takes several minutes to load its model.
    server = ThreadingHTTPServer(("0.0.0.0", PUBLIC_PORT), SaraGatewayHandler)
    server.rasa_process = rasa_process  # type: ignore[attr-defined]
    server.rasa_ready = threading.Event()  # type: ignore[attr-defined]

    def monitor_rasa_readiness() -> None:
        while rasa_process.poll() is None:
            try:
                with urllib.request.urlopen(RASA_ROOT_URL, timeout=2):
                    server.rasa_ready.set()  # type: ignore[attr-defined]
                    print("Rasa is ready to receive messages", flush=True)
                    return
            except urllib.error.HTTPError:
                # An HTTP response means Rasa has bound its internal port.
                server.rasa_ready.set()  # type: ignore[attr-defined]
                print("Rasa is ready to receive messages", flush=True)
                return
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1)
        print("Rasa exited before becoming ready", flush=True)

    threading.Thread(target=monitor_rasa_readiness, daemon=True).start()

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
