# 丸投げシステム (Maru-nage v2) フェーズ7 Compose Validation 設計書

## 0. 目的

本書は、Phase 7 において direct-edit 済みのリポジトリから生成アプリを起動する前に、`compose.yml` / `docker-compose.yml` 系定義と runtime override を安全に検査するための要件・基本設計を定義する。

本設計の目的は、Feature #7 の build / up 実行前に危険な compose 設定を確実に遮断し、ワークスペース逸脱、特権昇格、ホスト資源の露出、既存本番系との衝突を防ぐことである。永続的な設計判断は `docs/` 配下へ記録し、`.tdd_protocol.md` には実行中タスクの進捗のみを記録する。

## 1. 要件定義

1. validation 対象は、copilot CLI が直接編集した repo 内の compose 定義とし、artifact 生成物は対象にしないこと。
2. 初期スライスでは repo 直下の `compose.yml`、`compose.yaml`、`docker-compose.yml`、`docker-compose.yaml` を検査候補とし、Feature #6 導入後は `/workspace/{task_id}/runtime/compose.override.yml` などの runtime override を同一 validator に再投入できること。
3. compose ファイルが存在しない、YAML 構文が壊れている、または Compose モデルとして解釈できない場合は安全側で `blocked` に落とすこと。
4. 検査は Feature #7 の build / up 実行前、かつ Feature #6 のポート予約結果を反映した runtime override 適用前後の両方で再利用できること。
5. `privileged: true`、`network_mode: host`、`pid: host`、`ipc: host`、`docker.sock` の bind mount、ホストの `/`、`/etc`、`/var`、`/dev`、`/sys`、`/proc` を露出する bind mount は拒否すること。
6. `cap_add` による危険 capability 追加、`devices` によるホストデバイス露出、`external: true` な network / volume の利用要求は拒否すること。
7. bind mount のホスト側パス、`build.context`、`build.dockerfile`、`env_file`、`configs[].file`、`secrets[].file` などのファイル参照は、repo 直下または `/workspace/{task_id}/runtime/` 配下に正規化できる場合のみ許可すること。
8. relative path は repo root 基準で解決し、absolute path や `..` を含む path traversal、symlink によるワークスペース逸脱は拒否すること。
9. path 系フィールドに対する未解決の環境変数展開は原則拒否し、初期スライスで許可するのは Feature #6 が生成する runtime scratch を指す専用変数のみとすること。
10. base compose の `ports` は Feature #6 の上書き対象であり、Feature #5 はポート衝突解決を担当しない。ただし host namespace を使う設定や解釈不能な port 定義は拒否すること。
11. validation 失敗時は当該 task を `blocked` に遷移させ、違反ルール、対象 service、対象フィールド、正規化後 path などを `logs.details_json` に構造化して残すこと。
12. Phase 6 orchestration 配下の task で validation が失敗した場合は、既存の blocked 伝播方針に従って root task も `blocked` に遷移できること。
13. 初期スライスでは DB migration を追加せず、既存の `tasks.runtime_spec_json`、`port_allocator`、`logs`、`WorkspaceSandbox` を再利用すること。
14. validation 失敗時の `logs.details_json` は Dashboard UI がそのまま展開可能な粒度を持ち、少なくとも「どの compose ファイルの」「どの service / field が」「どの rule_id で」blocked されたかを Phase 0 の実行ログ上で説明できること。

## 1.1 非機能要件

1. 安全性: validator は「起動可能か」より「危険でないか」を優先し、解釈不能・未定義・あいまいな入力は許可しないこと。
2. 一貫性: repo compose 単体の検査と、Feature #6 が生成する runtime override との再検査で同一ルールセットを適用できること。
3. 可観測性: `compose_validation_started`、`compose_validation_blocked`、`compose_validation_passed` を `logs` に記録し、違反ルール ID を追跡可能にすること。
4. 保守性: compose の読み込み、path 正規化、禁止ルール判定、違反整形を `src/security/compose_validator.py` に集約し、`task_backend.py` や UI 側へ条件分岐を散在させないこと。
5. 互換性: 既存の `MariaDBTaskBackend.process_next_queued_task()`、`MariaDBAccessor`、`WorkspaceSandbox`、Phase 6 orchestration の blocked 伝播と整合すること。
6. 拡張性: Feature #6 の port override、Feature #7 の build / up、Feature #8 の health verification が validator の出力をそのまま利用できること。
7. 決定性: 同一 compose 入力に対して同一 violation 一覧を返し、task ごとの実行順や worker 再起動で判定結果が揺れないこと。
8. 性能: 初期スライスでは repo 直下 compose 群と runtime override のみを対象にし、大規模 repo 全走査を避けること。
9. 可理解性: validation による `blocked` は一般的な execution error と UI 上で区別でき、利用者が詳細ログを開けば違反ファイル・対象設定・適用ルールを追加調査なしで把握できること。

