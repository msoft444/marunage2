# 丸投げシステム (Maru-nage v2) フェーズ4 LLM 連携 破壊的テスト設計書

## 0. 目的

本書は、フェーズ4の LLM 連携設計を論理的に破壊し、RED テストで保証すべき異常系と合格基準を定義する。

## 1. テスト方針

1. 正常応答だけを確認するのではなく、認証、CLI 実行、保存、状態遷移、秘密情報保護の暗黙前提が崩れた時に安全に失敗することを定義する。
2. 失敗は可能な限り `brain` ワーカー内で明示的に分類し、`blocked` と `logs` によって再現可能にする。
3. `GITHUB_TOKEN` 漏洩、`copilot` CLI 障害、DB 不整合、巨大応答、過剰リトライによる worker 停滞を重点的に検証する。

## 2. 破壊シナリオと合格基準

### DT-01: `copilot` コマンド未インストール

- 攻撃シナリオ: コンテナに `copilot` CLI コマンドがインストールされていない。
- Red 条件: `brain` が `FileNotFoundError` 等の曖昧な例外で落ちるか、無限リトライを起こす。
- Green 条件: 呼び出し前に `copilot` コマンドの不在を検知し、タスクを `blocked` に遷移させ、理由を `logs` に残す。

### DT-02: 認証失敗（GITHUB_TOKEN 無効）

- 攻撃シナリオ: `GITHUB_TOKEN` が無効、失効済み、または Copilot ライセンスなしのトークン。
- Red 条件: 一時障害として無制限に再試行し、worker が詰まる。
- Green 条件: 恒久障害として分類し、限定回数以内で処理を打ち切って `blocked` に遷移する。

### DT-03: レートリミット

- 攻撃シナリオ: Copilot API がレートリミットを返し、`copilot` コマンドが該当エラーで終了する。
- Red 条件: 即時 `failed` になる、または無限リトライする。
- Green 条件: 設計で定義した回数だけリトライし、上限到達後は `blocked` として失敗理由を記録する。

### DT-04: タイムアウト

- 攻撃シナリオ: `copilot` プロセスが応答しない、または極端に遅い。
- Red 条件: worker スレッドがハングし、lease 失効や queue 停滞を招く。
- Green 条件: `subprocess.run(timeout=...)` でプロセスを打ち切り、タスクを `blocked` に落とし、次周期へ影響を残さない。

### DT-05: 空応答

- 攻撃シナリオ: `copilot` コマンドが正常終了(exit 0)するが stdout が空、または空白のみ。
- Red 条件: 成功扱いで `waiting_approval` または `succeeded` に進む。
- Green 条件: 無効応答として `blocked` に遷移し、保存や後続フェーズへ進めない。

### DT-06: 巨大応答

- 攻撃シナリオ: `copilot` コマンドが非常に大きい stdout を返し、DB やメモリ、artifact 保存容量を圧迫する。
- Red 条件: `logs` や `result_summary_md` に全文を書き込み、永続層や UI が破綻する。
- Green 条件: 要約と artifact を分離し、サイズ上限を超える部分は切り詰めまたは保存拒否して `blocked` へ遷移できる。

### DT-07: 秘密情報混入

- 攻撃シナリオ: prompt または `copilot` コマンドの応答に `GITHUB_TOKEN`、高エントロピー文字列、認証トークン断片が含まれる。
- Red 条件: `logs`、`result_summary_md`、artifact に無加工で保存される。
- Green 条件: 保存前にマスキングまたは拒否され、平文の秘密情報が永続層へ残らない。

### DT-08: DB 不整合

- 攻撃シナリオ: `tasks.workspace_path` や `target_repo` が不整合な状態で LLM 実行保存処理に入る。
- Red 条件: 正規化されていないパスへ artifact を書き込み、workspace 外へ脱出する。
- Green 条件: 既存の sandbox と path 正規化で検知し、`blocked` へ遷移する。

### DT-09: CLI プロセス異常終了

- 攻撃シナリオ: `copilot` プロセスがシグナルで kill される、segfault する、または予期しない終了コードを返す。
- Red 条件: 例外が worker 外まで伝播して cycle 全体が不安定になる。
- Green 条件: CLI 障害として分類し、タスク単位で `blocked` に閉じ込め、worker 自体は次タスクを処理可能な状態を維持する。

### DT-10: GITHUB_TOKEN の不要なサービスへの LLM 利用拡散

- 攻撃シナリオ: `dashboard`、`guardian`、`librarian` が `copilot` コマンドを実行して LLM を呼び出す。
- Red 条件: `brain` 以外のサービスから LLM 呼び出しが発生する。
- Green 条件: `copilot` CLI は `brain` コンテナのみにインストールされるか、`brain` 以外のサービスからは `copilot` が呼ばれない設計であること。

### DT-11: MCP 接続失敗

- 攻撃シナリオ: 外部補助系や将来の MCP 連携が利用不能な状態で、`copilot` CLI 呼び出しも同時に不安定になる。
- Red 条件: 障害原因が混線し、どの依存が失敗したか分からない。
- Green 条件: `copilot` CLI 由来の失敗は専用イベントで切り分けられ、MCP など他依存の障害と区別できる。

### DT-12: 依頼 payload 欠落

- 攻撃シナリオ: `instruction` が空または不正型の task が queue に存在する。
- Red 条件: 空 prompt を `copilot` コマンドに送信して成功扱いになる。
- Green 条件: 入力不正として `blocked` に遷移し、どのフィールドが欠落していたかを `logs` で追跡できる。

## 3. RED で確認すべき項目

1. `brain` が `copilot` コマンド未インストール、認証失敗、タイムアウト、空応答、巨大応答で `blocked` に遷移すること。
2. `logs` に失敗分類と理由が残り、prompt 全文や `GITHUB_TOKEN` が残らないこと。
3. 成功時でも全文は `logs` に入れず、要約と artifact を分離して保存すること。
4. `copilot` CLI 由来の障害が worker 全体ではなく task 単位の失敗に閉じること。
5. 不正な workspace/path、空 instruction が worker 全体ではなく task 単位の失敗に閉じること。

## 4. 関連ドキュメント

1. `docs/phase4-llm-integration-design.md`
2. `.tdd_protocol.md`
3. GitHub Copilot CLI リファレンス: https://docs.github.com/ja/copilot/reference/cli-command-reference

補足: Phase 4 の完了済み作業履歴は旧 `.todo/.tdd_protocol.1.md` から `.tdd_protocol.md` と本設計書群へ移管済み。