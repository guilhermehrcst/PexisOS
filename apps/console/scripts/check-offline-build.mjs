#!/usr/bin/env node
/**
 * Offline guard for the PexisOS Console build (Open Core requirement: no call-home,
 * no runtime network access). Fails if the built output:
 *   - loads any HTML/CSS resource from an absolute or protocol-relative URL;
 *   - contains a browser network API (fetch, XMLHttpRequest, WebSocket, EventSource,
 *     navigator.sendBeacon, importScripts);
 *   - contains any absolute URL not in the small allowlist of inert strings
 *     (XML namespaces and React's error-decoder link, which are never requested).
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
const NETWORK_APIS = [/\bfetch\s*\(/, /\bXMLHttpRequest\b/, /\bWebSocket\b/, /\bEventSource\b/, /\bsendBeacon\b/, /\bimportScripts\b/];

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

const problems = [];
let files;
try {
  files = walk(root);
} catch {
  console.error(`check-offline-build: cannot read ${root}; run the build first`);
  process.exit(2);
}
if (!files.some((f) => f.endsWith("index.html"))) problems.push("index.html missing from build output");

for (const file of files) {
  const ext = extname(file);
  if (![".html", ".js", ".css", ".mjs"].includes(ext)) continue;
  const text = readFileSync(file, "utf8");
  const rel = relative(root, file);

  if (ext === ".html") {
    for (const m of text.matchAll(/\b(?:src|href)\s*=\s*["']([^"']+)["']/g)) {
      const v = m[1] ?? "";
      if (/^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(v)) problems.push(`${rel}: external resource ${v}`);
    }
  }
  if (ext === ".css" || ext === ".html") {
    for (const m of text.matchAll(/url\(\s*["']?([^"')]+)["']?\s*\)/g)) {
      const v = m[1] ?? "";
      if (/^(?:https?:)?\/\//i.test(v)) problems.push(`${rel}: external url() ${v}`);
    }
    if (/@import\s+(?:url\()?["']?(?:https?:)?\/\//i.test(text)) problems.push(`${rel}: external @import`);
  }
  if (ext === ".js" || ext === ".mjs") {
    for (const api of NETWORK_APIS) if (api.test(text)) problems.push(`${rel}: network API ${api}`);
  }
  for (const m of text.matchAll(/https?:\/\/[^\s"'`)<>\\]+/g)) {
    const url = m[0];
    if (!ALLOWED_URL_PREFIXES.some((p) => url.startsWith(p))) problems.push(`${rel}: absolute URL ${url}`);
  }
}

if (problems.length) {
  console.error("check-offline-build: FAILED");
  for (const p of [...new Set(problems)]) console.error(`  - ${p}`);
  process.exit(1);
}
console.log(`check-offline-build: OK (${files.length} files, no external resources or network APIs)`);
