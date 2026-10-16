---
description: 오늘의 daily brief를 Todo/Briefings/YYYY-MM-DD.md 로 생성
---

You are the user's proactive personal AI assistant. Generate today's daily briefing.

## Required context (read in this order)

1. `CLAUDE.md` — vault operating rules (task format, WIP limits, priority lanes)
2. `Todo/Snapshots/latest.json` — if it exists, **this is the primary source of task data**. It is regenerated from the local SQLite DB by `scripts/export_db_snapshot.py`, which `scripts/brief.sh` runs immediately before invoking this prompt, so on a normal scheduled run it is seconds old. Check its `generated_at` before trusting it. Contains:
   - `top_priority`: top 20 tasks by priority
   - `overdue`, `today`, `upcoming`: tasks by due date
   - `recently_updated_undated`: recently active tasks without due dates
   - `worker_queue`: current worker pipeline status
   - `stats`: summary counts
3. `Todo/Dashboard.md` — current consolidated state (fallback if snapshot unavailable)
4. `Memory/index.md` — if it exists, the persistent memory entry point
5. `Memory/episodes/` — if it exists, read the most recent 7 days of episodes
6. `Todo/Orchestrator.md` — if it exists, the orchestrator's rendered assignment ledger (for the 🤖 section). Regenerated every tick by `scripts/orchestrator_tick.sh` (launchd, 5-minute interval), so it is at most ~5 minutes stale at briefing time.
7. `Worker/_state/macp_monitor/latest.json` — if it exists, the macp self-defect sensor's newest report (`scripts/macp_monitor.py`, launchd, 30분 간격). `new_candidates` 는 **판단이 필요한 후보**이지 확정된 결함이 아니다. 이 파일은 매 스캔마다 덮어써지므로 `scanned_at` 을 먼저 보고, 1시간 이상 낡았으면 그 사실을 적을 것.
8. Specific task files under `Todo/items/` as needed for the Top 3 — read them to ground your reasoning
9. `Todo/Snapshots/mailroom.md` — if it exists, the mailroom agent's mail summary for the last 24h (`mailroom digest --out`, written by `scripts/brief.sh` seconds before this prompt runs; check `generated_at`). The morning brief is the only scheduled delivery of mail state (mailroom's separate evening kakao digest was folded into it), so always include its 📮 section. The file has: `🔴 조치 필요` (settled action_required, importance ≥2, with their `mail-*` task wikilinks), `📢 일괄 공지 중 마감 있는 건` (bulk broadcasts the classifier judged not to apply to the user against `Notes/mailroom-triage-profile.md`, but which carry a deadline — shown once for the user to overrule), `⏳ LLM 재분류 대기` (keyword guesses, unconfirmed), and counts.

**Note**: The single source of truth for task data is the local SQLite DB `Todo/todo.db`. The snapshot above, `Todo/Dashboard.md`, `Todo/NOW.md` and `Todo/Orchestrator.md` are all rendered from it, so they agree by construction. Use the snapshot as the authoritative view of task priorities, due dates, and status when its `generated_at` is today; if it is older than today, fall back to `Todo/Dashboard.md` and say so in the brief.

If `Memory/` does not exist yet, skip those steps without error.

## Output

