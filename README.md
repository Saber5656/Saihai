# Saihai

Saihai は、AI エージェント組織の実行を「プロンプトの勢い」ではなく
typed artifact、明示承認、監査可能な state で制御するためのローカル
orchestrator / organization runtime リポジトリです。

旧 Agent-Teams-Viewer（ATV）由来のローカル状態ビューアは残っていますが、
現在の主軸は deterministic frontdoor、workflow-run、main-agent bridge、
typed report / evidence gate です。

## 現在の構成

| 領域 | 主なファイル | 役割 |
|---|---|---|
| Saihai operator CLI | `scripts/saihai.py` | `frontdoor` と `workflow` を分けた operator 向け CLI。提案・承認と workflow-run 操作を同じ namespace に混ぜない |
| Organization facade | `scripts/configure_organization.py` | 組織 mode 判定、runtime path 確認、workflow selector/frontdoor/server、既存 ITB 互換 command の入口 |
| Workflow contracts | `organization/runtime/workflows/` | schema、template、deterministic selector、frontdoor harness、HTTP bridge、durable run store、tests |
| Organization knowledge | `organization/settings.json`, `organization/policies/`, `organization/roles/`, `organization/runtime/` | 組織運用設定、Policy、Team Role 定義、runtime registry / model registry / team config の repo 内ミラー |
| Local status viewer | `server.py`, `static/index.html` | `~/.claude/state/itb` / `~/.codex/state/itb` の session、queue、report、role 状態を読むローカル read-only dashboard |
| Migration notes | `docs/issues/`, `organization/runtime/workflows/operator-runbook.md` | legacy queue/tmux から typed-run へ移行するための現状、制約、未実装範囲 |

依存は Python 3.10+ のみです。通常の利用に `pip install` は不要です。

## 実装済みの責務

| 機能 | 現在の仕様 |
|---|---|
| Prompt 分類 | `scripts/configure_organization.py classify` と `/api/decide` が `fast` / `strict` / `maintenance` を判定する。どの mode でも task record と Vault 記録は必要 |
| Workflow selection | `workflow_selector.py` が typed classification を deterministic に workflow template へ対応付ける。raw prompt は workflow 選択の authority ではない |
| Frontdoor proposal | prompt 起点の request は `proposed` または `waiting_human` まで。`propose` は approved artifact や workflow run を作らない |
| Approval | `approve` は proposal digest 由来の challenge id を要求する。approved envelope の `activation_source` は `human_ui` / `manual_cli` / `orchestrator-start` のみ。実行 principal は `human_operator` / `manual_operator` / `orchestrator_start` を許可し、Saihai CLI の `frontdoor approve` は `human_operator` / `human-ui` / `local_ui` を既定値にする |
| Workflow run | approved request から durable `runs/<run_id>.json` を作り、`drain` で bounded work order を作る |
| Report validation | typed report と normalized provider evidence が canonical result。stdout、provider transcript、tmux pane output は signal only |
| Main-agent bridge | main agent は request submit、redacted projection read、ack だけが可能。classification、approval、run 作成、adapter 準備、report path 指定は拒否される |
| Status viewer | ITB session state、queue inbox、reports、role metadata、organization settings を read-only に表示する |

## 明示的な非スコープ

| 非スコープ | 理由 |
|---|---|
| live provider runner | P0 harness は adapter request / report path / evidence path を作るだけ。provider 実行は外側の operator / runner フェーズ |
| tmux worker execution | `tmux_interactive` は compatibility model として残るが、この repo の P0 実行 path では使わない |
| daemon / LaunchAgent | 現在の scheduler は invocation-drain、durable state、concurrency 1 |
| commit / push / PR automation | publication は別 gate。P0 workflow は publish を直接実行しない |
| Viewer からの workflow 実行 | local status viewer は read-only。workflow control は CLI / frontdoor HTTP API の責務 |

## Saihai CLI

`scripts/saihai.py` は operator が使う主 CLI です。

