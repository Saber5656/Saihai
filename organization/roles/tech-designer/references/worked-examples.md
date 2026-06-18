# Tech Designer Worked Examples

Use these as output-shape examples, not as universal UI prescriptions.
Keep the actual task scope, existing components, and product context authoritative.

## Dashboard Example

### Design Scope

| Field | Value |
|---|---|
| Target User / Job | Operations user checks current status, spots anomalies, and opens the next action quickly. |
| Target Screens / Components | Summary metrics, alert list, trend chart, task queue |
| Out of Scope | New analytics model, backend metrics definition, brand refresh |

### UI/UX Assessment

| 観点 | Findings | Risk | Recommendation |
|---|---|---|---|
| 情報設計 | Metrics, alerts, and actions compete for attention | User may scan the wrong number first | Put action-critical status first, then trend, then diagnostics |
| 視覚階層 | All cards have similar weight | Dashboard becomes decorative rather than operational | Use one primary status band and lower-weight secondary panels |
| 状態設計 | Empty / stale / loading states are undefined | Operators cannot distinguish no data from broken data | Define stale timestamp, empty state, and error banner |
| アクセシビリティ | Color-only status is likely | Status may be missed | Pair color with labels/icons and ARIA text |

### Design Spec / Frontend Handoff

| Target | Layout | Token / Visual | State | Responsive | Accessibility |
|---|---|---|---|---|---|
| Status summary | 12-column desktop, 1-column mobile | Semantic success/warn/error tokens | loading, stale, error | Collapse metrics into prioritized list on mobile | Status labels are text, not color-only |
| Alert queue | Fixed row height with wrapped title | Existing table/list tokens | empty, filtered-empty, retry | Full width on mobile | Keyboard focus on each row action |
| Trend chart | Aspect-ratio constrained | Existing chart palette | loading, no-data | Hide secondary series on mobile if needed | Text alternative for summary trend |

### Validation Handoff

| Recipient | Check | Evidence Needed | Blocking |
|---|---|---|---|
| `tech-tester` | viewport screenshots | mobile, desktop, stale/error states | yes |
| `tech-qa` | acceptance criteria | primary status visible without scroll on desktop | yes |

## Form Example

### Design Scope

| Field | Value |
|---|---|
| Target User / Job | User completes a multi-field request without losing entered data. |
| Target Screens / Components | Form layout, validation messages, submit state, confirmation |
| Out of Scope | Server validation rules, notification delivery |

### UI/UX Assessment

| 観点 | Findings | Risk | Recommendation |
|---|---|---|---|
| 操作導線 | Required fields and submit constraints are unclear | User fails late at submit | Mark required fields and validate progressively |
| エラー回復 | Error location may be hard to find | User cannot fix quickly | Add summary error and field-level messages |
| 入力保持 | Retry behavior is unspecified | Data loss on failure | Keep form values on error and expose retry |
| モバイル | Multi-column forms can break | Poor touch usability | Use single-column mobile layout |

### Design Spec / Frontend Handoff

| Target | Layout | Token / Visual | State | Responsive | Accessibility |
|---|---|---|---|---|---|
| Field group | One logical group per section | Existing form tokens | default, focus, invalid, disabled | Single column below tablet | Label is programmatically associated |
| Error summary | Top of form after failed submit | Error semantic token | hidden, visible | Sticky only if existing pattern allows | Focus moves to summary on submit failure |
| Submit action | Bottom aligned with secondary action | Primary button token | default, loading, disabled, success | Full-width mobile if existing pattern supports | Loading state announces progress |

### Validation Handoff

| Recipient | Check | Evidence Needed | Blocking |
|---|---|---|---|
| `tech-tester` | keyboard and validation flow | tab order, focus after error, retry preserves values | yes |
| `contents-quality-manager` | field copy | error text is specific and recoverable | no |

## Content-Heavy Page Example

### Design Scope

| Field | Value |
|---|---|
| Target User / Job | Reader scans dense content, finds relevant section, and acts on one item. |
| Target Screens / Components | Table of contents, content sections, callouts, related actions |
| Out of Scope | Editorial rewrite, SEO strategy |

### UI/UX Assessment

| 観点 | Findings | Risk | Recommendation |
|---|---|---|---|
| 情報設計 | Long content needs scanning support | Reader misses key section | Add sticky local nav on desktop and compact jump menu on mobile |
| 視覚階層 | Headings and callouts may blend together | Important notes are missed | Use heading scale and one callout style per purpose |
| UI copy境界 | Some clarity issues are content quality | TD may over-own writing | Send copy tone/readability to CQM |
| レスポンシブ | Side nav may crowd mobile | Small viewport friction | Collapse nav to select/jump list |

### Design Spec / Frontend Handoff

| Target | Layout | Token / Visual | State | Responsive | Accessibility |
|---|---|---|---|---|---|
| Local nav | Sticky left rail desktop | Existing nav tokens | active section, hover, focus | Collapse to jump menu on mobile | Current section announced |
| Callout | Full-width within content measure | Semantic info/warn tokens | default | No nested cards | Icon has hidden label or decorative role |
| Related action | End of relevant section | Existing button/link tokens | default, focus | Inline on desktop, stacked mobile | Link text describes destination |

### Validation Handoff

| Recipient | Check | Evidence Needed | Blocking |
|---|---|---|---|
| `tech-tester` | text fit and navigation | mobile screenshot, desktop scroll, active section state | yes |
| `contents-quality-manager` | copy density | headings and callout text readability | no |
