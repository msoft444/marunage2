# 丸投げシステム (Maru-nage v2) フェーズ7 DinD Sandbox 破壊的テスト設計書

## 0. 目的

本書は、Phase 7（DinD Sandbox Setup）の設計を論理的に破壊し、RED テストで保証すべき異常系と合格基準を定義する。対象設計書は `docs/phase7-dind-sandbox-design.md`。

## 1. テスト方針

1. DinD コンテナのライフサイクル（生成→readiness wait→runtime→cleanup）各段階で異常が発生した場合に、安全側（task `blocked` / `failed`）へ確実に倒れることを最優先で保証する。
2. ホスト Docker ソケットや secrets パスが DinD コンテナへ露出しないことを保証し、DinD 導入がホスト保護を弱めないことを検証する。
3. task 間の DinD コンテナ名・network 名・proxy ポートの衝突が発生しないことを保証する。
4. UID/GID 不整合による生成ファイルの cleanup 失敗・後続読み取り失敗を検出可能にする。
5. CPU / Memory / PIDs / ディスクのリソース制限が未設定のまま DinD が起動することを防ぎ、ホスト全体への連鎖を阻止する。
6. task 終了時の cleanup（compose down → DinD 破棄 → network 削除 → port release）が確実に実行されることを保証し、残留リソースの蓄積を防ぐ。
7. worker 再起動時に残留 DinD コンテナ・network を再検出し、cleanup / release 可能であることを検証する。
8. trusted_dind validation が DinD 内閉域で必要な差分だけを緩和し、それ以外のホスト危険設定は strict と同等に拒否することを保証する。
9. 全 DinD イベントが `logs.details_json` に構造化記録され、障害箇所を task detail から特定できることを保証する。
10. DinD 起動失敗・trusted validation block・cleanup failure が orchestration 配下の root task に正しく伝播することを検証する。

## 2. 破壊シナリオと合格基準

### SDT-01: DinD コンテナ生成失敗（Docker API エラー）

- 攻撃シナリオ: Docker daemon のリソース不足やイメージ不在により、DinD コンテナの `docker create` / `docker run` が失敗する。
- Red 条件: エラーが握りつぶされ、DinD コンテナが存在しないまま compose 実行へ進む。
- Green 条件: DinD コンテナ生成失敗を捕捉し、task を `blocked` に遷移させる。`dind_container_start_requested` と `dind_container_start_failed` をログに記録する。worker は次 task を処理可能であること。§2.2 item 3 が遵守されること。

### SDT-02: dockerd readiness タイムアウト

- 攻撃シナリオ: DinD コンテナは起動したが、内部 `dockerd` が利用可能にならないまま readiness ポーリングが上限回数に達する。
- Red 条件: 無限ポーリングにより worker lease を永続的に占有し、他 task がブロックされる。
- Green 条件: readiness ポーリングに有限回数・有限時間の上限を設け、超過時は task を `blocked` に遷移させる。`dind_dockerd_wait_started` と `dind_dockerd_wait_timeout` をログに記録し、DinD コンテナ自体も cleanup する。§1 要件 4 / §1.1 NF-9 が遵守されること。

### SDT-03: dockerd readiness 完了前に compose 実行へ進行

- 攻撃シナリオ: readiness チェックの結果を無視して、DinD 内 `dockerd` が未起動のまま compose build / up を開始する。
- Red 条件: `dockerd` 未起動で `docker compose` が失敗し、エラー原因が「DinD 未 ready」ではなく「compose 実行失敗」として記録される。根本原因の特定が困難になる。
- Green 条件: readiness 完了を確認するまで compose 実行へ進まないこと。readiness チェックが false のまま compose を開始するコードパスが存在しないこと。§1 要件 4 が遵守されること。

### SDT-04: DinD コンテナが runtime 中に予期せず終了

- 攻撃シナリオ: compose build / up 実行中に DinD コンテナ自体が OOM kill やカーネルエラーで停止する。
- Red 条件: compose 実行のタイムアウト待ちが無限に続くか、エラーが「compose 失敗」としてのみ記録され DinD 停止が判別できない。
- Green 条件: DinD コンテナの生存確認を行い、予期せず停止した場合は task を `failed` に遷移させる。`details_json` に `dind_container_exited_unexpectedly` を記録し、DinD 停止と compose 実行失敗を区別できること。

### SDT-05: ホスト Docker ソケットの DinD マウント防止

