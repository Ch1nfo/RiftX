# RiftX

[中文文档](README_ZH.md)

RiftX is a local, single-user Web UI for an authorized Web penetration testing and vulnerability validation agent. It provides a compact command-center interface for controlled reconnaissance, analysis, and evidence-driven validation.

RiftX is an MVP. It focuses on core agent capabilities and safe local operation; it does not ship dedicated scanners such as nmap, httpx, or subfinder.

## Highlights

- Agent sessions with streaming text, thinking, tool execution, and errors.
- Built-in read-only tools: `read`, `grep`, `find`, and `ls`.
- Approval-controlled `bash`, `write`, `edit`, and browser mutation actions.
- Three approval modes: request approval, AI-assisted approval, and full access.
- Model profiles with provider, model ID, API key, base URL, protocol, transport, context window, output limit, and thinking level.
- Clickable model switching from the workbench composer when multiple profiles exist.
- Concurrent sub-agents with queueing, retry, cancellation, independent threads, independent approval gates, and configurable profile inheritance or override.
- Session findings with confidence, impact, reproduction notes, and reviewable quote, tool-call, browser-request, or screenshot evidence from the main Agent and sub-agents.
- Automatic mid-turn context compaction that keeps the active Agent run alive and updates context usage from the compacted conversation.
- Reconnection restores sub-agent snapshots and pending approvals, while session streams reject archived or out-of-workspace sessions.
- Session history, archive management, tool cards, Markdown rendering, context usage ring, and light/dark themes.
- English/Chinese UI language toggle.
- A system directory picker in the workbench header; session history is scoped to the selected working directory.
- Separate Settings sections for model/agent configuration and archived sessions, with independent save actions.
- Optional custom system prompt for the main Agent, with the built-in prompt used when disabled or empty.
- Custom prompt changes apply when a session is newly created or reopened; an already loaded session keeps its current prompt.
- AI-generated session titles that update immediately when a new task is sent.
- SSE event streaming with local JSON/JSONL persistence.
- A single action-based `browser` tool backed by Playwright/Chromium, with DOM refs (`e1`, `e2`), request history, cookies, storage, tabs, and screenshots.

## Requirements

- Node.js compatible with the installed Next.js version.
- A local conda environment named `agent` for development and verification.
- An API-compatible model endpoint and API key configured from the Settings page.
- Playwright Chromium installed once with `conda run -n agent npx playwright install chromium`.

RiftX does not require a database or a remote RiftX account.

## Current MVP Scope

RiftX currently focuses on:

- Interactive agent sessions
- Controlled local command/file execution
- Approval-gated browser automation for authenticated or interactive Web flows
- Concurrent child-agent delegation inside a single parent session
- Persistent, reviewable findings linked to their supporting evidence
- Automatic context compaction before the configured response reserve is exhausted
- Persistent context usage and model metadata for active and historical sessions

RiftX currently does not include:

- bundled offensive scanners such as nmap, httpx, subfinder, nuclei, or ffuf
- multi-user accounts, RBAC, or remote authentication
- databases or remote task orchestration
- automatic export of browser auth state into CLI tools

## Quick Start

```bash
conda run -n agent npm install
conda run -n agent npm run dev
```

Open <http://localhost:3000>, choose a working directory from the folder button in the workbench header, then open **Settings** to create or select a model profile. The initial working directory is the directory from which RiftX is started; changing it replaces the visible session list with sessions from the new directory.

The composer model selector changes the current Agent session in place, including an empty session that has not yet written its first message. This keeps the session ID and history intact while updating the model and context window.

For a production build:

```bash
conda run -n agent npm run build
conda run -n agent npm start
```

## Development Commands

```bash
conda run -n agent npm run typecheck
conda run -n agent npm test
conda run -n agent npm run build
```

Install Playwright Chromium once after dependencies are installed:

```bash
conda run -n agent npx playwright install chromium
```

## Safety Model

RiftX is intended for targets where the operator has explicit authorization.

