# ADR 0012: Runtime-Enforced Shell, Path, And Approval Policy

## Status

Accepted for Phase 1.

## Context

Phase 1 introduces higher-risk tool surfaces: writable native tools,
shell-command execution, runtime-control tools such as `activate_skill`, and
session-local approval grants.

ADR 0004 already requires all tools to pass through `ToolBroker`. Phase 1 must
define how shell policy, path policy, and approval interact so later subagents,
workflow steps, and MCP tools inherit one policy model.

Users also need workflow-specific command restrictions, such as denying all git
commands while allowing other shell commands.

## Decision

Shell policy and path policy are independent and both must pass before shell
execution:

```text
shell_policy allow/deny passed
AND path_policy allow/deny classification applied
AND approval/risk policy passed
```

Path policy and shell policy are declared by the main agent config in
`~/.debug-agent/agent.toml`, but runtime and `ToolBroker` enforce them. Prompt
text is not an enforcement boundary.

Phase 1 uses a fixed broker-owned permission decision pipeline. Runtime parses
builtin policy, main-agent path policy, main-agent shell policy,
runtime-control constraints, and reusable session approval grants into frozen
policy facts. `PermissionEvaluator` consumes those facts; raw TOML policy
structures are declarations, not the runtime decision format.

Path `trust` is not a final allow: trusted paths influence approval-mode
automation but do not grant read, write, execute, or runtime-control permission
by themselves.

Path policy applies only to model-visible tool invocations mediated by
`ToolBroker`. Runtime-owned persistence and artifact store operations are not
tool invocations; writes under `.sessions/` through runtime service APIs are
governed by persistence, artifact, checkpoint, and audit contracts rather than
path policy.

Trusted workspace is the session workspace root plus path policy `trust`
paths. `trust` paths add trusted roots and do not narrow or replace the default
trust for the workspace root. Trusted workspace controls whether an operation
can be automatically allowed under the active approval mode; non-blacklisted
paths outside trusted workspace are not denied by path policy solely for being
outside trusted workspace. Path policy scopes are only `trust` and `deny`; path
policy does not classify read, write, or execute tool types. Path policy deny
rules are blacklist vetoes: they deny before approval is requested, apply in
every approval mode including `yolo`, apply to every tool category and access
type, and cannot be overridden by approval.

Phase 1 shell policy uses argv-prefix matching, not regular expressions.

Rules:

- builtin deny rules take precedence over user allow rules and cannot be
  overridden by approval.
- deny takes precedence over allow.
- empty user allow means default allow, subject to builtin deny, user deny, path
  policy, approval, timeout, and audit.
- non-empty allow means only matching argv prefixes are allowed.
- matching occurs on parsed argv, not raw shell strings.
- unrestricted `shell=True` execution is not a Phase 1 model-visible tool
  contract.

Command matching uses runtime-normalized executable identities. Path-qualified
executables and common Windows executable suffixes normalize before matching.
Runtime-defined transparent wrapper forms are unwrapped when the nested command
is structurally visible; Phase 1 must support at least `env`-style wrappers such
as `env FOO=1 git status`. Opaque wrappers such as package-manager tasks, build
tools, interpreter script execution, and arbitrary local scripts are not
semantically inspected for nested commands. Users who need to restrict those
paths must deny the wrapper itself or use a narrow non-empty allowlist.

Builtin shell deny rules block privilege escalation commands, destructive
recursive delete prefixes, and raw shell trampoline forms such as `sh -c`,
`bash -c`, `zsh -c`, `cmd /c`, and PowerShell command-string execution. Builtin
deny rules are a safety backstop, not a full sandbox.

Generic `shell_exec` evaluates path policy for `cwd`, path-qualified `argv[0]`,
and runtime-classified path-like argv tokens. If a classified argv path is
blacklisted, the command is denied before approval. If it is untrusted,
approval-mode rules decide whether interactive approval is required.
For shell calls with multiple participating paths, classification is aggregated
conservatively: the call is trusted only when `cwd`, any path-qualified
executable path, and every runtime-classified argv path are trusted. If any
participating path is untrusted, the whole shell call is treated as untrusted for
approval-mode decisions.

Model-visible tools must not read, list, search, write, edit, or shell into
`.sessions/`, and cannot use artifact ids or runtime references to bypass the
builtin `.sessions/` deny rule. Runtime may expose controlled artifact ids,
summaries, trace commands, and audited metadata without granting operational
filesystem access to `.sessions/`.

Known issue: argv path classification cannot prove that shell commands avoid
paths that are absent from argv or hidden behind command-specific semantics.
Phase 1 accepts this limitation. A future phase may need filesystem sandboxing
or specialized command wrappers for complete shell path isolation.

