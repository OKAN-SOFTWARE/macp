"""No test may write into this checkout's live mailbox tree.

Pins the live-mailbox half of todo_system/conftest.py: the tripwire's subtree
protection of ``<checkout>/Worker/agents``, and the ``Mailbox.__init__``
redirect in front of it. A leaked TASK_ASSIGN there once rode a squash merge
into main and dispatched no-op runs.

The class and the refusal are reached through the ``runtime_tripwire`` fixture
only — importing the conftest module from a test risks a second audit hook.
Every direct hook call below does no I/O; the one test that performs real file
operations is refused before anything is created.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from todo_system.orchestrator import mailbox
from todo_system.orchestrator import message as m

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE = REPO_ROOT / mailbox.DEFAULT_ROOT
INBOX = LIVE / "worker-default" / "inbox"
_W = os.O_WRONLY | os.O_CREAT | os.O_TRUNC


def _envelope_path(name="x.json"):
    return str(INBOX / name)


def test_session_tripwire_protects_the_live_mailbox_tree(runtime_tripwire):
    assert os.path.realpath(LIVE) in runtime_tripwire.trees


def test_red_control_hook_without_tree_lets_live_inbox_write_through(runtime_tripwire):
    # The pre-fix wire: files only, no trees. It lets an envelope write through.
    tw = type(runtime_tripwire)
    bare = tw(())
    args = (_envelope_path(), "w", _W)
    assert bare("open", args) is None
    assert bare.hits == []
    # The same call against a wire that protects the tree is refused.
    guarded = tw((), trees=(LIVE,))
    with pytest.raises(runtime_tripwire.error):
        guarded("open", args)
    assert [(h.event, h.path) for h in guarded.hits] == [
        ("open", os.path.realpath(_envelope_path()))]


_REFUSED = {
    "open-w": lambda: ("open", (_envelope_path(), "w", _W)),
    "open-a": lambda: ("open", (_envelope_path(), "a", os.O_WRONLY | os.O_CREAT | os.O_APPEND)),
    "open-x": lambda: ("open", (_envelope_path(), "x", os.O_WRONLY | os.O_CREAT | os.O_EXCL)),
    "open-r+": lambda: ("open", (_envelope_path(), "r+", os.O_RDWR)),
    "os.open": lambda: ("open", (_envelope_path(), None, os.O_CREAT | os.O_RDWR)),
    "cursor": lambda: ("open", (str(LIVE / "_state" / "worker-default" / "cursor.json"), "w", _W)),
    "gitkeep": lambda: ("open", (_envelope_path(".gitkeep"), "a", os.O_WRONLY | os.O_CREAT)),
    "remove": lambda: ("os.remove", (_envelope_path(), -1)),
    "rename-src": lambda: ("os.rename", (_envelope_path(), "/tmp/elsewhere.json", -1, -1)),
    "rename-dst": lambda: ("os.rename", ("/tmp/elsewhere.json", _envelope_path(), -1, -1)),
    "utime": lambda: ("os.utime", (_envelope_path(), None, None, -1)),
    "truncate": lambda: ("os.truncate", (_envelope_path(), 0)),
    "link": lambda: ("os.link", ("/tmp/elsewhere.json", _envelope_path(), -1, -1)),
    "symlink": lambda: ("os.symlink", ("/tmp/elsewhere.json", _envelope_path(), -1)),
    "sqlite": lambda: ("sqlite3.connect", (str(LIVE / "x.db"),)),
    "rmtree-root": lambda: ("shutil.rmtree", (str(LIVE), None)),
    "rmtree-ancestor": lambda: ("shutil.rmtree", (str(LIVE.parent), None)),
    "rmtree-inside": lambda: ("shutil.rmtree", (str(LIVE / "_state"), None)),
    "tree-root-itself": lambda: ("os.utime", (str(LIVE), None, None, -1)),
}


@pytest.mark.parametrize("case", sorted(_REFUSED))
def test_every_write_event_in_the_tree_is_refused(case, runtime_tripwire):
    event, args = _REFUSED[case]()
    tw = type(runtime_tripwire)((), trees=(LIVE,))
    with pytest.raises(runtime_tripwire.error):
        tw(event, args)
    assert [h.event for h in tw.hits] == [event]


@pytest.mark.parametrize("case", ["registry-read", "sibling-prefix", "tmp", "fd"])
def test_reads_and_outside_paths_pass(case, runtime_tripwire, tmp_path):
    event, args = {
        # The live registry is read by every orchestrator entry point.
        "registry-read": ("open", (str(LIVE / "registry.yaml"), "r", os.O_RDONLY)),
        # Prefix on a path boundary, not on characters.
        "sibling-prefix": ("open", (str(LIVE.parent / (LIVE.name + "-other") / "x.json"), "w", _W)),
        "tmp": ("open", (str(tmp_path / "x.json"), "w", _W)),
        "fd": ("os.truncate", (3, 0)),
    }[case]
    tw = type(runtime_tripwire)((), trees=(LIVE,))
    assert tw(event, args) is None
    assert tw.hits == []


def test_relative_path_from_the_checkout_root_is_refused(runtime_tripwire, monkeypatch):
    # The mailbox root is cwd-relative, which is how the leak happened.
    monkeypatch.chdir(REPO_ROOT)
    tw = type(runtime_tripwire)((), trees=(LIVE,))
    with pytest.raises(runtime_tripwire.error):
        tw("open", (str(Path(mailbox.DEFAULT_ROOT) / "x.json"), "w", _W))
    assert [h.path for h in tw.hits] == [os.path.realpath(LIVE / "x.json")]


def test_real_writes_into_the_live_inbox_are_refused(runtime_tripwire, tmp_path):
    # Real file operations through the installed hook. The refusal comes before
    # the open, so nothing is created; no directory is made here either.
    target = INBOX / f"guard-{uuid4().hex}.json"
    staged = tmp_path / "envelope.json"
    staged.write_text("{}", encoding="utf-8")
    session_before = len(runtime_tripwire.hits)

    with runtime_tripwire.guarding(INBOX, tree=True) as hits:
        with pytest.raises(runtime_tripwire.error):
            target.write_text("{}", encoding="utf-8")
        with pytest.raises(runtime_tripwire.error):
            os.replace(staged, target)                  # atomic_write_json's last step

    assert [h.event for h in hits] == ["open", "os.rename"]
    assert {h.path for h in hits} == {os.path.realpath(target)}
    assert not target.exists()
    assert staged.exists()
    assert len(runtime_tripwire.hits) == session_before


def test_file_guard_inside_a_tree_guard_keeps_its_own_hits(runtime_tripwire, tmp_path):
    # guarding() of a single file (the RED-control path of the file rows) still
    # works next to a tree, and wins over it for its own file.
    tree = tmp_path / "tree"
    tree.mkdir()
    one = tree / "one"
    other = tree / "other"
    session_before = len(runtime_tripwire.hits)

    with runtime_tripwire.guarding(tree, tree=True) as tree_hits, \
            runtime_tripwire.guarding(one) as one_hits:
        with pytest.raises(runtime_tripwire.error):
            one.write_text("x", encoding="utf-8")
        with pytest.raises(runtime_tripwire.error):
            other.write_text("x", encoding="utf-8")

    assert [h.path for h in one_hits] == [os.path.realpath(one)]
    assert [h.path for h in tree_hits] == [os.path.realpath(other)]
    assert len(runtime_tripwire.hits) == session_before
    # Both guards are gone after the block.
    one.write_text("x", encoding="utf-8")
    assert one.read_text(encoding="utf-8") == "x"


def test_live_file_mailbox_opens_on_a_tmp_mirror(runtime_tripwire, tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    mb = mailbox.open_mailbox("file://" + mailbox.DEFAULT_ROOT, base=REPO_ROOT)
    assert isinstance(mb, mailbox.FileMailbox)
    assert mb.root.is_relative_to(base)
    assert mb.root.parts[-2:] == Path(mailbox.DEFAULT_ROOT).parts   # mirrors the layout
    before = len(runtime_tripwire.hits)

    env = m.Envelope(to="worker-default", type=m.TASK_ASSIGN, task_id="t")
    sent = mb.send(env)

    assert sent.is_relative_to(base)
    assert not (INBOX / f"{env.msg_id}.json").exists()
    assert [e.msg_id for e in mb.receive("worker-default")] == [env.msg_id]
    assert len(runtime_tripwire.hits) == before


def test_default_git_mailbox_from_the_checkout_root_is_redirected(monkeypatch, tmp_path_factory):
    monkeypatch.chdir(REPO_ROOT)
    mb = mailbox.open_mailbox()
    assert isinstance(mb, mailbox.GitMailbox)
    assert mb.root.is_relative_to(tmp_path_factory.getbasetemp())
    # Its git commands must not run in this checkout either.
    assert not Path(mb.repo_dir).resolve().is_relative_to(REPO_ROOT)
    # The mirror starts empty: the live inbox's real envelopes are not visible.
    assert mb.receive("worker-default") == []


def test_direct_construction_under_the_live_tree_is_redirected(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    assert mailbox.FileMailbox(LIVE).root.is_relative_to(base)
    nested = mailbox.GitMailbox(LIVE / "sub", branch="other")
    assert nested.root.is_relative_to(base)
    assert nested.root.name == "sub"
    assert nested.branch == "other"


def test_mailbox_outside_the_live_tree_is_left_alone(tmp_path):
    root = tmp_path / "mb"
    assert mailbox.FileMailbox(root).root == root
    sibling = LIVE.parent / (LIVE.name + "-other")      # boundary, not a prefix
    assert mailbox.FileMailbox(sibling).root == sibling


def test_file_guard_alone_is_unchanged(runtime_tripwire, tmp_path):
    f = tmp_path / "f"
    session_before = len(runtime_tripwire.hits)
    with runtime_tripwire.guarding(f) as hits:
        with pytest.raises(runtime_tripwire.error):
            f.write_text("x", encoding="utf-8")
        (tmp_path / "g").write_text("x", encoding="utf-8")    # a neighbour is free
    assert [h.event for h in hits] == ["open"]
    assert len(runtime_tripwire.hits) == session_before
