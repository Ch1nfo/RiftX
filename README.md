# RiftX

[中文文档](README_ZH.md)

RiftX is a local, single-user Web UI for an authorized Web penetration testing and vulnerability validation agent. It embeds the Pi coding-agent SDK and provides a compact command-center interface for controlled reconnaissance, analysis, and evidence-driven validation.

RiftX is an MVP. It focuses on Pi's core agent capabilities and safe local operation; it does not ship dedicated scanners such as nmap, httpx, or subfinder.

## Highlights

- Pi coding-agent sessions with streaming text, thinking, tool execution, and errors.
- Built-in read-only tools: `read`, `grep`, `find`, and `ls`.
- Approval-controlled `bash`, `write`, `edit`, and browser mutation actions.
- Three approval modes: request approval, AI-assisted approval, and full access.
- Model profiles with provider, model ID, API key, base URL, protocol, transport, context window, output limit, and thinking level.
- Clickable model switching from the workbench composer when multiple profiles exist.
- Concurrent sub-agents with queueing, retry, cancellation, independent Pi threads, independent approval gates, and configurable profile inheritance or override.
- Session history, archive management, tool cards, Markdown rendering, context usage ring, and light/dark themes.
- English/Chinese UI language toggle.
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

- Pi-based interactive agent sessions
- Controlled local command/file execution
- Approval-gated browser automation for authenticated or interactive Web flows
- Concurrent child-agent delegation inside a single parent session

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

Open <http://localhost:3000>, open **Settings**, and create or select a model profile. The default working directory is the directory from which RiftX is started.

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
- Browser read actions (`snapshot`, `requests`, `cookies`, `storage`, `screenshot`, and `tabs`) are direct; navigation and page-mutating actions use the same approval gate.
- Sub-agents use the same guarded tool surface as the main agent, but cannot create further sub-agents.
- Set `RIFTX_BROWSER_ALLOWED_ORIGINS` to comma-separated authorized origins to enforce navigation scope. Without it, the first navigation locks the session to its origin. Out-of-scope requests and redirects are blocked.
- Request approval pauses for an explicit human decision.
- AI-assisted approval evaluates the proposed operation and blocks when local or target impact cannot be determined.
- Full access bypasses the approval gate and should only be used in a controlled environment.
- Approval failures, timeouts, and disconnected clients fail closed.

The agent must not be used to access systems outside the authorized scope, disrupt services, delete data, steal credentials, or retain access. A model response is not a security guarantee; review evidence and commands before allowing impactful operations.

## Configuration and Sensitive Data

Runtime state is stored outside the repository under `~/.riftx/`:

- `~/.riftx/config.json` stores model profiles and RiftX settings.
- `~/.riftx/sessions/` stores Pi session JSONL history.
- `~/.riftx/pi-agent/` stores RiftX-isolated Pi auth and model metadata.

API keys are stored locally with restricted file permissions. Never commit API keys, session history, authorization headers, cookies, target data, certificates, private keys, or generated reconnaissance artifacts. The repository `.gitignore` covers common secrets, runtime files, build output, and local caches, but always review `git status` before committing.

## Project Layout

```text
src/app/          Next.js pages and API routes
src/components/   Workbench, settings, and shared UI
src/server/pi/    Pi adapter, session lifecycle, approvals, and usage
src/server/       Local configuration and persistence helpers
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
- `requests`
- `request_detail`
- `response_body`
- `cookies`
- `storage`
- `screenshot`
- `tabs`
- `close`

Snapshots are agent-friendly text views with stable element refs such as `e1`, `e2`, and `e3`, so the model interacts with page elements by ref instead of raw selectors.

## Sub-agents

RiftX includes an application-level child-agent system on top of Pi sessions.

- Child agents run in parallel up to the configured concurrency limit.
- Extra tasks wait in the parent-session queue.
- Child agents share the same working directory as the parent.
- Child agents have independent Pi threads, approval gates, and browser contexts.
- Child agents inherit the main model by default, or can use an independent profile from Settings.
- Child agents cannot recursively spawn more child agents.
- Child approval requests surface in the main composer approval area, and child status/logs appear in the workbench panel.

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
- `GET /api/sessions/:id/subagents`
- `POST /api/sessions/:id/subagents/:taskId/cancel`
- `POST /api/sessions/:id/subagents/:taskId/retry`
- `PUT /api/settings/approval-mode`
- `GET/PUT /api/settings/model-profiles`

All endpoints are designed for local single-user operation and do not provide remote authentication.

## License

See [LICENSE](LICENSE).
