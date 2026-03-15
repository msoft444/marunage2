# 丸投げシステム (Maru-nage v2) フェーズ7 Compose Validation 破壊的テスト設計書

## 0. 目的

本書は、Phase 7（Compose Validation）の設計を論理的に破壊し、RED テストで保証すべき異常系と合格基準を定義する。対象設計書は `docs/phase7-compose-validation-design.md`。

## 1. テスト方針

1. compose 定義を静的に検査するバリデータのすべての禁止ルールについて、設計§2.5 の各カテゴリを最低 1 シナリオで破壊し、拒否が確実に発動することを保証する。
2. YAML パース前処理（ファイル検出、読み込み、構文解析）が壊れた時に安全側（`blocked`）へ倒れることを最優先で保証する。
3. path 解決（§2.3）の全経路（relative、absolute、`..` traversal、symlink、env var 展開）で repo root / runtime root 外への逸脱が拒否されることを保証する。
4. 複数 compose ファイルが同時に存在する場合に「1 件でも違反があれば全体失敗」の安全側挙動を保証する。
5. validation 失敗時の `blocked` 遷移、log イベント記録、violation 構造体の整合が壊れた時に検出可能であることを保証する。
6. 異常は task 単位で閉じ込め、worker 全体を巻き込まないことを検証する。

## 2. 破壊シナリオと合格基準

### CVT-01: compose 候補ファイルが 1 件も存在しない

- 攻撃シナリオ: repo root に `compose.yml`、`compose.yaml`、`docker-compose.yml`、`docker-compose.yaml` のいずれも存在しない。
- Red 条件: validation が成功扱いになり、Feature #7 の build / up に進む。
- Green 条件: compose 候補が 0 件であることを検出し、`blocked` へ遷移する。`compose_validation_blocked` ログに `rule_id: no_compose_file` を記録する。§2.1「候補が 1 件も存在しない場合は validation failure」が遵守されること。

### CVT-02: compose ファイルの YAML 構文エラー

- 攻撃シナリオ: `compose.yml` に不正な YAML（インデント不整合、タブ混入、閉じ括弧欠落、不正 Unicode）が含まれている。
- Red 条件: YAML パーサーの例外が worker まで伝播して worker が停止する。または構文エラーを無視して空の services として処理する。
- Green 条件: YAML パース例外を捕捉し、`blocked` へ遷移する。`compose_validation_blocked` ログに `rule_id: yaml_parse_error` と当該ファイル名を記録する。§1 要件 3「構文が壊れている場合は blocked に落とす」が遵守されること。

### CVT-03: compose ファイルが YAML として有効だが `services` キーを持たない

- 攻撃シナリオ: `compose.yml` が `version: "3"` のみ、または空辞書、リスト、スカラ値など `services` キーを持たない YAML。
- Red 条件: `services` が `None` / 存在しないまま反復処理に入り `TypeError` が発生する。または空 services として validation pass 扱いになる。
- Green 条件: Compose モデルとして解釈できないことを検出し、`blocked` へ遷移する。`rule_id: invalid_compose_model` を記録する。§1 要件 3「Compose モデルとして解釈できない場合は blocked」が遵守されること。

### CVT-04: `privileged: true` を含むサービス

- 攻撃シナリオ: compose の `services.evil.privileged: true` が設定されている。
- Red 条件: `privileged: true` が検出されず、validation が通過する。
- Green 条件: 当該 service を拒否し、violation に `rule_id: privileged_container`、`service: evil`、`field: privileged` を含める。§2.5.1 が遵守されること。

### CVT-05: `network_mode: host`

- 攻撃シナリオ: `services.web.network_mode: host` が設定されている。
- Red 条件: host network mode が検出されず、validation が通過する。
- Green 条件: `rule_id: host_network_mode`、`service: web` を含む violation が生成される。§2.5.1 が遵守されること。

### CVT-06: `pid: host`

- 攻撃シナリオ: `services.debugger.pid: host` が設定されている。
- Red 条件: host PID namespace が検出されず、validation が通過する。
- Green 条件: `rule_id: host_pid` を含む violation が生成される。§2.5.1 が遵守されること。

### CVT-07: `ipc: host`

- 攻撃シナリオ: `services.shmem.ipc: host` が設定されている。
- Red 条件: host IPC namespace が検出されず、validation が通過する。
- Green 条件: `rule_id: host_ipc` を含む violation が生成される。§2.5.1 が遵守されること。

### CVT-08: `docker.sock` の bind mount

- 攻撃シナリオ: `services.dind.volumes` に `/var/run/docker.sock:/var/run/docker.sock` が含まれる。
- Red 条件: docker.sock マウントが検出されず、validation が通過する。
- Green 条件: `rule_id: docker_sock_mount`、`service: dind`、`field: volumes`、`normalized_path: /var/run/docker.sock` を含む violation が生成される。§2.5.2 が遵守されること。

