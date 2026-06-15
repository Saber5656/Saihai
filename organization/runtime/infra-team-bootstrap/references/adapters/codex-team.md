# Codex Team Adapter

Codex Team adapter は、`infra-team-bootstrap` の共通契約を Codex App / Codex CLI / Codex-native agent 環境で実行するための実装詳細を扱う。

## Responsibilities

| Item | Rule |
|---|---|
| Organization Instance | chat session ごとに session-local roster を作る |
| Agent registration | hook stage では各 resident agent instance を roster に登録する |
| Agent process startup | SessionStart hook で ITB-owned lightweight resident shell を tmux 上の独立 process として ensure する |
| Agent activation | task flow 上の Director 指示後、ITB が必要な process を Claude / Codex provider CLI に昇格し、response evidence を追記する |
| Model mapping | `references/model-registry.md` の `provider` / `primary_model` / `execution_mode` を参照する |
| Bridge | Claude 側実行が必要な role は bridge を一時実行 tool として扱う |
| Evidence | Codex session id、request id、effective model、usage source を記録する |

## Codex Hooks Adapter

Codex は `~/.codex/hooks.json` の `SessionStart` / `UserPromptSubmit` hooks で ITB startup / preflight gate を実行する。
Current Codex runtime may not expose a `SessionEnd` event in `/hooks`; archive-time shutdown therefore uses the explicit `archive-shutdown` CLI as the reliable primary path.

| Event / Entry | Script | Responsibility |
|---|---|---|
| `SessionStart` | `$HOME/dotfiles/codex/hooks/itb-session-start.sh` | `scripts/itb_bootstrap_builder.py --launch-agents` を呼び出し、`~/.codex/state/itb/<session_id>/` に Bootstrap Report / Resident Roster / startup対象の `readiness_scope: process_ready` / role-agent worker tmux process evidence / status を作る。`startup` source は full bootstrap、`resume` / `clear` / `compact` source は fingerprint と tmux liveness が unchanged の場合だけ `session_start_compacted` で metadata 更新に短絡する。resident launch 後は session-local detached `queue-watch-daemon` と `interactive-readiness-followup` を起動し、既存 pid 生存時は二重起動しない |
| `UserPromptSubmit` | `$HOME/dotfiles/codex/hooks/itb-prompt-preflight.sh` | status が `ready` でなくても prompt は止めず、not-ready 状況を advisory な `additionalContext`（`## ITB Preflight Not Ready (advisory, not blocking)`）として注入し、メインエージェントに判断を委ねる（No Task ゲートは注入文の指示で維持し、詰み状態を回避する）。ready なら fast path では `ITB_GATE_ENTRY_DISPATCH=1` を使い、thin GPF response を provider から捕捉して ITB atomic queue writer が `queue/reports/` と inbox status を確定する。fallback では `role-queue` で `gate-prompt-formatter` inbox に投入し、Gate Entry Queue context を注入する |
| optional `Stop` / pre-response guard | `$HOME/dotfiles/codex/hooks/itb-final-response-guard.sh` | `scripts/itb_bootstrap_builder.py final-response-guard` を呼び、active task が `pre_final_response` の場合だけ `finalization-check` と `final-transport-render-check` の機械結果で最終応答を allow/block する。active task が無い通常 turn、または `pre_final_response` 以外の phase では `skipped_*` として allow し、archive shutdown には使わない |
| explicit archive shutdown | `$HOME/skills-repo/skills/archive-shutdown/scripts/archive-shutdown` | `scripts/itb_bootstrap_builder.py archive-shutdown` を呼び出し、state を `archived` にし、`bootstrap.json` の `tmux_session` が `itb-<organization_instance_id>` と一致する場合だけ対象 tmux session を停止する。`$HOME/dotfiles/codex/bin/archive-shutdown` が存在する環境では、この script への薄い wrapper として扱う |
| compatibility `SessionEnd` | `$HOME/dotfiles/codex/hooks/itb-session-end.sh` | runtime が `SessionEnd` を提供する場合のみ、後方互換として `session-end` を呼び出す |

