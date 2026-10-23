"""End-to-end proof of mission entry through the Brain, in a real browser.

    python apps/petron/tests/e2e/brain_interaction_e2e.py --out /tmp/brain-interaction

Real `petron brain serve --interactive` process with the E002 template (Docker sandbox), a real
Chromium page operated only by pointer and keyboard on the sphere. Checks that typing creates a
draft that waits, that a tap or an interrupted hold approves nothing, that a full hold approves and
the runtime runs and verifies the mission, that Escape discards, and that a page without the
per-run key (or a read-only bridge) cannot act. It also proves that a draft which ended elsewhere
never blocks the page (a CLI cancel while connected, and while the event stream is down), and that a
run the runtime refuses after approval ends as a truthful failure instead of a phantom "running".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PETRON = Path(__file__).resolve().parents[2]
TEMPLATE = PETRON / "fixtures" / "e002" / "mission.json"


def provenance() -> dict:
    """Which code these results describe: the commit under test, and whether the tree had local edits."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=PETRON, capture_output=True, text=True, timeout=30).stdout.strip()
    try:
        return {"head": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--", ".")),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class E2E:
    def __init__(self, out: Path):
        self.out = out
        self.home = Path(tempfile.mkdtemp(prefix="petron-brain-interaction-"))
        self.env = {**os.environ, "PYTHONPATH": str(PETRON / "src"), "PETRON_HOME": str(self.home)}
        self.checks: dict = {}

    def check(self, name, ok, detail=None):
        self.checks[name] = {"ok": bool(ok), **({"detail": detail} if detail is not None else {})}
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail is not None else ""), flush=True)

    def bridge(self, port: int, interactive: bool, template: Path = TEMPLATE) -> tuple[subprocess.Popen, str]:
        args = [sys.executable, "-B", "-m", "petron", "brain", "serve", "--port", str(port)]
        if interactive:
            args += ["--interactive", "--template", str(template)]
        proc = subprocess.Popen(args, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        line = proc.stdout.readline()
        match = re.search(r"(http://127\.0\.0\.1:\d+/(?:#k=[A-Za-z0-9_-]+)?)", line)
        if not match:
            raise RuntimeError(f"bridge did not start: {line}")
        return proc, match.group(1)

    def events(self, mission_id: str) -> list[tuple[str, dict]]:
        db = sqlite3.connect(f"{(self.home / 'petron.sqlite3').as_uri()}?mode=ro", uri=True)
        rows = db.execute("SELECT kind, payload_json FROM events WHERE mission_id = ? ORDER BY seq",
                          (mission_id,)).fetchall()
        db.close()
        return [(kind, json.loads(payload)) for kind, payload in rows]

    def missions(self) -> list[tuple[str, str]]:
        db = sqlite3.connect(f"{(self.home / 'petron.sqlite3').as_uri()}?mode=ro", uri=True)
        rows = db.execute("SELECT mission_id, state FROM missions ORDER BY created_at").fetchall()
        db.close()
        return rows


class Proxy:
    """A TCP forwarder in front of the bridge that can be cut and restored, to lose the event stream for real
    (browser offline emulation does not close an open stream). It rewrites only the proxy's own host:port in
    the request headers so the bridge's Host/Origin guards see their own address."""

    def __init__(self, target_port: int):
        self.target, self.up, self.conns = target_port, True, []
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            if not self.up:
                client.close()
                continue
            upstream = socket.create_connection(("127.0.0.1", self.target))
            self.conns += [client, upstream]
            threading.Thread(target=self._pipe, args=(client, upstream, True), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client, False), daemon=True).start()

    def _pipe(self, source, sink, rewrite):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                if rewrite:
                    data = data.replace(f"127.0.0.1:{self.port}".encode(), f"127.0.0.1:{self.target}".encode())
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                try:
                    sock.close()
                except OSError:
                    pass

    def cut(self):
        self.up = False
        for sock in self.conns:
            try:
                sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            except OSError:
                pass
        self.conns = []

    def restore(self):
        self.up = True


def mode(page) -> str:
    return page.evaluate("window.PetronBrain.getState().mode")


