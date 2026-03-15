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
14. root task の詳細画面では、ユーザー投稿時の本文 (`instruction`) を確認できること。
15. root task の詳細画面では、subtask 一覧をデフォルト非展開のアコーディオンとして表示し、必要な subtask の詳細だけを展開できること。
16. 各 subtask 詳細では、当該 phase 実行に使った LLM モデル名、phase 間 handoff message、phase summary、結果要約、主要ログを確認できること。
17. root task / subtask の詳細画面では、依頼本文、handoff message、result の 3 領域を優先情報として視認しやすく表示できること。
18. Dashboard の task detail UI は日本語ベースの情報設計とし、主要見出し・ラベル・補助文言・empty state は日本語を第一表記に統一すること。
19. 依頼本文、引き継ぎ事項、結果の各パネルでは、背景色と本文文字色が近似しないこと。少なくとも「背景は淡色」「本文は十分に濃い色」という役割分離を持ち、長文でも視線で境界を見失わないこと。
20. detail-reading-panel 系の配色は通常時だけでなく、hover・focus・disabled 相当の状態でも可読性を落とさないこと。補助文言やメタラベルを含めて、本文より弱いが読める階層を維持すること。
21. subtask が `blocked` になった場合、task detail UI は `failed` と混同させず blocked 理由を説明できること。特に `compose_validation_blocked` では、どの compose ファイルのどの field がどの rule_id でブロックされたかをログ詳細として表示できること。

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
12. 可読性: subtask 詳細は初期 collapsed を保ちつつ、展開時には本文・handoff message・result が改行保持で読めること。
13. 監査性: どの subtask がどの LLM モデルで動いたか、どのメッセージを次 phase に渡したかを Dashboard detail だけで追跡できること。
14. 可読性: 依頼本文、handoff message、result は heading、余白、行間、背景コントラスト、最大幅/高さを調整し、長文でも「読む領域」が明確に分離されること。
15. 日本語 UX: `Subtasks`, `Phase Summary`, `Handoff Message`, `Result`, `Logs` などの英語優先ラベルは日本語ベースへ寄せ、必要な英語は補助情報として扱うこと。
16. 一貫性: root detail と subtask detail の情報階層は「概要 → 依頼本文 → サブタスク → ログ/結果」の順で安定させ、ユーザーが日本語の自然な読み順で追跡できること。
17. コントラスト: `instruction` / `handoff_message` / `result_summary_md` の読み物パネルは、背景色と本文文字色の明度差を十分に確保し、背景と本文が同系色に寄って埋もれないこと。見出し、本文、補助文言で色階層を分離すること。
18. 操作状態: 読み物パネルとその周辺メタ情報は hover・focus・disabled の各状態でも本文の可読性を維持し、状態変化が本文コントラストを損なわないこと。
19. 障害判別性: task detail のログ表示は `blocked` と `failed` を区別し、構造化された `details_json` を優先して描画することで、Compose Validation の安全側 block 理由を文字列メッセージだけに依存せず説明できること。

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
- フェーズ固有の指示（phase 0: 壁打ちと要求整理、phase 1: 基本設計、phase 2: 破壊テスト設計、phase 3: テスト実装、phase 4: 本体実装、phase 5: レビュー）

フェーズ固有 prompt の最低要件:
- phase 0 は要件の整理結果を durable な docs へ反映することを主目的とし、いきなり本体実装へ進まない。
- phase 1 は設計書の更新を主目的とし、phase 0 と同一 instruction を再実行するのではなく、前フェーズ成果を踏まえて設計へ収束させる。
- phase 2 は異常系・破壊シナリオを docs に追加する。
- phase 3 はテストコードを先に追加し、RED を作る。
- phase 4 は実装と必要最小限の設計同期を行う。
- phase 5 は review 専用であり、実装変更ではなく APPROVED / REJECTED 判定と指摘の列挙を返す。

### 2.4 commit/push の実装