```sh
python3 scripts/saihai.py --help
python3 scripts/saihai.py frontdoor --help
python3 scripts/saihai.py workflow --help
```

| Group | Commands | 境界 |
|---|---|---|
| `frontdoor` | `propose`, `approve`, `status` | activation artifact の提案・明示承認・read-only 状態確認。workflow run は作らない |
| `workflow` | `create-run`, `drain`, `validate-report` | approved artifact / run id / typed report を扱う。`--prompt` や `--classification` は受け取らない |

最小の operator flow:

```sh
STATE_ROOT=/tmp/frontdoor-state

python3 scripts/saihai.py frontdoor --state-root "$STATE_ROOT" propose \
  --task-id TSK-example \
  --request-id req-example \
  --prompt "Run a readonly external review." \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'

python3 scripts/saihai.py frontdoor --state-root "$STATE_ROOT" status \
  --request-id req-example

python3 scripts/saihai.py frontdoor --state-root "$STATE_ROOT" approve \
  --request-id req-example \
  --nonce <approval.human_action_id>

python3 scripts/saihai.py workflow --state-root "$STATE_ROOT" create-run \
  --request-id req-example

python3 scripts/saihai.py workflow --state-root "$STATE_ROOT" drain \
  --run-id <run_id>

# `drain` は work order を作るだけです。validation の前に full harness で
# adapter request を作り、provider が typed report / evidence を書く必要があります。
python3 scripts/configure_organization.py workflow-frontdoor --state-root "$STATE_ROOT" prepare-claude-adapter \
  --run-id <run_id>

# Provider は harness 外で実行し、adapter request に書かれた report / evidence path へ出力します。
python3 scripts/saihai.py workflow --state-root "$STATE_ROOT" validate-report \
  --run-id <run_id>
```

未実装 command を README に載せない方針です。現在 `run-step`、`resume`、
`abort`、`verify-completion`、`task-view`、`lock-status`、`list` は
`scripts/saihai.py` の parser には存在しません。

## Organization Facade

`scripts/configure_organization.py` は skills / automation / 既存 runtime のための
互換 facade です。Saihai CLI より広い surface を持ちます。

```sh
python3 scripts/configure_organization.py status
python3 scripts/configure_organization.py runtime-paths
python3 scripts/configure_organization.py classify --prompt "最近の天気予報を調べる"
AGENT_ORG_MAINTENANCE=1 python3 scripts/configure_organization.py classify --prompt "Hookを直す"
python3 scripts/configure_organization.py workflow-selector validate-contracts
```

主な command:

| Command | 用途 |
|---|---|
| `status` | `organization/settings.json`、role count、policy count、repo root を JSON で出す |
| `runtime-paths` | ITB runtime、workflow selector/frontdoor/server、Saihai CLI、registry mirror の存在確認 |
| `classify` | prompt を `fast` / `strict` / `maintenance` に分類する |
| `workflow-selector` | workflow contract validation、selection、activation envelope helper |
| `workflow-frontdoor` | frontdoor harness の full compatibility surface |
| `workflow-frontdoor-server` | Agent UI / bridge 用の localhost HTTP API |
| `itb`, `itd-monitor`, `agent-call`, `agent-switch`, `provider-failover` など | legacy / compatibility runtime の入口 |

## Frontdoor Harness

`workflow-frontdoor` は `frontdoor_orchestrator.py` に委譲し、Saihai CLI には出していない
adapter / bridge / token 操作も提供します。

```sh
python3 scripts/configure_organization.py workflow-frontdoor --help
```

実装済み command:

| Command | 用途 |
|---|---|
| `propose` | typed classification と bounded refs から proposed request を作る |
| `approve` | challenge id を検証して approved activation を記録する |
| `create-run` | approved request から workflow-run を作る |
| `drain` | run から work order を作る |
| `adapter-capability` | provider adapter capability descriptor を出す |
| `prepare-claude-adapter` | bounded Claude adapter request、prompt、report/evidence/transcript path を作る |
| `validate-report` | typed report と evidence を検証して run を terminal state へ進める |
| `bridge-submit-request` | main-agent bridge から request を submit する |
| `bridge-read-projection` | redacted orchestrator projection を読む |
| `bridge-ack-output` | projection digest 一致時だけ inert ack を記録する |
| `channel-token` | local HTTP channel token を生成する |

