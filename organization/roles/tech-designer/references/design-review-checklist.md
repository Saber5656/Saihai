# Tech Designer Review Checklist

Use this checklist before handing design work to `tech-frontend`, `tech-tester`, or `tech-qa`.

## Design Scope

| Check | Pass Criteria |
|---|---|
| Task scope is fixed | Parent task, tech task, target screen, and out-of-scope items are named |
| Target user/job is explicit | One sentence states who is using the UI and what they need to complete |
| Existing system is checked | Existing components, tokens, and layout patterns are preferred |
| Approval needs are identified | New tokens, brand changes, or major UX direction changes are flagged |

## UI/UX Assessment

| Check | Pass Criteria |
|---|---|
| Information hierarchy | Primary, secondary, and supporting information are distinct |
| Main action path | The shortest successful path is clear |
| Recovery path | Error, empty, retry, and stale states have user actions |
| Visual hierarchy | Size, spacing, weight, and color support the task rather than decoration |
| Interaction feedback | hover, focus, active, loading, disabled are covered where relevant |
| Accessibility | keyboard, focus order, contrast, labels, hit areas, and reduced motion are considered |

## Design Spec

| Check | Pass Criteria |
|---|---|
| Layout | Width, height, max-width, overflow, and responsive behavior are clear |
| Spacing | Uses existing spacing scale and avoids one-off values unless justified |
| Color | Uses existing semantic tokens and avoids color-only meaning |
| Typography | Text hierarchy, wrapping, and text-fit are specified |
| State | default, loading, error, empty, hover, focus, active, disabled are covered as needed |
| Motion | Duration, easing, and reduced-motion handling are named if animation is used |

## Generated UI Review

| Check | Pass Criteria |
|---|---|
| Candidate source is recorded | image model, Claude Design, Artifact, code model, or manual prototype |
| Candidate type is explicit | static image, interactive prototype, or browser-rendered code |
| Static image is not source of truth | image-only output is marked `exploration_only` |
| Browser evidence exists when possible | screenshot, console, network, Lighthouse, and performance evidence are captured when rendered |
| Review verdict is actionable | `exploration_only`, `ready_for_frontend_handoff`, or `needs_revision` |

## Handoff

| Recipient | Required Payload |
|---|---|
| `tech-frontend` | layout, components, token choices, states, responsive behavior |
| `tech-tester` | viewport matrix, state cases, keyboard/focus checks, screenshot expectations |
| `tech-qa` | acceptance criteria, residual risk, verification links |
| `contents-quality-manager` | copy, tone, explanation density, readability questions |
| `tech-reviewer` | decision matrix, scope, rejected alternatives, handoff completeness |
