---
tags:
  - todo
  - work
status: open
due: YYYY-MM-DD
updated: 2026-03-02
outcome: 완료 조건 한 줄
next_action: 지금 당장 할 다음 행동 1개
if_then: "언제/어디서/무엇을 할지 If-Then 문장"
estimate_p50: 30m
estimate_p90: 60m
review_cycle: weekly
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
---

# 작업 제목

- [ ] 첫 번째 실행 단계
- [ ] 두 번째 실행 단계
- [ ] 세 번째 실행 단계

## Progress Log

- 2026-03-02: 생성
