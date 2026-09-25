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
import urllib.parse
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
MAX_UPSTREAM_BYTES = 512 * 1024

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

    def _verified_uid(self) -> str | None:
        auth_header = self.headers.get("Authorization", "")
        scheme, separator, firebase_token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not separator or not firebase_token.strip():
            self._send_json(401, {"error": "firebase_auth_required"})
            return None
        try:
            claims = verify_firebase_token(
                firebase_token.strip(), Request(), audience=FIREBASE_PROJECT_ID
            )
        except Exception:
            self._send_json(401, {"error": "invalid_firebase_token"})
            return None
        uid = claims.get("user_id") or claims.get("sub")
        if not isinstance(uid, str) or not uid or len(uid) > 128:
            self._send_json(401, {"error": "invalid_firebase_identity"})
            return None
        return uid

    def _read_json_body(self, *, allow_empty: bool = False) -> dict[str, Any] | None:
        content_length = self.headers.get("Content-Length")
        try:
            length = int(content_length or "0")
        except ValueError:
            self._send_json(400, {"error": "invalid_content_length"})
            return None
        if length == 0 and allow_empty:
            return {}
        if length <= 0:
            self._send_json(400, {"error": "empty_request"})
            return None
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "request_too_large"})
            return None
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            self._send_json(400, {"error": "unsupported_transfer_encoding"})
            return None
        try:
            raw_body = self.rfile.read(length)
            if len(raw_body) != length:
                self._send_json(400, {"error": "incomplete_request"})
                return None
            body = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid_json"})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"error": "json_object_required"})
            return None
        return body

    def _proxy_rasa(self, uid: str, method: str, path: str, body: Any = None) -> None:
        ready = getattr(self.server, "rasa_ready", None)
        if ready is None or not ready.wait(timeout=210):
            rasa = getattr(self.server, "rasa_process", None)
            error = "rasa_stopped" if rasa is None or rasa.poll() is not None else "rasa_starting"
            self._send_json(503, {"error": error})
            return

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
        request_body = None if body is None else _json_bytes(body)
        headers = {
            "Authorization": f"Bearer {rasa_jwt}",
            "Accept": "application/json, application/yaml, text/yaml, text/plain",
        }
        if request_body is not None:
            headers["Content-Type"] = "application/json"
        upstream = urllib.request.Request(
            f"http://{RASA_HOST}:{RASA_PORT}{path}",
            data=request_body,
            headers=headers,
            method=method,
        )
        try:
            try:
                response = urllib.request.urlopen(upstream, timeout=180)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                result = response.read(MAX_UPSTREAM_BYTES + 1)
                if len(result) > MAX_UPSTREAM_BYTES:
                    self._send_json(502, {"error": "rasa_response_too_large"})
                    return
                content_type = response.headers.get("Content-Type", "application/json")
                self.send_response(response.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(result)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(result)
        except (urllib.error.URLError, TimeoutError, OSError):
            self._send_json(502, {"error": "rasa_unavailable"})

    def _health_payload(self) -> tuple[int, dict[str, str]]:
        rasa = getattr(self.server, "rasa_process", None)
        ready = getattr(self.server, "rasa_ready", None)
        if rasa is None or rasa.poll() is not None:
            return 503, {"status": "rasa_stopped"}
        if ready is None or not ready.is_set():
            return 503, {"status": "rasa_starting"}
        return 200, {"status": "ok", "service": "sara-gateway"}

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in ("/", "/healthz"):
            self.send_error(404)
            return
        status, payload = self._health_payload()
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path
        if route in ("/", "/healthz"):
            status, payload = self._health_payload()
            self._send_json(status, payload)
            return

        uid = self._verified_uid()
        if uid is None:
            return

        if route == "/api/rasa/version":
            self._proxy_rasa(uid, "GET", "/version")
            return
        if route == "/api/rasa/status":
            self._proxy_rasa(uid, "GET", "/status")
            return
        if route == "/api/rasa/domain":
            self._proxy_rasa(uid, "GET", "/domain")
            return

        conversation = urllib.parse.quote(uid, safe="")
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        if route == "/api/rasa/tracker":
            include_events = query.get("include_events", ["AFTER_RESTART"])[0]
            if include_events not in {"ALL", "APPLIED", "AFTER_RESTART", "NONE"}:
                self._send_json(400, {"error": "invalid_include_events"})
                return
            params = {"include_events": include_events}
            until = query.get("until", [None])[0]
            if until is not None:
                try:
                    params["until"] = str(float(until))
                except ValueError:
                    self._send_json(400, {"error": "invalid_until"})
                    return
            path = f"/conversations/{conversation}/tracker?{urllib.parse.urlencode(params)}"
            self._proxy_rasa(uid, "GET", path)
            return
        if route == "/api/rasa/story":
            params: dict[str, str] = {}
            all_sessions = query.get("all_sessions", ["false"])[0].lower()
            if all_sessions not in {"true", "false"}:
                self._send_json(400, {"error": "invalid_all_sessions"})
                return
            params["all_sessions"] = all_sessions
            until = query.get("until", [None])[0]
            if until is not None:
                try:
                    params["until"] = str(float(until))
                except ValueError:
                    self._send_json(400, {"error": "invalid_until"})
                    return
            path = f"/conversations/{conversation}/story?{urllib.parse.urlencode(params)}"
            self._proxy_rasa(uid, "GET", path)
            return

        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        route = urllib.parse.urlsplit(self.path).path
        uid = self._verified_uid()
        if uid is None:
            return

        body = self._read_json_body(allow_empty=(route in {"/api/rasa/predict", "/api/rasa/reset"}))
        if body is None:
            return

        if route == "/webhooks/rest/webhook":
            message = body.get("message")
            if not isinstance(message, str) or not message.strip():
                self._send_json(400, {"error": "message_required"})
                return
            if len(message.strip()) > MAX_MESSAGE_CHARS:
                self._send_json(413, {"error": "message_too_long"})
                return
            self._proxy_rasa(uid, "POST", "/webhooks/rest/webhook", {
                "sender": uid,
                "message": message.strip(),
            })
            return

        if route == "/api/rasa/parse":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
                self._send_json(400, {"error": "valid_text_required"})
                return
            self._proxy_rasa(uid, "POST", "/model/parse", {"text": text.strip()})
            return

        conversation = urllib.parse.quote(uid, safe="")
        if route == "/api/rasa/predict":
            self._proxy_rasa(uid, "POST", f"/conversations/{conversation}/predict", None)
            return
        if route == "/api/rasa/reset":
            # Append Rasa's restart event; do not expose the unsafe tracker-replace API.
            self._proxy_rasa(uid, "POST", f"/conversations/{conversation}/tracker/events", {
                "event": "restart",
            })
            return
        if route == "/api/rasa/trigger-intent":
            name = body.get("name")
            entities = body.get("entities", {})
            if not isinstance(name, str) or not name or len(name) > 128 or not isinstance(entities, dict):
                self._send_json(400, {"error": "valid_intent_required"})
                return
            self._proxy_rasa(uid, "POST", f"/conversations/{conversation}/trigger_intent", {
                "name": name,
                "entities": entities,
            })
            return
        if route == "/api/rasa/message":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
                self._send_json(400, {"error": "valid_text_required"})
                return
            self._proxy_rasa(uid, "POST", f"/conversations/{conversation}/messages", {
                "text": text.strip(),
                "sender": "user",
            })
            return
        if route == "/api/rasa/events":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
                self._send_json(400, {"error": "valid_text_required"})
                return
            self._proxy_rasa(uid, "POST", f"/conversations/{conversation}/tracker/events", {
                "event": "user",
                "text": text.strip(),
                "input_channel": "rest",
                "metadata": {},
            })
            return

        self._send_json(404, {"error": "not_found"})

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
