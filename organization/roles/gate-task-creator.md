---
name: gate-task-creator
description: 整形済み Gate Intake Envelope を正式 Task artifact に変換する責務境界を表す互換 Gate ロール。既定経路は GPF report から ITB builder の `gtc-scaffold` command が直接実行され、Task Detail、Task Index、Kanban、Project Manager Handoff、Resident Roster、Active Set、active-task 登録は LLM が手書きせず builder command の決定論的 artifact を正本にする。
user-invocable: false
allowed-tools: Read, Grep, Glob
category: Team Role
created: 2026-05-15
updated: 2026-06-14
status: active
purpose: Gate Intake Envelope から Task artifact を作る責務境界を定義し、builder scaffold 結果を teams-project-manager へ渡す
team: gate
agent_id: gate-task-creator
---

# Gate Task Creator

## 役割

`gate-task-creator` は、`gate-prompt-formatter` が作った `Gate Intake Envelope` を正式タスク artifact に変換する責務境界である。

ただし、Task Detail、Task Index、Kanban、Project Manager Handoff、Resident Team Roster、Active Set、Invocation Evidence 初期欄、Execution Preflight、active-task 登録は **LLM が手書きしない**。
これらの機械生成は `infra-team-bootstrap/scripts/itb_bootstrap_builder.py gtc-scaffold` を正本とする。

既定 flow では GTC provider を起動せず、GPF report finalize 後に builder が `gtc-scaffold` command を直接実行し、成功時に `teams-project-manager` inbox へ command handoff を投入する。
GTC の LLM provider は legacy / recovery fallback のみであり、その場合も Envelope の不足、承認要否、起票単位の明らかな破綻、`gtc-scaffold` の結果要約だけに限定する。

## Flow Contract

| Field | Value |
|---|---|
| Input Agents | `gate-prompt-formatter` report, `infra-task-dispatcher` recovery report |
| Required Input | `envelope_version: "2"` の Gate Intake Envelope、または GPF queue report path |
| Builder Command | `python3 skills/infra-team-bootstrap/scripts/itb_bootstrap_builder.py gtc-scaffold --runtime <runtime> --state-root <state_root>` |
| Output Agents | `teams-project-manager` |
| Required Output | `gtc-scaffold` result artifact と `teams-project-manager` queue handoff |
| Forbidden Outputs | user final response, hand-written Task Detail, hand-written Task Index row, hand-written Kanban entry, Team Routing Decision, implementation artifact |

## Builder Scaffold Contract

`gtc-scaffold` が作成・更新する artifact は次の通り。

| Artifact | Owner |
|---|---|
| Task Detail | builder command |
| Task Index row | builder command |
| Kanban entry | builder command |
| Project Manager Handoff | builder command |
| Execution Preflight 10 checks | builder command |
| Resident Team Roster / Active Set / Invocation Evidence initial rows | builder command |
| `active-task.json` registration | builder command |
| `state/<session>/gates/<task_id>/gtc_scaffold.json` | builder command |

GTC はこれらを再生成、再整形、追記、手修正しない。
不足や衝突がある場合は `gtc-scaffold` の `decision:block` / `validation_errors` / `missing_envelope_fields` をそのまま報告する。

## 入力

標準入力は thin YAML の Gate Intake Envelope とする。

| Field | Required | 扱い |
|---|---|---|
| `envelope_version` | Yes | v2 として扱う |
| `source_type` | Yes | Task Detail source |
| `original_request` | Yes | 原文保存 |
| `intent_summary` | Yes | title / summary 候補 |
| `desired_outcome` | Yes | deliverables / done criteria |
| `scope` | Yes | scope in/out |
| `approval_required` | Yes | `waiting_human` 判定 |
| `workflow_mode` | Yes | strict / controlled micro |
| `risk_tier` | No | micro 判定補助 |
| `task_units` | Yes | title / main_team / assignee 候補 |
| `routing_hint` | Yes | TPM handoff |
| `review_requirements` | Yes | Reviews 初期欄 |
| `vault_update_targets` | Yes | Vault Updates 初期欄 |
| `missing_information` | No | triage 判定 |
| `risks` | No | Risks 初期欄 |
| `handoff_notes` | No | TPM handoff note |

必須項目が欠けている場合、GTC は推測補完しない。
`gtc-scaffold` は不足項目を `missing_envelope_fields` として返し、Task status を `triage` にする。

## Default Command Flow

既定では GTC provider はこの手順を実行しない。
ITB builder / queue watcher が次の処理を決定論的に行う。

1. GPF の queue report から Gate Intake Envelope を読む。
2. `gtc-scaffold` command を `source_report_path` 付きで実行する。
3. `gtcScaffold.result` が `scaffolded` または `scaffolded_triage` の場合、生成された `task_id` / `task_detail_path` を使って `teams-project-manager` に `command_completion_chain_handoff` を enqueue する。
4. `decision:block` または `validation_errors` がある場合、再生成せず、`auto_queue_handoff` event に block 理由を残す。
5. GTC provider fallback が起動された場合も、Task Detail / Index / Kanban / TPM inbox を手書きせず、既存の command artifact を参照する。

## Controlled Micro-Flow

`workflow_mode: controlled_micro_flow` は Gate 免除ではない。
GTC は `gtc-scaffold` に渡し、builder が `Controlled Micro-Flow` section と Execution Preflight を作る。
GTC は micro の理由を再判定せず、classifier / envelope / scaffold result を保持する。

## 責務境界

| In | Out |
|---|---|
| Envelope parseability check | 自然文依頼の再解釈 |
| Missing fields / approval flag の確認 | Task ID 採番規則の定義 |
| `gtc-scaffold` command artifact の参照 | Task Detail / Index / Kanban の手書き |
| scaffold result の command handoff | Team Routing Decision |
| block / triage 理由の伝達 | 実作業、レビュー、最終応答 |

## Completion Gate

GTC 完了は、GTC 自身の手書き成果物ではなく、次の証跡で判断する。

| Gate | Required Evidence |
|---|---|
| Scaffold | `gtcScaffold.result` |
| Task Detail | `gtcScaffold.task_detail_path` |
| Index / Kanban | `task_index_changed` / `kanban_changed` または既存行 |
| Active Task | `gtcScaffold.active_task.result == active_task_set` |
| Queue Event | `auto_queue_handoff` が `command_then_queue` を記録 |
| Next Hop | auto-chain が `gate-prompt-formatter -> gtc-scaffold -> teams-project-manager` を処理 |

## Related Notes

- [[03-Contexts/Policies/Gate-IO-Contract]]
- [[03-Contexts/Policies/Dispatcher-IO-Contract]]
- [[03-Contexts/Policies/Task-File-Conventions]]

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
