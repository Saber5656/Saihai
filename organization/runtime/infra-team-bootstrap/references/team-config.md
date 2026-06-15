# Resident Organization Team Config

このファイルは、チャットセッション単位の Organization Instance を起動する共通契約である。Claude、Codex、tmux などの実行環境固有手順は adapter に分離する。

## Source Of Truth

| Item | Source |
|---|---|
| 組織原則 | Agents-Vault `03-Contexts/Policies/AI-Organization.md` |
| Gate I/O | Agents-Vault `03-Contexts/Policies/Gate-IO-Contract.md` |
| ロール定義 | 各 `skills/<agent_id>/SKILL.md` frontmatter and Flow Contract |
| モデル設定 | `references/model-registry.md` |
| Completion chain / final gate required sections | `config/completion-chain.yaml` |
| 起動 lifecycle | `infra-team-bootstrap` |
| 実行環境差分 | `references/adapters/` |

## Lifecycle

| Event | Handler | Result |
|---|---|---|
| `SessionStart` | `infra-team-bootstrap` | Organization Instance bootstrap |
| `SessionResume` | `infra-team-bootstrap` | Roster check and partial recovery |
| `PromptSubmit` | `gate-prompt-formatter` | Normal Gate intake |
| `SessionArchive` / `SessionClose` / `SessionEnd` | `infra-team-bootstrap` shutdown mode | Handoff summary, scoped tmux shutdown, and archived roster |

`SessionStart` は、新しいチャット/作業セッション作成直後、最初のユーザープロンプトを処理する前の初期化タイミングを指す。

## Organization Instance Scope

| Concept | Rule |
|---|---|
| Chat session | 1 Organization Instance |
| Project | 原則 1 chat session |
| Multiple projects | 複数 chat session / 複数 Organization Instance |
| Skill | 共有ロール定義 |
| Agent instance | chat session ごとの起動実体 |

GPF は Organization Instance ごとに起動する。全チャット横断の singleton として扱わない。

## Resident Set

| Team | Initial State |
|---|---|
| Gate entry | resident / metadata_ready / eager process for `startup_profile: provider_cli` roles |
| GTC command | `gtc-scaffold` builder command / `gate-task-creator` lazy fallback |
| TPM | resident / metadata_ready / eager process |
| Directors | resident / idle / eager process |
| Specialist members | resident / idle / lazy activation |

bridge、commit、save、Obsidian CLI、browser などの道具スキルは resident agent にしない。

`status: deprecated` の legacy Team Role は resident agent にしない。互換参照が必要な場合だけ `references/model-registry.md` の `startup_profile: compatibility_only` を使う。

bootstrap hook が作る `metadata_ready` は、Organization Instance と resident roster の登録完了を示す。SessionStart hook はさらに `--launch-agents` で `startup_profile: provider_cli` の role を ITB-owned provider CLI tmux process として ensure し、成功時は `process_status: process_ready` を記録する。queue message は YAML として保存し、tmux には対象 YAML と report path を読む nudge prompt だけを送る。実作業と report YAML 作成は role provider が行う。`startup_profile: lazy_activation` の role は roster には残すが、Director が task scope に応じて ITB activation を依頼するまで process 起動しない。provider へ実際に投げて `session_id`、`effective_model`、request / usage evidence が揃った状態は `response_active` として別に扱う。

Gate / TPM のうち model registry で `status: active`、`resident_target: true`、`startup_profile: provider_cli` の role は SessionStart で eager process として ensure する。`startup_profile: lazy_activation` の active Gate role は roster 登録だけを行い、handoff 時に起動する。`status: reference` / `startup_profile: compatibility_only` の旧 Gate role は runtime resident / queue consumer にしない。Claude 指定の role は activation / Invocation Evidence で Claude provider 証跡を必須にし、Codex/OpenAI evidence で代替してはならない。

agent-dispatch は resident provider の会話 context 肥大を防ぐため、`task_id` が前回 dispatch から変わった場合、または `dispatch_context_turns` が `ITB_AGENT_DISPATCH_CONTEXT_RESET_EVERY` / `context_reset_every`（既定 8）以上になった場合に provider pane を `force_respawn` する。respawn 理由、turn count、last task は roster と Invocation Evidence に記録する。複数の独立 worker / reviewer を同時に動かす場合は `agent-dispatch-batch` を使う。batch item は `independent: true` または `dependency: none` を必ず宣言し、同一 role への同時 dispatch は禁止する。builder は各 item を `wait: false` で fan-out し、provider-written report file の terminal status を barrier として待つ。

SessionEnd / archive hook は、`bootstrap.json` の `tmux_session` が `itb-<organization_instance_id>` と一致する場合だけその tmux session を停止する。unsafe target、tmux unavailable、already stopped の場合は停止せず、`shutdown.json` と Invocation Evidence に結果を残す。

## Resident Shell

起動時は軽量な resident shell を使う。full SKILL.md、Vault policy、references は active 化された agent だけが必要時に読む。

必須初期文脈:

- `agent_id`
- `role_id`
- `organization_instance_id`
- `chat_session_id`
- Flow Contract
- intended model / provider / execution mode from `references/model-registry.md`
- idle rule
- SKILL.md path

## Shared Resource Rule

Vault、Task Index、Kanban は複数 Organization Instance から触られる共有資源である。起票時の排他制御、重複検出、採番整合性は `gtc-scaffold` builder command が `gate-task-creator` 責務境界として担い、継続監視は `infra-task-dispatcher` の責務とする。

## Prompt Entry Rule

ユーザー発話は常に `gate-prompt-formatter` へ渡す。`チームを起動して` という発話も ITB 直通にしない。

GPF が動く時点で Organization Instance / Resident Roster が存在しない場合は、GPF が起動を代行せず `bootstrap_missing` policy violation として停止する。
