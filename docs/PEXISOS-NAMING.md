# PexisOS | Project naming and compatibility transition

**Decision:** The project is named **PexisOS** (previously **PexisLink**).
**Status:** New public-facing name adopted. GitHub repository, distribution, module, command, and schemas retain their current technical identifiers until separate changes are reviewed.
**Date:** 2026-10-16

## What PexisOS is today

PexisOS is open-source research and a provider-neutral, read-only Agent Fabric for representing authorized physical and digital observations, evidence quality, and policy-enforced access. Existing offline vehicle telemetry research remains part of this project.

The name **does not assert** that PexisOS is already a bootable operating system, a Linux distribution, a device driver, a certified automotive/aerospace platform, or a trusted runtime for unreviewed native code. A later PexisOS Edge OS built using Linux/Yocto/Buildroot may be evaluated independently if validated device or gateway deployments justify it.

## Compatibility contract during transition

| Scope | Public identity or identifier | Status |
| --- | --- | --- |
| Project and documentation title | `PexisOS` | New identity |
| Historic project name | `PexisLink` | Legacy name, preserved where historically accurate |
| Current GitHub repository | `guilhermehrcst/Pexislink` | **Not renamed by this PR** |
| Current pip distribution | `pexislink` | No change |
| Current CLI | `pexislink` | No change |
| Current Python imports | `pexislink.*` | No change |
| Versioned API and observation schemas | `pexislink.observation.v0`, etc. | No change |
| Optional MCP stdio transport and tool names | Existing contracts | No change |
| Future `pexisos` package/CLI aliases | Not yet implemented | Separate migration gate |
| Future `PexisOS Edge OS` | Not implemented | Separate architecture and safety qualification |

An identifier is not automatically replaced because a logo or documentation heading changes. Versioned schemas are machine contracts, not marketing copy.

## Change sequence

1. **Project identity:** update the main README and project description without modifying the runtime, API, tests, versioned contracts, Git history, or ongoing pull request branches. This is the only scope of the current PR.
2. **Repository slug:** after evaluating active PRs, CI workflows, hardcoded URLs, package publishing, badges, clones, installations and branch protection, the repository owner can rename it under GitHub Settings > General > Repository name to `PexisOS`. This needs repository administration access, is **not executed in this PR**, and must not rely on redirects alone as proof that every integration still works.
3. **CLI / Python distribution:** if valuable, introduce an additive `pexisos` entry point and a documented package migration with deprecation/version policy, alias tests and clean installation checks. Keep legacy `pexislink` consumers supported until migration criteria are met.
4. **Hardware/OS work:** only after explicit scope and safety review, prototype OS-neutral gateways and minimal nodes; an optional Linux-based appliance image is a distinct future deliverable. Never require devices to replace their OEM firmware merely to expose authorized observations.

## Architecture and safety constraints

- Provider-neutral and open source first. The Model Context Protocol (MCP) is an optional agent-facing adapter, not the product's internal trust root.
- No real hardware, CAN bus, ECU access, actuation, remote network transports or certified device claims in the current read-only lab.
- Preserve legitimate identifiers and provenance. Do not fabricate sensors, device ownership or evidence.
- On-device, local gateway and independently authorized connectors are distinct deployment shapes; one OS cannot be installed on every device.
- Read-only capability enforcement, bounded data, isolation, least privilege and independent review remain required, regardless of branding.
- Historic ADRs, public API contracts and release notes should not be silently rewritten to pretend the old name or interface never existed.

## Acceptance gate for this branding-only change

The diff must contain only README, project description, and this naming document. Existing `pexislink` commands and imports must continue unchanged, all existing CI gates must remain intact, and open PRs #10 and #13 must not be altered or merged as a side effect.