- `read`, `grep`, `find`, and `ls` are allowed by default.
- `bash`, `write`, and `edit` are guarded by the approval extension.
- `browser` is one unified action-based tool. Read-only actions can run directly; navigation, page changes, form submission, and browser teardown are approval-controlled.
- Browser read actions (`snapshot`, `requests`, `cookies`, `storage`, `screenshot`, and `tabs`) are direct; navigation and page-mutating actions use the same approval gate.
- Sub-agents use the same guarded tool surface as the main agent, but cannot create further sub-agents. They have their own approval gate and BrowserContext.
- Set `RIFTX_BROWSER_ALLOWED_ORIGINS` to comma-separated authorized origins to enforce navigation scope. Without it, the first navigation locks the session to its origin. Out-of-scope requests and redirects are blocked.
- Request approval pauses for an explicit human decision.
- AI-assisted approval evaluates the proposed operation and blocks when local or target impact cannot be determined.
- Full access bypasses the approval gate and should only be used in a controlled environment.
- Approval failures, timeouts, and disconnected clients fail closed.
- The selected approval mode applies to the main Agent and running sub-agents.

The agent must not be used to access systems outside the authorized scope, disrupt services, delete data, steal credentials, or retain access. A model response is not a security guarantee; review evidence and commands before allowing impactful operations.

## Configuration and Sensitive Data

Runtime state is stored outside the repository under `~/.riftx/`:

- `~/.riftx/config.json` stores model profiles and RiftX settings.
- `~/.riftx/sessions/` stores Agent session JSONL history.
- `~/.riftx/agent/` stores RiftX-isolated auth and model metadata.
- `~/.riftx/subagents/<parent-session-id>/` stores child-agent task state, logs, summaries, and thread metadata. Running tasks are marked `interrupted` after a restart and are not replayed automatically.
- `~/.riftx/evidence/<session-id>/` stores session findings and retained finding screenshots.
- `~/.riftx/skills/` stores locally installed Agent Skills (`<skill-name>/SKILL.md`). Skills in this directory are loaded for new and reopened sessions.

Each skill uses the Agent Skills format:

```text
~/.riftx/skills/<skill-name>/SKILL.md
```

The `SKILL.md` frontmatter must include a matching lowercase `name` and a `description`. RiftX exposes the skill catalog to the Agent, automatically loads the best lexical match for a specialized task, and supports `/skill:<skill-name>` for explicit invocation. Skill files are read-only from RiftX; their content is never rewritten. Only this explicit user skill directory is loaded; implicit SDK and project skill directories are ignored.
On Windows, this resolves under `%USERPROFILE%\\.riftx\\skills` through the platform home directory.

API keys are stored locally with restricted file permissions. Never commit API keys, session history, authorization headers, cookies, target data, certificates, private keys, or generated reconnaissance artifacts. The repository `.gitignore` covers common secrets, runtime files, build output, and local caches, but always review `git status` before committing.

## Project Layout

```text
src/app/          Next.js pages and API routes
src/components/   Workbench, settings, and shared UI
src/server/       Agent runtime adapter, configuration, session lifecycle, approvals, usage, and persistence helpers
src/lib/          Shared TypeScript types
src/browser/      Unified Playwright browser tool, snapshots, scope guard, and network recorder
public/           RiftX logo assets
```

## Browser Tool

RiftX exposes one unified `browser` tool rather than many separate tools.

Supported actions currently include:

- `navigate`
- `snapshot`
- `click`
- `fill`
- `press`
- `select`
- `back`
- `reload`
- `evaluate`
- `console`
- `requests`
- `request_detail`
- `response_body`
- `use_identity`
- `identities`
- `cookies`
- `cookies_export`
- `cookies_import`
- `set_host_mappings`
- `set_user_agent`
- `set_extra_headers`
- `storage`
- `screenshot`
- `tabs`
- `close`

Snapshots are agent-friendly text views with stable element refs such as `e1`, `e2`, and `e3`, so the model interacts with page elements by ref instead of raw selectors.

Browser capabilities beyond basic navigation:

