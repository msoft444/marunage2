# 丸投げシステム (Maru-nage v2) フェーズ6 オーケストレーション 破壊的テスト設計書

## 0. 目的

本書は、Phase 6 オーケストレーション（フェーズ間遷移の制御）の設計を論理的に破壊し、RED テストで保証すべき異常系と合格基準を定義する。対象設計書は `docs/phase6-orchestration-design.md`。

## 1. テスト方針

1. root task と phase task の生成・遷移における暗黙の前提が崩れた時に安全に失敗することを定義する。
2. phase 完了イベントの二重処理による重複 task 生成を最優先で防御し、`root_task_id + phase + status` による冪等性保証が崩れた場合に安全側に倒れることを保証する。
3. トランザクション境界の一貫性を検証し、「子 task だけ生成されて root task が未更新」「root task だけ terminal になって子が orphan」の中途半端な状態を排除する。
4. 差し戻しフロー（phase 5 REJECTED → phase 4 再生成）が無限ループを引き起こさないこと、および監査指摘が欠落せず引き継がれることを保証する。
5. 既存の local `workspace_path` task や orchestration 未対応の task が、orchestration 導入後も従来どおり動作することを保証する（後方互換）。
6. 異常は task 単位で閉じ込め、worker 全体を巻き込まないことを検証する。

## 2. 破壊シナリオと合格基準

### ODT-01: root task と phase 0 task の生成トランザクションが部分失敗

- 攻撃シナリオ: Dashboard から GitHub clone task を投稿する。root task の INSERT は成功するが、phase 0 task の INSERT 直前に DB 接続が切れる。トランザクション外で処理していた場合、root task だけが `running` として残り、phase 0 task が存在しない。
- Red 条件: root task が `running` で残り、対応する phase 0 task が存在しない。worker が root task を拾えず、UI からも状態把握できない孤立レコードが発生する。
- Green 条件: root task と phase 0 task の生成が同一トランザクションで行われ、いずれかの INSERT 失敗時はトランザクション全体がロールバックされること。§2.2 item 1「同一トランザクションで生成する」が遵守されること。

### ODT-02: phase 完了イベントの二重処理による重複 task 生成

- 攻撃シナリオ: phase 0 task が `succeeded` に遷移した後、worker の再起動や lease 再取得により同じ phase 0 task の完了イベントが再度発火する。orchestrator が冪等性チェックなしに phase 1 task を再生成する。
- Red 条件: 同一 root task 配下に phase 1 task が 2 件以上存在する。worker が両方を lease して重複実行する。
- Green 条件: `root_task_id + phase + status in ('queued','leased','running','waiting_approval')` の重複チェックにより、既に同 phase の active な task が存在する場合は再生成をスキップすること。§1.1 NF-3 冪等性が遵守されること。

### ODT-03: phase task 正常終了と次 phase task 生成のトランザクション分離

- 攻撃シナリオ: phase 2 task が `succeeded` に遷移し、orchestrator が phase 3 task の INSERT を開始する前に worker プロセスがクラッシュする。phase 2 は `succeeded` だが phase 3 task が存在しない。
- Red 条件: phase 2 は完了済みだが phase 3 が生成されておらず、root task は `running` のまま永続停止する。手動介入以外の回復手段がない。
- Green 条件: 現 phase task の `succeeded` 遷移と次 phase task の INSERT が同一トランザクションで行われること。トランザクション失敗時は現 phase task も `succeeded` にならず、worker の lease 失効後に再処理が可能であること。§1.1 NF-2 一貫性が遵守されること。

### ODT-04: root task が phase 途中で succeeded に遷移

- 攻撃シナリオ: phase 3 task が正常完了した際に、orchestrator のバグにより root task が `running` ではなく `succeeded` に遷移する。以後の phase 4/5 が生成されない。
- Red 条件: phase 0〜4 の途中で root task が `succeeded` に遷移し、残りのフェーズが実行されない。
- Green 条件: root task が `succeeded` に遷移する条件は「phase 5 APPROVED かつ local task」のみであること。phase 0〜4 完了時は root task を `running` に維持すること。§2.4 の状態遷移表が厳密に守られること。

### ODT-05: phase 途中の blocked / failed が root task に伝播しない

- 攻撃シナリオ: phase 2 task が `blocked` に遷移するが、orchestrator が root task の更新を怠り、root task が `running` のまま残る。UI 上は root task が進行中に見えるが、実際は停止している。
- Red 条件: phase task が `blocked` / `failed` であるのに root task が `running` を維持し、進行中に見える。
- Green 条件: phase task が `blocked` / `failed` に遷移した場合、後続 phase を生成せず root task を `blocked` に遷移させること。§2.4「途中 phase が blocked / failed / timeout → running → blocked」が遵守されること。

### ODT-06: phase 5 REJECTED の無限差し戻しループ