- 攻撃シナリオ: DinD コンテナの起動パラメータに `/var/run/docker.sock:/var/run/docker.sock` が含まれる。DinD 内からホスト Docker daemon を直接操作可能になる。
- Red 条件: ホスト Docker ソケットが DinD コンテナにマウントされ、DinD 内部からホストコンテナの操作が可能になる。
- Green 条件: DinD manager がホスト Docker ソケットを DinD コンテナにマウントしないこと。mount パラメータ生成時に `/var/run/docker.sock` などの Docker ソケットパスを明示的に除外するガードを持つこと。§1 要件 3 が遵守されること。

### SDT-06: ホスト危険パスの DinD マウント防止

- 攻撃シナリオ: DinD コンテナに `/etc`、`/var`、`/dev`、`/sys`、`/proc`、`/` などのホスト危険パスがマウントされる。
- Red 条件: ホスト危険パスが DinD コンテナにマウントされ、DinD 内部からホストファイルシステムへの読み書きが可能になる。
- Green 条件: DinD manager がマウント対象を `WorkspaceSandbox` で containment 判定し、許可パス（repo root、runtime scratch、artifact 出力先）以外を拒否すること。§1 要件 3 / §2.4 が遵守されること。

### SDT-07: secrets パスの DinD マウント防止

- 攻撃シナリオ: DinD コンテナのマウントリストに `/run/secrets/` や本系の `secrets/` ディレクトリが含まれる。
- Red 条件: 本系の DB パスワードや GitHub token が DinD 内部から読み取り可能になる。
- Green 条件: DinD manager が secrets パスを mount 対象から除外すること。§2.4「ホスト外 path や secrets path を追加マウントしてはならない」が遵守されること。

### SDT-08: DinD 内部からホスト Docker daemon への到達

- 攻撃シナリオ: DinD コンテナ内で `DOCKER_HOST=tcp://host.docker.internal:2375` などを設定し、ホスト Docker daemon に接続を試みる。
- Red 条件: DinD 内部からホスト Docker daemon へ接続でき、ホストコンテナの操作が可能になる。
- Green 条件: DinD コンテナの network 設定でホスト Docker daemon のポートへの到達を遮断すること。§2.7「DinD 自身のホスト向き公開面は予約済み proxy ポートに限定する」が遵守されること。

### SDT-09: DinD コンテナ名の task 間衝突

- 攻撃シナリオ: task_id=42 と task_id=42（再利用または ID 衝突）から同名の DinD コンテナ `mn2-task-42-dind` の生成が試みられる。
- Red 条件: 既存コンテナとの名前衝突で `docker create` が失敗するか、先行 task の DinD コンテナが後続 task に再利用される。
- Green 条件: DinD manager が `mn2-task-{task_id}` 接頭辞で一意なコンテナ名を生成し、同名コンテナが既に存在する場合は先に cleanup してから生成するか、エラーとして task を `blocked` にすること。§2.3 / §1.1 NF-7 が遵守されること。

### SDT-10: DinD network 名の task 間衝突

- 攻撃シナリオ: 複数 task が同名の bridge network `mn2-task-net-42` を生成しようとする。
- Red 条件: Docker network 名の衝突で後続 task の DinD 起動が失敗するか、他 task の network に接続される。
- Green 条件: task ごとに一意な network 名を生成し、衝突を防止すること。既存同名 network が検出された場合の処理方針（cleanup or block）が明確であること。§1.1 NF-2 が遵守されること。

### SDT-11: runtime_spec_json への DinD metadata 未記録

- 攻撃シナリオ: DinD コンテナを起動した後、`tasks.runtime_spec_json` に `dind.container_name`、`dind.network_name`、`dind.proxy_ports` などが記録されない。
- Red 条件: task detail やクリーンアップ処理から DinD 資産を特定できず、残留リソースの追跡が不可能になる。
- Green 条件: DinD 起動直後に `runtime_spec_json` へ少なくとも `dind.enabled`、`dind.container_name`、`dind.network_name`、`dind.proxy_ports`、`dind.runtime_root`、`dind.validation_profile` を書き込むこと。§2.3 が遵守されること。

### SDT-12: DinD 命名規則の決定性

- 攻撃シナリオ: 同一 task_id に対して DinD コンテナ名や network 名がランダムに生成され、再実行時に追跡不能になる。
- Red 条件: DinD 資産名が task_id から導出できず、cleanup や障害調査で該当リソースの特定に手動調査が必要になる。
- Green 条件: 同一 task_id に対して container 名、network 名、runtime root が規則的に導出されること。§1.1 NF-7 決定性が遵守されること。

