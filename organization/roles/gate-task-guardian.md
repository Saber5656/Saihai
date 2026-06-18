---
name: gate-task-guardian
description: 互換参照用。現行フローでは git-publisher、GTE final review、Agents-Vault final update 後の最終確認は finalization-check command evidence に統合する。旧 Guardian Verdict を読むときだけ参照する。
user-invocable: false
allowed-tools: Read, Grep, Glob
category: Team Role
created: 2026-05-18
updated: 2026-06-14
status: reference
purpose: 旧 Guardian Verdict 契約の互換参照。runtime resident agent としては使わず、Finalization Check を正本にする
team: gate
agent_id: gate-task-guardian
---

# Gate Task Guardian

## 役割

`gate-task-guardian` は旧フローの互換参照である。
現行フローでは、`gate-task-evaluator`、`git-publisher` の後に、ITB builder の `vault-final-update` command が compact gate artifacts を Agents-Vault final update として一度だけ rollup し、その後 `finalization-check` command evidence としてタスクを `done` として扱ってよいかを最終確認する。
`finalization-check` が pass した後、`final-transport-render-check` command が `Final Transport Render Check` section / artifact を生成してから main transport renderer へ渡す。`auto_final_transport_render_check: true` の場合だけ、この生成を `finalization-check` から続けて実行できる。
`Git Publication Result` が thin section の場合、`finalization-check` は Task Detail 本文ではなく linked report artifact の `git_publication_result` fields を正本として検査する。
repo 全体の clean state ではなく、この task の Task Change Manifest と Git Publication Result が commit / push / PR の必要証跡で閉じているかを確認する。

`Finalization Check` が complete になるまで、Task Detail を `done` にせず、main transport renderer へ渡さない。旧 `Guardian Verdict` は既存タスクの読み取り互換としてだけ扱う。

## Flow Contract

| Field | Value |
|---|---|
| Input Agents | Legacy Vault final update, GTE final review, and Git Publication Result references |
| Output Agents | none for runtime; use `finalization-check` / `final-transport-render-check` |
| Required Handoff Artifact | Legacy Guardian Verdict reference only |
| Return Policy | メインエージェントへの返却は transport としてのみ許可し、workflow output として扱わない |
| Forbidden Outputs | user final response, quality evaluation, commit execution, main transport render handoff |

## Report / Queue Boundary

この Gate role は現行 workflow の queue consumer ではない。
新規 task で `gate-task-guardian` inbox、role-report、provider turn、tmux resident process を作ってはならない。
旧 Task Detail を読むときも、互換 section の意味を `finalization-check` / `final-transport-render-check` の現行 artifact へ写像するだけにする。

## Builder Precheck Compatibility

旧 `guardian-precheck` は互換 alias として残すが、正本 command は `finalization-check` と `final-transport-render-check` である。

| Precheck | Required |
|---|---|
| Command | `itb_bootstrap_builder.py finalization-check` |
| Default phase | `pre_final_response` |
| Pass condition | `precheck_status: pass` |
| Block behavior | `validation_errors` を GTE / publication / final update へ戻す理由にし、独立 guardian LLM 再監査へ進まない |
| Pass behavior | guardian runtime を起動せず、`final-transport-render-check` 後に main transport renderer へ進む |
| Notification class | command artifact の `notification_class` を正本にする。`done` は最終表示へ進行可、`flow_alert` は完了応答前に block / operator alert、`approval_wait` は人間承認待ちとして扱う |

`finalization-check` は実行時に `active-task.json` を `flow_phase: pre_final_response` / `last_gate: finalization-check` に更新する。これに失敗した場合、最終応答へ進めず Task Detail / active-task の整合性を直す。

## Current Flow Mapping

| Legacy Concept | Current Source Of Truth |
|---|---|
| old Guardian Verdict | `Finalization Check` JSON artifact plus Task Detail thin section |
| old guardian complete status | `finalization-check.status: pass` and `next_phase_allowed: true` |
| old renderer readiness check | `final-transport-render-check` artifact |
| old handoff to final renderer | builder / hook guard output from `final-response-guard` |

## New Task Rules

新規 task では次を禁止する。

| Forbidden | Replacement |
|---|---|
| `gate-task-guardian` を resident / queue consumer として起動する | `finalization-check` command |
| Git / Vault / Index / Kanban / Completion Envelope を guardian LLM に再検査させる | compact command artifacts and linked report files |
| guardian 名義の Markdown 判定表を Task Detail に追加する | `task-detail-append` による `Finalization Check` thin section |
| main transport renderer への handoff を guardian が手書きする | `final-transport-render-check` + final response guard |
| commit / push / PR を guardian から実行する | `git-publisher` / publication command flow |

## Legacy Read Compatibility

過去 task に旧 verdict section が残る場合は、次の互換情報として読む。

| Old Field | Interpret As |
|---|---|
| `complete` | 現行 `finalization-check.status: pass` 相当。ただし新規証跡ではない |
| `incomplete` / `blocked` / `missing` | 現行 `validation_errors` / `blockers` 相当 |
| old commit / push / PR rows | `Git Publication Result` linked report の手掛かり |
| old reasons | GTE、git-publisher、Vault final update へ戻す修復 hint |

この互換読み取りは完了証跡を新規生成しない。
現行フローの complete 判定には、必ず `finalization-check` command artifact、`final-transport-render-check` artifact、Completion Envelope、linked Git publication report を使う。

## Related Notes

- [[03-Contexts/Policies/AI-Organization]]
- [[03-Contexts/Policies/Gate-IO-Contract]]
- [[03-Contexts/Policies/Dispatcher-IO-Contract]]

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
| AI-Organization | `ready` | `380b4a2cab2325b88f68993485ae997428265913` | 31825 | `${AGENTS_VAULT_ROOT}/03-Contexts/Policies/AI-Organization.md` |
| Gate-IO-Contract | `ready` | `7af1c38f0b140feb45a11009ca94f70da542344d` | 34482 | `${AGENTS_VAULT_ROOT}/03-Contexts/Policies/Gate-IO-Contract.md` |
| Dispatcher-IO-Contract | `ready` | `75cd888d160d7ae0a87640cefd1268ea84b4209e` | 6188 | `${AGENTS_VAULT_ROOT}/03-Contexts/Policies/Dispatcher-IO-Contract.md` |
| Task-File-Conventions | `ready` | `ac5b009a443216dd7b00ebaa5541eaecfe341176` | 18748 | `${AGENTS_VAULT_ROOT}/03-Contexts/Policies/Task-File-Conventions.md` |
<!-- ITB_POLICY_DIGEST_SNAPSHOT_END -->
