---
tags:
  - todo
  - work
  - worker
status: open
due: YYYY-MM-DD
updated: 2026-05-19
outcome: 완료 조건 한 줄
next_action: worker 큐에 올라가는지 확인
if_then: "worker 큐 등록 후 5단계 파이프라인이 자동 진행"
estimate_p50: 30m
estimate_p90: 90m
review_cycle: weekly
priority: 70

# --- Worker 전용 필드 ---
# 주의: 이 파서는 값 뒤의 인라인 주석을 떼어내지 않는다. 비인용 값 옆에 `# ...`을
# 붙이면 주석까지 값에 포함돼, worker_mode가 "auto"로 인식되지 않는다.
# 설명은 반드시 이렇게 값 줄 위에 둘 것.
#
# worker_mode: auto | review | quick | delegate | blocked | manual | off
#
# 스캐폴드는 blocked로 나온다. 내용을 다 채운 뒤 직접 auto로 바꿀 것.
# 이유: todo-sync 데몬이 주기적으로 import-md를 돌리므로, `cli new` 직후의
# 반쯤 쓴 파일도 몇 분 안에 DB에 들어가고 worker_mode가 auto면 오케스트레이터가
# 그대로 디스패치한다. 2026-08-05 dripcoffee-bean-cards-1이 이렇게 나갔다 —
# 작성자가 내용을 채우기 전 템플릿 상태(placeholder outcome, repo 필드 없음,
# auto_merge:false)로 디스패치돼, 파이프라인이 엉뚱한 payload로 돌았다.
# blocked는 DISPATCHABLE_MODES에 없으므로 그 창이 구조적으로 닫힌다.
worker_mode: blocked
auto_merge: false
max_turns: 80
# max_budget_usd 는 informational — 강제 차단하지 않는다.
max_budget_usd: 2.0
# allowed_tools 는 반드시 따옴표 친 콤마 구분 "문자열". YAML 리스트로 쓰면
# 워커가 통째로 무시하고 기본 툴셋으로 폴백한다 (gh/gcloud/gradlew 없음).
allowed_tools: "Read,Edit,Write,Bash(git:*),Bash(uv:*),Bash(pytest:*)"
# model_tier: heavy | standard | light — 이 태스크가 쓸 모델 등급 (생략 가능).
# 생략하면 워커가 tags(hard/complex → heavy, trivial/quick → light) →
# worker_mode(quick → light, delegate → standard) → estimate_p90(≤45m light,
# ≥4h heavy) 순으로 추정하고, 아무 신호도 없으면 standard.
#   heavy    = Fable 5 → Opus 5 → Opus 4.8 (설계 수준·긴 작업)
#   standard = Opus 5 → Opus 4.8            (보통 파이프라인, 기본값)
#   light    = Sonnet 5 → Opus 5 → Opus 4.8 (한 줄 수정·잡무, effort high)
# model_tier: standard
# touches: 이 태스크가 건드릴 경로 glob의 YAML 리스트 (리포 루트 기준).
# 같은 repo의 태스크끼리는 touches가 서로 겹치지 않을 때만 동시에 돈다.
# 생략하면 "겹침 미판정 (touches 없음)"으로 같은 repo의 다른 태스크와 항상
# 직렬화된다. allowed_tools와 반대로 반드시 리스트 — 문자열·빈 리스트는 ERROR.
# 함정: 디스패처(_static_prefix)는 glob을 첫 와일드카드 앞의 정적 접두로만
# 비교한다. 파일명에 와일드카드를 쓴 `dir/Foo*Test.kt`는 `dir` 전체로 읽혀
# dir 아래 모든 형제와 겹친다 — 디렉토리 단위(`dir/**`)로 쓰거나 파일을 열거할 것.
# touches:
#   - "src/feature/**"
#   - "tests/test_feature.py"
acceptance_criteria:
  - "tests pass"
  - "lint clean"
worker_branch: "worker/<slug>"
# long_job: 스테이지(60분 캡)보다 오래 걸리는 선행 작업의 셸 명령.
# 러너가 분리 세션으로 띄우고 끝날 때까지 태스크를 미룬다 — 대기 중 에이전트 spawn 없음.
# 스테이지 안에서 배경 실행하면 스테이지와 함께 죽는다 (8b: 15/100에서 잘림).
# long_job: "uv run python collect.py --years 10"
---

# 작업 제목

> Worker 가 자동으로 처리할 단일 태스크. 본문 `## 작업 명세 (Spec)` 섹션이 Research/Plan agent 에게 전달되는 1차 입력이다.

## 작업 명세 (Spec)

- **목표**: 한 줄로 "무엇을 끝내야 하는가"
- **배경**: 왜 이 작업이 필요한가 (1~3 문장)
- **범위**:
  - 포함: 어떤 파일/모듈을 건드릴 수 있는가
  - 제외: 절대 건드리지 말아야 할 영역 (예: `todo_system/cli.py` 수정 금지)
- **참고 자료**:
  - 관련 메모: [[Memory/entities/...]]
  - 관련 결정: [[Decisions/...]]
  - 외부 링크: ...

## 완료 조건 (Acceptance)

- frontmatter `acceptance_criteria` 와 일치해야 한다. 본문에는 풀어 쓴 설명.
- 예: `pytest tests/` 가 통과한다.
- 예: `ruff check` 가 깨끗하다.

## 실행 단계 (선택적 — Plan agent 가 자체 생성하므로 비워둬도 됨)

- [ ] 단계 1
- [ ] 단계 2

## Progress Log

- 2026-05-19: 생성
