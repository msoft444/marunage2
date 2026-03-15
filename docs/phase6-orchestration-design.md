# 丸投げシステム (Maru-nage v2) フェーズ6 オーケストレーション設計書

## 0. 目的

本書は、direct-edit 前提の Phase 6 において、フェーズ 0（壁打ち）→ 1（設計）→ 2（破壊テスト設計）→ 3（テスト実装）→ 4（本体実装）→ 5（監査）を自動連結する orchestration の要件・基本設計を定義する。

本設計は `docs/phase6-direct-edit-design.md` の §2.6 を具体化するものであり、永続的な設計判断を `docs/` 配下へ記録し、`.tdd_protocol.md` には実行中タスクの進捗だけを記録する。

## 1. 要件定義

1. GitHub clone 前提の direct-edit タスクでは、投稿後に phase 0 から phase 5 までの実行タスクを順番に自動生成し、同一 `working_branch` 上で処理を継続できること。
2. orchestration の根となる root task は、全フェーズの進捗・最終結果・監査状態を表す単一の親レコードとして保持されること。
3. 各 phase 実行 task は、直前フェーズ task を `parent_task_id` に持つ子タスクとして生成され、`root_task_id` は root task に固定されること。
4. phase 番号は既存 `tasks.phase` を利用し、phase ごとの種別は `task_type` で区別すること。
5. フェーズ遷移時は `workspace_path`、`target_repo`、`target_ref`、`working_branch`、必要な payload を引き継ぐこと。
6. 各 phase task の prompt には phase 番号と phase 固有の責務を含め、phase 0 と phase 1 以降で同一の汎用 instruction を再実行しないこと。
7. 各フェーズの direct-edit 実行結果が `git_commit_succeeded` / `git_push_succeeded` または `phase_edit_no_changes` で正常終了した場合のみ、次フェーズ task を `queued` で生成すること。
8. 途中フェーズで `blocked` / `failed` / タイムアウトが発生した場合は、それ以上の phase を生成せず root task も `blocked` に遷移させること。
9. phase 5（監査）で `APPROVED` が得られた場合、local `workspace_path` task は root task を `succeeded` にし、GitHub clone task は root task を `waiting_approval` に遷移させて Feature #3 の承認フローへ引き渡すこと。
10. phase 5（監査）で `REJECTED` が得られた場合、root task は `running` を維持し、監査指摘を引き継いだ新しい phase 4 task を `queued` で再生成すること。
11. orchestration 実装にあたり、初期スライスでは DB スキーマ追加を行わず、既存の `tasks.parent_task_id`、`tasks.root_task_id`、`tasks.phase`、`tasks.task_type`、`tasks.payload_json` を再利用すること。
12. Dashboard の一覧表示は root task のみを対象とし、phase child task は root task の詳細画面で時系列に表示すること。
13. root task の詳細画面では child task を phase ごとのアコーディオンとして表示し、初期状態では各 child task 詳細を非展開とすること。
14. 各 child task 詳細では、当該 phase 実行で使用した LLM モデル名、前 phase から引き継いだ handoff message、phase summary、結果要約を確認できること。
15. root task の詳細画面では、ユーザー投稿時の元 instruction を確認できること。
16. root task の詳細画面では、依頼本文・handoff message・result を長文でも読みやすい優先情報として表示し、他の補助メタデータより視認性を高く保つこと。
17. orchestration の Dashboard UI は日本語ベースのラベル体系とし、主要セクション名・補助文言・empty state は日本語を第一表記とすること。
18. orchestration detail の依頼本文・引き継ぎ事項・結果パネルは、背景色と本文文字色の明度差を十分に確保し、同系色で埋もれないこと。
19. Phase 0 などの child task が `blocked` になった場合、Dashboard detail は単に失敗扱いせず、`blocked` の理由種別を表示できること。特に `compose_validation_blocked` では、どの compose ファイルのどの設定がどの rule_id に違反したかを child task のログから確認できること。

## 1.1 非機能要件