The model-visible Phase 0 `git_status` native tool is removed in Phase 1.
Model-initiated git access goes through `shell_exec` and shell policy. Denying
`["git"]` blocks direct and runtime-normalized git invocations, including
supported transparent wrapper forms. It does not claim to detect git invocations
hidden inside opaque scripts, package-manager tasks, build tools, or arbitrary
binaries. CLI commands such as `debug-agent status` and `debug-agent trace` are
unrelated to shell policy.

Approval grants are session-local. `approval_grants` records only interactive
user approval prompt decisions. Reusable grants do not apply to future sessions.
Policy auto-allow outcomes, including `semi-auto` and `yolo` runtime-control
decisions, are recorded through ToolBroker, runtime-control, skill, trace, and
engine-log audit facts, not as approval grant rows.

Reusable grant keys include an exact operation scope signature. Phase 1 uses
narrow signatures: file tools include canonical path and access type,
`shell_exec` includes normalized argv plus canonical cwd and classified argv
paths, `activate_skill` includes skill name plus content hash, and
`load_skill_ref_file` records skill name, skill content hash, reference path, and
reference content hash for audit/scope consistency.

Approval mode behavior is path-aware:

- `normal`: read access inside trusted workspace is automatic; read access
  outside trusted workspace requires approval; write and execute access always
  require approval.
- `semi-auto`: read access is automatic unless blacklisted; write and execute
  access inside trusted workspace is automatic; write and execute access outside
  trusted workspace requires approval.
- `yolo`: skips interactive approval.

`yolo` does not bypass schema validation, path blacklist veto, path policy,
shell policy, timeout, artifact handling, or audit. Execute access also requires
shell policy.

Runtime-control tools such as `activate_skill` require interactive approval in
`normal`. In `semi-auto` and `yolo`, they skip interactive approval but still
write audit records. These `semi-auto` and `yolo` outcomes do not emit
`approval_requested` or `approval_decision_recorded` events. Policy denial,
schema validation failure, and config errors cannot be overridden by any
approval mode.

The permission decision order is:

1. normalize tool-call facts, including risk, access, canonical paths, shell
   identity, classified argv paths, runtime-control targets, and scope
   signature.
2. check hard denies, including builtin/user path denies, builtin/user shell
   denies, and invalid runtime-control targets.
3. for `shell_exec`, apply the shell allowlist gate; a non-empty allowlist miss
   is a policy denial, not an approval prompt.
4. classify normalized paths as trusted or untrusted after deny checks.
5. apply the approval-mode matrix to decide `allow` or `ask`.
6. if the decision is `ask`, check reusable session approval grants for the
   exact scope signature.
7. ask the user through `ApprovalProvider` if the resulting decision is still
   `ask`.

When a shell allowlist is non-empty, matching it is a required allow gate.
Failure to match is a policy denial, not a user-approval prompt. Path trust rules
are not gates and do not create an exclusive path allowlist.

Only after the call is allowed does `ToolBroker` route it to a native, shell, or
runtime-control handler. Handlers do not own permission decisions and do not
write audit events directly.

Phase 1 defaults are `normal` for REPL and `normal` for one-shot. Users may
explicitly select `semi-auto` or `yolo` for one-shot execution through the CLI
approval-mode option.

TTY REPL users may cycle the active approval mode with `Ctrl+Y` only while the
REPL is idle. Runtime records the switch as an `approval_mode_changed` run event
and in `engine.log`. `Ctrl+Y` during active execution or during an inline
approval prompt is a silent no-op, must not queue a later mode change, and must
not change the current tool decision.

## Alternatives Considered

### Regex shell policy

Regex is expressive, but it is harder to audit, easier to misconfigure, and
less portable across macOS and Windows command parsing.

### Keep native `git_status`

This preserves a convenient read-only tool, but it creates a bypass when a user
wants to deny all git access for a workflow.

### Let approval override policy denial

This gives users flexibility, but weakens policy as an enforceable runtime
boundary. Approval can permit risky allowed operations, but it cannot override a
policy denial.

## Consequences

- Shell execution policy is reviewable and deterministic.
- Path policy, shell policy, runtime-control decisions, approval mode, and
  approval grants share one deterministic broker decision path.
- Workflows can deny direct and runtime-normalized model-initiated git access.
- Cross-platform shell execution must use structured argv and `shell=False`.
- Shell path policy remains best-effort for generic shell commands until a real
  sandbox or specialized wrappers exist.
- Tool tests must cover shell policy, path policy, approval mode, and denial
  audit together.
