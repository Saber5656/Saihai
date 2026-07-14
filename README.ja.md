# Sahai

[English](README.md) | [日本語](README.ja.md)

Sahai は、プロンプトを実行権限として扱うのではなく、typed artifact、明示的な承認、durable state、監査可能な evidence を通じて AI エージェントの作業を実行するためのローカル orchestrator / organization runtime リポジトリです。

このリポジトリには Agent-Teams-Viewer（ATV）由来のローカル status viewer も残っていますが、現在の主要な product surface は deterministic frontdoor、durable workflow run、制約された main-agent bridge、typed report / evidence gate です。pre-release の記録では、historical artifact や compatibility alias を指す際に ATV の名称が残っている場合があります。

通常の利用では Python 3.10+ の標準ライブラリだけを使用します。`pip install` は不要です。

## 必要環境

- Python 3.10 以上
- live scoped-worker backend を有効にする場合は Git 2.37 以上
- primary checkout から設定された、書き込み可能な Agents Vault
- operator が live provider を明示的に有効化する場合のみ provider CLI と credential。offline path では不要

<a id="local-environment"></a>

## ローカル環境

ローカル path は primary checkout の untracked な `directory-path.env` に設定します。shell profile へ追加したり、このファイルを commit したりしないでください。linked worktree は primary checkout の catalog を再利用します。

```sh
python3 scripts/setup_directory_paths.py --help
# Supply all nine required directory options, then validate the catalog.
python3 scripts/setup_directory_paths.py --check
```

setup command は非破壊的で、owner-only file を書き込みます。process environment の値は、明示的に空にされた値も含めて catalog より優先されます。解決規則と復旧方法は [Local environment configuration](docs/configuration.md)、path audit の全体は [Directory path variable inventory](docs/environment-variable-inventory.md) を参照してください。

## リポジトリ構成

| 領域 | 主なファイル | 責務 |
|---|---|---|
| Operator CLI | `scripts/saihai.py` | frontdoor の提案・承認と workflow-run 実行を分離する |
| Organization facade | `scripts/configure_organization.py` | organization mode、runtime path、workflow selector/frontdoor/server、validation、legacy ITB compatibility command |
| Workflow runtime | `organization/runtime/workflows/` | schema、template、deterministic selector、frontdoor harness、HTTP bridge、durable run state、provider adapter、test |
| Organization knowledge | `organization/settings.json`, `organization/policies/`, `organization/roles/`, `organization/runtime/` | organization settings、policy、team role、runtime registry、model registry、team configuration の repository mirror |
| Local status viewer | `server.py`, `static/index.html` | ITB session、queue、report、role、workflow run の read-only dashboard |
| Migration guidance | `docs/issues/`, `organization/runtime/workflows/operator-runbook.md` | legacy queue/tmux の前提から typed workflow run への移行 |

## 実装済みの挙動

