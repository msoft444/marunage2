# 丸投げシステム (Maru-nage v2) フェーズ7 DinD Sandbox 設計書

## 0. 目的

本書は、Phase 7 において検証対象アプリの build / up をホスト Docker へ直接流さず、task 専用の DinD (Docker-in-Docker) コンテナ内へ隔離して実行するための要件・基本設計を定義する。

本設計の目的は、Feature #5 で確立した Compose Validation の安全性を維持しつつ、ホストポート衝突、特権コンテナ要求、Docker ソケット露出、他 task とのネットワーク干渉をタスク単位でカプセル化することである。永続的な設計判断は `docs/` 配下へ記録し、`.tdd_protocol.md` には実行中タスクの進捗のみを記録する。

## 1. 要件定義

1. 検証対象アプリの `docker compose build` / `up` はホスト Docker ではなく、task ごとに生成される専用 DinD コンテナ内で実行されること。
2. DinD コンテナは task 単位で 1 つ生成し、task 終了時には `succeeded` / `failed` / `blocked` / `cancelled` を問わず破棄されること。
3. DinD コンテナには少なくとも repo 作業領域、runtime scratch、必要最小限の一時出力領域のみをマウントし、ホストの `/var/run/docker.sock` を直接渡さないこと。
4. DinD コンテナ起動後、内部 `dockerd` が利用可能になるまで待機し、利用不能のまま compose 実行へ進まないこと。
5. DinD 内で起動される検証対象 compose のアプリ用ポート衝突は DinD 内部で閉じ、ホストへ露出するのは Dashboard から参照が必要な少数の proxy / ingress ポートに限定すること。
6. ホストへ露出するポートは既存 `port_allocator` により予約し、task 間競合を回避すること。
7. Feature #5 の Compose Validation は DinD 用の trusted 実行モードを持ち、DinD 内閉域で必要となる `privileged: true` や DinD 内部 Docker ソケット参照など、ホスト危険設定とは区別して評価できること。
8. trusted 実行モードでも、repo 外への path 逸脱、ホスト危険 mount、外部 network / volume 依存、未許可デバイス露出は禁止のまま維持すること。
9. DinD コンテナの CPU / Memory / PIDs / ディスク消費は task ごとに上限を設け、暴走時もホスト全体へ連鎖しないこと。
10. DinD コンテナのネットワークは task 専用に分離され、他 task の DinD や本系コンテナへ無制限に到達できないこと。
11. repo 作業領域を DinD へ渡す際、ホストと DinD 内の UID / GID またはファイル権限整合を維持し、生成ファイルの cleanup や後続読み取りで権限不整合を起こさないこと。
12. DinD 起動、待機、compose 実行、proxy ポート割り当て、終了 cleanup の全イベントは `logs.details_json` に構造化記録され、task detail から追跡できること。
13. 初期スライスでは DB migration を追加せず、既存 `tasks.runtime_spec_json`、`port_allocator`、`WorkspaceSandbox`、`logs` を再利用すること。

## 1.1 非機能要件

1. 安全性: DinD 導入後も「ホスト保護」を最優先とし、trusted 実行モードは DinD 内閉域で必要な差分だけを緩和すること。
2. 隔離性: task ごとに DinD コンテナ、内部 network、runtime metadata を分離し、他 task の compose 名、container 名、internal port 空間と衝突しないこと。
3. 可観測性: `dind_container_started`、`dind_dockerd_ready`、`dind_proxy_port_reserved`、`dind_container_cleanup_started`、`dind_container_cleanup_finished` などのイベントを記録し、失敗箇所を task detail だけで特定できること。
4. 回復性: worker 再起動や compose 実行失敗後も、残留 DinD コンテナと port reservation を再検出し cleanup / release 方針を決められること。
5. 保守性: DinD の起動・待機・停止・cleanup・resource 設定を `src/backend/dind_manager.py` 相当へ集約し、`task_backend.py` に Docker CLI 手順を分散させないこと。
6. 一貫性: strict compose validation と trusted compose validation は同一 validator 実装からプロファイル差分として導出し、重複実装しないこと。
7. 決定性: 同一 task 入力に対して DinD コンテナ名、network 名、runtime root、proxy ポート割り当て結果が規則的かつ追跡可能であること。
8. 互換性: 既存の `MariaDBTaskBackend.process_next_queued_task()`、`RepositoryWorkspaceManager`、Phase 6 orchestration、Dashboard detail API と整合すること。
9. 性能: DinD 起動待ちは有限回数・有限時間で失敗判定し、無限待機で worker lease を占有し続けないこと。
10. 操作性: Dashboard では「通常失敗」と「DinD 起動失敗」「DinD cleanup 失敗」「trusted validation による block」を区別して表示できること。

