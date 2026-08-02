---
name: RiftX Web Control Plane
description: "Blue Team Cartridge: a durable blue 16-bit tactical control plane for authorized red-team operations."
colors:
  space-ink: "#030611"
  canvas-night: "#060b1c"
  panel-night: "#0d1d3c"
  panel-raised: "#123063"
  panel-deep: "#071632"
  border-cobalt: "#245dc7"
  command-blue: "#3478f6"
  telemetry-cyan: "#55d9ff"
  telemetry-cyan-high: "#a8edff"
  signal-text: "#eff8ff"
  muted-data: "#a9c5e8"
  dim-coordinate: "#7fa4d1"
  approval-gold: "#ffd45a"
  caution-orange: "#ff9f3f"
  confirmed-green: "#62e2ae"
  danger-red: "#ff5578"
  danger-well: "#42142b"
  command-button: "#0e2a5a"
  input-well: "#06142f"
  hard-shadow: "#01030a"
  canvas-day: "#dce9f7"
  panel-day: "#f7fbff"
  text-day: "#071b36"
  command-blue-day: "#155dbd"
  telemetry-cyan-day: "#007fa8"
  approval-gold-day: "#9b6500"
  confirmed-green-day: "#08734f"
  danger-red-day: "#b42145"
  input-day: "#ffffff"
  hard-shadow-day: "#8aa7c9"
typography:
  display:
    fontFamily: '"RiftX Silkscreen", ui-monospace, monospace'
    fontSize: "clamp(24px, 3vw, 36px)"
    fontWeight: 700
    lineHeight: 1.13
    letterSpacing: "-0.025em"
  headline:
    fontFamily: '"RiftX Silkscreen", ui-monospace, monospace'
    fontSize: "clamp(19px, 2vw, 27px)"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  title:
    fontFamily: '"RiftX Silkscreen", ui-monospace, monospace'
    fontSize: "10px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "normal"
  body:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.65
    letterSpacing: "normal"
  label:
    fontFamily: '"RiftX Silkscreen", ui-monospace, monospace'
    fontSize: "9px"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "0.04em"
  data:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "10px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
rounded:
  none: "0px"
spacing:
  pixel: "2px"
  micro: "4px"
  compact: "8px"
  control-gap: "12px"
  panel: "14px"
  section: "22px"
  large-section: "26px"
components:
  button-primary:
    backgroundColor: "{colors.command-button}"
    textColor: "{colors.telemetry-cyan-high}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "44px"
  button-secondary:
    backgroundColor: "{colors.panel-deep}"
    textColor: "{colors.muted-data}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "44px"
  button-danger:
    backgroundColor: "{colors.danger-well}"
    textColor: "{colors.danger-red}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "44px"
  input-field:
    backgroundColor: "{colors.input-well}"
    textColor: "{colors.signal-text}"
    typography: "{typography.data}"
    rounded: "{rounded.none}"
    padding: "10px 11px"
    height: "44px"
  nav-active:
    backgroundColor: "{colors.panel-deep}"
    textColor: "{colors.telemetry-cyan-high}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    width: "58px"
    height: "54px"
  tactical-panel:
    backgroundColor: "{colors.panel-night}"
    textColor: "{colors.signal-text}"
    rounded: "{rounded.none}"
    padding: "14px"
  status-badge:
    backgroundColor: "{colors.panel-deep}"
    textColor: "{colors.muted-data}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 8px"
    height: "28px"
  approval-alert:
    backgroundColor: "{colors.panel-raised}"
    textColor: "{colors.approval-gold}"
    typography: "{typography.data}"
    rounded: "{rounded.none}"
    padding: "12px 14px"
---

# Design System: RiftX Web Control Plane

## Overview

**Creative North Star: "Blue Team Cartridge / 蓝队战术卡带"**

RiftX is a blue 16-bit tactical operator terminal: cobalt command frames, cyan telemetry, pixel glyphs, hard-edged map tiles, and compact data panels make an authorized red-team operation feel like a serious cartridge-era control surface. The calibrated expression is Variance 7 / Motion 3 / Density 8—distinctive and dense, but never visually noisy enough to obscure scope, evidence, approval, or stop proof.

The WebUI is a projection of durable state, not the owner of the work. Visual emphasis therefore follows operational truth: authorization boundaries precede action, pause and emergency stop fence new effects first, and terminal state is not visually claimed until Execution, Browser session, and Target HTTP request dispositions are explicit. Dark and light themes, English and Chinese, keyboard operation, and non-color status language preserve the same tactical grammar.

**Key Characteristics:**

- Blue 16-bit tactical-console world with restrained micro-pixel craft.
- Dense, scan-first information hierarchy anchored by durable state.
- Square 2px frames, stepped cuts, and hard 3–4px pixel shadows.
- Cyan command/telemetry, gold approval, green confirmation, and red danger semantics.
- Persistent bilingual light/dark operation with visible focus and reduced-motion support.

