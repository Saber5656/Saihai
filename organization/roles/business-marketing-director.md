---
name: business-marketing-director
description: 非推奨の互換エイリアス。旧名称 `business-marketing-director` を参照する既存ノートやタスクを読むときだけ使い、通常は `business-director` を使う。
user-invocable: false
allowed-tools: Read, Grep, Glob
category: Team Role
created: 2026-05-09
updated: 2026-05-20
status: deprecated
purpose: business-director への移行用エイリアス
team: business
agent_id: business-marketing-director
---

# Business Marketing Director

このスキルは旧名称の互換用だよ。
新しい正本は `${SKILLS_ROOT}/business-director/SKILL.md`。

## 移行ルール

- 新規の組織図、タスク、参照は `business-director` を使う
- 旧名が残るノートは段階的に `business-director` へ置き換える
- 互換参照で読んだ場合でも、実際の判断と記録は `business-director` 名義に寄せる

## Flow Contract

| Field | Value |
|---|---|
| Input Agents | Legacy references only |
| Output Agents | `business-director` |
| Required Handoff Artifact | Alias resolution note |
| Return Policy | メインエージェントへの返却は transport としてのみ許可し、workflow output として扱わない |
| Forbidden Outputs | new workflow ownership, user final response, specialist work artifact |

## 現状

- このスキル自体は新規運用しない
