---
name: "RiftX Demo — Blue Team Cartridge"
description: "A conversation-led operation theater for durable human control."
colors:
  night-space: "#030611"
  tactical-canvas: "#060b1c"
  command-shell: "#09142c"
  cartridge-panel: "#0d1d3c"
  selected-panel: "#102753"
  deep-panel: "#071632"
  cobalt-frame: "#245dc7"
  cobalt-frame-soft: "#17386f"
  action-blue: "#3478f6"
  telemetry-cyan: "#55d9ff"
  telemetry-cyan-high: "#a8edff"
  primary-text: "#eff8ff"
  secondary-text: "#a9c5e8"
  tertiary-text: "#7fa4d1"
  approval-gold: "#ffd45a"
  confirmation-green: "#62e2ae"
  danger-red: "#ff5578"
  danger-red-soft: "#ff9eb2"
  danger-red-deep: "#42142b"
  control-input: "#06142f"
  control-button: "#0e2a5a"
  control-button-hover: "#17448d"
  pixel-shadow: "#01030a"
  map-grid: "rgb(52 120 246 / 9%)"
  map-grid-strong: "rgb(85 217 255 / 14%)"
  inset-highlight: "rgb(168 237 255 / 13%)"
typography:
  display:
    fontFamily: "RiftX Silkscreen, ui-monospace, monospace"
    fontSize: "clamp(25px, 2.8vw, 42px)"
    fontWeight: 700
    lineHeight: 1.13
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "RiftX Silkscreen, ui-monospace, monospace"
    fontSize: "clamp(22px, 2.2vw, 34px)"
    fontWeight: 700
    lineHeight: 1.15
    letterSpacing: "-0.02em"
  title:
    fontFamily: "RiftX Silkscreen, ui-monospace, monospace"
    fontSize: "11px"
    fontWeight: 700
    lineHeight: 1.35
    letterSpacing: "normal"
  body:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
  label:
    fontFamily: "RiftX Silkscreen, ui-monospace, monospace"
    fontSize: "8px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "normal"
rounded:
  none: "0"
spacing:
  micro: "4px"
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "22px"
  hero: "30px"
components:
  button-primary:
    backgroundColor: "{colors.telemetry-cyan-high}"
    textColor: "#03142a"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "42px"
  button-secondary:
    backgroundColor: "{colors.control-button}"
    textColor: "{colors.telemetry-cyan-high}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "42px"
  button-danger:
    backgroundColor: "{colors.danger-red-deep}"
    textColor: "{colors.danger-red-soft}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 14px"
    height: "42px"
  text-field:
    backgroundColor: "{colors.control-input}"
    textColor: "{colors.primary-text}"
    typography: "{typography.body}"
    rounded: "{rounded.none}"
    padding: "10px 11px"
    height: "44px"
  status-approval:
    backgroundColor: "{colors.deep-panel}"
    textColor: "{colors.approval-gold}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 8px"
    height: "25px"
  demo-stamp:
    backgroundColor: "{colors.deep-panel}"
    textColor: "{colors.telemetry-cyan-high}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 8px"
    height: "28px"
  pixel-panel:
    backgroundColor: "{colors.cartridge-panel}"
    textColor: "{colors.primary-text}"
    rounded: "{rounded.none}"
    padding: "16px"
---

# Design System: RiftX Demo — Blue Team Cartridge

## Overview

**Creative North Star: "Blue Team Cartridge"**

Blue Team Cartridge is a conversation-led operation theater: a navy tactical field where every authorization, action, evidence edge, ownership change, and stop proof reads like a durable control record. The visual world is dense but deliberate—cobalt two-pixel frames establish structure, cyan carries telemetry, and hard cartridge depth makes controls feel physical without drifting into decorative arcade nostalgia.

The system refuses the generic dashboard tour. Its signature composition is a split tactical deck that pairs the active sanitized mission with a scope lock, stage route, proof states, and a direct operation action; the broader product unfolds from that same evidence-led grammar. Light mode preserves the hierarchy and state roles as a daylight field rather than becoming a different brand.