### SDT-13: UID/GID 不整合による生成ファイルの cleanup 失敗

- 攻撃シナリオ: DinD コンテナ内で root (UID=0) として生成されたファイルが `/workspace/{task_id}/` に書き込まれる。ホスト側の worker プロセス（非 root）が cleanup 時にそのファイルを削除できない。
- Red 条件: permission denied で cleanup が失敗し、workspace ディスクが蓄積的に消費される。後続 task が同 workspace を再利用する際にも権限エラーが発生する。
- Green 条件: DinD manager が UID/GID 戦略を固定し、生成ファイルのホスト側 cleanup が可能な権限を維持すること。cleanup 失敗時は `dind_container_cleanup_failed` に `permission_denied` を含めてログに記録すること。§1 要件 11 / §2.4 が遵守されること。

### SDT-14: DinD 内生成ファイルの後続読み取り失敗

- 攻撃シナリオ: DinD 内で生成された artifact（パッチ、ログ、compose override）が、ホスト側の worker プロセスから読み取れない権限で作成される。
- Red 条件: ホスト側から artifact が参照できず、task result の集約や audit 向け出力が不完全になる。
- Green 条件: DinD 内で生成されるすべてのファイルがホスト側 worker から読み取り可能であること。§2.4「cleanup 側でホストから再取得可能な権限を維持する」が遵守されること。

### SDT-15: mount パスが `/workspace/{task_id}` 外を参照

- 攻撃シナリオ: DinD manager のマウント指定に `/workspace/9999/` のような他 task の workspace パスが含まれる。
- Red 条件: 他 task の workspace が DinD 内部から参照可能になり、task 間のデータ漏洩が発生する。
- Green 条件: DinD manager が `WorkspaceSandbox` により mount パスを当該 task の workspace 配下に限定し、他 task のパスを拒否すること。§2.4 が遵守されること。

### SDT-16: proxy ポート枯渇（port_allocator 割り当て不能）

- 攻撃シナリオ: `port_allocator` のポートプール上限に達し、新規 task の proxy ポートを予約できない。
- Red 条件: ポート予約失敗が無視され、ポートなしで DinD が起動する。Dashboard からのアクセス経路が確立できないまま task が `running` を維持する。
- Green 条件: ポート予約失敗時は DinD 起動へ進まず、task を `blocked` に遷移させること。`dind_proxy_port_exhausted` をログに記録すること。§1 要件 6 / §2.5 が遵守されること。

### SDT-17: task 終了時に proxy ポートが解放されない

- 攻撃シナリオ: task が完了したが、`port_allocator` からのポート解放が漏れる。繰り返し実行するとポートプールが枯渇する。
- Red 条件: task 終了後も proxy ポートが予約状態のまま残り、port_allocator のプールが漸減する。
- Green 条件: task 終了時の cleanup で `port_allocator` からのポート解放が確実に実行されること。cleanup の成否にかかわらず port release が試行されること。§2.2 item 6 が遵守されること。

### SDT-18: DinD 内部ポートのホストへの直接露出

- 攻撃シナリオ: DinD 起動パラメータで `-p 80:80` のように検証対象アプリのポートがホストに直接 bind される。他 task や本系プロセスとのポート衝突が発生する。
- Red 条件: DinD 内部のアプリ用ポートがホスト側で直接公開され、proxy 経由以外でアクセス可能になる。
- Green 条件: DinD コンテナのホスト公開ポートは `port_allocator` で予約された proxy ポートのみに限定されること。検証対象アプリのポートは DinD 内部空間で完結すること。§2.5 が遵守されること。

### SDT-19: proxy ポート予約結果が runtime_spec_json に未記録

- 攻撃シナリオ: `port_allocator` でポートを予約したが、`runtime_spec_json.dind.proxy_ports` への記録が漏れる。
- Red 条件: Dashboard や後続処理から予約済み proxy ポートを特定できず、ヘルスチェックやアクセス導線が確立できない。
- Green 条件: proxy ポート予約直後に `runtime_spec_json.dind.proxy_ports` へ記録すること。§2.3 / §1 要件 12 が遵守されること。

### SDT-20: strict validation が DinD 起動前にバイパスされる

