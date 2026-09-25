"""Firebase-authenticated gateway for Sara (Rasa).

Only this gateway is exposed by Render. Rasa listens on loopback, and its
RASA_AUTH_TOKEN is held exclusively in the Render environment.
"""

from __future__ import annotations

import json
import math
import jwt
import os
import re
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

# Event-specific fields from the Rasa 3.6 HTTP API OpenAPI schema.
RASA_EVENT_FIELDS = {
    "user": {"text", "input_channel", "message_id", "parse_data"},
    "bot": set(),
    "session_started": set(),
    "action": {"policy", "confidence", "name", "hide_rule_turn", "action_text"},
    "slot": {"name", "value"},
    "reset_slots": set(),
    "restart": set(),
    "reminder": set(),
    "cancel_reminder": set(),
    "pause": set(),
    "resume": set(),
    "followup": set(),
    "export": set(),
    "undo": set(),
    "rewind": set(),
    "agent": set(),
    "entities": {"entities"},
    "user_featurization": set(),
    "action_execution_rejected": set(),
    "form_validation": set(),
    "loop_interrupted": set(),
    "form": set(),
    "active_loop": set(),
}
RASA_EVENT_COMMON_FIELDS = {"event", "timestamp", "metadata"}
ENTITY_FIELDS = {"start", "end", "entity", "confidence", "extractor", "value", "role", "group"}


def _validate_tracker_event(event: Any) -> str | None:
    if not isinstance(event, dict):
        return "event_object_required"
    event_type = event.get("event")
    if not isinstance(event_type, str) or event_type not in RASA_EVENT_FIELDS:
        return "unsupported_event_type"
    allowed = RASA_EVENT_COMMON_FIELDS | RASA_EVENT_FIELDS[event_type]
    if set(event) - allowed:
        return "unexpected_event_fields"
    if "timestamp" in event and (not isinstance(event["timestamp"], int) or isinstance(event["timestamp"], bool)):
        return "timestamp_must_be_integer"
    if "metadata" in event and not isinstance(event["metadata"], dict):
        return "metadata_must_be_object"

    if event_type == "user":
        for field in ("text", "input_channel", "message_id"):
            if field in event and event[field] is not None and not isinstance(event[field], str):
                return f"{field}_must_be_string"
        if isinstance(event.get("text"), str) and len(event["text"]) > MAX_MESSAGE_CHARS:
            return "text_too_long"
        if "parse_data" in event and event["parse_data"] is not None and not isinstance(event["parse_data"], dict):
            return "parse_data_must_be_object"
    elif event_type == "action":
        for field in ("policy", "name", "action_text"):
            if field in event and event[field] is not None and not isinstance(event[field], str):
                return f"{field}_must_be_string"
        confidence = event.get("confidence")
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence)):
            return "confidence_must_be_finite_number"
        if "hide_rule_turn" in event and not isinstance(event["hide_rule_turn"], bool):
            return "hide_rule_turn_must_be_boolean"
    elif event_type == "slot":
        if not isinstance(event.get("name"), str) or not event["name"].strip() or "value" not in event:
            return "slot_requires_name_and_value"
    elif event_type == "entities":
        entities = event.get("entities")
        if not isinstance(entities, list) or len(entities) > 100:
            return "entities_must_be_array_of_at_most_100"
        for entity in entities:
            if not isinstance(entity, dict) or set(entity) - ENTITY_FIELDS:
                return "invalid_entity_object"
            if not isinstance(entity.get("entity"), str) or "value" not in entity:
                return "entity_requires_name_and_value"
            for field in ("start", "end"):
                if field in entity and (not isinstance(entity[field], int) or isinstance(entity[field], bool)):
                    return f"entity_{field}_must_be_integer"
            confidence = entity.get("confidence")
            if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence)):
                return "entity_confidence_must_be_finite_number"
            for field in ("extractor", "role", "group"):
                if field in entity and entity[field] is not None and not isinstance(entity[field], str):
                    return f"entity_{field}_must_be_string"
    return None


FELO_API_KEY = os.environ.get("FELO_API_KEY", "").strip()
FELO_CHAT_URL = "https://openapi.felo.ai/v2/chat"
SEARCH_PREFIX_RE = re.compile(
    r"^\s*(?:sara[,:]\s*)?(?:/buscar|/search|"
    r"(?:busca|buscar|investiga)\s+(?:(?:en\s+)?(?:internet|la\s+web|web|google)))"
    r"\s*[:,-]?\s*(?P<query>.+)$",
    re.IGNORECASE,
)
SEARCH_REQUESTS_PER_MINUTE = 6
_search_call_times: dict[str, list[float]] = {}
_search_rate_lock = threading.Lock()


