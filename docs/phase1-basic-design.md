# 丸投げシステム (Maru-nage v2) フェーズ1 基本設計書

## 0. 目的と前提

本書は、Maru-nage v2 のフェーズ1として、基本設計と非機能設計を確定するための文書である。対象は Brain、Librarian、Dashboard、Guardian、MariaDB の5サービスであり、実装コードは含めない。

設計前提は以下のとおりとする。

1. LLM の実行は Brain コンテナ内で起動する Copilot CLI のみを使用し、外部 LLM API は使用しない。
2. サービス間の通常連携は MariaDB を通信ハブとし、HTTP API は Dashboard から Librarian へのナレッジ操作に限定する。
3. 自己改修を含むすべての作業は、ホスト上の隔離ワークスペースで行い、稼働中の本系コンテナを直接編集しない。
4. 機微情報はホスト側単一の .env で管理し、Git 管理対象に入れない。
5. 監視、再起動、本番反映は Guardian が執行し、Brain 自身は自己再起動しない。

設計上の判断基準は以下とする。

1. 可用性: ゾンビタスクを検知し、再取得と復旧が可能であること。
2. 保守性: 二重ロギングと構造化イベントにより、UI と監査の双方で追跡できること。
3. 安全性: OS コマンド注入、秘密情報混入、ライブ環境破壊を防ぐこと。

## 1. MariaDB 詳細スキーマ設計

### 1.1 tasks テーブル

`tasks` は唯一の業務キュー兼状態管理テーブルとする。フェーズ遷移、再起動要求、ナレッジ更新、モデル指定、ポート予約、自己改修の切替要求をすべてここで表現する。

| 列名 | 型 | 用途 |
| --- | --- | --- |
| `id` | BIGINT PK | タスク識別子 |
| `parent_task_id` | BIGINT NULL | 派生タスクの親。フェーズ分割、再試行、子ジョブを表現 |
| `root_task_id` | BIGINT | 依頼全体の相関キー |
| `task_type` | VARCHAR(64) | `requirement_session` `phase1_design` `phase2_test_design` `knowledge_sync` `knowledge_query` `restart_service` `promote_release` `port_allocator` など |
| `phase` | TINYINT | 0 から 5。運用タスクは 90 番台予約 |
| `status` | VARCHAR(32) | `queued` `leased` `running` `waiting_approval` `succeeded` `failed` `blocked` `cancelled` |
| `requested_by_role` | VARCHAR(32) | `dashboard` `sales_ai` `manager_ai` `worker_ai` `guardian` `librarian` |
| `assigned_role` | VARCHAR(32) | 実行主体。`sales_ai` `manager_ai` `worker_ai` `guardian` `librarian` |
| `assigned_service` | VARCHAR(32) | `brain` `guardian` `librarian` |
| `assigned_model` | VARCHAR(64) NULL | Copilot CLI で使用するモデル識別子。例: `gpt-5.4` `claude-4.6-sonnet` |
| `model_contract_json` | JSON NULL | 実行時に許可されるモデル、CLI 引数、フェーズ、禁止フォールバック、検証ハッシュを格納 |
| `priority` | INT | 取得優先度 |
| `workspace_path` | VARCHAR(255) NULL | 隔離ワークスペース |
| `target_repo` | VARCHAR(255) NULL | 対象リポジトリ |
| `target_ref` | VARCHAR(255) NULL | ベースブランチまたはコミット |
| `working_branch` | VARCHAR(255) NULL | 専用作業ブランチ |
| `runtime_spec_json` | JSON NULL | コンテナ名、予約ポート、ネットワーク名、compose オーバーレイ名 |
| `payload_json` | JSON NULL | フェーズ入力、検索条件、Librarian 指示、Guardian 操作指示 |
| `result_summary_md` | MEDIUMTEXT NULL | 機微情報を除いた要約 |
| `lease_owner` | VARCHAR(128) NULL | タスク取得者インスタンス ID |
| `lease_expires_at` | DATETIME NULL | ゾンビ検知用リース期限 |
| `retry_count` | INT | 再試行回数 |
| `max_retry` | INT | 上限回数 |
| `approval_required` | BOOLEAN | 人間承認が必要か |
| `created_at` | DATETIME | 作成時刻 |
| `updated_at` | DATETIME | 更新時刻 |
| `started_at` | DATETIME NULL | 実行開始 |
| `finished_at` | DATETIME NULL | 実行完了 |

