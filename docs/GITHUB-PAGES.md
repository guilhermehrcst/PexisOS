# PexisOS GitHub Pages

## Published scope

The site's public entrypoint is **PexisOS Console V1**, the browser-only
**Lab 001 / Smart Fan** demonstration using deterministic synthetic readings.
It is not a real diagnostic, telemetry feed, physical control system, API or AI agent.

The GitHub Pages site is served at:

`https://guilhermehrcst.github.io/PexisOS/`

The URL is **not live until GitHub Pages is enabled and a deployment succeeds**.

The Pages build uses the existing `apps/console` Vite application. Its
`base: "./"` keeps CSS/JS/fonts working under the repository subpath `/PexisOS/`.
The offline checker and CSP prohibit calls to third-party services and runtime
network APIs. No backend, secrets, keys, or live mission endpoints are shipped.

**PETRON Brain is not deployed to Pages here.** The Brain's WebP texture and mask
are still marked `review-required` in `docs/ip/OPEN-CORE-IP-REGISTER.md`.
The PETRON mission bridge is loopback-only and cannot run on GitHub Pages.
Any public visual Brain demo needs its asset provenance reviewed, and must be
separate from the local mission approval/execution bridge.

## First activation (repository administrator)

1. Open [PexisOS > Settings > Pages](https://github.com/guilhermehrcst/PexisOS/settings/pages).
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.
3. Save if a Save button appears. Do not select the branch-publishing mode.
4. Go to [Actions > PexisOS Pages (Console demo)](https://github.com/guilhermehrcst/PexisOS/actions/workflows/pages.yml)
   and choose **Run workflow** on `main` if the first push ran before Pages was enabled.
5. Confirm that both `Verify and build static Console` and
   `Deploy static Console to GitHub Pages` succeed before sharing the URL.

GitHub's default `GITHUB_TOKEN` can deploy an already-enabled Pages site, but
**cannot enable Pages by itself**. Do not create a personal access token or add
a secret for this workflow. Selecting GitHub Actions in Settings is the intended
one-time activation.

## Automated updates

Changes under `apps/console/**` or the Pages workflow on `main` rebuild,
test and publish automatically. A PR runs the build and tests, but never deploys.
The workflow also supports a manual `workflow_dispatch` on `main`.

Build gates:

- pinned Node.js setup and checkout actions;
- `npm ci --ignore-scripts` (lockfile integrity, no install hooks);
- strict TypeScript check and interface/domain tests;
- Vite build with relative paths;
- existing offline/network prohibition;
- static index and asset checks.

The deploy job has only the GitHub Pages and OIDC permissions it requires.
To restrict deployments further, add an environment rule that only `main`
may deploy to `github-pages`.

## Troubleshooting

- **404:** check Settings > Pages > Source = GitHub Actions, and check
  that the deploy workflow completed successfully.
- **Assets missing:** confirm `base: "./"` in `apps/console/vite.config.ts`
  and that the Pages artifact contains `index.html` and `assets/`.
- **Pages not enabled:** the `configure-pages` step fails. Enable the site
  in Settings, then manually run the workflow. Do not bypass this step.
- **Offline guard failure:** stop publication and correct the bundle. Never
  weaken the guard or remove CSP to force a deploy.
- **No real data:** expected. The Console deliberately uses synthetic data.

The public-source inventory and IP signoff remain unchanged:
`legal_signoff: not_recorded` is not an approval to ship additional assets.