### CVT-09: ホスト危険ディレクトリの bind mount (`/etc`, `/var`, `/dev`, `/sys`, `/proc`, `/`)

- 攻撃シナリオ: `services.spy.volumes` に `/etc:/mnt/etc:ro` が含まれる。
- Red 条件: ホスト `/etc` の bind mount が検出されず、validation が通過する。
- Green 条件: `rule_id: dangerous_host_mount`、`normalized_path: /etc` を含む violation が生成される。§2.5.2 の `/`、`/etc`、`/var`、`/dev`、`/sys`、`/proc` すべてが拒否対象であること。

### CVT-10: repo root 外を向く bind mount（path traversal `..`）

- 攻撃シナリオ: `services.app.volumes` に `../../etc/shadow:/secrets/shadow` が含まれる。ホスト側パスが repo root の外を指す。
- Red 条件: `..` を含むパスが path 正規化をすり抜けて許可される。
- Green 条件: `rule_id: path_traversal`、`raw_value: ../../etc/shadow` を含む violation が生成される。§2.3 / §1 要件 8 が遵守されること。

### CVT-11: bind mount に絶対パス（repo / runtime root 外）

- 攻撃シナリオ: `services.app.volumes` に `/tmp/evil:/data` が含まれる。ホスト側が repo root にも runtime root にも containment できない。
- Red 条件: 絶対パスが許可される。
- Green 条件: `rule_id: absolute_path_outside_allowed_roots` を含む violation が生成される。§2.3「absolute path は原則拒否」が遵守されること。

### CVT-12: symlink 経由での repo 外参照

- 攻撃シナリオ: repo 内に `data → /etc/shadow` のシンボリックリンクが存在し、`services.app.volumes` に `./data:/app/data` が含まれる。relative path としては repo 内だが、`realpath` は sandbox 外。
- Red 条件: symlink の解決先が検証されず、sandbox 外ファイルへのアクセスが許可される。
- Green 条件: `rule_id: symlink_escape`、`normalized_path` に解決先パスを含む violation が生成される。§2.3「symlink を経由した repo 外参照は containment 判定で拒否する」が遵守されること。

### CVT-13: `build.context` が repo 外

- 攻撃シナリオ: `services.app.build.context: ../../other_repo` が設定されている。
- Red 条件: repo 外の build context が許可される。
- Green 条件: `rule_id: build_context_outside_repo`、`field: build.context` を含む violation が生成される。§2.5.4 が遵守されること。

### CVT-14: `build.dockerfile` が repo 外

- 攻撃シナリオ: `services.app.build.dockerfile: /tmp/evil/Dockerfile` が設定されている。
- Red 条件: repo 外の Dockerfile 参照が許可される。
- Green 条件: `rule_id: dockerfile_outside_repo`、`field: build.dockerfile` を含む violation が生成される。§2.5.4 が遵守されること。

### CVT-15: `env_file` が repo 外

- 攻撃シナリオ: `services.app.env_file: ../../.env.production` が設定されている。
- Red 条件: repo 外の env_file が許可される。
- Green 条件: `rule_id: env_file_outside_repo`、`field: env_file` を含む violation が生成される。§2.5.4 が遵守されること。

### CVT-16: `configs[].file` が repo 外

- 攻撃シナリオ: top-level `configs.myconfig.file: /etc/nginx/nginx.conf` が設定されている。
- Red 条件: repo 外の config ファイルが許可される。
- Green 条件: `rule_id: config_file_outside_repo`、`field: configs.file` を含む violation が生成される。§2.5.4 が遵守されること。

### CVT-17: `secrets[].file` が repo 外

- 攻撃シナリオ: top-level `secrets.db_password.file: /etc/shadow` が設定されている。
- Red 条件: repo 外の secret ファイルが許可される。
- Green 条件: `rule_id: secret_file_outside_repo`、`field: secrets.file` を含む violation が生成される。§2.5.4 が遵守されること。

### CVT-18: `external: true` な network

- 攻撃シナリオ: `networks.production_net.external: true` が設定されている。
- Red 条件: 外部 network 参照が検出されず、validation が通過する。
- Green 条件: `rule_id: external_network`、`field: networks.production_net` を含む violation が生成される。§2.5.3 が遵守されること。

### CVT-19: `external: true` な volume

- 攻撃シナリオ: `volumes.shared_data.external: true` が設定されている。
- Red 条件: 外部 volume 参照が検出されず、validation が通過する。
- Green 条件: `rule_id: external_volume`、`field: volumes.shared_data` を含む violation が生成される。§2.5.3 が遵守されること。

