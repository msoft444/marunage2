import os
content = """# [SYSTEM] Maru-nage v2: TDD Protocol & State

## 1. 🚩 Execution Context
- **Phase:** Phase 7 (Security — Compose Validation)
- **Feature #:** 5 — 生成 compose のセキュリティ検査
- **Status:** IN_PROGRESS
- **Current Objective:** repo 内の compose 定義と runtime override を起動前に検査し、危険な設定やワークスペース逸脱があれば `blocked` に落とすセキュリティ検査を実装する。
- **Dependencies:** direct-edit 後の repo 実行基盤。実行タイミングの定義や、PyYAML等のパースライブラリ追加要件が含まれる。
- **Last Audit:** N/A (Feature #5 just started)
- **Runtime Ops:** N/A

## 2. 📝 Activity Log Reference
- Detailed chronological history is tracked in `.tdd_activitylog.md`.
- Append a new execution entry to `.tdd_activitylog.md` for every task.

## 3. 📚 Active References
- `.todo/.tdd_protocol.5.md` (Origin)
- `docs/phase7-compose-validation-design.md` (To be created)
- `docs/phase7-compose-validation-destructive-test-design.md` (To be created)
- `pyproject.toml` (Needs PyYAML)
- `src/security/compose_validator.py` (To be created)

## 4. 🎯 Next Actions
### Feature #5: 生成 compose のセキュリティ検査（Compose Validation）
- [ ] **Prerequisite Analysis**: `pyproject.toml` に、docker-composeファイルをパース・検査するための依存ライブラリ（`PyYAML` 等）を追加手配する。
- [ ] **Docs/Basic Design**: `docs/phase7-compose-validation-design.md` を作成し、検査対象（`docker-compose.yml`）、検査タイミング（例: Phase 4 完了後や Phase 7 開始時）、危険な設定の具体的な定義（ホストへの volume マウント逸脱、`privileged: true`、`network_mode: host` 等）の要件を定義する。
- [ ] **Docs/Test Design**: `docs/phase7-compose-validation-destructive-test-design.md` を作成し、悪意ある compose（パス・トラバーサルを用いた volume、特権コンテナ要求など）を確実に検知して `blocked` 遷移させる破壊シナリオを定義する。
- [ ] **RED**: `tests/test_compose_validation.py` に、上記破壊シナリオをカバーするセキュリティテストを追加する。
- [ ] **GREEN**: `src/security/compose_validator.py` にパースエンジンとバリデーションロジックを実装し、テストを通過させる。
- [ ] **Integration**: バリデーション結果によってタスク状態を `blocked` にフォールバックするフローと統合する（Feature #7 コンテナ起動基盤に向けた前準備）。

## 5. ✅ Findings Remediation (`ff`)
- N/A

## 6. 📊 Test Metrics
- (Pending)
"""
with open(".tdd_protocol.md", "w", encoding="utf-8") as f:
    f.write(content)
print("done")