Phase 5 の `RepositoryWorkspaceManager` にある commit/push ロジックを再利用し、以下のメソッドを追加または修正する:

1. `commit_and_push(workspace_path, working_branch, commit_message)` — diff 抽出を行わず、`git status` で変更検出 → sandbox 検証 → `git add -A` → `git commit` → `git push origin {branch}` を実行する。
2. `validate_changed_files(repo_path)` — `git diff --name-only HEAD` の出力が sandbox 内に収まるか検証する。Phase 5 の `_validate_diff_target` のパス検証ロジックを転用する。
3. GitHub 向けの `git clone` / `git fetch` / `git push` は、`GITHUB_TOKEN` から生成した一時的な HTTP Authorization header を `git -c http.https://github.com/.extraheader=...` として付与する。remote URL の書き換えや token の永続保存は行わない。
4. `prepare_repository()` は毎回 `target_ref` から `working_branch` を作り直してはならない。`git fetch origin --prune` 後に remote `working_branch` の存在を確認し、存在する場合は `origin/{working_branch}` を起点に local `working_branch` を復元する。remote に存在しない場合のみ `target_ref` 起点で `working_branch` を作成する。これにより既存 branch の先行更新を取り込み、`git push origin {working_branch}` の non-fast-forward 拒否を防ぐ。

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
8. task 一覧は orchestration の root task のみを表示し、子 task は親 task の詳細画面で確認できること。詳細画面では phase 順に subtask 一覧を表示し、現在フェーズと直近完了フェーズを追跡できること。

#### 2.7.6 Dashboard root detail / subtask detail

1. root task detail API は `instruction` を返し、詳細画面上で「依頼本文」として表示する。
2. root task detail API は `subtasks` を phase 順配列で返し、各 subtask に `id`, `phase`, `task_type`, `status`, `llm_model`, `handoff_message`, `phase_summary`, `result_summary_md`, `logs` を含める。
3. フロントエンドは各 subtask をアコーディオンで描画し、初期状態ではすべて collapsed とする。
4. アコーディオンのヘッダには少なくとも phase 番号、task_type、status、LLM モデル名を表示する。
5. 展開領域には `phase_summary`, `handoff_message`, `result_summary_md`, 主要ログを表示する。
6. `handoff_message` は「前 phase から次 phase へ渡した要点」であり、UI では phase 境界ごとに読めるよう subtask 単位で表示する。
7. 詳細本文は markdown を前提にせず、まずはプレーンテキスト + 改行保持で安全に描画する。
8. root detail では `instruction`、各 subtask の `handoff_message`、`result_summary_md` を他の補助情報より視覚的に強いテキストブロックとして配置し、読み始める場所が即座に分かるようにする。
9. 見出しは日本語を第一表記とし、少なくとも `依頼本文`、`サブタスク`、`引き継ぎ事項`、`結果`、`ログ` を用いる。必要な英語メタデータは phase 番号や model 名など識別用途に限定する。
10. 長文テキスト領域は単なる `pre` の羅列ではなく、行間・padding・背景色・境界線・最大高さを調整した「読み物向けパネル」として描画する。
11. subtask 展開領域の情報順は `phase summary`、`handoff message`、`result_summary_md`、`logs` を基本とするが、日本語 UI では利用者理解を優先し、表示ラベルは「要約」「引き継ぎ事項」「結果」「ログ」へ置換してよい。
12. empty state と helper text は日本語で記述し、エラー時も英語由来の内部用語をそのまま露出しない。
13. `instruction`、`handoff_message`、`result_summary_md` を描画する `.detail-reading-panel` 系コンポーネントは、配色トークンを本文レイヤ、見出しレイヤ、補助文言レイヤに分離する。本文レイヤは濃色文字、背景は白寄りまたは淡色寄りに固定し、見出しは本文よりさらに強い色で視点の起点を作る。
14. 読み物パネルの border / shadow / background は本文色と競合しない控えめな装飾とし、hover や active 相当の状態変化が入る場合も本文色は維持する。disabled 表示では本文を薄くしすぎず、操作不能であっても内容は読めることを優先する。