- 攻撃シナリオ: phase 5 で REJECTED が返り、orchestrator が phase 4 を再生成する。再生成された phase 4 が実行されるが、LLM が同じコードを出力し、再度 phase 5 で REJECTED になる。これが無限に繰り返される。
- Red 条件: 差し戻し回数に上限がなく、phase 4 → 5 → 4 → 5 のサイクルが無制限に繰り返される。CPU・DB リソースを消費し続ける。
- Green 条件: `payload_json.orchestration.phase_attempt` により同一 phase の再試行回数を追跡し、上限超過時は root task を `blocked` に遷移させて差し戻しループを打ち切ること。上限値は設定可能であること。

### ODT-07: phase 5 REJECTED 時の audit_feedback 欠落

- 攻撃シナリオ: phase 5 task が REJECTED を返すが、監査指摘コメントが空文字列または NULL のまま orchestrator に渡される。再生成された phase 4 task の `payload_json.orchestration.audit_feedback` が空で、LLM に差し戻し理由が伝わらない。
- Red 条件: 再生成された phase 4 task の `audit_feedback` が空で、LLM が修正内容を特定できない。
- Green 条件: phase 5 の REJECTED 結果に audit_feedback が空の場合は、少なくともデフォルトのフィードバックメッセージ（例: 「監査で不合格」）を設定すること。`logs` にも差し戻し理由を記録すること。§1.1 NF-7 監査性が遵守されること。

### ODT-08: 差し戻し phase 4 の parent_task_id が不正

- 攻撃シナリオ: phase 5 REJECTED 後に再生成される phase 4 task の `parent_task_id` が、phase 5 task id ではなく元の phase 4 task id や root task id に設定される。task 系譜のトレーサビリティが破壊される。
- Red 条件: 再生成 phase 4 の `parent_task_id` が phase 5 task id 以外の値を持ち、DB 上で差し戻し元を辿れない。
- Green 条件: 再生成 phase 4 task の `parent_task_id` は phase 5 task id であること。§2.5 item 3「再生成 phase 4 task の parent_task_id は phase 5 task id とする」が遵守されること。

### ODT-09: payload_json.orchestration が欠損した phase task

- 攻撃シナリオ: DB 直接操作やバグにより、phase task の `payload_json` から `orchestration` キーが欠落している。orchestrator が `payload_json['orchestration']['phase_flow']` を参照した瞬間に `KeyError` / `TypeError` が発生する。
- Red 条件: orchestrator が未処理例外をスローし、worker プロセスが停止する。
- Green 条件: orchestrator が `payload_json.orchestration` の存在と必須フィールド（`phase_flow`, `current_phase`）を検証し、欠損時は phase task を `blocked` に遷移させて `logs` にエラーを記録すること。worker は次 task を処理可能であること。

### ODT-10: payload_json.orchestration.phase_flow が空配列

- 攻撃シナリオ: `phase_flow` が `[]`（空配列）に設定されている phase task が実行される。orchestrator が「次 phase なし」と判定し、phase 0 完了時点で root task を terminal に遷移させようとする。
- Red 条件: 全フェーズがスキップされ、root task が内容未確認で `succeeded` になる。
- Green 条件: orchestrator が `phase_flow` の妥当性を検証し、空配列の場合は phase task を `blocked` に遷移させること。

### ODT-11: phase_flow に重複した phase 番号

- 攻撃シナリオ: `phase_flow` が `[0, 1, 2, 3, 3, 4, 5]` のように同一 phase 番号を重複して含む。orchestrator が phase 3 完了時に次 phase として再び phase 3 を生成しようとする。
- Red 条件: 同じ phase task が重複生成されるか、冪等性チェックで「既に active な phase 3 が存在する」と判定されて進行不能になる。
- Green 条件: `phase_flow` に重複を検出した場合にエラーとして扱うか、重複を無視して一意な sequence として解釈すること。いずれの場合も進行不能に陥らないこと。

### ODT-12: root_task_id が存在しない task を参照

- 攻撃シナリオ: phase task の `root_task_id` が指す root task レコードが DB 上に存在しない（削除済み、参照整合性違反）。orchestrator が root task を SELECT する際に `None` が返る。
- Red 条件: orchestrator が `NoneType` の属性アクセスで未処理例外をスローし、worker が停止する。
- Green 条件: root task が存在しない場合は phase task を `blocked` に遷移させ、`root_task_not_found` を `logs` に記録すること。

### ODT-13: root task が既に terminal 状態（succeeded / failed / cancelled）で phase task の完了通知が到着

- 攻撃シナリオ: root task が手動操作やバグにより `cancelled` に遷移した後、実行中だった phase 3 task が `succeeded` となり orchestrator に完了通知が到着する。orchestrator が `cancelled` → `running` へ逆戻しを試みる。
- Red 条件: terminal 状態の root task が `running` に戻される、または次 phase task が生成されて不整合が拡大する。
- Green 条件: root task が terminal 状態の場合、phase 完了通知を無視して次 phase を生成しないこと。phase task は `succeeded` のまま残るが、root task の状態は変更しないこと。`logs` に `root_task_already_terminal` を記録すること。

### ODT-14: orchestrator 呼び出し対象外の task（local workspace_path task）