主要インデックスは以下とする。

1. `idx_tasks_queue` on (`assigned_service`, `status`, `priority`, `created_at`)
2. `idx_tasks_root` on (`root_task_id`, `phase`, `status`)
3. `idx_tasks_lease` on (`status`, `lease_expires_at`)
4. `idx_tasks_type` on (`task_type`, `status`)

### 1.2 messages テーブル

`messages` は AI 間会話、壁打ち履歴、タイムライン表示用の正規ログである。Dashboard の詳細画面はこのテーブルを時系列表示する。

| 列名 | 型 | 用途 |
| --- | --- | --- |
| `id` | BIGINT PK | メッセージ識別子 |
| `task_id` | BIGINT | 紐づくタスク |
| `root_task_id` | BIGINT | 依頼全体の相関キー |
| `phase` | TINYINT | フェーズ番号 |
| `sender_role` | VARCHAR(32) | 送信者ロール |
| `receiver_role` | VARCHAR(32) | 受信者ロール |
| `message_kind` | VARCHAR(32) | `prompt` `response` `decision` `review` `status` `knowledge_result` |
| `content_md` | MEDIUMTEXT | UI 表示用 Markdown。秘密情報は格納しない |
| `content_redaction_json` | JSON NULL | 伏字箇所、理由、ルール ID |
| `artifact_refs_json` | JSON NULL | 参照ファイル、コミット、ログ ID、ナレッジ ID |
| `created_at` | DATETIME | 作成時刻 |

主要インデックスは以下とする。

1. `idx_messages_task` on (`task_id`, `created_at`)
2. `idx_messages_root` on (`root_task_id`, `phase`, `created_at`)

### 1.3 logs テーブル

`logs` は構造化監査ログであり、詳細なデバッグ、再起動分析、機微情報スキャン、CLI 実行履歴の証跡を保持する。Dashboard トップ画面の進捗集計と Guardian の復旧判断はこのテーブルを利用する。

| 列名 | 型 | 用途 |
| --- | --- | --- |
| `id` | BIGINT PK | ログ識別子 |
| `task_id` | BIGINT NULL | 関連タスク |
| `root_task_id` | BIGINT NULL | 相関キー |
| `service` | VARCHAR(32) | `brain` `librarian` `dashboard` `guardian` `mariadb` |
| `component` | VARCHAR(64) | サブコンポーネント名 |
| `level` | VARCHAR(16) | `DEBUG` `INFO` `WARN` `ERROR` `AUDIT` |
| `event_type` | VARCHAR(64) | `task_leased` `model_validated` `cli_started` `cli_finished` `secret_scan_blocked` `port_retry` `restart_requested` など |
| `message` | TEXT | 機微情報除去済みメッセージ |
| `details_json` | JSON NULL | 非機微メタデータ。CLI 終了コード、検出ルール ID、ポート番号など |
| `redaction_state` | VARCHAR(16) | `clean` `redacted` `blocked` |
| `trace_id` | VARCHAR(64) | 分散追跡用 |
| `created_at` | DATETIME | 作成時刻 |

主要インデックスは以下とする。

1. `idx_logs_task` on (`task_id`, `created_at`)
2. `idx_logs_service` on (`service`, `level`, `created_at`)
3. `idx_logs_event` on (`event_type`, `created_at`)

### 1.4 タスク取得と同期方式

共通 DB アクセッサーは `SELECT ... FOR UPDATE` で `tasks` をロックし、以下の順で取得する。

1. `status = 'queued'` かつ `assigned_service = 自サービス` を優先度順に選択する。
2. 選択行を `FOR UPDATE` でロックし、`status = 'leased'`、`lease_owner`、`lease_expires_at` を更新する。
3. 実行開始時に `running` へ遷移する。
4. ハートビート更新が途切れ、`lease_expires_at` を超えたタスクは Guardian または同一サービスが再取得判定する。

