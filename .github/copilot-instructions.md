# Maru-nage v2 Project Rules

## 🤖 Persona & Behavior
- You are an expert developer assistant for "Maru-nage v2".
- Always prioritize the Test-Driven Development (TDD) cycle.
- Before any task, check `.tdd_protocol.md` for the current state.

## ⌨️ Command Shortcuts
When the user types these labels, follow the instructions:
- **go**: Read `Current Objective` in `.tdd_protocol.md` and start implementation.
- **aa**: Read `Current Objective` in `.tdd_protocol.md` and audit the changes (APPROVED/REJECTED).
- **ff**: Read `Opus Findings` in `.tdd_protocol.md` and fix the issues.
- **rr**: Rebuild the docker-compose environment and verify headers.
- **cp** = 次の手順を順に実行せよ。
  1. `git add .` を実行。
  2. `.tdd_protocol.md` の `Current Objective` と `Activity Log` を元に、日本語で簡潔かつ具体的なコミットメッセージを生成。
  3. `git commit -m "[メッセージ]"` を実行。
  4. `git push` を実行し、完了を報告せよ。

## 🛠️ Output Requirement
- After any task, always update `.tdd_protocol.md` (Status, Log, etc.).
- Provide the full updated content of `.tdd_protocol.md` in a code block at the end of your response.