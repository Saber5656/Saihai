---
name: infra-team-bootstrap
description: 作業セッション開始、再開、アーカイブ時に Resident Organization Roster を作成・検証・終了する Infrastructure ロール。新しいチャット/作業セッションが作成された直後、最初のユーザープロンプトを GPF に渡す前に resident agent の独立 tmux process readiness を確認する。ユーザーの「チームを起動して」という自然文から直通起動せず、その発話は通常どおり gate-prompt-formatter に渡す。
user-invocable: false
allowed-tools: Read, Grep, Glob, Bash, Write, Edit, Agent
category: Team Role
created: 2026-05-20
updated: 2026-05-24
status: active
purpose: チャットセッション単位の Organization Instance を bootstrap / resume / shutdown する
team: infra
agent_id: infra-team-bootstrap
---

# Infra Team Bootstrap

## 役割

`infra-team-bootstrap` は、チャットセッション単位の Organization Instance を起動、再開、終了する Infrastructure ロール。

ユーザー依頼を解釈したり、Task Detail を作成したり、実作業を開始したりしない。ITB の出力は、`gate-prompt-formatter` が通常入口として動ける状態を作るための `Bootstrap Report`、Resident Roster 証跡、ready 後の Gate Entry Queue 投入証跡である。

## Adapter-driven Bootstrap

Claude / Codex の各 adapter は、`SessionStart` / `UserPromptSubmit` 相当の lifecycle hook から共通 builder (`scripts/itb_bootstrap_builder.py`) を呼び出し、Resident Roster と Bootstrap Report の readiness state を作る。

SessionStart hook は共通 builder に `--launch-agents` を渡し、`model-registry.md` で `startup_profile: provider_cli` の role を ITB 管理の provider CLI tmux process として ensure する。queue message は YAML として保存され、tmux には inbox / task payload / report path を読むための nudge だけを送る。実作業と report 作成は role provider が行い、Python builder は queue 書き込み、nudge、validation の制御面だけを担う。
Claude resident provider は、明示指定がなければ Haiku 系を `--permission-mode acceptEdits`、それ以外を `--permission-mode auto` とし、workspace / Vault roots / queue root は `--add-dir` で明示許可する。ロール分離は provider 認証の複製ではなく、旧仕様どおり hook input の `cwd` から起動し、`--safe-mode`、`--append-system-prompt`、`--tools`、`--permission-mode`、`--add-dir`、queue evidence で担保する。既定では SessionStart / adapter が渡した workspace cwd から起動し、global Claude workspace trust/auth を再利用して session ごとの `Quick safety check` 停止を避ける。
`ITB_PROVIDER_MEMORY_ISOLATION=1` は診断 / 特殊用途の opt-in であり、その場合だけ session-local `provider-state/<agent>/<provider>` を launch cwd にして `memory-policy.json`、最小 `AGENTS.md`、最小 `CLAUDE.md` を書き、main agent 用 project/global memory の自動探索を避ける。provider auth/config は既定で分離しない。`CLAUDE_CONFIG_DIR` / `CODEX_HOME` は default では export せず、`~/.claude` / `~/.codex` と Keychain 等の共通認証を参照するだけで、HOME 配下の auth file をコピー・symlink・更新しない。Claude resident は `--safe-mode` と `--append-system-prompt` を併用し、auth は通常参照のまま CLAUDE.md / hooks / plugins / MCP などの customizations 混入を抑える。Codex one-shot provider adapter は `codex exec --ephemeral --ignore-user-config --ignore-rules --json` を使い、auth は通常の `CODEX_HOME` を参照しつつ user config / execpolicy rules の混入を避ける。`ITB_PROVIDER_CONFIG_ISOLATION=1` は Claude の診断用 opt-in であり、その場合だけ `CLAUDE_CONFIG_DIR` と `CLAUDE_SECURESTORAGE_CONFIG_DIR=""` を併用し、isolated config の onboarding/trust/theme seed 結果を `memory-policy.json` に記録する。Codex auth copy は行わない。
Sonnet / Haiku 系は `--effort medium`、Opus 系は `--effort max` で起動する。fast mode は Claude Code 実仕様上 Opus 4.6/4.7/4.8 限定機能であり、組織方針（2026-06-11）として全 Claude resident で無効化する。全 Claude provider に `CLAUDE_CODE_DISABLE_FAST_MODE=1` を渡して CLI 側の fast を強制 off にし、`ITB_CLAUDE_FAST_MODE_EFFECTIVE=disabled` を再起動判定と証跡用の署名として残す。定型 Gate role は `acceptEdits` 前提で Haiku primary を許可し、auto mode が必要な role だけ Sonnet 以上を primary とする。Codex resident provider は interactive resident を既定で無効にし、明示診断時のみ `--model gpt-5.5`、`--ask-for-approval never`、`--sandbox workspace-write`、`-c model_reasoning_effort="xhigh"`、`-c service_tier="fast"`、hook input の `cwd` と `--add-dir` roots で起動する。provider-state cwd が必要な場合は `ITB_PROVIDER_MEMORY_ISOLATION=1` を併用する。必要な場合は `ITB_PROVIDER_PERMISSION_MODE=default` / `plan` / `auto`、`ITB_CODEX_APPROVAL_POLICY=on-request`、`ITB_PROVIDER_MEMORY_ISOLATION=1`、`ITB_PROVIDER_CONFIG_ISOLATION=1`、`ITB_PROVIDER_AUTH_SHARE=0`、`ITB_CLAUDE_HAIKU_SONNET_EFFORT`、`ITB_CLAUDE_OPUS_EFFORT`、`ITB_CODEX_MODEL`、`ITB_CODEX_REASONING_EFFORT`、`ITB_CODEX_SERVICE_TIER`、または hook input の `permission_mode` で上書きする。`startup_profile: lazy_activation` の role は roster 登録だけ行い、Director からの activation 要求まで process 起動しない。