Codex hooks は command handler として動く。Hook はメインエージェント配下の subagent を作らず、`startup_profile: provider_cli` の role を ITB-owned provider CLI tmux process として起動する。queue message は YAML として `queue/inbox/<role>.yaml` と `queue/tasks/<task>/<message>.yaml` に保存する。
Repo 内の hook wrapper 正本は `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/hooks/` にある。`$HOME/dotfiles/codex/hooks/itb-*.sh` はこの bundle の symlink / copy として扱い、`ITB_RUNTIME=codex`、`ITB_STATE_ROOT=$HOME/.codex/state/itb`、必要なら `ITB_BUILDER` を渡す。`codex-hooks.example.json` と `codex-config.example.toml` は settings 登録例であり、wrapper 自体は `~/.codex/hooks.json` を変更しない。
反映作業は builder `hook-install` command を使う。既定は `dry_run` で、`codex_hooks_path`、`codex_config_path`、`hooks_dir` を明示すると dotfiles 配下への変更計画を返す。`apply: true` のときだけ settings JSON、`[features].codex_hooks = true`、hook wrapper copy を実行し、`~/.codex/hooks.json` / `~/.codex/config.toml` が symlink の場合は link を壊さず解決先へ書く。dotfiles copy 先では `itb-hook-common.sh` の相対推定だけでは builder を発見できないため、settings command は `ITB_BUILDER` を必ず渡す。
適用後の検証は builder `hook-health-check` command を使う。通常は settings と copied wrapper を読むだけで、canonical command の `ITB_RUNTIME=codex`、`ITB_STATE_ROOT="$HOME/.codex/state/itb"`、script path、`ITB_BUILDER`、実行権限を確認する。`run_smoke: true` は既定で検証済み Stop / PreToolUse wrapper だけを直接実行する。`smoke_scripts: ["startup_preflight"]` または対応する `smoke_events` を明示した場合は、`smoke_state_root` 必須で SessionStart dry-run と UserPromptSubmit controlled micro-flow を一時 state に閉じ込めて実行する。UserPromptSubmit 単体 smoke は同じ `smoke_state_root` に ready な SessionStart smoke state が無い場合 `session_start_smoke_required` で block する。live state を汚さない smoke では `smoke_state_root` を一時 directory に向ける。現行 Codex session が settings reload 後に自然 hook 発火したかを見るときは `check_live_evidence: true` を付け、settings command の `ITB_STATE_ROOT` にある `last-session` と session JSON / JSONL 証跡を read-only で `live_evidence` に出す。missing は既定で informational のため、検収条件にしたい場合だけ `require_live_evidence: true` と `required_live_events` を指定する。reload / new session 後の検収では古い証跡で pass しないよう `max_live_evidence_age_seconds` も指定する。
`resume` / `clear` / `compact` の unchanged SessionStart は `bootstrap.json` / `bootstrap-report.md` / `status` / `last_event` / `last-session` と `session_start_compacted` evidence だけを更新し、state-dir GC、stale cleanup、resident launch、daemon autostart を skip する。再起動を強制したい場合は hook input に `force_session_start_rebuild: true` を渡すか、`ITB_SESSION_START_COMPACT_UNCHANGED=0` で wrapper を実行する。
`Stop` は Codex では各 turn 終了で発火しうるため resident shutdown には使わない。final response guard として使う場合も、builder `final-response-guard` の `finalResponseGuard.result` が `blocked_finalization_gate` のときだけ応答を止め、`skipped_no_active_pre_final_task` / `skipped_non_pre_final_phase` は通常 turn として通す。

