# 丸投げシステム (Maru-nage v2) 直接編集アーキテクチャ設計書

## 0. 目的

本書は、Phase 4/5 の artifact 経由間接編集アーキテクチャを廃止し、copilot CLI がクローン済みリポジトリを直接編集し、各フェーズ終了時に commit & push する方式へ転換するための要件・設計を定義する。

### 0.1 背景 — なぜ転換するか

Phase 4 は copilot CLI に `--allow-all-tools --add-dir` を渡しながら、prompt で「ファイル編集するな」と制約し、提案テキスト（artifact）だけを返させていた。Phase 5 はその artifact から unified diff を抽出し `git apply` で適用する設計だった。

この設計には以下の構造的問題がある:

1. copilot は unified diff を常に返すとは限らず、全文提案が来ると Phase 5 が `blocked` になる（task #12 で発生）。
2. copilot の直接編集能力を意図的に殺し、diff パーサー＋patch 適用として自前で再実装している。
3. 各フェーズが独立してリポジトリを更新し commit/push する、という本来の設計意図が実現されていない。

## 1. 要件定義

1. copilot CLI はクローン済みリポジトリ (`/workspace/{task_id}/repo/`) のファイルを直接編集できること。
2. 各フェーズ（0〜5）の終了時に、copilot が行った変更を作業ブランチへ commit し、リモート `origin` へ push すること。
3. commit/push は Phase 3 で確立した `working_branch` に対して行い、push 先 remote は `origin` 固定とすること。
4. commit 前に変更差分の安全性検証（sandbox 外への書き込み、`.git/` 改変、symlink 脱出の検出）を行うこと。
5. 変更なし（copilot が何も編集しなかった）の場合は空 commit を作成せず、ログに記録して次フェーズへ進むこと。
6. commit/push 失敗は task を `blocked` に遷移させ、原因を `logs` に残すこと。
7. `GITHUB_TOKEN` やその他の秘密情報が commit message や `logs` に平文で残らないこと。

## 1.1 非機能要件

1. セキュリティ: copilot の編集範囲は `--add-dir` で `/workspace/{task_id}/repo/` に制限する。commit 前に `git diff --name-only` で変更ファイルパスを検証し、sandbox 外パスが含まれる場合は拒否する。
2. 可観測性: `phase_N_started`, `phase_N_edit_completed`, `git_commit_succeeded`, `git_push_succeeded`, `phase_N_no_changes` などのイベントを `logs` に残す。
3. 保守性: commit/push ロジックは `RepositoryWorkspaceManager` に集約し、Phase 5 の `apply_artifact()` の commit/push 部分を再利用する。
4. 互換性: Phase 3 の clone/branch 準備フロー、`WorkspaceSandbox` は変更なしで再利用する。Phase 4 の `LLMClient` は `generate()` インターフェイスを維持し、prompt のみ変更する。
5. 後方互換: artifact ファイル (`llm_response.md`) は引き続き生成してもよいが、ファイル反映の一次手段としては使わない。copilot の応答ログとして保存するのは許容する。
6. 認証保全: `GITHUB_TOKEN` は `.git/config` や remote URL へ永続化せず、GitHub HTTPS 通信時に一時的な HTTP Authorization header として渡すこと。

## 2. 基本設計

### 2.1 処理フロー（タスク受付〜フェーズ実行〜完了）

```
1. タスク受付 → queued
2. brain が lease → leased → running
3. clone & branch 準備 (/workspace/{task_id}/repo/, branch: mn2/{task_id}/phase0)
4. フェーズ 0 実行:
   a. copilot CLI に instruction + --add-dir {repo_path} を渡す
   b. copilot がリポジトリ内のファイルを直接編集
   c. git diff --name-only で変更検出（なければスキップ）
   d. 変更パスの sandbox 検証
   e. git add → git commit → git push origin {working_branch}
   f. logs にイベント記録
5. フェーズ 1〜5: 同様に copilot 呼び出し → commit → push
6. 全フェーズ完了 → succeeded
```

### 2.2 Phase 4 からの変更点

| 項目 | 旧（Phase 4/5） | 新（直接編集） |
|---|---|---|
| copilot prompt | 「ファイル編集するな」 | 「リポジトリを直接編集せよ」 |
| ファイル反映 | artifact → diff 抽出 → git apply | copilot が直接編集済み |
| commit タイミング | Phase 5 で一括 | 各フェーズ終了時 |
| artifact の役割 | 変更の一次ソース | 応答ログ（参考保存のみ） |

### 2.3 prompt 設計（Phase 4 からの変更）