## 2. 基本設計

### 2.1 検査対象ファイル

- repo compose の候補は repo root にある次のファイルとする。
  - `compose.yml`
  - `compose.yaml`
  - `docker-compose.yml`
  - `docker-compose.yaml`
- 初期スライスでは上記候補のうち存在するファイルをすべて検査対象とする。複数存在する場合も安全側で全件を読み込み、少なくとも 1 件に違反があれば失敗とする。
- Feature #6 以降は runtime scratch 配下の override を明示的な追加入力として validator に渡す。想定配置は `/workspace/{task_id}/runtime/compose.override.yml` とする。
- compose 候補が 1 件も存在しない場合は Feature #7 へ進めないため validation failure とする。

### 2.2 validator の責務

- validator は YAML を Compose モデル相当の辞書へ読み込み、`services`、`networks`、`volumes`、関連 file path を静的に検査する。
- validator は Docker / Compose CLI を実行しない。build や up は Feature #7 の責務とし、Feature #5 は静的検査のみに限定する。
- validator の返却値は少なくとも以下を含む。
  - `compose_files`: 検査したファイル一覧
  - `violations`: ルール ID、service 名、field 名、message、normalized_path を持つ違反配列
  - `blocked`: 実行可否
  - `validated_runtime_root`: runtime scratch の正規化済みパス
- 失敗時は最初の違反で打ち切らず、可能な限り複数違反を集約して利用者と監査が一度で把握できるようにする。

### 2.3 パス解決方針

- repo 相対 path は clone 済み repo root を基準に正規化する。
- runtime scratch は `/workspace/{task_id}/runtime/` を唯一の追加許可領域とし、Feature #6 が生成する override や一時 env file はこの配下に限定する。
- absolute path は原則拒否する。例外は runtime scratch 自身を指す正規化済み path のみとする。
- `WorkspaceSandbox` 相当の path 正規化を reuse し、repo root / runtime root のいずれかに containment できない path は拒否する。
- symlink を経由した repo 外参照は containment 判定で拒否する。

### 2.4 環境変数展開ポリシー

- path 系フィールドに `${...}` が含まれる場合、初期スライスでは安全な専用変数だけを許可する。
  - 想定許可変数: `${TASK_RUNTIME_DIR}`、`${MN2_RUNTIME_DIR}`
- 上記以外の未解決変数、または repo root / runtime root へ一意に正規化できない変数参照は拒否する。
- `ports`、`environment` など path 以外の値は本 Feature では秘密情報スキャンや衝突検査の対象にせず、Feature #6 以降の責務とする。

### 2.5 禁止ルール

#### 2.5.1 特権・namespace 系

- `privileged: true`
- `network_mode: host`
- `pid: host`
- `ipc: host`
- `userns_mode: host` 相当の host namespace 利用

#### 2.5.2 ホスト資源露出系

- `docker.sock` の mount
- ホストの `/`, `/etc`, `/var`, `/dev`, `/sys`, `/proc` を含む bind mount
- repo root / runtime root の外を向く bind mount
- `devices` によるホストデバイス露出

#### 2.5.3 ネットワーク・volume 外部依存系

- `external: true` な network
- `external: true` な volume
- 既存本番 network 名やホスト固定 namespace へ依存する設定

#### 2.5.4 ファイル参照系

- repo 外 `build.context`
- repo 外 `build.dockerfile`
- repo 外 `env_file`
- repo 外 `configs[].file`
- repo 外 `secrets[].file`

#### 2.5.5 capability 系

- `cap_add` のうち、少なくとも `SYS_ADMIN`、`NET_ADMIN`、`SYS_PTRACE`、`DAC_READ_SEARCH`、`DAC_OVERRIDE` を初期拒否対象とする。

### 2.6 Feature #6 / #7 との責務分界