`resume` / `clear` / `compact` 由来の SessionStart では、builder が `session_start_guard_fingerprint`（runtime、cwd、launch flags、registry / policy digest、resident agent set、startup agent set）と前回 `bootstrap.json` の ready 状態、前回 `roster.json` の tmux liveness を比較する。`ITB_SESSION_START_COMPACT_UNCHANGED` が有効で fingerprint と liveness が変わらない場合は `session_start_compacted: true` として `bootstrap.json` / report / status / last_event だけを更新し、state-dir GC、stale cleanup、resident process launch、daemon / follow-up autostart を skip する。通常の `startup` source は常に full bootstrap に通す。強制再構築は hook input の `force_session_start_rebuild` / `forceSessionStartRebuild`、または `ITB_SESSION_START_COMPACT_UNCHANGED=0` で行う。full bootstrap で resident process を ensure する場合も、tmux `history-limit` / `default-size` 設定は session ごとに一度だけ行い、既存 pane の liveness は `list-panes -a` の session cache を優先して読む。個別 `tmux_pane_info` は cache miss / tmux error 時の fallback に限定する。

`context-surface-report` command は resident provider を起動せず、registry の role `context_dirs` / effective `--add-dir`、session queue root、`preflight-events.jsonl` をもとに context surface を上限付きで estimate する。ディレクトリ走査は file stat のみで、`max_files` / `max_bytes` / `max_depth` に達したら truncate し、sampled bytes、rough token estimate、preflight compaction hit rate を `context-surface-report.json` と `context-surface-events.jsonl` に記録する。これは FIX-17 の tuning evidence であり、provider transcript の live token usage 証跡の代替ではない。

Claude workspace trust / onboarding は auth file copy ではなく global `~/.claude.json` の user-global state として扱う。builder は Claude resident launch 前に whitelist 済みの `launch_cwd` / `workspace_cwd` / `add_dir` だけへ `projects.<path>.hasTrustDialogAccepted=true` と `hasCompletedOnboarding=true` を backup + atomic write で seed し、結果を `memory-policy.json` に記録する。`ITB_PROVIDER_CONFIG_ISOLATION=1` の場合も trust / onboarding は global `~/.claude.json` に統一し、isolated config には theme settings のみを seed する。`wait_for_interactive_prompt` は current visible tail に残る Quick safety check だけを blocker とし、過去ログの trust prompt を current blocker とみなさない。

