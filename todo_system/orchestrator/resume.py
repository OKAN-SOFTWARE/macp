"""조건부 지연 재개(resume-when) — "볼트가 커밋 X를 반영한 뒤에 이 태스크를 재디스패치하라".

2026-09-25 `ai-stock-trading-lowturnover-ic-to-sharpe`가 awaiting_human으로 멈췄다.
태스크 md에 `long_job`을 붙여 볼트 main에 push했지만 `tasks.py`는 payload의
`long_job`을 **라이브 볼트 체크아웃의 md**에서 읽고, 그 체크아웃은 다른 볼트 태스크의
워커 브랜치에 올라가 있었다. 그 상태로 assignment를 되돌리면 `long_job` 없이 전체
파이프라인이 다시 돈다. 그래서 사람이 `/tmp/lt-reset.sh`(1분 폴링 → 조건 충족 시
`Worker/assignments/<slug>.json` 수동 편집)를 손으로 띄워야 했고, 그 임시 스크립트는
권한 분류기에 막혀 승인까지 받아야 했다. 이 패턴(태스크 md 수정 → 볼트 동기화 대기 →
재개)은 반복된다. 이 모듈이 그 감시 스크립트를 macp 기능으로 흡수한다.

예약 레코드는 `Worker/_state/pending_resumes/<slug>.json`에 산다. `Worker/_state/`는
gitignored(`.gitignore:139`)라서 이 레코드는 **호스트 로컬 런타임 상태**다 — 두 호스트가
같은 볼트를 push하므로, "이 체크아웃의 HEAD가 어디까지 왔는가"에 걸린 예약을 커밋하면
다른 호스트가 남의 조건을 소비하게 된다.

설계 제약:

- **stdlib만.** 이 코드는 오케스트레이터 틱 경로에서 돌고, 틱은 베어 `python3`로도
  떠야 한다(`assignment.py`·`session_log`와 같은 제약).
- **판정은 순수 함수로.** `evaluate()`는 I/O를 하지 않는다 — git 결과와 ledger 상태를
  인자로 받는다. 그래서 각 분기가 실제 git 없이 단위 테스트된다.
- **모든 불확실은 "아직 아님"(`WAIT`)으로 fail-safe.** git이 실패했는지, sha가 정말
  조상이 아닌지 구분할 수 없을 때 재개하면 조건 없이 파이프라인을 다시 사는 셈이다.
  단 하나의 예외가 **만료**다: 깨진/지난 만료 시각은 무한 대기가 아니라 폐기로 간다.
  "조용히 남아 있지 않는다"가 인수조건이다.
- **경로는 전부 인자로 주입.** `vault_root`는 모든 공개 함수의 필수 인자다(틱은
  `ledger.root.parent.parent`, CLI는 `paths.vault_root()`). 모듈이 루트를 스스로
  계산하지 않으므로 테스트가 `tmp_path`를 벗어날 수 없다.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from .assignment import AWAITING_HUMAN, FAILED, RESUMED_SCHEDULED

DEFAULT_EXPIRE_HOURS = 24

# 재개를 받아줄 수 있는 상태. AWAITING_HUMAN은 `TERMINAL`에 없고(그래야 dispatch가
# 파킹된 행을 멋대로 다시 돌리지 않는다) FAILED는 정상 재시도 경로다.
RESUMABLE_STATES = frozenset({AWAITING_HUMAN, FAILED})

# evaluate()의 반환값
RESUME = "resume"    # 세 조건 모두 충족 — 지금 재개한다
WAIT = "wait"        # 아직 아님(또는 판정 불가) — 레코드를 남겨두고 다음 틱에 다시 본다
STALE = "stale"      # 누가 이미 재개했거나 done — assignment를 건드리지 않고 예약만 폐기
EXPIRED = "expired"  # 만료 — assignment 미변경, 예약 폐기 + 경보 1회

# 한 스윕에서 같은 원인의 스킵 줄을 몇 개까지 찍을지 (orchestrator._SKIP_LINE_CAP과 같은 값).
_SKIP_LINE_CAP = 3


class ResumeError(RuntimeError):
    """예약을 받을 수 없다 — CLI가 이것을 비0 종료로 바꾼다."""


def resume_dir(vault_root: str | Path) -> Path:
    return Path(vault_root) / "Worker" / "_state" / "pending_resumes"


def record_path(vault_root: str | Path, slug: str) -> Path:
    return resume_dir(vault_root) / f"{slug}.json"


def _now() -> dt.datetime:
    """tz-aware 현재 시각. `message.now_iso()`와 같은 모양을 쓴다 — ledger의
    `updated_ts`는 `dt.datetime.fromisoformat`으로 파싱되므로 offset이 붙어야 한다."""
    return dt.datetime.now().astimezone().replace(microsecond=0)


def _iso(when: dt.datetime) -> str:
    return when.replace(microsecond=0).isoformat()


def _parse_ts(value, *, tz) -> dt.datetime | None:
    """ISO 문자열을 tz-aware datetime으로. 파싱 불가면 None.

    naive 값은 `tz`를 붙여 준다 — 사람이 손으로 쓴 레코드 하나가 비교에서
    TypeError를 내며 스윕을 흔들게 두지 않는다.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def is_expired(record: dict, now: dt.datetime) -> bool:
    """만료 여부. `expires_ts`가 없거나 깨졌으면 **만료로 본다**.

    깨진 만료 시각을 "무한 대기"로 읽으면 아무도 지우지 않는 예약이 영원히 남아
    조용히 조건을 기다린다 — 인수조건이 금지하는 상태다. 되살릴 수 없는 레코드는
    폐기하고 사람에게 알리는 쪽이 안전하다.
    """
    expires = _parse_ts(record.get("expires_ts"), tz=now.tzinfo)
    if expires is None:
        return True
    return now > expires