再起動要求とナレッジ更新同期は専用タスクとして扱う。

1. Dashboard の再起動ボタンは `task_type = 'restart_service'` の `queued` 行を作成する。
2. Guardian がこれを取得し、対象コンテナを再起動した結果を `logs` と `messages` に残す。
3. Dashboard の PDF 登録、削除、サブモジュール登録、削除は Librarian API 呼び出し後、Librarian 自身が `knowledge_sync` タスクと結果メッセージを記録する。

### 1.5 動的モデルアサインの伝達設計

管理 AI から作業 AI へのモデル指定は `tasks.assigned_model` と `tasks.model_contract_json` で伝達する。`model_contract_json` の必須要素は以下とする。

フェーズ別の標準モデル割当は以下とする。

1. フェーズ0: `claude-4.6-sonnet`
2. フェーズ1: `gpt-5.4`
3. フェーズ2: `claude-4.6-opus`
4. フェーズ3: `gpt-5.4`
5. フェーズ4: `gpt-5.4`
6. フェーズ5: `claude-4.6-opus`

特にフェーズ3とフェーズ4は、テスト実装と本体実装の双方で同一の `gpt-5.4` を用いる。これにより、フェーズ3で生成したテスト前提とフェーズ4で行う実装修正の推論品質差を排除し、Red から Green への遷移時にモデル差異が原因となるブレを抑制する。

1. `phase`: 対応フェーズ
2. `model`: 許可された単一モデル名
3. `cli_profile`: Copilot CLI に渡すプロファイル名または明示引数定義
4. `fallback_allowed`: 常に `false`
5. `contract_version`: ポリシーバージョン
6. `contract_digest`: 管理 AI が記録した整合性ハッシュ

作業 AI は実行前に `assigned_model` と `model_contract_json.model` の一致を検証し、一致しない場合はタスクを `blocked` に遷移する。CLI 起動時は契約に記載された引数のみを許可し、実際に起動したモデル名を `logs.event_type = 'model_validated'` と `cli_started` に記録する。

フェーズ3またはフェーズ4のタスクで `assigned_model != 'gpt-5.4'` の場合は、契約不一致として必ず `blocked` に遷移する。既にキュー投入済みの旧契約タスクが存在する場合は、管理 AI が `cancelled` にしたうえで `gpt-5.4` 契約で再発行する。

設計理由は以下である。

1. モデル指定をメッセージ本文ではなく構造化列に置くことで、改ざん検知と UI 表示を両立できる。
2. フォールバック禁止を契約に明記することで、フェーズごとの推論品質を固定できる。
3. 実際に起動したモデル名をログへ残すことで、設計どおりのモデルが使われたか監査できる。
4. フェーズ3とフェーズ4のモデルを統一することで、テスト生成と実装修正の解釈差による不安定性を抑えられる。

## 2. Dashboard 連携・UI 設計

### 2.1 連携原則

Dashboard は管制塔であり、通常の状態参照と操作要求は DB 経由、ナレッジ管理だけは Librarian API 経由とする。これにより制約どおり REST の乱立を防ぎつつ、ファイルアップロードを伴う知識登録だけを API として切り出せる。

### 2.2 画面設計

#### トップ画面

表示内容は以下とする。

1. 実行中タスク一覧。フェーズ、担当 AI、使用モデル、現在ステータス、開始時刻、経過時間を表示する。
2. サービス稼働状況。Brain、Librarian、Guardian、Dashboard、MariaDB のヘルスと最終ハートビートを表示する。
3. 直近警告。`logs` の `WARN` `ERROR` `AUDIT` を集計し、秘密情報ブロック、ポート再試行、再起動の有無を表示する。

データ取得元は `tasks` と `logs` である。リアルタイム性は Dashboard サービス内部の DB ポーリングまたは DB 通知購読を UI 用ストリームへ変換して実現する。外部公開用 REST は設けない。

#### 開発依頼画面

入力内容は以下とする。

1. 対象リポジトリ、基準ブランチ、依頼本文
2. 自己改修フラグ
3. 承認必須フラグ
4. 参照ナレッジの選択