- 攻撃シナリオ: local `workspace_path` task（`payload_json.phase_flow` を持たない、旧来のフロー）が `succeeded` に遷移した際に orchestrator が呼び出される。orchestrator が `phase_flow` 不在で例外を投げる。
- Red 条件: local task の正常完了時に orchestrator が例外を投げ、worker が停止する。従来の動作が壊れる。
- Green 条件: orchestrator は `payload_json.phase_flow` の有無で orchestration 対象かを判別し、非対象 task はスキップして既存フローに委ねること。§1.1 NF-8 後方互換が遵守されること。

### ODT-15: phase task に引き継がれるべき workspace_path が不正

- 攻撃シナリオ: phase 0 task 完了後、orchestrator が phase 1 task を生成する際に `workspace_path` の引き継ぎが漏れるか、不正なパスが設定される。phase 1 worker が存在しないディレクトリで copilot を起動する。
- Red 条件: phase 1 task の `workspace_path` が空、NULL、または phase 0 と異なるパスに設定され、copilot 実行が失敗する。
- Green 条件: 次 phase task に `workspace_path`、`target_repo`、`target_ref`、`working_branch` が前 phase task から正確に複製されること。§1 要件 5 が遵守されること。

### ODT-16: phase task に引き継がれるべき working_branch が NULL / 空

- 攻撃シナリオ: root task の `working_branch` が NULL のまま phase 0 task に複製される。phase 0 実行後の commit_and_push() で `git push origin ""` が実行される。
- Red 条件: `working_branch` が空のまま git push が試行され、デフォルトブランチに push されるか worker が例外で停止する。
- Green 条件: orchestrator が phase task 生成時に `working_branch` の有効性を検証し、未設定の場合は phase task を生成せず root task を `blocked` に遷移させること。

### ODT-17: phase 5 APPROVED だが task_type の判別が不正（local / GitHub clone の混同）

- 攻撃シナリオ: phase 5 が APPROVED を返すが、orchestrator が root task を local task と GitHub clone task のどちらか判別できず、GitHub clone task なのに `succeeded` に遷移させる（`waiting_approval` をスキップ）。承認フローが呼び出されずに task が完了扱いになる。
- Red 条件: GitHub clone task の root task が `waiting_approval` を経由せず `succeeded` に遷移し、人手承認なしで完了する。
- Green 条件: root task が GitHub clone task か local task かの判別ロジックが明確に実装され、GitHub clone task は必ず `waiting_approval` を経由すること。§2.4 の遷移表が厳密に守られること。

### ODT-18: phase 完了後のログイベント記録が欠落

- 攻撃シナリオ: orchestrator が次 phase task の生成には成功するが、`logs` テーブルへのイベント記録が省略されるか例外で失敗する。`phase_task_enqueued` イベントが残らず、root task 単位の時系列追跡が不可能になる。
- Red 条件: phase 遷移が発生したにもかかわらず `logs` にイベントが記録されておらず、障害発生時に追跡不可能。
- Green 条件: phase 完了・次 phase 生成・root task 更新のすべてにおいて、対応する log イベント（`phase_task_succeeded`, `phase_task_enqueued`, `root_task_promoted_*`, `root_task_blocked`）が `logs` に記録されること。§1.1 NF-1 可観測性が遵守されること。

### ODT-19: phase 5 task の結果判定（APPROVED / REJECTED）の解析失敗

- 攻撃シナリオ: phase 5 task が copilot CLI の review 結果を返すが、結果フォーマットが想定外（JSON ではなく自然言語、APPROVED/REJECTED 以外の文字列、空レスポンス）で orchestrator が結果を解析できない。
- Red 条件: 解析失敗で未処理例外がスローされるか、不明な結果が APPROVED として扱われて承認フローに進む。
- Green 条件: phase 5 結果が APPROVED / REJECTED のいずれにも解析できない場合は、root task を `blocked` に遷移させ、`phase5_result_unparseable` を `logs` に記録すること。不明な結果を APPROVED として扱わないこと。

### ODT-20: concurrent worker が同じ root task 配下の phase task を二重 lease

- 攻撃シナリオ: 複数 worker が同時に `select_next_queued_task()` を実行し、同じ root task 配下の phase 3 task をそれぞれ lease する。`FOR UPDATE SKIP LOCKED` は同一行の二重 lease を防ぐが、冪等性チェック前に phase 3 task が 2 件生成されていた場合は両方が lease される。
- Red 条件: 同一 root task の同一 phase が 2 つの worker で並行実行され、重複 commit / push が発生する。
- Green 条件: ODT-02 の冪等性チェックにより phase task が 1 件のみ生成されること。`FOR UPDATE SKIP LOCKED` により同一 task 行の二重 lease が防止されること。

### ODT-21: Dashboard 投稿時の root task の task_type が不正

- 攻撃シナリオ: Dashboard がリクエスト改竄により root task の `task_type` を `phase_orchestration_root` ではなく任意の文字列で生成する。orchestrator が root task を識別できず、phase 完了時に root task 更新をスキップする。
- Red 条件: root task が更新されないまま残り、phase task だけが進行する。
- Green 条件: Dashboard は root task の `task_type` をバックエンドでハードコードし、リクエスト payload からの上書きを許容しないこと。orchestrator は root task の `task_type` が `phase_orchestration_root` であることを検証すること。

