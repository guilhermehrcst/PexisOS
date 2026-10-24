# PexisOS GitHub Pages

## Published scope

The site has two public pages, both static:

| Route | Content |
|---|---|
| `/PexisOS/` | **PETRON Brain**, a visual-only demonstration: the particle sphere on pure black, in full color, with no text, controls, panels or menus. |
| `/PexisOS/console/` | **PexisOS Console V1**, the browser-only **Lab 001 / Smart Fan** demonstration using deterministic synthetic readings. |

Neither page is a real diagnostic, a telemetry feed, a physical control system, an API or an AI agent.

The GitHub Pages site is served at:

`https://guilhermehrcst.github.io/PexisOS/`

The URL is **not live until GitHub Pages is enabled and a deployment succeeds**.

### How the site is assembled

`scripts/build_pages_site.py` builds the published tree from two inputs and then verifies it:

- the Console build (`apps/console`, Vite, `base: "./"`) is copied to `/console/`; its relative paths keep
  CSS, JS and fonts working under the repository subpath `/PexisOS/`;
- the Brain is a **fixed allowlist of four files** from `apps/petron/brain/`: `index.html`, `brain.js`,
  `brain-texture.webp`, `brain-mask.webp`. Nothing else from `apps/petron` is copied.

The published Brain page gets a Content-Security-Policy that denies all network access (`connect-src 'none'`,
same-origin scripts and images only) and declares the full-color palette as its default, because the adaptive
palette follows mission state and the public page has no runtime to drive it (`?palette=adaptive` and
`?palette=mono` still work). The local PETRON page keeps its adaptive default.

### What is never published

**The PETRON bridge and runtime are not on Pages.** No `live.js`, bridge, command channel, mission draft or
approval, event stream (SSE), API path, Python source or database is published, and the build refuses
to produce a site that contains one. The bridge is loopback-only and cannot run on GitHub Pages.

### Brain asset provenance (open decision)

The Brain's WebP texture and mask are still `review-required` in `docs/ip/OPEN-CORE-IP-REGISTER.md`
(`ownership_review: pending`, `legal_signoff: not_recorded`). **Publishing them on the public site is an
owner decision made while that review is pending; it is not an IP approval**, and nothing in the build or
in this document changes the register. The files were already public in this repository; the site adds a
display surface. If provenance review does not clear them, remove the four-file Brain from the allowlist
(or replace the images) and redeploy.

## First activation (repository administrator)

1. Open [PexisOS > Settings > Pages](https://github.com/guilhermehrcst/PexisOS/settings/pages).
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.
3. Save if a Save button appears. Do not select the branch-publishing mode.
4. Go to [Actions > PexisOS Pages (Brain home + Console)](https://github.com/guilhermehrcst/PexisOS/actions/workflows/pages.yml)
   and choose **Run workflow** on `main` if the first push ran before Pages was enabled.
5. Confirm that both `Verify and build the static site (Brain home + Console)` and
   `Deploy the static site to GitHub Pages` succeed, then open `/PexisOS/` (the Brain) and
   `/PexisOS/console/` (the Console) before sharing the URL.

GitHub's default `GITHUB_TOKEN` can deploy an already-enabled Pages site, but
**cannot enable Pages by itself**. Do not create a personal access token or add
a secret for this workflow. Selecting GitHub Actions in Settings is the intended
one-time activation.

## Automated updates

Changes under `apps/console/**`, `apps/petron/brain/**`, `scripts/build_pages_site.py` or the Pages
workflow on `main` rebuild, test and publish automatically. A PR runs the build and tests, but never deploys.
The workflow also supports a manual `workflow_dispatch` on `main`.

Build gates:

- pinned Node.js setup and checkout actions;
- `npm ci --ignore-scripts` (lockfile integrity, no install hooks);
- strict TypeScript check and interface/domain tests;
- Vite build with relative paths;
- existing offline/network prohibition for the Console;
- tests of the site builder and its publication rules (`tests/test_pages_site.py`);
- site assembly and verification: exact file set at the root, no operational capability in the Brain,
  CSP present, every relative reference resolves, no absolute asset path, no Python or database file.

The deploy job has only the GitHub Pages and OIDC permissions it requires.
To restrict deployments further, add an environment rule that only `main`
may deploy to `github-pages`.

## Troubleshooting

- **404:** check Settings > Pages > Source = GitHub Actions, and check
  that the deploy workflow completed successfully.
- **Assets missing:** confirm `base: "./"` in `apps/console/vite.config.ts`
  and that the Pages artifact contains `index.html` and `console/assets/`. Run
  `python scripts/build_pages_site.py --verify-only --out site` on the assembled site.
- **`/PexisOS/console` without a slash:** GitHub Pages redirects it to `/PexisOS/console/`.
- **Pages not enabled:** the `configure-pages` step fails. Enable the site
  in Settings, then manually run the workflow. Do not bypass this step.
- **Offline guard failure:** stop publication and correct the bundle. Never
  weaken the guard or remove CSP to force a deploy.
- **No real data:** expected. The Console deliberately uses synthetic data, and the Brain shows no data at all.
- **Build refused (`pages_site_refused`):** read the named rule. Never weaken the check or copy extra files to
  force a deploy.

The public-source inventory and IP signoff remain unchanged:
`legal_signoff: not_recorded` is not an approval to ship additional assets.