送信時は `tasks` に `task_type = 'requirement_session'` またはフェーズ開始タスクを作成し、初回メッセージを `messages` に書く。

#### 開発履歴一覧画面

表示内容は以下とする。

1. 依頼 ID、タイトル、対象リポジトリ
2. 現在フェーズ、最終結果、最終更新時刻
3. 使用したモデル一覧
4. 承認待ち、失敗、差し戻しのラベル

一覧は `tasks.root_task_id` 単位で集約し、`messages` の最新要約と `logs` の重要イベントをバッジ化して表示する。

#### 開発履歴詳細画面

詳細はタイムライン画面とし、以下を同一時系列で表示する。

1. フェーズ0の壁打ちメッセージ
2. 管理 AI と作業 AI の指示、回答、レビュー
3. ナレッジ検索結果
4. テスト Red、Green、監査結果
5. 再起動や秘密情報検知などの運用イベント

メッセージ本体は `messages`、構造化運用イベントは `logs` を統合表示する。UI 上は秘密情報の検知件数だけを見せ、秘密文字列そのものは出さない。

#### ナレッジ管理 UI

提供機能は以下とする。

1. PDF アップロード
2. PDF 削除
3. GitHub サブモジュール登録
4. GitHub サブモジュール削除
5. 登録済みナレッジ一覧と状態表示

この画面だけが Librarian API を呼ぶ。登録、削除の結果は Librarian が DB に同期し、トップ画面や履歴詳細に反映される。

#### エージェント再起動 UI

再起動ボタンは Dashboard から直接 Docker を叩かず、`restart_service` タスクを発行する。Guardian がそれを実行し、完了結果を返す。

### 2.3 画面と内部コンポーネントの責務分離

1. Dashboard frontend: 表示とユーザー入力のみを担当する。
2. Dashboard backend: DB 読み書き、UI ストリーム生成、Librarian API 呼び出しを担当する。
3. Guardian: 実際の再起動、マージ、切替を担当する。

設計理由は以下である。

1. Dashboard に Docker 権限を持たせないことで、誤操作の爆発半径を小さくできる。
2. 履歴の唯一ソースを DB に置くことで、UI、監査、復旧が同一データを参照できる。
3. 再起動要求を非同期タスク化することで、UI 操作と危険なホスト操作を切り離せる。

## 3. Librarian の探索・API 設計

### 3.1 基本責務

Librarian は以下の責務を持つ。

1. PDF を `pymupdf` で Markdown 化する。
2. 変換結果に `reference_pdf_ingestion.py` の整形ルールを厳密適用する。
3. 知識メタデータを管理し、ChromaDB 用チャンクを生成する。
4. GitHub サブモジュールを取り込み、参照対象の実体取得を GitHub MCP と連携する。
5. Dashboard からの登録、削除要求だけを API として受ける。
6. 検索実行は DB タスク経由で処理し、結果を `messages` へ返す。

### 3.2 Dashboard から呼ばれる Librarian API

HTTP API はナレッジ操作だけとする。

1. `POST /knowledge/pdf`
   入力: ファイル、タイトル、タグ、参照範囲、保持ポリシー
   出力: `knowledge_id`、受付状態、重複判定結果
2. `DELETE /knowledge/pdf/{knowledge_id}`
   入力: 論理削除フラグまたは物理削除フラグ
   出力: 削除状態、影響チャンク数
3. `POST /knowledge/submodule`
   入力: GitHub URL、参照ブランチまたはコミット、取り込みパス、説明
   出力: `knowledge_id`、取り込み状態
4. `DELETE /knowledge/submodule/{knowledge_id}`
   入力: 削除モード
   出力: 削除状態

これ以外の問い合わせ API は追加しない。検索は DB タスクで実行する。

### 3.3 PDF 変換と登録シーケンス

処理順は以下とする。

