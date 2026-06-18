# Token, Copy, And Mobile Patterns

Use this when design decisions involve tokens, UI copy ownership, or mobile-first failure modes.

## Token Decision Examples

| Scenario | Decision | Reason | Approval |
|---|---|---|---|
| Existing semantic color covers the state | Use existing token | Keeps design system coherent | not_required |
| Existing token is close but contrast fails | Propose adjusted text/surface pairing | Accessibility beats visual similarity | may_require_design_review |
| New product-specific status appears | Propose new semantic token | Repeated use and distinct meaning justify token | required |
| One-off marketing color requested in app UI | Reject or isolate | App UI should not fragment tokens | required if pursued |

## Token Decision Output

| Field | Required Content |
|---|---|
| Use Case | Component / state / surface |
| Existing Options | Tokens considered |
| Decision | Reuse / combine / propose new / reject |
| Accessibility | contrast or readability note |
| Impact | affected components and future reuse |
| Approval | not_required / required_before_execution |

## UI Copy Boundary

| Situation | Owner | Handoff |
|---|---|---|
| Button label affects task clarity | `tech-designer` | Include in design spec |
| Error message needs recovery language | `tech-designer` + `contents-quality-manager` | TD defines UX need; CQM improves wording |
| Long explanatory paragraph | `contents-quality-manager` | TD sets placement and density constraint |
| Tone / brand voice | `contents-quality-manager` or `business-director` | TD does not decide brand strategy |
| Legal / policy wording | `business-legal-reviewer` | TD only flags placement and comprehension risk |

## Mobile-First Failure Examples

| Failure | Signal | Design Response |
|---|---|---|
| Desktop table squeezed into mobile | horizontal scroll hides primary action | Use card/list mobile variant or priority columns |
| Sticky footer covers form fields | screenshots show hidden inputs | Add safe area and scroll padding |
| Long labels overflow controls | button or tab text clips | Wrap, shorten, or use icon + accessible label |
| Hover-only affordance | mobile user cannot discover action | Add visible action or touch-friendly menu |
| Modal too tall | actions below fold and keyboard covers content | Use full-screen mobile sheet or anchored actions |
| Chart loses meaning | labels unreadable | Provide summary text and simplified mobile chart |

## Mobile Review Questions

| Question | Expected Answer |
|---|---|
| What is the first visible action on mobile? | It matches the primary user job |
| What information is removed or collapsed? | It is secondary and recoverable |
| What fails with long text? | The design has wrapping or alternative layout |
| What happens with keyboard open? | Inputs and submit path remain reachable |
| Is any action hover-only? | No |