role provider 起動の判断と実行は ITB の責務であり、個別 agent に lifecycle 知識を持たせない。`role-agent-worker` は provider evidence を記録する補助コマンドであり、provider evidence なしに pending message を claim したり done report を生成したりしてはならない。
Queue consumer の `allowed_tools` は `role-agent-registry.yaml` と各 role `SKILL.md` frontmatter の `allowed-tools` が一致していることを builder が検証する。queue finalizer / report writer は registry を正本にし、provider turn 内の任意 report file 確定に戻さない。`role-report` finalizer に必要な Bash は provider 起動時の transport finalizer tool としてだけ追加され、role の作業 allowed-tools や git 操作許可とは別に扱う。`agent-dispatch` も dispatch manifest に `agent-dispatch-report` finalizer contract、required fields、provider evidence fields、allowed results を持たせ、provider は完了/blocked 時に `atomic_report_writer` へ 1 JSON object を stdin で渡して report file を先に確定する。pane marker は fallback signal に限定する。`agent-dispatch-batch` は director が各 item で `independent: true` または `dependency: none` を明示した場合だけ使える fan-out primitive とし、同一 role への同時 dispatch は 1 pane 競合として block する。batch barrier は各 dispatch を `wait: false` で送信した後、provider-written report file の terminal status をまとめて待ち、roster / metrics / invocation evidence を更新する。
`role-agent-registry.yaml` / `completion-chain.yaml` / queue report は通常の YAML として読み込む。PyYAML が利用可能な環境では `safe_load`、未導入環境では ITB 設定 subset の厳格 loader を使い、JSON-compatible 前提には戻さない。
Completion chain は `completion-chain.yaml` を正本にする。現行は `role-report` または queue report recovery を起点に `auto_queue_handoffs` を評価し、GPF→`gtc-scaffold` command→TPM、TPM→GTE を builder / daemon が self-advancing hop として投入する。`gate-task-creator` provider は SessionStart で起動せず、legacy / recovery fallback のみ lazy activation する。TPM→GTE は `team-completion-check` command が `status: pass` かつ `next_phase_allowed: true` の場合だけ進める。GTE report が `result: quality_ok` かつ `handoff_to: git-publisher` の場合は、publication result ではなく manifest-only gate で `Git Publication Manifest` と commit required 時の `Task Change Manifest` を検査し、pass した場合だけ `git-publisher` inbox へ queue handoff する。GTE→`vault_final_update` は GTE report が `result: quality_ok` かつ `handoff_to: vault_final_update` の場合だけ queue ではなく command handoff として `vault-final-update` を実行する。git-publisher→`vault_final_update` は git-publisher の done report が `next_role` / `handoff_to: vault_final_update` を示し、Task Detail の `Git Publication Result` と linked report artifact を `git_publication_gate_errors` で検査して pass した場合だけ command handoff として `vault-final-update` を実行する。旧 `gate-task-assessor` は互換参照に限る。
`team-completion-check`、`evaluator-precheck`、`finalization-check` の command artifact は `status`、`missing_evidence`、`blockers`、`next_phase_allowed`、`next_action`、`llm_dispatch_policy` を必ず持つ。`block` / `ambiguous` 時は次 LLM を dispatch せず、`next_action` に示された前段修復へ戻す。`pass` 時も assessor / guardian runtime は起動せず、command artifact の `llm_dispatch_policy` に従って evaluator thin verdict または final renderer だけへ進める。`vault-final-update` command は compact gate artifacts を `vault_final_update.json` に集約し、Task Detail の `Vault Final Update` section を一度だけ idempotent に置換する。`task-detail-append` は `Git Publication Result` thin section の `Report Path` / `Report SHA256` から publication result report を検査でき、`auto_vault_final_update: true` の場合だけ publication gate pass 後に `vault-final-update` を実行する。git-publisher の done report / recovered report からの auto handoff も同じ publication gate を通すため、Task Detail に valid な `Git Publication Result` が無い場合は block する。`final-transport-render-check` command は `finalization-check` pass artifact を source として `Final Transport Render Check` section / artifact を生成し、`auto_final_transport_render_check: true` 指定時だけ finalization-check から自動実行する。`final-response-guard` command は hook wrapper として active `pre_final_response` task だけを検査し、通常 turn では `skipped_no_active_pre_final_task` / `skipped_non_pre_final_phase` で通す。人間向け rollup は command の出力に限定し、各 gate role は長い Markdown 表を再生成しない。
`tests/fixtures/gate_command_ambiguity_cases.json` は `team-completion-check` / `finalization-check` の既知 pass/block ケースが `ambiguous` に落ちないことを固定する command benchmark である。benchmark が失敗した場合は command schema または required evidence を修復し、LLM に状況推論を戻してはならない。
同じ `completion-chain.yaml` の `gate_sla` を queue pending の機械閾値として使う。`queue-watch` / `queue-watch-all` / `queue-watch-daemon` は pending age が role / hop / default SLA を超えた場合に `queue_sla_breach` event、`sla_breach` metric、`notification_class: flow_alert` を記録する。通知判断は prose ではなく `notification_class`（`silent` / `flow_alert` / `approval_wait` / `done`）を正本にする。OS 通知は `notification-dispatch` command が担当し、`silent` は送信しない。既定の送信対象 class は `flow_alert,approval_wait` で、`done` 通知は `notification_classes` / `ITB_OS_NOTIFICATION_CLASSES` または `force_notification` で明示した場合だけ扱う。command は `dry_run` または `--dry-run` で送信せず `notification-events.jsonl` だけを記録し、実際に `osascript` を呼ぶのは `enable_os_notification` / `send_os_notification` または `ITB_OS_NOTIFICATIONS=1` がある場合に限る。
Queue watcher は単一 role の `queue-watch`、全 queue consumer 横断の一回 sweep `queue-watch-all`、周期 sweep 用の `queue-watch-daemon` を持つ。hook からは軽量性を保つため一回 sweep を基本とし、SessionStart の resident launch 後は session-local detached `queue-watch-daemon` を起動する。daemon は既定で `queue/reports/` と既存 report subdir を `kqueue` / `EVFILT_VNODE` で監視し、report 書き込み event があれば次 cycle を即時実行する。`kqueue` 不可、watch dir 欠落、または `event_driven=false` の場合は `poll_interval_seconds` の sleep fallback に戻し、各 cycle の `event_wait` 証跡を残す。max retry 到達時は dead-letter report へ閉じ、failed / dead-letter の再実行は `queue-replay-failed` の明示操作だけで pending に戻す。古い依頼や superseded prompt を provider に再送せず閉じる場合は `queue-close-message` で `role_id` と `message_id` を明示し、`queue_manual_close` report / event / `manual_close` metric を残す。この close は provider 実行証跡ではなく、roster failure としても扱わない。
SessionStart の同期 interactive readiness は hook timeout を守るため gate-entry resident の spot-check に限定する。resident launch 後は session-local detached `interactive-readiness-followup` を起動し、gate-entry 以外の process-ready provider CLI resident を `wait_for_interactive_prompt` で確認して `roster.json` / `bootstrap.json` / `bootstrap-report.md` / `invocation-evidence.jsonl` に lock 付きで書き戻す。follow-up は `interactive_readiness_target_count`、`interactive_followup_target_count`、`interactive_readiness_scope`、`resident_agents_prompt_ready`、`prompt_readiness_scope`、`startup_interactive_blockers` を更新し、trust / onboarding / approval / busy を user-facing blocker として surface する。`resident_agents_response_ready` は互換用の provider 応答証跡 alias とし、新規診断では `resident_agents_provider_response_ready` / `provider_response_readiness_scope` を使う。無効化は `ITB_INTERACTIVE_READINESS_FOLLOWUP_AUTOSTART=0`。
`queue-watch` は pending message の既存 terminal report を回収するだけでなく、回収した terminal report から completion-chain handoff を起動する。nudge 時の tmux / target / prompt readiness は live probe として扱う。tmux 不在や target 欠落は dead-letter + roster `unavailable` に閉じ、prompt busy は pending を維持したまま roster `busy` として記録する。`agent-dispatch` も timeout / unconfirmed / request_sent を roster に残し、`not_invoked` のまま放置しない。
`role-queue` enqueue 時に同一 role の古い pending message がある場合、まず既存 terminal report の回収を試す。未回収の head message が role / hop SLA を超過している場合は、新規 message を消さずに `queue-watch` と同じ watch path で head を 1 件だけ再 nudge / recovery し、その結果を `stale_pending_watch` として nudge evidence に残す。SLA 未満の pending は従来通り `nudge_deferred_pending_message` として head-of-line を維持する。hook 同期 path の prompt-ready check は短い best-effort（既定 0.8s）に抑え、ACK wait / recovery は `queue-watch` / `queue-watch-daemon` の `ack_mode=recover` path へ委譲する。
`role-queue` は必要時だけ builder-side bounded completion wait を持つ。`completion_wait_seconds` または `ITB_ROLE_QUEUE_COMPLETION_WAIT_SECONDS` が 0 より大きい場合、builder は enqueue / nudge 後に role の terminal report file を正として待ち、見つけたら `recover_pending_message_from_existing_report` と同じ schema validation / inbox 更新 / roster 更新 / auto handoff を実行する。pane scrape は completion source ではなく診断 fallback に限定する。wait 結果は `role_queue_completion_wait` event、`completion_wait` metric、`duration_sec`、`completion_source`、`wait_result` として記録する。既定値は `not_requested` で、hook の同期待機時間を増やさない。hook で安全に有効化する場合は `completion_wait_profile=hook_light`（0.75s / 0.1s poll）を使い、明示 `completion_wait_seconds` / `completion_wait_event_driven` は profile より優先する。長めの検証は `daemon_assisted` または `live_validation` profile に限定する。
Final gate は `vault-final-update` command、`finalization-check` command、`final-transport-render-check` command、hook-facing `final-response-guard` command を正本にする。`vault-final-update` は compact command artifacts の SHA と status を `Vault Final Update` thin section に集約する。`finalization-check` は `Finalization Check`、Git publication closure、Vault final update、Completion Envelope、provider transcript 照合を検査し、`status`、`next_phase_allowed`、`notification_class` を返す。`auto_final_transport_render_check: true` の場合だけ missing `Final Transport Render Check` を一時許容し、pass artifact 作成後に `final-transport-render-check` が `Final Transport Render Check` thin section を生成する。`final-response-guard` は hook が毎 turn 発火しても active pre-final task 以外は block せず、active pre-final task では `finalization-check` + `final-transport-render-check` の機械結果が pass した場合だけ `permissionDecision: allow` を返す。失敗時は `decision: block` / `permissionDecision: deny` として main transport renderer へ渡さない。
tmux transport は paste 前に overlay dismiss / composer clear を行い、送信後に request marker ACK を確認する。ACK が取れない場合でも、recovery 開始時と re-paste 直前に request marker が transcript 領域へ submitted 済みかを確認し、既 submit なら追加 Enter / re-paste を行わない。未 submit かつ composer に payload marker が残っている場合だけ追加 Enter、clear + re-paste を試す。最終的に ACK が無い場合は `provider_send_unconfirmed` / `nudge_send_unconfirmed` として evidence に残して block / retry 対象へ回す。
Gate latency の比較は `gate-latency-report` で session-local `gate-metrics.jsonl` を集計し、`claude_haiku_acceptEdits`、`claude_sonnet_interactive`、`codex_exec_json`、`builder_command` を同じ p50 / p90 / avg 軸で比べる。`role-report`、queue report recovery、`role-agent-worker`、`agent-dispatch-report` は provider evidence の `input_tokens` / `output_tokens` / `duration_api_ms` / `turn_duration_ms` / `num_turns` を metrics に転記する。`gtc-scaffold` は deterministic builder command として `gate-task-creator` / `builder_command` metric を書き、`gate-latency-report` は `prompt_submit_comparison` に UserPromptSubmit 起点の GPF p50、`gtc-scaffold` p50、GTC LLM baseline p50、combined SLA verdict、10秒級 target verdict、baseline speedup ratio を出す。provider evidence に token fields が無い場合は、`transcript_path` の JSON / JSONL transcript から同等 usage を non-blocking fallback として補完する。Claude JSONL の `system` / `turn_duration` は API duration ではないため `duration_api_ms` には混ぜず、別指標 `turn_duration_ms` として扱う。`gate-latency-report` は既定で queue report を read-only 参照し、既存 metric に usage が欠落していても matching role report と Claude transcript discovery から report 出力時だけ token / API duration / turn duration を補完する。補完は metric ファイルを書き換えず、`enrich_provider_evidence: false` で無効化できる。latency report の variant row には token 合計、avg API ms、avg turn ms、report enrichment 件数を出す。Claude print / `claude -p` は新規 fast path として推奨せず、OpenAI one-shot は `codex exec --ephemeral --json` の `codex_exec_json` evidence として扱う。