### CVT-20: 危険な `cap_add`（`SYS_ADMIN`, `NET_ADMIN`, `SYS_PTRACE`, `DAC_READ_SEARCH`, `DAC_OVERRIDE`）

- 攻撃シナリオ: `services.escalator.cap_add: [SYS_ADMIN, NET_ADMIN]` が設定されている。
- Red 条件: 危険 capability の追加が検出されず、validation が通過する。
- Green 条件: 各危険 capability について violation が生成される。`rule_id: dangerous_capability`、`field: cap_add`、`raw_value` に当該 capability 名を含む。§2.5.5 の初期拒否対象 5 種がすべて拒否されること。

### CVT-21: `devices` によるホストデバイス露出

- 攻撃シナリオ: `services.gpu.devices: ["/dev/sda:/dev/sda"]` が設定されている。
- Red 条件: ホストデバイスの露出が検出されず、validation が通過する。
- Green 条件: `rule_id: host_device_exposure`、`field: devices` を含む violation が生成される。§2.5.2 が遵守されること。

### CVT-22: path 系フィールドに未許可の環境変数展開

- 攻撃シナリオ: `services.app.volumes` に `${HOME}/data:/app/data` が含まれる。`${HOME}` は許可変数リストに含まれない。
- Red 条件: 未解決変数が検証をバイパスし、実行時に展開されて sandbox 外にアクセスする。
- Green 条件: `rule_id: unresolved_env_var`、`raw_value: ${HOME}/data` を含む violation が生成される。§2.4「上記以外の未解決変数は拒否する」が遵守されること。

### CVT-23: 許可環境変数（`${TASK_RUNTIME_DIR}`）を使った正常パス

- 攻撃シナリオ: `services.app.volumes` に `${TASK_RUNTIME_DIR}/config:/app/config` が含まれる。
- Red 条件: 許可変数を使った runtime scratch 配下のパスが誤って拒否される。
- Green 条件: 許可変数が runtime root に正規化した上で containment チェックを通過し、validation が成功する。§2.4 の許可変数ポリシーが遵守されること。

### CVT-24: 複数 compose ファイルが存在し、1 件だけに違反がある

- 攻撃シナリオ: `compose.yml`（安全）と `docker-compose.yml`（`privileged: true` を含む）が repo root に同時に存在する。
- Red 条件: `compose.yml` の検査だけで pass し、`docker-compose.yml` の違反が見逃される。
- Green 条件: 両ファイルが検査対象となり、`docker-compose.yml` の violation により全体が `blocked` となる。§2.1「少なくとも 1 件に違反があれば失敗」が遵守されること。

### CVT-25: 安全な compose（repo 内 relative path のみ）

- 攻撃シナリオ: repo 内の安全な compose 定義（`./src:/app`、`build.context: .`、`env_file: .env` など、すべて repo root 配下）。
- Red 条件: 安全な compose が誤って `blocked` になる。
- Green 条件: validation が成功し、`compose_validation_passed` ログが記録される。`blocked: false`、`violations: []` が返される。§4 受け入れ条件 7「repo 内 relative path と runtime scratch 配下の安全な override だけは許可できること」が遵守されること。

### CVT-26: runtime override（`/workspace/{task_id}/runtime/compose.override.yml`）の安全な再検査

- 攻撃シナリオ: Feature #6 が生成した runtime override を追加入力として validator に渡す。override 内容は安全（port override のみ）。
- Red 条件: runtime override が validator に再投入可能な設計になっていない、または安全な override が拒否される。
- Green 条件: validator が compose files リストに runtime override を追加で受け取り、同一ルールセットで検査・通過できること。§1.1 NF-2 一貫性および §4 受け入れ条件 9 が遵守されること。

### CVT-27: validation 失敗時に複数 violation が集約される

- 攻撃シナリオ: 1 つの compose ファイルに `privileged: true`、`network_mode: host`、`docker.sock` マウントの 3 つの違反が含まれる。
- Red 条件: 最初の違反で打ち切り、残り 2 件が報告されない。
- Green 条件: 3 件すべての violation が集約されて返される。§2.2「失敗時は最初の違反で打ち切らず、可能な限り複数違反を集約」が遵守されること。

### CVT-28: validation 失敗時の `blocked` 遷移と log イベント記録

- 攻撃シナリオ: validation が失敗するが、task の状態更新や log イベント記録が省略されるか例外で失敗する。
- Red 条件: validation 失敗にもかかわらず task が `running` のまま残り、`compose_validation_blocked` ログが記録されない。
- Green 条件: task が `blocked` に遷移し、`compose_validation_blocked` ログに violation 詳細（`rule_id`、`service`、`field`、`normalized_path`、`raw_value`）が `details_json` として記録される。§3.2 のログイベントが遵守されること。