## Colors

The palette is a disciplined blue command field: deep navy carries the durable surface, cobalt establishes structure, cyan identifies commands and telemetry, and gold/green/red are reserved for operational meaning.

### Primary

- **Command Blue** (`#3478f6`): primary cobalt emphasis, structural command framing, and active plotting.
- **Telemetry Cyan** (`#55d9ff`): focus, live telemetry, selected boundaries, and high-attention controls.
- **Telemetry Cyan High** (`#a8edff`): readable command text and focus contrast on dark blue surfaces.

### Secondary

- **Approval Gold** (`#ffd45a`): approval-required, waiting, paused, partial, and selected approval-mode states.
- **Confirmed Green** (`#62e2ae`): running, completed, available, online, succeeded, and confirmed states.

### Tertiary

- **Danger Red** (`#ff5578`): failure, cancellation, rejected or unconfirmed stop states, and emergency controls.
- **Caution Orange** (`#ff9f3f`): a supporting caution tone; it does not replace gold approval or red danger semantics.

### Neutral

- **Canvas Night / Tactical Panel / Deep Panel** (`#060b1c` / `#0d1d3c` / `#071632`): layered dark operating field without soft glass effects.
- **Signal Text / Muted Data / Dim Coordinate** (`#eff8ff` / `#a9c5e8` / `#7fa4d1`): primary copy, secondary evidence, and tertiary coordinates.
- **Canvas Day / Day Panel / Day Text** (`#dce9f7` / `#f7fbff` / `#071b36`): the persistent light theme preserves the same hierarchy instead of inverting into a different brand.
- **Day Semantic Set** (`#155dbd`, `#007fa8`, `#9b6500`, `#08734f`, `#b42145`): light-theme command blue, cyan, approval, confirmation, and danger remain contrast-adjusted and semantically identical.

### Named Rules

**The Semantic Signal Rule.** Cyan means command or telemetry, gold means approval or waiting, green means affirmative confirmation, and red means danger, failure, or unconfirmed stop; always pair color with explicit text, iconography, or disposition.

## Typography

- **Display Font:** RiftX Silkscreen (with `ui-monospace, monospace` fallback)
- **Body Font:** system monospace (`ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace`)
- **Label/Mono Font:** RiftX Silkscreen for labels; system monospace for evidence and data

**Character:** Silkscreen creates the cartridge-era command voice at small, deliberate doses. The system monospace stack carries body copy, identifiers, code, tables, timelines, and evidence so dense operational content stays readable in both languages.

### Hierarchy

- **Display** (700, `clamp(24px, 3vw, 36px)`, 1.13): Dashboard, New Run, and major operation headings.
- **Headline** (700, `clamp(19px, 2vw, 27px)`, 1.1): shell and workspace titles.
- **Title** (700, `10px`, 1.3): compact panel and section headings.
- **Body** (400, `12px`, 1.65): explanatory and conversational copy.
- **Label** (700, `9px`, `0.04em`, uppercase): controls, kickers, badges, tabs, and tactical metadata.
- **Data** (400, `10px`, 1.45): IDs, timestamps, code, table cells, traffic metadata, and dispositions.

### Named Rules

**The Silkscreen Restraint Rule.** Use the pixel face for commands, labels, compact headings, and identity; use system monospace for sentences, evidence, bilingual content, and long data.

## Layout

The shell uses a fixed 80px left command rail and a sticky topbar, with page content capped at 1540px and padded by 22px. Dashboard cards use compact 12–14px gaps; New Run uses a two-column configuration layout until 1180px; Run Detail uses a dense main workspace plus facts/sidebar structure and horizontally scrollable operational tabs where needed. The recurring, currently implicit rhythm is 2, 4, 8, 12, 14, 22, and 26px.

At 1180px wide compositions collapse; at 900px dashboard and dense workspaces reduce columns and padding; at 720px the left rail becomes a fixed 70px five-item bottom navigation and layouts stack; at 560px dense control clusters become a single column; at 420px controls remain at least 44px and decorative cuts simplify. Long identifiers wrap or truncate without causing page-level horizontal overflow.

**The Durable View Rule.** The first viewport prioritizes location, Run status, authorized objective, node/model/mode, outstanding approvals, and real controls—not decorative metrics, XP, or score.

## Elevation & Depth

Depth is structural, not atmospheric. Panels use 2px cobalt borders, a 2px inset highlight, a dark inset edge where needed, and a hard 4px offset shadow; controls use a hard 3px shadow that grows to 4px on hover and collapses on press. There are no diffuse neon glows, glass blur layers, or soft floating cards. Light theme replaces the shadow ink with a blue-gray hard edge rather than changing the depth model.

### Shadow Vocabulary

