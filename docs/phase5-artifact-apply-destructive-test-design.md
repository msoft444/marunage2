# 丸投げシステム (Maru-nage v2) フェーズ5 Artifact Apply 破壊的テスト設計書

## 0. 目的

本書は、Phase 5（Artifact Apply）の設計を論理的に破壊し、RED テストで保証すべき異常系と合格基準を定義する。対象設計書は `docs/phase5-artifact-apply-design.md`。

## 1. テスト方針

1. artifact 解析、patch 適用、git commit / push の各段階で暗黙の前提が崩れた時に安全に失敗することを定義する。
2. sandbox 脱出（path traversal、symlink、`.git/` 改変）は最優先で防御し、失敗原因は全て `blocked` + `logs` で追跡可能にする。
3. `GITHUB_TOKEN` やその他の秘密情報が diff 内容、commit message、`logs` に平文で残らないことを保証する。
4. 異常は task 単位で閉じ込め、worker 全体を巻き込まないことを検証する。

## 2. 破壊シナリオと合格基準

### DT-01: artifact ファイル不在

- 攻撃シナリオ: `waiting_approval` の task に対して apply を開始するが、`/workspace/{task_id}/artifacts/llm_response.md` が存在しない（削除済み、パス不一致、ディスク障害）。
- Red 条件: `FileNotFoundError` が worker 外へ伝播するか、空 artifact として成功扱いになる。
- Green 条件: artifact 読み取り時点で不在を検知し、`artifact_apply_failed` を `logs` に残して `blocked` へ遷移する。

### DT-02: artifact に unified diff セクションなし

- 攻撃シナリオ: artifact が自然言語の説明文のみで、`--- a/` / `+++ b/` / `@@` を含む diff セクションが一切ない。
- Red 条件: 空 patch として成功扱いになる、または diff 抽出が例外を投げて worker を停止させる。
- Green 条件: diff セクション不在を検知し、`artifact_apply_failed` (原因: `no_diff_section`) を `logs` に残して `blocked` へ遷移する。

### DT-03: diff のハンクヘッダ不正

- 攻撃シナリオ: artifact に `--- a/` / `+++ b/` は存在するが、`@@` ハンクヘッダが欠損、行番号が負値、または開始・長さの形式が壊れている。
- Red 条件: 不正ハンクを無視して部分適用するか、パーサーが未処理例外を投げる。
- Green 条件: diff フォーマットバリデーションで不正を検知し、`blocked` へ遷移する。部分適用は行わない。

### DT-04: diff パスに `..` を含む path traversal

- 攻撃シナリオ: diff の `--- a/../../../etc/passwd` や `+++ b/../../secret` のように `..` セグメントを含み、`/workspace/{task_id}/repo/` の外を書き換えようとする。
- Red 条件: `repo/` 外のファイルが変更される。
- Green 条件: diff パス検証で `..` を含むパスを拒否し、`artifact_apply_failed` (原因: `path_traversal`) を記録して `blocked` へ遷移する。

### DT-05: diff パスが絶対パス

- 攻撃シナリオ: diff の対象パスが `/etc/passwd` や `/workspace/other_task/repo/secrets.txt` のように絶対パスで指定されている。
- Red 条件: 絶対パスが解決されてターゲットファイル外への書き込みが発生する。
- Green 条件: 絶対パスを拒否し、`blocked` へ遷移する。

### DT-06: diff パスが `.git/` 配下を対象

- 攻撃シナリオ: `+++ b/.git/hooks/pre-commit` や `+++ b/.git/config` のように `.git/` ディレクトリを書き換える diff が含まれる。
- Red 条件: git 内部ファイルが改変され、git hooks によるコード実行やリモート URL の書き換えが発生する。
- Green 条件: `.git/` 配下への変更を拒否し、`blocked` へ遷移する。

### DT-07: diff パスが空文字列

- 攻撃シナリオ: `--- a/` / `+++ b/` の後のパスが空文字列（`+++ b/`）、または空白のみ。
- Red 条件: パスが repo ルート自体として解釈されるか、パーサーがクラッシュする。
- Green 条件: 空パスを不正として拒否し、`blocked` へ遷移する。

### DT-08: patch 適用先ファイルが存在しない

- 攻撃シナリオ: diff が `--- a/nonexistent.py` を修正しようとするが、repo にそのファイルが存在しない。
- Red 条件: 無視して成功扱い、または worker が未処理例外で停止する。
- Green 条件: 適用失敗を検知し、`artifact_apply_failed` (原因: `patch_apply_error`) を記録して `blocked` へ遷移する。repo を中途半端な状態に残さない。

### DT-09: patch 適用後に差分なし

- 攻撃シナリオ: diff が正常に解析・適用されるが、`git status --short` が空（変更が既に適用済み、または diff が no-op）。
- Red 条件: 空 commit を作成して push する。
- Green 条件: 差分なしを検知し、`artifact_apply_no_changes` を `logs` に記録する。commit / push は行わず、task を `blocked` に遷移させる（§2.3 で確定）。

### DT-10: commit 失敗 — user.name / user.email 未設定

- 攻撃シナリオ: コンテナ環境に `user.name` / `user.email` が未設定で `git commit` が拒否される。
- Red 条件: 未処理例外が worker 外へ伝播する、または差分が staged のまま放置される。
- Green 条件: commit 前に既定値を設定するか、commit 失敗を捕捉して `blocked` へ遷移する。

### DT-11: commit message に秘密情報が混入