- 攻撃シナリオ: DinD manager が strict compose validation の結果を確認せずに DinD コンテナを起動する。悪意ある compose 定義がホスト上の Docker API 操作を誘発する前段階で検出されない。
- Red 条件: strict validation を通過していない compose が DinD 起動のトリガーとなる。
- Green 条件: DinD 起動は strict validation 通過後にのみ実行されること。§2.2 item 1-2 / §2.6 の 2 段階分離が遵守されること。

### SDT-21: trusted_dind validation で repo 外パスが許可される

- 攻撃シナリオ: trusted_dind profile の緩和が過剰で、`../../etc/shadow` のような repo 外パスが trusted_dind でも許可される。
- Red 条件: repo 外 path traversal が trusted_dind profile で検出されず、DinD 内から sandbox 外のファイルにアクセス可能になる。
- Green 条件: trusted_dind profile でも repo 外 / runtime root 外パスの path traversal は strict 同等に拒否すること。§1 要件 8 / §2.6「trusted_dind でも禁止のまま」が遵守されること。

### SDT-22: trusted_dind validation で external network/volume が許可される

- 攻撃シナリオ: trusted_dind profile で `networks.production_net.external: true` が許可される。
- Red 条件: DinD 内 compose がホスト側の外部 network に参加し、本系コンテナへの到達経路が生まれる。
- Green 条件: trusted_dind でも `external: true` な network / volume は拒否されること。§1 要件 8 / §2.6 が遵守されること。

### SDT-23: trusted_dind validation でホスト危険 mount が許可される

- 攻撃シナリオ: trusted_dind profile で `/etc:/mnt/etc` のようなホスト危険 mount が許可される。
- Red 条件: DinD 内 compose がホスト危険パスをマウントし、ホスト側ファイルへの読み書きが可能になる。
- Green 条件: trusted_dind でも `/`、`/etc`、`/var`、`/dev`、`/sys`、`/proc` のマウントは拒否されること。§1 要件 8 / §2.6 が遵守されること。

### SDT-24: trusted_dind validation で未許可デバイスが許可される

- 攻撃シナリオ: trusted_dind profile で `devices: ["/dev/sda:/dev/sda"]` が許可される。
- Red 条件: DinD 内 compose がホストデバイスに直接アクセス可能になる。
- Green 条件: trusted_dind でも未許可デバイス露出は拒否されること。§1 要件 8 / §2.6 が遵守されること。

### SDT-25: trusted_dind で `privileged: true` が DinD 基盤コンテナ以外にも許可される

- 攻撃シナリオ: trusted_dind profile で、DinD 基盤コンテナ以外の任意サービスにも `privileged: true` が許可される。検証対象の任意コンテナが特権で起動される。
- Red 条件: DinD 基盤以外のサービスが `privileged: true` で起動し、DinD 内のカーネルリソースに無制限アクセスが可能になる。
- Green 条件: trusted_dind での `privileged: true` 許容は DinD 内部運用に必要な最小限のコンテキストに制限されること。§2.6「DinD 内 Docker 運用に本当に必要な差分だけ」が遵守されること。

### SDT-26: validation profile パラメータが validator に渡されない

- 攻撃シナリオ: DinD manager が trusted_dind validation を実施する際に、validator にプロファイル指定を渡さず、デフォルトの strict profile で検査される。DinD 内で必要な `privileged: true` が strict で拒否され、正当な compose が blocked になる。
- Red 条件: DinD 内の正当な compose が strict profile で拒否され、runtime フェーズに進めない。
- Green 条件: validator がプロファイルパラメータ（`strict` / `trusted_dind`）を受け取り、DinD 内再検査では trusted_dind を使用すること。§2.6「validation は 2 段階」が遵守されること。

### SDT-27: task 間 DinD の network 到達性

- 攻撃シナリオ: task_id=42 の DinD コンテナと task_id=43 の DinD コンテナが同一 Docker network 上にあり、互いのサービスへ通信可能になる。
- Red 条件: 異なる task の DinD コンテナ同士が相互にネットワーク到達可能で、データ漏洩やサービス干渉が発生する。
- Green 条件: task ごとに専用の bridge network を生成し、他 task の DinD container 名解決ができないこと。§1 要件 10 / §2.7 が遵守されること。

### SDT-28: DinD から本系コンテナ（MariaDB / Dashboard / Guardian）への無制限到達