1. 可観測性: `phase_task_enqueued`、`phase_promoted`、`phase_rejected`、`root_task_promoted`、`root_task_blocked` などのイベントを `logs` に構造化して残し、root task 単位で時系列追跡できること。
2. 一貫性: 子 task 生成と root task 更新は同一トランザクションで扱い、中途半端に「次フェーズだけ存在する」「親だけ terminal になる」状態を避けること。
3. 冪等性: 同じ phase 完了イベントを二重処理しても、次 phase task が重複生成されないこと。少なくとも `root_task_id + phase + status in ('queued','leased','running','waiting_approval')` 相当の重複防止戦略を持つこと。
4. 保守性: phase 遷移判定、次 task payload 生成、root task 更新を `task_backend.py` に分散させず、専用 orchestration 層へ集約すること。
5. 互換性: 既存の `TaskStateMachine`、`MariaDBAccessor`、Dashboard 投稿 API、Feature #3 承認フローと整合すること。特に GitHub clone task の最終出口は `waiting_approval` を維持すること。
6. 回復性: worker 再起動や lease 失効後も、既存 task 行と `logs` を見ればどの phase で停止したか判別でき、手動 requeue で再開方針を決められること。
7. 監査性: phase 5 の監査結果と差し戻し理由は `payload_json.orchestration.audit_feedback` と `logs.details_json` に残し、再生成された phase 4 task から参照可能であること。
8. 後方互換: local `workspace_path` task で従来どおり phase 4 単体実行を続ける運用を壊さず、orchestration は `payload_json.phase_flow` を持つ GitHub clone task から段階導入できること。
9. 操作性: 一覧画面は root task のみを表示して orchestration 1 件を 1 行で追跡可能にし、詳細画面で child task の phase 履歴を確認できること。
10. 可読性: child task 詳細は初期状態で折りたたみ、必要な phase だけを展開して確認できること。長文 instruction や handoff message は改行を保持しつつ UI 崩れを起こさないこと。
11. 監査性: LLM モデル名、phase 間 handoff message、ユーザー入力本文は root/child task detail API から一貫して取得でき、task 系譜に沿って追跡可能であること。
12. 可読性: root detail と child detail は「概要の確認」と「本文を読む行為」を分離したレイアウトとし、instruction / handoff message / result の各テキスト領域に十分な行間、padding、背景コントラストを与えること。
13. 日本語 UX: orchestration detail の見出し、補助文言、empty state、説明文は日本語で自然に読めることを優先し、英語は phase 番号や LLM model 名の識別用途に限ること。
14. コントラスト: orchestration detail の読み物パネルは、本文色、見出し色、補助文言色を分離し、hover・focus・disabled を含む状態変化でも本文コントラストを維持すること。
15. 障害判別性: child task の terminal 状態は `failed` と `blocked` を UI 上で区別し、`compose_validation_blocked` のような想定済み安全側 block では violation 詳細をその場で読めること。

## 2. 基本設計

### 2.1 タスクモデル

#### 2.1.1 root task

- root task は orchestration 全体の親レコードであり、Dashboard からの投稿要求を表す。
- `task_type` は `phase_orchestration_root` とし、初期スライスでは `assigned_service='brain'`、`status='running'` の制御用タスクとして保持する。
- root task 自体は LLM 実行を行わず、phase 実行 task 群の進捗集約と最終状態の反映のみを担う。
- root task の `payload_json.orchestration` には最低限、`phase_flow`, `current_phase`, `last_completed_phase`, `phase_attempts`, `final_review_state` を保持する。
- root task の `payload_json` にはユーザー投稿時の `instruction` を保持し、detail API が原文を表示できるようにする。

#### 2.1.2 phase 実行 task

- phase 実行 task は実際に worker が lease / 実行する子タスクであり、以下の `task_type` を持つ。
  - phase 0: `phase0_brainstorm`
  - phase 1: `phase1_design`
  - phase 2: `phase2_test_design`
  - phase 3: `phase3_test_impl`
  - phase 4: `phase4_impl`
  - phase 5: `phase5_audit`