Gate entry fast path では、`gate-prompt-formatter` は薄い YAML envelope だけを返し、report YAML 作成と inbox `done` / `failed` 更新は Python builder の atomic queue writer が行う。これにより role-side file write 失敗や長い report 作成で GPF が滞留することを避ける。`gate-prompt-formatter` が OpenAI / `execution_mode: codex` row の場合は `codex exec --ephemeral --json` の one-shot provider adapter を使い、対話型 Codex resident chat を増やさない。Anthropic row の場合は既存の Claude tmux interactive dispatch を使うが、`claude -p` / `claude --print` を新規 fast path として推奨しない。
Haiku + `acceptEdits`、Sonnet interactive、`codex exec --ephemeral --json`、deterministic `gtc-scaffold` の比較は `gate-latency-report` が `gate-metrics.jsonl` から生成する `Gate Latency Comparison` を正本にする。UserPromptSubmit 起点の GPF + `gtc-scaffold` と GTC LLM baseline の比較は `prompt_submit_comparison` の target verdict / SLA verdict / speedup ratio を読む。サンプル不足の場合は `missing_required_variants` や `prompt_submit_comparison.status` に明示し、推測で速い経路を確定しない。

Resident Claude providers は、明示指定がなければ Haiku 系を `--permission-mode acceptEdits`、それ以外を `--permission-mode auto` とする。provider は既定で provider auth/config home を分けず、通常の Claude / Codex 認証情報と hook input cwd の workspace trust を参照する。ロール分離は既定で hook input の `cwd` 起動、role prompt、tools、permission mode、add-dir、queue evidence で担保する。`ITB_PROVIDER_MEMORY_ISOLATION=1` の場合だけ session-local `provider-state/<agent>/<provider>` launch cwd、最小 `AGENTS.md` / `CLAUDE.md`、`memory-policy.json` を使う。HOME 配下の `~/.codex/auth.json` や Claude auth state はコピー、symlink、更新しない。Claude resident は `--safe-mode` と `--append-system-prompt` を併用し、auth は通常参照のまま CLAUDE.md / hooks / plugins / MCP 混入を抑える。Codex one-shot provider adapter は `codex exec --ephemeral --ignore-user-config --ignore-rules --json` を使い、auth は通常の `CODEX_HOME` を参照しつつ user config / execpolicy rules の混入を避ける。`ITB_PROVIDER_CONFIG_ISOLATION=1` は Claude 診断用 opt-in に限り、Codex auth copy は行わない。Sonnet / Haiku 系は `--effort medium`、Opus 系は `--effort max` で起動する。auto mode が必要な role は `claude-sonnet-4-6` 以上を `primary_model` とし、定型 Gate role は `acceptEdits` 前提で `claude-haiku-4-5` primary を許可する。fast mode は実仕様で Opus 4.6/4.7/4.8 限定のため組織方針（2026-06-11）で全 Claude resident 無効化し、`CLAUDE_CODE_DISABLE_FAST_MODE=1` を渡したうえで `ITB_CLAUDE_FAST_MODE_EFFECTIVE=disabled` を旧 pane 置換と証跡用署名として扱う。Resident Codex providers は既定で無効化し、明示診断時だけ `--model gpt-5.5`、`--ask-for-approval never`、`--sandbox workspace-write`、`-c model_reasoning_effort="xhigh"`、`-c service_tier="fast"`、hook input の `cwd` と `--add-dir` roots で起動する。provider-state cwd が必要な検証では `ITB_PROVIDER_MEMORY_ISOLATION=1` を併用する。検証や安全確認で手動承認に戻す場合は `ITB_PROVIDER_PERMISSION_MODE=default` / `plan` / `auto` または `ITB_CODEX_APPROVAL_POLICY=on-request` を指定する。

Claude workspace trust / onboarding は auth state copy ではなく global `~/.claude.json` を正本にする。builder は Claude resident launch 前に whitelist 済みの `launch_cwd` / `workspace_cwd` / `add_dir` だけへ `projects.<path>.hasTrustDialogAccepted=true` と `hasCompletedOnboarding=true` を backup + atomic write で seed する。`ITB_PROVIDER_CONFIG_ISOLATION=1` 時も trust / onboarding は global state に統一し、isolated config には theme settings だけを seed する。Quick safety check の検出は current visible tail のみを対象にし、tmux history の古い trust prompt は current blocker としない。