- 攻撃シナリオ: DinD コンテナが本系の MariaDB や Dashboard のポートに直接接続し、DB 操作や管理 API 呼び出しが可能になる。
- Red 条件: DinD 内部から本系 MariaDB への直接接続で DB データの読み書きが可能になる。
- Green 条件: DinD の network 設定で本系コンテナへの到達を明示的に制限すること。必要最小限の通信経路以外は遮断すること。§2.7「明示的に必要な最小経路だけを許可」が遵守されること。

### SDT-29: CPU 制限なしの DinD コンテナ

- 攻撃シナリオ: DinD コンテナが CPU 制限なしで起動され、compose build が CPU を 100% 消費し、他 task や本系プロセスが応答不能になる。
- Red 条件: DinD コンテナの CPU 暴走がホスト全体のパフォーマンスを劣化させ、他 task の処理や Dashboard の応答が著しく遅延する。
- Green 条件: DinD コンテナに CPU 制限が設定されること。制限値は `runtime_spec_json` から追跡可能であること。§1 要件 9 / §2.8 が遵守されること。

### SDT-30: Memory 制限なしの DinD コンテナ（OOM ホスト連鎖）

- 攻撃シナリオ: DinD コンテナがメモリ制限なしで起動され、compose build で大量メモリを消費し、ホストがスワップに入るか OOM killer が本系プロセスを殺す。
- Red 条件: ホストの OOM killer が本系コンテナ（MariaDB、worker）を停止させ、全 task がダウンする。
- Green 条件: DinD コンテナに memory 制限が設定されること。OOM 発生時は DinD コンテナが停止し、task を `failed` に遷移させること。`details_json` に OOM による障害種別を記録すること。§1 要件 9 / §1.1 NF-10 / §2.8 が遵守されること。

### SDT-31: PIDs 制限なしによる fork bomb

- 攻撃シナリオ: DinD 内の compose サービスが fork bomb を実行し、PIDs 制限がないためホスト全体のプロセステーブルが枯渇する。
- Red 条件: DinD 内の fork bomb がホスト全体に影響し、新規プロセスが生成不能になる。
- Green 条件: DinD コンテナに PIDs 制限が設定されること。PIDs 上限到達時は DinD 内で閉じ込められ、ホストに影響しないこと。§1 要件 9 / §2.8 が遵守されること。

### SDT-32: DinD 内ディスク消費によるホストディスク枯渇

- 攻撃シナリオ: DinD 内の compose build で巨大な Docker image を生成し、DinD の書き込み領域がホストディスクを圧迫する。
- Red 条件: ホストディスクが枯渇し、本系 MariaDB のデータ書き込みや他 task のファイル操作が失敗する。
- Green 条件: DinD コンテナの書き込み領域に上限を設定するか、ディスク使用量を監視して閾値超過時に task を `blocked` / `failed` にすること。§1 要件 9 / §2.8 が遵守されること。

### SDT-33: task 終了時に DinD コンテナが破棄されない

- 攻撃シナリオ: task が `succeeded` で終了したが、DinD コンテナの `docker rm` が漏れる。繰り返し実行するとホスト上に停止コンテナが蓄積し、ディスクとメモリを圧迫する。
- Red 条件: task 終了後も DinD コンテナが残留し、`docker ps -a` で蓄積的に増加する。
- Green 条件: task 終了時に status を問わず（`succeeded` / `failed` / `blocked` / `cancelled`）DinD コンテナが確実に削除されること。§1 要件 2 が遵守されること。

### SDT-34: task 終了時に DinD network が削除されない

- 攻撃シナリオ: task 終了時に DinD コンテナは削除されたが、専用 bridge network の削除が漏れる。
- Red 条件: 残留 network が蓄積し、Docker の network 上限に達して後続 task の DinD 起動が失敗する。
- Green 条件: cleanup 処理で DinD コンテナ削除後に関連 network も削除すること。§2.2 item 6 が遵守されること。

### SDT-35: compose down 未実行のまま DinD コンテナを破棄

- 攻撃シナリオ: DinD 内で `docker compose down` を実行せずに DinD コンテナ自体を `docker rm -f` で強制削除する。DinD 内部の子コンテナやボリュームが孤立する。
- Red 条件: DinD 内部の子コンテナやボリュームがホスト上で孤立リソースとして残留する。
- Green 条件: cleanup は「DinD 内 compose down」→「DinD コンテナ削除」の順で実行すること。compose down が失敗した場合も DinD コンテナ削除は続行すること。§2.2 item 6 / §3.3 が遵守されること。

