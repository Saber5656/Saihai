# Generated UI Review Workflow

Use this when a task asks for UI generation from a spec, image model output, Claude Design / Artifact prototype, or code-generated page.

## Workflow

| Step | Owner | Artifact | Acceptance |
|---|---|---|---|
| 1. Normalize spec | `tech-designer` | Design brief and review rubric | Target user, screens, states, constraints, and acceptance criteria are explicit |
| 2. Generate candidates | vendor adapter / code model | static image, interactive prototype, or browser-rendered code | Candidate source and prompt/spec version are recorded |
| 3. Render if possible | `tech-frontend` or prototype harness | local page, Artifact, or browser session | Candidate can be inspected beyond a static screenshot |
| 4. Capture evidence | Chrome DevTools MCP / browser tooling | screenshot, console, network, Lighthouse, performance trace | Evidence packet is attached or summarized |
| 5. Review design | `tech-designer` | Generated UI Review Packet | Verdict is actionable |
| 6. Validate | `tech-tester` | viewport/state/keyboard/accessibility checks | TT verdict uses allowed vocabulary |
| 7. Gate | `tech-qa` | QA verdict | Candidate is accepted, revised, or kept as exploration only |

## Vendor Adapter Rubric

| Candidate Source | Use | Required Guardrail |
|---|---|---|
| Image generation | Visual direction, layout exploration, mood comparison | Cannot be implementation source of truth |
| Claude Design / Artifact | Interactive prototype and stakeholder review | Must still be checked against existing components and tokens |
| Code model | Browser-rendered candidate and FE handoff | Must pass lint/build/test expectations of target repo before implementation |
| Manual prototype | Precise UX demonstration | Must record assumptions and differences from production code |

## Generated UI Review Packet

| Field | Required Content |
|---|---|
| Source Spec | Task link, spec version, prompt or design brief |
| Generator | Tool/vendor/model or manual source |
| Candidate Type | static image / interactive prototype / browser-rendered code |
| Browser Evidence | screenshot, console summary, network summary, Lighthouse summary, performance notes |
| Design Review Findings | information architecture, flow, visual hierarchy, state coverage, accessibility, copy boundary |
| Acceptance | `exploration_only` / `ready_for_frontend_handoff` / `needs_revision` |
| Handoff | next owner and blocking items |

## Chrome DevTools MCP Evidence

Use Chrome DevTools MCP when a candidate can be opened in a browser.

| Evidence | Required When | Review Use |
|---|---|---|
| Screenshot | Always for browser-rendered candidate | Layout, viewport, text-fit, visual regression baseline |
| Console messages | Always | Runtime errors, hydration problems, missing assets |
| Network requests | API-backed or asset-heavy UI | Failed calls, slow assets, blocked requests |
| Lighthouse audit | Reviewable page state | Accessibility, best practices, SEO, agentic browsing |
| Performance trace | Interaction or render performance is in scope | Long tasks, layout shifts, slow interactions |
| Selected request detail | Failed or suspicious request exists | Payload / response debugging |

## Acceptance Criteria

| Verdict | Meaning |
|---|---|
| `exploration_only` | Useful as visual direction but not implementation input |
| `ready_for_frontend_handoff` | Enough evidence and spec detail for FE to implement or refine |
| `needs_revision` | Candidate has blocking design, runtime, accessibility, or system-fit issues |

Never approve generated UI solely because it looks plausible.
The accepted source of truth is the normalized design brief, existing design system, and browser-rendered evidence.