`agent-dispatch`（coordinator → resident agent の同期 dispatch）は Claude / Codex 双方の provider に対応する（2026-06-11）。`role-queue` 同様 tmux pane への paste + Enter を基盤にし、provider 差は本質的に影響しない。prompt readiness 判定は Claude composer の `❯`(U+276F) と Codex composer の `›`(U+203A) を両対応で検出し、応答は `[ITB_AGENT_RESPONSE_DONE id=...]` マーカーで捕捉する。paste 前は Escape + `C-u` で overlay / composer 残留を払う。送信後 ACK が取れない場合は追加 Enter を一度送り、composer に request marker が残っている場合だけ clear + re-paste を試す。最終 ACK 不成立は `provider_send_unconfirmed` / `nudge_send_unconfirmed` evidence として閉じる。完了判定と response は `reports/agent-dispatch/...yaml` を再読込して schema / integrity を確認した結果を正本にし、返却 summary には `completion_source: dispatch_report_file` と `dispatch_report_integrity` を載せる。usage_source は Claude=`claude_tmux_interactive` / Codex resident=`codex_tmux_interactive`、Codex one-shot=`codex_exec_json` として証跡化する。`role-queue` nudge は `queue_consumer: true` の role にのみ届く点は従来どおり（これは provider 差ではなく role 設定）。

GPF report finalize 後は、既定で builder が `gtc-scaffold` command を直接実行し、成功した `gtcScaffold` artifact の `task_id` / `task_detail_path` を使って `teams-project-manager` へ `command_completion_chain_handoff` を自動 enqueue する。これにより `UserPromptSubmit -> GPF -> gtc-scaffold -> TPM` の入口を main agent 判断や GTC provider 起動に依存させない。smoke / debug prompt で Vault 起票を避けたい場合だけ、hook input の `skip_gate_entry_auto_gtc: true` または `ITB_GATE_ENTRY_AUTO_GTC=0` を明示する。

Fallback queue mode では、tmux へ対象 YAML と report path を読む nudge prompt を送り、role provider が report を書く。`queue-watch` command は pending inbox を 1 role だけ 1 回監視し、`queue-watch-all` は queue consumer role を横断して同じ watcher primitive を 1 回実行する。`queue-watch-daemon` は SessionStart 後に session-local detached subprocess として起動し、`queue/reports/` と既存 report subdir を `kqueue` / `EVFILT_VNODE` で監視して report 書き込み時に次 cycle を即時実行する。`kqueue` 不可時は `poll_interval_seconds` sleep fallback に戻る。`interactive-readiness-followup` は SessionStart 後に detached subprocess として起動し、hook 同期 spot-check 対象外の process-ready provider CLI resident を `wait_for_interactive_prompt` で確認して `bootstrap.json` / `roster.json` に lock 付きで書き戻す。retry_count / last_nudged_at / metrics を更新して nudge を送り、max retry 到達時は dead-letter report へ閉じる。failed/dead-letter の再実行は自動 replay せず、`queue-replay-failed` の明示操作だけで pending に戻す。古い prompt や superseded queue item を provider に再送しない場合は `queue-close-message` で `role_id` と `message_id` を明示し、`queue_manual_close` report / event / `manual_close` metric を残す。この close は provider completion evidence ではなく、roster unavailable / failed 更新も行わない。`role-queue` は `completion_wait_seconds` opt-in 時だけ terminal report file を bounded wait し、見つけたら builder が recovery / metric / auto handoff を確定する。Python builder は queue 書き込み、watch nudge、validation を担当し、provider evidence なしに done report を作らない。`startup_profile: lazy_activation` の role は roster 登録のみで、Director からの activation 要求まで起動しない。

