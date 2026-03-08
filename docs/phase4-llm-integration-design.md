# 丸投げシステム (Maru-nage v2) フェーズ4 LLM 連携設計書

## 0. 目的

本書は、`brain` ワーカーがキュー投入済みタスクの `instruction` を LLM へ渡し、コード生成または設計生成の応答を安全に取得・保存し、後続フェーズへ引き渡すための要件・設計を定義する。

このフェーズでは、永続的な設計判断を本書に記録し、`.tdd_protocol.md` には実行中の目的、直近タスク、進捗のみを記録する。

## 1. 要件定義

1. `brain` は `assigned_service='brain'` の `queued` タスクを取得した後、必要に応じて対象リポジトリを clone し、タスク payload の `instruction` を `copilot` CLI コマンド経由で LLM に渡せること。
2. 外部 LLM API（OpenAI、Anthropic 等）は使用しない。LLM 機能はコンテナにインストールされた `copilot` CLI コマンドを通じてのみ利用する。
3. LLM 応答は、後続フェーズが参照できる永続データとして保存すること。少なくとも task 単位で再取得でき、監査時に参照可能であること。
4. `copilot` CLI の認証は Phase 3 で確立した `GITHUB_TOKEN`（ホスト側の `gh auth token` 由来）を使用し、追加の API キーを必要としないこと。
5. `copilot` コマンド未インストール、認証失敗、タイムアウト、空応答などの異常時は、タスクを安全側へ倒して `blocked` へ遷移させ、原因を `logs` に残すこと。
6. LLM への入力と保存される結果から、`GITHUB_TOKEN` や禁止された秘密情報がログや artifact へ漏洩しないこと。

## 1.1 非機能要件

1. セキュリティ: LLM 認証は Phase 3 の `GITHUB_TOKEN` 注入のみに依存する。別個の LLM API キーは導入しない。`GITHUB_TOKEN` を DB の `tasks`、`logs`、`messages`、生成 artifact に平文で保存しない。
2. 可観測性: `task_started`、`repository_prepared` に続き、LLM 実行開始・成功・失敗をイベントとして `logs` に記録し、障害原因を運用者が追跡できるようにする。
3. 回復性: 一時的な CLI エラーや短時間の通信断では限定リトライを許可し、恒久障害時は無限リトライさせず `blocked` に遷移する。
4. 保守性: `copilot` CLI の呼び出し詳細は `src/backend/llm_client.py` に隔離し、`task_backend` は統一インターフェイス (`generate()`) だけを呼ぶ。
5. 互換性: 既存の task state machine、workspace sandbox、repository clone フロー、Phase 3 の `GITHUB_TOKEN` 注入運用と衝突しない。
6. 性能: 1 タスクあたりの LLM 呼び出しは同期 1 回を基本とし、リトライ込みでも worker の lease 有効時間内に完結させる。

## 2. 基本設計

### 2.1 処理フロー

1. `dashboard` が投入した task は、既存どおり `queued` 状態で `brain` に割り当てられる。
2. `brain` は task を `leased` -> `running` へ遷移させ、GitHub リポジトリ指定タスクでは `/workspace/{task_id}/repo` を準備する。
3. `brain` は payload から `instruction`、`task`、`repository_path`、`phase_flow` など必要情報を抽出し、LLM クライアントへ渡す。
4. LLM クライアントは `copilot` CLI コマンドをサブプロセスとして実行し、標準出力から応答テキストを取得する。
5. `task_backend` は応答を永続化し、成功時は後続フェーズで利用可能な形に整える。失敗時は `logs` を残したうえで `blocked` に遷移させる。

### 2.2 認証方式

- 認証は Phase 3 で確立した `GITHUB_TOKEN` を唯一の経路とする。追加の LLM API キー (`OPENAI_API_KEY` 等) は導入しない。
- `copilot` コマンドは `GITHUB_TOKEN` 環境変数を参照して GitHub に認証する。Phase 3 の `gh_token_compose.py` による注入がそのまま利用できる。
- `GITHUB_TOKEN` は既に `brain`、`guardian`、`dashboard`、`librarian` の全コンテナに注入されているが、`copilot` コマンドを実行するのは `brain` のみである。
- `entrypoint.sh` は `brain` 起動時に `copilot` コマンドの存在と `GITHUB_TOKEN` の設定を検証する。

### 2.3 Copilot CLI の利用方式

- コンテナの Dockerfile runtime ステージに `copilot` CLI コマンドをインストールする（`gh` CLI はホスト側のトークン取得専用であり、コンテナには不要）。
- `copilot` コマンドを使い、prompt をパイプまたは引数で渡してテキスト応答を取得する。
- `copilot` が非ゼロ終了コードを返した場合は、stderr の内容を分類して適切なエラー例外へ変換する。

### 2.4 結果保存方針

- 正本は task 単位で再利用しやすい永続領域とし、初期実装では `tasks.result_summary_md` に要約、詳細本文は artifact ファイルへ保存する。
- 生成全文は `/workspace/{task_id}/artifacts/llm_response.md` に保存する。
- `logs` には全文を格納せず、イベント名、結果種別、失敗理由、保存先パスのみを記録する。
- Phase 6 以降、artifact は Copilot 応答ログとして保持する。ファイル反映の一次手段は direct-edit + commit/push であり、Dashboard には `result_summary_md` のみを返す。

### 2.5 状態遷移方針