- `root_task_id` は root task の id を保持する。
- `parent_task_id` は直前 phase task の id を保持する。初回 phase 0 task の `parent_task_id` は root task id とする。
- `phase` には実行フェーズ番号を保存し、phase index のための追加カラムは導入しない。
- `payload_json.orchestration` には `phase_attempt`, `source_task_id`, `audit_feedback`, `phase_flow` を持たせる。
- `payload_json.orchestration` には表示用/引き継ぎ用メタデータとして `llm_model`, `phase_summary`, `handoff_message` も保持する。

### 2.2 phase 開始フロー

1. Dashboard は GitHub clone task 投稿時、単一の phase 実行 task を直接投入するのではなく、root task と phase 0 task を同一トランザクションで生成する。
2. root task は制御専用として `running` で生成する。
3. phase 0 task は `queued` で生成し、`working_branch`、`workspace_path`、`target_repo`、`target_ref` を root task から複製する。
4. `payload_json.phase_flow` は `[0, 1, 2, 3, 4, 5]` を基本とし、後続 phase 判定はこの配列と現在 phase の組み合わせで行う。

### 2.3 phase 完了フロー

1. worker は phase task を `queued -> leased -> running` と実行する。
2. direct-edit 本体は既存の `task_backend.py` が担当し、phase task 単体では `git_commit_succeeded` / `git_push_succeeded` または `phase_edit_no_changes` を正常完了とみなす。
3. 正常完了後、orchestrator は次 phase が存在するかを判定する。
4. 次 phase が存在する場合は、次 phase task を `queued` で生成し、現在 phase task は `succeeded` に遷移させる。
5. 次 phase が存在しない場合は、phase 5 の結果に応じて root task を `waiting_approval` / `succeeded` / `blocked` のいずれかへ遷移させる。

### 2.3.1 phase ごとの prompt 役割

- phase 0 は要求整理と壁打ちに限定し、要求や前提、作業計画を docs に反映する。
- phase 1 は設計に限定し、phase 0 の結果を踏まえて基本設計や非機能要件を docs に反映する。
- phase 2 は破壊テスト設計に限定し、異常系・破壊シナリオを docs に反映する。
- phase 3 はテスト実装に限定し、RED を作る。
- phase 4 は実装に限定し、必要な code/docs 同期のみを行う。
- phase 5 は監査に限定し、コード変更を伴わず APPROVED / REJECTED と指摘を返す。

各 phase の完了時には、次 phase へ引き継ぐ表示用/実行用メタデータとして以下を確定させる。
- `llm_model`: 当該 phase を実行した LLM モデル識別子。
- `phase_summary`: 当該 phase の結果要約。
- `handoff_message`: 次 phase に伝える作業前提・未解決事項・注目点の要約。

phase task の prompt 生成器は最低限 `phase` と `task_type` を prompt に埋め込み、各 phase が異なる責務を持つことを明示しなければならない。

### 2.3.2 Dashboard への投影