Git 操作権限は `role-agent-registry.yaml` の `git_operations_allowed` と ITB builder の git role allowlist を正本にする。resident role へ `git add` / `git commit` / `git push` などの操作依頼を agent-dispatch で渡した場合、git 系 tool role 以外は dispatch 前に block する。Bash が許可されていても Git publication は `git-publisher` / `commit` / `push` / `pull` / `git-workspace-prep` の一時 tool role 経由で閉じる。
共有 Vault / git repo の横断 serializer は builder command を正本にする。Task Index / Kanban / Task Detail などの共有ファイル更新は `shared-file-update` で `shared-file:<abs path>` lock を取得して行う。git index / commit / push / PR など複数コマンドにまたがる repo 操作だけ、`shared-resource-lock` で `repo:<repo_root>` lease を取得してから進め、完了時に lease_id 一致で release する。serializer event は session-local `shared-serializer-events.jsonl` に記録する。`roster.json` の read-merge-write は `roster.json.lock.d` 内で再読込、merge、atomic replace まで行い、queue-watch / agent-dispatch / provider activation の lost update を避ける。SessionStart が作る `bootstrap.json` / `roster.json` は locked atomic write、`hook-input.json` / `status` / `last_event` / `last-session` / `bootstrap-report.md` は atomic replace で書く。JSONL event は `append_jsonl` 既定で `<file>.lock.d` を取り、旧 call path も locked append に合流させる。
PreToolUse 互換の raw tmux guard は `pretooluse-guard` command を正本にする。`tmux send-keys` / `tmux paste-buffer` が `itb-org-*` target を直接叩く場合は `permissionDecision: deny` / `decision: block` を返す。ITB provider への送信は role queue / nudge / builder command 経由に限定し、hook 設定側はこの command の機械出力を読む。
Stop / pre-response 互換の final response guard は `final-response-guard` command を正本にする。hook 設定側はこの command の `permissionDecision`、`decision`、`notification_class`、`finalResponseGuard.result` を読み、`blocked_finalization_gate` では最終応答を止め、`skipped_*` では通常 turn として通す。
Hook wrapper の repo 内正本は `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/hooks/` に置く。`itb-session-start.sh`、`itb-prompt-preflight.sh`、`itb-final-response-guard.sh`、`itb-pretooluse-guard.sh`、`itb-session-end.sh` は `ITB_RUNTIME` / `ITB_STATE_ROOT` / `ITB_BUILDER` / `ITB_PYTHON` を受け取り、stdin の hook JSON を builder へそのまま渡す薄い wrapper とする。`codex-hooks.example.json`、`codex-config.example.toml`、`claude-settings-hooks.example.json` は settings 登録例であり、dotfiles や provider settings を直接変更しない。
`hook-install` command は wrapper bundle を adapter settings へ反映するための決定論的 installer として扱う。既定は `dry_run` で、`apply: true` が明示されたときだけ settings JSON / Codex config / hook wrapper copy を実行する。`~/.codex/hooks.json` や `~/.claude/settings.json` が symlink の場合は symlink 自体を置換せず、解決先へ atomic write する。既存 hook event は保持し、同じ `itb-*.sh` script だけ canonical command / matcher へ揃える。wrapper を dotfiles へ copy する場合は relative path で builder を推定できないため、installer が settings command に `ITB_BUILDER` を明示する。
`hook-health-check` command は install 後の adapter settings / copied wrapper / `ITB_BUILDER` 到達性を検査する。通常は settings を読むだけで、canonical ITB hook command の runtime、state root、script path、builder path、実行権限を検証する。`run_smoke: true` が明示された場合だけ、任意 shell command を実行せず検証済み wrapper を直接呼び、既定では Stop / PreToolUse の安全な payload で builder 到達性を確認する。`smoke_scripts` / `smoke_events` に `startup_preflight`、`SessionStart`、`UserPromptSubmit` などを明示した場合は、`smoke_state_root` 必須で SessionStart を launch dry-run、UserPromptSubmit を controlled micro-flow として実行し、一時 state 内の bootstrap / preflight event を検査する。UserPromptSubmit 単体 smoke は、同じ `smoke_state_root` に SessionStart smoke 済みの `status=ready` が無い場合 `session_start_smoke_required` で block する。live state を汚さない検証では `smoke_state_root` を指定し、smoke 実行時の `ITB_STATE_ROOT` だけを一時 directory へ上書きする。Codex では `require_hook_trust_state: true` が明示された場合だけ `[hooks.state]` を読み、`session_start`、`user_prompt_submit`、`pre_tool_use`、`stopped` の state entry を検査する。Codex の `trusted_hash` 算出式は ITB 側で未確定のため、hash が存在しても `present_unverified` として扱い strict 検収では block する。`SessionEnd` は Codex trust-state 対象外にする。`check_live_evidence: true` は settings command の `ITB_STATE_ROOT` が指す live state を read-only で読み、`last-session`、`bootstrap.json`、`invocation-evidence.jsonl`、`preflight-events.jsonl`、`final-response-guard-events.jsonl`、`pretooluse-guard-events.jsonl` の観測結果を `live_evidence` に出す。これは smoke 実行ではなく、missing でも既定では block しない。`require_live_evidence: true` の場合だけ、既定の `SessionStart` / `UserPromptSubmit` または `required_live_events` で指定した event が無ければ `live_evidence:<event>:missing` で block する。reload / new session 後の検収では `max_live_evidence_age_seconds` を併用し、required event の timestamp が古い場合は `live_evidence:<event>:stale`、timestamp が無い / 読めない場合は `live_evidence:<event>:timestamp_unknown` で block する。
Bootstrap Report には Agents-Vault policy の `policy_digest_sha1` と policy 別 SHA1 / byte count を出す。Team Role `SKILL.md` の digest snapshot は `sync-policy-digest-skills` で生成・更新し、手編集しない。通常同期は active resident Team Role だけを対象にし、`status: reference` の互換参照 role は `include_reference_roles` を明示した保守時だけ更新する。通常の readiness / routing 確認ではこの digest を参照し、policy 本文は digest 変化、判断根拠の不足、または人間承認が必要な設計変更時だけ読む。