def wait_mode(page, wanted, timeout_s):
    deadline = time.time() + timeout_s
    current = None
    while time.time() < deadline:
        current = mode(page)
        if current in wanted:
            return current
        time.sleep(0.05)
    return current


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chromium", default="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    run = E2E(args.out)
    port = free_port()
    bridge, url = run.bridge(port, interactive=True)
    base = url.split("#")[0]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.chromium, headless=True,
                                     args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_timeout(1200)
        center = {"x": 640, "y": 430}

        run.check("key_removed_from_address_bar", page.evaluate("location.hash") == "" and "#k=" in url)
        dom = page.evaluate("""() => ({
          visible: [...document.querySelectorAll('main > *')].filter(e => { const r = e.getBoundingClientRect();
            return r.width > 2 && r.height > 2; }).map(e => e.tagName.toLowerCase()),
          controls: document.querySelectorAll('button,nav,a,input,select,form,dialog').length })""")
        run.check("still_only_the_brain_visible", dom["visible"] == ["img", "div", "canvas"]
                  and dom["controls"] == 0, dom)

        # 1. Tap opens an invisible input session; typing; Enter creates a draft that waits.
        page.mouse.click(**center)
        run.check("tap_opens_listening_session", wait_mode(page, {"listening"}, 2) == "listening")
        page.screenshot(path=str(args.out / "1-listening.png"))
        page.keyboard.type("Corrija o defeito que faz os testes de intervalos falharem", delay=15)
        run.check("typed_text_is_not_visible", page.evaluate(
            "(() => { const r = document.getElementById('petron-input').getBoundingClientRect(); "
            "return r.width <= 1 && r.height <= 1; })()"))
        page.keyboard.press("Enter")
        run.check("request_becomes_draft_awaiting_approval",
                  wait_mode(page, {"awaiting_approval"}, 6) == "awaiting_approval", run.missions())
        mission_id = run.missions()[-1][0]
        kinds = [k for k, _ in run.events(mission_id)]
        run.check("draft_not_approved_or_running", "brain.draft" in kinds and "plan.approved" not in kinds
                  and "run.lease" not in kinds, mission_id)

        # 2. A tap and an interrupted hold approve nothing.
        page.mouse.click(**center)
        page.wait_for_timeout(500)
        page.mouse.move(**center)
        page.mouse.down()
        run.check("hold_shows_confirming", wait_mode(page, {"confirming"}, 1) == "confirming")
        page.screenshot(path=str(args.out / "2-confirming.png"))
        page.wait_for_timeout(600)
        page.mouse.up()
        page.wait_for_timeout(800)
        kinds = [k for k, _ in run.events(mission_id)]
        run.check("tap_and_short_hold_approve_nothing", "plan.approved" not in kinds
                  and mode(page) == "awaiting_approval", mode(page))

        # 3. A full hold approves the exact draft; the runtime runs and verifies it.
        page.mouse.down()
        page.wait_for_timeout(2200)
        page.mouse.up()
        final = wait_mode(page, {"success"}, 90)
        events = run.events(mission_id)
        kinds = [k for k, _ in events]
        approval = next((p for k, p in events if k == "plan.approved"), {})
        running_at = next((i for i, (k, p) in enumerate(events) if k == "mission.state" and p.get("to") == "running"), -1)
        order_ok = "plan.approved" in kinds and 0 <= kinds.index("plan.approved") < running_at
        run.check("full_hold_approves_and_runtime_completes", final == "success" and order_ok
                  and approval.get("channel") == "brain-hold-gesture" and run.missions()[-1][1] == "completed",
                  {"final_mode": final, "approval_channel": approval.get("channel"),
                   "mission": run.missions()[-1]})
        transitions = [(p.get("from"), p.get("to")) for k, p in events if k == "mission.state"]
        run.check("success_after_verification", "verification.result" in kinds
                  and ("verifying", "completed") in transitions
                  and kinds.index("verification.result") < len(kinds) - 1, transitions[-2:])
        page.wait_for_timeout(5000)

        # 4. Keyboard only, from a fresh page: Tab reaches the Brain; Enter, type, Enter; Escape discards.
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_timeout(1200)
        page.keyboard.press("Tab")
        run.check("tab_focuses_the_brain", page.evaluate("document.activeElement.id") == "brain",
                  page.evaluate("document.activeElement.id"))
        page.keyboard.press("Enter")
        run.check("keyboard_opens_session", wait_mode(page, {"listening"}, 2) == "listening")
        page.keyboard.type("Outra tarefa", delay=10)
        page.keyboard.press("Enter")
        wait_mode(page, {"awaiting_approval"}, 6)
        second = run.missions()[-1][0]
        page.keyboard.press("Escape")
        page.wait_for_timeout(1200)
        run.check("escape_discards_draft", dict(run.missions())[second] == "cancelled"
                  and "plan.approved" not in [k for k, _ in run.events(second)],
                  {"state": dict(run.missions())[second],
                   "said_after": page.evaluate("document.getElementById('petron-status').textContent"),
                   "focus": page.evaluate("document.activeElement.id")})

        # 5. Keyboard hold (Space) approves a new draft.
        page.keyboard.press("Enter")
        wait_mode(page, {"listening"}, 2)
        page.keyboard.type("Terceira tarefa", delay=10)
        page.keyboard.press("Enter")
        wait_mode(page, {"awaiting_approval"}, 6)
        third = run.missions()[-1][0]
        page.keyboard.down(" ")
        page.wait_for_timeout(2200)
        page.keyboard.up(" ")
        run.check("space_hold_approves", wait_mode(page, {"success"}, 90) == "success"
                  and dict(run.missions())[third] == "completed",
                  {"state": dict(run.missions())[third]})

        # 6. A palette chosen in the URL survives the key's removal; entry still works.
        colored = browser.new_page(viewport={"width": 1000, "height": 700})
        colored.on("pageerror", lambda e: errors.append(str(e)))
        colored.goto(url.replace("/#k=", "/?palette=spectrum#k="))
        colored.wait_for_timeout(1000)
        address = colored.evaluate("[location.search, location.hash]")
        colored.mouse.click(500, 350)
        run.check("palette_query_survives_key_removal", address == ["?palette=spectrum", ""]
                  and colored.evaluate("window.PetronBrain.getPalette()") == "spectrum"
                  and wait_mode(colored, {"listening"}, 2) == "listening", address)
        colored.keyboard.press("Escape")

        # 9. A draft that ended elsewhere never blocks the page.
        proxy = Proxy(port)
        through = browser.new_page(viewport={"width": 1000, "height": 700})
        through.on("pageerror", lambda e: errors.append(str(e)))
        proxied_url = url.replace(f":{port}/", f":{proxy.port}/")
        through.goto(proxied_url)
        through.wait_for_timeout(1200)

        def new_draft(page_, text):
            page_.mouse.click(500, 350)
            wait_mode(page_, {"listening"}, 3)
            page_.keyboard.type(text, delay=10)
            page_.keyboard.press("Enter")
            wait_mode(page_, {"awaiting_approval"}, 6)
            return run.missions()[-1][0]

        def cli_stop(mission_id):
            return subprocess.run([sys.executable, "-B", "-m", "petron", "mission", "stop", mission_id],
                                  env=run.env, capture_output=True, text=True, timeout=60)

        stale = new_draft(through, "Rascunho que será cancelado fora do navegador")
        cli_stop(stale)                                              # an operator cancels it in the terminal
        run.check("external_cancel_reaches_the_page", wait_mode(through, {"idle"}, 6) == "idle"
                  and dict(run.missions())[stale] == "cancelled", dict(run.missions())[stale])
        through.mouse.click(500, 350)
        run.check("new_mission_possible_after_external_cancel", wait_mode(through, {"listening"}, 3) == "listening",
                  mode(through))
        through.keyboard.press("Escape")
        through.wait_for_timeout(300)

        offline = new_draft(through, "Rascunho cancelado enquanto o fluxo de eventos está fora")
        proxy.cut()
        run.check("lost_stream_is_shown_as_disconnected", wait_mode(through, {"disconnected"}, 10) == "disconnected",
                  mode(through))
        cli_stop(offline)                                            # it ends while the page cannot hear anything
        proxy.restore()
        run.check("stream_returns_with_the_authoritative_state", wait_mode(through, {"idle"}, 15) == "idle"
                  and dict(run.missions())[offline] == "cancelled", mode(through))
        through.mouse.click(500, 350)
        run.check("stale_draft_does_not_block_after_reconnect", wait_mode(through, {"listening"}, 3) == "listening",
                  mode(through))
        through.keyboard.type("Nova missão depois da reconexão", delay=10)
        through.keyboard.press("Enter")
        run.check("new_mission_created_after_reconnect",
                  wait_mode(through, {"awaiting_approval"}, 6) == "awaiting_approval"
                  and dict(run.missions())[run.missions()[-1][0]] == "awaiting_approval", run.missions()[-2:])
        through.keyboard.press("Escape")
        through.wait_for_timeout(500)
        proxy.cut()

        # 10. A run the runtime refuses after approval is a truthful failure, never "running".
        refusing = Path(tempfile.mkdtemp(prefix="petron-refusing-template-")) / "template.json"
        template = json.loads(TEMPLATE.read_text())
        template["workspace"]["source"] = str(PETRON / "fixtures" / "e002" / "project")
        template["provider"] = {"kind": "deterministic", "script": "no_such_policy"}
        refusing.write_text(json.dumps(template))
        bridge3, url3 = run.bridge(free_port(), interactive=True, template=refusing)
        refused_page = browser.new_page(viewport={"width": 1000, "height": 700})
        refused_page.on("pageerror", lambda e: errors.append(str(e)))
        refused_page.goto(url3)
        refused_page.wait_for_timeout(1200)
        refused_page.evaluate("""() => { window.__modes = [];
          setInterval(() => { const m = window.PetronBrain.getState().mode;
            if (!window.__modes.length || window.__modes[window.__modes.length - 1] !== m) window.__modes.push(m); }, 20); }""")
        refused_id = new_draft(refused_page, "Missão que o runtime vai recusar")
        refused_page.mouse.move(500, 350)
        refused_page.mouse.down()
        refused_page.wait_for_timeout(2200)
        refused_page.mouse.up()
        refused_page.wait_for_timeout(2500)
        seen = refused_page.evaluate("window.__modes")
        events = run.events(refused_id)
        kinds = [k for k, _ in events]
        refusal = next((p for k, p in events if k == "brain.run_refused"), {})
        run.check("refused_run_ends_failed_with_an_auditable_event",
                  dict(run.missions())[refused_id] == "failed" and "plan.approved" in kinds
                  and kinds.index("plan.approved") < kinds.index("brain.run_refused")
                  and refusal.get("code") == "provider_error:unknown_policy", {"mission": dict(run.missions())[refused_id],
                                                                            "refusal": refusal})
        run.check("brain_never_shows_a_refused_run_as_running", "error" in seen
                  and not ({"working", "verifying", "success"} & set(seen)), seen)
        refused_page.wait_for_timeout(8000)
        run.check("terminal_failure_settles_to_calm", mode(refused_page) == "idle", mode(refused_page))
        refused_page.mouse.click(500, 350)
        run.check("new_mission_possible_after_a_refused_run", wait_mode(refused_page, {"listening"}, 3) == "listening",
                  mode(refused_page))
        refused_page.keyboard.press("Escape")
        bridge3.terminate()
        bridge3.wait(timeout=10)

        # 7. Without the key, the same server cannot be driven.
        nokey = browser.new_page(viewport={"width": 1000, "height": 700})
        nokey.goto(base)
        nokey.wait_for_timeout(1000)
        nokey.mouse.click(500, 350)
        nokey.wait_for_timeout(600)
        run.check("page_without_key_cannot_open_input", mode(nokey) != "listening", mode(nokey))
        status = nokey.evaluate("""async () => (await fetch('/api/missions', {method: 'POST',
          headers: {'Content-Type': 'application/json'}, body: JSON.stringify({objective: 'x'})})).status""")
        run.check("post_without_key_refused", status == 403, status)
        run.check("no_page_errors", not errors, errors[:3])
        browser.close()
    bridge.terminate()
    bridge.wait(timeout=10)

    # 8. Read-only bridge: nothing to act on.
    port2 = free_port()
    readonly, url2 = run.bridge(port2, interactive=False)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.chromium, headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        page.goto(url2)
        page.wait_for_timeout(1000)
        page.mouse.click(400, 300)
        page.wait_for_timeout(500)
        status = page.evaluate("""async () => (await fetch('/api/missions', {method: 'POST',
          headers: {'Content-Type': 'application/json'}, body: '{}'})).status""")
        run.check("read_only_bridge_has_no_commands", mode(page) != "listening" and status == 405,
                  {"mode": mode(page), "post": status})
        browser.close()
    readonly.terminate()
    readonly.wait(timeout=10)
    ok = all(c["ok"] for c in run.checks.values())
    (args.out / "results.json").write_text(json.dumps({"ok": ok, "provenance": provenance(), "checks": run.checks},
                                                          indent=2))
    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}: {len(run.checks)} checks; evidence in {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
