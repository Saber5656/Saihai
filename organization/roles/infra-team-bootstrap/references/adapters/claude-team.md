# Claude Team Adapter

Claude Team adapter は、`infra-team-bootstrap` の共通契約を Claude Code / tmux / TeamCreate 環境で実行するための実装詳細を扱う。

## Responsibilities

| Item | Rule |
|---|---|
| TeamCreate | Organization Instance の Claude team metadata を作成する |
| Agent registration | hook stage では各 resident agent instance を roster に登録する |
| Agent process startup | SessionStart hook で ITB-owned lightweight resident shell を tmux 上の独立 process として ensure する |
| Agent activation | task flow 上の Director 指示後、ITB が必要な process を Claude / Codex provider CLI に昇格し、response evidence を追記する |
| Model mapping | `references/model-registry.md` の `provider` / `primary_model` を Claude family alias に解決する |
| Layout | tmux / pane / TeammateIdle layout は表示上の adapter detail として扱う |
| Evidence | transcript sessionId、requestId、effective model、usage source を記録する |

## Existing Hook Compatibility

既存の `TeammateIdle` hook と `team-layout-*` scripts はレイアウト用途であり、Organization Instance の論理 bootstrap trigger ではない。

`SessionStart` hook が無い runtime では adapter-driven bootstrap が動かないため、最初の GPF 前提チェックで roster 不在を `bootstrap_missing` として扱い、ITB skill を手動 recovery モードで実行する。

## ITB Hook Integration

Claude Code の ITB integration は次の hook scripts を使う。