- **Scope authorization**: `~/.riftx/config.json` `browserScope` rules (CIDR `10.0.0.0/8`, host any-port `10.0.181.248`, host+port, `*.target.com`, `https://target.com`) gate navigations. With no rules, the first navigation locks the host; out-of-scope navigations raise an approval where "allow for this task" grants the host for the session. Subresource requests (scripts, fetches, images) are scope-checked as well: any host a page may load from must be in scope explicitly.
- **Runtime validation**: `evaluate` runs JavaScript in the page and `console` returns captured logs, uncaught errors, and alert/confirm/prompt dialogs - a captured dialog is the runtime proof for DOM-XSS payloads. `navigate` output includes recent console errors.
- **Identities**: each identity is an isolated cookie jar and storage (anonymous / low-privilege / admin in parallel). `cookies_export`/`cookies_import` bridge authenticated state with CLI tools such as curl. Recorded requests are tagged with the identity that made them.
- **Network control**: `set_host_mappings` applies curl `--resolve` semantics through a loopback proxy (connection goes to the mapped address while the Host header and TLS SNI are preserved) for virtual-host probing; `set_user_agent` and `set_extra_headers` customize the identity's fingerprint. Self-signed certificates are accepted by default (configurable via `browserIgnoreTlsErrors`).
- **Vision**: with "supports image input" enabled on the model profile, `screenshot` returns the page as an image the model can read directly (CAPTCHAs, dashboards, visual state).

## Session Findings

The main Agent and child agents can record evidence-backed conclusions into the selected parent session.

- Findings include an affected asset, confidence (`confirmed`, `likely`, `suspected`, or `not_reproducible`), impact, reproduction notes, source, and timestamps.
- Evidence can reference a short quote, tool call, captured browser request, or retained screenshot.
- The workbench evidence panel links back to tool and request details and lazily loads screenshots.
- Operators can adjust confidence, dismiss a finding, and restore dismissed findings without deleting the underlying record.
- Findings are deduplicated by normalized asset and title, then merged with new evidence.

## Sub-agents

RiftX includes an application-level child-agent system on top of Agent sessions.

- Child agents run in parallel up to the configured concurrency limit.
- Extra tasks wait in the parent-session queue.
- Child agents share the same working directory as the parent.
- Child agents have independent threads, approval gates, and browser contexts.
- Child agents inherit the main model by default, or can use an independent profile from Settings.
- The maximum concurrent child-agent count is configurable from 1 to 8. Tasks beyond the limit wait in the parent-session queue.
- Scheduling aggressiveness has `low`, `default`, and `high` modes. High mode favors broader delegation and warns about higher token and concurrency consumption; it still avoids duplicate or state-dependent tasks.
- Child agents cannot recursively spawn more child agents.
- Child approval requests surface in the main composer approval area, and child status/logs appear in the workbench panel.
- Child results and incremental logs are persisted and restored with the selected parent session.
- Every spawned child runs in the background so the main Agent can continue independent work. Completed child results are returned as they become available. All spawned children are mandatory for the assessment: if the main Agent reaches a conclusion while children are still active, RiftX waits for every child to reach a terminal state before requesting the final conclusion.
- `spawn_subagent` has no optional wait mode. The main Agent must never poll child logs, `tasks.json`, or filesystem state with `bash`/`sleep`; the runtime performs the final join.
- Reconnecting to a session replays current child task snapshots and unresolved approval requests.

## Web API

The main endpoints include:

- `GET /api/bootstrap`
- `GET/POST /api/sessions`
- `DELETE /api/sessions/:id`
- `POST /api/sessions/:id/archive`
- `GET /api/sessions/:id/stream`
- `GET /api/sessions/:id/messages`
- `POST /api/sessions/:id/prompt`
- `POST /api/sessions/:id/title`
- `POST /api/sessions/:id/abort`
- `POST /api/sessions/:id/approval`
- `GET /api/sessions/:id/findings`
- `PATCH /api/sessions/:id/findings/:findingId`
- `GET /api/sessions/:id/findings/screenshot/:screenshotId`
- `GET /api/sessions/:id/subagents`
- `POST /api/sessions/:id/subagents/:taskId/cancel`
- `POST /api/sessions/:id/subagents/:taskId/retry`
- `PUT /api/settings/approval-mode`
- `GET/PUT /api/settings/model-profiles`
- `POST /api/workspace`

All endpoints are designed for local single-user operation and do not provide remote authentication.

## License

See [LICENSE](LICENSE).
