# 丸投げシステム (Maru-nage v2) フェーズ5 Artifact Apply 設計書

## 0. 目的

本書は、Phase 4 で生成された LLM artifact (`/workspace/{task_id}/artifacts/llm_response.md`) を clone 済みリポジトリへ安全に反映し、作業ブランチへ commit / push するための要件・設計を定義する。

このフェーズでは、永続的な設計判断を本書に記録し、`.tdd_protocol.md` には実行中の目的、直近タスク、進捗のみを記録する。

## 1. 要件定義

1. `brain` は Phase 4 が `waiting_approval` で停止した task について、承認後に `artifacts/llm_response.md` を読み取り、対象リポジトリ (`/workspace/{task_id}/repo/`) へ変更を反映できること。
2. 変更反映は、少なくとも unified diff 形式の artifact を正として扱い、適用対象ファイルが workspace sandbox 外へ脱出しないこと。
3. artifact の適用結果は task 単位で追跡可能であり、成功時は commit / push の結果と合わせて `logs` に記録できること。
4. commit は task ごとの作業ブランチ (`mn2/{task_id}/phase0` など) に対して行い、clone 元と異なる remote へ push しないこと。
5. patch 不正、適用失敗、差分なし、commit 失敗、push 失敗などの異常時は、task を安全側へ倒して `blocked` に遷移させ、原因を `logs` に残すこと。
6. `GITHUB_TOKEN` やその他の秘密情報を artifact、git message、`logs` に平文で残さないこと。

## 1.1 非機能要件

1. セキュリティ: patch 適用先は `/workspace/{task_id}/repo/` 配下に限定し、path traversal や symlink 経由で workspace 外を書き換えない。
2. 可観測性: `artifact_apply_started`、`artifact_apply_succeeded`、`artifact_apply_failed`、`git_push_succeeded` などのイベントを `logs` に残し、どの段階で止まったか追跡できるようにする。
3. 保守性: artifact の解釈、patch 適用、git commit / push は専用コンポーネントへ分離し、`task_backend` に手続き的ロジックを集中させない。
4. 互換性: Phase 3 の repository clone / branch 準備フロー、Phase 4 の artifact 保存方式、workspace sandbox と衝突しない。
5. 回復性: 一時的な push 失敗は限定リトライ余地を残すが、patch 不正や apply 失敗のような恒久障害では無限リトライしない。
6. 監査性: 何を適用し、どの commit を生成し、どの remote / branch へ push したかを task 単位で追跡できる。

## 2. 基本設計

### 2.1 処理フロー

1. `brain` は `waiting_approval` の task を対象に、承認済みであることを確認して artifact apply フローへ進む。
2. `brain` は `/workspace/{task_id}/artifacts/llm_response.md` を読み取り、正規フォーマットか検証する。
3. artifact が unified diff として解釈可能であれば、`/workspace/{task_id}/repo/` 配下に対して patch を適用する。
4. patch 適用後に `git status --short` で差分有無を確認し、差分がある場合のみ commit を作成する。
5. commit 後は clone 元 remote と task の `working_branch` を使って push する。
6. 成功時は task を次状態へ進め、失敗時は `blocked` に遷移させる。

### 2.2 artifact の正規フォーマット

- Phase 5 初期実装では unified diff を唯一の正規フォーマットとする。
- artifact に説明文やコードフェンスが混在する可能性があるため、抽出対象は `--- a/...` / `+++ b/...` / `@@` を含む diff セクションとする。
- diff セクションが抽出できない artifact は apply 不可能として `blocked` にする。
- 将来、ファイル全文生成や JSON manifest 形式を追加する余地は残すが、本フェーズでは扱わない。

### 2.3 git 操作方針

