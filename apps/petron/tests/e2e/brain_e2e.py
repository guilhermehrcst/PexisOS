"""End-to-end proof: the Brain follows real PETRON runtime events in a real browser.

Not part of the unit suite (needs Playwright, a Chromium binary and Docker). Run from the repo root:

    python apps/petron/tests/e2e/brain_e2e.py --out /tmp/brain-e2e [--chromium PATH]

Everything acted on is real: a `petron brain serve` process, PETRON-E002 missions created,
approved and run through the CLI (Docker sandbox), a failing mission, and the bridge process
being killed and restarted. The page only observes: it never calls the runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import sync_playwright

PETRON = Path(__file__).resolve().parents[2]          # apps/petron
FIXTURE = PETRON / "fixtures" / "e002"


def tint(path: Path) -> dict:
    """Mean saturation, and red minus blue over the lit pixels (warm > 0 > cool)."""
    image = Image.open(path).convert("RGB")
    lit = [px for px in image.getdata() if sum(px) > 90]
    sat = ImageStat.Stat(image.convert("HSV").split()[1]).mean[0]
    warm = sum(r - b for r, _, b in lit) / max(1, len(lit))
    return {"saturation": round(sat, 2), "warm": round(warm, 2)}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Run:
    def __init__(self, home: Path, out: Path):
        self.home, self.out = home, out
        self.env = {**os.environ, "PYTHONPATH": str(PETRON / "src"), "PETRON_HOME": str(home)}
        self.results: dict = {"checks": {}, "notes": []}

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-B", "-m", "petron", *args], env=self.env, capture_output=True,
                              text=True, timeout=600)

    def check(self, name: str, ok: bool, detail=None) -> None:
        self.results["checks"][name] = {"ok": bool(ok), **({"detail": detail} if detail is not None else {})}
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail is not None else ""), flush=True)

    def start_bridge(self, port: int) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-B", "-m", "petron", "brain", "serve", "--port", str(port)],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return proc
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("bridge did not start")


RECORDER = """() => {
  window.__modes = [{t: performance.now(), mode: window.PetronBrain.getState().mode}];
  window.__wire = [];
  setInterval(() => {
    const m = window.PetronBrain.getState().mode;
    if (m !== window.__modes[window.__modes.length - 1].mode) window.__modes.push({t: performance.now(), mode: m});
  }, 20);
  const es = new EventSource('/api/events');   // a second, observing subscriber (same origin)
  for (const kind of ['snapshot', 'runtime', 'status'])
    es.addEventListener(kind, (e) => { const d = JSON.parse(e.data); window.__wire.push({event: kind, seq: d.seq,
      kind: d.kind || null, state: d.state, mission: d.mission_id}); });
}"""


def wait_mode(page, wanted, timeout_s: float) -> str:
    deadline = time.time() + timeout_s
    mode = None
    while time.time() < deadline:
        mode = page.evaluate("window.PetronBrain.getState().mode")
        if mode in wanted:
            return mode
        time.sleep(0.05)
    return mode


def modes_since(page, index: int) -> list[str]:
    return [m["mode"] for m in page.evaluate("window.__modes")[index:]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chromium", default="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="petron-brain-e2e-"))
    run = Run(home, args.out)
    port = free_port()
    bridge = run.start_bridge(port)
    url = f"http://127.0.0.1:{port}/"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.chromium, headless=True,
                                     args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors, requests = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("request", lambda r: requests.append(r.url))
        page.goto(url)
        page.wait_for_timeout(1200)
        page.evaluate(RECORDER)

        # 1. Only the Brain is visible.
        dom = page.evaluate("""() => ({
          children: [...document.querySelector('main').children].map(e => e.tagName.toLowerCase()
            + (e.id ? '#' + e.id : e.className ? '.' + e.className : '')),
          controls: document.querySelectorAll('button,nav,a,input,textarea,select,form,dialog').length,
          status: (() => { const r = document.getElementById('petron-status').getBoundingClientRect(); return [r.width, r.height]; })(),
          scroll: document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight,
          live: document.querySelector('meta[name=petron-mode]')?.content })""")
        run.check("only_brain_visible", dom["controls"] == 0 and dom["status"][0] <= 1 and not dom["scroll"]
                  and dom["children"] == ["img.brain-reference", "div.brain-chroma", "canvas#brain",
                                          "div#petron-status"], dom)
        page.screenshot(path=str(args.out / "1-idle-desktop.png"))
        time.sleep(0.6)
        page.screenshot(path=str(args.out / "1b-idle-desktop.png"))
        a = Image.open(args.out / "1-idle-desktop.png").convert("L")
        b = Image.open(args.out / "1b-idle-desktop.png").convert("L")
        run.check("brain_rendered_and_animating", ImageStat.Stat(a).mean[0] > 3 and
                  ImageChops.difference(a, b).getbbox() is not None, round(ImageStat.Stat(a).mean[0], 2))
        run.check("initial_state_idle", page.evaluate("window.PetronBrain.getState().mode") == "idle")
        run.check("default_palette_adaptive", page.evaluate("window.PetronBrain.getPalette()") == "adaptive")

        # 2. Mission created through the CLI -> awaiting approval.
        created = run.cli("experiment", "e002")
        scope = json.loads(created.stdout)["scope_sha256"]
        run.check("awaiting_approval_shown", wait_mode(page, {"awaiting_approval"}, 5) == "awaiting_approval")
        page.screenshot(path=str(args.out / "2-awaiting-approval.png"))
        run.cli("mission", "approve", "petron-e002", "--scope-sha256", scope, "--approver", "e2e-harness")
        run.check("approval_recorded_is_not_execution", wait_mode(page, {"idle"}, 5) == "idle")

        # 3. Run E002 (real Docker sandbox) and watch.
        start = len(page.evaluate("window.__modes"))
        wire_start = len(page.evaluate("window.__wire"))
        result = run.cli("mission", "run", "petron-e002")
        run.check("e002_cli_completed", result.returncode == 0, result.stdout.strip().splitlines()[-4:])
        page.wait_for_timeout(1000)
        seen = modes_since(page, start)
        run.check("brain_followed_live_states", {"working", "verifying", "success"} <= set(seen), seen)
        wire = page.evaluate("window.__wire")[wire_start:]
        kinds = [w["kind"] for w in wire]
        first_success = next((i for i, w in enumerate(wire) if w["state"] == "success"), None)
        verified = [i for i, w in enumerate(wire) if w["kind"] == "verification.result"]
        run.check("success_only_after_runtime_verification",
                  first_success is not None and verified and verified[0] < first_success,
                  {"first_success_index": first_success, "verification_result_index": verified[:1]})
        states = [w["state"] for w in wire if w["event"] == "runtime"]
        run.check("decisions_streamed", kinds.count("agent.decision_requested") >= 10,
                  {"decision_requested": kinds.count("agent.decision_requested"),
                   "tool_authorizations": kinds.count("tool.authorization"),
                   "thinking_messages_on_wire": states.count("thinking"),
                   "note": "deterministic decisions take microseconds, so 'thinking' is on the wire but rarely "
                           "visible on screen; a real model's decision time would keep it visible"})
        seqs = [w["seq"] for w in wire if w["event"] == "runtime"]
        run.check("events_in_order_no_duplicates", seqs == sorted(set(seqs)), len(seqs))
        run.check("success_returns_to_calm", wait_mode(page, {"idle"}, 7) == "idle")

        # 4. A failing mission never looks successful.
        spec = json.loads((FIXTURE / "mission.json").read_text())
        spec.update(mission_id="e2e-failure")
        spec["workspace"]["source"] = str(FIXTURE / "project")
        spec["provider"] = {"kind": "deterministic", "script": "e002_cheater"}
        spec_path = home / "failure.json"
        spec_path.write_text(json.dumps(spec))
        run.cli("mission", "create", str(spec_path))
        fail_scope = json.loads(run.cli("mission", "plan", "e2e-failure").stdout)["plan_sha256"]
        run.cli("mission", "approve", "e2e-failure", "--scope-sha256", fail_scope, "--approver", "e2e-harness")
        start = len(page.evaluate("window.__modes"))
        failed = run.cli("mission", "run", "e2e-failure")
        page.wait_for_timeout(800)
        seen = modes_since(page, start)
        run.check("failure_shows_error_never_success", failed.returncode == 1 and "error" in seen
                  and "success" not in seen, {"modes": seen, "cli": failed.stdout.strip().splitlines()[-2:]})
        page.wait_for_timeout(2500)   # let the color settle (same ~0.6 s easing as motion)
        page.screenshot(path=str(args.out / "4-error.png"))

        # 5. Bridge killed -> disconnected, and nothing else, until it returns.
        errors_before_disconnect = list(errors)
        bridge.terminate()
        bridge.wait(timeout=10)
        run.check("disconnect_detected", wait_mode(page, {"disconnected"}, 6) == "disconnected")
        start = len(page.evaluate("window.__modes"))
        page.wait_for_timeout(3000)
        run.check("no_fake_activity_while_disconnected", set(modes_since(page, start)) <= {"disconnected"},
                  modes_since(page, start))
        page.screenshot(path=str(args.out / "5-disconnected.png"))
        colors = {name: tint(args.out / f) for name, f in
                  (("idle", "1-idle-desktop.png"), ("error", "4-error.png"), ("disconnected", "5-disconnected.png"))}
        run.check("adaptive_color_follows_real_states", colors["idle"]["warm"] < 0
                  and colors["error"]["warm"] - colors["idle"]["warm"] > 8      # error clearly warmer than idle
                  and colors["disconnected"]["saturation"] < min(colors["idle"]["saturation"],
                                                                colors["error"]["saturation"]), colors)

        # 6. Bridge restarted -> recovers the authoritative state, no success replay.
        bridge = run.start_bridge(port)
        start = len(page.evaluate("window.__modes"))
        recovered = wait_mode(page, {"idle", "error", "awaiting_approval"}, 10)
        page.wait_for_timeout(1500)
        seen = modes_since(page, start)
        run.check("reconnect_recovers_state_without_replaying_success", recovered in {"idle", "error"}
                  and "success" not in seen, {"recovered": recovered, "modes": seen})

        unexpected = errors_before_disconnect + [e for e in errors[len(errors_before_disconnect):]
                                                 if "ERR_CONNECTION_REFUSED" not in e]
        run.check("no_page_errors_outside_the_deliberate_outage", not unexpected,
                  {"unexpected": unexpected[:5], "expected_refused_during_outage": len(errors) - len(unexpected)})
        run.check("only_local_requests", all(r.startswith(url) for r in requests), sorted(set(requests))[:8])

        # 7. Responsive and reduced-motion.
        mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, has_touch=True)
        mobile.goto(url)
        mobile.wait_for_timeout(1200)
        info = mobile.evaluate("({scroll: document.documentElement.scrollWidth > innerWidth || "
                               "document.documentElement.scrollHeight > innerHeight, "
                               "canvas: [document.getElementById('brain').width, document.getElementById('brain').height]})")
        mobile.screenshot(path=str(args.out / "7-mobile.png"))
        mobile.tap("canvas#brain")  # touch gives a pulse; it never triggers runtime actions
        run.check("mobile_layout", not info["scroll"] and info["canvas"][0] > 0, info)
        reduced = browser.new_page(viewport={"width": 1200, "height": 800}, reduced_motion="reduce")
        reduced.goto(url)
        reduced.wait_for_timeout(600)
        run.check("reduced_motion_respected", reduced.evaluate("window.PetronBrain.getState().reducedMotion"))
        mono = browser.new_page(viewport={"width": 1200, "height": 800})
        mono.goto(url + "?palette=mono")
        mono.wait_for_timeout(1500)
        mono.screenshot(path=str(args.out / "8-mono.png"))
        run.check("mono_palette_stays_monochrome", mono.evaluate("window.PetronBrain.getPalette()") == "mono"
                  and tint(args.out / "8-mono.png")["saturation"] < 12, tint(args.out / "8-mono.png"))
        page.keyboard.press("Tab")
        page.keyboard.press("Enter")
        run.check("keyboard_focus_and_status", page.evaluate("document.activeElement.id") == "brain"
                  and page.evaluate("document.getElementById('petron-status').textContent").startswith("PETRON:"),
                  page.evaluate("document.getElementById('petron-status').textContent"))
        browser.close()
    bridge.terminate()
    bridge.wait(timeout=10)
    ok = all(c["ok"] for c in run.results["checks"].values())
    run.results["ok"] = ok
    (args.out / "results.json").write_text(json.dumps(run.results, indent=2))
    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}: {len(run.results['checks'])} checks; "
          f"evidence in {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
