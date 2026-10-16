"""자동 재조정(auto-rescope) — 워커가 파킹하며 남긴 분할안을 사람 없이 집행한다.

어떤 태스크의 Implement(또는 Verify)가 "이건 한 Implement 스테이지를 넘어서
재조정이 필요하다"로 멈추면, 지금까지는 Verify가 `blocked_on`을 세우고
오케스트레이터 `collect()`가 `AWAITING_HUMAN`으로 파킹하며 ⛔ 카톡을 보냈다
(2026-10-10 `dancemirror-unified-projects`가 정확히 이 경로). 그런데 워커가 run
디렉터리에 **기계 판독 가능한 분할안**(`rescope.json`)을 남겼고 그게 안전 조건을
만족하면, 사람 판단은 필요 없다 — 분할안대로 하위 태스크 2~5개를 만들어
`depends_on`으로 직렬 연결하고 상위를 `worker_mode: off` 묶음으로 바꾸면 끝이다.

이 모듈이 그 집행을 한다. `resume.py`의 설계 제약을 그대로 따른다:

- **stdlib만.** 틱 경로에서 돌고, 틱은 베어 `python3`로도 떠야 하며 엔진 격리
  (`scripts/check_engine_isolation.sh`)를 깨지 않아야 한다. 시블링 엔진 모듈
  (`cli`·`repos`·`task_validate`)은 **함수 안에서 지연 import** 한다.
- **판정은 순수 함수로.** `can_auto_rescope()`는 I/O를 하지 않는다 — plan dict와
  부모 frontmatter dict를 인자로 받으므로 각 "사람에게 넘김" 분기가 파일 없이
  단위 테스트된다.
- **모든 불확실은 fail-open으로 "자동 불가"(⛔ 유지).** plan을 못 읽거나 판정이
  애매하면 자동 재조정하지 않고 종전처럼 사람에게 넘긴다 — 조건 없이 하위를
  찍어내는 것보다 안전하다.
- **경로는 전부 인자로 주입.** `vault_root`·`items_dir`·`templates_dir`는
  공개 함수의 인자다(틱은 `ledger.root`에서 조립, 기본값만 `cli.*`). 모듈이
  루트를 스스로 계산하지 않으므로 테스트가 tmp 볼트를 벗어날 수 없다.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

RESCOPE_SCHEMA_VERSION = 1
RESCOPE_FILENAME = "rescope.json"

# collect()가 파킹 행에 찍는 framing. `ALERT_BLOCKED`(⛔)와 분리해 이 행만
# reconcile_rescope 스윕으로 라우팅한다. 선량한 park(ALERT_BLOCKED·
# ALERT_TIMEOUT_PARK처럼)이므로 `FAILURE_ALERT_KINDS`에 넣지 않는다. 문자열은
# orchestrator.ALERT_RESCOPE_PENDING과 같아야 한다(둘 다 wire format).
ALERT_RESCOPE_PENDING = "rescope-pending"
# 자동 불가로 판명돼 사람에게 넘길 때 되돌리는 framing(= orchestrator.ALERT_BLOCKED).
_ALERT_BLOCKED = "blocked"

# notify 레벨(= kakao_format.LEVEL_DIGEST / LEVEL_IMMEDIATE). 엔진 격리를 위해
# kakao_format을 import하지 않고 문자열로 미러한다 — 매핑은 호출자(_notify_routed)가 한다.
_LEVEL_DIGEST = "digest"
_LEVEL_IMMEDIATE = "immediate"

_MIN_SUBTASKS = 2
_MAX_SUBTASKS = 5
# 하위 하나가 반드시 가져야 하는 키. acceptance_criteria는 "비지 않은 list"여야 한다.
_REQUIRED_SUBTASK_KEYS = ("slug", "title", "outcome", "acceptance_criteria")

# "사람에게 넘김" 신호들 — 보수적으로(의심되면 HUMAN).
_FALSEY = frozenset({"false", "0", "no", "off", "none", ""})
# 실계좌·실거래. repo/working_dir에 등장하면 HUMAN.
_LIVE_MARKERS = ("ai-stock-trading",)
# 실거래 태그(소문자).
_LIVE_TAGS = frozenset({"live", "real-trade", "real-account"})
# planner 자기 경계 가드레일(사람 소유). 부모 outcome/spec이나 하위 files에 등장하면 HUMAN.
_GUARDRAIL_PATHS = ("Worker/planner_policy.yaml", ".claude/commands/worker-planner.md")
# if_then이 사람 확인을 요구하는 문구(케이스무시 substring). 보수적으로 HUMAN.
_HUMAN_CONFIRM_PHRASES = (
    "사람 확인", "사람이 확인", "사람이 판단", "사람 판단", "사람에게",
    "수동", "manual", "human",
)


def _now_iso() -> str:
    """tz-aware 현재 시각 — ledger `updated_ts`/history가 fromisoformat으로 파싱되므로 offset이 붙는다."""
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _default_log(msg: str) -> None:
    print(f"[orchestrator] rescope: {msg}")


# ---------------------------------------------------------------------------
# rescope.json 로드 · 발견
# ---------------------------------------------------------------------------


def load_rescope(run_dir) -> dict | None:
    """`run_dir/rescope.json`을 읽어 dict로. 없거나 깨졌으면 None(never raises)."""
    if run_dir is None:
        return None
    try:
        data = json.loads((Path(run_dir) / RESCOPE_FILENAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 없음/깨짐 모두 "분할안 없음"으로 수렴
        return None
    return data if isinstance(data, dict) else None


def find_latest_rescope(runs_dir, slug: str) -> tuple[Path, dict] | None:
    """`runs_dir` 아래 `YYYYMMDDTHHMMSS-<slug>` 디렉터리 중 **slug를 정확히** 매칭하는
    것만 모아 이름 역순(=시간 역순)으로, `rescope.json`이 읽히는 첫 run의 (run_dir, plan).

    slug 접미사 매칭을 금지한다 — 정확 매칭이라 `foo`가 `foo-bar`의 run을 집지 않는다
    ([[macp-resumable-run-glob-matches-slug-suffix]] #54 교훈). None이면 발견 실패."""
    try:
        d = Path(runs_dir)
        if not d.is_dir():
            return None
        pat = re.compile(r"\d{8}T\d{6}-" + re.escape(slug) + r"\Z")
        cands = sorted(
            (p for p in d.iterdir() if p.is_dir() and pat.match(p.name)),
            key=lambda p: p.name, reverse=True,
        )
    except Exception:  # noqa: BLE001 — 발견 실패는 "분할안 없음"과 동급
        return None
    for run_dir in cands:
        plan = load_rescope(run_dir)
        if plan is not None:
            return run_dir, plan
    return None


# ---------------------------------------------------------------------------
# 자동 재조정 가능? — 순수 판정 함수 (AC3)
# ---------------------------------------------------------------------------


@dataclass
class RescopeDecision:
    allow: bool
    reason: str


def _is_falsey(v) -> bool:
    return str(v).strip().lower() in _FALSEY


def _as_tag_set(tags) -> set[str]:
    items = tags if isinstance(tags, list) else [tags]
    return {str(t).strip().lower() for t in items if t is not None and str(t).strip()}


def _touches_live_account(parent_fm: dict, subtasks: list) -> bool:
    hay = " ".join(str(parent_fm.get(k) or "") for k in ("repo", "working_dir"))
    if any(mk in hay for mk in _LIVE_MARKERS):
        return True
    return bool(_as_tag_set(parent_fm.get("tags")) & _LIVE_TAGS)


def _touches_guardrail(parent_fm: dict, subtasks: list) -> bool:
    texts = [str(parent_fm.get(k) or "") for k in ("outcome", "next_action", "if_then")]
    for s in subtasks:
        if not isinstance(s, dict):
            continue
        files = s.get("files")
        if isinstance(files, list):
            texts.extend(str(f) for f in files)
        spec = s.get("spec")
        if isinstance(spec, dict):
            texts.extend(str(v) for v in spec.values())
    hay = "\n".join(texts)
    return any(gp in hay for gp in _GUARDRAIL_PATHS)


def _subtasks_malformed(subtasks: list) -> str | None:
    """형식 결손 사유 한 줄, 멀쩡하면 None. 필수 키 결손/빈값·중복 slug를 잡는다."""
    seen: set[str] = set()
    for i, s in enumerate(subtasks):
        if not isinstance(s, dict):
            return f"#{i} is not an object"
        for k in _REQUIRED_SUBTASK_KEYS:
            v = s.get(k)
            if k == "acceptance_criteria":
                if not (isinstance(v, list) and v and all(str(x).strip() for x in v)):
                    return f"#{i} acceptance_criteria must be a non-empty list of strings"
            elif not (isinstance(v, str) and v.strip()):
                return f"#{i} missing or empty {k}"
        slug = str(s.get("slug")).strip()
        if slug in seen:
            return f"duplicate subtask slug {slug!r}"
        seen.add(slug)
    return None


def can_auto_rescope(rescope: dict | None, parent_fm: dict, *,
                     lineage_depth_reached: bool | None = None) -> RescopeDecision:
    """"이 분할안을 사람 없이 집행해도 되는가"를 판정하는 **순수 함수**(I/O 없음).

    먼저 걸리는 "사람에게 넘김" 사유를 반환한다. `lineage_depth_reached`가 None이면
    부모 frontmatter의 `auto_rescoped_from` 존재로 파생한다(= 이미 한 번 자동 분할로
    태어난 태스크가 또 초과 → 깊이 1에서 멈춰 무한 분할을 막는다). 모든 검사를
    통과하면 allow=True.
    """
    if not isinstance(rescope, dict):
        return RescopeDecision(False, "no/broken rescope plan")
    subs = rescope.get("subtasks")
    if not isinstance(subs, list) or not (_MIN_SUBTASKS <= len(subs) <= _MAX_SUBTASKS):
        return RescopeDecision(
            False, f"rescope plan must have {_MIN_SUBTASKS}..{_MAX_SUBTASKS} subtasks")
    detail = _subtasks_malformed(subs)
    if detail:
        return RescopeDecision(False, f"subtask malformed: {detail}")
    if lineage_depth_reached is None:
        lineage_depth_reached = bool(str(parent_fm.get("auto_rescoped_from") or "").strip())
    if lineage_depth_reached:
        return RescopeDecision(False, "already auto-rescoped once in this lineage (depth cap 1)")
    am = parent_fm.get("auto_merge")
    if am is not None and _is_falsey(am):
        return RescopeDecision(False, "parent auto_merge:false — human merge gate")
    low = str(parent_fm.get("if_then") or "").lower()
    if any(ph.lower() in low for ph in _HUMAN_CONFIRM_PHRASES):
        return RescopeDecision(False, "if_then asks for human confirmation")
    if _touches_live_account(parent_fm, subs):
        return RescopeDecision(False, "real-account/live-trade task — human only")
    if _touches_guardrail(parent_fm, subs):
        return RescopeDecision(False, "touches a human-owned guardrail file")
    return RescopeDecision(True, f"{len(subs)} subtasks, inherits parent contract")


# ---------------------------------------------------------------------------
# 손 분할 부모 자동 닫기 신호 — 순수 판정 (reconcile_split_parents)
# ---------------------------------------------------------------------------

# "한 Implement 스테이지 초과 → 재조정 필요" park를 식별하는 키워드(소문자 substring).
# collect()가 그런 park에 세우는 `blocked_on`/`last_note`에 남는 문구다 — 계기 사례인
# `dancemirror-unified-projects` 행은 "task exceeds a single Implement stage — needs
# re-scoping into the 3 sub-tasks …"였다. 외부-블로커 park(상류 PR 대기 등)는 이 중
# 아무 키워드도 담지 않고, 타임아웃 park(ALERT_TIMEOUT_PARK)는 blocked_on이 비어 있고
# last_note가 "N회 연속 stage timeout"이라 역시 걸리지 않아, 둘 다 자연히 제외된다.
_RESCOPE_PARK_KEYWORDS = (
    "re-scop", "rescop", "exceeds a single implement stage",
    "sub-task", "sub task", "subtask",
)


def is_oversize_rescope_park(blocked_on: str, last_note: str = "") -> bool:
    """이 park가 "한 Implement 스테이지 초과 → 재조정 필요" park인가 — 순수 함수(I/O 없음).

    `blocked_on`과 `last_note`를 합쳐 소문자로 보고 `_RESCOPE_PARK_KEYWORDS` 중
    하나라도 들어 있으면 True. reconcile_split_parents가 "손으로 쪼갠 부모"를
    집어내는 1차 신호이고, 공동 필수조건(자식 ≥1이 전부 done)은
    tasks.child_status_rollup이 따로 센다. `can_auto_rescope`처럼 인자만 보므로
    파일 없이 단위 테스트된다."""
    hay = f"{blocked_on}\n{last_note}".lower()
    return any(k in hay for k in _RESCOPE_PARK_KEYWORDS)


# ---------------------------------------------------------------------------
# 멱등 상태파일 — 파킹당 중복 집행 방지 (AC4)
# ---------------------------------------------------------------------------


def _rescoped_state_path(vault_root) -> Path:
    """`parent_reconcile.json` 패턴. gitignored `Worker/_state/` 아래 호스트-로컬 상태."""
    return Path(vault_root) / "Worker" / "_state" / "rescope_reconcile.json"


def _load_state(state_path) -> dict:
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _already_rescoped(state_path, parent_slug: str) -> bool:
    return parent_slug in _load_state(state_path)


def _record_rescoped(state_path, parent_slug: str, child_slugs: list[str]) -> None:
    """read-merge-write, never raises (`_record_parent_surfaced` 패턴 그대로)."""
    try:
        p = Path(state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = _load_state(p)
        data[parent_slug] = {"ts": _now_iso(), "into": list(child_slugs)}
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 멱등 기록은 best-effort
        pass


def plan_is_auto_viable(run_dir, parent_fm: dict, *, state_path, parent_slug: str) -> bool:
    """collect()가 쓰는 얇은 게이트 — run-dir의 분할안이 자동 집행 가능한가.

    load_rescope + `parent_slug` 일치 + 아직 집행 안 됨 + `can_auto_rescope.allow`.
    예외는 fail-open으로 False(=⛔ 유지, silent-skip 방지)."""
    try:
        plan = load_rescope(run_dir)
        if plan is None:
            return False
        if str(plan.get("parent_slug") or "").strip() != parent_slug:
            return False
        if _already_rescoped(state_path, parent_slug):
            return False
        return can_auto_rescope(plan, parent_fm).allow
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 분할안 → 하위 태스크 생성기 (체인·상속·dedup) (AC2)
# ---------------------------------------------------------------------------

# 상위에서 하위로 상속하는 frontmatter 필드. repo·auto_merge·allowed_tools·
# model_tier는 AC2가 명시. max_turns·max_budget_usd·priority는 상위 계약을
# 그대로 물려줘 하위가 같은 예산·우선순위로 돌게 한다(없으면 템플릿 기본값).
_INHERIT_FIELDS = ("repo", "auto_merge", "allowed_tools", "model_tier",
                   "max_turns", "max_budget_usd", "priority")
# 문자열로 들어오면 따옴표로 감싸는 필드(쉼표·괄호·URL·콜론 포함 가능).
_QUOTED_FIELDS = frozenset({"repo", "allowed_tools", "outcome", "next_action", "if_then"})


def _yaml_dq(v) -> str:
    """YAML double-quoted 스칼라. 백슬래시·따옴표만 이스케이프(파서는 평문으로 읽어도 무해)."""
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_scalar(key: str, v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"      # yaml True → `true`(frontmatter 관용)
    if key in _QUOTED_FIELDS:
        return _yaml_dq(v)
    return str(v).strip()


def _set_block_list(content: str, key: str, rendered_items: list[str]) -> str:
    """frontmatter의 `key`를 YAML 블록 list로 설정(기존 값 제거 후 끝에 추가).

    `update_frontmatter_value`는 스칼라 한 줄만 쓰므로 블록 list에 못 쓴다. 여기서
    `remove_frontmatter_key`로 기존 줄+블록을 지우고 블록 list를 재부착한다.
    `rendered_items`는 이미 렌더된 문자열(`"..."` 또는 bare slug)이다."""
    from .. import cli

    content = cli.remove_frontmatter_key(content, key)
    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return content
    fm_part, body = parts
    block = f"{key}:\n" + "\n".join(f"  - {it}" for it in rendered_items)
    return fm_part + "\n" + block + "\n---\n" + body


def _subtask_tags(parent_fm: dict) -> list[str]:
    """todo + 상위 태그(todo 제외) + worker — hand-made 하위의 태그 모양."""
    raw = parent_fm.get("tags")
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    out = ["todo"]
    for t in items:
        t = str(t).strip()
        if t and t.lower() != "todo" and t not in out:
            out.append(t)
    if "worker" not in out:
        out.append("worker")
    return out


def _render_body(spec: dict, design_notes: list, run_ref: str, title: str,
                 parent_slug: str) -> str:
    specd = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
    goal = str(specd.get("goal") or spec.get("outcome") or "").strip()
    scope_in = str(specd.get("scope_in") or "").strip()
    scope_note = str(specd.get("scope_note") or "").strip()
    today = dt.date.today().isoformat()
    lines = [
        f"# {title}", "",
        "> 자동 재조정으로 만들어진 하위 태스크. 본문 `## 작업 명세 (Spec)` 섹션이 "
        "Research/Plan agent 에게 전달되는 1차 입력이다.", "",
        "## 작업 명세 (Spec)", "",
        f"- **목표**: {goal}",
    ]
    if scope_in:
        lines.append(f"- **범위(포함)**: {scope_in}")
    files = spec.get("files")
    if isinstance(files, list) and files:
        lines.append(f"- **범위 파일**: {', '.join(str(f) for f in files)}")
    if scope_note:
        lines.append(f"- **주의**: {scope_note}")
    header = f"## 이미 정해진 설계 (다시 도출하지 말 것 — {run_ref})" if run_ref \
        else "## 이미 정해진 설계 (다시 도출하지 말 것 — 상위 run.log)"
    lines += ["", header, ""]
    notes = [str(n).strip() for n in design_notes if str(n).strip()] \
        if isinstance(design_notes, list) else []
    if notes:
        lines += [f"- {n}" for n in notes]
    else:
        lines.append("- 상위 태스크 Implement 분석 참조.")
    lines += ["", "## 완료 조건 (Acceptance)", "",
              "- frontmatter `acceptance_criteria`와 같다.", "",
              "## 실행 단계 (선택적 — Plan agent 가 자체 생성하므로 비워둬도 됨)", ""]
    steps = spec.get("steps") if isinstance(spec.get("steps"), list) else []
    if steps:
        lines += [f"- [ ] {str(s).strip()}" for s in steps if str(s).strip()]
    else:
        lines.append("- [ ] (Plan agent 가 생성)")
    lines += ["", "## Progress Log", "",
              f"- {today}: 상위 `{parent_slug}` 자동 재조정으로 생성 (orchestrator)."]
    return "\n".join(lines) + "\n"


def build_subtask_markdown(spec: dict, *, parent_slug: str, parent_fm: dict,
                           design_notes: list, run_ref: str, position: int,
                           prev_slug: str | None, templates_dir) -> str:
    """하위 태스크 md 전문을 만든다 — `auto-worker` 템플릿의 frontmatter를 cmd_new식
    스캐폴드 + 주입으로 채우고, 본문은 spec/설계노트로 재작성한다.

    `position==0` → `worker_mode: auto`(의존 없음, 바로 큐). `position>0` →
    `worker_mode: blocked` + `on_unblock: auto` + `depends_on: [prev_slug]`(직렬 체인).
    `allowed_tools`는 반드시 **따옴표 친 콤마 문자열**(YAML list면 워커가 통째로 무시).
    인라인 `# 주석`을 값에 붙이지 않는다(파서가 안 뗀다)."""
    from .. import cli

    slug = str(spec["slug"]).strip()
    title = str(spec.get("title") or slug).strip()
    today = dt.date.today().isoformat()
    content = (Path(templates_dir) / "auto-worker-task-template.md").read_text(encoding="utf-8")
    # cmd_new식 스캐폴드(값 placeholder 치환).
    content = content.replace("updated: 2026-05-19", f"updated: {today}")
    content = content.replace("due: YYYY-MM-DD", 'due: "-"')
    content = content.replace('worker_branch: "worker/<slug>"', f'worker_branch: "worker/{slug}"')

    specd = spec.get("spec") if isinstance(spec.get("spec"), dict) else {}
    goal = str(specd.get("goal") or spec.get("outcome") or "").strip()
    # next_action/if_then는 비면 spec에서 파생해 템플릿 placeholder가 새지 않게 한다.
    next_action = str(spec.get("next_action") or goal).strip()
    if_then = str(spec.get("if_then")
                  or f"워커가 이 하위 태스크를 잡으면 {goal}").strip()

    set_ = cli.update_frontmatter_value
    content = set_(content, "status", "open")
    content = set_(content, "parent", f'"[[{parent_slug}]]"')
    content = set_(content, "outcome", _yaml_dq(spec["outcome"]))
    content = set_(content, "next_action", _yaml_dq(next_action))
    content = set_(content, "if_then", _yaml_dq(if_then))
    if spec.get("estimate_p50"):
        content = set_(content, "estimate_p50", str(spec["estimate_p50"]).strip())
    if spec.get("estimate_p90"):
        content = set_(content, "estimate_p90", str(spec["estimate_p90"]).strip())
    content = set_(content, "auto_rescoped_from", parent_slug)

    for k in _INHERIT_FIELDS:
        v = parent_fm.get(k)
        if v is None:
            continue
        if isinstance(v, str) and (not v.strip() or v.strip() == "-"):
            continue
        content = set_(content, k, _render_scalar(k, v))

    if position == 0:
        content = set_(content, "worker_mode", "auto")
    else:
        content = set_(content, "worker_mode", "blocked")
        content = set_(content, "on_unblock", "auto")
        content = _set_block_list(content, "depends_on", [str(prev_slug)])

    content = _set_block_list(content, "tags", _subtask_tags(parent_fm))
    content = _set_block_list(content, "acceptance_criteria",
                              [_yaml_dq(x) for x in spec["acceptance_criteria"]])

    fm_part = content.split("\n---\n", 1)[0]
    body = _render_body(spec, design_notes, run_ref, title, parent_slug)
    return fm_part + "\n---\n\n" + body


def generate_subtasks(rescope: dict, *, parent_slug: str, parent_fm: dict,
                      items_dir, templates_dir, run_ref: str = "") -> list[Path]:
    """분할안의 하위 2~5개를 `items_dir`에 순서대로 쓰고 각 하위를 즉시 validate.

    **dedup**: 이미 있는 하위 파일은 건너뛴다(재실행 안전, `_propagate_downstream` 선례).
    새로 쓴 하위에 validate ERROR가 있으면 `ValueError`를 올리고 **이번 호출이 만든 파일만
    롤백**한다(executor가 사람 경로로 돌린다). WARN은 통과."""
    from .. import task_validate as _task_validate

    subs = rescope.get("subtasks") or []
    design_notes = rescope.get("design_notes") or []
    items_dir = Path(items_dir)
    items_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    created: list[Path] = []
    prev: str | None = None
    try:
        for i, spec in enumerate(subs):
            slug = str(spec["slug"]).strip()
            dest = items_dir / f"{slug}.md"
            if dest.exists():
                paths.append(dest)       # dedup — 이미 있으면 다시 만들지 않는다
                prev = slug
                continue
            md = build_subtask_markdown(
                spec, parent_slug=parent_slug, parent_fm=parent_fm,
                design_notes=design_notes, run_ref=run_ref, position=i,
                prev_slug=prev, templates_dir=templates_dir)
            dest.write_text(md, encoding="utf-8")
            created.append(dest)
            errors = [iss for iss in _task_validate.validate_file(dest)
                      if iss.level == _task_validate.ERROR]
            if errors:
                raise ValueError(f"{slug}: " + "; ".join(iss.message for iss in errors))
            paths.append(dest)
            prev = slug
    except ValueError:
        for p in created:                # 이번 호출이 쓴 것만 지운다 — 볼트를 원상복구
            try:
                p.unlink()
            except OSError:
                pass
        raise
    return paths


def _append_progress_log(content: str, note: str) -> str:
    content = content.rstrip("\n")
    if "## Progress Log" in content:
        return content + "\n" + note + "\n"
    return content + "\n\n## Progress Log\n\n" + note + "\n"


def flip_parent_to_bundle(parent_path, child_slugs: list[str]) -> None:
    """상위 md를 `worker_mode: off` 묶음으로 바꾸고 경위를 Progress Log에 남긴다.

    `auto_rescoped: true`는 AC5 자동 닫기의 표식이고, `is_parent: true`는
    `parents_needing_review`가 이 묶음을 보려면 필요하다 — import-md가 자식 `parent:`
    링크에서 파생하지 **않고** 태스크 자신의 `is_parent` 필드만 읽기 때문이다
    (cli.parse_task_file). `cli.cli`는 수정하지 않고 재사용만 한다."""
    from .. import cli

    p = Path(parent_path)
    content = p.read_text(encoding="utf-8")
    content = cli.update_frontmatter_value(content, "worker_mode", "off")
    content = cli.update_frontmatter_value(content, "auto_rescoped", "true")
    content = cli.update_frontmatter_value(content, "is_parent", "true")
    today = dt.date.today().isoformat()
    note = (f"- {today}: 자동 분할 → 하위 {len(child_slugs)}개"
            f"({', '.join(child_slugs)}), 상위 묶음(worker_mode: off)으로 전환 (orchestrator)")
    p.write_text(_append_progress_log(content, note), encoding="utf-8")


# ---------------------------------------------------------------------------
# Executor — 파킹 감지 → 집행 → 정보 알림 → 멱등 → 파킹 정리 (AC2/AC4)
# ---------------------------------------------------------------------------


def compose_rescope_alert(parent_slug: str, child_slugs: list[str]) -> str:
    """자동 분할 정보 알림(🔀, digest). count+ask는 headline, slug들은 word-clip tail."""
    from ..kakao_format import compose_alert

    headline = f"🔀 자동 분할: {parent_slug} → 하위 {len(child_slugs)}개 · 첫 태스크 시작"
    return compose_alert(headline, tail=", ".join(child_slugs))


def _fallback_alert(parent_slug: str, reason: str) -> str:
    from ..kakao_format import compose_alert

    headline = "⛔ 자동 재조정 불가 — 사람 판단이 필요해요 (분할안을 손보거나 태스크를 접어주세요)"
    return compose_alert(headline, [f"📋 {parent_slug}"], tail=reason)


def _notify(notify, msg: str, event_key: str, emit, *, level: str | None = None) -> None:
    """알림은 곁가지 — 실패해도 이미 저장된 집행을 되돌리지 않는다(resume._notify 패턴)."""
    if notify is None:
        return
    try:
        if level is None:
            notify(msg, event_key=event_key)
        else:
            notify(msg, event_key=event_key, level=level)
    except Exception as e:  # noqa: BLE001
        emit(f"알림 실패(무시): {type(e).__name__}: {e}")


def _refresh_db(paths, db_path=None) -> None:
    """상위+하위 md를 todo.db에 즉시 반영 — 데몬 lag 창(stale DB 재디스패치) 차단.
    best-effort: 실패해도 todo-sync 데몬이 다음 틱에 흡수한다(`mark_done` 패턴)."""
    try:
        from .. import cli

        con = cli.db_connect(db_path or cli.DB_PATH)
        try:
            cli.init_db(con)
            for p in paths:
                cli.refresh_single_task(con, Path(p))
            con.commit()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        _default_log(f"DB refresh best-effort 실패(데몬이 흡수): {type(e).__name__}: {e}")


def _fallback_to_human(a, ledger, notify, reason: str, emit) -> None:
    """자동 불가 → ⛔ 1건(immediate) 보내고 파킹 유지. collect()가 ⛔를 억제했을 수
    있으니 silent park를 막는다. framing을 ALERT_BLOCKED로 바꿔 다음 틱 재집행을 멈추고
    reconcile_unparked(사람이 blocker 해제) 경로로 넘긴다."""
    a.last_alert_kind = _ALERT_BLOCKED
    a.last_note = f"rescope not auto-viable: {reason}"[:200]
    a.history.append({"ts": _now_iso(), "type": "rescope-fallback",
                      "from": "orchestrator", "reason": str(reason)[:120]})
    ledger.save(a)
    _notify(notify, _fallback_alert(a.task_id, reason),
            f"rescope-fallback:{a.task_id}", emit, level=_LEVEL_IMMEDIATE)
    emit(f"{a.task_id}: 자동 재조정 불가({reason}) — 사람에게 넘김(⛔), 파킹 유지")


def execute_rescope(ledger, vault_root, notify, *, items_dir=None, templates_dir=None,
                    db_path=None, log=None) -> list[str]:
    """파킹된(`AWAITING_HUMAN` + `blocked_on` + `ALERT_RESCOPE_PENDING`) 행을 찾아 자동
    집행한다. 집행한 상위 slug 목록을 돌려주며 **절대 raise하지 않는다**(틱의 dispatch를
    멈추면 안 됨 — 바깥 한 겹 + per-row 한 겹 try/except, `resume.consume` 구조).

    각 행: run-dir의 분할안을 읽어 `can_auto_rescope`로 재확인(방어) → AUTO면
    하위 생성(validate 포함)·상위 묶음 전환·파킹 assignment를 `ledger.save`로 정식 정리
    (`Worker/assignments/*.json` 손편집 아님)·멱등 기록·DB refresh·🔀 1건(digest). HUMAN이면
    fallback ⛔ 1건(immediate) 후 파킹 유지. validate 실패는 생성분 롤백 + fallback."""
    done: list[str] = []
    emit = log or _default_log
    try:
        from .assignment import AWAITING_HUMAN, DONE
        from . import repos
        root = Path(vault_root)
        runs_dir = Path(ledger.root).parent / "runs"
        if items_dir is None or templates_dir is None:
            from .. import cli
            items_dir = items_dir or cli.ITEMS_DIR
            templates_dir = templates_dir or (cli.TODO_DIR / "templates")
        items_dir = Path(items_dir)
        templates_dir = Path(templates_dir)
        state_path = _rescoped_state_path(root)
        for a in ledger.all():
            try:
                if not (a.state == AWAITING_HUMAN and getattr(a, "blocked_on", "")
                        and getattr(a, "last_alert_kind", "") == ALERT_RESCOPE_PENDING):
                    continue
                parent_slug = a.task_id
                if _already_rescoped(state_path, parent_slug):
                    continue
                stored = str((a.payload or {}).get("last_run_dir") or "").strip()
                run_dir = runs_dir / Path(stored).name if stored else None
                plan = load_rescope(run_dir) if run_dir else None
                if plan is None:
                    found = find_latest_rescope(runs_dir, parent_slug)
                    if found:
                        run_dir, plan = found
                parent_path = items_dir / f"{parent_slug}.md"
                parent_fm = repos._frontmatter(parent_path)
                decision = (can_auto_rescope(plan, parent_fm) if plan is not None
                            else RescopeDecision(False, "no/broken rescope plan"))
                if not decision.allow:
                    _fallback_to_human(a, ledger, notify, decision.reason, emit)
                    continue
                run_ref = f"Worker/runs/{run_dir.name}/run.log" if run_dir else ""
                try:
                    children = generate_subtasks(
                        plan, parent_slug=parent_slug, parent_fm=parent_fm,
                        items_dir=items_dir, templates_dir=templates_dir, run_ref=run_ref)
                except ValueError as e:
                    _fallback_to_human(a, ledger, notify,
                                       f"subtask validate failed: {e}", emit)
                    continue
                child_slugs = [p.stem for p in children]
                flip_parent_to_bundle(parent_path, child_slugs)
                a.state = DONE
                a.blocked_on = ""
                a.last_note = f"auto-rescoped into {len(child_slugs)} subtasks"[:200]
                a.history.append({"ts": _now_iso(), "type": "rescoped",
                                  "into": child_slugs, "from": "orchestrator"})
                ledger.save(a)
                _record_rescoped(state_path, parent_slug, child_slugs)
                _refresh_db([parent_path, *children], db_path)
                done.append(parent_slug)
                _notify(notify, compose_rescope_alert(parent_slug, child_slugs),
                        f"rescope:{parent_slug}", emit)
                emit(f"{parent_slug}: 자동 분할 → {child_slugs} (상위 묶음, 파킹 정리)")
            except Exception as e:  # noqa: BLE001 — per-row 격리
                emit(f"{getattr(a, 'task_id', '?')}: skipped: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001 — 바깥 그물: tick()으로 raise 금지
        emit(f"sweep aborted: {type(e).__name__}: {e}")
    return done