1. Librarian が PDF を受領する。
2. `pymupdf` でページ単位に抽出する。
3. `reference_pdf_ingestion.py` と同一の見出し規則、箇条書き整形、コードブロック扱い、ページ境界表現を適用する。
4. 正規化後 Markdown を原文保存領域へ保管する。
5. チャンク化し、埋め込み用テキストを生成する。
6. ChromaDB にベクトル登録する。
7. 知識メタデータを DB に `knowledge_sync` タスク結果として記録する。
8. Dashboard 詳細画面用に `messages` へ結果要約を記録する。

### 3.4 GitHub サブモジュール登録シーケンス

処理順は以下とする。

1. Dashboard から登録要求を受ける。
2. Librarian が指定 URL と参照コミットを検証する。
3. サブモジュールとして固定コミットで取得し、可変ブランチ参照はそのまま使わない。
4. 取り込みメタデータを保存する。
5. 実体検索時は GitHub MCP を使って必要断片を取得する。
6. 取得断片の要約またはチャンクを ChromaDB と突合し、重複を避ける。

### 3.5 探索シーケンス

検索要求は `task_type = 'knowledge_query'` として DB に投入し、Librarian が処理する。探索順序は以下とする。

1. クエリ正規化
2. ChromaDB で意味検索し、候補チャンクを得る
3. 候補チャンクに紐づく GitHub サブモジュールがある場合、GitHub MCP で実体を取得する
4. ベクトル候補と実体候補を重み付け統合する
5. 重複と古い版を除去する
6. 参照元、信頼度、抜粋を `messages.message_kind = 'knowledge_result'` で返す

設計理由は以下である。

1. UI 向けアップロードは API が必要だが、検索は DB タスクに寄せたほうが全体方針と整合する。
2. PDF は ChromaDB、コードや実体確認は GitHub MCP と責務分担すると、速さと正確さを両立できる。
3. サブモジュールを固定コミットで扱うことで、後から回答根拠が変わる問題を防げる。

## 4. ワークスペース展開およびコンテナ運用設計

### 4.1 隔離ワークスペース設計

管理 AI は依頼開始時にホスト上へ独立ディレクトリを作成する。標準構成は以下とする。

1. `/workspace/{root_task_id}/repo/`: 対象リポジトリ本体
2. `/workspace/{root_task_id}/artifacts/`: 生成成果物、テスト結果、差分要約
3. `/workspace/{root_task_id}/system_docs_snapshot/`: 実行時点の内部運用文書スナップショット
4. `/workspace/{root_task_id}/patches/`: 編集マニフェストとバックアップ差分

展開後に専用ブランチを作成する。命名規則は `mn2/{root_task_id}/{phase-or-purpose}` とする。

### 4.2 コンテナ名とネットワーク名の衝突回避

動作中の本系コンテナと衝突しないよう、隔離実行で起動するコンテナ名、ネットワーク名、ボリューム名はすべてタスク名前空間付きとする。

1. コンテナ名: `mn2-{root_task_id}-{service}-{short_hash}`
2. ネットワーク名: `mn2-net-{root_task_id}`
3. 一時ボリューム名: `mn2-vol-{root_task_id}-{purpose}`

### 4.3 動的ポート割当

本系とのポート衝突を避けるため、ポート予約は DB ロック付きアロケータ方式にする。

1. `tasks` にブートストラップ時から `task_type = 'port_allocator'` の単一行を常駐させる。
2. 管理 AI はポート割当時にこの行を `SELECT ... FOR UPDATE` でロックする。
3. サービスごとに予約レンジを分ける。例として Dashboard 18080-18179、Librarian 18180-18279、検証用 MariaDB 18300-18399 を使う。
4. 初回候補は `root_task_id` のハッシュで決定し、同一依頼で再現可能にする。
5. Docker 上の使用状況とホストの待受状況を両方確認する。
6. 衝突時は次候補へ進み、指数バックオフではなく短い固定バックオフとジッタで最大 8 回まで再試行する。
7. 8 回失敗した場合はタスクを `blocked` にし、Guardian へ異常通知する。

固定バックオフとジッタを採用する理由は、全タスクが同時に指数的に遅くなることを防ぎ、かつ再衝突の同位相を避けるためである。

### 4.4 Brain 内部フロー