def evaluate(record: dict, *, branch: str | None, head_contains: bool | None,
             state: str | None, now: dt.datetime) -> str:
    """"지금 재개해도 되는가"를 판정하는 **순수 함수**. I/O 없음.

    판정 순서가 이 함수의 본질이다:

    1. **만료를 최우선으로** 본다. 만료된 예약이 조건을 마침내 충족했다고 조용히
       실행되면 사람이 24시간 전에 건 예약이 지금 파이프라인을 사는 셈이다.
    2. `branch != "main"` → `WAIT`. `branch is None`(= git 실패)도 `WAIT`다.
       라이브 체크아웃이 남의 워커 브랜치에 있으면 태스크 md도 그 브랜치 것이고,
       바로 그것이 이 기능이 존재하는 이유(원래 사고)다.
    3. `after_commit`이 있는데 `head_contains is not True` → `WAIT`.
       `None`(판정 불가)을 `False`(조상 아님)와 한 덩어리로 묶는 것이 아니라,
       둘 다 "아직 아님"으로 보낸다 — 어느 쪽이든 재개하면 안 되므로.
    4. state가 `awaiting_human`/`failed`가 아니면 → `STALE`. 누가 이미 재개했거나
       done이다. assignment를 건드리는 것이 오히려 해가 된다.
    5. 그 외 → `RESUME`.
    """
    if is_expired(record, now):
        return EXPIRED
    if branch != "main":
        return WAIT
    if record.get("after_commit") and head_contains is not True:
        return WAIT
    if state not in RESUMABLE_STATES:
        return STALE
    return RESUME


