#!/usr/bin/env node
/**
 * Offline guard for the PexisOS Console build (Open Core requirement: no call-home,
 * no runtime network access). Fails closed if the built output:
 *   - contains a file whose type the guard does not know how to inspect;
 *   - loads any HTML/CSS resource from an absolute or protocol-relative URL, in quoted or
 *     unquoted attributes, srcset, form actions or a meta refresh;
 *   - contains a browser network API (fetch, XMLHttpRequest, WebSocket, EventSource,
 *     sendBeacon, importScripts, RTCPeerConnection, WebTransport, service or web workers,
 *     window.open, dynamic import());
 *   - contains any absolute URL (plain, protocol-relative or with escaped slashes) not in
 *     the small allowlist of inert strings (XML namespaces and React's error-decoder link,
 *     which are never requested);
 *   - lacks the Content-Security-Policy that blocks network access at runtime.
 * The CSP is the independent runtime barrier; this guard is the build-time one.
 * Usage: node scripts/check-offline-build.mjs dist
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { extname, join, relative } from "node:path";

const root = process.argv[2] ?? "dist";
const ALLOWED_URL_PREFIXES = [
  "http://www.w3.org/", // SVG / XLink / XHTML namespaces
  "https://react.dev/errors/", // React production error message text
  "https://reactjs.org/docs/error-decoder.html", // older React error text
];
const TEXT_EXTENSIONS = new Set([".html", ".js", ".mjs", ".css", ".svg", ".json", ".txt", ".webmanifest"]);
const BINARY_EXTENSIONS = new Set([".woff2", ".woff", ".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".ico"]);
const NETWORK_APIS = [
  /\bfetch\b/, /\bXMLHttpRequest\b/, /\bWebSocket\b/, /\bEventSource\b/, /\bsendBeacon\b/, /\bimportScripts\b/,
  /\bRTCPeerConnection\b/, /\bWebTransport\b/, /\bserviceWorker\b/, /\bnew\s+(?:Shared)?Worker\s*\(/,
  /\bwindow\.open\s*\(/, /\bimport\s*\(/,
];
const REQUIRED_CSP = ["default-src 'self'", "connect-src 'none'", "script-src 'self'"];
/** Anything with a scheme and authority, or protocol-relative; data: and blob: are local. */
const EXTERNAL = /^\s*(?:[a-z][a-z0-9+.-]*:)?(?:\/\/|\\\/\\\/)/i;
const ATTR = /\b(src|href|action|formaction|poster|data|srcset|content|http-equiv)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))/gi;

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

function checkHtml(rel, text, problems) {
  for (const tag of text.matchAll(/<[a-z][^>]*>/gi)) {
    const attrs = new Map();
    for (const m of tag[0].matchAll(ATTR)) attrs.set((m[1] ?? "").toLowerCase(), m[2] ?? m[3] ?? m[4] ?? "");
    for (const [name, value] of attrs) {
      if (name === "srcset") {
        for (const candidate of value.split(",")) {
          const url = candidate.trim().split(/\s+/)[0] ?? "";
          if (EXTERNAL.test(url)) problems.push(`${rel}: external srcset ${url}`);
        }
      } else if (name === "content") {
        const refresh = /url\s*=\s*['"]?([^'";]+)/i.exec(value);
        if (refresh && EXTERNAL.test(refresh[1] ?? "")) problems.push(`${rel}: external meta refresh ${refresh[1]}`);
      } else if (name !== "http-equiv" && EXTERNAL.test(value)) {
        problems.push(`${rel}: external ${name} ${value}`);
      }
    }
  }
}

const problems = [];
let files;
try {
  files = walk(root);
} catch {
  console.error(`check-offline-build: cannot read ${root}; run the build first`);
  process.exit(2);
}
const indexHtml = files.find((f) => relative(root, f) === "index.html");
if (!indexHtml) problems.push("index.html missing from build output");

for (const file of files) {
  const ext = extname(file).toLowerCase();
  const rel = relative(root, file);
  if (BINARY_EXTENSIONS.has(ext)) continue;
  if (!TEXT_EXTENSIONS.has(ext)) {
    problems.push(`${rel}: unknown file type; add it to the guard before shipping it`);
    continue;
  }
  const text = readFileSync(file, "utf8");

  if (ext === ".html" || ext === ".svg") checkHtml(rel, text, problems);
  if (ext === ".css" || ext === ".html" || ext === ".svg") {
    for (const m of text.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/g)) {
      const v = m[1] ?? "";
      if (EXTERNAL.test(v)) problems.push(`${rel}: external url() ${v}`);
    }
    if (/@import\s+(?:url\()?\s*["']?(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(text)) problems.push(`${rel}: external @import`);
  }
  if (ext === ".js" || ext === ".mjs" || ext === ".html") {
    for (const api of NETWORK_APIS) if (api.test(text)) problems.push(`${rel}: network API ${api}`);
    if (/["'`]\s*(?:\/\/|\\\/\\\/)[a-z0-9.-]+\.[a-z]{2,}/i.test(text)) problems.push(`${rel}: protocol-relative URL literal`);
  }
  // Escaped slashes ("https:\/\/") are normalised so they cannot hide a URL.
  const normalised = text.replace(/\\\//g, "/");
  for (const m of normalised.matchAll(/\b(?:https?|wss?|ftp):\/\/[^\s"'`)<>\\]+/gi)) {
    const url = m[0];
    if (!ALLOWED_URL_PREFIXES.some((p) => url.startsWith(p))) problems.push(`${rel}: absolute URL ${url}`);
  }
}

if (indexHtml) {
  const html = readFileSync(indexHtml, "utf8");
  const csp = /<meta[^>]+http-equiv\s*=\s*["']Content-Security-Policy["'][^>]*content\s*=\s*"([^"]*)"/i.exec(html)?.[1] ?? "";
  for (const directive of REQUIRED_CSP) {
    if (!csp.includes(directive)) problems.push(`index.html: Content-Security-Policy missing "${directive}"`);
  }
}

if (problems.length) {
  console.error("check-offline-build: FAILED");
  for (const p of [...new Set(problems)]) console.error(`  - ${p}`);
  process.exit(1);
}
console.log(`check-offline-build: OK (${files.length} files, no external resources or network APIs, CSP present)`);