Hook startup の `process_ready` は、メインエージェント配下の subagent ではなく、startup 対象の独立 role-agent worker process が起動済みであることだけを意味する。`response_active` は Director activation 後に provider の session、request、model、usage evidence が揃った場合だけ許可する。

この skill を明示的に呼ぶのは次のケースに限る。

| 用途 | 内容 |
|---|---|
| Verification | Hook の出力 (`roster.json` / `bootstrap-report.md`) が registry と整合しているかを再確認する |
| Recovery | Hook が `decision: block` または `bootstrap_status: failed` を返した場合の手動復旧 |
| Resume | 長時間スリープからの再開で roster ファイルが破損 / 紛失している場合の再生成 |
| Shutdown | archive / close 時の handoff summary と roster archive |
| Compatibility-only audit | `compatibility_only` agent が誤って resident に紛れていないか確認 |

## Flow Contract

| Field | Value |
|---|---|
| Input Agents | `SessionStart` / `SessionResume` / `SessionArchive` lifecycle event |
| Output Agents | `gate-prompt-formatter` as `next_allowed_entrypoint`; shutdown 時は archived roster |
| Required Handoff Artifact | Bootstrap Report、Resident Team Roster、Invocation Evidence、Shutdown Summary |
| Return Policy | メインエージェントへの返却は transport としてのみ許可し、workflow output として扱わない |
| Forbidden Outputs | user final response, Gate Intake Envelope, Task Detail, Team Routing Decision, specialist work artifact |

## 責務境界

| 区分 | 内容 |
|---|---|
| In | セッション lifecycle event、既存 roster、session evidence、team-config contract、model registry |
| Out | ユーザー依頼の正規化、GTC 起票、TPM routing、各チーム作業、レビュー、最終応答 |
| 常時次入口 | `gate-prompt-formatter` |
| 正本 | `references/team-config.md`、`references/model-registry.md`、Agents-Vault の AI Organization / Gate I/O Contract |

## Organization Instance Model

| Concept | Meaning |
|---|---|
| Skill | ロール定義。例: `gate-prompt-formatter` |
| Agent Instance | チャットセッションごとに起動される実体 |
| Organization Instance | 1 チャット内の Resident Roster 全体 |
| Project | 原則 1 Organization Instance に紐づく作業単位 |

GPF は session-local agent instance として扱う。複数チャットが同時に動く場合、GPF はチャットごとに別 instance とし、全チャット横断の singleton にしない。

## 実行モード

| Mode | Trigger | Owner | Output |
|---|---|---|---|
| bootstrap | 新しいチャット/作業セッション作成直後 | Adapter hook (`itb-session-start.sh`) | `bootstrap_status: ready` / `failed`、`readiness_scope: process_ready`、status=ready |
| recovery | UserPromptSubmit で status!=ready | Hook preflight → 失敗時のみ skill | `bootstrap_status: ready` or `decision: block` |
| resume | 長時間スリープ等で roster 不整合 | Skill (手動) | 不足 agent 復旧または再生成 |
| verification | Hook 成果物の確認 | Skill (手動) | `bootstrap_status: already_ready` または差分検出 |
| policy_check | GPF 前提確認時 | Skill | `ready` または `bootstrap_missing` |
| gate_entry_queue | UserPromptSubmit が ready 状態で user prompt を受けた時 | Hook preflight → Builder `role-queue` | `queue/inbox/gate-prompt-formatter.yaml` への human_prompt message、Gate Entry Queue context |
| role_queue_nudge | UserPromptSubmit または上流 role が downstream role に渡す時 | Builder `role-queue` | `queue/inbox/<role>.yaml`、`queue/tasks/<task>/<message>.yaml`、tmux nudge prompt |
| role_provider_report | role provider が inbox を処理する時 | Claude / Codex provider CLI | `queue/reports/<role>/<task>/<report>.yaml`、message status done/failed、provider evidence |
| role_agent_worker | provider evidence を既存 queue report へ記録する補助時のみ | Builder `role-agent-worker` | provider-backed report; local stub completion is forbidden |
| active_task_registration | GTC が Task Detail 作成後に現在 task を登録する時 | Builder `active-task` | `active-task.json` に `task_detail_path` / `flow_phase` / `last_gate` を保存 |
| flow_artifact_check | `task_detail_path` / `ITB_TASK_DETAIL_PATH` または `active-task.json` が渡された preflight | Hook preflight | `gtc-scaffold`→TPM、TPM→director、`vault-final-update` / `finalization-check` / `final-transport-render-check` 証跡の欠落を block |
| stale_active_task_recovery | 完了済みまたは invalid final の `active-task.json` が通常 UserPromptSubmit を塞ぐ時 | Hook preflight | active-task を recovery clear し、原因を `active-task-events.jsonl` と `preflight-events.jsonl` に残して GPF 入口へ戻す |
| provider_activation | 明示的な検証入力で Gate / Team Role provider response を確認 | ITB verification | Claude / Codex provider の runtime response evidence、model、session、token usage を roster / invocation evidence に記録 |
| shutdown | archive / close 時 | Skill または adapter hook | handoff summary、resident shutdown、archived roster |