### ODT-22: phase 完了順序の逆転（phase 2 が phase 1 より先に完了）

- 攻撃シナリオ: バグやデータ不整合により、phase 1 task がまだ`running` なのに phase 2 task が `queued` で存在し、別 worker に lease されて先に完了する。
- Red 条件: phase 2 完了後に phase 3 が生成されるが、phase 1 が未完了のまま残留する。root task の `last_completed_phase` が不正確になる。
- Green 条件: orchestrator は前 phase が `succeeded` であることを確認してから次 phase task を `queued` にすること。phase 2 の完了時に phase 1 の状態が `succeeded` でない場合は異常として `blocked` に遷移させること。

### ODT-23: root task の payload_json.orchestration.current_phase が実態と不一致

- 攻撃シナリオ: orchestrator が次 phase task を生成する際に `current_phase` の更新を漏らす。root task の `current_phase=2` のまま phase 3 task が生成・実行される。
- Red 条件: root task の `current_phase` が実際に実行中の phase と一致せず、UI 表示やログ解析が不正確になる。
- Green 条件: orchestrator が次 phase task 生成と同一トランザクションで root task の `current_phase` を更新すること。

### ODT-24: phase task の assigned_service が不正

- 攻撃シナリオ: orchestrator が phase task を生成する際に `assigned_service` を設定し忘れるか、存在しないサービス名を設定する。worker が task を lease しない。
- Red 条件: phase task が `queued` のまま indefinitely 残り、root task が `running` で停止する。
- Green 条件: orchestrator が phase task の `assigned_service` を `brain` に固定すること。§3.2「初期スライスでは phase 0〜5 をすべて assigned_service='brain' で統一する」が遵守されること。

### ODT-25: 差し戻し後の phase 5 再実行で working_branch が stale

- 攻撃シナリオ: phase 5 REJECTED → phase 4 再生成 → phase 4 が succeeded → phase 5 再生成の流れで、再生成された phase 5 task の `working_branch` が元の値を保持するが、phase 4 の再実装で branch が force-push されていた場合、phase 5 の review 対象が想定と異なる。
- Red 条件: phase 5 が古い commit を review し、phase 4 の修正が反映されない監査結果を返す。
- Green 条件: phase 5 task は常に working_branch の最新 HEAD に対して review を実行すること。branch 名の引き継ぎは正しいが、commit SHA の固定参照は行わないこと。

### ODT-26: root task detail API が subtasks を返さない（child task 存在時）

- 攻撃シナリオ: root task 配下に phase 0〜2 の child task が存在するが、detail API が `subtasks` フィールドを返さない。UI はアコーディオンを描画できず、child task の進捗が一切表示されない。
- Red 条件: root task detail レスポンスに `subtasks` が含まれず、フロントエンドが child task の存在を検知できない。
- Green 条件: root task detail API は `root_task_id` で紐づく child task を phase 順配列として `subtasks` に含めること。child task が 0 件の場合は空配列を返すこと。§2.3.2「root task の場合に subtasks を返し」が遵守されること。

### ODT-27: subtask の llm_model が payload_json.orchestration に保存されない

- 攻撃シナリオ: phase task 実行時に LLM クライアントがモデル名を返すが、phase_orchestrator が `payload_json.orchestration.llm_model` への保存を怠る。detail API の `subtasks[].llm_model` が常に null になる。
- Red 条件: 全 child task の `llm_model` が null / 未設定で、Dashboard 詳細画面でどのモデルで実行されたか確認できない。
- Green 条件: phase task 完了時に `payload_json.orchestration.llm_model` に実行モデル識別子が保存されること。§2.3.1「llm_model: 当該 phase を実行した LLM モデル識別子」が遵守されること。

### ODT-28: subtask の handoff_message が次 phase に引き継がれない

- 攻撃シナリオ: phase 0 完了時に orchestrator が次 phase への handoff_message を生成しない、または空文字のまま phase 1 タスクを生成する。phase 間の作業前提が消失し、phase 1 がコンテキスト不足のまま設計に着手する。
- Red 条件: child task の `handoff_message` が null / 空で、Dashboard 上で phase 間の伝達内容を確認できない。
- Green 条件: 各 phase 完了時に `payload_json.orchestration.handoff_message` に次 phase への引き継ぎメッセージが設定されること。メッセージが空の場合は少なくとも「引き継ぎ事項なし」のデフォルトを設定すること。

### ODT-29: root task detail API が instruction を返さない

- 攻撃シナリオ: root task の `payload_json` にユーザー投稿時の `instruction` が保存されているが、detail API がこのフィールドをレスポンスに含めない。Dashboard 詳細画面でユーザーが何を依頼したか確認できない。
- Red 条件: root task detail レスポンスに `instruction` が含まれず、ユーザー投稿本文が表示されない。
- Green 条件: root task detail API は `payload_json` から `instruction` を取得してレスポンスに含めること。§2.3.2「root task detail API は、ユーザー投稿時の instruction を返す」が遵守されること。

