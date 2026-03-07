# 丸投げシステム (Maru-nage v2) フェーズ3 コンテナ認証・起動導線設計書

## 0. 目的

本書は、`.copilot` のホストマウントと `secrets/github_token` 依存を廃止し、ホスト側で `gh auth token` を評価して `GITHUB_TOKEN` を各コンテナへ動的注入する運用へ切り替えるための要件・設計を定義する。

このフェーズでは、永続的な設計判断を本書に記録し、`.tdd_protocol.md` には実行中の目的、直近タスク、進捗のみを記録する。

## 1. 要件定義

1. 外部 LLM API は使用しない。GitHub 認証は `gh auth token` で取得した `GITHUB_TOKEN` のみを利用する。
2. コンテナはホストの `.copilot` ディレクトリに依存してはならない。
3. `secrets/github_token` ファイルは廃止し、トークンをファイルへ永続化しない。
4. `brain`、`guardian`、`dashboard`、`librarian` の各コンテナは、起動時に `GITHUB_TOKEN` を環境変数として受け取れること。
5. ホスト側で `gh` が未導入、未ログイン、または空トークンを返した場合は、コンテナ起動前に即座に失敗させること。

## 1.1 非機能要件

1. セキュリティ: GitHub トークンはファイルへ永続化せず、Maru-nage の実行対象コンテナ群の外へ漏らさない。
2. 可観測性: 起動失敗はホスト側で即時に判定でき、失敗理由を標準エラーへ明示する。
3. 運用性: 開発者は `gh auth login` 済みであれば追加の secret ファイル作成なしに起動できる。
4. 保守性: 認証経路は `gh auth token` -> `GITHUB_TOKEN` に一本化し、`.copilot` マウントや `github_token` secret との二重運用を残さない。
5. 互換性: 既存の DB secret 運用や他サービスの起動要件には不要な変更を加えない。

## 2. 基本設計

### 2.1 認証方式

- ホスト側の起動スクリプトまたはタスクが `gh auth token` を実行する。
- 取得したトークンは、そのプロセスの環境変数 `GITHUB_TOKEN` として `docker compose` 実行時に注入する。
- コンテナ内では `GITHUB_TOKEN_FILE` を使用せず、`GITHUB_TOKEN` を直接参照する。

### 2.2 コンテナ構成

- `docker-compose.prod.yml` から `.copilot` のホストマウント設定を削除する。
- `docker-compose.prod.yml` から `github_token` secret と `GITHUB_TOKEN_FILE` 設定を削除する。
- `brain`、`guardian`、`dashboard`、`librarian` は `GITHUB_TOKEN` を必須環境変数として扱う。
- `GITHUB_TOKEN` は Maru-nage のアプリケーションコンテナ群へ統一的に注入し、`GITHUB_TOKEN_FILE` や secret ファイルとの二重運用を残さない。

### 2.3 障害時の扱い

- `gh` コマンドが存在しない場合は、起動スクリプトが理由を表示して終了する。
- `gh auth token` が失敗した場合は、コンテナを起動しない。
- `GITHUB_TOKEN` が空文字列の場合は、コンテナを起動しない。

## 3. 詳細設計

### 3.1 改修対象

1. `docker-compose.prod.yml`
2. `scripts/entrypoint.sh`
3. 起動用スクリプトまたはタスク定義
4. `scripts/init_runtime.sh`
5. `scripts/README-runtime.txt`
6. 関連テスト

### 3.2 期待される変更

- Compose は `env_file` と実行時環境変数の組み合わせで `GITHUB_TOKEN` を受け取る。
- `entrypoint.sh` は `GITHUB_TOKEN_FILE` なしでも動作し、各アプリケーションサービスで `GITHUB_TOKEN` の存在を検証する。
- 初期化スクリプトと運用手順書は `secrets/github_token` の作成や記入を要求しない。
- テストは、secret 依存の除去、`GITHUB_TOKEN` 注入、起動前失敗条件の検証をカバーする。

## 4. 受け入れ条件

1. `docker-compose.prod.yml` に `.copilot` マウント設定が存在しない。
2. `docker-compose.prod.yml` に `github_token` secret と `GITHUB_TOKEN_FILE` が存在しない。
3. ホスト側の起動導線が `gh auth token` を使って全アプリケーションコンテナ向けの `GITHUB_TOKEN` を注入できる。
4. `gh` 未導入・未ログイン・空トークンの各ケースで、コンテナ起動前に失敗する。
5. 運用ドキュメントが新しい起動手順を説明している。

## 5. 関連ドキュメント

1. `docs/phase3-destructive-test-design.md`
2. `.tdd_protocol.md`