## 実行手順

1. Roster を確認する。
   - 既存の Resident Team Roster、bootstrap state、session evidence を読む。
   - `role_id`、`agent_instance_id`、`organization_instance_id`、`chat_session_id`、`project_id` を照合する。
   - bridge、commit、save、Obsidian CLI などの道具スキルが resident に混入していないことを確認する。

2. readiness を判定する。
   - 各 resident agent について `resident_status`、`activation_status`、`metadata_status`、`response_status`、`session_id`、`effective_model`、`usage_source` を確認する。
   - bootstrap hook 直後の Gate / Infra は `metadata_ready`、Tech / Contents / Business は `idle` として扱う。
   - `process_status: process_ready` は独立 tmux process の起動証跡として扱い、`provider_status`、`tool_sidecar_status`、`response_status` とは混同しない。
   - 既定の `resident_process_mode` は `provider_cli` とし、tmux 上の provider process を readiness 対象にする。tool sidecar readiness と provider response evidence は別に扱う。
   - `response_active` は provider の `session_id`、`effective_model`、request / usage evidence が揃った場合だけ許可する。
   - `startup_profile: provider_cli` の process readiness が揃っていれば、`bootstrap_status: ready` と `readiness_scope: process_ready` を出して終了する。`lazy_activation` の role は未起動でも ready を妨げない。

2.5. Task flow artifact が渡された場合は、組織フロー証跡を検査する。
   - UserPromptSubmit では、GPF dispatch / queue 前に deterministic pre-GPF classifier を実行し、`workflow_mode`、`risk_tier`、`fast_path_candidate`、`approval_required` を queue payload、dispatch prompt、preflight event に記録する。
   - classifier は read-only / no-diff / single-team 候補だけを `controlled_micro_flow` / `low` とし、read-only でもファイル読解、コードレビュー、調査、比較、差分確認など provider 判断が必要な場合は `standard_flow` として通常 Gate flow に渡す。書き込み、権限、security、publication、policy などの trigger があれば `strict_flow` へ倒す。
   - GPF は classifier を default として使い、必要な場合だけ strict 側へ escalate する。strict 判定を standard / micro へ downgrade してはならない。
   - `task_detail_path`、`taskDetailPath`、`task_path`、または `ITB_TASK_DETAIL_PATH` で Task Detail を受け取る。
   - hook input / env に Task Detail が無い場合は、session state の `active-task.json` を fallback として読む。
   - GTC は Task Detail 作成後に `active-task` command で `task_id`、`task_detail_path`、`flow_phase`、`owner_role`、`last_gate` を登録する。
   - `active-task.json` が壊れている、または active status なのに `task_detail_path` が無い場合は block する。
   - Task Detail がすでに `done` / `complete` / `closed` の場合でも、commit_required / publication_required が残っていないかを先に検査する。
   - completed task に task-owned Git diff の未完了 `Git Publication Result`、`deferred_not_requested`、`not_requested`、commit hash 不足が残る場合、その `active-task.json` は stale として解除せず block する。
   - Git publication gate が閉じている completed task だけを stale として recovery clear し、通常の UserPromptSubmit をブロックしない。
   - fallback 由来の `pre_final_response` active-task が final validation に失敗した場合、完了扱いにはせず recovery clear し、validation errors を証跡に残して新規プロンプト入力を GPF へ戻す。
   - `flow_phase` / `ITB_FLOW_PHASE` は `pre_execution`、`post_routing`、`pre_final_response` のいずれかを扱う。
   - `pre_execution` では Execution Preflight 6項目と `Project Manager Handoff -> teams-project-manager` を必須にする。
   - `post_routing` では TPM の `Team Routing Decision` と director handoff、Completion Gate の維持を必須にする。
   - `pre_final_response` では `Team Completion Check`、`Quality Evaluation`、`Task Change Manifest`、`Role Execution Evidence`、`Finalization Check`、`Completion Envelope`、`Final Transport Render Check`、必須 Gate roles の Invocation Evidence、Vault final update、Git publication gate closure を必須にする。
   - `finalization-check` command は `pre_final_response` の機械ガードであり、pass では `notification_class: done`、block では `notification_class: flow_alert` を返す。この field を UI / OS 通知 / hook 側の判断に使い、自由文 reason の推論へ戻らない。
   - `final-response-guard` command は Stop / pre-response 系 hook から呼ぶ薄い wrapper として扱う。active task が無い、または `flow_phase != pre_final_response` の場合は silent skip し、active pre-final task がある場合だけ `finalization-check` と `final-transport-render-check` を実行して pass / block を返す。
   - active task が無く、pre-GPF classifier が `read_only_no_diff_single_team` / `low` / approval 不要と判定し、Git diff が clean または repo なしの場合、`prompt-preflight` は controlled micro-flow fast-path を使って role provider turn を 0 にする。builder は TPM completion / evaluation / finalization / Completion Envelope artifact を `state/<session>/gates/<micro-task>/` に書き、main transport renderer へ直接進める。
   - fast-path は read-only/no-diff/single-team 専用。dirty repo、write/edit intent、approval、publication、multi-team、`force_gate_entry_queue` / `force_gate_entry_dispatch` がある場合は通常 Gate flow に戻す。
   - Task Detail は thin index として扱う。詳細ログ、レビュー本文、validation output、manifest detail は role-report / command artifact を正本にし、Task Detail には `task-detail-append` command で status、1行 summary、report path、report sha256 だけを残す。`Git Publication Result` は thin section でもよく、builder は linked report artifact の `git_publication_result` fields を validation に使う。
   - `pre_final_response` では Task Detail の line cap を完了 lint として扱い、既定 cap 超過時は block する。途中段階の cap 超過は warning として、verbose evidence を report file へ退避する。
   - `Final Transport Render Check` は main transport renderer による表示整形だけを証跡化する。実装、調査、レビュー、最終判定の evidence として `main_transport_renderer` を使ってはならない。
   - `Role Execution Evidence` に `main-agent`、`codex-main`、`claude-main`、`entrypoint` を実行者として記録してはならない。最低 1 つの non-gate role が provider-backed usage source で complete している必要がある。
   - `active-task` command で `pre_final_response` を登録する場合も同じ final validation を先に実行し、失敗した active-task は作成しない。
   - Provider activation transcript 照合は `pre_final_response` のみで必須にし、pre-execution の初期 Invocation Evidence 表だけで通常入力を止めない。
   - Provider evidence は provider family だけでなく model tier も照合する。registry の intended model tier と effective model tier が異なる場合は block ではなく warning にし、GTC が Haiku 想定なのに Sonnet で完了したような latency-affecting drift を completion evidence に残す。runtime removed の compatibility role は tier warning 対象外にする。
   - Controlled Micro-Flow では、Task Detail の `Controlled Micro-Flow` section が完全で、`Risk Tier: low`、`Organization Policy: preserved`、`Strict Flow Escalation Checked: true`、`Local Gate Evidence Allowed: true`、`External Provider Dispatch: not_required_for_micro_flow`、`Escalation Required: false`、`Escalation Triggers: none` が揃う場合だけ、`local_controlled_micro_flow` usage source を provider transcript の代替 evidence として許可する。
   - Controlled Micro-Flow section が無い local evidence、または上記 control field のいずれかが欠ける local evidence は block する。
   - failed / timeout / unavailable provider evidence が残り、後続の成功 transcript で閉じていない場合は micro-flow でも block する。
   - hook input / env / active-task のいずれにも artifact が無い通常の UserPromptSubmit では、この追加検査は実行しない。

