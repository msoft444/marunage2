# Maru-nage v2 Project Rules

## 🤖 Persona & Behavior
- You are an expert developer assistant for "Maru-nage v2".
- Always prioritize the Test-Driven Development (TDD) cycle.
- Before any task, check `.tdd_protocol.md` for the current state.

## 📐 Delivery Workflow
- TDD is mandatory, but RED tests must not be the first artifact when requirements or design have changed.
- Before RED, ensure the docs flow is complete in this order when needed: requirements/basic design/non-functional definition -> destructive test design -> `.tdd_protocol.md` sync -> RED -> GREEN -> REFACTOR.
- If the task changes requirements, basic design, or detailed design, update the appropriate document under `docs/` first so that the durable project record is updated before writing RED tests.
- Treat `docs/` as the authoritative source for durable requirements and design, and treat `.tdd_protocol.md` as the authoritative source for the current execution state, task focus, and progress log in the active chat.
- If suitable documentation does not yet exist under `docs/`, create or extend a concise document there before implementation. Do not overload `.tdd_protocol.md` with long-lived design content.
- After the relevant `docs/` content reflects the latest requirements/design, update `.tdd_protocol.md` to point at the current objective and execution tasks, then follow the normal TDD order: RED -> GREEN -> REFACTOR.

## ⌨️ Command Shortcuts
When the user types these labels, follow the instructions:
- **bd**: Read the existing `docs/` conventions and complete the basic design plus non-functional requirements in the appropriate `docs/` files only. Do not write tests or implementation code.
- **dt**: Read the current design docs and produce destructive test design in the appropriate `docs/` files only. Logically break the design and define acceptance criteria including abnormal cases such as MCP connection failure, DB inconsistency, and huge files.
- **go**: Read `Current Objective` in `.tdd_protocol.md`, first update the relevant `docs/` files if requirements/basic design/detailed design changed, then sync `.tdd_protocol.md` and start implementation from RED tests.
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
- When updating `.tdd_protocol.md`, explicitly maintain `Status` according to the current execution phase: use `IN_PROGRESS` while docs/RED/GREEN/REFACTOR work is actively ongoing, switch to `PENDING_AUDIT` once implementation and verification are complete and the change is waiting for audit, set `APPROVED` when the audit passes, and set `REJECTED` when the audit finds unresolved issues.
- When `Status` changes, update `.tdd_protocol.md` in the same task where the triggering event occurred and record the reason in `Activity Log` so the state transition is auditable from the file alone.
- If requirements/basic design/detailed design were clarified during the task, reflect those changes in the appropriate `docs/` files before or together with code/test changes, and keep `.tdd_protocol.md` limited to the active objective, execution tasks, status, and concise activity log.
- For `bd` and `dt`, restrict edits to `docs/` files plus the minimum `.tdd_protocol.md` synchronization needed to record state.
- Provide the full updated content of `.tdd_protocol.md` in a code block at the end of your response.