---
name: LabPilot
description: Quiet Gallery visual system for an evidence-traceable laboratory procurement workspace.
colors:
  gallery-canvas: "#F4F1E8"
  gallery-raised: "#FBF9F2"
  specimen-paper: "#FFFDF7"
  archive-green: "#103B35"
  archive-green-hover: "#174C44"
  carbon-ink: "#17201E"
  pilot-cobalt: "#315EFB"
  pilot-cobalt-deep: "#2146C7"
  review-coral: "#FF6948"
  review-coral-soft: "#FFE4DC"
  signal-lime: "#D9F26A"
  ruled-line: "#CAD3CC"
  secondary-copy: "#64726D"
  failure-red: "#B42318"
typography:
  display:
    fontFamily: "Instrument Serif, Times New Roman, serif"
    fontSize: "clamp(3.2rem, 6vw, 6.3rem)"
    fontWeight: 400
    lineHeight: 0.91
    letterSpacing: "-0.04em"
  title:
    fontFamily: "Instrument Serif, Times New Roman, serif"
    fontSize: "2rem"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "-0.03em"
  body:
    fontFamily: "Manrope, Microsoft YaHei, system-ui, sans-serif"
    fontSize: "0.82rem"
    fontWeight: 500
    lineHeight: 1.75
  label:
    fontFamily: "Manrope, Microsoft YaHei, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 700
    lineHeight: 1.2
  technical:
    fontFamily: "DM Mono, Cascadia Mono, monospace"
    fontSize: "0.62rem"
    fontWeight: 500
    lineHeight: 1.4
rounded:
  square: "0px"
  marker: "50%"
  status: "999px"
spacing:
  micro: "4px"
  compact: "8px"
  control: "12px"
  cluster: "16px"
  section: "24px"
  canvas: "32px"
  gallery: "48px"
components:
  button-primary:
    backgroundColor: "{colors.pilot-cobalt}"
    textColor: "{colors.specimen-paper}"
    rounded: "{rounded.square}"
    padding: "0 17px"
    height: "46px"
    typography: "{typography.label}"
  button-primary-hover:
    backgroundColor: "{colors.archive-green}"
    textColor: "{colors.specimen-paper}"
    rounded: "{rounded.square}"
    padding: "0 17px"
    height: "46px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.archive-green}"
    rounded: "{rounded.square}"
    padding: "0 17px"
    height: "46px"
    typography: "{typography.label}"
  input-field:
    backgroundColor: "{colors.specimen-paper}"
    textColor: "{colors.carbon-ink}"
    rounded: "{rounded.square}"
    padding: "0 13px"
    height: "48px"
    typography: "{typography.body}"
  status-chip:
    backgroundColor: "{colors.signal-lime}"
    textColor: "{colors.archive-green}"
    rounded: "{rounded.status}"
    padding: "4px 9px"
    typography: "{typography.technical}"
---

# Design System: LabPilot

## Overview

**Creative North Star: "Quiet Gallery"**

LabPilot behaves like a carefully edited laboratory exhibition catalogue: warm paper, decisive type, precise ruled lines, and isolated samples of color. The interface is operational rather than promotional, but it keeps enough editorial tension to make dense procurement work feel authored instead of administered.

The system pairs oversized, low-weight serif titles with compact sans-serif controls and monospace evidence markers. Most depth comes from spacing, tonal surfaces, and thin rules. Motion follows a measured instrument track: one directional entrance, slow orbital status cues, and restrained state transitions that stop when the underlying request stops.

**Key Characteristics:**

- Warm, paper-like working surfaces with deep archive-green anchors.
- Asymmetric grids and ruled measurement lines instead of generic dashboard cards.
- Sparse cobalt, coral, and lime signals with fixed semantic responsibilities.
- Honest states: unavailable stays static, while motion belongs only to real work in progress.
- Chinese-first operational copy with English reserved for the LabPilot mark and technical fields.

## Colors

The palette stays quiet over most of the canvas, then uses three highly legible laboratory markers for action, review, and completion.

### Primary

- **Pilot Cobalt:** The main action, link, focus, and active-Agent color. Its rarity makes each blue element actionable.
- **Archive Green:** The structural anchor for the sidebar, headings, and high-confidence dark surfaces.

### Secondary

- **Review Coral:** Marks missing evidence, pending confirmation, warnings, and critical counts; it never substitutes for failure red.
- **Signal Lime:** Marks online, complete, or selected states and carries the brand signal inside dark-green surfaces.

### Neutral

- **Gallery Canvas:** The persistent warm-white application background.
- **Specimen Paper:** The slightly brighter field surface for forms and working material.
- **Carbon Ink:** The default high-contrast reading color.
- **Ruled Line:** The primary divider and field boundary.
- **Secondary Copy:** Supporting text and descriptions, never primary instructions.
- **Failure Red:** Reserved for actual failure and destructive risk.

**The Three Marker Rule.** Cobalt means action or Agent activity, coral means review or warning, and lime means success or online; do not exchange their roles.

**The Honest Red Rule.** Red appears only when something has failed or may be lost. A pending item remains coral.

## Typography

**Display Font:** Instrument Serif (with Times New Roman fallback)
**Body Font:** Manrope (with Microsoft YaHei and system-ui fallbacks)
**Label/Mono Font:** DM Mono (with Cascadia Mono fallback)