- 適用先ブランチは task に保存された `working_branch` を正とする。
- push 先 remote は clone 時に使用した `origin` のみとし、artifact や task payload が任意 remote を上書きできないようにする。
- commit message は task タイトルまたは `result_summary_md` を基に生成する。自由形式の artifact 本文をそのまま commit message へ使わない。
- 差分なしの場合は commit / push を行わず、`artifact_apply_no_changes` を記録して `blocked` とするか、別状態にするかは `dt` で異常系として確定する。

### 2.4 状態遷移方針

- Phase 4 の正常終端は `waiting_approval` とし、Phase 5 はそこから開始する。
- 正常系の候補は `waiting_approval` -> `running` -> `succeeded` とする。
- 異常系は `waiting_approval` または `running` から `blocked` へ遷移させる。
- apply / commit / push を段階別に観測できるよう、状態遷移とは別に `logs` のイベントで詳細を残す。
- 承認モデルや UI 操作の詳細は本書では扱わず、Phase 5 では「承認済み task に対して apply 実行できること」を前提条件とする。

## 3. 詳細設計

### 3.1 改修対象

1. `src/backend/task_backend.py`
2. `src/backend/repository_workspace.py`
3. `src/backend/code_writer.py` または patch 適用専用モジュール
4. `src/backend/database.py`
5. 関連テスト
6. `docs/phase5-artifact-apply-destructive-test-design.md`

### 3.2 artifact 解析

- `ArtifactApplyService` 相当の専用コンポーネントを導入し、artifact 読み取りと diff 抽出を担当させる。
- 説明文付き Markdown から diff セクションを抽出する。
- `a/` / `b/` パスを検証し、絶対パス、`..`、空パス、`.git/` 配下への変更を拒否する。
- binary patch や rename / delete を初期実装で許可するかは `dt` で異常系を詰めて確定する。

### 3.3 patch 適用

- patch 適用は git 互換の unified diff を使う。
- 適用前に対象ファイルパスを workspace sandbox で再検証する。
- 適用失敗時は repo を中途半端な状態に残さないよう、適用単位を task 単位で閉じ込める。
- 適用後は `git diff --name-only` または `git status --short` で変更ファイル一覧を取得し、`logs` には件数と代表ファイルのみを残す。

### 3.4 commit / push

- commit 前に `user.name` / `user.email` の既定値を定義する。
- commit message は task タイトル優先、なければ `result_summary_md` の先頭行を使用し、長さ制限を設ける。
- push は `git push origin {working_branch}` を明示的に実行し、追跡先の自動決定に依存しない。
- push 失敗時は stderr を分類し、認証失敗・ネットワーク失敗・remote 拒否を区別できるようにする。

### 3.5 ログと永続化

- `logs.message` には patch 全文や secret を入れず、件数・失敗分類・生成 commit SHA・push 先 branch のみを記録する。
- `tasks.result_summary_md` は Phase 4 の要約を維持し、必要なら `result_payload_json` へ commit SHA や changed_files を拡張する。
- Dashboard で利用する表示項目の拡張は別フェーズとし、本フェーズでは backend の永続化契約を定める。

## 4. 受け入れ条件

1. `waiting_approval` の task に対して `llm_response.md` の unified diff を repo へ適用できる。
2. 適用結果が task の `working_branch` に commit され、`origin` へ push できる。
3. patch のパスが workspace 外や `.git/` を指す場合は拒否され、task は `blocked` になる。
4. patch 不正、apply 失敗、commit 失敗、push 失敗の各ケースで原因が `logs` に残る。
5. `GITHUB_TOKEN` やその他 secret が artifact、commit message、`logs` に平文で残らない。
6. Phase 4 の artifact-only 制約と矛盾せず、Phase 5 が artifact を一次ソースとして repo 反映を担当する。

## 5. 関連ドキュメント

1. `docs/phase4-llm-integration-design.md`
2. `.tdd_protocol.md`
3. `docs/phase5-artifact-apply-destructive-test-design.md`
4. `.todo/.tdd_protocol.3.md`
5. `.todo/.tdd_protocol.6.md`