### CVT-29: validation 成功時の `compose_validation_passed` ログ記録

- 攻撃シナリオ: validation が成功するが、`compose_validation_passed` ログの記録が省略される。
- Red 条件: 成功時のログが記録されず、後段で validation が実行済みかどうかを追跡できない。
- Green 条件: `compose_validation_passed` ログに `compose_files` リストが記録される。§3.2 / §1.1 NF-3 可観測性が遵守されること。

### CVT-30: validation 開始時の `compose_validation_started` ログ記録

- 攻撃シナリオ: validator 呼び出し直後に YAML パース例外が発生し、`compose_validation_started` が記録される前に処理が中断する。
- Red 条件: validation 試行の痕跡が logs に残らず、「validation を開始したが失敗した」のか「validation 自体が呼ばれなかった」のか区別できない。
- Green 条件: `compose_validation_started` ログは YAML パース前に記録される。§3.2 / §1.1 NF-3 が遵守されること。

### CVT-31: orchestration 配下の task で validation 失敗時の blocked 伝播

- 攻撃シナリオ: orchestration root task 配下の phase task が compose validation で `blocked` になるが、root task への伝播が行われない。
- Red 条件: phase task が `blocked` なのに root task が `running` のまま残り、UI 上は進行中に見える。
- Green 条件: 既存の `handle_phase_blocked` により root task にも `blocked` が伝播すること。§2.7「orchestration 配下の task では既存 blocked 伝播を再利用できる形にする」が遵守されること。

### CVT-32: validator 返却値の構造不備

- 攻撃シナリオ: validator が `compose_files`、`violations`、`blocked`、`validated_runtime_root` のいずれかを返却値から欠落させる。
- Red 条件: 呼び出し元が `KeyError` / `AttributeError` をスローし、worker が停止する。
- Green 条件: validator の返却値が §2.2 で定義された 4 フィールドを常に含む構造であること。不備がある場合はテストで構造検証として検出する。

### CVT-33: 巨大 compose ファイルによる資源枯渇

- 攻撃シナリオ: compose ファイルが数万行、数十 MB で YAML パースにメモリと CPU を大量消費する。
- Red 条件: worker が OOM で停止するか、タイムアウトなく他 task を巻き込む。
- Green 条件: compose ファイルサイズに上限を設け、超過時は `blocked` へ遷移する。§1.1 NF-8「大規模 repo 全走査を避けること」と整合し、初期スライスではファイルサイズ上限で防御する。

### CVT-34: compose ファイルがバイナリ／非テキストファイル

- 攻撃シナリオ: `compose.yml` が実際にはバイナリファイル（画像、実行ファイル等）であり、YAML パーサーが予期しない動作をする。
- Red 条件: バイナリ入力で YAML パーサーがハングするか、大量メモリを消費する。
- Green 条件: YAML パース例外または事前のテキスト妥当性チェックにより `blocked` へ遷移する。CVT-02 の YAML 構文エラーハンドリングでカバーされること。

### CVT-35: `ports` に host namespace を使う設定

- 攻撃シナリオ: `services.web.ports` に `host_ip: 0.0.0.0` を含む長形式ポート定義、または `network_mode: host` と組み合わせた解釈不能な port 定義が含まれる。
- Red 条件: host namespace ポート定義が検出されず、validation が通過する。
- Green 条件: host namespace を使うポート設定を拒否する。§1 要件 10「host namespace を使う設定や解釈不能な port 定義は拒否」が遵守されること。

### CVT-36: validator が Docker / Compose CLI を実行しない

- 攻撃シナリオ: validator 実装内で `docker compose config` や `docker compose up --dry-run` を subprocess 経由で実行して検証する。
- Red 条件: validator が外部プロセスを起動する。Docker daemon 依存が生じ、daemon 停止時に validation 自体が失敗する。
- Green 条件: validator のコード内に `subprocess`、`docker`、`compose` CLI の呼び出しが存在しない。§2.2「validator は Docker / Compose CLI を実行しない」が遵守されること。

### CVT-37: 同一入力に対する決定的な結果

- 攻撃シナリオ: 同一 compose ファイルで validator を 2 回呼び出す。violation の順序や件数が実行ごとに異なる。
- Red 条件: 同一入力に対して violation の件数や内容が異なる。
- Green 条件: 同一入力に対して同一の violation 一覧（件数、内容、順序）が返される。§1.1 NF-7 決定性が遵守されること。

### CVT-38: `userns_mode: host` 相当の host namespace 利用