def _extract_web_search_query(message: str) -> str | None:
    match = SEARCH_PREFIX_RE.match(message.strip())
    if not match:
        return None
    query = match.group("query").strip()
    return query if query else None


def _allow_web_search(uid: str) -> bool:
    now = time.time()
    with _search_rate_lock:
        recent = [t for t in _search_call_times.get(uid, []) if now - t < 60]
        if len(recent) >= SEARCH_REQUESTS_PER_MINUTE:
            _search_call_times[uid] = recent
            return False
        recent.append(now)
        _search_call_times[uid] = recent
        return True


def _felo_web_search(query: str) -> dict[str, Any]:
    if not FELO_API_KEY:
        raise RuntimeError("search_not_configured")
    request = urllib.request.Request(
        FELO_CHAT_URL,
        data=_json_bytes({"query": query}),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {FELO_API_KEY}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=55)
    except urllib.error.HTTPError as exc:
        # Do not forward provider response bodies or credentials to the client.
        if exc.code == 429:
            raise RuntimeError("search_rate_limited") from None
        if exc.code in (401, 403):
            raise RuntimeError("search_auth_failed") from None
        raise RuntimeError("search_provider_error") from None
    with response:
        raw = response.read(MAX_UPSTREAM_BYTES + 1)
    if len(raw) > MAX_UPSTREAM_BYTES:
        raise RuntimeError("search_response_too_large")
    payload = json.loads(raw)
    if payload.get("status") != "ok":
        raise RuntimeError("search_provider_error")
    data = payload.get("data") or {}
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("search_no_answer")
    analysis = data.get("query_analysis") or {}
    queries = analysis.get("queries") or []
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for resource in data.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        url = resource.get("link")
        title = resource.get("title")
        if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in seen:
            seen.add(url)
            sources.append({"title": title if isinstance(title, str) and title else url, "url": url})
        if len(sources) >= 8:
            break
    return {
        "answer": answer.strip(),
        "search_queries": [q for q in queries if isinstance(q, str)][:8],
        "sources": sources,
    }