1. 営業 AI はフェーズ0専用で要件壁打ちを行う。
2. 管理 AI は隔離ワークスペース展開、ブランチ作成、モデル契約決定、実行タスク分解を行う。
3. 作業 AI は管理 AI が指定したモデル契約で Copilot CLI を起動し、ファイル変更、テスト、修正を行う。
4. Docker 操作が必要な場合は Brain コンテナから Docker CLI をホストソケット経由で実行する。

### 4.5 再帰的自己改修シナリオ

対象リポジトリがシステム自身である場合、以下の追加制約を適用する。

1. 変更は本系 bind mount や稼働中コンテナ内ではなく、必ず隔離ワークスペースで行う。
2. Dockerfile、compose 定義、起動スクリプト変更時は影響範囲を `runtime_spec_json` に明示する。
3. 検証用コンテナ群は別名、別ポート、別ネットワークで起動する。
4. 本番反映は Guardian のみが実施し、Brain は `promote_release` タスクを出すだけにする。
5. 反映方式は blue-green に準ずる段階切替とし、新系ヘルス確認後に旧系を停止する。

設計理由は以下である。

1. 自己改修時の最大リスクは稼働基盤の自壊であるため、実行面の権限者を Guardian に限定する。
2. 名前空間分離とポート分離を徹底すると、本系と検証系を同時稼働できる。
3. 同一 root task 内でワークスペース、ログ、差分を閉じることで、監査とロールバック判断が容易になる。

## 5. リポジトリ内ファイル運用プロトコル

### 5.1 基本原則

既存ファイルは破壊的に置換しない。編集は常に差分最小、意味保存、再適用可能性を優先する。

### 5.2 編集前判定

作業 AI は編集前に対象ファイルを次の4種へ分類する。

1. 構造化設定ファイル: JSON、YAML、TOML、Compose、Dockerfile
2. ソースコード: Python、TypeScript、JavaScript など
3. ドキュメント: Markdown、設計書、運用手順
4. 生成物またはビルド成果物

生成物は原則直接編集しない。生成元を更新する。

### 5.3 編集作法

1. 構造化設定ファイルは AST または構造パーサ前提で編集し、キー順やコメントを可能な限り保持する。
2. ソースコードはシンボル単位の局所修正を原則とし、無関係な整形を混在させない。
3. ドキュメントは既存見出し配下への追記を優先し、必要時のみ新規節を追加する。
4. AI 管理領域が必要な場合のみ、開始マーカーと終了マーカーを設ける。マーカーには task_id、目的、再生成条件を含める。
5. 同一ファイルの全面置換は、生成専用ファイルであり、かつハッシュ一致でベース確認できた場合だけに限定する。

### 5.4 書き込み安全策

1. 編集前に `patches/` へ差分マニフェストを出力する。
2. 書き込み後は構文検証、フォーマット検証、関連テストを実行する。
3. 失敗時は元ファイル全体を戻すのではなく、該当差分単位で巻き戻す。
4. 別 AI が同一ファイルへ並行編集しないよう、管理 AI はファイル単位の作業計画を分離する。

### 5.5 ドキュメント二層保存ポリシー

1. システム内部文書は `/system_docs/` に保存する。ここにはモデルポリシー、運用手順、レビュー基準を置く。
2. 実行成果物は対象リポジトリの `/docs/` `/src/` `/tests/` などに保存する。
3. 実行時に参照した内部文書は `system_docs_snapshot/` へ複写し、当該タスクの根拠として固定する。

設計理由は以下である。

1. 既存ファイル破壊を防ぐには、編集手順自体をファイル種別ごとに固定する必要がある。
2. マーカーの濫用は可読性を落とすため、AI 管理領域が必要な場合に限定する。
3. 内部文書と成果物を分離すると、システム知識と案件成果物を混同せずに済む。

## 6. 機微情報漏洩防止メカニズム

### 6.1 保持ポリシー

1. 機微情報はホスト側単一の `.env` に保存する。
2. `.env` は全サービスへ環境変数として注入し、リポジトリに保存しない。
3. `.gitignore` には `.env` `.env.*` `*.pem` `*.key` `*.p12` 認証キャッシュ、ローカル資格情報ファイルを必須登録する。
4. `messages` と `logs` は秘密そのものを保存せず、検知ルール ID と伏字済みメタデータだけを保存する。