- `GET /api/v1/tasks` は root task のみを返す。
- root task の `payload_json.orchestration.current_phase` と `last_completed_phase` は一覧で可視化できるようレスポンスへ含める。
- `GET /api/v1/tasks/{id}` は root task の場合に `subtasks` を返し、phase 順に child task の進捗を確認できるようにする。
- root task detail API は、ユーザー投稿時の `instruction` を返す。
- `subtasks` の各要素は、少なくとも `phase`, `task_type`, `status`, `llm_model`, `handoff_message`, `phase_summary`, `result_summary_md`, `logs` を返す。
- UI は `subtasks` を phase 順のアコーディオンとして描画し、初期状態では全 item を collapsed とする。
- UI は `instruction`、`handoff_message`、`result_summary_md` を日本語見出し付きの読みやすいテキストブロックで表示し、補助メタデータと視覚的に区別する。
- UI の主要見出しは `依頼本文`、`サブタスク`、`引き継ぎ事項`、`結果`、`ログ` を基本とし、既存英語ラベルは日本語優先へ置換する。
- 読み物パネルの配色は本文用の濃色文字と淡色背景を基本とし、見出しは本文より強く、補助文言は本文より弱いが背景に埋もれない色を使う。背景色と文字色が近似する組み合わせは採用しない。
- hover / focus / disabled 状態の視覚差は border, shadow, surface tint で表現し、本文色そのものを弱めて可読性を落とす設計を避ける。
- `compose_validation_blocked` ログは通常ログの一行表示で埋もれさせず、`blocked reason` セクションとして強調表示してよい。表示内容は最低限 `compose_file`, `service`, `field`, `rule_id`, `raw_value`, `message` を含む。
- `blocked_reason=compose_validation` を持つ場合、UI は `エラー` ではなく `Compose Validation により安全側でブロック` のような日本語説明を優先表示し、実行失敗と混同させない。

### 2.4 root task 状態遷移方針

- root task の既定状態遷移は以下とする。

| 条件 | root task の遷移 |
|---|---|
| root 作成直後 | `running` |
| phase 0〜4 正常完了 | `running` を維持 |
| phase 5 APPROVED かつ local `workspace_path` task | `running -> succeeded` |
| phase 5 APPROVED かつ GitHub clone task | `running -> waiting_approval` |
| phase 5 REJECTED | `running` を維持し phase 4 を再生成 |
| 途中 phase が `blocked` / `failed` / timeout | `running -> blocked` |
| 承認却下 (`reject`) | `waiting_approval -> blocked` |
| 承認成功 (`approve`) | `waiting_approval -> succeeded` |

- root task は phase 途中で `succeeded` にしてはならない。
- GitHub clone task では phase 5 の `APPROVED` は最終承認完了を意味しない。人手承認前の待機状態として `waiting_approval` に留める。

### 2.5 差し戻しフロー

1. phase 5 task が `REJECTED` を返した場合、orchestrator は `payload_json.orchestration.audit_feedback` に監査指摘を格納する。
2. 既存の phase 4 task を再利用せず、新しい phase 4 task を `queued` で再生成する。
3. 再生成 phase 4 task の `parent_task_id` は phase 5 task id とする。
4. `working_branch` は既存の開発ブランチを継続利用し、新しい branch を作成しない。
5. root task は `running` を維持し、`payload_json.orchestration.current_phase=4`、`final_review_state='rejected'` を更新する。

### 2.6 worker 統合位置

- phase 完了イベントの捕捉点は `MariaDBTaskBackend.process_next_queued_task()` の phase task 正常終了直後とする。
- direct-edit、commit/push、artifact 保存の責務は既存 backend に残し、orchestrator は「phase task の終端結果を受けて次 task を積む」「root task を更新する」ことだけを担う。
- 実装上は `task_backend.py` から `PhaseOrchestrator.handle_phase_completion(...)` を呼び出し、DB 更新・child task 生成・log 記録を集中させる。

## 3. 詳細設計メモ

### 3.1 既存スキーマの再利用方針

- 既存 `tasks` テーブルには `parent_task_id`, `root_task_id`, `task_type`, `phase`, `payload_json` が存在するため、初期スライスでは migration を行わない。
- `root_task_id` は root task id、`parent_task_id` は直前 phase task id、`phase` は実行フェーズ番号に固定する。
- `payload_json.orchestration` で補う情報は以下とする。
  - `phase_flow`: 実行対象フェーズ配列
  - `current_phase`: root task から見た現在フェーズ
  - `last_completed_phase`: 直近成功フェーズ
  - `phase_attempt`: 同一フェーズの再試行回数
  - `source_task_id`: 次 task を生成した元 task
  - `audit_feedback`: phase 5 差し戻しコメント
  - `final_review_state`: `pending`, `approved`, `rejected`
  - `llm_model`: 当該 phase を実行した LLM モデル識別子
  - `phase_summary`: 当該 phase の結果要約
  - `handoff_message`: 次 phase に引き継ぐメッセージ