- 攻撃シナリオ: `services.rootmap.userns_mode: host` が設定されている。
- Red 条件: user namespace の host モードが検出されず、validation が通過する。
- Green 条件: `rule_id: host_userns_mode` を含む violation が生成される。§2.5.1 が遵守されること。

### CVT-39: volume の短形式・長形式の両方で path traversal を検出

- 攻撃シナリオ: `services.app.volumes` に短形式 `../../secret:/data` と長形式 `{type: bind, source: ../../other, target: /mnt}` の両方が含まれる。
- Red 条件: 一方の形式でのみ path traversal を検出し、他方がすり抜ける。
- Green 条件: 短形式・長形式いずれの volume 定義でも、ホスト側パスの path traversal を検出して拒否する。

### CVT-40: `env_file` がリスト形式で複数指定

- 攻撃シナリオ: `services.app.env_file: [".env", "../../.env.secret"]` のようにリスト形式で複数の env_file が指定され、1 件目は安全だが 2 件目が repo 外を参照する。
- Red 条件: 1 件目だけ検査して 2 件目の repo 外参照を見逃す。
- Green 条件: リスト内全件を検査し、repo 外参照を含む項目を拒否する。

## 3. RED で確認すべき項目

### 3.1 ファイル検出・パース異常（CVT-01〜03, CVT-33〜34）

1. compose 候補が 0 件で `blocked` に遷移すること。
2. YAML 構文エラーで `blocked` に遷移し、worker が停止しないこと。
3. `services` キー不在の YAML で `blocked` に遷移すること。
4. 巨大 compose ファイルでサイズ上限により `blocked` に遷移すること。

### 3.2 特権・namespace 系禁止（CVT-04〜07, CVT-38）

5. `privileged: true` が拒否されること。
6. `network_mode: host` が拒否されること。
7. `pid: host` が拒否されること。
8. `ipc: host` が拒否されること。
9. `userns_mode: host` が拒否されること。

### 3.3 ホスト資源露出系禁止（CVT-08〜09, CVT-21）

10. `docker.sock` マウントが拒否されること。
11. `/etc`、`/var`、`/dev`、`/sys`、`/proc`、`/` の bind mount が拒否されること。
12. `devices` によるホストデバイス露出が拒否されること。

### 3.4 パス検証（CVT-10〜17, CVT-22〜23, CVT-39〜40）

13. `..` による path traversal が拒否されること（短形式・長形式両方）。
14. repo / runtime root 外の絶対パスが拒否されること。
15. symlink 経由の repo 外参照が拒否されること。
16. `build.context`、`build.dockerfile`、`env_file`、`configs.file`、`secrets.file` の repo 外参照が拒否されること。
17. 未許可環境変数展開が拒否されること。
18. 許可環境変数（`${TASK_RUNTIME_DIR}`）を使った安全パスが許可されること。
19. `env_file` リスト形式で全件が検査されること。

### 3.5 ネットワーク・volume 外部依存系禁止（CVT-18〜19）

20. `external: true` な network が拒否されること。
21. `external: true` な volume が拒否されること。

### 3.6 capability 系禁止（CVT-20）

22. 初期拒否対象 5 種（`SYS_ADMIN`、`NET_ADMIN`、`SYS_PTRACE`、`DAC_READ_SEARCH`、`DAC_OVERRIDE`）が拒否されること。

### 3.7 ポート系（CVT-35）

23. host namespace を使うポート設定が拒否されること。

### 3.8 複合・正常系（CVT-24〜27, CVT-37）

24. 複数 compose ファイルのうち 1 件の違反で全体が `blocked` になること。
25. 安全な compose が正常に通過すること。
26. runtime override が同一 validator で再検査できること。
27. 複数 violation が集約されること。
28. 同一入力に対して決定的な結果が返ること。

### 3.9 ログ・状態遷移（CVT-28〜31）

29. validation 失敗時に `blocked` 遷移と `compose_validation_blocked` ログが記録されること。
30. validation 成功時に `compose_validation_passed` ログが記録されること。
31. `compose_validation_started` ログがパース前に記録されること。
32. orchestration 配下 task の validation 失敗が root task に伝播すること。

### 3.10 設計制約（CVT-32, CVT-36）

33. validator 返却値が 4 フィールド構造を常に持つこと。
34. validator が Docker / Compose CLI を呼び出さないこと（静的検査のみ）。

### 3.11 UI 表示 — Compose Validation blocked 理由の可視化（CVT-41〜CVT-52）