### ODT-30: phase_summary が null の child task で detail 表示がクラッシュ

- 攻撃シナリオ: phase task が正常完了したが `phase_summary` が null のまま保存される。フロントエンドがアコーディオン展開時に null を描画しようとして TypeError をスローする。
- Red 条件: `phase_summary` が null の subtask 展開時にフロントエンドが例外をスローし、詳細画面が操作不能になる。
- Green 条件: フロントエンドは `phase_summary` が null / undefined の場合に安全にフォールバック（空文字またはプレースホルダ表示）すること。API レスポンスは null を許容するが、UI がクラッシュしないこと。

### ODT-31: subtasks の phase 順序が不正（phase 2 が phase 0 より先に返される）

- 攻撃シナリオ: detail API の subtasks 配列が `created_at` 降順で返され、phase 番号順と一致しない。ユーザーがアコーディオンを順に確認しても、フェーズの流れを追跡できない。
- Red 条件: subtasks 配列が phase 番号順でソートされていない。
- Green 条件: detail API は subtasks を `phase` 昇順で返すこと。同 phase の差し戻し再実行がある場合は `created_at` 昇順をセカンダリソートとすること。

### ODT-32: instruction に HTML/script タグが含まれる（XSS）

- 攻撃シナリオ: ユーザーが投稿時の instruction に `<script>alert('xss')</script>` や `<img onerror="alert(1)" src=x>` を含む。detail API がプレーンテキストとして返し、フロントエンドが innerHTML で描画してスクリプトが実行される。
- Red 条件: instruction 内の HTML/script タグがエスケープされず、XSS が発生する。
- Green 条件: フロントエンドは instruction を `textContent` または適切な HTML エスケープで描画し、スクリプトが実行されないこと。§3.1.1「markdown 変換前のプレーンテキストを正本とし」に従い安全に描画すること。

### ODT-33: handoff_message / phase_summary に HTML/script タグが含まれる（XSS）

- 攻撃シナリオ: LLM が生成した handoff_message や phase_summary に `<script>` や `<img onerror=...>` が含まれる。フロントエンドがこれを innerHTML で描画して XSS が発生する。
- Red 条件: handoff_message / phase_summary 内の HTML タグがエスケープされず、スクリプトが実行される。
- Green 条件: subtask の全テキストフィールドを `textContent` または HTML エスケープで描画すること。LLM 出力はサニタイズ済みとみなさず、常にエスケープすること。

### ODT-34: instruction が非常に長い（数千行）場合の表示崩れ

- 攻撃シナリオ: ユーザーが数千行の instruction を投稿する。detail 画面で instruction 全文を描画しようとして、ページ全体がスクロール不能になるか、他の UI 要素が押し出される。
- Red 条件: 長文 instruction により詳細画面のレイアウトが崩壊し、他の操作（subtask 展開、approve/reject）が不可能になる。
- Green 条件: instruction 表示領域に max-height と overflow-y: auto を適用し、長文時はスクロール可能とすること。§1.1 NF-10「長文 instruction は改行を保持しつつ UI 崩れを起こさないこと」が遵守されること。

### ODT-35: handoff_message が非常に長い場合のアコーディオン内表示崩れ

- 攻撃シナリオ: LLM が数千文字の handoff_message を生成する。アコーディオン展開時にメッセージ全文が描画され、他の subtask のアコーディオンが画面外に押し出される。
- Red 条件: 長文 handoff_message によりアコーディオン展開領域が無制限に拡大し、詳細画面の操作性が損なわれる。
- Green 条件: アコーディオン展開領域内のテキスト表示に max-height と overflow-y: auto を適用し、長文時はスクロール可能とすること。

### ODT-36: orchestration detail の主要見出しが日本語ラベル体系に準拠しない

- 攻撃シナリオ: orchestration root task の detail 画面で、`phase_summary` のラベルが `Phase Summary`、`handoff_message` のラベルが `Handoff Message`、`result_summary_md` のラベルが `Result` の英語のまま描画される。§2.3.2 で定めた日本語ラベル体系に反する。
- Red 条件: orchestration detail 画面の subtask 内ラベルが英語のままで、「要約」「引き継ぎ事項」「結果」の日本語表記が適用されていない。
- Green 条件: §2.3.2「UI の主要見出しは依頼本文、サブタスク、引き継ぎ事項、結果、ログを基本とし、既存英語ラベルは日本語優先へ置換する」が遵守されていること。§3.1.1 の日本語ラベル対応表（phase_summary→要約、handoff_message→引き継ぎ事項、result_summary_md→結果）に従うこと。

### ODT-37: orchestration detail で instruction / handoff / result が補助メタデータと視覚的に区別されない

- 攻撃シナリオ: root task detail の instruction 領域と subtask の handoff_message / result_summary_md が、current_phase / last_completed_phase / status 等の orchestration メタデータと同じフォントサイズ・余白・背景色で描画される。テキスト読解対象と管理メタデータの区別が付かない。
- Red 条件: instruction / handoff_message / result_summary_md のテキスト領域に、補助メタデータと区別する専用スタイル（padding、background、border、line-height 差）が適用されていない。
- Green 条件: §3.1.1「instruction、handoff_message、result_summary_md は detail 画面における主要読解対象として扱い、見出し、spacing、background、line-height を分離して設計する」が遵守されていること。NF-12 が遵守されること。