Phase 4 の `_build_prompt()` から以下のガードレールを **撤去** する:
- ~~"You are generating a proposal artifact only."~~
- ~~"Do not edit files."~~
- ~~"Do not run git push."~~
- ~~"Do not run commands that modify the repository."~~
- ~~"Return only the proposed content or patch in markdown."~~

新しい prompt 方針:
- 「リポジトリのファイルを直接編集し、変更を完成させよ」
- 「git commit / git push は実行するな（システム側で行う）」
- 「リポジトリ外のファイルを編集するな」
- フェーズ固有の指示（設計書作成、テスト設計、テスト実装、本体実装、レビュー）

### 2.4 commit/push の実装

Phase 5 の `RepositoryWorkspaceManager` にある commit/push ロジックを再利用し、以下のメソッドを追加または修正する:

1. `commit_and_push(workspace_path, working_branch, commit_message)` — diff 抽出を行わず、`git status` で変更検出 → sandbox 検証 → `git add -A` → `git commit` → `git push origin {branch}` を実行する。
2. `validate_changed_files(repo_path)` — `git diff --name-only HEAD` の出力が sandbox 内に収まるか検証する。Phase 5 の `_validate_diff_target` のパス検証ロジックを転用する。
3. GitHub 向けの `git clone` / `git push` は、`GITHUB_TOKEN` から生成した一時的な HTTP Authorization header を `git -c http.https://github.com/.extraheader=...` として付与する。remote URL の書き換えや token の永続保存は行わない。

### 2.5 Phase 5 (`apply_artifact`) の扱い

- Phase 5 の `apply_artifact()` は **非推奨** とし、呼び出しパスを削除する。
- Phase 5 の sandbox 検証ロジック (`_validate_diff_target`, `_mask_secrets`, `_build_commit_message`) は `commit_and_push` で再利用する。
- `ArtifactApplyError` 例外クラスは `CommitPushError` にリネームまたは統合する。
- `apply_artifact_for_task()` は `commit_and_push_for_phase()` に置き換える。

### 2.6 状態遷移方針

- フェーズ遷移の検討は Feature #4（Phase Orchestration）に委ねるが、直接編集アーキテクチャの前提として以下を定義する:
- 各フェーズの copilot 呼び出し後、commit/push が成功したら次フェーズへ進む。
- commit/push が失敗したら `blocked` に遷移する。
- 承認 (Feature #3) は特定フェーズの完了後（例: フェーズ 4 の実装完了後、ユーザーに確認を求める）に挟む。承認前に Dashboard で `git diff` のプレビューを表示する。

## 3. 改修対象

1. `src/backend/task_backend.py` — `_build_prompt()` のガードレール撤去、`_generate_task_result()` の後に commit/push を呼び出すフロー追加
2. `src/backend/repository_workspace.py` — `commit_and_push()`, `validate_changed_files()` 追加、`apply_artifact()` 非推奨化
3. `src/backend/llm_client.py` — 変更なし（generate() インターフェイス維持）
4. `tests/test_worker_engine.py` — prompt ガードレール検証テストを撤去・置換
5. `tests/test_artifact_apply.py` — commit/push テストに転換
6. `docs/phase4-llm-integration-design.md` — §3.3 prompt 方針を更新

## 4. 受け入れ条件

1. copilot CLI がリポジトリのファイルを直接編集し、その結果が `working_branch` に commit & push されること。
2. commit 前に変更ファイルパスの sandbox 検証が行われ、sandbox 外変更は `blocked` になること。
3. 変更なしの場合は空 commit を作らず、ログに記録して処理を継続すること。
4. commit message に秘密情報が平文で含まれないこと。
5. 既存の clone/branch 準備フロー（Phase 3）と workspace sandbox が変更なしで動作すること。
6. Phase 4 の prompt ガードレール 5 行が撤去されていること。
7. GitHub への clone / push が `GITHUB_TOKEN` で認証され、token が `.git/config` やログへ平文で永続化されないこと。

## 5. 関連ドキュメント

1. `docs/phase4-llm-integration-design.md`
2. `docs/phase5-artifact-apply-design.md` (旧設計・参考)
3. `.tdd_protocol.md`
4. `docs/phase5-artifact-apply-design.md` (旧 Artifact Apply 設計・移行前の基準点)
5. `.todo/.tdd_protocol.3.md` (Feature #3 — 承認ワークフロー。旧 apply 承認から direct-edit 承認へ移行)
6. `.todo/.tdd_protocol.4.md` (Feature #4 — フェーズ間遷移。旧 artifact 適用後遷移から direct-edit 後遷移へ移行)