| 機能 | 現在の挙動 |
|---|---|
| Prompt classification | `scripts/configure_organization.py classify` と `/api/decide` が作業を `fast`、`strict`、`maintenance` に分類する。どの mode でも適用される task record と Vault record が必要 |
| Workflow selection | `workflow_selector.py` が typed classification を active workflow template へ deterministic に対応付ける。raw prompt は selection authority にならない |
| Frontdoor proposal | prompt 起点の request は `proposed` または `waiting_human` まで進むか、`blocked` として fail closed する。`propose` は approved activation を生成せず、workflow run も作成しない |
| Approval | `approve` は proposal digest から導出した challenge を検証する。許可される activation source は `human_ui`、`manual_cli`、`orchestrator-start`、trusted execution principal は `human_operator`、`manual_operator`、`orchestrator_start`。narrow CLI の default は `human_operator` / `human-ui` / `local_ui` |
| Workflow runs | approved request から durable な `runs/<run_id>.json` を作り、`drain` が bounded かつ immutable な work order を生成する |
| Recovery | compatibility harness が durable state に対する typed `resume`、`abort`、`task-view`、`lock-status` 操作を提供する |
| Provider runner | `run-provider` が deterministic fake provider または pinned `claude_headless_p0` / `codex_cli_openai_p0` live adapter を dispatch し、runner-owned typed report、normalized evidence、confined transcript を書き込む |
| Report and completion gates | typed report と normalized provider evidence が canonical。`verify-completion` が terminal artifact を別途検証し、thin Vault evidence block を生成する |
| Main-agent bridge | main agent は request の submit、redacted projection の read、output の acknowledge が可能。authoritative classification、approval、run 作成、adapter preparation、report path は指定できない |
| Child-thread action gateway | `child-thread-create` が検証済みの issue-scoped child-worktree plan と結果を記録する。main-agent projection には redacted summary だけが含まれる |
| Scoped worker executor | host が approved work order から capability を導出し、task worktree で pinned Codex CLI worker を実行できる。capability の発行と実行は credential-bound な `action_gateway` channel に限定される |
| Status viewer | local dashboard が ITB session、queue、report、role metadata、organization settings、workflow run、lock state を runtime state の変更なしに読み取る |

## 明示的な非スコープ

| 非スコープ | 境界 |
|---|---|
| Provider credential provisioning | operator が credential を手動で作成・設定する。runner は credential 値、任意 argv、shell、model、cwd、endpoint override を受け取らない |
| tmux worker execution | `tmux_interactive` は compatibility model として残るが、P0 execution path では使わない |
| Daemon / LaunchAgent scheduling | scheduler は invocation-drain、durable state、global concurrency 1 |
| Implicit commit, push, or PR automation | publication は別 gate。通常の P0 workflow は変更を直接 publish しない |
| Status viewer からの workflow control | viewer は read-only。workflow control は operator CLI または authenticated frontdoor HTTP API の責務 |

## Offline quickstart

