#!/usr/bin/env bash
#
# export_engine_tree.sh — 공개 분리 10: 엔진 트리 단독 실행 재현 스크립트
#
# `check_public_identifiers.sh --export` 와 **같은 집합**을 <dest> 에 복원해,
# 공개 엔진 export 리포의 미래 상태(볼트에서 떼어낸 엔진만)를 로컬에서 재현한다.
# 집합 = 엔진 트리 3경로(todo_system · scripts · .claude/commands)
#        − 게이트 자기제외 3(check_public_identifiers.sh · public_identifiers_baseline.txt
#          · todo_system/tests/test_check_public_identifiers.py)
#        − scripts/public_export_exclude.txt 의 볼트 이동분
#      + pyproject.toml · uv.lock
#      + vault-template/ (식별자 0 인 Todo/templates + 빈 Todo/items·Memory·Notes·Decisions·Worker).
#
# dest 는 git 리포로 초기화하고, 커버리지 규칙을 담은 .gitignore 를 심는다 — 공개
# 리포 gitignore 갭(인벤토리 §11.8 c-2: htmlcov/·coverage.xml 누락)을 이 재현
# 트리에서 사전 차단하기 위함이다(공개 리포 자체의 .gitignore 수정은 별도 사람
# 게이트 leaf). test_gitignore_coverage_artifacts·test_public_gates_real_tree 는
# dest 가 git 체크아웃일 것을 전제하므로 git init 이 필수다.
#
# 자매 선례 scripts/check_engine_isolation.sh 골격(REPO=git rev-parse,
# git archive HEAD, 범위 한정 정리, trap 없는 영속 dest)을 따른다. 이 파일은
# scripts/ 스코프 안이라 개인 식별자 리터럴을 담지 않는다
# ([[public-split-standing-gates]]) — 식별자 패턴은 canonical 게이트
# scripts/check_public_identifiers.sh 에서 런타임에 추출해 템플릿을 거른다.
#
# 사용법:
#   bash scripts/export_engine_tree.sh <dest>          # dest 는 없거나 비어 있어야 함
# 단독 실행 검증(0 failed / 0 errors, skip 은 vault-only 사유만):
#   uv run --env-file "<dest>/standalone.env" --directory "<dest>" --with pytest \
#     python -m pytest -q -rsfE todo_system scripts/systemd
#   (standalone.env 한 줄: MACP_VAULT_ROOT=<dest>/vault-template)
#
set -euo pipefail

dest="${1:-}"
if [ -z "$dest" ]; then
  echo "usage: $0 <dest>" >&2
  exit 2
fi
# 안전: 존재하고 비어있지 않은 dest 는 거부(광역 덮어쓰기 방지). 신규/빈 dest 만.
if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
  echo "ERROR: dest exists and is not empty: $dest" >&2
  exit 2
fi
mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"   # 절대경로로 정규화

REPO="$(git rev-parse --show-toplevel)"
EXPORT_EXCLUDE="$REPO/scripts/public_export_exclude.txt"
if [ ! -f "$EXPORT_EXCLUDE" ]; then
  echo "ERROR: export exclude list not found: $EXPORT_EXCLUDE" >&2
  exit 2
fi

echo "== export_engine_tree =="
echo "repo: $REPO"
echo "dest: $dest"
echo

# --- Phase 1: 엔진 트리 3경로 + 매니페스트를 committed 트리(HEAD)에서 복원 ----
# git archive 는 커밋된 트리만 반영한다 — 미커밋 편집은 재현 대상이 아니다.
echo "[1] archiving engine tree (todo_system scripts .claude/commands + manifest)"
git -C "$REPO" archive HEAD todo_system scripts .claude/commands pyproject.toml uv.lock \
  | tar -x -C "$dest"

# --- Phase 2: 게이트 자기제외 3 + 제외 목록 제거 (범위 한정, 글롭 없음) -------
remove_one() {
  # 한 번에 하나, 글롭 없음 — 광역 삭제 금지.
  local rel="$1"
  if [ -e "$dest/$rel" ]; then
    rm -f -- "$dest/$rel"
  fi
}

# check_public_identifiers.sh SCOPE 의 자기제외 3 과 byte-identical.
remove_one "scripts/check_public_identifiers.sh"
remove_one "scripts/public_identifiers_baseline.txt"
remove_one "todo_system/tests/test_check_public_identifiers.py"