- **Panel hard edge** (`inset 2px 2px 0 var(--pc-inset-hi), inset -2px -2px 0 color-mix(in srgb, var(--pc-space) 70%, transparent), 4px 4px 0 var(--pc-shadow)`): standard panels, cards, loading, and error surfaces.
- **Control hard edge** (`inset -2px -2px 0 color-mix(in srgb, var(--pc-space) 66%, transparent), 3px 3px 0 var(--pc-shadow)`): buttons and compact command controls.
- **Terminal inset** (`inset 2px 2px 0 rgba(168, 237, 255, 0.08)`): terminal containment without a glow.

### Named Rules

**The Hard Depth Rule.** Every elevation cue must read as a pixel offset or inset frame; never soften it into ambient blur or purple glow.

## Shapes

The form language is square and cartridge-like: controls, inputs, chips, badges, panels, scrollbars, and status dots use zero radius with 2px borders. The reusable stepped cut clips 4px and 2px corners, while component-local 5–7px cuts are reserved for larger tactical cards. Pixel icons render at 18–20px with square line caps and miter joins; 16px or 32px grid tiles provide measured map structure rather than decoration.

**The Cartridge Geometry Rule.** Keep corners square, borders two pixels, and cuts stepped; do not introduce rounded SaaS cards or pill controls.

## Components

### Buttons

- **Shape:** square 2px command frame, stepped 4px cut, 42–44px minimum height, and hard 3px shadow.
- **Primary:** core surfaces use deep command blue with cyan-high text; dense operation workspaces use a cyan fill with deep-blue text for the highest-priority action.
- **Hover / Focus / Active:** 90–100ms stepped transitions; hover shifts by `-1px`, focus uses a 3px cyan-high outline with 3px offset, and active moves `3px` while removing the external shadow.
- **Secondary:** deep-blue surface, cobalt frame, and muted/cyan-high text.
- **Danger:** red frame on the danger well with explicit “Emergency stop” language; danger remains available for terminal cleanup when the control plane permits it.
- **Disabled:** reduced saturation and opacity plus disabled semantics; never rely on dimming alone to explain why an action is unavailable.

### Chips

- **Style:** square 2px frame, deep-blue background, 9px compact type, and 28px minimum height.
- **State:** status colors follow the semantic signal mapping and always retain the state label and square status dot.

### Cards / Containers

- **Corner Style:** square with optional stepped clipping.
- **Background:** night panel over deep panel/canvas layers; light theme uses the paired day surfaces.
- **Shadow Strategy:** hard 4px pixel offset with a 2px inset highlight.
- **Border:** 2px cobalt or semantic state color.
- **Internal Padding:** typically 12–17px for dense cards and 22–26px for large sections.

### Inputs / Fields

- **Style:** square 2px cobalt stroke, night input well, system-monospace data text, and 10px × 11px internal padding.
- **Focus:** cyan border plus a 4px cyan left inset marker; global focus-visible remains independently visible.
- **Error / Disabled:** error surfaces use explicit message and error code; disabled controls preserve text and native disabled semantics.

### Navigation

The desktop shell uses an 80px fixed icon rail with five 54px destinations; the active item has a cyan frame, deep-blue fill, cyan-high text, hard shadow, and gold corner pixels. At 720px it becomes a fixed 70px bottom bar with five equal columns. Labels remain visible, translated, keyboard focusable, and non-tooltip-dependent.

### Approval and Stop Disposition

Approval alerts and cards surface exact command, working context, target summary, environment changes, reason, actor, and time using durable state. A stop failure is a red alert with a textual table for Execution, Browser session, and Target HTTP request, each marked confirmed or unconfirmed with reason; color alone never asserts physical stop.

### Named Rules

**The Safety State Rule.** Requested, persisted, synchronized, and physically confirmed are different states; components must name the current state and must never optimistically collapse them.

## Do's and Don'ts

### Do:

- **Do** preserve the Blue Team Cartridge world across Dashboard, New Run, Run Detail, administration, gate, dark/light, and English/Chinese surfaces.
- **Do** keep authorization scope, exclusions, approval context, durable evidence, and stop disposition visible at the decision boundary.
- **Do** use real persisted state and explicit loading, empty, stale, error, disabled, and unconfirmed-stop language.
- **Do** preserve keyboard tab behavior, focus restoration, ARIA semantics, 3px focus-visible outlines, 44px mobile targets, and reduced-motion handling.
- **Do** reveal only the minimum safe metadata needed by the local operator.

### Don't:

- **Don't** use Matrix rain, purple glow, skulls, XP, score, fake terminal theater, or generic hacker clichés.
- **Don't** imply that closing the browser stops a Run or that a requested pause/emergency stop proves every effect has stopped.
- **Don't** expose secrets, credentials, full sensitive URLs, Runner-local paths, request/response bodies, headers, cookies, or authentication material.
- **Don't** fabricate customers, metrics, benchmarks, CVSS scores, public-deployment claims, or real engagement data.
- **Don't** encode approval risk, failure, or unconfirmed stop with color or animation alone.