**Key Characteristics:**

- Navy map fields and compact operational density.
- Cobalt two-pixel structure with square or four-pixel cut corners.
- Cyan telemetry, gold approval, green confirmation, and red danger semantics.
- Pixel display labels paired with a legible monospace data face.
- Hard cartridge shadows and restrained step-based motion.
- Persistent `DEMO / SANITIZED` and local-only identity.

## Colors

The dark default behaves like an illuminated command cartridge; light mode remaps the same semantic roles onto cool paper-blue surfaces with darker, accessible telemetry inks.

### Primary

- **Telemetry Cyan:** The scarce high-attention signal for active structure, focus, selected routes, icons, and live telemetry.
- **Telemetry Cyan High:** The brightest readable accent for primary actions, identifiers, and high-priority labels.

### Secondary

- **Cobalt Frame:** The structural stroke for shells, panels, controls, and dividers; the softer cobalt separates dense rows and nested regions.
- **Action Blue:** A hover and interaction bridge between cobalt structure and cyan activation.

### Tertiary

- **Approval Gold:** Reserved for waiting, approval, locked scope, and human-ownership moments.
- **Confirmation Green:** Reserved for completed, verified, attached, online, and affirmative stop-proof states.
- **Danger Red:** Reserved for rejection, cancellation, blocked states, emergency stop, and destructive controls.

### Neutral

- **Night Space, Tactical Canvas, and Command Shell:** The outer field, gridded work surface, and fixed navigation shell.
- **Cartridge Panel, Selected Panel, and Deep Panel:** The surface ladder for containers, active rows, and inset work areas.
- **Primary, Secondary, and Tertiary Text:** A three-step information hierarchy; identifiers stay brightest, explanations recede, and metadata remains readable.
- **Control Input and Control Button:** Purpose-built dark control surfaces rather than generic gray form chrome.
- **Pixel Shadow, Map Grid, and Inset Highlight:** Structural depth and cartographic texture, never ambient decoration.

### Light-mode token behavior

The frontmatter records the shipped dark default. Under `data-theme="light"`, the same source roles remap as follows:

| Source role | Light override |
| --- | --- |
| `--space` / `--canvas` / `--shell` | `#07152d` / `#dce9f7` / `#eef6ff` |
| `--panel` / `--panel-blue` / `--panel-deep` | `#f7fbff` / `#e4f0ff` / `#edf6ff` |
| `--line` / `--line-soft` | `#3474c7` / `#86aad3` |
| `--blue` / `--cyan` / `--cyan-high` | `#155dbd` / `#007fa8` / `#005a7a` |
| `--text` / `--muted` / `--dim` | `#071b36` / `#365879` / `#587493` |
| `--gold` / `--green` | `#875800` / `#08734f` |
| `--red` / `--red-soft` / `--red-deep` | `#b42145` / `#9f1738` / `#ffe3ea` |
| `--input` / `--button` / `--button-hover` | `#ffffff` / `#155dbd` / `#0d478e` |
| `--shadow` | `#8aa7c9` |
| `--grid` / `--grid-strong` | `rgb(21 93 189 / 12%)` / `rgb(0 127 168 / 18%)` |
| `--inset-highlight` | `rgb(255 255 255 / 86%)` |

**The State Has a Name Rule.** Color always arrives with visible text, an icon or pip, and a structural treatment; no operational state is communicated by hue alone.

**The Human Gold Rule.** Gold means approval, scope lock, or human ownership—not generic emphasis.

## Typography

- **Display Font:** RiftX Silkscreen (with `ui-monospace`, `monospace` fallback)
- **Body Font:** system monospace (`SFMono-Regular`, Menlo, Monaco, Consolas)
- **Label/Mono Font:** RiftX Silkscreen for controls and state labels; system monospace for data and code

**Character:** The pixel face gives commands, stages, and labels a cartridge identity. The data face carries equivalent English and Chinese explanatory copy, timestamps, hashes, hostnames, and evidence without sacrificing scan speed.

### Hierarchy