### SDT-36: cleanup 失敗が後続 task をブロックしない

- 攻撃シナリオ: DinD コンテナの cleanup で `docker rm` が失敗（ネットワークエラー等）し、例外が worker まで伝播して worker が停止する。
- Red 条件: cleanup 失敗で worker プロセスが停止し、後続 task が処理不能になる。
- Green 条件: cleanup 失敗は `dind_container_cleanup_failed` としてログに記録されるが、worker 自体は停止せず次 task を処理可能であること。§1.1 NF-4 が遵守されること。

### SDT-37: worker 再起動時の残留 DinD コンテナ再検出

- 攻撃シナリオ: worker がクラッシュし、実行中だった task の DinD コンテナと port reservation が残留する。worker 再起動後、残留リソースが検出されない。
- Red 条件: 残留 DinD コンテナが永続的に稼働し続け、ポートプールが減少し、ホストリソースを消費し続ける。
- Green 条件: worker 再起動時に `mn2-task-*` 接頭辞の残留コンテナ / network を再検出し、cleanup / port release の方針を決められること。§1.1 NF-4 が遵守されること。

### SDT-38: `dind_container_started` ログイベント欠落

- 攻撃シナリオ: DinD コンテナは正常に起動したが、`dind_container_started` イベントが `logs` に記録されない。
- Red 条件: task detail から DinD 起動の成否を追跡できない。障害調査時に DinD が起動されたかどうかが判別不能。
- Green 条件: DinD コンテナ起動成功直後に `dind_container_started` を `logs.details_json` に記録すること。§1 要件 12 / §1.1 NF-3 / §3.2 が遵守されること。

### SDT-39: `dind_dockerd_ready` ログイベント欠落

- 攻撃シナリオ: dockerd readiness 確認が完了したが、`dind_dockerd_ready` イベントが記録されない。
- Red 条件: readiness 完了のタイムスタンプが追跡できず、DinD 起動から readiness までの所要時間を分析できない。
- Green 条件: dockerd readiness 確認完了時に `dind_dockerd_ready` を記録すること。§3.2 が遵守されること。

### SDT-40: `dind_container_cleanup_finished` ログイベント欠落

- 攻撃シナリオ: cleanup が正常に完了したが、`dind_container_cleanup_finished` イベントが記録されない。
- Red 条件: cleanup 完了の監査証跡が欠落し、「cleanup が実行されたのか」の判別が不可能。
- Green 条件: cleanup 完了時に `dind_container_cleanup_finished` を記録し、cleanup 所要時間と結果を追跡可能にすること。§3.2 が遵守されること。

### SDT-41: cleanup 失敗時の `dind_container_cleanup_failed` ログ未記録

- 攻撃シナリオ: cleanup で例外が発生するが、`dind_container_cleanup_failed` が記録されずにエラーが握りつぶされる。
- Red 条件: cleanup 失敗の証跡がなく、残留リソースの原因調査が不可能。
- Green 条件: cleanup 失敗時に `dind_container_cleanup_failed` を記録し、失敗原因（例外メッセージ）を `details_json` に含めること。§3.2 が遵守されること。

### SDT-42: DinD 起動失敗が orchestration root task に伝播しない

- 攻撃シナリオ: orchestration 配下の phase task で DinD 起動が失敗し、phase task は `blocked` になるが、root task への `blocked` 伝播が行われない。
- Red 条件: phase task が `blocked` なのに root task が `running` のまま残り、UI 上は進行中に見える。
- Green 条件: DinD 起動失敗で phase task が `blocked` になった場合、既存の blocked 伝播方針に従って root task にも `blocked` が伝播すること。§2.9 が遵守されること。

### SDT-43: trusted validation block が orchestration root task に伝播しない

- 攻撃シナリオ: DinD 内での trusted_dind validation で phase task が `blocked` になるが、root task への伝播が漏れる。
- Red 条件: trusted validation block が root task に反映されず、root task が `running` を維持する。
- Green 条件: trusted validation block も既存の blocked 伝播方針に従って root task に反映されること。§2.9 が遵守されること。

### SDT-44: 非 runtime フェーズで DinD が誤って起動される