| Hook Event | Script | Responsibility |
|---|---|---|
| `SessionStart` | `$HOME/.claude/hooks/itb-session-start.sh` | ITB 共通 builder (`scripts/itb_bootstrap_builder.py --launch-agents`) を呼び出して Resident Roster / Bootstrap Report / startup対象の `readiness_scope: process_ready` / role-agent worker tmux process evidence を生成し、成功時に `status=ready` を書く。`startup` source は full bootstrap、`resume` / `clear` / `compact` source は fingerprint と tmux liveness が unchanged の場合だけ `session_start_compacted` で metadata 更新に短絡する。resident launch 後は session-local detached `queue-watch-daemon` と `interactive-readiness-followup` を起動し、既存 pid 生存時は二重起動しない |
| `UserPromptSubmit` | `$HOME/.claude/hooks/itb-prompt-preflight.sh` | status!=ready の場合もプロンプトは block せず、not-ready 状況を advisory な `additionalContext`（`## ITB Preflight Not Ready (advisory, not blocking)`）として注入し、メインエージェントに判断を委ねる（No Task ゲートは注入文の指示で維持し、詰み状態を回避する）。ready なら user prompt を ITB builder `role-queue` command で `gate-prompt-formatter` inbox に投入し、Gate Entry Queue context を注入する |
| optional `Stop` / `SubagentStop` final guard | `$HOME/.claude/hooks/itb-final-response-guard.sh` | `scripts/itb_bootstrap_builder.py final-response-guard` を呼び、active task が `pre_final_response` の場合だけ `finalization-check` と `final-transport-render-check` の機械結果で最終応答を allow/block する。active task が無い通常 turn、または `pre_final_response` 以外の phase では `skipped_*` として allow する |
| `SessionEnd` | `$HOME/.claude/hooks/itb-session-end.sh` | hook state を `archived` にし、`bootstrap.json` の safe `tmux_session` だけを停止して終了理由と shutdown evidence を記録する |
| Roster builder | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/scripts/itb_bootstrap_builder.py` | `model-registry.md` を解析し、`resident_target=true` の active role を roster JSON に登録する。`compatibility_only` / `deprecated` は除外 |
| manual utility | `$HOME/.claude/hooks/itb-state.sh` | 例外復旧で `mark-ready <session_id>` を手動記録する |

Hook scripts は `additionalContext` 注入、Roster ファイル生成、`status=ready` 書き込み、startup対象の tmux process ensure、session-local `queue-watch-daemon` / `interactive-readiness-followup` autostart を担う。Claude Code の Agent tool 配下に subagent として吊るすのではなく、`startup_profile: provider_cli` の role を ITB-owned provider CLI tmux process として起動する。queue message は YAML として `queue/inbox/<role>.yaml` と `queue/tasks/<task>/<message>.yaml` に保存し、tmux へは対象 YAML と report path を読む nudge prompt だけを送る。実作業と report YAML 作成は role provider が行い、Python builder は queue 書き込み、nudge、validation、terminal report file recovery、optional bounded completion wait だけを担当する。daemon は `queue/reports/` を `kqueue` / `EVFILT_VNODE` で監視し、不可時は `poll_interval_seconds` sleep fallback に戻る。interactive follow-up は hook 同期 budget 外で gate-entry 以外の process-ready provider CLI resident を `wait_for_interactive_prompt` で確認し、`bootstrap.json` / `roster.json` へ lock 付きで書き戻す。failed/dead-letter の再実行は `queue-replay-failed` の明示操作だけで pending に戻し、古い prompt や superseded queue item を provider に再送しない場合は `queue-close-message` で `role_id` と `message_id` を明示して `queue_manual_close` 証跡に閉じる。この close は provider completion evidence ではなく、roster unavailable / failed 更新も行わない。`startup_profile: lazy_activation` の role は Director からの activation 要求まで process 起動しない。
Repo 内の hook wrapper 正本は `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/hooks/` にある。`$HOME/.claude/hooks/itb-*.sh` はこの bundle の symlink / copy として扱い、`ITB_RUNTIME=claude`、`ITB_STATE_ROOT=$HOME/.claude/state/itb`、必要なら `ITB_BUILDER` を渡す。`claude-settings-hooks.example.json` は settings 登録例であり、wrapper 自体は Claude settings を変更しない。
反映作業は builder `hook-install` command を使う。既定は `dry_run` で、`claude_settings_path` と `hooks_dir` を明示すると dotfiles 配下への変更計画を返す。`apply: true` のときだけ settings JSON と hook wrapper copy を実行し、`~/.claude/settings.json` が symlink の場合は link を壊さず解決先へ書く。既存の `TeammateIdle` など ITB 以外の hook event は保持する。dotfiles copy 先では `itb-hook-common.sh` の相対推定だけでは builder を発見できないため、settings command は `ITB_BUILDER` を必ず渡す。
適用後の検証は builder `hook-health-check` command を使う。通常は settings と copied wrapper を読むだけで、canonical command の `ITB_RUNTIME=claude`、`ITB_STATE_ROOT="$HOME/.claude/state/itb"`、script path、`ITB_BUILDER`、実行権限を確認する。`run_smoke: true` は既定で検証済み Stop / PreToolUse wrapper だけを直接実行する。`smoke_scripts: ["startup_preflight"]` または対応する `smoke_events` を明示した場合は、`smoke_state_root` 必須で SessionStart dry-run と UserPromptSubmit controlled micro-flow を一時 state に閉じ込めて実行する。UserPromptSubmit 単体 smoke は同じ `smoke_state_root` に ready な SessionStart smoke state が無い場合 `session_start_smoke_required` で block する。live state を汚さない smoke では `smoke_state_root` を一時 directory に向ける。現行 Claude session が settings reload 後に自然 hook 発火したかを見るときは `check_live_evidence: true` を付け、settings command の `ITB_STATE_ROOT` にある `last-session` と session JSON / JSONL 証跡を read-only で `live_evidence` に出す。missing は既定で informational のため、検収条件にしたい場合だけ `require_live_evidence: true` と `required_live_events` を指定する。reload / new session 後の検収では古い証跡で pass しないよう `max_live_evidence_age_seconds` も指定する。
`resume` / `clear` / `compact` の unchanged SessionStart は `bootstrap.json` / `bootstrap-report.md` / `status` / `last_event` / `last-session` と `session_start_compacted` evidence だけを更新し、state-dir GC、stale cleanup、resident launch、daemon autostart を skip する。再起動を強制したい場合は hook input に `force_session_start_rebuild: true` を渡すか、`ITB_SESSION_START_COMPACT_UNCHANGED=0` で wrapper を実行する。
Final response guard は builder `final-response-guard` の `permissionDecision`、`decision`、`notification_class`、`finalResponseGuard.result` だけを読む。`blocked_finalization_gate` は最終応答を止め、`skipped_no_active_pre_final_task` / `skipped_non_pre_final_phase` は通常 turn として通す。hook 側で Task Detail や free-form reason を再解釈しない。

`UserPromptSubmit` の user prompt queue 正規化は Codex adapter と同じ `role-queue` schema を使う。
Claude 固有の TeamCreate / tmux layout は表示・起動 detail であり、Gate 入口 I/O の正本にはしない。

`TeammateIdle` は引き続き tmux layout のみを扱い、bootstrap trigger として扱わない。

## Provider Launch Profile

Claude resident providers は、明示指定がなければ Haiku 系を `--permission-mode acceptEdits`、それ以外を `--permission-mode auto` で起動する。auto mode が必要な role は `claude-sonnet-4-6` 以上を `primary_model` とし、定型 Gate role は `acceptEdits` 前提で `claude-haiku-4-5` primary を許可する。Sonnet / Haiku 系は `--effort medium`、Opus 系は `--effort max` で起動する。fast mode は実仕様で Opus 4.6/4.7/4.8 限定のため、組織方針（2026-06-11）として全 Claude resident で無効化する。全 Claude provider に `CLAUDE_CODE_DISABLE_FAST_MODE=1` を渡して CLI の fast を強制 off にし、`ITB_CLAUDE_FAST_MODE_EFFECTIVE=disabled` を旧 pane 置換（再起動判定）と証跡用の署名として扱う。resident provider は既定で `CLAUDE_CONFIG_DIR` を分離せず、通常の Claude auth / Keychain と hook input cwd の workspace trust を参照する。ロール分離は既定で hook input の `cwd` 起動、`--safe-mode`、`--append-system-prompt`、`--tools`、`--permission-mode`、`--add-dir`、queue evidence で担保する。`ITB_PROVIDER_MEMORY_ISOLATION=1` の場合だけ session-local `provider-state/<agent>/<provider>` launch cwd、最小 `AGENTS.md` / `CLAUDE.md`、`memory-policy.json` を使う。`ITB_PROVIDER_CONFIG_ISOLATION=1` は診断用 opt-in であり、その場合だけ `CLAUDE_CONFIG_DIR` と `CLAUDE_SECURESTORAGE_CONFIG_DIR=""` を併用し、isolated config の onboarding/trust/theme state を seed する。HOME 配下の auth file はコピー、symlink、更新しない。

Workspace trust / onboarding は isolated config ではなく global `~/.claude.json` を正本にする。builder は Claude resident launch 前に whitelist 済みの `launch_cwd` / `workspace_cwd` / `add_dir` だけへ `projects.<path>.hasTrustDialogAccepted=true` と `hasCompletedOnboarding=true` を backup + atomic write で seed し、isolated config opt-in 時も theme settings 以外は global state に統一する。Quick safety check の検出は current visible tail のみを対象にし、tmux history に残った古い trust prompt は current blocker としない。
Haiku + `acceptEdits` 回帰、Sonnet interactive 維持、OpenAI one-shot 代替、deterministic `gtc-scaffold` の比較は、`gate-latency-report` が `gate-metrics.jsonl` から生成する `Gate Latency Comparison` を正本にする。UserPromptSubmit 起点の GPF + `gtc-scaffold` と GTC LLM baseline の比較は `prompt_submit_comparison` の target verdict / SLA verdict / speedup ratio を読む。Claude print / `claude -p` は新規 fast path として推奨しない。

Codex resident providers は、Claude adapter 経由で起動される場合も `gpt-5.5`、`--ask-for-approval never`、`--sandbox workspace-write`、`model_reasoning_effort="xhigh"`、`service_tier="fast"` を既定にする。これらの起動署名が欠ける既存 pane は ready とみなさず、次の bootstrap / dispatch で respawn する。

## Spawn Prompt Requirements

各 resident agent には次だけを初期注入する。

- agent identity
- organization instance identity
- Flow Contract
- metadata_ready / idle / response_active rule
- SKILL.md path
- evidence logging requirement

Full SKILL.md / references / project docs は active 化時に読む。

## Model Registry Requirements

Claude Team adapter は、起動前に `references/model-registry.md` を読む。

| Check | Expected |
|---|---|
| `resident_target` | `true` の role を Resident Roster に登録する |
| `startup_profile: provider_cli` | SessionStart で provider CLI tmux process を起動する |
| `startup_profile: lazy_activation` | Roster 登録のみ行い、Director からの activation 要求まで process 起動しない |
| `provider` | bootstrap では provider 種別を roster に記録するだけで provider CLI を起動しない。activation 時に `anthropic` は `claude` CLI、`openai` / `execution_mode: codex` は `codex` CLI に昇格する |
| `provider` mismatch | 未対応 provider は fallback せず `launch_failed` として evidence に残す |
| `primary_model` | Resident Roster の `intended_model` として記録する |
| `effective_model` | Claude transcript JSONL から取得し、registry 値で代用しない |
| `startup_profile: compatibility_only` | resident 登録しない |

## Shutdown

archive / close 時は以下を実行する。

1. active agent に handoff summary を要求する。
2. `organization_instance_id` と `tmux_session` を照合し、`tmux_session == itb-<organization_instance_id>` の場合だけ resident tmux session を停止する。
3. session-local detached `queue-watch-daemon` / `interactive-readiness-followup` は pid file と command line が ITB builder daemon と一致する場合だけ停止する。
4. roster / state を `archived` に更新する。
5. `shutdown.json`、session_id、last_seen_at、未完了 task、復旧メモ、tmux shutdown result、detached process shutdown result を残す。