- **Display** (700, fluid 25–42px, 1.13): First-viewport thesis copy only; it resolves to 22–25px on narrow screens.
- **Headline** (700, fluid 22–34px, 1.15): Screen and operation titles.
- **Title** (700, 11px, 1.35): Panel headings and compact signature-card titles.
- **Body** (400, 13px, 1.55): Explanations and conversational content, generally capped near 66–75 characters.
- **Label** (700, 8px, 1.2): Buttons, state chips, stage names, and metadata keys; narrow screens raise state-bearing labels to 10px.

**The Pixel Sparingly Rule.** Use the pixel face for navigation, commands, headings, labels, and state; use the data face for sentences and dense operational evidence.

**The Narrow State Scale Rule.** At 720px and below, state labels, risk labels, HTTP states, and credential states use a 10px label and a 30px minimum height; never shrink them to recover space.

## Layout

The desktop shell uses an 84px fixed command rail and a 64px fixed top bar. Main content sits inside a centered 1540px maximum stack with 22px page insets and an 18px vertical rhythm. The first viewport is a split tactical deck: mission thesis and actions occupy the left field, while the active cartridge, scope lock, five-stage route, and proof states occupy the right. Preserve this two-part relationship even when it stacks.

The responsive sequence is deliberate. At 1180px, secondary split views tighten; at 1020px, the command deck and operational sidecars stack; at 900px, telemetry and proof grids reduce columns; at 720px, the rail becomes a fixed 72px five-item bottom navigation, the top bar becomes 58px, and page insets become 12px; at 520px, action groups and dense status regions become single-column while the stage route remains intact. Tables and long execution routes scroll rather than crushing labels.

On narrow screens, the top bar keeps a fixed two-line safety cartridge: `DEMO / SANITIZED` on line one and `LOCAL ONLY` on line two. Do not hide, abbreviate, or replace it with color. The active screen title may truncate before this identity does.

**The Split Deck Rule.** The thesis and the active cartridge are peers: desktop places them side by side, compact layouts stack them without removing scope, route, or proof context.

**The Safety Cartridge Rule.** `DEMO / SANITIZED` plus local-only identity remains persistently visible at every breakpoint.

## Elevation & Depth

Depth is structural, hard, and pixel-aligned. Panels combine two-pixel inset highlights with a four-pixel offset shadow; controls use a three-pixel hard shadow, rise by one pixel on hover, and press flush on active. Light mode keeps the same geometry but replaces black depth with a cool blue-gray shadow. There are no blurred ambient shadows.

### Shadow Vocabulary

- **Panel Cartridge:** Two opposing two-pixel inset edges plus a four-pixel hard cast; use on primary framed surfaces.
- **Control Cartridge:** A two-pixel dark inset edge plus a three-pixel hard cast; use on tactile buttons.
- **Compact Lift:** A three-pixel hard cast; use on tabs, inspectors, and status blocks.
- **Pressed:** No cast shadow with a three-pixel down-right translation.

Motion reinforces the hardware metaphor: button transitions run for 90ms, transforms use two discrete steps, and active-state pulses run slowly at 1.5–1.8s in two steps. Hover movement never exceeds one pixel. Under reduced-motion preferences, animation and transition durations collapse to 0.01ms and smooth scrolling is disabled.

**The Hard Depth Rule.** Never use blur, glass, or floating-card haze; depth must read as a crisp cartridge edge.

**The Restrained Step Rule.** Animate only state change, selection, and physical press feedback; keep motion short, stepped, and optional.

## Shapes

The base geometry is square (`border-radius: 0`). Primary panels and controls may use the recurring four-pixel cut-corner silhouette, expressed as a stepped clip path rather than a rounded rectangle. Frames are predominantly two cobalt pixels; nested rows use one-pixel soft dividers. Status pips, stage nodes, graph nodes, avatars, and focus geometry remain square.

**The No Soft Corners Rule.** Do not introduce pills, rounded cards, circular icon buttons, or soft container radii into the cartridge world.

**The Two-Pixel Frame Rule.** Two pixels is the default structural border; one pixel belongs to internal separators and compact state labels.