- 攻撃シナリオ: task タイトルや `result_summary_md` に `GITHUB_TOKEN` や認証トークン断片が含まれており、それが commit message にそのまま使用される。
- Red 条件: git history に秘密情報が平文で残る。
- Green 条件: commit message 生成時に Phase 4 同等のマスキングを適用し、秘密情報を除去する。

### DT-12: push 認証失敗

- 攻撃シナリオ: `GITHUB_TOKEN` が失効しており、`git push origin {working_branch}` が認証エラーで失敗する。
- Red 条件: 無制限リトライ、または task が `succeeded` 扱いになる。
- Green 条件: push 認証失敗を分類して `blocked` へ遷移し、`logs` にエラー種別を残す。

### DT-13: push がリモートに拒否される

- 攻撃シナリオ: ブランチ保護ルールや force-push 禁止により `git push` が remote から拒否される。
- Red 条件: worker がハングするか、一時障害として無制限リトライする。
- Green 条件: remote 拒否を恒久障害として分類し、`blocked` へ遷移する。

### DT-14: artifact が任意 remote を注入

- 攻撃シナリオ: artifact 本文中に `git remote add malicious ...` や diff パスに remote URL が含まれ、push 先を上書きしようとする。
- Red 条件: clone 元以外の remote へ push が発生する。
- Green 条件: push 先は常に `origin` をハードコードし、artifact や task payload による remote の追加・変更を許容しない（§2.3）。

### DT-15: task の初期状態が `waiting_approval` 以外

- 攻撃シナリオ: `queued` や `succeeded` 状態の task に対して artifact apply が呼ばれる。
- Red 条件: 二重適用、または状態不整合が発生。
- Green 条件: `waiting_approval` 以外の task は apply 対象外としてスキップし、`logs` に不正遷移の試行を記録する。

### DT-16: workspace_path が不正（sandbox 逸脱）

- 攻撃シナリオ: `tasks.workspace_path` が DB 操作で `/tmp/evil` へ改変されている。
- Red 条件: workspace 外のディレクトリで patch 適用・commit・push が実行される。
- Green 条件: Phase 3/4 同等の sandbox 再検証を apply フロー開始時に実施し、不正パスなら `blocked` へ遷移する。

### DT-17: diff 本文に秘密情報が含まれる

- 攻撃シナリオ: LLM が生成した diff の追加行に `GITHUB_TOKEN` の実値やサービスキーが埋め込まれている。
- Red 条件: 秘密情報を含むファイルが commit / push され、git history に残る。
- Green 条件: diff 適用前またはcommit 前に秘密情報マスキングを適用するか、秘密情報検知時に `blocked` へ遷移する。

### DT-18: 巨大 diff による資源枯渇

- 攻撃シナリオ: artifact に数万行の diff が含まれ、patch 適用やファイル I/O でメモリ・ディスクを圧迫する。
- Red 条件: worker が OOM で死ぬか、ディスク枯渇で他 task を巻き込む。
- Green 条件: artifact サイズに上限を設け、超過時は `blocked` へ遷移する。Phase 4 の応答サイズ上限と整合させる。

### DT-19: symlink 経由の sandbox 脱出

- 攻撃シナリオ: `/workspace/{task_id}/repo/` 配下に workspace 外を指す symlink が事前に作成されており、diff がそのシンボリックリンク先を改変する。
- Red 条件: symlink 解決後の実パスが sandbox 外を指し、ファイルが改変される。
- Green 条件: patch 適用先パスの実パス (`realpath`) を検証し、sandbox 外を指す場合は拒否して `blocked` へ遷移する。

## 3. RED で確認すべき項目

1. artifact 不在、diff セクションなし、ハンクヘッダ不正で `blocked` に遷移すること（DT-01〜03）。
2. `..`、絶対パス、`.git/`、空パス、symlink を含む diff パスが拒否されること（DT-04〜07, DT-19）。
3. patch 適用失敗、差分なしで `blocked` に遷移し、repo が中途半端な状態に残らないこと（DT-08〜09）。
4. commit / push 失敗が task 単位で閉じ込められ、worker が次 task を処理可能なこと（DT-10, DT-12〜13）。
5. `GITHUB_TOKEN` やその他秘密情報が commit message、diff 本文、`logs` に平文で残らないこと（DT-11, DT-17）。
6. artifact や task payload による remote 注入が不可能なこと（DT-14）。
7. `waiting_approval` 以外の task に対して apply が実行されないこと（DT-15）。
8. Phase 3/4 同等の workspace sandbox 再検証が apply フローでも適用されること（DT-16）。
9. 巨大 artifact がサイズ上限で拒否されること（DT-18）。

## 4. 設計へのフィードバック

§2.3 で未確定だった「差分なし時の振る舞い」を本書で確定する:

- **DT-09 確定**: diff が no-op の場合、commit / push は行わず `artifact_apply_no_changes` を `logs` に記録して `blocked` に遷移する。空 commit は許容しない。

§3.2 の binary patch / rename / delete の初期扱いを本書で確定する:

- **初期実装**: rename (`rename from` / `rename to`)、delete (`deleted file mode`)、binary patch (`Binary files`) は初期実装では対応しない。これらが含まれる diff は `unsupported_diff_operation` として `blocked` に遷移する。将来サポート時にテスト追加で緩和する。

## 5. 関連ドキュメント

1. `docs/phase5-artifact-apply-design.md`
2. `docs/phase4-llm-integration-destructive-test-design.md`
3. `.tdd_protocol.md`
