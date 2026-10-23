"""Local, read-only bridge between the PETRON runtime and the Brain (browser).

    petron brain serve [--port 8765]      ->  http://127.0.0.1:8765/

What it does: serves the approved Brain assets and streams the runtime's own events, mapped to
visual states, over Server-Sent Events. By default it is strictly read-only. With
``--interactive --template SPEC`` it also accepts three commands from the Brain (see
``brain_commands``): draft a mission from words, approve a Brain draft by a deliberate gesture,
discard a Brain draft. Those require a per-run secret delivered only in the URL fragment printed
in the operator's terminal, an exact same-origin ``Origin``, a JSON body and a closed schema.

Security posture:

* binds to a loopback address only (anything else is refused);
* a fixed asset allowlist: no path from the URL ever reaches the filesystem;
* the ``Host`` header must name this loopback server (blocks DNS rebinding), a present ``Origin``
  must be this server's own origin, and cross-site fetches are refused; no CORS headers at all;
* GET/HEAD only; a strict Content-Security-Policy (self only, no inline script);
* the SQLite store is opened read-only (``mode=ro``); event payload contents (file text, tool
  output) are never forwarded, only kind, mission, sequence, time and the mapped visual state.

Visual mapping is a documented heuristic over authoritative events (see ``brain_state_for``):
no CPU, token or progress figure is invented. ``activity`` is a fixed value per event type.
"""

from __future__ import annotations

import datetime as _dt
import hmac
import json
import re
import socket
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store import Store

_PACKAGED_BRAIN = Path(__file__).resolve().parent / "brain_assets"
# Source checkout: apps/petron/brain; installed wheel: petron/brain_assets.
BRAIN_DIR = _PACKAGED_BRAIN if (_PACKAGED_BRAIN / "index.html").is_file() else Path(__file__).resolve().parents[2] / "brain"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
FRESH_S = 15.0                 # a completion/failure older than this is shown as calm, never replayed
POLL_S = 0.25
HEARTBEAT_S = 10.0
STARTUP_GRACE_S = 120.0        # on top of the mission's own exec timeout, before "never took a lease" counts as dead
CONNECTION_TIMEOUT_S = 30.0    # a client that stops sending cannot hold a handler thread indefinitely
MAX_STREAMS = 8
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/brain.js": ("brain.js", "text/javascript; charset=utf-8"),
    "/live.js": ("live.js", "text/javascript; charset=utf-8"),
    "/brain-texture.webp": ("brain-texture.webp", "image/webp"),
    "/brain-mask.webp": ("brain-mask.webp", "image/webp"),
}
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# Activity per tool effect class (heuristic: heavier effects animate more; not a measurement).
_EFFECT_ACTIVITY = {"read": 0.45, "external_read": 0.5, "write_workspace": 0.6, "execute": 0.8}
_TERMINAL = {"completed", "failed", "cancelled"}
_COMMAND_PATH = re.compile(r"^/api/missions(?:/(brain-[0-9]{14}-[0-9a-f]{6})/(approve|discard))?$")
_MAX_BODY = 2048


