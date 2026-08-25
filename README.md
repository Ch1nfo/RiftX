<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="public/riftx-logo-dark-mark.png">
  <source media="(prefers-color-scheme: light)" srcset="public/riftx-logo-light-mark.png">
  <img alt="RiftX" src="public/riftx-logo-light-mark.png" width="420">
</picture>

### A local multi-agent workbench for authorized Web security testing

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](https://github.com/Ch1nfo/RiftX/releases)
[![Node.js](https://img.shields.io/badge/Node.js-20.18.1%2B-339933.svg?logo=nodedotjs&logoColor=white)](https://nodejs.org/)
[![Next.js](https://img.shields.io/badge/Next.js-15-000000.svg?logo=nextdotjs&logoColor=white)](https://nextjs.org/)
[![React](https://img.shields.io/badge/React-19-149ECA.svg?logo=react&logoColor=white)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6.svg?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33.svg?logo=playwright&logoColor=white)](https://playwright.dev/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#requirements)

English | [中文](README_ZH.md)

</div>

---

## Why RiftX?

Web security validation is often split across terminals, browsers, proxies, notes, and several model conversations. Context must be moved by hand, independent work is difficult to parallelize, and conclusions can become detached from the requests, screenshots, and command output that support them.

**RiftX** brings Agent conversations, controlled local tools, a Playwright browser, multi-agent collaboration, and evidence capture into one local workbench. It is designed for explicitly authorized Web testing, with an emphasis on operator control, visible execution, and reviewable conclusions. It does not replace dedicated scanners or decide the limits of an assessment for you.

- **One workbench** - Follow streaming responses, reasoning, tool calls, approvals, context usage, subagents, and evidence in one interface.
- **Parallel multi-agent work** - Delegate independent tasks to background subagents and consolidate the result after all required work completes.
- **Browser-native validation** - Operate a real page and inspect DOM snapshots, network traffic, console output, cookies, storage, and screenshots.
- **Evidence-backed findings** - Link findings to tool calls, browser requests, quotes, and screenshots with impact, confidence, and reproduction notes.
- **Local first** - No database or RiftX cloud account. Configuration, sessions, Skills, and evidence stay in the current user's home directory.
- **Flexible model access** - Connect OpenAI, Anthropic, Google, and compatible endpoints, with per-session model switching.

## Interface Preview

| Agent workbench | Model and Agent settings |
| :---: | :---: |
| ![RiftX Agent workbench](docs/images/riftx-workbench.png) | ![RiftX settings](docs/images/riftx-settings.png) |

> The screenshots use demo data and contain no real API keys, targets, or session history.

## Features

### Agent Workbench

- **Streaming sessions** - Render text, thinking, tool calls, errors, and task state as they happen; restore session state after reconnecting.
- **Continuous guidance** - Send follow-up instructions while the Agent is running. The conversation follows new output while still allowing manual history review.
- **Session management** - Scope sessions to a working directory, generate titles with AI, switch sessions, archive them, and permanently remove archived records.
- **Context management** - Show input, output, cache, and remaining tokens. Compact context automatically near the limit and expose the compaction state in the conversation.
- **Model switching** - Change the selected session's model from the composer without affecting other foreground or background sessions.
- **Bilingual UI** - Switch between English and Chinese, with light and dark themes.

### Multi-Agent Orchestration

- The main Agent can delegate independent investigation tasks to background subagents while continuing the primary line of work.
- Each subagent has an independent thread, approval gate, and BrowserContext while sharing the parent working directory.
- Subagents inherit the main Agent model by default or can use a separate model profile.
- Concurrency is configurable from `1-8`; excess tasks wait in the parent-session queue.
- Low, default, and high scheduling modes balance parallelism against token usage.
- Incremental logs, cancellation, and retry are available; reconnecting restores task snapshots and unresolved approvals.
- Subagents cannot recursively create more subagents. The runtime waits for every required child task before requesting the final answer.

### Browser Tool

RiftX exposes one action-based `browser` tool backed by Playwright and Chromium:

- **Page interaction** - `navigate`, `snapshot`, `click`, `fill`, `press`, `select`, `back`, `reload`
- **Runtime inspection** - `evaluate`, `console`, `screenshot`
- **Network evidence** - `requests`, `request_detail`, `response_body`
- **Identity and state** - `use_identity`, `identities`, `cookies`, `cookies_export`, `cookies_import`, `storage`
- **Network controls** - `set_host_mappings`, `set_user_agent`, `set_extra_headers`
- **Page management** - `tabs`, `close`

Snapshots produce an Agent-friendly text representation with stable element references such as `e1` and `e2`. Each identity has isolated cookies and storage, allowing anonymous, low-privilege, and high-privilege states to be tested in parallel. When image input is enabled for the model, screenshots can be sent directly as visual context.

Browser scope accepts CIDR, host, host and port, wildcard domain, and scheme-restricted URL rules. With no configured rules, the first navigation locks the host; out-of-scope navigation requires approval. Host mappings preserve the Host header and TLS SNI for virtual-host testing, and self-signed or invalid certificates can be accepted for controlled internal environments.

### Approvals and Operational Control

RiftX provides three approval modes for actions that may change local or target state:

| Mode | Behavior | Recommended use |
| --- | --- | --- |
| Request approval | Every guarded action waits for an explicit allow or reject decision | Default mode with step-by-step control |
| AI-assisted | The Agent evaluates impact and rejects when it cannot determine the effect | Continuous validation within a known scope |
| Full access | Bypasses the approval gate | Isolated, fully controlled environments only |

Read-only operations such as `read`, `grep`, `find`, and `ls` run directly. `bash`, `write`, `edit`, and browser actions that change state go through the approval flow. Approval errors, timeouts, and client disconnects fail closed.

### Findings and Evidence

- The main Agent and subagents can write structured findings to the parent session.
- A finding includes the affected asset, confidence, impact, reproduction notes, source, and timestamps.
- Evidence can link a message quote, tool call, captured browser request, or retained screenshot.
- Findings are deduplicated by normalized asset and title, then enriched with later evidence.
- Operators can change confidence, dismiss a finding, or restore it without deleting the underlying evidence.

### Agent Skills

RiftX loads local Skills from:

```text
~/.riftx/skills/<skill-name>/SKILL.md
```

The `SKILL.md` frontmatter must contain a lowercase `name` matching its directory and a clear `description`. Every ordinary user message attempts Skill matching. A matching Skill is automatically injected only once per live session, and `/skill:<skill-name>` can be used for explicit invocation. RiftX reads this directory without modifying Skill files and does not implicitly load project or SDK Skill directories.

### Model and Agent Configuration

- Supports `openai-completions`, `openai-responses`, `anthropic-messages`, and `google-generative-ai` API protocols.
- Each profile defines a provider, model ID, API key, base URL, transport, context window, output limit, thinking level, and image-input capability.
- Main and child Agents can use different profiles. Live model changes are scoped to the selected session.
- Configure a custom main Agent system prompt, subagent concurrency, and scheduling behavior.
- API keys remain in local configuration; no RiftX account is required.

### Persistence and Recovery

- Sessions use local JSON/JSONL persistence and remain available after a restart.
- Subagent tasks, logs, summaries, and approvals are restored with their parent. Tasks still running during a restart become `interrupted` and are not replayed automatically.
- Findings and retained screenshots are stored separately. Corrupt JSON preserves a backup of the original bytes, and writes use temporary files with atomic replacement.
- Stop, archive, working-directory changes, and deletion share one cleanup path that terminates Agent, Bash, and browser resources.

## Architecture

```text
+--------------------------------------------------------------+
|                 Next.js 15 + React 19 WebUI                  |
| Sessions / Stream / Approvals / Subagents / Evidence / i18n  |
+-----------------------------+--------------------------------+
                              | REST + SSE
+-----------------------------v--------------------------------+
|                     RiftX Server Runtime                     |
| Session Manager - Approval Gate - Context Compaction         |
|        |                 |                  |                |
|        +-- Main Agent    +-- Local Tools    +-- JSONL Store  |
|        +-- Subagents     +-- Browser Scope  +-- Evidence     |
|        +-- Skills                                            |
+-----------------------------+--------------------------------+
                              |
+-----------------------------v--------------------------------+
|           Playwright / Chromium + Local File Tools           |
| DOM Snapshot / Network / Identities / Screenshot / Bash      |
+--------------------------------------------------------------+
```

RiftX is a local, single-process Web application. The React workbench uses Next.js Route Handlers to call the Agent runtime, SSE continuously delivers session events to the UI, and runtime state is stored under `~/.riftx/` without a database or remote control plane.

## Technology Stack

| Layer | Technology | Purpose |
| --- | --- | --- |
| Web framework | Next.js 15 | UI, Route Handlers, and production server |
| Frontend | React 19, TypeScript 5 | Workbench, settings, and live state |
| Agent runtime | pi coding agent | Model sessions, tools, and context management |
| Browser | Playwright, Chromium | Page interaction, network capture, and screenshots |
| UI foundation | Radix Select, Phosphor Icons | Accessible controls and icons |
| Content | react-markdown, remark-gfm | Agent Markdown output |
| Validation | TypeBox, Zod | Tool and runtime schema validation |
| Persistence | Node.js filesystem, JSON/JSONL | Local configuration, sessions, tasks, and evidence |
| Tests | Node.js Test Runner, tsx | TypeScript unit and regression tests |

## Requirements

- Node.js `20.18.1` or newer; Node.js 22 LTS is recommended.
- npm 10 or the npm version bundled with the chosen Node.js release.
- Git 2.x or newer, required for GitHub installation.
- A model API endpoint and API key.
- Playwright Chromium, downloaded automatically during installation.

RiftX does not require Conda, Python, a database, or a remote RiftX account. On Linux, Playwright system packages may need to be installed once if Chromium reports missing libraries.

## Installation and Launch

### Option 1: Install Directly from GitHub

Install into a user-owned prefix without cloning the repository manually:

```bash
npm_config_prefix="$HOME/.local" npm install --global git+https://github.com/Ch1nfo/RiftX.git
export PATH="$HOME/.local/bin:$PATH"
rx webui
```

Add the following line to `~/.zshrc` or `~/.bashrc` so `rx` remains available in new terminals:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

#### Windows PowerShell

The Unix `$HOME` and `export PATH` syntax above is not PowerShell syntax. Windows npm normally uses a user-writable global prefix, so install directly with:

```powershell
npm install --global git+https://github.com/Ch1nfo/RiftX.git
rx webui
```

If PowerShell cannot find `rx` after installation, check the global npm directory and add it to the user `PATH`:

```powershell
npm config get prefix
```

The default directory is commonly `$env:APPDATA\npm`. Reopen PowerShell after changing `PATH`.

The explicit `git+https://` URL avoids Git/npm configurations that rewrite `github:Ch1nfo/RiftX` to SSH. RiftX is not yet published to the npm registry, so `npm install -g riftx` is not currently available.

### Option 2: Install from Source

```bash
git clone https://github.com/Ch1nfo/RiftX.git
cd RiftX
npm install
npm_config_prefix="$HOME/.local" npm link --ignore-scripts
export PATH="$HOME/.local/bin:$PATH"
rx webui
```

`npm install` installs dependencies, downloads Chromium, and creates the production build. `npm link --ignore-scripts` only registers that build as the `rx` command and does not build it again. This flow does not require root or `sudo`.

On Windows PowerShell, after `npm install` has finished the build and downloaded Chromium, link the source checkout with:

```powershell
npm link --ignore-scripts
rx webui
```

With a user-managed Node.js installation such as nvm or fnm, the same PowerShell commands apply. No `npm_config_prefix` or `export PATH` line is needed unless you changed the npm global prefix.

### First-Time Configuration

1. Open <http://localhost:3000>.
2. Select the working directory the Agent may access using the folder button in the workbench header.
3. Open **Settings**, add a model profile, and enter its API key, base URL, protocol, and model ID.
4. Save the settings, return to the workbench, create a session, and send a task.

Change the port or listening address when needed:

```bash
rx webui --port 4000
rx webui --hostname 127.0.0.1
rx webui --port 4000 --hostname 0.0.0.0
```

### Linux Browser Dependencies

If Chromium reports missing operating-system libraries, run:

```bash
npx playwright install-deps chromium
```

This installs system packages and may require administrator access. RiftX itself can still remain in a user-owned npm prefix.

## Development Commands

```bash
npm install          # Install dependencies and Chromium, then build
npm run dev          # Start the development server with hot reload
npm run typecheck    # Run TypeScript type checking
npm test             # Run unit and regression tests
npm run build        # Create a production build
npm start            # Start the production build directly
```

The development server and `rx webui` use <http://localhost:3000> by default. `rx webui` always serves the existing production build.

## Runtime Data

All RiftX runtime data is stored under `~/.riftx/` by default:

| Path | Contents |
| --- | --- |
| `~/.riftx/config.json` | Model profiles, approval mode, browser scope, and Agent settings |
| `~/.riftx/sessions/` | Agent session history in JSONL format |
| `~/.riftx/agent/` | RiftX-isolated model and authentication metadata |
| `~/.riftx/subagents/<session-id>/` | Subagent state, logs, summaries, and thread metadata |
| `~/.riftx/evidence/<session-id>/` | Findings and retained screenshots |
| `~/.riftx/skills/` | User-installed Agent Skills |

Do not commit API keys, authorization headers, cookies, target data, certificates, private keys, session history, or generated assessment artifacts. Always inspect `git status` before committing.

## Project Layout

```text
RiftX/
|-- bin/                 # rx CLI entry point
|-- docs/images/         # README interface screenshots
|-- public/              # Logos and static assets
|-- src/
|   |-- app/             # Next.js pages and API Route Handlers
|   |-- browser/         # Playwright tool, scope control, and recorder
|   |-- components/      # Workbench, settings, and shared UI
|   |-- lib/             # Shared types, i18n, and frontend helpers
|   `-- server/          # Agent runtime, config, sessions, approvals, storage
|-- package.json
|-- README.md
`-- README_ZH.md
```

## Web API

<details>
<summary><strong>Show the primary local endpoints</strong></summary>

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/bootstrap` | Load the workspace, sessions, and settings |
| `GET/POST` | `/api/sessions` | List or create sessions |
| `DELETE` | `/api/sessions/:id` | Permanently delete an archived session |
| `POST` | `/api/sessions/:id/archive` | Archive a session |
| `GET` | `/api/sessions/:id/stream` | Subscribe to SSE session events |
| `GET` | `/api/sessions/:id/messages` | Read session messages |
| `POST` | `/api/sessions/:id/prompt` | Send a task or follow-up instruction |
| `POST` | `/api/sessions/:id/abort` | Stop the running task |
| `POST` | `/api/sessions/:id/approval` | Resolve an approval request |
| `GET` | `/api/sessions/:id/findings` | Read session findings |
| `PATCH` | `/api/sessions/:id/findings/:findingId` | Update a finding |
| `GET` | `/api/sessions/:id/subagents` | Read subagent state |
| `POST` | `/api/sessions/:id/subagents/:taskId/cancel` | Cancel a subagent |
| `POST` | `/api/sessions/:id/subagents/:taskId/retry` | Retry a subagent |
| `PUT` | `/api/settings/approval-mode` | Update the approval mode |
| `GET/PUT` | `/api/settings/model-profiles` | Read or save model profiles |
| `POST` | `/api/workspace` | Change the working directory |

These endpoints are designed for local, single-user operation and do not provide remote user authentication. Do not expose the server directly to an untrusted network.

</details>

## Usage Boundaries

Use RiftX only on systems for which the operator has explicit authorization. Do not use it to access out-of-scope targets, disrupt services, delete data, steal credentials, or maintain unauthorized access. Agent output is not a security guarantee; inspect the command, target, and evidence before allowing impactful operations.

RiftX does not currently bundle dedicated scanners such as nmap, httpx, subfinder, nuclei, or ffuf. It also does not provide multi-user accounts, RBAC, remote task orchestration, or automatic export of browser authentication state into arbitrary CLI tools.

## FAQ

<details>
<summary><strong>Why install under <code>~/.local</code>?</strong></summary>

The system npm prefix often points to `/usr/local`, which is not writable by a regular user. `npm_config_prefix="$HOME/.local"` avoids `EACCES` without requiring `sudo`. Make sure `$HOME/.local/bin` is on `PATH`.

</details>

<details>
<summary><strong>Why does installation download Chromium and run a build?</strong></summary>

The Browser tool depends on Playwright Chromium, and `rx webui` serves a Next.js production build. `postinstall` installs the browser, while `prepare` creates the `.next` build, so the first installation takes longer than a typical CLI package.

</details>

<details>
<summary><strong>Can I open the WebUI without an API key?</strong></summary>

Yes, the interface and settings remain available, but the Agent cannot run model tasks. Add at least one valid model profile in **Settings**.

</details>

<details>
<summary><strong>Are Skills selected only when a session starts?</strong></summary>

No. Every ordinary user message attempts Skill matching, and a newly matched Skill can be injected into the active session. A Skill already injected automatically is not injected again; `/skill:<name>` can explicitly invoke one at any time.

</details>

<details>
<summary><strong>Does RiftX upload data to a RiftX service?</strong></summary>

RiftX has no cloud account or RiftX data service. Runtime state remains local. Content sent to the configured model provider is still governed by that provider's service and privacy terms.

</details>

## Contributing

Issues and pull requests are welcome. Before submitting a change, run:

```bash
npm run typecheck
npm test
npm run build
```

For substantial features, open an [issue](https://github.com/Ch1nfo/RiftX/issues) first to align on scope. Do not commit local `~/.riftx/` data, API keys, target information, or development plan documents.

## License

RiftX is open source under the [MIT License](LICENSE).

## Contact

- Email: [ch1nfo@foxmail.com](mailto:ch1nfo@foxmail.com)

---

<div align="center">

**If RiftX helps you, please consider giving the project a Star.**

</div>