- 攻撃シナリオ: Phase 0（repo clone）や Phase 1（copilot 実行）のような非 runtime フェーズで DinD manager が呼び出される。
- Red 条件: repo clone 段階で不要な DinD コンテナが起動され、リソースが浪費される。
- Green 条件: DinD manager の呼び出しは runtime 実行フェーズでのみ行われること。非 runtime フェーズでは DinD 起動が行われないこと。§2.9「DinD は runtime 実行フェーズでのみ使用する」が遵守されること。

### SDT-45: local workspace_path task での後方互換

- 攻撃シナリオ: orchestration を持たない local workspace_path task が実行される際に、DinD manager が介入しようとして例外をスローする。
- Red 条件: DinD 導入により既存の local task フローが壊れ、worker が例外で停止する。
- Green 条件: local workspace_path task では DinD manager が介入せず、従来のフローがそのまま動作すること。§1.1 NF-8 が遵守されること。

## 3. RED で確認すべき項目

### 3.1 ライフサイクル異常（SDT-01〜04）

1. DinD コンテナ生成失敗で `blocked` に遷移し、worker が停止しないこと。
2. dockerd readiness タイムアウトで `blocked` に遷移し、DinD コンテナが cleanup されること。
3. dockerd readiness 完了前に compose 実行へ進まないこと。
4. DinD コンテナの予期せぬ停止を検知して `failed` に遷移すること。

### 3.2 ホスト保護（SDT-05〜08）

5. ホスト Docker ソケットが DinD コンテナにマウントされないこと。
6. ホスト危険パス（`/`、`/etc`、`/var`、`/dev`、`/sys`、`/proc`）が DinD にマウントされないこと。
7. secrets パスが DinD にマウントされないこと。
8. DinD 内部からホスト Docker daemon への到達が遮断されること。

### 3.3 命名規則と metadata（SDT-09〜12）

9. DinD コンテナ名が task 間で衝突しないこと。
10. DinD network 名が task 間で衝突しないこと。
11. `runtime_spec_json` に DinD metadata が記録されること。
12. 同一 task_id から DinD 資産名が決定的に導出されること。

### 3.4 マウントと権限（SDT-13〜15）

13. UID/GID 不整合による cleanup 失敗がログに記録されること。
14. DinD 内生成ファイルがホスト側から読み取り可能であること。
15. 他 task の workspace が DinD にマウントされないこと。

### 3.5 ポート管理（SDT-16〜19）

16. proxy ポート枯渇時に `blocked` に遷移すること。
17. task 終了時に proxy ポートが解放されること。
18. DinD 内部ポートがホストに直接露出しないこと。
19. proxy ポート予約結果が `runtime_spec_json` に記録されること。

### 3.6 Compose Validation 統合（SDT-20〜26）

20. strict validation 通過前に DinD が起動しないこと。
21. trusted_dind で repo 外パスが拒否されること。
22. trusted_dind で external network / volume が拒否されること。
23. trusted_dind でホスト危険 mount が拒否されること。
24. trusted_dind で未許可デバイスが拒否されること。
25. trusted_dind での `privileged: true` 許容が最小限に制限されること。
26. validator にプロファイルパラメータが正しく渡されること。

### 3.7 ネットワーク隔離（SDT-27〜28）

27. 異なる task の DinD 間で network 到達性がないこと。
28. DinD から本系コンテナへの無制限到達が遮断されること。

### 3.8 リソース制限（SDT-29〜32）

29. DinD コンテナに CPU 制限が設定されること。
30. DinD コンテナに memory 制限が設定されること（OOM 時のホスト連鎖防止）。
31. DinD コンテナに PIDs 制限が設定されること。
32. DinD 内ディスク消費にホスト保護策があること。

### 3.9 cleanup（SDT-33〜37）

33. task 終了時に DinD コンテナが確実に破棄されること。
34. task 終了時に DinD network が削除されること。
35. cleanup が「compose down → DinD 削除」の順で実行されること。
36. cleanup 失敗が worker を停止させないこと。
37. worker 再起動時に残留 DinD が再検出されること。

### 3.10 ログイベント（SDT-38〜41）

38. `dind_container_started` が記録されること。
39. `dind_dockerd_ready` が記録されること。
40. `dind_container_cleanup_finished` が記録されること。
41. `dind_container_cleanup_failed` が cleanup 失敗時に記録されること。

### 3.11 worker 統合（SDT-42〜45）

42. DinD 起動失敗が orchestration root task に `blocked` 伝播すること。
43. trusted validation block が root task に伝播すること。
44. 非 runtime フェーズで DinD が起動しないこと。
45. local workspace_path task で DinD が介入しないこと。