次の flow は deterministic fake provider を使用し、live provider call を行わないため、local checkout で安全に実行できます。先に[ローカル環境](#local-environment)の設定を完了してください。

```sh
suffix="$(date +%s)"
request_id="req-readme-smoke-$suffix"
run_id="run-readme-smoke-$suffix"

python3 scripts/saihai.py frontdoor propose \
  --task-id TSK-readme-smoke \
  --request-id "$request_id" \
  --prompt "Run a readonly external review." \
  --ref organization/runtime/workflows/README.md \
  --classification '{"classification_version":"1","classification_source":"deterministic_fixture","classification_confidence":1.0,"classification_evidence":["operator-reviewed-context"],"task_kind":"external_review","permission_required":"readonly","external_provider_required":true,"publication_required":false,"security_sensitive":false,"destructive_operation":false,"context_scope":"refs_only","expected_artifacts":["typed_report"]}'

python3 scripts/saihai.py frontdoor status --request-id "$request_id"

nonce="$(
  python3 scripts/saihai.py frontdoor status --request-id "$request_id" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["request"]["approval"]["human_action_id"])'
)"

python3 scripts/saihai.py frontdoor approve \
  --request-id "$request_id" \
  --nonce "$nonce"

python3 scripts/saihai.py workflow create-run \
  --request-id "$request_id" \
  --run-id "$run_id"

python3 scripts/saihai.py workflow drain --run-id "$run_id"

python3 scripts/saihai.py workflow run-provider \
  --run-id "$run_id" \
  --adapter-id claude_headless_p0 \
  --fake-provider-mode success

python3 scripts/configure_organization.py workflow-frontdoor \
  verify-completion --run-id "$run_id"
```

最後の command は typed completion decision を返す必要があります。blocked state、artifact inspection、recovery、migration、rollback の詳しい背景は [operator runbook](organization/runtime/workflows/operator-runbook.md) を参照してください。この README の command list は現在の executable surface と照合済みです。

## Operator CLI

`scripts/saihai.py` は operator 向けの narrow CLI です。

```sh
python3 scripts/saihai.py --help
python3 scripts/saihai.py frontdoor --help
python3 scripts/saihai.py workflow --help
```

| Group | Commands | Authority boundary |
|---|---|---|
| `frontdoor` | `propose`, `approve`, `status` | activation artifact の提案・明示承認と request state の read。workflow run は作成しない |
| `workflow` | `create-run`, `drain`, `run-provider`, `validate-report` | approved request artifact、run ID、work order、typed report を操作する。raw prompt や classification は受け取らない |

recovery と inspection の command は narrow CLI へ重複実装せず、より広い compatibility harness から提供します。

```sh
python3 scripts/configure_organization.py workflow-frontdoor resume \
  --run-id <run_id> --requeue
python3 scripts/configure_organization.py workflow-frontdoor abort \
  --run-id <run_id> --reason "operator cancelled"
python3 scripts/configure_organization.py workflow-frontdoor task-view \
  --task-id <task_id>
python3 scripts/configure_organization.py workflow-frontdoor lock-status
```

`run-step`、`resume`、`abort`、`verify-completion`、`task-view`、`lock-status`、`list` は `scripts/saihai.py` の subcommand ではありません。実装済みの recovery、verification、inspection command には compatibility harness を使用してください。

## Live provider adapter

live readonly execution には `--live` と正確な environment guard の両方が必要です。provider authentication とすべての host binding は operator が手動で設定します。

```sh
SAIHAI_ALLOW_LIVE_PROVIDERS=1 python3 scripts/saihai.py workflow run-provider \
  --run-id <run_id> \
  --adapter-id claude_headless_p0 \
  --live \
  --timeout-seconds 1800

SAIHAI_ALLOW_LIVE_PROVIDERS=1 python3 scripts/saihai.py workflow run-provider \
  --run-id <run_id> \
  --adapter-id codex_cli_openai_p0 \
  --live \
  --timeout-seconds 1800
```

live command boundary は host-owned です。

| Adapter | Mechanical boundary |
|---|---|
| `claude_headless_p0` | `SAIHAI_CLAUDE_EXECUTABLE_PATH` と対応する SHA-256 変数で pin した absolute executable が必要。runner は owner、mode、digest を再検証し、`--print --output-format json` と plan/safe mode を使用し、tool、slash command、MCP、session persistence を無効化する |
| `codex_cli_openai_p0` | pinned executable に加えて、host-owned confinement wrapper/profile の path と digest が必要。isolated cwd、`exec --ephemeral --json`、approval `never`、read-only sandbox を使用し、user rule、configuration、shell environment を継承しない。confinement binding が欠けると fail closed する |

host-binding variable は path と digest だけを保持し、credential 値は保持しません。Codex では以下の binding がすべて必要です。

- `SAIHAI_CODEX_EXECUTABLE_{PATH,SHA256}`
- `SAIHAI_CODEX_CONFINEMENT_WRAPPER_{PATH,SHA256}`
- `SAIHAI_CODEX_CONFINEMENT_PROFILE_{PATH,SHA256}`

caller は argv、shell、cwd、model、provider endpoint、output path を選択できません。provider call の前に runner は signed work order、iteration-frozen snapshot、run/request/step binding、context-file の size/digest、adapter-request digest、lease、pinned executable を再検証します。

live context は20 files、1 file あたり256 KB、合計1 MBまでです。canonical inline JSON として渡すため、provider は repository-read authority を受け取りません。stdout/stderr の合計は4 MiBで打ち切られ、owner-only `0700` directory と `0600` transcript にだけ保存されます。`stdout_sha256` は raw stdout、`transcript_sha256` は transcript JSON 全体を対象にします。

provider CLI の1回の実行は既定30分で、1秒から24時間まで設定できます。harness 全体には累積 wall-clock timeout を設けません。durable claim は30秒ごとに heartbeat され、provider subprocess の実行中は global workflow lock を保持しません。attempt journal と retry counter は再起動後も残り、同じ障害は初回実行後に最大5回再試行され、その後 `waiting_human` へ移ります。host または process の再起動後は `resume` または再度の `run-provider` で継続できます。

## Organization facade と frontdoor harness

`scripts/configure_organization.py` は skill、automation、既存 runtime が利用する compatibility facade です。

```sh
python3 scripts/configure_organization.py status
python3 scripts/configure_organization.py runtime-paths
python3 scripts/configure_organization.py classify --prompt "Review the latest forecast"
AGENT_ORG_MAINTENANCE=1 python3 scripts/configure_organization.py classify --prompt "Repair a hook"
python3 scripts/configure_organization.py validate-all
python3 scripts/configure_organization.py workflow-selector validate-contracts
python3 scripts/configure_organization.py workflow-frontdoor --help
```

| Command | 用途 |
|---|---|
| `status` | organization settings、role/policy count、repository root を JSON で出力する |
| `runtime-paths` | ITB runtime、workflow selector/frontdoor/server、operator CLI、registry mirror を検証する |
| `classify` | prompt を `fast`、`strict`、`maintenance` に分類する |
| `validate-all` | offline suite、contract validation、Python compile check を実行する |
| `workflow-selector` | workflow contract を検証し、deterministic selection と activation-envelope operation を行う |
| `workflow-frontdoor` | host-owned な frontdoor / recovery surface 全体を提供する |
| `workflow-frontdoor-server` | localhost frontdoor HTTP API を起動する |
| `itb`, `itd-monitor`, `agent-call`, `agent-surfaces`, `agent-switch`, `provider-failover`, `transport-status` | legacy / compatibility runtime entry point を維持する |

frontdoor harness は現在、以下を実装しています。

| Command | 用途 |
|---|---|
| `propose`, `approve`, `orchestrator-start-approve`, `manual-approve` | trusted channel を通じて bounded activation artifact を作成・明示承認する |
| `create-run`, `drain` | durable run と immutable work order を作成する |
| `resume`, `abort` | durable な non-terminal run を復旧または終了する |
| `adapter-capability` | provider adapter capability descriptor を出力する |
| `prepare-claude-adapter` | deprecated かつ non-executable な compatibility artifact を作成する。live execution は `run-provider --live` に集約されている |
| `run-provider`, `validate-report` | bounded fake adapter または pinned readonly adapter を実行し、runner-owned artifact を report gate に渡す |
| `verify-completion` | terminal typed artifact を検証し、thin final-evidence decision を生成する |
| `task-view`, `lock-status` | task-linked run evidence と global lock state を読み取る |
| `bridge-submit-request`, `bridge-read-projection`, `bridge-ack-output` | 制約された main-agent bridge を操作する |
| `child-thread-create` | action gateway を通じて検証済み child-thread plan と結果を記録する |
| `channel-token` | owner-only な local HTTP channel-token file を作成する |

default orchestrator state root は `~/.codex/state/itb/frontdoor-orchestrator` です。別の場所を使う場合は、primary checkout の owner-only（`0600`）な `directory-path.env` に `SAIHAI_ORCH_STATE_ROOT` を設定します。catalog は current user が所有する regular file で、state root は検証済みの absolute path である必要があります。この security-sensitive key は process environment から override できません。

linked worktree は host-managed primary checkout `~/dev/Saihai` だけを参照し、Git metadata や fallback path から catalog を再探索しません。`--state-root` は設定済み canonical root の確認に使われ、任意の場所を選択することはできません。

### Scoped worker backend

live scoped-worker backend は、host operator が以下の asset を手動設定するまで fail closed します。Sahai は key や credential を生成しません。

| Environment variable | 用途 |
|---|---|
| `SAIHAI_SCOPED_EXECUTOR_KEY_FILE` | regular、non-symlink、`0600` file に保存された32 bytes以上の capability HMAC key |
| `SAIHAI_SCOPED_WORKTREE_ROOT` | host が task/run-bound worktree path を導出する canonical root |
| `SAIHAI_SCOPED_REPO_ROOT` | host-owned absolute repository path。default は Sahai repository root |
| `SAIHAI_SCOPED_CODEX_EXECUTABLE` | digest が work order と capability に bind される absolute pinned Codex CLI path。group/world-writable binary は拒否される |
| `SAIHAI_SCOPED_CODEX_HOME` | dedicated worker runtime/auth root。main-agent profile は継承しない |
| `SAIHAI_ENABLE_SCOPED_WORKER_LIVE=1` | 明示的な live-execution gate。未設定時は deterministic fake harness だけが利用可能 |

初期 v1 が機械的に受理する scope は task worktree 全体だけです。subpath grant、commit、push、PR publication、worker-tool network、任意 provider は fail closed します。固定された Codex model control plane は host transport であり、worker tool への network/provider grant ではありません。capability の発行と実行は CLI subcommand ではなく、credential-bound な `action_gateway` HTTP channel からだけ利用できます。

## Offline validation

すべての offline suite を1つの command で実行できます。

```sh
python3 scripts/validate_all.py
```

organization facade からも同じ validation を実行できます。

```sh
python3 scripts/configure_organization.py validate-all
```

harness は標準ライブラリの self-runner test、workflow contract validation、Python source compile を実行し、最後に1行の JSON summary を出力します。child process は `SAIHAI_ALLOW_LIVE_PROVIDERS` を空にするため、validation は live provider token や network access に依存しません。adapter test は recorded fixture と patched subprocess/binary discovery だけを使用します。

contract だけを素早く確認する場合:

```sh
python3 scripts/configure_organization.py workflow-selector validate-contracts
```

## Frontdoor HTTP API

authenticated local API を loopback で起動します。

```sh
python3 scripts/configure_organization.py workflow-frontdoor-server \
  --host 127.0.0.1 \
  --port 8766
```

| Endpoint | 用途 |
|---|---|
| `GET /` | main-agent output-confirmation UI |
| `GET /healthz` | health check |
| `POST /main-agent/submit-request` | bridge request を submit する |
| `GET /main-agent/projections/{request_id}` | redacted bridge projection を読み取る |
| `POST /main-agent/ack-output` | verified かつ inert な acknowledgement を記録する |
| `POST /action-gateway/child-thread-create` | validated child-thread plan/result を記録する。`action_gateway` 専用 |
| `POST /action-gateway/scoped-worker-capabilities` | frozen work order から capability を導出する。body は `run_id` と `step_id` だけ。`action_gateway` 専用 |
| `POST /action-gateway/scoped-worker-execute` | capability を消費して pinned worker を起動する。body は `capability_id` だけ。`action_gateway` 専用 |
| `POST /frontdoor/propose` | operator proposal を作成する |
| `POST /frontdoor/approve` | human-UI approval を記録する |
| `POST /orchestrator/runs` | workflow run を作成する |
| `POST /orchestrator/runs/{run_id}/drain` | run を work order へ drain する |
| `POST /orchestrator/runs/{run_id}/resume` | durable run を resume または requeue する |
| `POST /orchestrator/runs/{run_id}/abort` | operator reason を付けて non-terminal run を abort する |
| `GET /orchestrator/runs/{run_id}/verify-completion` | operator または harness principal として terminal artifact を検証する |
| `GET /orchestrator/tasks/{task_id}/runs` | operator として thin task-linked run / evidence view を読み取る |
| `POST /provider/claude/prepare` | bounded compatibility adapter request を作成する |
| `POST /provider/reports/validate` | harness principal として typed provider report を検証する |

`/frontdoor/requests/{request_id}` と `/orchestrator/runs/{run_id}` での raw request/run read は `403` を返します。main agent は redacted projection を使い、operator は dedicated task/completion view または設定済み state root 配下の canonical artifact を確認します。

local channel token は次の command で作成します。

```sh
python3 scripts/configure_organization.py workflow-frontdoor channel-token \
  --channel bridge
```

command が出力するのは owner-only token file の path だけです。operator は対象 client のために token を明示的に読み取り、設定する必要があります。API は `X-Orchestrator-Channel` と `X-Orchestrator-Token` から principal を導出し、request body の `principal_type`、`principal_id`、`authn_method` を authority として受け取ることを拒否します。

## Local status viewer

ATV 由来の dashboard は local read-only viewer として利用できます。

```sh
python3 server.py
python3 server.py --port 8799
```

default URL は `http://127.0.0.1:8765/` です。server は loopback に bind し、Host 値は `127.0.0.1`、`localhost`、`::1` だけを受け付けます。authentication はないため、remote へ公開しないでください。

ITB session discovery は `~/.claude/state/itb` と `~/.codex/state/itb` を読み取ります。workflow-run discovery は各 root の `frontdoor-orchestrator` child を default とし、viewer は process-level の `SAIHAI_ORCH_STATE_ROOT` も追加で読み取れます。この viewer-only discovery は execution authority ではありません。host workflow CLI は primary checkout catalog から読み込んだ canonical root だけを受け付けます。

### Viewer API

| Endpoint | Response |
|---|---|
| `GET /api/sessions` | `~/.claude/state/itb` と `~/.codex/state/itb` 配下の観測可能な session |
| `GET /api/org?session=<id>` | team role state、active task、busy count |
| `GET /api/role?session=<id>&role=<role_id>` | role metadata、inbox、latest report、provider evidence |
| `GET /api/config` | organization settings と role/policy index |
| `GET /api/decide?prompt=<text>` | `fast`、`strict`、`maintenance` classification |
| `GET /api/workflow-runs?session=<id>&task=<id>&state=<state>` | thin read-only workflow-run summary |
| `GET /api/workflow-run?session=<id>&run=<id>` | work order、report、provider evidence、transition metadata |
| `GET /api/workflow-lock` | 設定済み orchestrator root ごとの global workflow-lock status |

UI には organization-control summary、既存の team board、Workflow Runs panel があります。Workflow Runs panel は state badge、stale-lock banner、read-only の work-order/report/evidence/transition detail を表示します。provider の起動、configuration の変更、workflow state の mutation は行いません。

viewer の role state は次のように導出されます。

| State | Condition |
|---|---|
| `working` | latest report が120秒以内、または report/provider evidence が進行中 |
| `processing` | queue inbox または report が `processing`、`running`、`invoked` |
| `pending` | queue inbox に pending message がある |
| `ready` | queue-consumer role が利用可能 |
| `deferred` | lazy/on-call role が現在の task 対象外 |
| `offline` | session metadata または context pointer が見つからない |

## Canonical artifact

| Artifact | Path | Authority |
|---|---|---|
| Organization settings | `organization/settings.json` | organization mode、strict/fast behavior、hook observer、provider-transport policy |
| Policy mirror | `organization/policies/*.md`, `organization/policy-index.json` | repository policy mirror と checksum index |
| Role mirror | `organization/roles/<role>/skill.md`, `organization/role-index.json` | team-role definition と checksum/team index |
| Runtime registry | `organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml`, `organization/runtime/role-agent-registry.yaml` | cleanup 完了まで互換性を維持する role-registry path |
| Workflow contracts | `organization/runtime/workflows/registry.yaml`, `templates/`, `schemas/` | deterministic workflow contract source |
| Request record | `<state_root>/requests/<request_id>.json` | request、bounded ref、proposal、approval、bridge metadata |
| Workflow run | `<state_root>/runs/<run_id>.json` | durable run state、current step、terminal status、transition provenance |
| Work order | `<state_root>/work-orders/<run_id>/<step_id>.json` | bounded instruction と canonical report path |
| Adapter request | `<state_root>/adapter-requests/<run_id>/<step_id>-<adapter_id>.json` | `claude_headless_p0` や `codex_cli_openai_p0` などの adapter に対する provider prompt、evidence/transcript path、authority boundary |
| Typed report | `<state_root>/reports/<run_id>/<step_id>-external-review-report.json` | canonical P0 external-review result |
| Provider evidence | `<state_root>/provider-evidence/<run_id>/*` | normalized evidence と signal-only transcript |
| Session run index | `<session_dir>/orchestrator-runs.json` | rebuild 可能な viewer projection。canonical run state ではない |
| Task view | `workflow-frontdoor task-view` / `GET /orchestrator/tasks/{task_id}/runs` | derived thin link/status と queue-shaped evidence |
| Role queue files | `<session_dir>/queue/inbox`, `<session_dir>/queue/tasks`, `<session_dir>/queue/reports` | canonical ITB role-queue evidence。orchestrator は書き込まない |
| Audit log | `<state_root>/audit/*.jsonl` | principal-scoped transition、replay、rejection、acknowledgement evidence |

## Workflow contract

| Template | 用途 |
|---|---|
| `single_step_external_review` | read-only external review |
| `research_only` | no-diff research、design、source review |
| `standard_code_change` | publication を伴わない bounded code change |
| `publication_required` | separate publication gate が必要な code change または publication |
| `policy_or_permission_change` | policy、permission、hook、governance の変更 |
| `security_sensitive_change` | 明示的な security review と risk evidence が必要な変更 |

readonly external-review path が主要な end-to-end P0 path です。他の template にも active contract と deterministic routing がありますが、それによって LLM に直接 write、shell、commit、push、network、provider authority が付与されることはありません。これらの effect は明示的な host-owned gate の背後に残ります。

## Security boundary

- raw prompt は workflow-selection / execution authority ではない
- prompt 起点の activation は `proposed` または `waiting_human` まで進むか、`blocked` として fail closed する
- main-agent bridge は authoritative classification、approval、run ID、report path、adapter request、workflow definition を指定できない
- context ref は repository root 配下に解決される必要がある。symlink escape、`.git`、`.env*`、key/token/secret/credential path、count/byte limit 違反は拒否される
- provider output は signal。検証済み typed report と normalized evidence だけが completion を authorize できる
- local viewer は機械的に loopback-only。frontdoor HTTP API は default が loopback であり、そのまま維持すべきだが、`--host` option は remote bind を機械的には防がない。screen sharing では prompt、evidence、internal path が見える可能性がある

## Focused test

上記の one-command validation が正本です。個別に確認する際は、次の entry point を利用できます。

```sh
python3 tests/test_configure_organization.py
python3 tests/test_saihai_cli.py
python3 organization/runtime/workflows/tests/test_workflow_selector.py
python3 organization/runtime/workflows/tests/test_run_store.py
python3 organization/runtime/workflows/tests/test_task_state_bridge.py
python3 organization/runtime/workflows/tests/test_frontdoor_orchestrator.py
```

## 関連ドキュメント

| Document | 内容 |
|---|---|
| [Organization layout](organization/README.md) | organization knowledge-mirror の構成と migration rule |
| [Workflow runtime](organization/runtime/workflows/README.md) | contract、CLI、HTTP API、bridge、runner、state behavior の詳細 |
| [Frontdoor protocol](organization/runtime/workflows/frontdoor-orchestrator-protocol.md) | authority boundary と protocol invariant |
| [Operator runbook](organization/runtime/workflows/operator-runbook.md) | day-one operation、blocked-state recovery、artifact inspection、legacy migration、rollback |
| [Main-agent output UI](docs/issues/main-agent-output-confirmation-ui.md) | main agent を output confirmation に制限する実装記録 |
| [Runtime cleanup](docs/issues/runtime-cleanup-obsolete-files.md) | legacy ITB / mirror cleanup 候補と migration prerequisite |

## ライセンス

MIT License。詳細は [LICENSE](LICENSE) を参照してください。