default state root は `~/.codex/state/itb/frontdoor-orchestrator` です。
検証時は `--state-root /tmp/frontdoor-state` のように disposable root を使ってください。

## Frontdoor HTTP API

HTTP wrapper は `127.0.0.1` bind を推奨します。

```sh
python3 scripts/configure_organization.py workflow-frontdoor-server \
  --state-root /tmp/frontdoor-state \
  --host 127.0.0.1 \
  --port 8766
```

| Endpoint | 役割 |
|---|---|
| `GET /` | main-agent output confirmation UI |
| `GET /healthz` | health check |
| `POST /main-agent/submit-request` | bridge request submit |
| `GET /main-agent/projections/{request_id}` | redacted projection read |
| `POST /main-agent/ack-output` | verified no-op acknowledgement |
| `POST /frontdoor/propose` | operator proposal |
| `POST /frontdoor/approve` | human UI approval |
| `POST /orchestrator/runs` | create workflow run |
| `POST /orchestrator/runs/{run_id}/drain` | drain run into work order |
| `POST /provider/claude/prepare` | prepare bounded Claude adapter request |
| `POST /provider/reports/validate` | validate typed provider report |

raw request / run read endpoints（`/frontdoor/requests/{request_id}` と
`/orchestrator/runs/{run_id}`）は現在 403 を返します。main-agent には
redacted projection を使い、operator は dedicated read API が実装されるまで
`--state-root` 配下の canonical artifacts を直接確認します。

channel token は次の command で作ります。

```sh
python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/frontdoor-state channel-token \
  --channel bridge
```

HTTP API は `X-Orchestrator-Channel` と `X-Orchestrator-Token` から principal を決めます。
request body の `principal_type`、`principal_id`、`authn_method` は authority として受け取りません。

## Local Status Viewer

旧 ATV dashboard は、現在も read-only viewer として利用できます。

```sh
python3 server.py
python3 server.py --port 8799
```

起動先は `http://127.0.0.1:8765/` です。`ThreadingHTTPServer` は
`127.0.0.1` に bind し、`Host` は `127.0.0.1` / `localhost` / `::1` のみ許可します。
認証はないため、リモート公開しないでください。

Viewer API:

| Endpoint | 説明 |
|---|---|
| `GET /api/sessions` | `~/.claude/state/itb` / `~/.codex/state/itb` から監視可能 session を列挙する |
| `GET /api/org?session=<id>` | team 別 role 状態、active task、busy count を返す |
| `GET /api/role?session=<id>&role=<role_id>` | role metadata、inbox、latest report、provider evidence を返す |
| `GET /api/config` | organization settings、role count、policy count、policy index を返す |
| `GET /api/decide?prompt=<text>` | prompt を `fast` / `strict` / `maintenance` に判定する |

Viewer の status 判定:

| status | 条件 |
|---|---|
| `working` | latest report が直近 120 秒以内、または report / provider evidence が進行中 |
| `processing` | queue inbox / report が `processing` / `running` / `invoked` |
| `pending` | queue inbox に `pending` message がある |
| `ready` | queue consumer role として待機可能 |
| `deferred` | lazy / on-call role で現タスク対象外 |
| `offline` | session metadata または context pointer が見つからない |

## Canonical Artifacts

