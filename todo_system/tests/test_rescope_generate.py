"""Generator coverage (AC2/AC6): a dancemirror-shaped expand→migrate→contract
split produces three valid, serially-chained sub-tasks that inherit the parent
contract, and the parent flips to an off bundle.

The plan is identifier-clean (a neutral `example` repo) — generated files are
written to a tmp items dir, but the generator copies the parent's `repo` through,
so the parent frontmatter the test builds uses a neutral URL.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from todo_system import paths, task_deps, worker
from todo_system import task_validate as _task_validate
from todo_system.orchestrator import rescope

PARENT = "unified-projects"
S0, S1, S2 = (f"unified-{x}" for x in ("model-foundation", "switchover-logic", "ui-and-export"))


def _plan() -> dict:
    return {
        "version": rescope.RESCOPE_SCHEMA_VERSION,
        "parent_slug": PARENT,
        "reason": "~36-file big-bang refactor with no green intermediate — exceeds one stage",
        "design_notes": [
            "오프셋 부호: music time = reference time + offset; 재기준 = positions.map { it - delta }",
            "모델 모양: referenceUri(non-null) → videoUri: String?; kind/cameraEnabled는 파생",
            "재기준은 실제 오프셋 변경·소스 전환 때만 — 모드 전환/열기/복원 때는 절대 하지 않는다",
        ],
        "subtasks": [
            {
                "slug": S0, "title": "통합 모델 기반 (expand, additive)",
                "outcome": "Project에 videoUri/lastMode를 옛 필드와 나란히 추가, 전 게이트 green",
                "next_action": "Project·Repository·PracticeMode 순수함수를 additive로 추가",
                "if_then": "워커가 잡으면 옛 필드를 지우지 말고 새 필드를 더하고 마이그레이션 read를 back-fill",
                "files": ["model/Project.kt", "model/ProjectsRepository.kt", "player/MasterTimeline.kt"],
                "acceptance_criteria": [
                    "Project: videoUri/lastMode 추가, 옛 referenceUri/kind/cameraEnabled 유지",
                    "ProjectMigrationTest/PracticeModeTest/MasterTimelineTest green",
                ],
                "estimate_p50": "3h", "estimate_p90": "6h",
                "spec": {"goal": "모델 기반을 additive로 확장한다", "scope_in": "model/ + player/MasterTimeline",
                         "scope_note": "옛 필드/함수는 이 단계에서 지우지 않는다"},
            },
            {
                "slug": S1, "title": "로직 전환 + 옛 필드 제거 (migrate→contract)",
                "outcome": "ViewModel/SyncedPlayers가 새 모델로 동작하고 옛 필드·함수가 삭제됨",
                "if_then": "SyncedPlayers 기준을 videoUri ?: musicUri로 바꾸고 카메라 게이트를 mode==SHOOT로",
                "files": ["player/SyncedPlayers.kt", "PracticeViewModel.kt", "AppUiState.kt"],
                "acceptance_criteria": [
                    "SyncedPlayers 기준 = videoUri ?: musicUri; 모드 전환은 seek/재로드 없음",
                    "옛 referenceUri/kind/cameraEnabled/ProjectKind 삭제, 기존 싱크 테스트 green",
                ],
                "estimate_p50": "4h", "estimate_p90": "8h",
                "spec": {"goal": "로직 계층을 새 모델로 옮기고 옛 필드를 지운다"},
            },
            {
                "slug": S2, "title": "UI 표면 + 레퍼런스 없는 내보내기",
                "outcome": "FAB 생성 흐름·세그먼트 컨트롤·레퍼런스 없는 export가 동작",
                "files": ["ui/PickScreen.kt", "ui/PracticeScreen.kt", "export/CompositeExport.kt"],
                "acceptance_criteria": [
                    "PickScreen FAB → createProject, PracticeScreen 세그먼트 컨트롤",
                    "ExportPlan reference nullable, 음원만 렌더 가드 테스트 green",
                ],
                "estimate_p50": "4h", "estimate_p90": "8h",
                "spec": {"goal": "UI와 내보내기를 새 모델로 전환한다"},
            },
        ],
    }


def _parent_fm() -> dict:
    return {
        "auto_merge": True,
        "repo": "https://github.com/example/UnifiedApp.git",
        "model_tier": "heavy",
        "allowed_tools": "Read,Edit,Write,Bash(git:*),Bash(./gradlew:*)",
        "max_turns": 200,
        "max_budget_usd": 5.0,
        "priority": 50,
        "tags": ["private", "app", "unified", "feature", "worker"],
        "if_then": "워커가 잡으면 분할안대로 집행",
        "outcome": "통합 프로젝트 모델 전환",
    }


@pytest.fixture
def gen_env(tmp_path: Path):
    items = tmp_path / "items"
    items.mkdir()
    templates = tmp_path / "templates"
    templates.mkdir()
    src = paths.vault_root() / "Todo" / "templates" / "auto-worker-task-template.md"
    shutil.copy(src, templates / "auto-worker-task-template.md")
    return tmp_path, items, templates


def _gen(items, templates):
    return rescope.generate_subtasks(
        _plan(), parent_slug=PARENT, parent_fm=_parent_fm(),
        items_dir=items, templates_dir=templates,
        run_ref="Worker/runs/20261010T175652-unified-projects/run.log")


def test_generates_three_valid_chained_subtasks(gen_env):
    _tmp, items, templates = gen_env
    paths_out = _gen(items, templates)
    assert [p.stem for p in paths_out] == [S0, S1, S2]
    for p in paths_out:
        assert p.exists()

    fms = {p.stem: worker.parse_frontmatter(p.read_text(encoding="utf-8"))[0] for p in paths_out}

    # position 0: auto, no deps
    fm0 = fms[S0]
    assert fm0["worker_mode"] == "auto"
    assert "depends_on" not in fm0 or not fm0["depends_on"]
    assert fm0["parent"] == f"[[{PARENT}]]"
    assert fm0["auto_rescoped_from"] == PARENT
    assert fm0["worker_branch"] == f"worker/{S0}"

    # inheritance from the parent contract
    assert fm0["repo"] == "https://github.com/example/UnifiedApp.git"
    assert str(fm0["auto_merge"]).lower() == "true"
    assert fm0["model_tier"] == "heavy"
    assert "Bash(git:*)" in fm0["allowed_tools"] and isinstance(fm0["allowed_tools"], str)

    # positions 1,2: blocked + on_unblock auto + serial depends_on
    assert fms[S1]["worker_mode"] == "blocked"
    assert fms[S1]["on_unblock"] == "auto"
    assert fms[S1]["depends_on"] == [S0]
    assert fms[S2]["worker_mode"] == "blocked"
    assert fms[S2]["depends_on"] == [S1]

    # body carries the decided-design section + a design note
    body0 = paths_out[0].read_text(encoding="utf-8")
    assert "## 이미 정해진 설계" in body0
    assert "오프셋 부호" in body0

    # every subtask passes the worker contract (no ERROR-level issues)
    for p in paths_out:
        errors = [i for i in _task_validate.validate_file(p) if i.level == _task_validate.ERROR]
        assert errors == [], f"{p.stem}: {[i.message for i in errors]}"


def test_serial_chain_unblocks_in_order(gen_env):
    _tmp, items, templates = gen_env
    _gen(items, templates)
    nodb = _tmp / "nodb.db"

    # before any child is done, nothing unblocks
    flips, _ = task_deps.resolve_unblocks(task_deps.scan_items(items), nodb)
    assert flips == []

    # mark the first child done -> the second becomes auto-dispatchable
    from todo_system import cli
    p0 = items / f"{S0}.md"
    p0.write_text(cli.update_frontmatter_value(p0.read_text(encoding="utf-8"), "status", "done"),
                  encoding="utf-8")
    flips, _ = task_deps.resolve_unblocks(task_deps.scan_items(items), nodb)
    assert (S1, "auto") in flips
    assert (S2, "auto") not in flips      # its dep (S1) is not done yet


def test_generate_is_idempotent(gen_env):
    _tmp, items, templates = gen_env
    _gen(items, templates)
    before = sorted(p.name for p in items.glob("*.md"))
    paths_out = _gen(items, templates)        # second call: dedup
    after = sorted(p.name for p in items.glob("*.md"))
    assert before == after
    assert [p.stem for p in paths_out] == [S0, S1, S2]


def test_flip_parent_to_bundle(gen_env):
    _tmp, items, templates = gen_env
    parent = items / f"{PARENT}.md"
    parent.write_text(
        "---\ntags:\n  - todo\nstatus: open\nworker_mode: auto\n"
        "outcome: 통합\n---\n\n# 통합 프로젝트\n\n## Progress Log\n\n- 2026-10-10: 생성\n",
        encoding="utf-8")
    rescope.flip_parent_to_bundle(parent, [S0, S1, S2])
    fm, body = worker.parse_frontmatter(parent.read_text(encoding="utf-8"))
    assert fm["worker_mode"] == "off"
    assert str(fm["auto_rescoped"]).lower() == "true"
    assert str(fm["is_parent"]).lower() == "true"
    assert "자동 분할 → 하위 3개" in body