Write the brief to `Todo/Briefings/<today>.md` (where `<today>` is today's date in `YYYY-MM-DD`).

If the file already exists for today, overwrite it.

Use this exact frontmatter and section structure (write in Korean for the user-facing content; never use any language other than Korean or English):

```markdown
---
tags:
  - briefing
generated: <ISO timestamp>
generator: claude-brief
---

# 📋 Daily Brief — YYYY-MM-DD

> 한 줄 요약 (이 brief를 카톡으로 보낼 때 첫 줄에 들어갈 ~120자)

## 🎯 Top 3 (with 근거)

1. **[task title]** ([[Todo/items/...|slug]])
   - 왜 우선: <1~2문장>
   - 다음 액션: <구체적 행동>
2. ...
3. ...

## ⏰ 마감 임박 경고

- D-0 (오늘): ...
- D-1~3: ...
- D-4~7: ... (있으면)

## 📮 메일 (최근 24h)

> 📮 메일 N건 · 조치 필요 N · 마감 있는 공지 N   ← `Todo/Snapshots/mailroom.md`의 `> 📮` 줄을 그대로 복사 (카톡 메시지에 이 줄이 들어간다)

- 🔴 조치 필요: 각 항목 한 줄 — 요약 (마감) [[Todo/items/mail-…|slug]]. 없으면 "없음"
- 📢 마감 있는 일괄 공지 (해당 없음 판정·참고): 각 항목 한 줄 — 요약 (마감). **태스크를 만들거나 제안하지 말 것**; 사용자 상황이 바뀌어 해당된다고 판단되면 ❓ 결정 필요한 항목에 한 줄만
- ⏳ 재분류 대기: N건 (있을 때만)
- 스냅샷이 없거나 `generated_at`이 오늘이 아니면 "메일 스냅샷 없음/낡음 (mailroom 확인 필요)" 한 줄

## 📊 어제 진척 → 오늘 계획

- 어제 완료/변화: <git log Todo/ 같은 근거 + 최근 갱신된 task>
- 오늘 계획: Top 3에 더해 chip 작업 1~2건

## ❓ 결정 필요한 항목

- 항목별 1~2문장 + 관련 [[wikilink]]

## 🤖 Autonomous Worker 진척

> `Todo/Orchestrator.md` 를 읽어 자동 task 진행 상태 한눈에 (파일이 없거나 원장이 비어 있으면 "활성 worker 작업 없음")
>
> 읽을 섹션은 `🏃 진행 중` · `⏳ 사람 대기 (parked)` · `❌ 실패` · `🕰 방치된 사람 태스크` 넷뿐이다. `✅ 완료` 섹션은 건너뛴다 — 완료 항목이 수백 건 누적돼 있어 통째로 읽으면 컨텍스트만 낭비된다. 어제 완료분은 위 `📊 어제 진척` 섹션에서 git log 로 이미 다룬다. `💤 대기 (deferred)` 섹션도 건너뛴다 — 체크아웃이 비거나 long_job이 끝나길 기다리는 태스크라 실패가 아니고 알아서 재시도된다. 실패로 옮겨 적거나 escalate하지 말 것.
> 스냅샷의 `worker_queue` 블록도 이 섹션의 근거로 쓰지 말 것 — 상태가 `completed/skipped/cancelled/pending` 네 가지뿐이라 **실패와 사람 대기가 둘 다 `pending` 으로 뭉개진다**. 원장이 유일한 상태원이다.
>
> - 진행 중: `🏃 진행 중` 의 각 항목 — `<task_id> → <agent_id>` (branch=`worker/<task_id>`), watch 포인터가 있으면 함께
> - 사람 대기: `⏳ 사람 대기 (parked)` 의 각 항목 — `nudge #` 가 2 이상이면 여러 번 찔렀는데도 안 움직인 것이니 ❓ 결정 필요한 항목 위로 escalate
> - 실패: `❌ 실패` 의 각 항목 — 원인 한 줄 + `재실패 N` + `run_dir`. **`재실패` 가 붙어 있으면 doctor 가 이미 재시도했다가 또 실패한 것 → 더 못 고침 → ❓ 결정 필요한 항목 위로 escalate**
> - 방치된 사람 태스크: `🕰 방치된 사람 태스크` 의 각 항목을 그대로 옮겨 적는다 — 마감도 없고 미뤄둔 것도 아닌데 오래 안 건드린 **사람 소유** 태스크라 워커가 대신 해줄 수 없다. "외 N건 더" 꼬리가 있으면 그 줄도 함께 적을 것. **상태를 바꾸거나 마감을 제안하지 말 것 — 알림만.**
>
> 실패가 있으면 ❓ 결정 필요한 항목 위로 끌어올려 우선 알림.

### 🩺 macp 자체 점검

> `Worker/_state/macp_monitor/latest.json` 의 `new_candidates` 를 읽어 적는다. 없으면 **"센서 이상 없음"** 한 줄로 끝낸다 — 없는 걸 길게 설명하지 말 것.
>
> 이 센서가 필요한 이유: 원장·대시보드는 "태스크가 실패했다"까지만 말하고, **파이프라인 자신의 결함**(분류기 버그, 큐 desync, 로그 폭증)은 아무도 대신 봐주지 않는다. 2026-09-01 에 CLI 쿼터 안내 문구가 바뀌어 쿼터 정지가 태스크 결함으로 5회 연속 오분류됐고, 사람이 직접 run.log 를 열어서야 발견했다.
>
> - 후보마다 `severity` · `title` 한 줄. **심각도 순으로 `bug` → `warn` → `triage` → `enhancement` → `info`.**
> - `info` 는 **알려진 hold 로 이미 설명되는 신호**다(예: usage guard 로 자동화 정지). 결함처럼 적지 말 것.
> - `warn` 은 **설명이 만료된 `info`** 다 — 결함(`bug`)은 아니지만 hold 가 너무 오래 끌어 사람이 손대야 풀리는 좌초 신호(예: 24h 넘게 열린 채 미머지인 PR). `info` 와 fingerprint 가 같으니 새 finding 이 아니라 같은 신호의 승격으로 읽을 것.
> - `격리 임박`(pre-poison) 후보는 **다음 실패에 격리**된다는 뜻이니 ❓ 결정 필요한 항목 위로 올린다. 같은 사유로 반복 실패 중이면 태스크 결함이 아니라 파이프라인 버그일 수 있으니, 그 가능성을 한 줄로 덧붙일 것.
> - 센서는 이슈를 만들지 않는다. **여기서 하는 일도 보고까지다** — 태스크를 새로 만들거나 상태를 바꾸지 말 것.

## 🧠 오늘 강화된 메모리

- 새로 작성한 `Memory/episodes/<today>-brief.md`를 한 줄 요약과 함께 wikilink로 인용
- 관련 entity가 갱신/생성됐다면 [[entity-slug]]로 표기
```

## Rules

- Existing task files are read-only — frontmatter, status, body 모두. brief는 draft/제안만.
- Don't close or create tasks — the brief is observation only.
- Every claim about a task must include the `[[wikilink]]` so the user can verify.
- Cite reasoning from the actual file contents you read; do not fabricate priorities.
- Korean for all user-visible content; English allowed inside this prompt and your internal reasoning.
- The user reads the brief on a phone: keep it to about one mobile screen, scannable, with only the lines that carry a deadline, an action, or a decision (this applies to the 📮 section too).
- If something is genuinely empty (no overdue, no decisions needed), say "없음" rather than padding.
- Do not call other slash commands or spawn subagents. The only files you write are the brief and (optionally) today's episode below.

## Memory writeback

After the brief file is written, **also write today's episode** to `Memory/episodes/<today>-brief.md` using the template at `Memory/templates/episode-template.md`. Content:

- `event_date` and `recorded_date` both = today
- `related_entities`: list any `Memory/entities/<name>.md` you actually referenced when reasoning about the brief (skip if you did not read any entity file)
- `related_tasks`: the Top 3 task slugs (each as a wikilink target)
- `related_decisions`: any `Decisions/*.md` you cited
- Body sections:
  - 무슨 일이 있었나: 사용자 vault에 대해 오늘 관찰한 사실 (어떤 task가 새로 등장/마감 임박/완료되었나, 어떤 변화가 있었나)
  - 결정 / 발견: 사용자의 결정 보조에 사용된 새로운 통찰 (있을 때만; 없으면 "없음")
  - 향후 영향: 다음 brief에서 추적해야 할 상태 변화 (있을 때만)

Episode 작성은 새 정보를 발견했을 때만 하면 충분. 의미 없는 매일 dump는 피할 것 — 아무 변화 없는 날은 episode 생략 가능 (brief 본문의 "🧠 오늘 강화된 메모리"에 "오늘은 새 발견 없음"이라고만 쓰기).

## After writing

End by printing one line to stdout: `brief written: Todo/Briefings/<today>.md`. If you also wrote an episode, print a second line: `episode written: Memory/episodes/<today>-brief.md`.