def _parse_at(value: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _state(state: str, activity: float, label: str) -> dict:
    return {"state": state, "activity": activity, "label": label}


def brain_state_for(event: dict, now: _dt.datetime) -> dict | None:
    """Map one stored runtime event to a visual state, or None when it carries no visual meaning.

    Success and error are shown only for a terminal transition that is fresh (younger than
    ``FRESH_S``); an older one maps to calm idle, so reconnecting never replays it.
    """
    kind = event.get("kind")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    at = _parse_at(event.get("at", ""))
    fresh = at is not None and 0 <= (now - at).total_seconds() <= FRESH_S
    if kind == "mission.state":
        target = payload.get("to")
        if target == "planning":
            return _state("thinking", 0.4, "planejando a missão")
        if target == "awaiting_approval":
            return _state("awaiting_approval", 0.15, "aguardando autorização do operador")
        if target == "running":
            return _state("working", 0.5, "missão em execução")
        if target == "verifying":
            return _state("verifying", 0.6, "verificação independente em andamento")
        if target == "completed":
            return _state("success", 0.7, "missão concluída e verificada") if fresh else \
                _state("idle", 0.25, "em repouso; última missão concluída")
        if target == "failed":
            return _state("error", 0.35, "missão falhou") if fresh else \
                _state("idle", 0.25, "em repouso; última missão falhou")
        if target == "cancelled":
            return _state("idle", 0.2, "missão cancelada")
        return None
    if kind == "mission.created":
        return _state("idle", 0.3, "missão criada")
    if kind == "plan.approved":
        return _state("idle", 0.3, "autorização registrada; aguardando execução")
    if kind == "agent.decision_requested":
        return _state("thinking", 0.55, "decidindo a próxima ação")
    if kind == "tool.authorization":
        if payload.get("decision") == "allow":
            return _state("working", _EFFECT_ACTIVITY.get(payload.get("effect"), 0.5), "executando ferramenta autorizada")
        return _state("error", 0.3, "ação negada pela política")
    if kind == "tool.result":
        exit_code = payload.get("exit_code")
        return _state("working", 0.65 if exit_code not in (None, 0) else 0.5, "observando o resultado")
    if kind == "step.outcome":
        return _state("working", 0.5, "passo concluído") if payload.get("ok") else _state("error", 0.3, "passo falhou")
    if kind in ("agent.verification_started", "verification.result"):
        return _state("verifying", 0.6, "verificação independente em andamento")
    if kind == "verification.baseline":
        return _state("working", 0.55, "reproduzindo o estado inicial")
    if kind == "agent.resumed":
        return _state("working", 0.5, "missão retomada do checkpoint")
    if kind in ("agent.provider_error", "execution.disabled"):
        return _state("error", 0.3, "falha registrada pelo runtime")
    return None


class EventReader:
    """Read-only view of the PETRON store. One instance per thread."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._db: sqlite3.Connection | None = None

    def _conn(self) -> sqlite3.Connection | None:
        if self._db is None:
            if not self.db_path.exists():
                return None
            self._db = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
            self._db.row_factory = sqlite3.Row
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def max_seq(self) -> int:
        db = self._conn()
        row = db.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM events").fetchone() if db else None
        return int(row["s"]) if row else 0

    def events_after(self, seq: int, limit: int = 500) -> list[dict]:
        db = self._conn()
        if db is None:
            return []
        rows = db.execute("SELECT seq, mission_id, at, kind, payload_json FROM events WHERE seq > ? ORDER BY seq "
                          "LIMIT ?", (seq, limit)).fetchall()
        return [{"seq": r["seq"], "mission_id": r["mission_id"], "at": r["at"], "kind": r["kind"],
                 "payload": json.loads(r["payload_json"])} for r in rows]

    def latest_mission(self) -> dict | None:
        db = self._conn()
        row = db.execute("SELECT mission_id, state, plan_sha256, spec_json, updated_at FROM missions "
                         "ORDER BY updated_at DESC LIMIT 1").fetchone() if db else None
        return dict(row) if row else None

    def active_brain_draft(self, bridge_id: str | None) -> dict | None:
        """The one non-terminal Brain mission this bridge created, or None.

        Ownership is the ``bridge`` recorded in the mission's ``brain.draft`` event: a draft made by
        another bridge (or by an older run without that field) is never reported, so it is never offered
        for approval here.
        """
        db = self._conn()
        if not bridge_id or db is None:
            return None
        rows = db.execute("SELECT mission_id, state, plan_sha256 FROM missions WHERE mission_id LIKE 'brain-%' "
                          "AND state NOT IN ('completed', 'failed', 'cancelled') ORDER BY created_at DESC").fetchall()
        for row in rows:
            kinds = {e["kind"]: e["payload"] for e in self.mission_events(row["mission_id"])
                     if e["kind"] in ("brain.draft", "plan.approved")}
            if kinds.get("brain.draft", {}).get("bridge") != bridge_id:
                continue
            draft = {"mission_id": row["mission_id"], "state": row["state"], "approved": "plan.approved" in kinds}
            if row["state"] == "awaiting_approval" and row["plan_sha256"]:
                draft["scope_sha256"] = row["plan_sha256"]
            return draft
        return None

    def mission_events(self, mission_id: str) -> list[dict]:
        db = self._conn()
        rows = db.execute("SELECT seq, mission_id, at, kind, payload_json FROM events WHERE mission_id = ? "
                          "ORDER BY seq", (mission_id,)).fetchall()
        return [{"seq": r["seq"], "mission_id": r["mission_id"], "at": r["at"], "kind": r["kind"],
                 "payload": json.loads(r["payload_json"])} for r in rows]

    def lease_alive(self, mission_id: str) -> bool | None:
        """V0.2 lease semantics, including releases and token ownership."""
        db = self._conn()
        row = db.execute(
            "SELECT kind, payload_json FROM events WHERE mission_id = ? "
            "AND kind IN ('run.lease', 'run.lease_released') ORDER BY seq DESC LIMIT 1",
            (mission_id,),
        ).fetchone() if db else None
        if row is None:
            return None
        if row["kind"] == "run.lease_released":
            return False
        holder = json.loads(row["payload_json"])
        return Store._holder_alive(mission_id, holder)


def _never_took_a_lease_and_start_expired(mission: dict, now: _dt.datetime) -> bool:
    """An agent run takes its lease after its start-up work (probe, copies, baseline run). Plan-mode runs
    never take one, so only an agent mission is judged, and only after that start-up work could have ended."""
    try:
        spec = json.loads(mission["spec_json"])
        timeout = float(spec["budget"]["exec_timeout_s"])
        agent = spec.get("mode") == "agent"
    except (KeyError, TypeError, ValueError):
        return False
    since = _parse_at(mission.get("updated_at", ""))
    return bool(agent and since is not None and (now - since).total_seconds() > timeout + STARTUP_GRACE_S)


def runner_interrupted(reader: EventReader, mission: dict, now: _dt.datetime) -> bool:
    """True when a mission that should have a live runner (running or verifying) has none.

    Uses the V0.2 lease semantics: a dead holder or a released lease on a non-terminal mission means the
    runner is gone; no lease at all is judged only for agent missions, after their start-up window."""
    if mission["state"] not in ("running", "verifying"):
        return False
    alive = reader.lease_alive(mission["mission_id"])
    if alive is None:
        return _never_took_a_lease_and_start_expired(mission, now)
    return alive is False


def snapshot(reader: EventReader, now: _dt.datetime | None = None, bridge_id: str | None = None) -> dict:
    """Current visual state from authoritative state. Never yields an old success or error.

    ``draft`` is the pending Brain draft that *this bridge* created (``bridge_id``), or None; the page
    reconciles its local draft against it, so a draft that ended elsewhere cannot linger."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    seq = reader.max_seq()
    mission = reader.latest_mission()
    draft = reader.active_brain_draft(bridge_id)
    if mission is None:
        return {"type": "snapshot", "seq": seq, "mission_id": None, "mission_state": None, "draft": draft,
                "interrupted": False, **_state("idle", 0.25, "em repouso; nenhuma missão registrada")}
    events = reader.mission_events(mission["mission_id"])
    state = mission["state"]
    interrupted = False
    visual = None
    if state in _TERMINAL:
        last = next((e for e in reversed(events) if e["kind"] == "mission.state"), None)
        visual = brain_state_for(last, now) if last else None
    elif runner_interrupted(reader, mission, now):
        interrupted = True
        visual = _state("idle", 0.2, "missão interrompida; retome pela CLI")
    else:
        for event in events:  # replay this mission's events; the last meaningful one wins
            mapped = brain_state_for(event, now)
            if mapped and mapped["state"] not in ("success", "error"):
                visual = mapped
    visual = visual or _state("idle", 0.25, "em repouso")
    return {"type": "snapshot", "seq": seq, "mission_id": mission["mission_id"], "mission_state": state,
            "interrupted": interrupted, "draft": draft, **visual}


def runtime_message(event: dict, now: _dt.datetime) -> dict:
    """What crosses the wire for one event: no payload content, only identifiers and the mapping."""
    mapped = brain_state_for(event, now) or {"state": None, "activity": None, "label": None}
    return {"type": "runtime", "seq": event["seq"], "mission_id": event["mission_id"], "kind": event["kind"],
            "at": event["at"], **mapped}


def _live_index(html: str, interactive: bool = False) -> str:
    marker = '<script src="./brain.js" defer></script>'
    if marker not in html:
        raise RuntimeError("index.html does not reference brain.js as expected")
    live = (marker + '\n    <script src="./live.js" defer></script>')
    html = (html.replace(marker, live)
            .replace('<meta name="color-scheme" content="dark">',
                     '<meta name="color-scheme" content="dark">\n    <meta name="petron-mode" content="live">')
            .replace("PETRON em modo visual autônomo, sem conexão com o runtime.", "PETRON conectando ao runtime."))
    if not interactive:
        return html   # read-only bridge: no input element exists at all
    return html.replace('<div id="petron-status"',
                        '<textarea id="petron-input" class="petron-status" aria-label="Pedido para o PETRON" '
                        'autocomplete="off" autocapitalize="sentences" spellcheck="false" maxlength="500" '
                        'rows="1" tabindex="-1"></textarea>\n      <div id="petron-status"')


class BrainServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, home: Path, host: str = "127.0.0.1", port: int = 8765, commands=None,
                 secret: str | None = None):
        if host not in LOOPBACK:
            raise ValueError("the Brain bridge binds to a loopback address only")
        if commands is not None and (not secret or len(secret) < 32):
            raise ValueError("interactive mode needs a strong per-run secret")
        self.commands = commands
        self.secret = secret
        family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        self.address_family = family
        self.db_path = Path(home) / "petron.sqlite3"
        self.stopping = threading.Event()
        self.streams = threading.Semaphore(MAX_STREAMS)
        self.bridge_id = commands.bridge_id if commands is not None else None
        # The name `localhost` is never handed to the resolver: a hosts/NSS override could point it at an
        # external interface. The loopback literal is bound instead; the Host guard still accepts the name.
        super().__init__(("127.0.0.1" if host == "localhost" else host, port), BrainHandler)
        self.port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()


