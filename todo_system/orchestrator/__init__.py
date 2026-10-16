"""Agent Orchestrator v2 — message-based runner/agent orchestration.

See Notes/agent-orchestrator-v2-design.md and Decisions/2026-07-agent-orchestrator-v2.md.

P0 (this package so far): the Mailbox abstraction — an envelope format plus
git-backed / file-backed transports that the orchestrator and every runner
(local, remote, human) speak uniformly. Stdlib-only so it runs under a bare
`python3` on remote hosts, same constraint as todo_system.session_log.
"""