### ODT-38: orchestration detail の empty state / helper text が英語で表示される

- 攻撃シナリオ: orchestration root task の subtask が 0 件の場合に "No subtasks" や "No data"、handoff_message が空の場合に "No handoff message"、エラー時に "Failed to load" 等の英語テキストが表示される。
- Red 条件: orchestration 画面の empty state、helper text に英語テキストが使用されている。
- Green 条件: NF-13「orchestration detail の見出し、補助文言、empty state、説明文は日本語で自然に読めることを優先」が遵守されていること。少なくとも「サブタスクなし」「引き継ぎ事項なし」「結果なし」が日本語で表示されること。

### ODT-39: orchestration detail の情報階層が設計順序に従わない

- 攻撃シナリオ: orchestration root task の detail 画面で、ログが依頼本文の前に来る、サブタスクが概要の前に来る等、§2.3.2 で想定した情報階層に反する順序で描画される。ユーザーが phase 間の因果関係を追跡しにくい。
- Red 条件: detail 画面の表示順が「概要 → 依頼本文 → サブタスク → ログ/結果」に従っていない。
- Green 条件: root detail の情報階層が §2.3.2 に従い安定していること。subtask 展開領域の内部順は「要約 → 引き継ぎ事項 → 結果 → ログ」を基本とすること。

### ODT-40: orchestration detail のエラー表示で英語内部用語が露出する

- 攻撃シナリオ: orchestration 固有の API エラー（`root_task_not_found`、`orchestration_payload_invalid`、`phase_rework_limit_exceeded`）がフロントエンドのメッセージ領域にそのまま表示される。
- Red 条件: ユーザー向けメッセージに orchestration 内部のエラーコードが英語のまま露出し、エラー内容を日本語で理解できない。
- Green 条件: NF-13 に従い、orchestration 由来のエラーもユーザー向けには日本語メッセージへ変換すること。英語エラーコードはログ・console にのみ出力すること。

### ODT-41: orchestration detail の読み物パネルで背景色と本文色が近似

- 攻撃シナリオ: orchestration root task detail 画面の `instruction`（依頼本文）・subtask の `handoff_message`（引き継ぎ事項）・`result_summary_md`（結果）表示用の読み物パネルで、`background` と `color` の明度差が小さく、文字が背景に埋もれる。§2.3.2「背景色と文字色が近似する組み合わせは採用しない」に反する。
- Red 条件: 読み物パネルの CSS に `color` が明示指定されていない、またはパネルの `background` と `color` が同系色で視認性が不十分。
- Green 条件: 読み物パネルは `color` に十分に濃い色、`background` に淡色を明示的に指定し、§2.3.2 および §1.1 NF-14 のコントラスト要件を遵守すること。

### ODT-42: orchestration detail の見出し・本文・補助文言で色階層が崩壊

- 攻撃シナリオ: orchestration root detail のセクション見出し（「依頼本文」「サブタスク」等）、読み物パネルの本文、orchestration メタデータ（現在フェーズ / 最終完了フェーズ / 状態等）がすべて同一または非常に近い色で描画される。ユーザーが「見出し → 本文 → 補助」の段階的な読み順を取れない。
- Red 条件: セクション見出しと本文とメタデータの色が実質的に同一で、色階層が機能していない。
- Green 条件: §2.3.2「見出しは本文より強く、補助文言は本文より弱いが背景に埋もれない色を使う」が遵守され、3 層の色階層が視覚的に確認できること。§1.1 NF-14 が遵守されること。

### ODT-43: orchestration detail の hover / disabled 状態で読み物パネルの本文コントラストが低下

- 攻撃シナリオ: orchestration root task が `blocked` / `succeeded` に遷移した後、detail 画面の読み物パネルに `opacity: 0.3` や `color` を薄色に変更する disabled スタイルが適用される。あるいはパネルにマウスホバーした際に `color` が変更されて可読性が低下する。完了・停止済みタスクの内容を確認できなくなる。
- Red 条件: hover / disabled 状態で読み物パネルの本文色が通常時より大幅に薄くなり、背景に近づいてテキストが読めない。
- Green 条件: hover / focus / disabled の視覚差は border、shadow、background tint で表現し、本文 `color` は通常時の値を維持すること。disabled 時の `opacity` は 0.65 を下回らないこと。§2.3.2「hover / focus / disabled 状態の視覚差は border, shadow, surface tint で表現し、本文色そのものを弱めて可読性を落とす設計を避ける」§1.1 NF-14 が遵守されること。

## 3. RED で確認すべき項目

### 3.1 トランザクション一貫性（ODT-01, ODT-03）

1. root task と phase 0 task の生成が同一トランザクションで行われ、部分失敗時にロールバックされること。
2. phase task の `succeeded` 遷移と次 phase task の INSERT が同一トランザクションで行われること。

### 3.2 冪等性と重複防止（ODT-02, ODT-11, ODT-20）