class BrainHandler(BaseHTTPRequestHandler):
    server: BrainServer
    protocol_version = "HTTP/1.1"
    server_version = "petron-brain"
    sys_version = ""
    timeout = CONNECTION_TIMEOUT_S

    def log_message(self, format, *args) -> None:  # noqa: A002 - keep local logs quiet and bounded
        return

    def _headers(self, status: int, content_type: str, length: int | None = None, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if length is not None:
            self.send_header("Content-Length", str(length))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

    def _refuse(self, status: int) -> None:
        # A refusal may leave the request body unread. On a persistent HTTP/1.1 connection those bytes would
        # be parsed as the next request, so every refusal ends the connection.
        self.close_connection = True
        body = status.phrase.encode() if isinstance(status, HTTPStatus) else b"refused"
        self._headers(int(status), "text/plain; charset=utf-8", len(body))
        if self.command != "HEAD":
            self.wfile.write(body)

    def _guard(self) -> bool:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._refuse(HTTPStatus.FORBIDDEN)
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            self._refuse(HTTPStatus.FORBIDDEN)
            return False
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            self._refuse(HTTPStatus.FORBIDDEN)
            return False
        return True

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        url = urlsplit(self.path)
        if url.path in _ASSETS:
            return self._asset(url.path)
        if url.path == "/favicon.ico":  # browsers ask on their own; answer with nothing, not a 404
            return self._headers(204, "image/x-icon", 0, cache="max-age=86400")
        if url.path == "/api/snapshot":
            reader = EventReader(self.server.db_path)
            try:
                body = json.dumps({**snapshot(reader, bridge_id=self.server.bridge_id),
                                   "interactive": self.server.commands is not None}).encode()
            finally:
                reader.close()
            self._headers(200, "application/json", len(body))
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if url.path == "/api/events":
            return self._events(parse_qs(url.query))
        self._refuse(HTTPStatus.NOT_FOUND)

    def _refuse_method(self) -> None:
        self._refuse(HTTPStatus.METHOD_NOT_ALLOWED)

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse_method  # noqa: N815

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self._headers(status, "application/json", len(body))
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        commands = self.server.commands
        if commands is None:
            return self._refuse(HTTPStatus.METHOD_NOT_ALLOWED)   # read-only unless --interactive
        if not self._guard():
            return
        if self.headers.get("Origin") not in self.server.allowed_origins:   # required, not just tolerated
            return self._refuse(HTTPStatus.FORBIDDEN)
        if self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            return self._refuse(HTTPStatus.FORBIDDEN)
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._refuse(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        key = self.headers.get("X-Petron-Key") or ""
        if not hmac.compare_digest(key.encode(), self.server.secret.encode()):
            return self._refuse(HTTPStatus.FORBIDDEN)
        match = _COMMAND_PATH.fullmatch(urlsplit(self.path).path)
        if match is None:
            return self._refuse(HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._refuse(HTTPStatus.BAD_REQUEST)
        if length < 2 or length > _MAX_BODY:
            return self._refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE if length > _MAX_BODY else HTTPStatus.BAD_REQUEST)
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self._refuse(HTTPStatus.BAD_REQUEST)
        mission_id, action = match.groups()
        expected = {None: {"objective"}, "approve": {"scope_sha256"}, "discard": set()}[action]
        if not isinstance(body, dict) or set(body) != expected:
            return self._json(400, {"error": "invalid_request"})
        from .brain_commands import CommandRefused
        try:
            if action is None:
                result = commands.create_draft(body["objective"])
            elif action == "approve":
                result = commands.approve_and_run(mission_id, body["scope_sha256"])
            else:
                result = commands.discard(mission_id)
        except CommandRefused as refused:
            return self._json(refused.status, {"error": refused.code})
        except Exception:   # a controlled answer, never a dropped connection; nothing about the failure is echoed
            self.close_connection = True
            return self._json(500, {"error": "internal_error"})
        self._json(200, result)

    def _asset(self, path: str) -> None:
        name, content_type = _ASSETS[path]
        data = (BRAIN_DIR / name).read_bytes()
        if name == "index.html":
            data = _live_index(data.decode("utf-8"), self.server.commands is not None).encode("utf-8")
        self._headers(200, content_type, len(data), cache="no-cache")
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send(self, event: str, message: dict, seq: int | None) -> None:
        chunk = (f"id: {seq}\n" if seq is not None else "") + f"event: {event}\ndata: {json.dumps(message)}\n\n"
        self.wfile.write(chunk.encode())
        self.wfile.flush()

    def _events(self, query: dict) -> None:
        if self.command == "HEAD":
            return self._headers(200, "text/event-stream")
        if not self.server.streams.acquire(blocking=False):
            return self._refuse(HTTPStatus.SERVICE_UNAVAILABLE)
        reader = EventReader(self.server.db_path)
        try:
            self.send_response(200)
            for name, value in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"),
                                ("Content-Security-Policy", _CSP), ("X-Content-Type-Options", "nosniff"),
                                ("Connection", "close")):
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            self.wfile.write(b"retry: 2000\n\n")
            resume = self.headers.get("Last-Event-ID") or (query.get("after") or [None])[0]
            last = int(resume) if resume and str(resume).isdigit() else None
            now = _dt.datetime.now(_dt.timezone.utc)
            if last is not None:  # reconnect: catch up (freshness-guarded), then the authoritative snapshot
                for event in reader.events_after(last, limit=1000):
                    self._send("runtime", runtime_message(event, now), event["seq"])
            current = {**snapshot(reader, now, self.server.bridge_id),
                       "interactive": self.server.commands is not None}
            self._send("snapshot", current, current["seq"])
            last = current["seq"]
            watched = current.get("mission_id")
            beat = time.monotonic()
            while not self.server.stopping.is_set():
                time.sleep(POLL_S)
                now = _dt.datetime.now(_dt.timezone.utc)
                for event in reader.events_after(last):
                    self._send("runtime", runtime_message(event, now), event["seq"])
                    last = event["seq"]
                    watched = event["mission_id"] or watched
                if time.monotonic() - beat >= HEARTBEAT_S:
                    beat = time.monotonic()
                    status = snapshot(reader, now)  # catches a runner that died without a final event
                    if status["interrupted"] and status["mission_id"] == watched:
                        self._send("status", {**status, "type": "status"}, None)
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            reader.close()
            self.server.streams.release()


def serve(home: Path, host: str = "127.0.0.1", port: int = 8765, commands=None,
          secret: str | None = None) -> BrainServer:
    """Build the server (not started). Callers run ``serve_forever`` and ``shutdown``."""
    if not (BRAIN_DIR / "index.html").is_file():
        raise FileNotFoundError("Brain assets not found next to the package (apps/petron/brain)")
    return BrainServer(Path(home), host, port, commands, secret)
