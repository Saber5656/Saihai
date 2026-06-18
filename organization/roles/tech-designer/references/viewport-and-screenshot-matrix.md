# Viewport And Screenshot Matrix

Use this when UI behavior depends on viewport, state, keyboard/focus, or screenshot review.

## Default Viewports

| Viewport | Size | Purpose |
|---|---|---|
| Mobile | 390 x 844 | Single-column layout, touch target, text wrapping |
| Tablet | 768 x 1024 | Transitional layout, two-column behavior, navigation collapse |
| Desktop | 1440 x 900 | Primary work layout, density, keyboard path |
| Wide | 1920 x 1080 | Max-width, empty space, extended tables/charts |

If the product has a known viewport matrix, use the product matrix instead.

## Screenshot Acceptance

| Check | Pass Criteria |
|---|---|
| No overlap | Text, buttons, controls, and fixed elements do not overlap |
| Text fit | Labels and high-risk long strings fit or wrap intentionally |
| Stable layout | Loading, empty, error, and success states do not shift core layout unexpectedly |
| Visible primary action | Main action is visible in the expected viewport without hidden affordances |
| Scroll behavior | Sticky headers, nav, or action bars do not cover content |
| Color / contrast | Semantic states remain readable in screenshot and token review |

## State Matrix

| State | Screenshot Required | Notes |
|---|---|---|
| default | yes | Baseline |
| loading | when async data exists | skeleton or progress indicator |
| empty | when list/content can be empty | explains next action |
| error | when recoverable failure exists | message and retry path visible |
| disabled | when action can be unavailable | reason is clear if needed |
| focus | when keyboard path matters | focus-visible is clear |

## Playwright / Visual Regression Handoff

| Field | Required |
|---|---|
| Target route | URL or local path |
| Setup data | fixture, auth state, mock, or seed |
| Viewports | selected from matrix |
| States | default / loading / empty / error / focus as applicable |
| Assertions | no overlap, text fit, main action visible, console clean |
| Artifacts | screenshots, trace if interaction is in scope |

`tech-designer` defines what must be visible and acceptable.
`tech-tester` decides the exact test harness and records execution evidence.
