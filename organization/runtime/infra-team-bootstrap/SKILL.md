---
name: infra-team-bootstrap
description: ATV headless runtime の SessionStart metadata pointer、Stop final gate、CLI role dispatch、Vault evidence を扱う Infrastructure ロール。
user-invocable: false
allowed-tools: Read, Grep, Glob, Bash, Write, Edit, Agent
category: Team Role
created: 2026-05-20
updated: 2026-06-23
status: active
purpose: headless Organization Instance の metadata / final gate / evidence を管理する
team: infra
agent_id: infra-team-bootstrap
---

# Infra Team Bootstrap

## 役割

`infra-team-bootstrap` は ATV headless runtime の制御面を担当する。

この role はユーザー依頼を直接解釈せず、Task Detail の成果物を手書きせず、実作業を開始しない。責務は session metadata、task-owned `execution_context` pointer、final gate verdict、queue / report / provider evidence の整合確認に限定する。

## Hook Contract

初期 hook set は二つだけに固定する。

| Event | Command | Role |
|---|---|---|
| `SessionStart` | `session-start` | metadata-only pointer を書く |
| `Stop` | `final-response-guard` | typed execution context を read-only で検査する |

`SessionStart` が書いてよい state は `session_id`、`runtime`、`cwd`、`started_at`、harness/config digest、`active_execution_context: null`、`active_execution_context_pointer_path` だけである。

`Stop` は deterministic final gate として毎回走れる必要がある。LLM / provider dispatch、queue progression、role dispatch、auto scaffold、Plan/fix 開始、transcript や broad Markdown の解釈は行わない。

## Execution Context

hard-block 判定の正本は task-owned `execution_context` JSON である。

| Schema | Owner |
|---|---|
| session pointer | SessionStart writer |
| canonical execution context | task phase の single writer |
| final gate verdict | Stop read-only gate |

blocking は `none` / `blocking` の二値だけを扱う。`none` は whitelist 方式で、未知の operation や required evidence 不足は `blocking` に倒す。

loop policy は gate block recovery cycle を単位に数える。既定 budget は 5、tuning range は 5-8、same blocker consecutive cap は 2 とする。

## Role Dispatch

role execution は headless CLI worker を使う。

| Provider | Runtime |
|---|---|
| Anthropic | `claude --print --output-format json` |
| OpenAI | `codex exec --ephemeral --json` |

provider response evidence は request id、provider session id、effective model、usage、duration、typed report path を記録する。Hook から provider call は行わない。

## Queue And Reports

`role-queue` は durable inbox / task payload / report path を作るだけで、provider を自動進行しない。既存 terminal report の回収、manual close、replay は明示 command として扱う。

`role-report` は provider-backed report を atomic に確定する。local stub completion は forbidden。

## Finalization

finalization の機械正本は `vault-final-update`、`finalization-check`、`final-transport-render-check`、`final-response-guard` の typed artifacts である。

final gate は small canonical allow/block schema を返す。

| Verdict | Meaning |
|---|---|
| allow | active context が無い、または required evidence complete |
| block | required evidence incomplete、approval required、または typed blocker present |

typed next action は `plan`、`fix`、`ask_human`、`mark_blocked` のいずれかだけを返す。

## Live Config Safety

repo 内の example / config / script / test は変更してよい。`~/.claude` / `~/.codex` の live config は明示承認なしに変更しない。