| 種別 | パス | Authority |
|---|---|---|
| Organization settings | `organization/settings.json` | 組織 enabled / disabled / maintenance、fast / strict、Hook observer、provider transport policy |
| Policy mirror | `organization/policies/*.md`, `organization/policy-index.json` | repo 内 policy mirror と checksum index |
| Role mirror | `organization/roles/<role>/skill.md`, `organization/role-index.json` | Team Role 定義 mirror と checksum / team index |
| Runtime registry | `organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml`, `organization/runtime/role-agent-registry.yaml` | role registry。cleanup 完了までは両方が互換対象 |
| Workflow contracts | `organization/runtime/workflows/registry.yaml`, `templates/`, `schemas/` | deterministic workflow contract 正本 |
| Request record | `<state_root>/requests/<request_id>.json` | request、bounded refs、proposal、approval、bridge metadata |
| Workflow run | `<state_root>/runs/<run_id>.json` | durable run state、current step、terminal status、transition provenance |
| Work order | `<state_root>/work-orders/<run_id>/<step_id>.json` | bounded step instruction と canonical report path |
| Adapter request | `<state_root>/adapter-requests/<run_id>/<step_id>-claude_headless_p0.json` | provider prompt、evidence path、transcript path、authority boundary |
| Typed report | `<state_root>/reports/<run_id>/<step_id>-external-review-report.json` | P0 external review の canonical provider result |
| Provider evidence | `<state_root>/provider-evidence/<run_id>/*` | normalized evidence と signal-only transcript |
| Audit log | `<state_root>/audit/*.jsonl` | principal-scoped transition / replay / rejection / ack evidence |

## Workflow Contracts

`organization/runtime/workflows/` は typed orchestrator の contract surface です。

| Template | 用途 |
|---|---|
| `single_step_external_review` | readonly external review |
| `research_only` | no-diff research / design / source review |
| `standard_code_change` | publication なしの bounded code change |
| `publication_required` | publication gate が必要な code change / publication |
| `policy_or_permission_change` | policy、permission、hook、governance 影響のある変更 |
| `security_sensitive_change` | security review が必要な変更 |

注意: 現在 end-to-end に運用可能な P0 path は readonly external review が中心です。
code change / publication / policy / security workflow は contract と routing を持ちますが、
write / shell / commit / push / network / provider dispatch を LLM に直接渡す action gateway は
この README 時点では実装済みとして扱いません。

## セキュリティ境界

- raw prompt は workflow 選択や実行の authority ではありません。
- prompt 起点の activation は `proposed` / `waiting_human` までです。
- main-agent bridge は classification、approval、run id、report path、adapter request、
  workflow definition を authority として送れません。
- context refs は repo root 配下に解決され、symlink escape、`.git`、`.env*`、
  key / token / secret / credential 系 path、件数上限、byte 上限を拒否します。
- provider output は signal です。typed report と normalized evidence の検証結果だけが
  completion authority です。
- local viewer と HTTP API は localhost 前提です。画面共有時は prompt、evidence、
  internal path が映る可能性に注意してください。

## テスト

主要 regression test:

```sh
python3 tests/test_configure_organization.py
python3 tests/test_saihai_cli.py
python3 organization/runtime/workflows/tests/test_workflow_selector.py
python3 organization/runtime/workflows/tests/test_run_store.py
python3 organization/runtime/workflows/tests/test_frontdoor_orchestrator.py
```

contract validation だけを素早く見る場合:

```sh
python3 scripts/configure_organization.py workflow-selector validate-contracts
```

## 参考ドキュメント

| ドキュメント | 内容 |
|---|---|
| `organization/README.md` | organization knowledge mirror の layout と migration rule |
| `organization/runtime/workflows/README.md` | workflow contracts、CLI、HTTP API、bridge の詳細 |
| `organization/runtime/workflows/frontdoor-orchestrator-protocol.md` | authority boundary と protocol invariant |
| `organization/runtime/workflows/operator-runbook.md` | day-1 operator workflow、artifact inspection、legacy queue/tmux migration |
| `docs/issues/main-agent-output-confirmation-ui.md` | main-agent を output confirmation UI に制限するための実装記録 |
| `docs/issues/runtime-cleanup-obsolete-files.md` | legacy ITB / mirror cleanup 候補と移行前提 |

## ライセンス

MIT License（[LICENSE](LICENSE) を参照）。