2.6. Provider activation 検証では、runtime response evidence を記録する。
   - `provider-activate` は既存 roster から `agent_id` を探し、registry の provider/runtime に従って provider adapter を選ぶ。
   - OpenAI / `execution_mode: codex` 対象では `codex exec --ephemeral --json` を使い、最小 prompt の応答、effective model、session、request、input/output token を記録する。
   - Claude 対象の旧 print-mode 経路は互換参照として残るが、新規改善方針では `claude --print` / `claude -p` を推奨しない。
   - primary Claude model に Claude fallback が登録されている場合は `--fallback-model` を付け、primary capacity 時の代替を provider 側に渡す。
   - one-shot provider adapter が exit 0 でも、result/message、input/output token、turns、API duration のいずれも無い場合は実推論未確認として block する。過去の `response_active` 証跡が残っている場合も無効化する。
   - token 消費を伴うため、live 実行は明示承認または検証用環境でだけ行う。unit test は fake provider を使って外部 token を消費しない。
   - provider response evidence が取れた row だけ `activation_status: response_active` に更新し、`usage_source` は Codex one-shot では `codex_exec_json`、Claude 互換経路では `claude_print_json` として記録する。

2.7. Gate output schema は `config/gate-output-schemas.yaml` を正本にする。
   - Team Completion Check、Quality Evaluation、Task Change Manifest、Finalization Check、Completion Envelope、Final Transport Render Check の必須 field / required value / truthy-falsy / main-agent self-cert 禁止 field は config で管理する。
   - builder の pre-final validator はこの schema を読み、Markdown table 互換の section でも同じ field alias を検査する。
   - Gate role SKILL.md の出力例は人間向け template であり、完了判定の機械条件は schema config と builder validator を正本にする。
   - `role-report` / `role-agent-worker` / queue recovery / dead-letter の terminal report は `validate_terminal_queue_report` を書き込み直後に通し、report 内の `schema_validation.status` と inbox / queue event / metrics の integrity を一致させる。

3. 不足 agent だけを activation 対象にする。
   - 未起動、session 不明、モデル不一致、`unavailable`、または `startup_profile: lazy_activation` の agent だけを activation 対象にする。
   - `references/model-registry.md` の `primary_model`、`provider`、`execution_mode` に従い、ITB が provider activation を行う。
   - registry に存在しない active Team Role は起動せず、`registry_missing` として fail する。
   - 起動プロンプトは resident shell として最小化し、full SKILL.md / references は active 化時に読む。

4. Ready 状態を記録する。
   - Gate / Infra は bootstrap 時点では常時 `metadata_ready`。
   - Tech / Contents / Business は resident だが初期状態 `idle`。
   - hook startup では `process_status`、`launch_status`、`runtime_kind`、`process_mode`、`provider_status`、`tool_sidecar_status`、`tmux_target` を Invocation Evidence に記録する。
   - provider response を実行していない bootstrap では `usage_source: bootstrap_metadata_only` を維持する。
   - 実 activation 後に intended/effective model、requestId、sessionId、usage source を Invocation Evidence に追記する。`provider: anthropic` または `primary_model: claude-*` の role が Codex/OpenAI evidence で response_active になった場合は policy violation として block する。