def _search_reply_text(result: dict[str, Any]) -> str:
    answer = result.get("answer", "").strip()
    sources = result.get("sources") or []
    if not sources:
        return answer + "\n\nNo recibí enlaces de fuente en esta búsqueda."
    source_lines = ["\n\nFuentes:"]
    for i, source in enumerate(sources[:8], start=1):
        source_lines.append(f"{i}. {source['title']}\n{source['url']}")
    return answer + "".join(source_lines)


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

        if route == "/api/rasa/search":
            query = body.get("query")
            if not isinstance(query, str) or not query.strip() or len(query.strip()) > 1000:
                self._send_json(400, {"error": "valid_query_required"})
                return
            if not FELO_API_KEY:
                self._send_json(503, {"error": "web_search_not_configured"})
                return
            if not _allow_web_search(uid):
                self._send_json(429, {"error": "web_search_rate_limited"})
                return
            try:
                self._send_json(200, _felo_web_search(query.strip()))
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError):
                self._send_json(502, {"error": "web_search_unavailable"})
            return

        if route == "/webhooks/rest/webhook":
            message = body.get("message")
            if not isinstance(message, str) or not message.strip():
                self._send_json(400, {"error": "message_required"})
                return
            if len(message.strip()) > MAX_MESSAGE_CHARS:
                self._send_json(413, {"error": "message_too_long"})
                return
            clean_message = message.strip()
            search_query = _extract_web_search_query(clean_message)
            if search_query is not None:
                if len(search_query) > 1000:
                    self._send_json(200, [{"recipient_id": uid, "text": "La consulta es demasiado larga; resúmela y vuelve a intentarlo."}])
                    return
                if not FELO_API_KEY:
                    self._send_json(200, [{"recipient_id": uid, "text": "La búsqueda web todavía no está configurada en Sara."}])
                    return
                if not _allow_web_search(uid):
                    self._send_json(200, [{"recipient_id": uid, "text": "Llegaste al límite de búsquedas disponible por ahora. Inténtalo más tarde."}])
                    return
                try:
                    result = _felo_web_search(search_query)
                    self._send_json(200, [{"recipient_id": uid, "text": _search_reply_text(result)}])
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError):
                    self._send_json(200, [{"recipient_id": uid, "text": "No pude completar la búsqueda ahora. Inténtalo de nuevo en un momento."}])
                return
            self._proxy_rasa(uid, "POST", "/webhooks/rest/webhook", {
                "sender": uid,
                "message": clean_message,
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
            self._proxy_rasa(uid, "POST", f"/conversations/{conversation}/tracker/events", [{
                "event": "restart",
            }])
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
            event = body.get("event")
            # Keep the earlier text-only client compatible while supporting the
            # distinct Rasa OpenAPI payload for each event type.
            if event is None and isinstance(body.get("text"), str):
                text = body["text"].strip()
                event = {"event": "user", "text": text, "input_channel": "rest"}
            validation_error = _validate_tracker_event(event)
            if validation_error:
                self._send_json(400, {"error": validation_error})
                return
            self._proxy_rasa(uid, "POST", f"/conversations/{conversation}/tracker/events", [event])
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


# Additional Felo tools for explicit YouTube-caption and webpage-reading requests.
FELO_YOUTUBE_SUBTITLING_URL = "https://openapi.felo.ai/v2/youtube/subtitling"
FELO_WEB_EXTRACT_URL = "https://openapi.felo.ai/v2/web/extract"
YOUTUBE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{11}")
CHAT_URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
YOUTUBE_SUMMARY_PATTERN = re.compile(
    r"\b(?:resume|resumen|resumir|analiza|explicame|explícame)\b",
    re.IGNORECASE,
)
YOUTUBE_INTENT_PATTERN = re.compile(
    r"\b(?:transcribe|transcribir|transcripcion|transcripción|subtitulo|subtítulos|subtitulos|que dice|qué dice)\b",
    re.IGNORECASE,
)
WEB_FETCH_INTENT_PATTERN = re.compile(
    r"\b(?:lee|leer|resume|resumen|analiza|extrae|explicame|explícame|que dice|qué dice|revisa)\b",
    re.IGNORECASE,
)


def _extract_youtube_video_code(message: str) -> str | None:
    for raw_url in CHAT_URL_PATTERN.findall(message):
        candidate_url = raw_url.rstrip(".,!?;:)]}\"'")
        try:
            parsed = urllib.parse.urlsplit(candidate_url)
            host = (parsed.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            code = ""
            if host == "youtu.be":
                code = parsed.path.strip("/").split("/")[0]
            elif host in {"youtube.com", "m.youtube.com", "youtube-nocookie.com"}:
                if parsed.path.rstrip("/") == "/watch":
                    code = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
                else:
                    parts = [part for part in parsed.path.split("/") if part]
                    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
                        code = parts[1]
            if YOUTUBE_ID_PATTERN.fullmatch(code):
                return code
        except (ValueError, IndexError):
            continue
    return None


def _extract_web_page_url(message: str) -> str | None:
    for raw_url in CHAT_URL_PATTERN.findall(message):
        candidate = raw_url.rstrip(".,!?;:)]}\"'")
        try:
            parsed = urllib.parse.urlsplit(candidate)
            host = (parsed.hostname or "").lower()
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            continue
        if host.startswith("www."):
            host = host[4:]
        if host in {"youtube.com", "m.youtube.com", "youtu.be", "youtube-nocookie.com"}:
            continue
        if host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
            continue
        return candidate
    return None


def _felo_json_request(request: urllib.request.Request) -> dict[str, Any]:
    if not FELO_API_KEY:
        raise RuntimeError("felo_not_configured")
    try:
        response = urllib.request.urlopen(request, timeout=55)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RuntimeError("felo_rate_limited") from None
        if exc.code in (401, 403):
            raise RuntimeError("felo_auth_failed") from None
        raise RuntimeError("felo_provider_error") from None
    with response:
        raw = response.read(MAX_UPSTREAM_BYTES + 1)
    if len(raw) > MAX_UPSTREAM_BYTES:
        raise RuntimeError("felo_response_too_large")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError("felo_invalid_response") from None
    if not isinstance(payload, dict) or payload.get("status") not in {"ok", 200, "200"}:
        raise RuntimeError("felo_provider_error")
    return payload


def _felo_youtube_subtitles(video_code: str) -> dict[str, str]:
    if not FELO_API_KEY:
        raise RuntimeError("felo_not_configured")
    query = urllib.parse.urlencode({"video_code": video_code, "with_time": "false"})
    request = urllib.request.Request(
        f"{FELO_YOUTUBE_SUBTITLING_URL}?{query}",
        headers={"Authorization": f"Bearer {FELO_API_KEY}", "Accept": "application/json"},
        method="GET",
    )
    payload = _felo_json_request(request)
    data = payload.get("data") or {}
    title = data.get("title") if isinstance(data, dict) else ""
    contents = data.get("contents", []) if isinstance(data, dict) else []
    transcript = "\n".join(
        item.get("text", "").strip()
        for item in contents
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text", "").strip()
    ) if isinstance(contents, list) else ""
    return {
        "title": title.strip() if isinstance(title, str) else "",
        "transcript": transcript,
        "url": f"https://www.youtube.com/watch?v={video_code}",
    }


def _felo_web_extract(url: str) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("invalid_web_url")
    if not FELO_API_KEY:
        raise RuntimeError("felo_not_configured")
    request = urllib.request.Request(
        FELO_WEB_EXTRACT_URL,
        data=_json_bytes({"url": url, "crawl_mode": "fast", "output_format": "text", "with_readability": True}),
        headers={
            "Authorization": f"Bearer {FELO_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    payload = _felo_json_request(request)
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise RuntimeError("felo_provider_error")
    content = data.get("content", "")
    if isinstance(content, dict):
        content = next((content.get(key) for key in ("text", "markdown", "content", "html") if isinstance(content.get(key), str)), "")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("felo_empty_content")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    title = data.get("title") or metadata.get("title") or ""
    return {"title": title.strip() if isinstance(title, str) else "", "content": content.strip(), "url": url}


def _sara_proxy_with_felo_tools(self: SaraGatewayHandler, uid: str, method: str, path: str, body: Any = None) -> None:
    if method == "POST" and path == "/webhooks/rest/webhook" and isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str):
            video_code = _extract_youtube_video_code(message)
            if video_code and YOUTUBE_SUMMARY_PATTERN.search(message):
                if not FELO_API_KEY:
                    self._send_json(200, [{"recipient_id": uid, "text": "La búsqueda de videos con Felo todavía no está configurada."}])
                    return
                if not _allow_web_search(uid):
                    self._send_json(200, [{"recipient_id": uid, "text": "Llegaste al límite temporal de consultas a Felo. Inténtalo más tarde."}])
                    return
                try:
                    video_url = f"https://www.youtube.com/watch?v={video_code}"
                    result = _felo_web_search(f"Resume en español este video de YouTube: {video_url}")
                    self._send_json(200, [{"recipient_id": uid, "text": _search_reply_text(result)}])
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError):
                    self._send_json(200, [{"recipient_id": uid, "text": "No pude buscar información de ese video ahora."}])
                return
            if video_code and YOUTUBE_INTENT_PATTERN.search(message):
                if not FELO_API_KEY:
                    self._send_json(200, [{"recipient_id": uid, "text": "La función de YouTube todavía no está configurada."}])
                    return
                if not _allow_web_search(uid):
                    self._send_json(200, [{"recipient_id": uid, "text": "Llegaste al límite temporal de consultas a Felo. Inténtalo más tarde."}])
                    return
                try:
                    result = _felo_youtube_subtitles(video_code)
                    transcript = result["transcript"]
                    if not transcript:
                        text = f"No encontré subtítulos disponibles para este video: {result['url']}"
                    else:
                        limit = 8000
                        shown = transcript[:limit]
                        title = result["title"] or "Video de YouTube"
                        text = f"Subtítulos de YouTube: {title}\n{result['url']}\n\n{shown}"
                        if len(transcript) > limit:
                            text += "\n\n[Transcripción recortada para caber en el chat.]"
                    self._send_json(200, [{"recipient_id": uid, "text": text}])
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError):
                    self._send_json(200, [{"recipient_id": uid, "text": "No pude obtener los subtítulos de ese video ahora."}])
                return

            page_url = _extract_web_page_url(message)
            if page_url and WEB_FETCH_INTENT_PATTERN.search(message):
                if not FELO_API_KEY:
                    self._send_json(200, [{"recipient_id": uid, "text": "La lectura de páginas con Felo todavía no está configurada."}])
                    return
                if not _allow_web_search(uid):
                    self._send_json(200, [{"recipient_id": uid, "text": "Llegaste al límite temporal de consultas a Felo. Inténtalo más tarde."}])
                    return
                try:
                    result = _felo_web_extract(page_url)
                    limit = 8000
                    title = result["title"] or "Contenido de la página"
                    text = f"{title}\nFuente: {result['url']}\n\n{result['content'][:limit]}"
                    if len(result["content"]) > limit:
                        text += "\n\n[Contenido recortado para caber en el chat.]"
                    self._send_json(200, [{"recipient_id": uid, "text": text}])
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, RuntimeError):
                    self._send_json(200, [{"recipient_id": uid, "text": "No pude extraer el contenido de esa página ahora."}])
                return
    _ORIGINAL_SARA_PROXY_RASA(self, uid, method, path, body)


_ORIGINAL_SARA_PROXY_RASA = SaraGatewayHandler._proxy_rasa
SaraGatewayHandler._proxy_rasa = _sara_proxy_with_felo_tools


if __name__ == "__main__":
    main()