## 3. 改修対象

1. `src/backend/task_backend.py` — `_build_prompt()` のガードレール撤去、`_generate_task_result()` の後に commit/push を呼び出すフロー追加
2. `src/backend/repository_workspace.py` — `commit_and_push()`, `validate_changed_files()` 追加、`apply_artifact()` 非推奨化
3. `src/backend/llm_client.py` — 変更なし（generate() インターフェイス維持）
4. `tests/test_worker_engine.py` — prompt ガードレール検証テストを撤去・置換
5. `tests/test_artifact_apply.py` — commit/push テストに転換
6. `docs/phase4-llm-integration-design.md` — §3.3 prompt 方針を更新
7. `src/security/dashboard.py` — 投稿時ブランチ一覧 API、diff / approve / reject API、task detail 拡張
8. `src/security/static/js/app.js` — リポジトリ URL 入力後のブランチ一覧取得 UI、diff プレビュー、approve / reject UI、root task 一覧と detail 内 subtask 表示
9. `src/security/static/index.html` — 投稿フォームの元ブランチコンボボックス、task detail の URL / 元ブランチ表示
10. `src/backend/database.py` — 投稿時選択ブランチの永続化、最終承認対象 task の取得、状態遷移、承認ログ補助
11. `src/backend/task_backend.py` / `src/backend/phase_orchestrator.py` — subtask ごとの `llm_model` / `phase_summary` / `handoff_message` の生成と `payload_json.orchestration` への保存
12. `src/security/static/js/app.js` / `src/security/static/index.html` — `compose_validation_blocked` の violation 詳細表示、blocked reason の日本語ラベル、通常エラーとの差別化 UI

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
19. 既存 remote `working_branch` が存在する場合、次フェーズ再開時はその branch HEAD を継承して commit/push し、`target_ref` 起点への巻き戻しで non-fast-forward を発生させないこと。
20. task 一覧では root task のみが表示され、同一 orchestration の child task により一覧が重複しないこと。
21. root task の詳細画面では child task 一覧が phase 順に表示され、各 child の phase、task_type、status を確認できること。
22. orchestration root task の prompt はフェーズごとに責務が分かれており、phase 1 以降が phase 0 と同一の汎用指示で再実行されないこと。
23. root task の詳細画面でユーザー投稿本文を確認できること。
24. child task 詳細はデフォルト非展開のアコーディオンで表示され、展開時に LLM モデル名、handoff message、phase summary、result、主要ログを確認できること。
25. 依頼本文、handoff message、result は detail 画面で相互に視認しやすい見た目になっており、長文でも読解しやすいこと。
26. Dashboard task detail の主要見出し・ラベル・補助文言・empty state が日本語ベースに統一されていること。
27. 依頼本文、引き継ぎ事項、結果の各パネルは、背景色と本文文字色が近似せず、見出し・本文・補助文言の色階層が明確であること。
28. 読み物パネル周辺の hover / focus / disabled 表示が導入されても、本文のコントラスト低下や背景との同化を起こさないこと。

## 5. 関連ドキュメント

1. `docs/phase4-llm-integration-design.md`
2. `docs/phase6-orchestration-design.md`
3. `docs/phase5-artifact-apply-design.md` (旧設計・参考)
4. `.tdd_protocol.md`
5. `docs/phase5-artifact-apply-design.md` (旧 Artifact Apply 設計・移行前の基準点)
6. `.todo/.tdd_protocol.3.md` (Feature #3 — 承認ワークフロー。旧 apply 承認から direct-edit 承認へ移行)
7. `.todo/.tdd_protocol.4.md` (Feature #4 — フェーズ間遷移。旧 artifact 適用後遷移から direct-edit 後遷移へ移行)
