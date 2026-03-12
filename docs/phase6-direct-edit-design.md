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
8. local `workspace_path` task は承認なしで処理継続できること。
9. GitHub clone task は最終承認前でも各フェーズ終了時に `working_branch` へ commit/push できること。
10. GitHub clone task の投稿時には、対象リポジトリ URL から取得したブランチ一覧の中から元ブランチを選択できること。
11. 選択した元ブランチは task に永続化され、clone 時の起点、差分比較先、最終承認時の merge 先として一貫して利用されること。
12. 承認後は merge 成功を確認した場合に限り、local / remote の開発用 `working_branch` を削除すること。
13. 承認画面ではマージ対象ブランチの再選択を許可せず、投稿時に選択したリポジトリ URL と元ブランチを task detail に表示すること。

## 1.1 非機能要件

1. セキュリティ: copilot の編集範囲は `--add-dir` で `/workspace/{task_id}/repo/` に制限する。commit 前に `git diff --name-only` で変更ファイルパスを検証し、sandbox 外パスが含まれる場合は拒否する。
2. 可観測性: `phase_N_started`, `phase_N_edit_completed`, `git_commit_succeeded`, `git_push_succeeded`, `phase_N_no_changes` などのイベントを `logs` に残す。
3. 保守性: commit/push ロジックは `RepositoryWorkspaceManager` に集約し、Phase 5 の `apply_artifact()` の commit/push 部分を再利用する。
4. 互換性: Phase 3 の clone/branch 準備フロー、`WorkspaceSandbox` は変更なしで再利用する。Phase 4 の `LLMClient` は `generate()` インターフェイスを維持し、prompt のみ変更する。
5. 後方互換: artifact ファイル (`llm_response.md`) は引き続き生成してもよいが、ファイル反映の一次手段としては使わない。copilot の応答ログとして保存するのは許容する。
6. 認証保全: `GITHUB_TOKEN` は `.git/config` や remote URL へ永続化せず、GitHub HTTPS 通信時に一時的な HTTP Authorization header として渡すこと。
7. 冪等性: approve / reject / merge / branch cleanup は二重実行されても追加副作用を起こさないこと。少なくとも `waiting_approval` 条件付き状態遷移と Git 側の存在確認で重複実行を防ぐこと。
8. 監査性: 投稿時に選択された元ブランチ、承認要求、merge 成否、local / remote ブランチ削除成否を `logs` に構造化イベントとして残すこと。
9. 安全性: 投稿フォームで提示する元ブランチ候補は remote の全ブランチをそのまま見せず、allowlist 条件を満たした候補に限定すること。
10. 操作性: Dashboard の投稿フォームはリポジトリ URL 入力後に自然にブランチ候補を提示し、承認画面では追加選択なしで承認できること。
11. 一貫性: 投稿後に remote 側ブランチ一覧が変化しても、task に保存済みの元ブランチを最終承認まで維持し、承認時に別ブランチへすり替わらないこと。

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
6. GitHub clone task は投稿時に選択された元ブランチを保持したまま全フェーズを進め、最終段のみ waiting_approval
7. 承認時は保存済みの元ブランチへ merge → succeeded
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
- `workspace_path` を直接指定する local repository task はローカルファイルを直接編集する運用とし、承認ゲートは設けない。必要なら各フェーズ終了時にそのまま commit/push またはローカル反映完了として扱う。
- GitHub clone task はタスク開始時に開発用 `working_branch` を作成し、各フェーズ終了時にそのブランチへ commit/push する。
- GitHub clone task の投稿時には、対象リポジトリ URL から allowlist 済み remote branch を取得し、元ブランチを選択して task に保存する。
- 承認 (Feature #3) の対象は GitHub clone task の最終マージのみとする。承認前に Dashboard で `working_branch` と保存済み元ブランチとの差分をプレビュー表示し、承認後にその元ブランチへ merge を実行する。
- 承認時は投稿時に保存した元ブランチへの merge 成功を確認したあと、開発用 `working_branch` を remote / local の両方から削除する。merge 成功前にブランチ削除を行ってはならない。

### 2.7 承認ワークフロー詳細設計

#### 2.7.1 task 種別ごとの扱い

1. local `workspace_path` task:
   local ディレクトリに対する直接編集タスクとして扱う。`approval_required=false` を基本とし、最終承認状態 `waiting_approval` は使用しない。
2. GitHub clone task:
   `/workspace/{task_id}/repo/` に、投稿時に選択した元ブランチを起点として clone / checkout したリポジトリを、開発用 `working_branch` で直接編集する。各フェーズ終了時に commit/push を許可し、最終段のみ `waiting_approval` に遷移する。

#### 2.7.2 Dashboard API

1. `GET /api/v1/repositories/branches?repository_url=...`
   投稿フォーム用の候補ブランチ一覧を返す。候補は allowlist 条件を満たした remote branch のみとし、既定値として `main` が存在すればそれを返す。
2. `GET /api/v1/tasks/{id}/diff`
   `working_branch` と task に保存済みの元ブランチとの差分を返す。比較先は request payload ではなく task の保存値を使う。
3. `POST /api/v1/tasks/{id}/approve`
   最終 `waiting_approval` task に対して、task に保存済みの元ブランチへの merge を実行する。payload でマージ先ブランチを上書きしてはならない。
4. `POST /api/v1/tasks/{id}/reject`
   最終 `waiting_approval` task を `blocked` に遷移させ、却下理由を `logs` に残す。

#### 2.7.3 approve 時の実行順序

1. task を `waiting_approval` で再取得し、対象が GitHub clone task であること、`working_branch` が有効であること、投稿時に保存済みの元ブランチが allowlist 内であることを確認する。
2. `origin/{target_ref}` を fetch し、ローカル repo を最新化する。
3. `target_ref` を checkout し、`origin/{target_ref}` に fast-forward 可能なら同期する。
4. `git merge --no-ff {working_branch}` 相当で merge を実行する。
5. merge 結果を `origin {target_ref}` へ push する。
6. push 成功後にのみ local `working_branch` を削除する。
7. local 削除成功後にのみ remote `working_branch` (`git push origin --delete {working_branch}`) を削除する。
8. 上記 1 〜 7 が完了した時点で task を `succeeded` に遷移させる。

#### 2.7.4 失敗時の扱い

1. merge 競合、push 失敗、保護ブランチ拒否、local / remote ブランチ削除失敗は `blocked` とする。
2. merge が成功していない状態では branch cleanup を実行しない。
3. local branch 削除成功後に remote branch 削除だけ失敗した場合も `blocked` とし、再実行時に remote branch のみを安全に削除できるよう event を区別して記録する。
4. approve の二重送信は task 状態と Git 側の存在確認で吸収し、二重 merge / 二重削除を防ぐ。
5. `working_branch` が local / remote のどちらにも存在しない場合、`diff` / `approve` API は `working_branch_not_found` を返す。フロントエンドは task の最新状態を再取得し、既に `succeeded` または `blocked` なら承認 UI を非表示にする。依然 `waiting_approval` のまま残っている異常系のみ、状態不整合メッセージを表示して操作を無効化する。
6. 投稿時に選択した元ブランチが承認時点で remote から消えている場合、approve は `merge_target_not_found` 相当のエラーで `blocked` とし、別ブランチへ自動フォールバックしてはならない。

#### 2.7.5 Dashboard 承認 UI

1. `waiting_approval` task の詳細画面では、差分表示領域に高コントラストな背景色と文字色を適用し、長い unified diff でも可読性を維持する。
2. approve / reject の押下後は API 応答の task payload または task detail 再取得結果を優先し、承認前に保持していた stale な task 状態で再描画してはならない。
3. task detail には GitHub clone task の `repository_path` と投稿時選択済みの元ブランチを表示し、承認者が merge 先を確認できるようにする。
4. 承認画面ではマージ対象ブランチの select UI を表示せず、approve / reject のみを提供する。
5. task が `succeeded` または `blocked` に遷移した後は承認パネルを非表示にし、approve / reject は表示しない。
6. approve / reject の操作ボタンは視覚的に区別可能な primary / danger スタイルを持ち、単純な横並びではなく承認パネル内でまとまりのあるアクション列として配置する。
7. `working_branch_not_found` は「承認済みの可能性」だけでなく「task 状態の stale 表示」の可能性も考慮し、最新 task 状態が `waiting_approval` の場合にのみ異常表示として扱う。

## 3. 改修対象

1. `src/backend/task_backend.py` — `_build_prompt()` のガードレール撤去、`_generate_task_result()` の後に commit/push を呼び出すフロー追加
2. `src/backend/repository_workspace.py` — `commit_and_push()`, `validate_changed_files()` 追加、`apply_artifact()` 非推奨化
3. `src/backend/llm_client.py` — 変更なし（generate() インターフェイス維持）
4. `tests/test_worker_engine.py` — prompt ガードレール検証テストを撤去・置換
5. `tests/test_artifact_apply.py` — commit/push テストに転換
6. `docs/phase4-llm-integration-design.md` — §3.3 prompt 方針を更新
7. `src/security/dashboard.py` — 投稿時ブランチ一覧 API、diff / approve / reject API、task detail 拡張
8. `src/security/static/js/app.js` — リポジトリ URL 入力後のブランチ一覧取得 UI、diff プレビュー、approve / reject UI
9. `src/security/static/index.html` — 投稿フォームの元ブランチコンボボックス、task detail の URL / 元ブランチ表示
10. `src/backend/database.py` — 投稿時選択ブランチの永続化、最終承認対象 task の取得、状態遷移、承認ログ補助

## 4. 受け入れ条件

1. copilot CLI がリポジトリのファイルを直接編集し、その結果が `working_branch` に commit & push されること。
2. commit 前に変更ファイルパスの sandbox 検証が行われ、sandbox 外変更は `blocked` になること。
3. 変更なしの場合は空 commit を作らず、ログに記録して処理を継続すること。
4. commit message に秘密情報が平文で含まれないこと。
5. 既存の clone/branch 準備フロー（Phase 3）と workspace sandbox が変更なしで動作すること。
6. Phase 4 の prompt ガードレール 5 行が撤去されていること。
7. GitHub への clone / push が `GITHUB_TOKEN` で認証され、token が `.git/config` やログへ平文で永続化されないこと。
8. local repository task は承認なしで処理継続できること。
9. GitHub clone task の投稿フォームで、対象リポジトリ URL から取得した許可済みブランチ一覧をコンボボックス表示できること。
10. 投稿時に選択した元ブランチが task に保存され、clone / diff / approve の全工程で同じ値が使われること。
11. GitHub clone task の承認対象は `working_branch` から投稿時選択済み元ブランチへのマージのみであり、途中の commit/push は承認対象に含めないこと。
12. 承認後、指定ブランチへの merge 成功が確認できた場合のみ、開発用 `working_branch` が local / remote の両方から削除されること。
13. approve API は `working_branch` と投稿時保存済み元ブランチの差分を確認可能な状態でのみ実行されること。
14. reject API は merge や branch cleanup を実行せず、task を `blocked` に遷移させるだけであること。
15. task detail には承認対象のリポジトリ URL と元ブランチが表示され、承認画面にマージ先 select が存在しないこと。
16. `working_branch` が既に削除済みの task では `diff` / `approve` API が `working_branch_not_found` を返し、task の最新状態が `succeeded` または `blocked` なら承認 UI は非表示になること。task の最新状態がなお `waiting_approval` の場合のみ、状態不整合として操作無効メッセージを表示すること。
17. diff preview は背景色と文字色のコントラストが十分で、差分プレビューの可読性を損なわないこと。
18. approve / reject ボタンは視認性の高いスタイルと整理された配置を持ち、意図しない操作を招かないこと。

## 5. 関連ドキュメント

1. `docs/phase4-llm-integration-design.md`
2. `docs/phase5-artifact-apply-design.md` (旧設計・参考)
3. `.tdd_protocol.md`
4. `docs/phase5-artifact-apply-design.md` (旧 Artifact Apply 設計・移行前の基準点)
5. `.todo/.tdd_protocol.3.md` (Feature #3 — 承認ワークフロー。旧 apply 承認から direct-edit 承認へ移行)
6. `.todo/.tdd_protocol.4.md` (Feature #4 — フェーズ間遷移。旧 artifact 適用後遷移から direct-edit 後遷移へ移行)
