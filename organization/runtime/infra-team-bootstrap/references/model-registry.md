# Headless Organization Model Registry

Updated: 2026-06-23

This file is the source of truth for Team Role model routing.

Team Role `SKILL.md` files define role identity, Flow Contract, and behavior. They do not define startup model, provider, or execution mode.

## Registry Rules

| Rule | Meaning |
|---|---|
| `status: active` | Role can be selected by organization flow and CLI dispatch. |
| `status: reference` | Role is retained only as a policy/reference role and is not dispatched by default. |
| `status: deprecated` | Legacy role retained only for historical references. |
| `always_active: true` | Role may participate in cross-task control flow; provider evidence is still required for actual work. |
| `primary_model` | Intended model recorded in role metadata and provider evidence. |
| `effective_model` | Runtime evidence from provider output, not this file. |

## Model Routing

| agent_id | team | status | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | long_run_preferred | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| business-director | business | active | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium |  | 上流判断、対外説明、法務リスク、レビュー線調整を安定して扱うため |
| business-information-strategy | business | active | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high |  | 複雑な情報整理、比較軸、意思決定材料の0→1設計を強化するため Opus primary に昇格 |
| business-legal-reviewer | business | active | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium |  | Business Director が必要時に起動する専門メンバー |
| business-marketing-director | business | deprecated | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium |  | 旧 `business-marketing-director` alias。通常は `business-director` を使う |
| business-partnership-manager | business | active | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium |  | Business Director が必要時に起動する専門メンバー |
| business-strategy | business | active | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high |  | 事業上の0→1判断、優先順位、選択肢設計を強化するため Opus primary に昇格 |
| contents-director | contents | active | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium |  | 文脈理解、読者適合、品質基準、チーム内レビュー線の整理を安定して扱うため |
| contents-formatter | contents | active | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium |  | Contents Director が必要時に起動する専門メンバー。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| contents-quality-manager | contents | active | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium |  | Contents Director が必要時に起動する専門メンバー |
| contents-researcher | contents | active | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high |  | 記事化・要約・説明の前提調査で網羅性と正確性を最優先するため Opus primary に昇格（2026-06-07） |
| gate-prompt-formatter | gate | active | true | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.4 | agent | medium | medium |  | 日本語の曖昧な依頼から意図、完了条件、範囲、暗黙の制約を高精度に構造化する役割のため |
| gate-response-humanizer | gate | reference | false | anthropic | claude-haiku-4-5 | claude-sonnet-4-6 | agent | low | low |  | 旧 mandatory final role。現行の最終表示は main transport renderer が `Final Transport Render Check` として実施するため runtime 起動しない |
| gate-task-assessor | gate | reference | false | anthropic | claude-haiku-4-5 | claude-sonnet-4-6, gpt-5.4 | agent | low | low |  | 2026-06-13: runtime agent から外す。TPM の `team-completion-check` command が完了確認を代替し、この行は責務境界参照用に残す |
| gate-task-creator | gate | active | false | anthropic | claude-haiku-4-5 | claude-sonnet-4-6, gpt-5.4 | agent | low | low |  | 2026-06-14: 既定入口は GPF report -> `gtc-scaffold` command -> TPM queue へ移行。GTC provider は legacy / recovery fallback のみ lazy activation し、初期 hook では起動しない |
| gate-task-evaluator | gate | active | true | anthropic | claude-sonnet-4-6 | gpt-5.4, claude-haiku-4-5 | agent | medium | medium |  | 2026-06-13: TPM の `team-completion-check` 通過後に queue activation で起動する。要求充足、レビュー結果、検証結果、Git 差分の関係を横断判断するため |
| gate-task-guardian | gate | reference | false | anthropic | claude-haiku-4-5 | claude-sonnet-4-6, gpt-5.4 | agent | low | low |  | 2026-06-13: runtime agent から外す。GTE final review と Vault final update 後の `finalization-check` command が最終確認を代替し、この行は責務境界参照用に残す |
| git-publisher | dev | active | false | anthropic | claude-sonnet-4-6 | gpt-5.4, claude-haiku-4-5 | agent | medium | medium |  | GTE の `quality_ok` + Git Publication Manifest から queue activation され、commit / push / PR tool flow の順序制御と Publication Result 記録を行う |
| infra-director | infra | active | true | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium |  | Vault 運用、タスク同期、Obsidian 関連作業の責務分解とレビュー線調整を安定して扱うため |
| infra-local-qa | infra | active | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium |  | Infra Director が必要時に起動する専門メンバー。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| infra-task-dispatcher | infra | active | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.2 | agent | medium | medium | gpt-5.2 | Infra Director または定期実行側が必要時に起動する運行メンバー。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| infra-team-bootstrap | infra | active | true | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.4 | agent | medium | medium |  | CLI surface確認、active set 照合、証跡記録、軽量な運行判断が中心のため。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| teams-coordination | tech | deprecated | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium |  | 旧 Claude Teams 実験用。現行 runtime 対象外 |
| teams-developer | tech | deprecated | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | gpt-5.2 | 旧 Claude Teams 実験用。現行 runtime 対象外 |
| teams-project-manager | gate | active | true | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium |  | Gate 起票後の組織横断ルーティング、レビュー線、実行順序を扱う |
| teams-researcher | tech | deprecated | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium |  | 旧 Claude Teams 実験用。現行 runtime 対象外 |
| teams-reviewer | tech | deprecated | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium |  | 旧 Claude Teams 実験用。現行 runtime 対象外 |
| teams-tech-leader | tech | deprecated | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | gpt-5.2 | 旧 Claude Teams 実験用。現行 runtime 対象外。Claude Opus 4.8 release 後の参照整合のため 4.7 から更新 |
| teams-ux-designer | tech | deprecated | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium |  | 旧 Claude Teams 実験用。現行 runtime 対象外 |
| tech-architect | tech | active | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high |  | システム境界、責務分割、長期構造判断の0→1設計を強化するため Opus primary に昇格 |
| tech-backend | tech | active | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-data-structure | tech | active | false | openai | gpt-5.5 | gpt-5.2, claude-sonnet-4-6 | codex | medium | medium | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-debugger | tech | active | false | openai | gpt-5.5 | gpt-5.2, claude-sonnet-4-6 | codex | medium | medium | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-designer | tech | active | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high |  | UI/UX、情報設計、体験骨格の0→1設計を強化するため Opus primary に昇格 |
| tech-devopssec | tech | active | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-director | tech | active | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium |  | 技術判断、依存関係整理、レビュー線設計、チーム内調整を安定して扱うため |
| tech-docs | tech | active | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium |  | Tech Director が必要時に起動する専門メンバー。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| tech-frontend | tech | active | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-infrastructure | tech | active | false | openai | gpt-5.5 | gpt-5.2, claude-sonnet-4-6 | codex | medium | medium | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-lead | tech | active | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | gpt-5.2 | Tech Director が必要時に起動する専門メンバー。2026-06-05 に Claude CLI 実行証跡で `claude-opus-4-8` を確認 |
| tech-mobile | tech | active | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-performance | tech | active | false | openai | gpt-5.5 | gpt-5.2, claude-sonnet-4-6 | codex | medium | medium | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-qa | tech | active | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium |  | Tech Director が必要時に起動する専門メンバー |
| tech-reviewer | tech | active | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium |  | Tech Director が必要時に起動する専門メンバー |
| tech-security | tech | active | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high |  | 認可境界、脅威モデル、重大設計リスクの0→1整理を強化するため Opus primary に昇格 |
| tech-tester | tech | active | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium |  | Tech Director が必要時に起動する専門メンバー |

## Change Policy

- Model changes are made by editing this registry, not Team Role `SKILL.md` files.
- Registry changes must update `Updated:` and include a short reason in `notes` when behavior changes.
- Active Team Roles must have provider, primary model, and execution mode metadata.
- Reference and deprecated entries are retained only so historical references can resolve intended model evidence.
