# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

RiftX serves a local professional operator conducting authorized penetration-testing or red-team work. The operator defines scope and exclusions, supervises Agent execution, reviews exact approval context, takes over terminals when needed, and turns durable evidence into findings and reports.

Platform administration is another responsibility of the same local operator. RiftX V2 does not implement multi-user roles or tenant RBAC.

## Product Purpose

RiftX turns an authorized security objective into a durable Run whose conversation, actions, approvals, execution attempts, traffic metadata, evidence, findings, and reports remain observable and recoverable through one control plane.

Success means the operator can understand what happened, intervene at the correct boundary, resume after interruption, and distinguish a requested stop from a physically confirmed stop.

## Positioning

RiftX is a host-native red-team Agent control plane that makes each operation auditable, recoverable, independently approvable, and provably stopped. The WebUI is a projection of persisted state, not the owner of the work.

## Operating Context

- The current trust profile is `local_single_operator` on loopback.
- A Run is created as durable context in `waiting_user`; no model or tool work starts until the operator sends the first concrete instruction.
- Conversation is the default Run view. Actions, approvals, graph projections, HTTP traffic metadata, timeline, raw events, terminal, artifacts, findings, and reports are separate operational views.
- Objective, authorization reference, entry points, positive scope, exclusions, success criteria, node, workspace, model profile, and approval mode define an engagement.
- WebUI and CLI share the same control plane. Browser and Burp extensions can attach external traffic workflows without becoming the Agent runtime.

## Capabilities and Constraints

- Durable resumable Runs, streaming conversation, independently attributable tool actions and approvals, terminal ownership handoff, node and tool registries, model profiles, immutable artifacts, evidence-backed findings, and Markdown/HTML/JSON reports are implemented.
- Pause and emergency stop fence new effects first. A terminal Run state requires affirmative stop dispositions for every known Execution, Browser, and Target HTTP effect.
- Graph views are deterministic read projections for task, evidence, and operation relationships. They are not an inferred knowledge graph.
- Traffic views are read-only and metadata-only. Request and response bodies, headers, cookies, authentication material, reveal, download, and replay are not available.
- The UI must not expose stored credentials, full secret-bearing URLs, Runner-local paths, or other sensitive payloads.
- Remote Runner protocol exists, but the current trust profile does not permit a real remote deployment. Windows and macOS do not provide the same proven process-containment boundary as delegated Linux cgroup v2.
- RiftX does not install penetration-testing tools and does not include C2, WebShell, bot, asset-management, multi-tenant RBAC, PDF/DOCX export, or a visual workflow builder in V2.

## Brand Commitments

- Preserve the RiftX name and the product's precise, evidence-led, safety-conscious voice.
- Preserve Chinese and English product language, persistent light/dark theme choice, and the distinction between ordinary state, approval risk, failure, and unconfirmed stop.
- The requested redesign may replace the incumbent visual world. It should incorporate restrained micro-pixel craft and authentic penetration-testing/red-team references without becoming a generic neon hacker interface.

## Evidence on Hand

- Product and safety truth: repository `README.md`, `docs/cyberstrikeai-adoption.md`, `docs/v2-completion-audit.md`, and `docs/deployment.md`.
- Implemented WebUI behavior: routes and components under `apps/web/src/`, with tests beside the relevant pages and components.
- Sanitized demonstration vocabulary includes `nmap`, `nuclei`, `masscan`, `msfconsole`, `custom_poc`, `10.10.10.0/24`, `api.example.test`, and `staging.example.test`. Preview data using these values must be marked `DEMO / SANITIZED`.
- No customers, testimonials, performance benchmarks, CVSS scores, public deployment claims, or real engagement data are provided and none may be fabricated.

## Product Principles

1. Persist intent before execution and keep the authorized boundary visible throughout the Run.
2. Separate conversation, current action state, approvals, audit events, evidence, and reports so each remains attributable.
3. Treat human control as a durable protocol, not a transient modal or optimistic UI event.
4. Prove safety-critical state with explicit text and evidence; never rely on color or animation alone.
5. Reveal only the minimum metadata required for the operator's task and fail closed on authorization or integrity mismatch.

## Accessibility & Inclusion

Preserve responsive layouts, keyboard operation, focus restoration for inspectors and deep links, ARIA semantics, bilingual copy, visible loading/empty/error states, and non-color status cues. Motion added by the redesign must honor reduced-motion preferences.