### 3.1.1 Dashboard detail 向け表示データ

- root task detail は `instruction`、`repository_path`、`target_ref`、`working_branch`、`payload_json.orchestration.current_phase`、`last_completed_phase` を返す。
- child task detail は、UI の追加クエリを避けるため、root task detail API に内包して返す。
- child task のログは詳細確認用に要約済み配列として返し、初期表示ではアコーディオン内にのみ描画する。
- `instruction` と `handoff_message` は markdown 変換前のプレーンテキストを正本とし、表示時は改行保持を優先する。
- `instruction`、`handoff_message`、`result_summary_md` は単なるメタデータ文字列ではなく、detail 画面における主要読解対象として扱い、見出し、spacing、background、line-height を分離して設計する。
- 日本語 UI では `phase_summary` の表示ラベルを `要約`、`handoff_message` を `引き継ぎ事項`、`result_summary_md` を `結果` とし、利用者が phase 間の文脈を日本語で追えることを重視する。
- `compose_validation_blocked` の `details_json.violations[]` は child task detail API の `logs` 経由で欠落なく返し、フロントエンド側で file / field / rule 単位の表示に再構成できること。
- blocked 詳細表示は汎用ログ本文の文字列検索に依存せず、`event_type` と `details_json` の構造を優先して描画する。

### 3.2 phase ごとの担当サービス

- 初期スライスでは phase 0〜5 をすべて `assigned_service='brain'` で統一する。
- `phase5_audit` も同じ worker で実行し、Copilot CLI の review モード相当を利用する。
- 将来的に専用監査サービスへ分離する余地は残すが、今回の基本設計では扱わない。

### 3.3 ログイベント

- 少なくとも以下の event を `logs` に記録する。
  - `phase_root_created`
  - `phase_task_enqueued`
  - `phase_task_succeeded`
  - `phase_task_rejected`
  - `phase_rework_enqueued`
  - `root_task_promoted_waiting_approval`
  - `root_task_promoted_succeeded`
  - `root_task_blocked`

## 4. 受け入れ条件

1. GitHub clone task 投稿時に root task と phase 0 task が生成され、以後の phase task が child task として連結されること。
2. `tasks.parent_task_id` と `tasks.root_task_id` により、phase task の系譜を DB 上で辿れること。
3. phase 0〜4 の正常完了では root task は `running` を維持し、途中で terminal に遷移しないこと。
4. phase 5 `APPROVED` 後、local `workspace_path` task は `succeeded`、GitHub clone task は `waiting_approval` へ遷移すること。
5. phase 5 `REJECTED` 後、既存 phase 4 task を再利用せず、新しい phase 4 task が `queued` で再生成されること。
6. phase 異常終了時は後続 phase が生成されず、root task が `blocked` に遷移すること。
7. 初期スライスでは DB migration を追加せず、既存 `tasks` カラムと `payload_json.orchestration` だけで実現できること。
8. root task の詳細画面でユーザー投稿本文を確認できること。
9. child task 詳細はデフォルト非展開のアコーディオンで表示され、展開時に LLM モデル名、handoff message、phase summary、result を確認できること。
10. root task / child task detail の主要テキスト領域が長文でも読みやすい視覚設計になっていること。
11. Dashboard detail の主要見出し・補助文言・empty state が日本語ベースに統一されていること。
12. orchestration detail の依頼本文・引き継ぎ事項・結果パネルで、背景色と本文文字色が近似せず、見出し・本文・補助文言の色階層が明確であること。
13. `compose_validation_blocked` を含む child task detail では、どのファイルのどの設定がどのルールで blocked されたかを追加 API 呼び出しなしで確認でき、通常エラーと誤認しないこと。

## 5. 関連ドキュメント

1. `docs/phase6-direct-edit-design.md`
2. `.tdd_protocol.md`
3. `.todo/.tdd_protocol.4.md`