## 2. 基本設計

### 2.1 実行トポロジ

- ホスト上の Brain worker は repo clone / direct-edit / strict compose validation までを従来どおり担当する。
- Feature #6 導入後、runtime 実行フェーズへ進む task では、worker が task 専用 DinD コンテナを生成する。
- DinD コンテナ内では `dockerd` を起動し、compose build / up、proxy 用 ingress、health verification 対象コンテナ群を閉じた Docker namespace 上で実行する。
- Dashboard や後続ヘルス確認から見えるのは、ホスト側で予約した少数の proxy ポート経由の公開面だけとする。

### 2.2 DinD コンテナのライフサイクル

1. repo clone / branch 準備完了後、strict compose validation を実施する。
2. strict validation を通過した task に対して、`port_allocator` から proxy 用ホストポートを予約する。
3. worker は task 専用 DinD コンテナを生成する。
4. DinD コンテナ内 `dockerd` の readiness をポーリングし、利用可能になった後で trusted validation と runtime override 適用へ進む。
5. trusted validation を通過した compose を DinD 内で build / up する。
6. task 終了時は status を問わず DinD 内 compose down、DinD コンテナ削除、関連 network / volume cleanup、port release を実施する。

### 2.3 命名規則と runtime metadata

- task ごとの DinD 資産名は `mn2-task-{task_id}` を基本接頭辞とし、少なくとも以下を導出可能にする。
  - DinD container 名
  - DinD 内 proxy service 名
  - task 専用 bridge network 名
  - runtime scratch path
- `tasks.runtime_spec_json` には少なくとも以下を保存できる設計とする。
  - `dind.enabled`
  - `dind.container_name`
  - `dind.network_name`
  - `dind.proxy_ports`
  - `dind.runtime_root`
  - `dind.validation_profile`

### 2.4 マウントと権限整合

- DinD へ渡すホストパスは repo root、runtime scratch、必要最小限の artifact 出力先に限定する。
- `/workspace/{task_id}` を丸ごと writable で渡す場合でも、ホスト外 path や secrets path を追加マウントしてはならない。
- 生成ファイルの ownership 崩れを避けるため、初期スライスでは DinD コンテナ内の実行ユーザー戦略を固定し、cleanup 側でホストから再取得可能な権限を維持する。
- 設計上は UID / GID ずれ検知を DinD manager の責務に含め、後続実装では書き込みテストを伴う。

### 2.5 ポート公開方針

- 検証対象 compose が内部で公開するアプリ用ポートは DinD 内部空間で完結させる。
- ホスト側へ公開するのは、Dashboard からの参照またはヘルスチェックに必要な少数の proxy ポートのみとする。
- proxy ポートは `port_allocator` で予約し、予約結果を runtime override または DinD 起動パラメータへ反映する。
- base compose に host 固定 port が書かれていても、Feature #6 では DinD 内 proxy 方式へ正規化し、ホストへの直接 bind は採用しない。

### 2.6 Compose Validation との統合

- validation は 2 段階に分ける。
  - strict profile: DinD 起動前のホスト保護検査。
  - trusted_dind profile: DinD 内閉域でのみ必要な設定を限定緩和した再検査。
- trusted_dind profile で許容候補に入るのは、DinD 内 Docker 運用に本当に必要な差分だけとする。
  - `privileged: true` な DinD 基盤コンテナ
  - DinD コンテナ内部 namespace / socket を参照する内部構成