3. 同一 phase 完了イベントの二重処理で重複 task が生成されないこと。
4. `phase_flow` の重複 phase 番号が安全に処理されること。
5. concurrent worker が同一 root task の同一 phase を二重実行しないこと。

### 3.3 root task 状態遷移（ODT-04, ODT-05, ODT-13, ODT-17）

6. phase 0〜4 完了時に root task が `running` を維持し、`succeeded` に遷移しないこと。
7. phase task の `blocked` / `failed` が root task に即時伝播すること。
8. terminal 状態の root task に対する phase 完了通知が無視されること。
9. phase 5 APPROVED 後、GitHub clone task は `waiting_approval`、local task は `succeeded` に正確に遷移すること。

### 3.4 差し戻しフロー（ODT-06, ODT-07, ODT-08, ODT-25）

10. 差し戻し回数に上限があり、超過時に `blocked` に遷移すること。
11. audit_feedback が空の場合にデフォルトメッセージが設定されること。
12. 再生成 phase 4 task の `parent_task_id` が phase 5 task id であること。

### 3.5 payload / データ検証（ODT-09, ODT-10, ODT-12, ODT-15, ODT-16）

13. `payload_json.orchestration` が欠損した task で worker が停止せず `blocked` に遷移すること。
14. `phase_flow` が空配列の場合に `blocked` に遷移すること。
15. `root_task_id` が存在しない task を参照した場合に `blocked` に遷移すること。
16. `workspace_path`, `target_repo`, `target_ref`, `working_branch` が次 phase task に正確に複製されること。

### 3.6 後方互換と分離（ODT-14, ODT-21, ODT-24）

17. local `workspace_path` task（`phase_flow` なし）が orchestrator 導入後も従来どおり正常終了すること。
18. root task の `task_type` がバックエンドでハードコードされ、リクエスト payload からの上書きが不可能であること。
19. phase task の `assigned_service` が `brain` に固定されること。

### 3.7 可観測性（ODT-18）

20. phase 完了・次 phase 生成・root task 更新のすべてで対応する log イベントが `logs` に記録されること。

### 3.8 結果解析と順序保証（ODT-19, ODT-22, ODT-23）

21. phase 5 結果が APPROVED / REJECTED に解析できない場合に `blocked` に遷移すること。
22. phase 完了順序が逆転した場合に異常として検出されること。
23. root task の `current_phase` が実態と同期されること。

### 3.9 Dashboard detail 表示（ODT-26〜ODT-35）

24. root task detail API が child task を `subtasks` 配列として phase 順に返すこと（ODT-26, ODT-31）。
25. subtask の `llm_model` が `payload_json.orchestration` に永続化され、detail API 経由で取得できること（ODT-27）。
26. subtask の `handoff_message` が設定され、null / 空時にデフォルト値が適用されること（ODT-28）。
27. root task detail API が `instruction` を返すこと（ODT-29）。
28. `phase_summary` / `handoff_message` / `llm_model` が null の subtask でフロントエンドがクラッシュしないこと（ODT-30）。
29. `instruction`、`handoff_message`、`phase_summary` の HTML/script タグが安全にエスケープされ XSS が発生しないこと（ODT-32, ODT-33）。
30. 長文 `instruction` / `handoff_message` が overflow-y: auto でスクロール可能に描画され、UI 崩れを起こさないこと（ODT-34, ODT-35）。

### 3.10 可読性・日本語 UX（ODT-36〜ODT-40）

31. **日本語ラベル体系**: orchestration detail の subtask 内ラベルが §2.3.2 / §3.1.1 の日本語対応表に従い、「要約」「引き継ぎ事項」「結果」「ログ」が適用されていること（ODT-36）。
32. **テキスト領域の視覚的分離**: instruction / handoff_message / result_summary_md が orchestration メタデータと異なる専用スタイルで描画され、主要読解対象として分離されていること（ODT-37）。
33. **日本語 empty state**: orchestration detail の empty state / helper text / エラーメッセージが日本語で記述されていること（ODT-38, ODT-40）。
34. **情報階層順序**: orchestration detail の表示順が「概要 → 依頼本文 → サブタスク → ログ/結果」に従い、subtask 内部は「要約 → 引き継ぎ事項 → 結果 → ログ」であること（ODT-39）。

### 3.11 コントラスト・色階層（ODT-41〜ODT-43）

35. **パネル本文コントラスト**: orchestration detail の読み物パネルで `color` と `background` が明示され、§2.3.2 のコントラスト要件を満たすこと（ODT-41）。
36. **色階層分離**: セクション見出し・本文・メタデータが 3 層の色階層として機能し、同一色に崩壊していないこと（ODT-42）。
37. **hover/disabled 耐性**: hover / disabled 表示で読み物パネルの本文色が通常時と同値を維持し、可読性が低下しないこと（ODT-43）。

## 4. 設計へのフィードバック

本書で設計書 `docs/phase6-orchestration-design.md` の未確定事項を確定する。

### ODT-06 確定: 差し戻し回数上限