**Character:** Instrument Serif gives the product its exhibition-catalogue voice, Manrope keeps Chinese operational copy compact and direct, and DM Mono identifies IDs, versions, endpoints, and machine state without turning the entire interface into a developer tool.

### Hierarchy

- **Display** (400, responsive 3.2–6.3rem, 0.91 line-height): Page identity and large task titles. The start-page wordmark is a deliberate brand exception and may exceed this scale.
- **Title** (400, 2rem, 1 line-height): Module and card titles.
- **Body** (500, 0.82rem, 1.75 line-height): Explanations and working copy, kept below roughly 70 characters per line where practical.
- **Label** (700, 0.75rem): Buttons, form labels, and navigation.
- **Technical** (500, 0.62rem, 1.4 line-height): Task IDs, versions, API state, and short status labels.

**The Serif for Orientation Rule.** Serif type names a place or section; it does not carry dense instructions, validation, or long-form operational copy.

**The Monospace for Evidence Rule.** Monospace is restricted to machine-readable values and compact status, never used as a decorative texture.

## Layout

The start page is a single centered brand composition with one horizontal action group. The workbench changes to a fixed 282px archive-green sidebar and a flexible canvas capped at 1500px. Main pages use large top and bottom breathing room, thin full-width rules, and asymmetric content grids; nested card stacks are avoided.

At 1180px the sidebar narrows and supporting panels compress. At 1024px the sidebar becomes an off-canvas drawer, two-column API panels become a single column, and contextual task information moves below the conversation. At 760px working grids collapse, controls wrap, task tabs scroll horizontally, and the start-page actions stack. The minimum supported viewport is 390px, with every actionable target at least 44px.

**The One Canvas Rule.** A route should read as one edited field separated by lines and spacing, not as a pile of equal floating cards.

## Elevation & Depth

The system is flat by default. Tonal contrast, ruled lines, and dark-versus-light planes establish hierarchy. A wide, soft green-tinted shadow is reserved for temporary elevation such as the mobile drawer or a lifted control; permanent content panels do not combine a border with a heavy shadow.

### Shadow Vocabulary

- **Ambient Lift** (`0 20px 56px rgba(16, 59, 53, .07)`): Rare soft separation for transient or emphasized surfaces.
- **Drawer Lift** (`22px 0 50px rgba(0, 0, 0, .20)`): Mobile navigation only.

**The Flat-at-Rest Rule.** Working surfaces remain flat until interaction or temporary stacking requires elevation.

## Shapes

Primary forms are square and ruled. Buttons, fields, panels, and navigation selections use zero-radius corners; this keeps the page closer to an instrument catalogue than a consumer dashboard. Circles are reserved for samples, orbit points, stage markers, and compact counts. Fully rounded pills are reserved for status tags only.

**The Circle Means Signal Rule.** A circle indicates a point, state, stage, or orbit; it is not a generic container.

## Components

### Buttons

- **Shape:** Square-edged with a minimum 46px height and a compact horizontal label.
- **Primary:** Pilot Cobalt with light text; the arrow or action icon stays aligned to the far edge.
- **Hover / Focus:** Lift by 2px and shift to Archive Green; keyboard focus uses a 3px cobalt outline outside the control.
- **Secondary / Ghost:** Outlined green for alternative actions and unboxed cobalt for low-emphasis inline actions.

### Chips

- **Style:** Compact monospace status labels with fully rounded ends.
- **State:** Lime communicates success, coral-soft communicates warning, gray-green communicates prototype or unavailable, and failure-red is reserved for actual errors.

### Cards / Containers

- **Corner Style:** Square-edged.
- **Background:** Transparent warm canvas or Specimen Paper depending on density.
- **Shadow Strategy:** Flat at rest; hierarchy comes from rules and tonal contrast.
- **Border:** One thin Ruled Line edge or divider, never a decorative boxed stack.
- **Internal Padding:** 20–42px based on viewport and information density.

### Inputs / Fields

- **Style:** Bright paper field, 1px Ruled Line border, square corners, and at least 48px height.
- **Focus:** Cobalt border plus a soft three-pixel cobalt focus halo.
- **Error / Disabled:** Failure uses the dedicated red family; disabled controls reduce emphasis without hiding their label.

### Navigation

The desktop navigation is a dark archive strip with light text, subtle tonal hover, and a two-pixel lime active rail. Below 1024px it becomes a keyboard-accessible off-canvas drawer with a scrim and a persistent 48px menu control.

### Pilot Beacon

The signature Agent marker uses a cobalt orbit only while a real request is running. Idle, completed, unavailable, and failed states are static. Reduced-motion mode removes loops and preserves status through color and text.

## Do's and Don'ts

### Do:

- **Do** use warm whitespace and thin ruled lines to separate stages before adding another container.
- **Do** keep cobalt, coral, and lime attached to their fixed semantic roles.
- **Do** show unavailable, empty, failure, and recovery states in direct Chinese operational language.
- **Do** pair one authored motion moment with static, readable fallback states.
- **Do** use Lucide icons at a consistent stroke weight and provide an accessible label where the icon stands alone.

### Don't:

- **Don't** fabricate model connectivity, evidence, recommendation, or progress to make a screen look complete.
- **Don't** turn the workspace into a grid of identical rounded dashboard cards.
- **Don't** use gradients, glass blur, emoji, or colored text effects as decoration.
- **Don't** use coral and red interchangeably, or apply lime to a merely pending state.
- **Don't** animate prototype or unavailable states as though back-end work were in progress.
