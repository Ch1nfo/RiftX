# RiftX

[中文文档](README_ZH.md)

RiftX is a local, single-user Web UI for an authorized Web penetration testing and vulnerability validation agent. It embeds the Pi coding-agent SDK and provides a compact command-center interface for controlled reconnaissance, analysis, and evidence-driven validation.

RiftX is an MVP. It focuses on Pi's core agent capabilities and safe local operation; it does not ship dedicated scanners such as nmap, httpx, or subfinder.

## Highlights

- Pi coding-agent sessions with streaming text, thinking, tool execution, and errors.
- Built-in read-only tools: `read`, `grep`, `find`, and `ls`.
- Approval-controlled `bash`, `write`, and `edit` tools.
- Three approval modes: request approval, AI-assisted approval, and full access.
- Model profiles with provider, model ID, API key, base URL, protocol, transport, context window, output limit, and thinking level.
- Clickable model switching from the workbench composer when multiple profiles exist.
- One-shot read-only sub-agent support with profile inheritance or override.
- Session history, archive management, tool cards, Markdown rendering, context usage ring, and light/dark themes.
- SSE event streaming with local JSON/JSONL persistence.

## Requirements

- Node.js compatible with the installed Next.js version.
- A local conda environment named `agent` for development and verification.
- An API-compatible model endpoint and API key configured from the Settings page.

RiftX does not require a database or a remote RiftX account.

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

## Safety Model

RiftX is intended for targets where the operator has explicit authorization.

- `read`, `grep`, `find`, and `ls` are allowed by default.
- `bash`, `write`, and `edit` are guarded by the approval extension.
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
public/           RiftX logo assets
```

## Web API

The main endpoints include:

- `GET /api/bootstrap`
- `GET/POST /api/sessions`
- `GET /api/sessions/:id/stream`
- `GET /api/sessions/:id/messages`
- `POST /api/sessions/:id/prompt`
- `POST /api/sessions/:id/abort`
- `POST /api/sessions/:id/approval`
- `GET/PUT /api/settings/model-profiles`

All endpoints are designed for local single-user operation and do not provide remote authentication.

## License

See [LICENSE](LICENSE).