# 제외 목록(공개 분리 9): 한 줄에 리포 상대 경로, `#` 이후 주석, 빈 줄 무시,
# 글롭(`*` `?` `[`) 금지 — canonical 게이트의 --export 파싱과 동일 규칙.
n_excluded=0
while IFS= read -r raw || [ -n "$raw" ]; do
  p="${raw%%#*}"
  p="${p#"${p%%[![:space:]]*}"}"
  p="${p%"${p##*[![:space:]]}"}"
  [ -n "$p" ] || continue
  case "$p" in
    *'*'* | *'?'* | *'['*)
      echo "ERROR: glob in exclude entry is not allowed (list files one by one): $p" >&2
      exit 2
      ;;
  esac
  remove_one "$p"
  n_excluded=$(( n_excluded + 1 ))
done < "$EXPORT_EXCLUDE"
echo "[2] removed self-exclude 3 + $n_excluded exclude-list entries"

# --- Phase 3: vault-template (빈 골격 + 식별자 0 템플릿) ----------------------
vt="$dest/vault-template"
mkdir -p \
  "$vt/Todo/items" \
  "$vt/Todo/templates" \
  "$vt/Memory/sessions" \
  "$vt/Memory/episodes" \
  "$vt/Notes" \
  "$vt/Decisions" \
  "$vt/Worker"

# 개인 식별자 패턴을 canonical 게이트에서 런타임 추출한다 — 이 파일에 리터럴을
# 담지 않기 위함(scripts/ 스코프라 게이트가 자신을 검사한다). 추출 실패 시 fail-loud.
PATTERN="$(git -C "$REPO" show HEAD:scripts/check_public_identifiers.sh \
           | grep -oP "^PATTERN='\K[^']*" || true)"
if [ -z "$PATTERN" ]; then
  echo "ERROR: could not extract identifier PATTERN from check_public_identifiers.sh" >&2
  exit 2
fi

# Todo/templates 중 식별자 히트 0 인 것만 복사(vault-template 은 공개 리포에 그대로
# 실리므로 식별자가 없어야 한다). auto-worker-task-template.md 는 히트 0 이라 포함되며
# test_task_validate 가 이를 요구한다.
n_tmpl=0
while IFS= read -r tmpl; do
  [ -n "$tmpl" ] || continue
  if git -C "$REPO" grep -I -P -q "$PATTERN" HEAD -- "$tmpl"; then
    echo "    skip (identifier hit): $tmpl"
    continue
  fi
  git -C "$REPO" show "HEAD:$tmpl" > "$vt/$tmpl"
  n_tmpl=$(( n_tmpl + 1 ))
done < <(git -C "$REPO" ls-files 'Todo/templates/*.md')
echo "[3] vault-template ready ($n_tmpl identifier-free template(s) copied)"

# --- Phase 4: dest .gitignore (커버리지 4규칙 포함 → c-2 사전 차단) -----------
cat > "$dest/.gitignore" <<'GITIGNORE_EOF'
# Python / uv 런타임
.venv/
__pycache__/
*.pyc
.pytest_cache/
# coverage 산출물 — test_gitignore_coverage_artifacts 가 이 4규칙을 요구한다.
# 공개 리포 gitignore 갭(인벤토리 §11.8 c-2: htmlcov/·coverage.xml 누락)을 이
# 재현 트리에서 사전 차단한다.
.coverage
.coverage.*
htmlcov/
coverage.xml
# 개발 체크아웃에 wheel 레이아웃 마커가 섞이면 paths.engine_root()가 설치본으로 오판한다
# (test_gitignore_blocks_a_stray_engine_marker_in_the_checkout).
todo_system/_engine/
# 파생 캐시 · 실행 산출물
Todo/todo.db
standalone-junit.xml
standalone.env
# 자격증명·로컬 설정 — 엔진 체크아웃에서 실행하다 생겨도 커밋되지 않게(볼트 .secrets/ 와 같은 부류)
.secrets/
.env
*.env
.macp/
GITIGNORE_EOF

# --- Phase 5: dest 를 git 리포로 (격리·gitignore 테스트가 git 체크아웃 전제) --
# dest 는 caller 제공 영속 산출물이므로 EXIT trap 으로 지우지 않는다.
git -C "$dest" init -q
git -C "$dest" add -A
git -C "$dest" -c user.email=macp@example.com -c user.name=macp \
  commit -q -m "export engine tree (public-split-10 standalone reproduction)"

# standalone.env 생성(gitignore 됨) — 검증 명령이 --env-file 로 참조.
printf 'MACP_VAULT_ROOT=%s/vault-template\n' "$dest" > "$dest/standalone.env"

echo "[5] dest git-initialized + committed; standalone.env written"
echo
echo "RESULT: engine export tree ready at $dest"
echo "verify: uv run --env-file $dest/standalone.env --directory $dest --with pytest \\"
echo "          python -m pytest -q -rsfE todo_system scripts/systemd"