### 6.2 コミット前機微情報スキャンの具体フロー

作業 AI は `git commit` の直前に必ず以下を行う。

1. ステージ対象ファイル一覧を取得する。
2. パスベース遮断を行う。`.env`、秘密鍵、資格情報キャッシュ、クラウド設定ファイルが含まれていれば即時ブロックする。
3. 差分本文を対象に既知シークレットパターンを検査する。例として GitHub token、JWT、Bearer token、MariaDB 接続文字列埋め込み、秘密鍵ヘッダ、Copilot 認証断片などを対象とする。
4. 高エントロピー文字列を検査する。Base64、Base32、hex、URL encode 済み疑似トークンを候補抽出する。
5. 難読化解除を一段だけ試す。Base64 decode、hex decode、URL decode を行い、再スキャンする。
6. 変数名文脈を検査する。`SECRET` `TOKEN` `PASSWORD` `API_KEY` `AUTH` `CREDENTIAL` を含むキーに値が直書きされていないか確認する。
7. 1件でもヒットしたらコミットを中断し、`logs` へ `secret_scan_blocked` を記録し、`messages` へ人間向け要約を残す。
8. 検出箇所はハッシュ化断片、ファイルパス、行番号、ルール ID だけを保存し、元値は保存しない。

### 6.3 多層防御

1. 入力防御: `.env` と秘密ファイルを Git 管理外に置く。
2. 実行防御: Brain と各サービスは必要最小限の環境変数のみ受け取る。
3. 出力防御: 生成ログとメッセージは redaction を通したものだけ保存する。
4. 提出防御: コミット前スキャンでブロックする。
5. 運用防御: Guardian と Dashboard はブロック件数を可視化する。

設計理由は以下である。

1. 単純な正規表現だけでは難読化トークンを見逃すため、エンコード解除とエントロピー判定を組み合わせる必要がある。
2. ブロック時にも元値を保存しないことで、検知ログ自体が漏洩源になることを防げる。
3. コミット直前で強制スキャンすることで、AI の生成経路に依らず最終出口を塞げる。

## 7. Dockerfile 作成指針

### 7.1 共通方針

1. システム独自サービスのベースは Python 3.11 以上とする。
2. すべての Python サービスでログのバッファリングを無効化する。
3. 環境変数はホストの `.env` から注入する。
4. 実行ユーザーは原則非 root とし、Docker ソケット操作が必要なコンテナだけ必要権限を明示する。
5. 依存はビルド段階と実行段階を分け、イメージ最小化を行う。

### 7.2 Brain 用 Dockerfile 構成案

含めるものは以下とする。

1. Python 3.11 以上
2. Git、GitHub CLI、Copilot CLI、Docker CLI
3. MariaDB client
4. `tini` などの init プロセス
5. `/var/run/docker.sock` を利用するためのグループ権限設定

主要環境変数は以下とする。

1. `PYTHONUNBUFFERED=1`
2. `MARUNAGE_ROLE_SET=sales,manager,worker`
3. `DB_HOST` `DB_PORT` `DB_NAME` `DB_USER` `DB_PASSWORD`
4. `COPILOT_CONFIG_DIR`
5. `DOCKER_HOST` または docker.sock マウント前提設定
6. `SYSTEM_DOCS_PATH=/system_docs`
7. `WORKSPACE_ROOT=/workspace`

### 7.3 Librarian 用 Dockerfile 構成案

含めるものは以下とする。

1. Python 3.11 以上
2. `pymupdf` とそのビルド依存
3. ChromaDB client または server 接続ライブラリ
4. MariaDB client
5. GitHub MCP 接続に必要なクライアント依存

主要環境変数は以下とする。

1. `PYTHONUNBUFFERED=1`
2. `DB_HOST` `DB_PORT` `DB_NAME` `DB_USER` `DB_PASSWORD`
3. `CHROMA_HOST` `CHROMA_PORT`
4. `KNOWLEDGE_STORE_PATH`
5. `REFERENCE_INGESTION_RULE_PATH`

