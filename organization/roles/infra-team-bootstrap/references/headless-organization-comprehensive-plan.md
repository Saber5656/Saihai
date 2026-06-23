# Headless Organization Comprehensive Plan

Updated: 2026-06-23

## Objective

ATV organization runtime は Codex App / mobile remote-control の形を保ちつつ、組織運用の制御を main agent の任意判断から分離する。

Phase0/1 の repo 実装は hook を metadata observer と deterministic final gate に縮小し、role work は明示的な headless CLI worker と typed evidence で扱う。

## Initial Hooks

| Hook | Contract |
|---|---|
| `SessionStart` | session pointer metadata only |
| `Stop` | read-only final gate over typed execution context |

Hook は role dispatch、queue progression、task scaffold、provider call、Plan/fix 開始を行わない。

## Canonical State

| State | Writer | Reader |
|---|---|---|
| session pointer | SessionStart | final gate |
| task-owned execution context | task phase owner | final gate |
| final gate verdict | final gate | main transport |

Hard-block 判定は execution context の typed state を正本にする。blocking は `none` / `blocking` の二値で、whitelist 外は `blocking` に倒す。

## Command Chain

Completion chain は typed artifacts を正本にする。

| Chain | Required command |
|---|---|
| TPM completion | `-> team-completion-check` |
| final evidence | `-> finalization-check` |
| final rendering | `-> final-transport-render-check` |

各 command は `status`、`missing_evidence`、`blockers`、`next_phase_allowed`、`next_action`、`notification_class` を typed field として返す。

## Role Execution

| Provider | Runtime |
|---|---|
| Anthropic | `claude --print --output-format json` |
| OpenAI | `codex exec --ephemeral --json` |

provider output は request id、provider session id、effective model、usage、duration、report path を evidence として保存する。

## Loop Policy

| Policy | Value |
|---|---|
| total recovery cycle budget | 5 |
| tuning range | 5-8 |
| same blocker consecutive cap | 2 |
| budget unit | gate block recovery cycle |

## Archive

Archive / shutdown は明示 command とする。Lifecycle hook は session close side effect を持たない。