- phase 4 → phase 5 の差し戻しサイクルに上限を設ける。`payload_json.orchestration.phase_attempt` が上限（初期値: 3）を超えた場合は root task を `blocked` に遷移させ、`phase_rework_limit_exceeded` を `logs` に記録する。上限値は将来の設定変更に備えて定数化する。

### ODT-07 確定: audit_feedback 空時のデフォルト

- phase 5 REJECTED の `audit_feedback` が空文字列 / NULL の場合、orchestrator は `"監査で不合格（詳細なし）"` をデフォルト値として設定する。

### ODT-09 確定: orchestration payload 検証

- orchestrator は phase task 処理開始時に `payload_json.orchestration` の存在と最低限のフィールド（`phase_flow`）を検証する。欠損時は `orchestration_payload_invalid` として `blocked` に遷移する。

### ODT-14 確定: orchestration 対象判別

- `task_backend.py` は phase task 完了後に `payload_json` に `phase_flow` キーが存在するかで orchestration 対象を判別する。`phase_flow` が存在しない場合は orchestrator を呼び出さず、既存の直接遷移フローに委ねる。

### ODT-19 確定: phase 5 結果解析ポリシー

- phase 5 の結果は `APPROVED` / `REJECTED` いずれかのキーワードを含むかで判定する。いずれにも該当しない場合は APPROVED として扱わず、`blocked` に遷移させる。安全側優先。

### ODT-28 確定: handoff_message 空時のデフォルト

- phase 完了時に `handoff_message` が空文字列 / NULL の場合、orchestrator は `"引き継ぎ事項なし"` をデフォルト値として設定する。`logs` にもデフォルト適用を記録する。

### ODT-41 確定: 読み物パネルのコントラスト

- orchestration detail の読み物パネルは `color` に濃色（`#3a2f22` 相当）、`background` に淡色を明示指定する。§2.3.2 で定めた「背景色と文字色が近似する組み合わせは採用しない」を遵守する。UA 既定や親要素からの色継承に依存しない。

### ODT-42 確定: 色階層

- セクション見出しは `--accent-strong` 相当で最も強い色、本文は濃色だが見出しより弱い、メタデータは `--muted` 相当で本文より弱く背景に埋もれない色とする。3 層の色階層を CSS カスタムプロパティまたは直値で維持する。

### ODT-43 確定: hover / disabled のコントラスト維持

- hover / focus / disabled の視覚差は `border-color`、`box-shadow`、`background`（surface tint）のみで表現する。`color`（本文文字色）は通常時と同値を維持し、hover / disabled で変更しない。disabled 時の `opacity` は 0.65 以上とし、テキスト内容を読めることを優先する。

### ODT-30 確定: null フィールドの UI 耐性

- `llm_model`, `phase_summary`, `handoff_message` が null / undefined の場合、フロントエンドは `"N/A"` / `"-"` / `"引き継ぎ事項なし"` 等のフォールバック表示を適用する。innerHTML ではなく textContent で描画し、null リテラルの表示を防止する。

### ODT-32/33 確定: XSS 防止ポリシー

- `instruction`、`handoff_message`、`phase_summary`、`result_summary_md` の全テキストフィールドは `textContent` またはフレームワーク標準のエスケープで描画し、innerHTML に直接代入しない。LLM 出力は信頼できない外部入力として扱う。

### ODT-34/35 確定: 長文表示の overflow 制御

- instruction 表示領域と accordion 展開領域内のテキストブロックに `max-height` と `overflow-y: auto` を適用する。初期値は instruction: `300px`、accordion 展開領域: `400px` とし、CSS カスタムプロパティで調整可能とする。

### ODT-36/37 確定: 日本語ラベルとテキスト領域分離

- orchestration detail の subtask フィールドラベルは §3.1.1 の日本語対応表に従い、`phase_summary` → `要約`、`handoff_message` → `引き継ぎ事項`、`result_summary_md` → `結果` とする。
- instruction / handoff_message / result_summary_md のテキストブロックには、orchestration メタデータ（current_phase, last_completed_phase, status 等）と区別する専用 CSS クラスを適用し、主要読解対象として視覚的に分離する。

### ODT-38/40 確定: 日本語 empty state / エラー表示

- orchestration detail の empty state は日本語で記述する。「サブタスクなし」「引き継ぎ事項なし」「結果なし」「ログなし」を準備する。
- orchestration 由来のエラーコード（`root_task_not_found`, `orchestration_payload_invalid`, `phase_rework_limit_exceeded` 等）はユーザー向け表示時に日本語メッセージへ変換し、英語コードは console / ログにのみ出力する。

### ODT-39 確定: 情報階層順序

- root detail の表示順は「概要（status / repository / branch / current_phase / last_completed_phase） → 依頼本文（instruction） → サブタスク（accordion） → 承認パネル」とする。
- subtask 展開領域の内部順は「要約 → 引き継ぎ事項 → 結果 → ログ」とする。

## 5. 関連ドキュメント

1. `docs/phase6-orchestration-design.md`
2. `docs/phase6-direct-edit-design.md`
3. `docs/phase6-direct-edit-destructive-test-design.md`
4. `.tdd_protocol.md`