35. `compose_validation_blocked` ログの `details_json` が API レスポンスに含まれること（CVT-41）。
36. 子 task の `compose_validation_blocked` ログが subtask 内ログまたは root task ログとして API 経由で取得可能であること（CVT-42）。
37. `violations[]` が存在するログでは file / field / rule_id / raw_value を表 / リストとして構造化描画されること（CVT-43）。
38. `compose_validation_blocked` ログが通常ログ行と視覚的に区別され、「安全側ブロック」として強調描画されること（CVT-44）。
39. `blocked_reason=compose_validation` を持つログに「Compose Validation により安全側でブロック」相当の日本語ラベルが適用されること（CVT-45）。
40. `blocked` と `failed` がフロントエンド上で区別され、compose validation blocked は `エラー` ではなく「安全側ブロック」として表示されること（CVT-46）。
41. violation の `raw_value` / `message` に HTML/script タグが含まれていても XSS が発生しないこと（CVT-47）。
42. violations が空配列（`[]`）の場合にフロントエンドがクラッシュせず、フォールバック表示されること（CVT-48）。
43. `details_json` が null / 不正 JSON / 想定外構造の場合にフロントエンドがクラッシュせず、従来のメッセージ表示にフォールバックすること（CVT-49）。
44. 大量 violation（50 件以上）が含まれるログでも UI が崩れず、スクロール可能に描画されること（CVT-50）。
45. violation の `compose_file` が非常に長いパス、マルチバイト文字、特殊文字を含む場合に安全に描画されること（CVT-51）。
46. subtask アコーディオン内のログで `compose_validation_blocked` が識別でき、root task detail でも同一情報にアクセスできること（CVT-52）。

## 3.5. UI 破壊シナリオ詳細（CVT-41〜CVT-52）

### CVT-41: `compose_validation_blocked` ログの `details_json` が API レスポンスに含まれない

- 攻撃シナリオ: child task が `compose_validation_blocked` で blocked になり、DB の `logs.details_json` に violation 詳細が保存されているが、Dashboard の `_get_task_detail()` ログ SELECT が `details_json` カラムを含まない。フロントエンドは `message` テキストしか受け取れない。
- Red 条件: API レスポンスのログオブジェクトに `details_json` / `details` フィールドが含まれず、フロントエンドが violation 詳細を表示できない。
- Green 条件: `_get_task_detail()` のログ SELECT に `details_json` が含まれ、API レスポンスの各ログオブジェクトが `details` フィールドとして構造化データを返すこと。§3.2「Dashboard は violations[] の存在により安全側 block を明示表示できる前提」が遵守されること。

### CVT-42: 子 task のログが subtask 経由で API に返されない

- 攻撃シナリオ: root task detail API が subtask を返す際、各 subtask にはログが含まれない。`compose_validation_blocked` ログは child task の `task_id` で記録されているが、subtask シリアライズではログを JOIN していないため violation 情報にアクセスできない。root task のログにも child task のログが含まれない。
- Red 条件: subtask の `logs` が空配列で返され、`compose_validation_blocked` の violation 詳細を detail 画面で確認できない。
- Green 条件: root task detail API が `root_task_id` で紐づくログ（child task 分含む）を返すか、または subtask ごとのログを subtask オブジェクトに内包して返すこと。§1 要件 14 / §3.2 Dashboard 前提が遵守されること。

### CVT-43: `violations[]` が存在するログで構造化描画されない

- 攻撃シナリオ: API が `details_json.violations[]` を正しく返しているが、フロントエンドの log 描画関数が全ログを一律に `<li><strong>service</strong> event_type<br>message</li>` で描画し、`violations[]` のファイル/field/rule_id が展開されない。
- Red 条件: `compose_validation_blocked` ログの `violations[]` が無視され、利用者は `message` テキストのみ閲覧可能。
- Green 条件: フロントエンドでログの `event_type === 'compose_validation_blocked'` または `details.violations` の存在を検出し、各 violation を `compose_file`, `service`, `field`, `rule_id`, `raw_value`, `message` の構造で表形式またはリスト形式に描画すること。§1 要件 14、NF-9 が遵守されること。

### CVT-44: `compose_validation_blocked` ログが通常ログ行と視覚的に区別されない

- 攻撃シナリオ: ログ一覧の中に `compose_validation_blocked` ログが紛れ込み、他の `phase_task_enqueued` / `git_commit_succeeded` 等のログと同一スタイルで描画される。利用者がブロック原因のログを素早く特定できない。
- Red 条件: `compose_validation_blocked` ログが通常ログと同一の `<li>` スタイルで描画され、視覚的区別がない。
- Green 条件: `compose_validation_blocked` ログには専用 CSS クラス（例: `log-blocked-reason`）が適用され、背景色、ボーダー、見出し等で通常ログと区別されること。§2.3.2「blocked reason セクションとして強調表示してよい」が遵守されること。

