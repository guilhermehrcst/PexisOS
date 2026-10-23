// @vitest-environment node
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, describe, expect, it } from "vitest";

/** Negative fixtures: each must make the offline guard fail; the clean build must pass. */

const GUARD = join(dirname(fileURLToPath(import.meta.url)), "check-offline-build.mjs");
const CSP = `<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; connect-src 'none'">`;
const page = (head = "", body = "") => `<!doctype html><html><head>${CSP}${head}</head><body>${body}</body></html>`;
const dirs = [];

/** Writes the files (null drops the default index.html) and runs the guard on them. */
function run(files) {
  const dir = mkdtempSync(join(tmpdir(), "offline-guard-"));
  dirs.push(dir);
  for (const [name, content] of Object.entries({ "index.html": page(), ...files })) {
    if (content === null) continue;
    mkdirSync(dirname(join(dir, name)), { recursive: true });
    writeFileSync(join(dir, name), content);
  }
  return spawnSync(process.execPath, [GUARD, dir], { encoding: "utf8" });
}

afterAll(() => { for (const d of dirs) rmSync(d, { recursive: true, force: true }); });

describe("offline guard", () => {
  it("passes a clean build", () => {
    const r = run({ "assets/app.js": "const a = 1; document.title = 'ok';", "assets/app.css": "a{color:red}", "assets/f.woff2": "\u0000" });
    expect(r.stderr).toBe("");
    expect(r.status).toBe(0);
  });

  const cases = {
    "fetch without a call": { "assets/app.js": "const f = window.fetch; f('/x');" },
    "WebSocket": { "assets/app.js": "new WebSocket('/x')" },
    "RTCPeerConnection": { "assets/app.js": "new RTCPeerConnection()" },
    "WebTransport": { "assets/app.js": "new WebTransport('/x')" },
    "service worker": { "assets/app.js": "navigator.serviceWorker.register('/sw.js')" },
    "web worker": { "assets/app.js": "new Worker('/w.js')" },
    "window.open": { "assets/app.js": "window.open('/x')" },
    "dynamic import": { "assets/app.js": "import('/x.js')" },
    "absolute URL": { "assets/app.js": "const u = 'https://example.com/a';" },
    "escaped absolute URL": { "assets/app.js": "const u = \"https:\\/\\/example.com\\/a\";" },
    "protocol-relative literal": { "assets/app.js": "const u = '//example.com/a';" },
    "websocket URL": { "assets/app.js": "const u = 'wss://example.com';" },
    "unquoted external src": { "index.html": page("<script src=//example.com/a.js></script>") },
    "external srcset": { "index.html": page("", "<img srcset=\"./a.png 1x, https://example.com/b.png 2x\">") },
    "external form action": { "index.html": page("", "<form action=\"//example.com/post\"></form>") },
    "meta refresh": { "index.html": page("<meta http-equiv=\"refresh\" content=\"0; url=//example.com\">") },
    "external css url()": { "assets/app.css": "a{background:url(//example.com/x.png)}" },
    "external @import": { "assets/app.css": "@import 'https://example.com/x.css';" },
    "unknown file type": { "assets/data.bin": "x" },
    "missing CSP": { "index.html": "<!doctype html><html><head></head><body></body></html>" },
    "CSP allowing connections": { "index.html": "<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'self'; script-src 'self'\"></head><body></body></html>" },
    "missing index.html": { "index.html": null, "assets/app.js": "1" },
  };
  for (const [name, files] of Object.entries(cases)) {
    it(`fails on ${name}`, () => {
      const r = run(files);
      expect(r.status, r.stdout).toBe(1);
      expect(r.stderr).toMatch(/FAILED/);
    });
  }
});