- 正常系: direct-edit 対象 task は `queued` -> `leased` -> `running` -> `succeeded` を既定とする。Copilot 応答保存後、repository への commit/push 成功をもって完了とみなす。
- `waiting_approval` は direct-edit pivot 前の legacy 互換経路として残るが、Phase 6 の標準出口ではない。
- 異常系: `copilot` コマンド未インストール、認証失敗、タイムアウト、空応答、保存失敗は `running` -> `blocked` とする。
- `failed` はアプリ内部例外ではなく、設計済み異常系に該当しない予期しない処理失敗に限定する。

## 3. 詳細設計

### 3.1 改修対象

1. `Dockerfile` — runtime ステージに `copilot` CLI コマンドをインストール
2. `src/backend/llm_client.py` — `copilot` CLI コマンドをサブプロセスで呼び出す実装へ書き換え
3. `src/backend/task_backend.py`
4. `src/backend/database.py`
5. `scripts/entrypoint.sh` — `brain` 起動時に `copilot` コマンド存在確認を追加
6. `docs/phase4-llm-integration-destructive-test-design.md`
7. 関連テスト

### 3.2 LLM クライアント（Copilot CLI ラッパー）

- `LLMClient` は `generate(prompt: str, metadata: dict | None = None) -> str` インターフェイスを維持する。
- 内部実装は `subprocess.run()` で `copilot` コマンドを呼び出し、stdout を応答として返す。
- `copilot -p` は非対話モードでもエージェントとして動作し、ツール実行時に確認を挟むため、`--allow-all-tools --no-ask-user` を常に付与する。
- ファイルアクセスは無制限にせず、task の `workspace_path` がある場合は `--add-dir {workspace_path}` を付与し、その sandbox 配下に権限を限定する。
- タイムアウトは `subprocess.run(timeout=...)` で制御する。デフォルト 120 秒（CLI 実行は HTTP API より遅いため余裕を持たせる）。
- 非ゼロ終了コードは stderr の内容に基づいてエラー分類する:
  - `copilot: command not found` → `LLMConfigurationError`
  - 認証エラー (401/403 相当) → `LLMAuthenticationError`
  - レートリミット → `LLMRateLimitError`
  - タイムアウト (`subprocess.TimeoutExpired`) → `LLMTimeoutError`
  - その他 → `LLMServiceError`
- stdout が空または空白のみの場合 → `LLMEmptyResponseError`

### 3.3 prompt 構成

- LLM へ渡す prompt は、少なくとも task タイトル、`instruction`、対象リポジトリ、作業ブランチ、期待成果物の種類を含む。
- prompt 先頭で「リポジトリのファイルを直接編集し、変更を完成させる」「git commit / git push はシステム側で行うため実行しない」「リポジトリ外のファイルを編集しない」を明示する。
- prompt 本文に `GITHUB_TOKEN`、DB password、未加工の secret scanner 判定情報は含めない。
- prompt テンプレートは `task_backend` 直書きではなく、専用ヘルパーまたはクライアント側で組み立てられる構造とする。

### 3.4 保存と参照

- `result_summary_md` には Dashboard で一覧表示できる短い要約を保存する。
- 生成全文は `artifacts/llm_response.md` に保存するが、これは監査・参照用の応答ログであり、ファイル書き出しの一次入力ではない。
- 追加カラムが不要な範囲では既存スキーマを優先し、必要になった時点で `result_payload_json` 相当の拡張を再検討する。

### 3.5 ログとマスキング

- `logs.message` には処理段階、失敗分類のみを出し、prompt 全文や応答全文は載せない。
- 応答に secret scanner が検知しうる高エントロピー文字列が含まれる場合でも、まず artifact 保存前にマスキング方針を適用できるよう抽象化する。
- trace id は既存の worker 名を継続利用する。

### 3.6 Dockerfile 変更

- runtime ステージに `copilot` CLI コマンドをインストールする。
- `gh` CLI はホスト側のトークン取得専用であり、コンテナには一切インストールしない。
- `copilot` はシステムレベルでインストールし、`marunage` ユーザーからも実行可能であること。
- インストールはビルド時に行い、ランタイムでのネットワーク依存を減らす。

## 4. 受け入れ条件

1. `brain` が task payload の `instruction` を `copilot` CLI コマンド経由で LLM へ渡し、成功応答を永続化できる。
2. 追加の LLM API キー（`OPENAI_API_KEY` 等）が不要であり、Phase 3 の `GITHUB_TOKEN` のみで認証が完了する。
3. コンテナに `copilot` CLI コマンドがインストールされている（`gh` CLI はホスト側のみ）。
4. 成功時に `logs`、`result_summary_md`、artifact 保存先、commit/push 結果の整合が取れている。
5. `copilot` コマンド未インストール、認証失敗、タイムアウト、空応答で `blocked` へ遷移し、失敗理由が追跡できる。
6. direct-edit 対象 task は repository 内の変更が `working_branch` に commit/push されて `succeeded` へ遷移する。

## 5. 関連ドキュメント

1. `.tdd_protocol.md`
2. `docs/phase3-container-auth-design.md`
3. `docs/phase4-llm-integration-destructive-test-design.md`
4. GitHub Copilot CLI リファレンス: https://docs.github.com/ja/copilot/reference/cli-command-reference

## 6. Legacy Note

Phase 4 の完了済み実装履歴は、旧 `.todo/.tdd_protocol.1.md` ではなく本設計書と `.tdd_protocol.md` の Activity Log を正本とする。`#1` の `.todo` は退役済み。