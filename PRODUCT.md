# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

LabPilot primarily serves laboratory procurement staff, research personnel, and project owners who need to compare real instrument options while preserving the source, status, and uncertainty behind each conclusion.

## Product Purpose

LabPilot turns a natural-language procurement scenario and user-supplied supplier material into a structured, reviewable laboratory-instrument selection task. Success means the user can move from requirements to candidates, evidence, fit checks, quotations, and a versioned report without losing the boundary between extracted text, verified facts, inference, and unresolved gaps.

## Positioning

LabPilot is an evidence-traceable procurement workspace rather than a recommendation chatbot: conclusions remain attached to task state, source material, review status, risk, and missing evidence.

## Operating Context

Users work with procurement scenarios, manuals, quotations, parameter images, spreadsheets, calibration or inspection material, candidate models, reports, and immutable task history. The interface is Chinese-first, while the LabPilot brand and necessary technical fields remain in English.

## Capabilities and Constraints

- The existing React and Vite frontend consumes a FastAPI API under `/api/v1`.
- Existing task, requirement confirmation, document upload, candidate recognition, report, and history endpoints remain the source of truth.
- Evidence, fit-evaluation, provider, deployment, and model capabilities that lack a live backend must be shown as unavailable or prototype states, never as successful online results.
- The frontend does not parse documents, perform OCR, retrieve evidence, score candidates, or invent recommendations.
- Connection settings, including API keys, may be saved only in this browser's `localStorage` when the user asks to reuse them. They must never enter URLs, the repository, SQLite, server logs, or API responses, and the interface must provide a clear-local-settings action.
- Historic prices are references rather than current quotations. Extracted supplier text is not automatically a verified product fact.

## Brand Commitments

- Product name: LabPilot.
- The entry page contains only the LabPilot wordmark, a `使用说明` action, and a `开始使用` action.
- The information architecture follows the confirmed MarketLens pattern: a minimal entry page leading into a fixed-sidebar workspace.
- The interface must feel professional, restrained, precise, and distinctly laboratory-oriented without AI marketing language.

## Evidence on Hand

- Product and UI requirements: `UI_DESIGN_PROMPT.md`.
- Existing task workflow and API behavior: `frontend/src/main.jsx` and the FastAPI routes under `backend/`.
- MarketLens is a structural and writing reference only; its code, data, credentials, and runtime are not part of LabPilot.
- No verified provider or deployment backend is currently available, so provider health and deployment success must not be fabricated.

## Product Principles

1. Evidence before opinion.
2. Preserve uncertainty instead of hiding it.
3. Every task state should reveal the next honest action.
4. Keep structured procurement work accessible from, but not trapped inside, the Agent conversation.
5. Prefer clear operational language over AI-flavored promises.

## Accessibility & Inclusion

The web interface targets WCAG AA contrast, keyboard-complete operation, 44px minimum touch targets, responsive behavior down to a 390px viewport, and a complete reduced-motion mode.