5. Shutdown を実行する。
   - active agent は handoff summary を Vault に残す。
   - `bootstrap.json` の `organization_instance_id` と `tmux_session` を照合し、`tmux_session == itb-<organization_instance_id>` の場合だけ対象 tmux session を停止する。
   - session-local detached process（`queue-watch-daemon.pid` / `interactive-readiness-followup.pid`）は pid file と command line が ITB builder の該当 daemon と一致する場合だけ SIGTERM し、結果を `detached_process_shutdown` として `shutdown.json` に記録する。
   - resident agent を停止し、roster / state を `archived` に更新する。
   - `shutdown.json`、Invocation Evidence、`last_seen_at`、終了理由、未完了 task、復旧に必要な情報を記録する。
   - tmux が未起動、既に停止済み、または unsafe target の場合も archive evidence を残し、誤って別 session を停止しない。
   - state dir retention は archived / shutdown 済み session に加え、`bootstrap.json` の `tmux_session` が存在しない古い ready/pending session を orphan として archive 候補に入れる。

## Resident Shell Prompt

resident 起動時は、次の最小情報だけを渡す。

```markdown
You are `<agent_id>` in organization instance `<organization_instance_id>`.
Current state: resident / metadata_ready when always_active is true; otherwise resident / idle.
Input Agents: <from Flow Contract>
Output Agents: <from Flow Contract>
Do not perform work until activated by your valid input agent.
Read your full SKILL.md only when activated.
Write decisions and handoffs to Vault when active work is assigned.
```

## Bootstrap Report

```markdown
## Bootstrap Report

| Field | Value |
|---|---|
| bootstrap_status | `ready` / `already_ready` / `blocked` / `failed` |
| readiness_scope | `metadata_only` / `process_partial` / `process_ready` / `response_evidence` |
| bootstrap_trigger | `session_start` / `session_resume` / `manual_recovery` / `policy_check` |
| organization_instance_id |  |
| chat_session_id |  |
| project_id |  |
| team_config_source | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/team-config.md` |
| adapter | `claude-team` / `codex` / `manual` |
| resident_agents_registered |  |
| resident_agents_process_ready |  |
| resident_agents_provider_ready |  |
| resident_agents_prompt_ready | prompt 入力可能と判定された provider CLI resident 数 |
| prompt_readiness_scope | `interactive_*` / `not_checked_*` |
| resident_agents_tool_sidecar_ready |  |
| resident_agents_response_ready | legacy alias for provider response evidence |
| resident_agents_provider_response_ready | provider session / request / usage evidence 付きの応答済み resident 数 |
| provider_response_readiness_scope | `response_evidence` / `not_invoked` |
| resident_agents_ready | compatibility alias for `resident_agents_registered` |
| resident_process_mode | `provider_cli` / `resident_shell` fallback |
| process_launch_target_count |  |
| process_launch_failure_count |  |
| tmux_session |  |
| unavailable_agents |  |
| model_mismatches |  |
| registry_source | `Agent-Teams-Viewer/organization/runtime/infra-team-bootstrap/references/model-registry.md` |
| next_allowed_entrypoint | `gate-prompt-formatter` |
| notes |  |
```

## 禁止事項

- ユーザー発話から ITB へ直通分岐しない。
- `gate-prompt-formatter` の代わりに自然文を解釈しない。
- `gate-task-creator` の代わりに Task Detail を作らない。
- `teams-project-manager` の代わりに task active set を決めない。
- idle agent に自発的な発話、作業、Vault 更新、レビューをさせない。
- full SKILL.md / references を全 agent に初期注入しない。

## Validation Checklist

| Check | Required |
|---|---|
| 既存 roster を先に確認した | Yes |
| 全 agent 起動済みなら `already_ready` で終了した | Yes |
| 未起動 agent だけを起動対象にした | Yes |
| Gate / Infra を metadata_ready にした | Yes |
| Tech / Contents / Business を resident idle にした | Yes |
| 道具スキルを resident に混ぜていない | Yes |
| bootstrap metadata-only evidence を記録した | Yes |
| hook startup で provider CLI の独立 tmux process readiness を記録した | Yes |
| provider / tool sidecar readiness を process readiness と分けた | Yes |
| response_active に model / session / usage evidence を必須化した | Yes |
| controlled_micro_flow の local evidence は Control section 完備時だけ許可した | When applicable |
| next allowed entrypoint を GPF にした | Yes |
| Task Detail が渡された preflight で `gtc-scaffold`→TPM、TPM→director、`vault-final-update` / `finalization-check` / `final-transport-render-check` 証跡を検査した | When applicable |
| Task Detail が hook input に無い場合に active-task fallback を確認した | When applicable |
| 完了済みまたは invalid final の stale active-task が通常入力を塞がない | When applicable |
| Provider activation 検証で Claude response/token usage evidence を記録した | When applicable |
| archive / close 時に shutdown summary を残す | Yes |
| shutdown は該当 Organization Instance の tmux session だけを停止する | Yes |

## Related References

- `references/team-config.md`
- `references/model-registry.md`
- `references/adapters/claude-team.md`
- `references/adapters/codex-team.md`

<!-- ITB_POLICY_DIGEST_SNAPSHOT_START -->
## ITB Policy Digest Snapshot

This block is generated by `infra-team-bootstrap sync-policy-digest-skills`.
Use the digest for routine freshness checks; read full policy bodies only when this digest changes, required judgment evidence is missing, or human approval is needed.
Narration policy: act on routine flow checks silently; surface only anomaly or approval blockers as `[FLOW-ALERT]`.

| Field | Value |
|---|---|
| policy_digest_status | `ready` |
| policy_digest_sha1 | `3208f43814e1e595e6baf885b6bc3e5641653fc4` |

| Policy | Status | SHA1 | Bytes | Source |
|---|---|---:|---:|---|
| AI-Organization | `ready` | `380b4a2cab2325b88f68993485ae997428265913` | 31825 | `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Agents-Vault/03-Contexts/Policies/AI-Organization.md` |
| Gate-IO-Contract | `ready` | `7af1c38f0b140feb45a11009ca94f70da542344d` | 34482 | `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Agents-Vault/03-Contexts/Policies/Gate-IO-Contract.md` |
| Dispatcher-IO-Contract | `ready` | `75cd888d160d7ae0a87640cefd1268ea84b4209e` | 6188 | `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Agents-Vault/03-Contexts/Policies/Dispatcher-IO-Contract.md` |
| Task-File-Conventions | `ready` | `ac5b009a443216dd7b00ebaa5541eaecfe341176` | 18748 | `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Agents-Vault/03-Contexts/Policies/Task-File-Conventions.md` |
<!-- ITB_POLICY_DIGEST_SNAPSHOT_END -->