def schedule(vault_root: str | Path, slug: str, *, after_commit: str | None = None,
             expire_hours: float = DEFAULT_EXPIRE_HOURS, note: str = "",
             now: dt.datetime | None = None) -> Path:
    """예약 레코드를 쓴다. 받을 수 없는 예약은 `ResumeError`로 거부한다.

    `Todo/items/<slug>.md`와 `Worker/assignments/<slug>.json`이 **둘 다** 있어야
    한다: md만 있으면 재개할 대상 자체가 없고(아직 한 번도 디스패치되지 않았다),
    assignment만 있으면 dispatch가 태스크를 만들지 못해 재개가 헛돈다. 어느 쪽이
    없는지 메시지에 적는다 — 사람이 오타와 "아직 안 돌았다"를 구분해야 한다.
    """
    root = Path(vault_root)
    slug = (slug or "").strip()
    if not slug:
        raise ResumeError("slug가 비어 있습니다")
    task_md = root / "Todo" / "items" / f"{slug}.md"
    if not task_md.is_file():
        raise ResumeError(f"태스크 파일이 없습니다: {task_md} — slug 오타인지 확인하세요")
    assignment = root / "Worker" / "assignments" / f"{slug}.json"
    if not assignment.is_file():
        raise ResumeError(
            f"assignment가 없습니다: {assignment} — 아직 디스패치되지 않은 태스크는 "
            f"재개할 것이 없습니다(worker_mode를 auto로 두면 그냥 큐에 들어갑니다)")
    if expire_hours <= 0:
        raise ResumeError(f"--expire-hours는 0보다 커야 합니다: {expire_hours}")

    when = now or _now()
    record = {
        "slug": slug,
        "after_commit": (after_commit or "").strip(),
        "created_ts": _iso(when),
        "expires_ts": _iso(when + dt.timedelta(hours=expire_hours)),
        "note": note or "",
    }
    path = record_path(root, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def _git(root: Path, args: list[str], _run=None, *, timeout: float = 30):
    run = _run or subprocess.run
    return run(["git", *args], cwd=root, capture_output=True, text=True,
               timeout=timeout)


def current_branch(root: str | Path, _run=None) -> str | None:
    """라이브 체크아웃의 현재 브랜치. 알 수 없으면 `None`(= 호출자는 대기).

    `_pull_main`의 HEAD==main 가드와 같은 형태다(`orchestrator.py:1680`).
    """
    try:
        p = _git(Path(root), ["rev-parse", "--abbrev-ref", "HEAD"], _run)
    except Exception:
        return None
    if getattr(p, "returncode", 1) != 0:
        return None
    return (p.stdout or "").strip() or None


def head_contains(root: str | Path, sha: str, _run=None) -> bool | None:
    """`sha`가 HEAD의 조상인가. `True`/`False`/`None`(판정 불가).

    rc 1과 그 밖의 rc를 **구분하는 것이 핵심**이다. `git merge-base --is-ancestor`는
    조상이 아니면 rc 1, 존재하지 않는 ref면 rc 128로 죽는다. 둘을 한 덩어리로
    `False`로 뭉개면 "아직 볼트에 오지 않은 sha"가 영구 거절로 읽히고, 반대로 둘 다
    `True` 쪽으로 뭉개면 조건 없이 재개된다. 없는 sha는 "아직 모른다"가 정답이다.
    """
    if not sha:
        return None
    try:
        p = _git(Path(root), ["merge-base", "--is-ancestor", sha, "HEAD"], _run)
    except Exception:
        return None
    rc = getattr(p, "returncode", None)
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def load_records(vault_root: str | Path) -> tuple[list[tuple[Path, dict]],
                                                  list[tuple[Path, str]]]:
    """(읽힌 레코드, 읽지 못한 레코드) 두 목록.

    파싱 실패한 파일은 **지우지 않는다**. 의도를 복구할 수 없는 파일을 조용히
    삭제하면 사람이 건 예약이 흔적 없이 사라진다 — 호출자가 한 줄 로그로 드러내고
    사람이 손으로 판단하게 둔다. dict가 아닌 JSON(리스트·숫자)도 읽지 못한 것으로
    센다: 아래의 `record.get`이 AttributeError로 터지는 것보다 낫다.
    """
    good: list[tuple[Path, dict]] = []
    bad: list[tuple[Path, str]] = []
    directory = resume_dir(vault_root)
    if not directory.is_dir():
        return good, bad
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as e:
        return good, [(directory, f"{type(e).__name__}: {e}")]
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            bad.append((path, f"{type(e).__name__}: {e}"))
            continue
        if not isinstance(data, dict):
            bad.append((path, f"레코드가 객체가 아닙니다: {type(data).__name__}"))
            continue
        good.append((path, data))
    return good, bad


class _Tally:
    """Caps repeated identical skip lines within ONE sweep.

    `orchestrator._SkipTally`와 같은 역할·같은 상한이지만 **import하지 않는다**:
    `orchestrator`가 이 모듈을 부르므로 반대 방향 import는 순환이 된다. 스윕마다
    새로 만들고 함께 버린다 — 틱을 넘어 사는 tally는 두 번째 틱부터 조용해지고,
    그건 홍수를 막는 게 아니라 홍수가 **안 보이게** 만드는 것이다.
    """

    def __init__(self, emit, cap: int = _SKIP_LINE_CAP):
        self.emit = emit
        self.cap = cap
        self.counts: dict[str, int] = {}

    def record(self, subject: str, cause: str) -> None:
        n = self.counts.get(cause, 0) + 1
        self.counts[cause] = n
        if n <= self.cap:
            self.emit(f"{subject} 건너뜀: {cause}")

    def flush(self) -> None:
        for cause, n in self.counts.items():
            if n > self.cap:
                self.emit(f"…같은 원인으로 {n - self.cap}건 더 건너뜀: {cause}")


def _default_log(msg: str) -> None:
    print(f"[orchestrator] resume: {msg}")


def consume(ledger, vault_root: str | Path, *, backdate_s: float,
            now: dt.datetime | None = None, notify=None, log=None,
            _run=None) -> list[str]:
    """예약 레코드를 소비한다. 재개된 slug 목록을 돌려준다. **절대 raise하지 않는다.**

    이 함수는 오케스트레이터 틱의 dispatch **앞**에서 돌기 때문에, 여기서 올라간
    예외 하나가 그 틱의 디스패치 전체를 멈춘다. 그래서 바깥에 한 겹, 레코드마다 한 겹
    `try/except`를 두고 스킵은 `_Tally`로 상한을 둔 채 로그만 남긴다
    (`reconcile_unparked`의 per-row 격리와 같은 구조 — 한 행의 결함이 그 뒤로 정렬된
    모든 행을 조용히 굶기던 사고를 막은 그 구조다).

    `RESUME`일 때 손대는 필드는 인수조건이 명시한 **4개 + 기록 2개**뿐이다:

        refail_count = 0  →  state = FAILED  →  blocked_on = ""  →  updated_ts = 과거

    **순서가 중요하다.** `refail_count`를 먼저 0으로 내리지 않고 `state`만 FAILED로
    바꾸면 다음 틱의 dispatch가 누적된 refail을 보고 곧바로 재격리(poison
    quarantine)한다. `blocked_on`이 남아 있으면 re-dispatch 머신 자체가 멈춘다.
    `updated_ts`를 과거로 백데이트하는 이유는 dispatch가 거기에 fail 쿨다운을 앵커하기
    때문이다(`orchestrator.py:357`) — 지금 시각을 찍으면 사람의 "지금 재개"가 한 쿨다운
    창을 더 기다린다. 백데이트 폭은 상수로 박지 않고 호출자가 `backdate_s`로 준다:
    오케스트레이터만이 자기 쿨다운 상한을 안다.

    `timeout_streak`·`timed_out`·`capacity_*`·`job_running`·`checkout_busy`는 **손대지
    않는다**(스코프 밖). 그래서 `timeout_streak >= 2`가 남은 행은 재개돼도 다음
    `collect()`에서 곧바로 재파킹될 수 있다 — 알려진 한계이고 `Worker/README.md`에
    적어 두었다.

    `notify(msg, event_key=...)`는 선택이다. 알림 실패가 재개를 되돌리지 않도록
    ledger 저장 **뒤**에 부르고, 그 자체도 삼킨다.
    """
    resumed: list[str] = []
    emit = log or _default_log
    try:
        root = Path(vault_root)
        moment = now or _now()
        tally = _Tally(emit)
        records, unreadable = load_records(root)
        for path, cause in unreadable:
            # 지우지 않는다 — 사람이 건 예약의 의도를 복구할 수 없으므로 드러내고 남긴다.
            tally.record(f"{path.name} (읽기 실패, 보존)", cause)
        for path, record in records:
            try:
                slug = str(record.get("slug") or path.stem)
                a = ledger.load(slug)
                sha = str(record.get("after_commit") or "")
                if is_expired(record, moment):
                    verdict = EXPIRED
                elif a is None:
                    # ledger에 행이 없으면 재개할 대상이 없다 — 예약만 폐기한다.
                    verdict = STALE
                else:
                    verdict = evaluate(
                        record,
                        branch=current_branch(root, _run),
                        head_contains=head_contains(root, sha, _run) if sha else None,
                        state=a.state,
                        now=moment,
                    )

                if verdict == WAIT:
                    # 레코드 유지. 조건이 뭐였는지 매 틱 한 줄씩 남긴다 — digest 알림에
                    # 묻히더라도 run 로그에는 "왜 아직 안 돌았나"가 보여야 한다.
                    emit(f"{slug}: 아직 아님 — 대기 유지 "
                         f"(after_commit={sha or '-'}, expires={record.get('expires_ts')})")
                    continue

                if verdict == RESUME:
                    a.refail_count = 0
                    a.state = FAILED
                    a.blocked_on = ""
                    a.updated_ts = _iso(moment - dt.timedelta(seconds=backdate_s))
                    a.last_note = (
                        f"예약 재개: 볼트 main이 {sha or 'HEAD'}를 반영 — 재디스패치")[:200]
                    a.history.append({"ts": _iso(moment), "type": RESUMED_SCHEDULED,
                                      "from": "orchestrator", "after_commit": sha,
                                      "note": str(record.get("note") or "")})
                    ledger.save(a)
                    path.unlink(missing_ok=True)
                    resumed.append(slug)
                    emit(f"{slug}: 조건 충족 — refail=0/state=failed/blocked_on='' "
                         f"+ updated_ts -{backdate_s:.0f}s, 예약 소비 "
                         f"(after_commit={sha or '-'})")
                    _notify(notify, f"♻️ 예약 재개: {slug} — 볼트 main이 "
                                    f"{sha or 'HEAD'}를 반영해 재디스패치합니다",
                            f"resume:{slug}", emit)
                elif verdict == STALE:
                    # assignment는 건드리지 않는다. 누가 이미 재개했거나 done이다.
                    path.unlink(missing_ok=True)
                    state = getattr(a, "state", None)
                    emit(f"{slug}: state={state or '행 없음'} — 이미 처리됨, "
                         f"assignment 미변경·예약만 폐기")
                elif verdict == EXPIRED:
                    path.unlink(missing_ok=True)
                    emit(f"{slug}: 예약 만료(expires={record.get('expires_ts')}) — "
                         f"assignment 미변경·예약 폐기")
                    _notify(notify, f"⌛ 예약 재개 만료: {slug} — 조건이 만료 전에 "
                                    f"충족되지 않아 예약을 지웠습니다(재개 안 됨)",
                            f"resume-expired:{slug}", emit)
            except Exception as e:
                tally.record(path.name, f"{type(e).__name__}: {e}")
        tally.flush()
    except Exception as e:
        # 바깥 그물: load_records·경로 계산·로거 자체가 실패해도 틱으로 올리지 않는다.
        emit(f"sweep aborted: {type(e).__name__}: {e}")
    return resumed


def _notify(notify, msg: str, event_key: str, emit) -> None:
    """알림은 곁가지다 — 실패해도 이미 저장된 재개를 되돌리지 않는다."""
    if notify is None:
        return
    try:
        notify(msg, event_key=event_key)
    except Exception as e:
        emit(f"알림 실패(무시): {type(e).__name__}: {e}")
