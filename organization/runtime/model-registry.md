# Resident Organization Model Registry

Updated: 2026-06-12

This file is the source of truth for Team Role model routing.

Team Role `SKILL.md` files define role identity, Flow Contract, and behavior. They do not define startup model, provider, or execution mode.

## Registry Rules

| Rule | Meaning |
|---|---|
| `resident_target: true` | ITB may register a resident agent instance for the role |
| `resident_target: false` | Compatibility or deprecated role; do not resident-start |
| `always_active: true` | Gate / Infra operating roles remain metadata_ready across tasks; response_active still requires runtime evidence |
| `primary_model` | Intended model recorded in Resident Roster |
| `effective_model` | Runtime evidence from transcript / session log, not this file |
| `startup_profile: provider_cli` | Start an ITB-owned provider CLI tmux process at SessionStart. The process is idle until a YAML queue nudge tells it which payload to read. |
| `startup_profile: lazy_activation` | Register in roster, but do not create a SessionStart tmux process; Director must request ITB activation before task work |
| `startup_profile: compatibility_only` | Keep model metadata for legacy references, but do not start as resident |

## Model Routing

| agent_id | team | status | resident_target | always_active | provider | primary_model | fallback_models | execution_mode | cost_tier | quality_tier | startup_profile | long_run_preferred | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| business-director | business | active | true | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium | provider_cli |  | 上流判断、対外説明、法務リスク、レビュー線調整を安定して扱うため |
| business-information-strategy | business | active | true | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | lazy_activation |  | 複雑な情報整理、比較軸、意思決定材料の0→1設計を強化するため Opus primary に昇格 |
| business-legal-reviewer | business | active | true | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium | lazy_activation |  | Business Director が必要時に起動する専門メンバー |
| business-marketing-director | business | deprecated | false | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium | compatibility_only |  | 旧 `business-marketing-director` alias。通常は `business-director` を使う |
| business-partnership-manager | business | active | true | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium | lazy_activation |  | Business Director が必要時に起動する専門メンバー |
| business-strategy | business | active | true | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | lazy_activation |  | 事業上の0→1判断、優先順位、選択肢設計を強化するため Opus primary に昇格 |
| contents-director | contents | active | true | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium | provider_cli |  | 文脈理解、読者適合、品質基準、チーム内レビュー線の整理を安定して扱うため |
| contents-formatter | contents | active | true | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium | lazy_activation |  | Contents Director が必要時に起動する専門メンバー。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| contents-quality-manager | contents | active | true | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium | lazy_activation |  | Contents Director が必要時に起動する専門メンバー |
| contents-researcher | contents | active | true | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | lazy_activation |  | 記事化・要約・説明の前提調査で網羅性と正確性を最優先するため Opus primary に昇格（2026-06-07） |
| gate-prompt-formatter | gate | active | true | true | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.4 | agent | medium | medium | provider_cli |  | 日本語の曖昧な依頼から意図、完了条件、範囲、暗黙の制約を高精度に構造化する役割のため |
| gate-response-humanizer | gate | reference | false | false | anthropic | claude-haiku-4-5 | claude-sonnet-4-6 | agent | low | low | compatibility_only |  | 旧 mandatory final role。現行の最終表示は main transport renderer が `Final Transport Render Check` として実施するため resident 起動しない |
| gate-task-assessor | gate | active | true | true | anthropic | claude-haiku-4-5 | claude-sonnet-4-6, gpt-5.4 | agent | medium | medium | provider_cli |  | Vault 上の完了証跡、team task、blocker の定型確認が中心のため。2026-06-12: `acceptEdits` 既定化により Haiku primary へ戻す |
| gate-task-creator | gate | active | true | true | anthropic | claude-haiku-4-5 | claude-sonnet-4-6, gpt-5.4 | agent | medium | medium | provider_cli |  | 定型的な起票、Vault リンク初期化、正本ポリシー照合、handoff 作成が中心のため。2026-06-12: `acceptEdits` 既定化により Haiku primary へ戻す |
| gate-task-evaluator | gate | active | true | true | anthropic | claude-sonnet-4-6 | gpt-5.4, claude-haiku-4-5 | agent | medium | medium | provider_cli |  | 要求充足、レビュー結果、検証結果、Git 差分の関係を横断判断するため |
| gate-task-guardian | gate | active | true | true | anthropic | claude-haiku-4-5 | claude-sonnet-4-6, gpt-5.4 | agent | medium | medium | provider_cli |  | 完了証跡、Vault 同期、commit hash の定型検査が中心のため。2026-06-12: `acceptEdits` 既定化により Haiku primary へ戻す |
| infra-director | infra | active | true | true | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium | provider_cli |  | Vault 運用、タスク同期、Obsidian 関連作業の責務分解とレビュー線調整を安定して扱うため |
| infra-local-qa | infra | active | true | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium | lazy_activation |  | Infra Director が必要時に起動する専門メンバー。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| infra-task-dispatcher | infra | active | true | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.2 | agent | medium | medium | lazy_activation | gpt-5.2 | Infra Director または定期実行側が必要時に起動する運行メンバー。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| infra-team-bootstrap | infra | active | true | true | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.4 | agent | medium | medium | provider_cli |  | 起動確認、roster 照合、証跡記録、軽量な運行判断が中心のため。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| teams-coordination | tech | deprecated | false | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium | compatibility_only |  | 旧 Claude Teams 実験用。現行 resident 対象外 |
| teams-developer | tech | deprecated | false | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | compatibility_only | gpt-5.2 | 旧 Claude Teams 実験用。現行 resident 対象外 |
| teams-project-manager | gate | active | true | true | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium | provider_cli |  | Gate 起票後の組織横断ルーティング、レビュー線、実行順序を扱う |
| teams-researcher | tech | deprecated | false | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium | compatibility_only |  | 旧 Claude Teams 実験用。現行 resident 対象外 |
| teams-reviewer | tech | deprecated | false | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | compatibility_only |  | 旧 Claude Teams 実験用。現行 resident 対象外 |
| teams-tech-leader | tech | deprecated | false | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | compatibility_only | gpt-5.2 | 旧 Claude Teams 実験用。現行 resident 対象外。Claude Opus 4.8 release 後の参照整合のため 4.7 から更新 |
| teams-ux-designer | tech | deprecated | false | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5, gpt-5.5 | agent | medium | medium | compatibility_only |  | 旧 Claude Teams 実験用。現行 resident 対象外 |
| tech-architect | tech | active | true | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | lazy_activation |  | システム境界、責務分割、長期構造判断の0→1設計を強化するため Opus primary に昇格 |
| tech-backend | tech | active | true | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | lazy_activation | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-data-structure | tech | active | true | false | openai | gpt-5.5 | gpt-5.2, claude-sonnet-4-6 | codex | medium | medium | lazy_activation | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-debugger | tech | active | true | false | openai | gpt-5.5 | gpt-5.2, claude-sonnet-4-6 | codex | medium | medium | lazy_activation | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-designer | tech | active | true | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | lazy_activation |  | UI/UX、情報設計、体験骨格の0→1設計を強化するため Opus primary に昇格 |
| tech-devopssec | tech | active | true | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | lazy_activation | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-director | tech | active | true | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium | provider_cli |  | 技術判断、依存関係整理、レビュー線設計、チーム内調整を安定して扱うため |
| tech-docs | tech | active | true | false | anthropic | claude-sonnet-4-6 | claude-haiku-4-5 | agent | medium | medium | lazy_activation |  | Tech Director が必要時に起動する専門メンバー。2026-06-11: auto mode 対応のため haiku-4-5 から昇格（Haiku 4.5 は auto/fast 非対応） |
| tech-frontend | tech | active | true | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | lazy_activation | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-infrastructure | tech | active | true | false | openai | gpt-5.5 | gpt-5.2, claude-sonnet-4-6 | codex | medium | medium | lazy_activation | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-lead | tech | active | true | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | lazy_activation | gpt-5.2 | Tech Director が必要時に起動する専門メンバー。2026-06-05 に Claude CLI 実行証跡で `claude-opus-4-8` を確認 |
| tech-mobile | tech | active | true | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | lazy_activation | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-performance | tech | active | true | false | openai | gpt-5.5 | gpt-5.2, claude-sonnet-4-6 | codex | medium | medium | lazy_activation | gpt-5.2 | Tech Director が必要時に起動する専門メンバー |
| tech-qa | tech | active | true | false | anthropic | claude-sonnet-4-6 | gpt-5.5 | agent | medium | medium | lazy_activation |  | Tech Director が必要時に起動する専門メンバー |
| tech-reviewer | tech | active | true | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | lazy_activation |  | Tech Director が必要時に起動する専門メンバー |
| tech-security | tech | active | true | false | anthropic | claude-opus-4-8 | claude-sonnet-4-6, gpt-5.5 | agent | high | high | lazy_activation |  | 認可境界、脅威モデル、重大設計リスクの0→1整理を強化するため Opus primary に昇格 |
| tech-tester | tech | active | true | false | openai | gpt-5.5 | claude-sonnet-4-6, gpt-5.2 | codex | medium | medium | lazy_activation |  | Tech Director が必要時に起動する専門メンバー |

## Change Policy

- Model changes are made by editing this registry, not Team Role `SKILL.md` files.
- Registry changes must update `Updated:` and include a short reason in `notes` when behavior changes.
- ITB must fail bootstrap when an active Team Role is missing from this registry.
- Deprecated compatibility entries are retained only so legacy references can resolve intended model evidence.