### CVT-45: 日本語ラベルが適用されない（blocked reason の表示）

- 攻撃シナリオ: blocked reason の表示に英語ラベルがそのまま使われ、「compose_validation_blocked」や「Compose validation failed」などの英語メッセージが利用者に表示される。
- Red 条件: blocked reason の表示ラベルが英語のままで、日本語ユーザーにとって原因把握が困難。
- Green 条件: `blocked_reason=compose_validation` を持つログに対して「Compose Validation により安全側でブロック」相当の日本語ラベルが適用されること。§2.3.2「UI は『エラー』ではなく『Compose Validation により安全側でブロック』のような日本語説明を優先表示」が遵守されること。

### CVT-46: blocked と failed の誤認（Dashboard 上での区別）

- 攻撃シナリオ: child task が `compose_validation_blocked` で blocked に遷移するが、Dashboard UI が blocked を「エラー」「失敗」と同一視して赤いエラーメッセージで表示する。利用者は「何かが壊れた」と誤認する。
- Red 条件: `blocked` 状態の child task が `failed` と同一の見た目で表示され、安全上のブロックと実行エラーの区別が UI 上で付かない。
- Green 条件: `blocked` と `failed` に異なるスタイルが適用され、特に `compose_validation_blocked` では「安全側で意図的に停止した」ことが分かる表示になること。§1 要件 14 / NF-9、orchestration §2.3.2 / NF-15 が遵守されること。

### CVT-47: violation フィールドに HTML/script タグが含まれる（XSS）

- 攻撃シナリオ: compose ファイルの service 名やフィールド値に `<script>alert(1)</script>` が含まれ、violation の `raw_value` / `message` / `compose_file` に HTML タグが混入する。フロントエンドが innerHTML で描画してスクリプトが実行される。
- Red 条件: violation フィールドの HTML タグがエスケープされず、XSS が発生する。
- Green 条件: violation の全フィールドが `escapeHtml()` または `textContent` でエスケープされ、スクリプトが実行されないこと。

### CVT-48: violations が空配列の場合の UI 耐性

- 攻撃シナリオ: `compose_validation_blocked` の `details_json` に `violations: []`（空配列）が含まれる。フロントエンドが空配列の `.map()` で空テーブルを描画し、何もない表のヘッダだけが残る。
- Red 条件: 空の violation テーブルが描画され、利用者に「0 件の violation で blocked? 理由が分からない」という混乱を与える。
- Green 条件: violations が空配列の場合は「violation 詳細なし — メッセージを確認してください」等のフォールバック表示を適用し、テーブルヘッダだけが残る状態を避けること。

### CVT-49: `details_json` が null / 不正 JSON / 想定外構造の場合

- 攻撃シナリオ: DB の `details_json` が NULL（旧バージョンのログ）、不正 JSON 文字列、または `violations` キーを持たない想定外の構造（例: `{"error": "unknown"}`）。フロントエンドが `details.violations.map()` で `TypeError` をスローする。
- Red 条件: `details_json` が null / 不正 / 想定外の場合にフロントエンドが例外をスローし、ログ一覧が表示されない。
- Green 条件: フロントエンドは `details_json` が null / parse 不能 / `violations` 未保持の場合に安全にフォールバックし、従来の `message` テキスト表示に切り替えること。JavaScript の TypeError を起こさないこと。

### CVT-50: 大量 violation が含まれるログの UI 耐性

- 攻撃シナリオ: 単一 compose ファイルから 50 件以上の violation が検出され、`details_json.violations` に大量のエントリが含まれる。テーブルが無制限に拡大し、ページ下部の他の UI 要素が押し出される。
- Red 条件: violation テーブルが無制限に拡大し、ログ表示領域の他の要素が見えなくなる。
- Green 条件: violation 表示領域に `max-height` + `overflow-y: auto` が適用され、大量 violation でもスクロール可能に描画されること。

### CVT-51: violation フィールドに特殊文字 / マルチバイト文字 / 長大パスが含まれる

- 攻撃シナリオ: violation の `compose_file` に `/workspace/26/repo/日本語ディレクトリ/compose.yml` や 200 文字を超えるパスが含まれる。テーブルセルがレイアウトを破壊する。
- Red 条件: 長大パスやマルチバイト文字によりテーブルが水平にはみ出すか、セル内テキストが切り詰めなしに表示されてレイアウトが崩壊する。
- Green 条件: テーブルセルに `word-break: break-all` または `overflow-wrap: break-word` が適用され、長大文字列でもセル内に収まること。マルチバイト文字が化けず正常に表示されること。

### CVT-52: subtask アコーディオン内のログと root task detail ログの両方で violation 詳細にアクセス可能