## 4. 設計フィードバック

### SDT-01〜04 ライフサイクル

- DinD コンテナ生成・readiness wait・runtime 中停止の 3 段階でエラーハンドリングを明確に分離すること。
- readiness ポーリングにはリトライ上限とタイムアウトを設け、失敗時に DinD コンテナ自体を cleanup すること。
- DinD コンテナ生存監視はオプションとし、初期スライスでは compose 実行結果で判定してもよい。

### SDT-05〜08 ホスト保護

- DinD manager の mount 生成に許可リスト方式を採用し、デフォルト禁止とすること。
- mount 候補はすべて `WorkspaceSandbox.contains()` で containment 検証すること。
- Docker ソケットパスと secrets パスは明示的なブロックリストで二重防御すること。

### SDT-09〜12 命名・metadata

- `mn2-task-{task_id}` 接頭辞を一元管理する命名関数を DinD manager に用意し、container / network / runtime root の導出を集約すること。
- DinD 起動後の `runtime_spec_json` 更新をトランザクション内で行い、起動成功と metadata 記録の原子性を確保すること。

### SDT-13〜15 マウント・権限

- 初期スライスでは DinD 内実行ユーザーを固定し、ホスト workspace の UID/GID との整合を保つ戦略を採用すること。
- mount パスのバリデーションに `WorkspaceSandbox` を再利用し、他 task の workspace 参照を防止すること。

### SDT-16〜19 ポート管理

- ポート予約失敗は DinD 起動前に検出し、DinD を起動しない設計とすること。
- cleanup ではポート解放を最後に実行し、compose down / DinD 削除の失敗に関わらず解放を試行すること。
- proxy ポート予約と `runtime_spec_json` 記録を同一フローで行い、記録漏れを防止すること。

### SDT-20〜26 Compose Validation 統合

- ライフサイクル上、strict validation → ポート予約 → DinD 起動 → trusted_dind validation の順を厳守し、前段失敗時は後段をスキップすること。
- trusted_dind profile は strict profile の差分として実装し、緩和対象を最小限に限定すること。
- `privileged: true` の trusted_dind 許容は、DinD 基盤コンテナのサービス名やコンテキストで制限する設計とすること。

### SDT-27〜28 ネットワーク隔離

- task 専用 bridge network を `--internal` オプションなしで作成し、proxy ポートのみをホスト公開面として制御すること。
- 本系コンテナへの到達制限は Docker network 分離によるデフォルト遮断を活用し、明示的な iptables 操作は初期スライスでは避けること。

### SDT-29〜32 リソース制限

- CPU / memory / PIDs 制限の初期値は固定値とし、`runtime_spec_json` から追跡可能にすること。
- OOM / PIDs 上限到達時の障害種別を `details_json` に記録し、通常の compose 失敗と区別可能にすること。
- ディスク制限は初期スライスでは `--storage-opt` または tmpfs マウントサイズで対応し、完全な quota は後続で検討すること。

### SDT-33〜37 cleanup

- cleanup は `finally` ブロックで実行し、compose / DinD / network / port の 4 段階を個別にエラーハンドリングすること。
- 各段階の失敗は個別にログ記録し、先行段階の失敗で後続段階をスキップしないこと。
- worker 再起動時の残留検出は `mn2-task-*` 接頭辞のコンテナ / network リスト取得で実現すること。

### SDT-38〜41 ログイベント

- §3.2 定義の 11 イベントすべてに対応するテストを用意し、各イベントが `logs.details_json` に構造化記録されることを検証すること。
- ログ記録は各操作の直後に行い、後続処理の成否に影響されない設計とすること。

### SDT-42〜45 worker 統合

- DinD 起動・trusted validation の結果に応じた status 遷移は、既存の phase blocked 伝播方針を再利用し、DinD 固有の伝播ロジックを追加しないこと。
- DinD manager の呼び出し判断は task の phase と flow 情報から行い、DinD が不要なフェーズでは呼び出し自体を行わないこと。

## 5. 関連ドキュメント

1. `docs/phase7-dind-sandbox-design.md`
2. `docs/phase7-compose-validation-design.md`
3. `docs/phase7-compose-validation-destructive-test-design.md`
4. `docs/phase6-orchestration-design.md`
5. `docs/phase6-orchestration-destructive-test-design.md`
6. `.tdd_protocol.md`