## Entry Symmetry

Claude / Codex のどちらから chat session が始まっても、共通の lifecycle は変えない。

```text
SessionStart
-> adapter hook
-> infra-team-bootstrap readiness state
-> Resident Roster metadata_ready / idle
-> independent provider CLI tmux processes for startup_profile=provider_cli agents
-> PromptSubmit
-> UserPromptSubmit preflight
-> fast path: gate-prompt-formatter thin YAML response
-> ITB atomic queue writer finalizes GPF report/inbox
-> gtc-scaffold command creates Task Detail / active-task
-> teams-project-manager receives command_completion_chain_handoff
```

Codex は旧単一メイン実行主体ではない。Codex UI / CLI / bridge は transport または adapter であり、workflow output は Flow Contract に従って次 role へ渡す。
`ITB_GATE_ENTRY_DISPATCH=1` は Gate entry fast path として使う。fallback が必要な場合だけ queue-only 正規化へ戻す。

## Model Registry Requirements

| Check | Expected |
|---|---|
| `resident_target` | `true` の role を Resident Roster に登録する |
| `startup_profile: provider_cli` | SessionStart で provider CLI tmux process を起動する |
| `startup_profile: lazy_activation` | Roster 登録のみ行い、Director からの activation 要求まで process 起動しない |
| `provider` | `openai` の role は Codex-native 実行候補にする |
| `execution_mode` | `codex` の role は bootstrap では provider metadata のみ記録し、activation 時に Codex CLI session へ昇格する |
| `provider: anthropic` | bootstrap では provider metadata のみ記録し、activation 時に Claude CLI session へ昇格する。Codex/OpenAI evidence で代替しない |
| `primary_model` | Resident Roster の `intended_model` として記録する |
| `effective_model` | Codex session log から取得し、registry 値で代用しない |
| `startup_profile: compatibility_only` | resident 起動しない |

## Spawn Prompt Requirements

各 resident agent には次だけを初期注入する。

- agent identity
- organization instance identity
- Flow Contract
- metadata_ready / idle / response_active rule
- SKILL.md path
- intended model / provider / execution mode from model registry
- evidence logging requirement

Full SKILL.md / references / project docs は active 化時に読む。

## Shutdown

archive / close 時は、現在の Codex では明示 CLI を使う。

```bash
archive-shutdown --current
archive-shutdown --session-id <session_id>
archive-shutdown --session-id <session_id> --dry-run
```

リモコン入力では `archive-shutdown` が長いため、`archive-shutdown` skill の
exact alias trigger として `/as` を使える。スキル名、コマンド本体、README、
eval の正本は `archive-shutdown` のまま維持する。

```bash
/as
/as --dry-run
/as --session-id <session_id>
```

`/as` の trigger は exact alias 用に限定する。通常の英単語 `as` や
`as a ...` のような文章では `archive-shutdown` を起動しない。

実行時は以下を行う。

1. active agent に handoff summary を要求する。
2. `bootstrap.json` の `organization_instance_id` と `tmux_session` を照合する。
3. `tmux_session == itb-<organization_instance_id>` の場合だけ `tmux kill-session -t <tmux_session>` を実行する。
4. session-local detached `queue-watch-daemon` / `interactive-readiness-followup` は pid file と command line が ITB builder daemon と一致する場合だけ停止する。
5. roster / state を `archived` に更新する。
6. `shutdown.json`、session_id、last_seen_at、未完了 task、復旧メモ、tmux shutdown result、detached process shutdown result を残す。

`--dry-run` は tmux kill、detached process shutdown、state 更新を行わず、対象判定結果だけを返す。

`Stop` hook は各 turn 終了で発火しうるため、Codex では resident shutdown に使わない。session/archive を表す lifecycle hook が runtime から渡される場合だけ `itb-session-end.sh` を互換入口として使う。