- Feature #5 は compose の静的安全性を判定する。
- Feature #6 は host port の予約、runtime override の生成、`TASK_RUNTIME_DIR` 相当の埋め込みを担当する。
- Feature #7 は validator を通過した compose files を使って build / up を行い、成功時の runtime metadata を `tasks.runtime_spec_json` に保存する。
- `tasks.runtime_spec_json` は validation 前提の実行メタデータ保存先であり、Feature #5 単体では DB に新しい永続フィールドを増やさない。
- `port_allocator` は Feature #6 で使用する既存テーブルであり、Feature #5 では「後段が利用可能な既存基盤」としてのみ参照する。

### 2.7 worker 統合位置

- validator の呼び出し位置は、repo clone / branch 準備が完了し repo root が確定した後、Feature #6 の port 予約前、Feature #7 の build / up 前とする。
- 実装上の最初の挿し込み候補は、現行 `MariaDBTaskBackend.process_next_queued_task()` における `repository_prepared` 後の runtime pipeline 分岐である。
- validation failure 時は task を `blocked` にし、`compose_validation_blocked` ログを挿入する。
- orchestration 配下の task では既存 blocked 伝播 (`handle_phase_blocked`) を再利用できる形にする。

## 3. 詳細設計メモ

### 3.1 既存資産の再利用

- `init.sql` と `docs/phase1-basic-design.md` ですでに `tasks.runtime_spec_json` と `port_allocator` テーブルが定義済みであるため、Feature #5 では migration を追加しない。
- `src/backend/database.py` の transaction / task status 更新 / log 挿入 API をそのまま使う。
- `src/security/sandbox.py` の workspace containment を path 検証に再利用する。
- 既存 tests では compose を文字列アサーションで検証するもの (`tests/test_phase3_container_auth.py`) があるが、compose の安全性を構造で検証するテストは未整備であるため、Feature #5 で新規追加する。
- 現在の依存関係には YAML パーサーが存在しないため、`pyproject.toml` へ `PyYAML` 追加が必要になる。

### 3.2 ログイベント

- 少なくとも以下の event を `logs` に記録する。
  - `compose_validation_started`
  - `compose_validation_blocked`
  - `compose_validation_passed`
- `details_json` には以下を持たせる。
  - `blocked_reason`: `compose_validation`
  - `blocked_reason_label`: `Compose Validation`
  - `violation_count`
  - `compose_files`
  - `compose_file`
  - `service`
  - `field`
  - `rule_id`
  - `normalized_path`
  - `raw_value`
  - `message`
- `compose_validation_blocked` の payload は UI が phase log 上で展開しやすいよう、単一 violation ではなく `violations[]` を正本とし、summary 文言だけに依存しない構造を維持する。
- Dashboard は `blocked_reason=compose_validation` または `violations[]` の存在により「想定された安全側 block」であることを明示表示できる前提とする。

## 4. 受け入れ条件

1. repo root に存在する compose 候補ファイルを検査対象として認識できること。
2. compose 候補が存在しない、または YAML 構文エラーである場合に task を `blocked` にできること。
3. `privileged: true`、`network_mode: host`、`pid: host`、`ipc: host` を含む compose を拒否できること。
4. `docker.sock` や `/etc` などの危険 bind mount を拒否できること。
5. repo 外 / runtime root 外の `build.context`、`env_file`、`secrets.file`、`configs.file` を拒否できること。
6. `external: true` の network / volume を拒否できること。
7. repo 内 relative path と runtime scratch 配下の安全な override だけは許可できること。
8. validation failure 時に task 状態、log event、違反詳細が一貫して記録されること。
9. Feature #6 が生成する runtime override に対しても同じ validator を再利用できる設計になっていること。
10. Feature #5 の導入にあたり DB migration を追加せず、既存 `runtime_spec_json`、`port_allocator`、`WorkspaceSandbox` を再利用できること。
11. Dashboard detail / Phase 0 実行ログで、`compose_validation_blocked` が通常エラーではなく安全上の block であること、および violation 一覧を人間が読める形で表示できる設計になっていること。

## 5. 関連ドキュメント

1. `docs/phase1-basic-design.md`
2. `docs/phase6-direct-edit-design.md`
3. `docs/phase6-orchestration-design.md`
4. `.todo/.tdd_protocol.5.md`
5. `.tdd_protocol.md`