- 攻撃シナリオ: subtask アコーディオン内のログは `compose_validation_blocked` を構造化表示するが、root task detail 下部のログ一覧には violation 詳細が含まれない（または逆）。利用者がどちらの表示を見るかで得られる情報が異なる。
- Red 条件: subtask 内ログと root task ログで violation 詳細の有無が不一致で、一方からしかアクセスできない。
- Green 条件: violation 詳細は subtask アコーディオン内のログと root task detail のログ一覧の両方で一貫して表示されること。同一の描画関数を再利用し、表示内容に差が生じないこと。

## 4. 設計へのフィードバック

§2.2 で未規定だったファイルサイズ上限を本書で補足する:

- **CVT-33 補足**: compose ファイルのサイズ上限は初期スライスで 1 MB とする。上限超過時は `rule_id: file_too_large` で `blocked` に遷移する。

§2.5.1 で列挙されていたが明示的にテスト対象と指定されていなかった `userns_mode: host` を本書で明示する:

- **CVT-38 補足**: `userns_mode: host` は §2.5.1 の拒否対象であることを RED テストで保証する。

§2.2 の返却値構造にフィールド有無の検証要件を追加する:

- **CVT-32 補足**: validator の返却値は型ではなく構造体（辞書）として `compose_files`、`violations`、`blocked`、`validated_runtime_root` の 4 キーを必ず含む。呼び出し側は構造を信頼するが、テストでフィールド存在を検証する。

### CVT-41 確定: API ログレスポンスに `details_json` を含める

- `_get_task_detail()` のログ SELECT に `details_json` カラムを追加し、API レスポンスの各ログオブジェクトに `details` フィールドとして構造化データを返す。`details_json` が NULL の場合は `null` を返す。不正 JSON の場合は `null` にフォールバックし、文字列をそのまま露出しない。

### CVT-42 確定: root task detail の `logs` で child task 分を含める

- `_get_task_detail()` のログ SELECT を `WHERE task_id = %s` から `WHERE root_task_id = %s` 系のクエリに変更し、root task 自身のログだけでなく child task 群のログもまとめて返す。これにより `compose_validation_blocked` ログが root task detail から直接確認可能になる。代替案として subtask ごとにログを内包する方式も許容するが、少なくとも child task のログが API 経由でアクセスできることを保証する。

### CVT-43 確定: violation 構造化描画のフロントエンド実装方針

- ログ描画関数で `event_type === 'compose_validation_blocked'` かつ `details.violations` が配列として存在する場合、violation ごとに `compose_file`, `service`, `field`, `rule_id`, `raw_value`, `message` を表形式で描画する。表ヘッダは日本語（「ファイル」「サービス」「フィールド」「ルール」「値」「メッセージ」）とする。

### CVT-44 確定: blocked reason の強調表示

- `compose_validation_blocked` ログに `log-blocked-reason` CSS クラスを適用し、通常ログ行と視覚的に区別する。背景色は warning 系（amber / yellow 淡色系）を使い、error（red）と区別して「安全側の意図的停止」を示す。

### CVT-45/46 確定: 日本語ラベルと blocked/failed 区別

- `compose_validation_blocked` ログのヘッダに「⚠ Compose Validation により安全側でブロック」を日本語で表示する。
- subtask の `status` が `blocked` の場合、CSS クラスを `task-status-blocked` として `task-status-failed` と区別する。背景色 / ボーダー色で failed（赤系）と blocked（オレンジ/アンバー系）を区別する。

### CVT-47 確定: XSS 防止

- violation の全フィールド描画に既存の `escapeHtml()` を適用する。innerHTML に直接代入する場合は全値を escapeHtml 経由で構築する。

### CVT-48/49 確定: 空配列 / null / 不正構造のフォールバック

- `violations` が空配列の場合は「violation 詳細なし」のプレースホルダ表示を適用する。
- `details` が null / undefined / `violations` 未保持の場合は従来の `message` テキスト表示にフォールバックし、構造化描画を試行しない。

### CVT-50 確定: 大量 violation の overflow 制御

- violation 表示領域に `max-height: 400px` + `overflow-y: auto` を適用する。

### CVT-51 確定: 長大文字列のテーブル制御

- violation テーブルのセルに `word-break: break-all` を適用し、長大パスやマルチバイト文字でレイアウトが崩壊しないようにする。テーブルに `table-layout: fixed` + `width: 100%` を適用する。

## 5. 関連ドキュメント

1. `docs/phase7-compose-validation-design.md`
2. `docs/phase5-artifact-apply-destructive-test-design.md`
3. `docs/phase6-direct-edit-destructive-test-design.md`
4. `docs/phase6-orchestration-destructive-test-design.md`
5. `.tdd_protocol.md`