## Components

### Buttons

- **Shape:** Square with four-pixel cut corners, a two-pixel frame, and a 42px minimum height.
- **Primary:** Bright cyan-high face with dark ink; in light mode, the ink becomes white for contrast.
- **Secondary:** Deep blue control face with cyan-high text; hover shifts to the implemented brighter button surface.
- **Danger:** Deep red face with a danger-red frame and readable soft-red label.
- **Hover / Focus / Active:** Hover rises one pixel, focus uses a three-pixel cyan-high outline with a three-pixel offset, and active moves three pixels down-right with no cast shadow.

### Chips

- **Style:** Compact square state labels use a one-pixel semantic frame on a deep panel, a visible square pip when applicable, and explicit status text.
- **State:** Green confirms, gold waits or requests approval, cyan observes or runs, and red cancels or blocks; wording and geometry carry the meaning alongside color.

### Cards / Containers

- **Corner Style:** Square or four-pixel cut cartridge.
- **Background:** Panel for the main surface, deep panel for inset work, and selected panel for active rows.
- **Shadow Strategy:** Primary panels use Panel Cartridge depth; nested cards stay flat or use Compact Lift.
- **Border:** Two-pixel cobalt frames with one-pixel soft internal separators.
- **Internal Padding:** Usually 12–16px, expanding to 22–30px only in the first-viewport thesis field.

### Inputs / Fields

- **Style:** Square input surface, two-pixel cobalt frame, 44px minimum height, and 10px by 11px internal padding.
- **Focus:** The global three-pixel cyan-high focus outline remains outside the control; hover shifts the border toward action blue.
- **Error / Disabled:** Error meaning requires text and danger structure; disabled controls reduce opacity and saturation while retaining their label.

### Navigation

The desktop command rail uses stacked 58px targets, pixel icons, and a cyan framed active cartridge with a right-edge marker. At 720px and below it becomes a fixed five-column bottom bar with a cyan top marker. The compact top-bar safety cartridge is never traded away for more title space.

### Demo Safety Cartridge

The shield-marked `DEMO / SANITIZED` stamp is a signature primitive. On mobile it expands to a fixed two-line block and exposes `LOCAL ONLY` in green; this is product identity and safety disclosure, not optional metadata.

### Mission Stage Route

Five square nodes connected by two-pixel rails show authorization boundary, low-impact discovery, active verification, evidence solidification, and report delivery. Completed segments use green, the active human gate uses gold and a restrained stepped pulse, future segments remain cobalt, and every node keeps a readable label.

### Conversation and Approval

Conversation is the primary operational surface. Operator, system, and agent messages use different framed avatars and named headers; approval stays in an adjacent hard-framed inspector with action, tool, target, reason, and explicit approve/reject controls visible together.

## Do's and Don'ts

### Do:

- **Do** keep the split tactical deck, active sanitized mission, scope lock, stage route, and proof states legible in the first experience.
- **Do** keep `DEMO / SANITIZED` and `LOCAL ONLY` persistently visible, including the fixed two-line mobile safety cartridge.
- **Do** pair every operational color with explicit state text and structural or icon cues.
- **Do** preserve square controls, two-pixel cobalt frames, cut-corner cartridges, hard pixel shadows, and visible keyboard focus.
- **Do** keep narrow-screen state labels at the implemented readable scale and let dense data scroll or reflow.
- **Do** honor reduced-motion preferences and reserve stepped motion for active state or tactile feedback.

### Don't:

- **Don't** turn the experience into a generic metric-card dashboard or hide the operation behind a tour.
- **Don't** use rounded pills, soft cards, blurred shadows, glass effects, gradients without structural purpose, or decorative glow.
- **Don't** use gold as generic emphasis, green as decoration, or red for anything other than danger and negative state.
- **Don't** communicate running, waiting, verified, blocked, or stopped states by color alone.
- **Don't** remove scope, evidence lineage, human approval, ownership, or stop proof to simplify a responsive layout.
- **Don't** compress or hide the mobile safety cartridge to make room for navigation labels.