- ただし以下は trusted_dind でも禁止のままとする。
  - repo 外 / runtime root 外 path
  - ホスト危険 path (`/`, `/etc`, `/var`, `/dev`, `/sys`, `/proc`) の露出
  - `external: true` な network / volume
  - 未許可 device mount
  - task 外 network への越境を前提とした設定

### 2.7 ネットワーク隔離

- task ごとの DinD は専用 bridge network を持ち、他 task の DinD container 名解決や service 参照に依存しない。
- DinD 自身のホスト向き公開面は予約済み proxy ポートに限定する。
- 本系 MariaDB、Dashboard、Guardian などへの接続は明示的に必要な最小経路だけを許可し、compose 側から任意到達できる設計を避ける。

### 2.8 リソース制限

- DinD コンテナには CPU、memory、pids、書き込み領域の上限を設定する前提とする。
- 上限値は task 種別または既定 runtime profile から導出し、初期スライスでは固定値でもよいが `runtime_spec_json` から追跡可能とする。
- OOM やディスク枯渇時は task を `failed` または `blocked` とし、障害種別を `details_json` で区別する。

### 2.9 worker 統合位置

- DinD manager の呼び出し位置は、repo clone / strict compose validation 完了後、Feature #7 の build / up 実行前とする。
- orchestration 配下では Phase 0 〜 5 の direct-edit 自体はホスト側 repo 上で進め、DinD は runtime 実行フェーズでのみ使用する。
- DinD 起動失敗や trusted validation failure は、既存 blocked 伝播方針に従って root task へ反映できる形とする。

## 3. 詳細設計メモ

### 3.1 既存資産の再利用

- `tasks.runtime_spec_json` を DinD runtime metadata の保存先として再利用する。
- `port_allocator` を proxy / ingress 公開ポートの競合管理に再利用する。
- `WorkspaceSandbox` を repo root / runtime scratch containment 判定へ再利用する。
- `logs.details_json` を DinD 起動待ち、proxy ポート、cleanup 成否、validation profile の監査情報に再利用する。

### 3.2 想定ログイベント

- 少なくとも以下の event を `logs` に記録する。
  - `dind_container_start_requested`
  - `dind_container_started`
  - `dind_dockerd_wait_started`
  - `dind_dockerd_ready`
  - `dind_proxy_port_reserved`
  - `dind_trusted_validation_started`
  - `dind_trusted_validation_blocked`
  - `dind_runtime_started`
  - `dind_container_cleanup_started`
  - `dind_container_cleanup_finished`
  - `dind_container_cleanup_failed`

### 3.3 Feature #7 以降への受け渡し

- Feature #7 は DinD manager から受け取った Docker 接続情報と proxy ポート情報を使って build / up を実行する。
- Feature #8 のヘルスチェックはホストから DinD proxy ポートを叩く設計へ切り替える。
- Feature #10 の cleanup は「DinD 内 compose down」→「DinD コンテナ破棄」→「port release」の順で行う。

## 4. 受け入れ条件

1. task ごとに専用 DinD コンテナを起動し、task 終了時に確実に破棄できること。
2. DinD 内 `dockerd` の readiness 完了前に compose 実行へ進まないこと。
3. ホストへ公開するポートは予約済み proxy ポートに限定され、既存 `port_allocator` と競合しないこと。
4. strict validation と trusted_dind validation の責務差分が docs 上で明確に定義されていること。
5. trusted_dind でも repo 外 path、危険 host mount、外部 network / volume、未許可 device 露出は禁止のままであること。
6. DinD container / network / proxy port / runtime root の情報が `tasks.runtime_spec_json` と `logs.details_json` から追跡できること。
7. DinD 起動失敗、trusted validation block、cleanup failure を UI / 監査上で区別できること。
8. 初期スライスでは DB migration を追加せず、既存 `runtime_spec_json`、`port_allocator`、`WorkspaceSandbox`、`logs` を再利用すること。

## 5. 関連ドキュメント

1. `docs/phase1-basic-design.md`
2. `docs/phase6-direct-edit-design.md`
3. `docs/phase6-orchestration-design.md`
4. `docs/phase7-compose-validation-design.md`
5. `.todo/.tdd_protocol.6.md`
6. `.tdd_protocol.md`