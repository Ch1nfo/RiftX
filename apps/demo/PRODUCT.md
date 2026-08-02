# Product

<!-- impeccable:product-schema 1 -->

> Scope note: product facts below are inherited from the checked-in RiftX product record. Demo-specific decisions are inferred from the user's request for an independent, directly runnable showcase.

## Platform

web

## Stack

React 19, TypeScript, and Vite inside the existing pnpm workspace. The Demo is a separate `@riftx/demo` application with no runtime import from `@riftx/web` and no Control Plane dependency.

## Users

The primary user is a local professional operator evaluating or presenting RiftX for authorized penetration-testing and red-team work. They need to understand the product's control model, move through a representative engagement, and demonstrate safety-critical states without configuring infrastructure or exposing a real target.

## Product Purpose

The Demo makes RiftX's implemented product model tangible in one self-contained browser application. It should let an evaluator explore mission setup, conversation-first execution, independent approvals, evidence, traffic metadata, graphs, terminal ownership, registry administration, model profiles, connector workflows, and provable stop semantics.

Success means a first-time evaluator can operate the sample engagement, understand what each major RiftX surface does, and distinguish synthetic demonstration behavior from the production control plane.

## Positioning

RiftX is a host-native red-team Agent control plane built around durable state, attributable human control, recoverable work, and affirmative stop proof. The Demo reproduces that control model with deterministic local state rather than pretending to execute real tools.

## Operating Context

- The Demo runs on loopback as a static client-side application.
- All engagements, targets, traffic, findings, commands, metrics, and events are synthetic and sanitized.
- The sample operation uses documentation-safe targets such as `10.10.10.0/24`, `api.example.test`, and `staging.example.test`.
- Browser refresh may restore lightweight UI preferences, but no credentials or sensitive operational data are accepted or persisted.
- The Demo never contacts a Control Plane, model provider, Runner, Temporal service, browser session, extension, Burp instance, or target system.

## Capabilities and Constraints

- Demonstrate Dashboard, mission creation, Run conversation, tool actions, approvals, graph projections, metadata-only HTTP traffic, audit timeline, synthetic terminal handoff, artifacts, findings, reports, nodes, tool registry, model profiles, Browser and Burp connectors, pause/resume, and emergency-stop proof.
- Interactions update deterministic in-memory sample state and must remain safe to click during a presentation.
- Every surface must visibly identify the application as `DEMO / SANITIZED`.
- No real shell, network replay, secret input, credential storage, hidden API calls, request/response bodies, cookies, authorization material, local filesystem paths, or claimed benchmark data.
- The Demo does not claim remote multi-user support, C2, WebShell, PDF/DOCX export, visual workflow building, or real macOS/Windows containment guarantees.

## Brand Commitments

- Preserve the RiftX name and the selected D direction: a blue 16-bit tactical cartridge interface with restrained micro-pixel craft.
- Preserve the evidence-led and safety-conscious voice. Operational state must be explicit in text and never communicated by color alone.
- Preserve square geometry, hard pixel depth, cobalt structure, cyan telemetry, gold approval, green confirmation, and red danger semantics.
- Support equivalent English and Chinese presentation copy, stable technical identifiers, persistent light/dark and locale choices, keyboard operation, responsive layouts, and reduced-motion preferences.

## Evidence on Hand

- Product truth: repository `README.md`, `docs/cyberstrikeai-adoption.md`, `docs/v2-completion-audit.md`, and `apps/web/PRODUCT.md`.
- Incumbent visual authority: `apps/web/DESIGN.md`, `apps/web/src/pixel-theme.css`, `apps/web/src/pixel-dense.css`, and assets under `apps/web/src/assets/`.
- Implemented UI and behavior: routes, pages, API types, and tests under `apps/web/src/`.
- No customer logos, testimonials, production engagement data, performance benchmarks, or real vulnerability scores are available. The Demo must not fabricate them.

## Product Principles

1. Demonstrate the real control model, not a fictional exploit simulator.
2. Make authorization, scope, approval, evidence provenance, and stop proof visible at every relevant step.
3. Let evaluators discover breadth without requiring backend setup or domain expertise.
4. Label synthetic content plainly and keep all interactions local and reversible.
5. Preserve the same concepts and terminology used by the production WebUI.

## Accessibility & Inclusion

The Demo must support keyboard navigation, visible focus, semantic controls, non-color state labels, responsive desktop and mobile layouts, reduced motion, readable Chinese and English identifiers, and comfortable 44px minimum interactive targets where space permits.