### 7.4 Dashboard 用 Dockerfile 構成案

Dashboard は UI と DB 連携を担う。REST API を一般化しないため、同一サービス内で画面描画、DB 読み書き、Librarian API 呼び出しを閉じる。

含めるものは以下とする。

1. Python 3.11 以上
2. UI サーバ実行環境
3. MariaDB client
4. Librarian API 呼び出し用 HTTP client

主要環境変数は以下とする。

1. `PYTHONUNBUFFERED=1`
2. `DB_HOST` `DB_PORT` `DB_NAME` `DB_USER` `DB_PASSWORD`
3. `LIBRARIAN_BASE_URL`
4. `DASHBOARD_SESSION_SECRET`
5. `UI_POLL_INTERVAL_MS` またはストリーム設定

### 7.5 Guardian 用 Dockerfile 構成案

Guardian はホスト側の聖域であり、再起動、マージ、本番反映を担当する。

含めるものは以下とする。

1. Python 3.11 以上
2. Docker CLI
3. Git
4. MariaDB client

主要環境変数は以下とする。

1. `PYTHONUNBUFFERED=1`
2. `DB_HOST` `DB_PORT` `DB_NAME` `DB_USER` `DB_PASSWORD`
3. `DOCKER_HOST` または docker.sock マウント設定
4. `PROMOTION_POLICY_PATH`
5. `HEALTHCHECK_TIMEOUT_SEC`

### 7.6 MariaDB 用構成案

MariaDB は永続ボリューム必須とし、公式イメージ採用を優先する。これはデータベースを Python ベースから自作するよりも、保守性と信頼性が高いためである。

主要環境変数は以下とする。

1. `MARIADB_DATABASE`
2. `MARIADB_USER`
3. `MARIADB_PASSWORD`
4. `MARIADB_ROOT_PASSWORD`
5. `TZ`

### 7.7 Dockerfile 共通の安全指針

1. `.env` はイメージへコピーしない。
2. 資格情報キャッシュをレイヤへ焼き込まない。
3. ヘルスチェックを定義し、Dashboard と Guardian が監視可能な状態にする。
4. 実行時に必要な権限だけを付与し、Brain と Guardian 以外へ Docker ソケットを渡さない。

設計理由は以下である。

1. Brain と Guardian のみが Docker 権限を持つと、攻撃面と誤操作面が最小化できる。
2. Python ベース統一で保守運用を単純化しつつ、MariaDB だけは公式実装を利用するのが合理的である。
3. 依存と環境変数をサービスごとに明示すると、自己改修時も差分影響を限定できる。

## 8. 非機能要件への具体反映

### 可用性

1. `lease_expires_at` によるゾンビタスク検知を行う。
2. Guardian が異常終了したタスクとコンテナを再評価する。
3. 自己改修時は blue-green 切替で停止時間を最小化する。

### 保守性

1. `messages` と `logs` を分け、会話履歴と構造化運用ログを分離する。
2. `root_task_id` と `trace_id` で全イベントを追跡可能にする。
3. `/system_docs/` と `system_docs_snapshot/` で設計根拠を固定する。

### 安全性

1. モデル契約の検証により誤モデル起動を防ぐ。
2. コマンド実行はテンプレート化された引数だけを許可し、自由文字列連結を禁止する。
3. コミット前機微情報スキャンを強制する。
4. Docker ソケット権限を Brain と Guardian に限定する。

## 9. フェーズ1 結論

本設計により、Maru-nage v2 は以下を満たす。

1. MariaDB を唯一の通常通信ハブとしつつ、Librarian の知識登録だけを専用 API として切り出す。
2. 管理 AI から作業 AI への動的モデルアサインを、構造化契約と監査ログで安全に伝達できる。
3. 隔離ワークスペース、動的名前空間、動的ポート割当により、本系への干渉なく自己改修を含む作業が可能になる。
4. 既存ファイル非破壊の編集作法とコミット前秘密情報スキャンにより、安全な自律開発基盤となる。
5. Guardian を聖域化することで、再起動と本番反映の危険操作を Brain から分離できる。

以上をフェーズ1の基本設計確定版とする。