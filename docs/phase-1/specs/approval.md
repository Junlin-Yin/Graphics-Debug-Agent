# Phase 1 Approval Specification

## Boundary

Approval is a runtime policy decision. Prompt instructions do not grant
permission, and tools cannot bypass `ToolBroker`.

Phase 1 approval grants are session-local. Decisions are persisted for audit,
but grants do not apply to future sessions.

## Config File Separation

Operational runtime settings (context budgets, model selection) live in
`~/.debug-agent/config.toml`. Agent-declared policy (path policy and shell
policy) lives in `~/.debug-agent/agent.toml`. This separation keeps policy
declarations distinct from operational configuration so that policy review
does not require parsing unrelated settings.

## Approval Modes

Phase 1 supports:

- `normal`
- `semi-auto`
- `yolo`

Default modes:

- REPL default: `normal`.
- one-shot default: `normal`.

Users may explicitly select `semi-auto` or `yolo` for one-shot execution through
the CLI approval-mode option. This is a Phase 1 breaking change from Phase 0
one-shot default autonomy: approval-required non-interactive operations fail
closed unless the user explicitly selects a more autonomous mode.

Mode behavior:

- `normal`: read access inside trusted workspace is allowed automatically. Read
  access outside trusted workspace requires approval. Write and execute access
  require approval for any path.
- `semi-auto`: read access is allowed automatically unless denied by blacklist.
  Write and execute access inside trusted workspace is allowed automatically.
  Write and execute access outside trusted workspace requires approval.
- `yolo`: no interactive approval is requested.

`yolo` is not a bypass. It must still pass schema validation, path policy
including blacklist veto, shell policy, timeout, artifact handling, and audit.
Path policy remains mandatory in `yolo`.

`semi-auto` intentionally relies on runtime-enforced builtin safety boundaries:
builtin path-policy deny rules, builtin shell deny rules, structured argv,
`shell=False`, timeout, artifact handling, and audit. This gives Phase 1 a
practical automation mode for local debugging, but it is not a filesystem
sandbox. A shell command allowed by shell policy may still perform side effects
that are not visible from argv path classification.

Phase 1 accepts the empty-shell-allowlist default as an explicit product risk
for skill-guided local automation. If user shell `allow` is empty, shell
commands are allowed by default after builtin shell deny, user shell deny, path
policy, approval mode, timeout, artifact handling, and audit pass. This is not a
sandbox and must not be described as one. The safety posture relies on `normal`
as the default approval mode, explicit user opt-in for `semi-auto` or `yolo`,
runtime-enforced builtin deny rules, `shell=False`, structured argv, and
auditable tool execution.

Runtime-control tools are evaluated independently from path access:

| risk level | normal | semi-auto | yolo |
| --- | --- | --- | --- |
| `runtime_control` | interactive approval required | no interactive approval, audit required | no interactive approval, audit required |

Policy denial, schema validation failure, and config errors cannot be overridden
by any approval mode.

`activate_skill` uses `runtime_control` risk. `load_skill_ref_file` is a
runtime-control category tool with `read` risk over the frozen session skill
snapshot. It does not access source files and has its own approval rule:

- if the target skill is already active, the requested path resolves to a frozen
  reference snapshot for that skill, and the frozen reference hash validates,
  the tool is audit-only in every approval mode and does not request interactive
  approval.
- inactive skills, invalid paths, missing references, corrupt snapshots, and
  hash mismatches are denied before approval and cannot be overridden by any
  approval mode.

## Tool Risk Metadata

Every tool definition must include runtime-owned metadata sufficient for policy:

```json
{
  "name": "write_file",
  "risk_level": "write",
  "access": ["write"]
}
```

Minimum risk levels:

- `read`
- `write`
- `execute`
- `network`
- `runtime_control`

`activate_skill` uses `runtime_control`. `load_skill_ref_file` uses `read` risk
and runtime-control category.

Phase 1 does not define per-tool approval metadata such as
`requires_approval`. Approval behavior is derived from approval mode plus
runtime-owned risk level, category, access, path policy, and shell policy.

In `normal` mode, `runtime_control` tools such as `activate_skill` require
approval. A user may choose approval for the current session so repeated
same-scope activations can proceed without asking again.

## Permission Rules And Evaluation

Phase 1 normalizes builtin policy, main-agent path policy, main-agent shell
policy, and session-local approval grants into runtime-owned `PermissionRule`
values before tool execution.

Main-agent policy is still declared in `~/.debug-agent/agent.toml`.
`~/.debug-agent/config.toml` remains operational runtime configuration. Policy
declarations are not enforced directly from TOML structures; they are parsed,
validated, frozen into the session config snapshot as policy facts, and then
evaluated as `PermissionRule` values.

Minimum rule shape:

```python
class PermissionRule:
    rule_id: str
    source: str
    target: dict
    matcher: dict
    effect: str
    priority: int
    reason: str
```

Allowed `source` values:

- `builtin_path`
- `user_path`
- `builtin_shell`
- `user_shell`
- `approval_grant`
- `runtime_control`

Allowed `effect` values:

- `deny`
- `allow`
- `trust`
- `ask`

`trust` is intentionally separate from `allow`. A trusted path affects whether
approval mode can automatically allow an operation; it does not by itself grant
read, write, execute, or runtime-control permission. Shell allow rules authorize
command identity only; they do not override path denies, builtin denies, schema
validation, timeout, artifact handling, or audit.

`PermissionEvaluator` evaluates normalized tool-call facts in this order:

1. check `deny` rules.
2. apply approval mode to risk, access, path trust, and shell facts.
3. check `allow` and `trust` rules, including shell allowlist and path trust.
4. check reusable session approval grants.
5. ask the user through `ApprovalProvider` if the resulting decision is still
   `ask`.

Some allow rules are gates, not convenience grants. In particular, when user
shell `allow` is non-empty, a shell command must match a shell allow rule or it
is denied with `policy_denied`; runtime must not ask the user to override the
missing allow match. Path `trust` rules are not gates and do not form an
exclusive path allowlist.

Policy denial, schema validation failure, config errors, invalid frozen skill
targets, and builtin deny matches are final. They cannot be overridden by
approval mode, `allow` rules, or session grants.

## Path Policy

Path policy applies only to model-visible tool invocations mediated by
`ToolBroker`.

Runtime-owned persistence and artifact store operations are not tool
invocations. Runtime stores may write under `.sessions/` through runtime service
APIs without path-policy evaluation, and remain governed by persistence,
artifact, checkpoint, and audit contracts.

The main agent declares path policy in:

```text
~/.debug-agent/agent.toml
```

Example:

```toml
[[path_policies]]
scope = "trust"
paths = ["../shared-debug-tools/"]

[[path_policies]]
scope = "deny"
paths = ["secrets/", ".env"]
```

The agent config only declares policy. Runtime and `ToolBroker` enforce it.
Runtime parses each path policy entry into `PermissionRule` values with
`effect="trust"` or `effect="deny"`.

If `~/.debug-agent/agent.toml` is absent or does not declare path policies,
Phase 1 creates a builtin workspace-root `PermissionRule` with `effect="trust"`.

Allowed path policy scopes are:

- `trust`
- `deny`

Trusted workspace is:

- session `workspace_root`.
- plus path policy `trust` paths.

Trusted workspace controls whether an operation can be automatically allowed
under the active approval mode. `trust` paths add trusted roots outside or under
the workspace root; they do not narrow or replace the default trust for
`workspace_root`. Non-blacklisted paths outside trusted workspace are not denied
by path policy solely for being outside trusted workspace; they follow the
approval-mode matrix above.

Path policy deny rules are a blacklist. Blacklist matches are a veto:

- they deny before approval is requested.
- they apply in every approval mode, including `yolo`.
- they apply to every tool category and access type.
- approval cannot override them.

Builtin path policy deny rules (cannot be overridden by user configuration):

- `.git/`
- `node_modules/`
- `build/`
- `dist/`
- `.venv/`
- `__pycache__/`
- `.pytest_cache/`
- `.sessions/`

TODO(Phase 4): shader/build artifact collection should expose controlled
runtime artifact summaries or previews instead of relaxing these Phase 1
model-visible filesystem denies.

These builtin deny rules are inherited from Phase 0 hardcoded `search_text`
skip directories and are now enforced uniformly through path policy. They apply
to all model-visible tools that traverse or access the filesystem, including
`search_text`, `read_file`, `list_dir`, `write_file`, `edit_file`, and
`shell_exec` classified path tokens.

This is an intentional Phase 1 breaking change. Phase 0 allowed explicit
`search_text` requests inside these directories even though default workspace
search skipped them. Phase 1 does not keep that exception because path policy is
now the runtime safety boundary for both native tools and shell execution. For
the Phase 1 target workflows, generated, dependency, cache, git, and runtime
state directories are intentionally hard-denied for the model-visible tool
surface. These builtin deny rules are not approval-overridable and are not soft
defaults. Any future relaxation, splitting, or override mechanism for this list
is a later breaking contract change.

Path policy must:

- resolve relative paths against `workspace_root`.
- allow absolute path policy entries.
- canonicalize requested paths before policy matching.
- classify paths as blacklisted, trusted, or untrusted without considering tool
  type.
- treat a trailing `/` policy entry as a subtree match.
- treat a non-trailing-`/` file policy entry as an exact canonical path match.
- match builtin directory deny rules against any same-name directory component
  under any accessed root.
- deny traversal into blacklisted paths.
- deny symlink escape into blacklisted paths.
- apply blacklist veto before approval mode decisions.
- write denial audit events.

Model-visible tools must not read, list, search, write, edit, or shell into
`.sessions/`. Runtime may expose artifact ids, summaries, trace commands, and
audited metadata, but those references do not grant operational filesystem
access to `.sessions/`. A model-visible tool cannot bypass `.sessions/` denial
by presenting an artifact id or runtime reference unless runtime explicitly
resolves that reference into a controlled preview or summary that does not expose
`.sessions/` path access.

For paths that do not yet exist, such as a `write_file` target whose parent
directory will be created, canonicalization uses this algorithm:

1. resolve the requested path lexically against `workspace_root`.
2. find the deepest existing parent path.
3. canonicalize that existing parent with symlinks resolved.
4. append the remaining non-existing path components lexically.
5. evaluate the resulting canonical candidate against builtin deny, user deny,
   trusted roots, and untrusted classification.

If any existing intermediate component is a symlink that resolves into a denied
root, the request is denied. If the final target already exists, the final
target is canonicalized with symlinks resolved before policy matching.

## Shell Policy

Shell policy is declared independently from path policy in:

```text
~/.debug-agent/agent.toml
```

Example:

```toml
[shell_policy]
allow = [
  ["uv"],
  ["python", "-m", "pytest"],
  ["npm", "test"]
]
deny = [
  ["git"]
]
```

Shell policy and path policy are independent. Shell policy decides whether the
command type is allowed, such as `git`, `npm`, or `uv`. Path policy decides
whether requested path-like fields or argv tokens are blacklisted, trusted, or
untrusted. It does not classify read, write, or execute tool types. Passing shell
policy does not grant path access, and passing path policy does not grant
command access.

If `~/.debug-agent/agent.toml` is absent or does not declare shell policy,
Phase 1 uses empty user `allow` and empty user `deny` rules, plus the builtin
shell deny rules below. This means user shell policy itself does not deny
additional command names by default, but shell execution still requires builtin
shell denies, path policy, approval/risk policy, timeout, and audit checks.

Execution requires:

```text
shell_policy allow/deny passed
AND path_policy allow/deny classification applied
AND approval/risk policy passed
```

`shell_exec` is treated as execute access for approval-mode and shell-policy
purposes. Separately, generic `shell_exec` evaluates `cwd` through path policy:
blacklist veto first, then trusted/untrusted classification. It also evaluates
argv tokens that can be classified as path-like using the Phase 1 argv path
classification rules below. Commands that need strict path-level read or write
enforcement should be modeled as native tools or specialized wrappers with
explicit path fields.

For execute access, shell policy is an additional required check on top of path
policy and approval mode.

Phase 1 uses argv-prefix matching, not regular expressions.

Rules:

- user deny rules take precedence over user allow rules.
- builtin deny rules take precedence over user allow rules and cannot be
  overridden by approval.
- empty `allow` means commands are allowed by default, subject to builtin deny
  rules, user deny rules, path policy, approval, timeout, and audit.
- non-empty `allow` means only matching argv prefixes are allowed.
- matching is performed on parsed argv, not on a raw shell string.
- unrestricted `shell=True` execution is not a Phase 1 tool contract.

Runtime parses shell policy into `PermissionRule` values:

- builtin shell denies use `effect="deny"` and `source="builtin_shell"`.
- user shell denies use `effect="deny"` and `source="user_shell"`.
- user shell allow prefixes use `effect="allow"` and `source="user_shell"`.

An empty shell allow list does not create a wildcard allow rule. It means there
is no user allowlist restriction after builtin deny, user deny, path policy,
approval mode, timeout, artifact handling, and audit are applied.

Command matching is based on runtime-normalized executable identities:

- path-qualified executable tokens such as `/usr/bin/git`, `./tools/git`, and
  Windows executable suffixes such as `git.exe`, `git.cmd`, and `git.bat` are
  normalized before shell-policy matching.
- when `argv[0]` is path-qualified, runtime must also evaluate that executable
  path through path policy before shell execution. A path-qualified executable
  under a blacklisted path is denied before approval, even if its normalized
  command identity would otherwise pass shell policy.
- runtime-defined transparent wrapper forms are unwrapped when their nested
  command is structurally visible. Phase 1 must support at least `env`-style
  wrappers such as `env FOO=1 git status`.
- opaque wrappers such as `npm run test`, `make test`, `uv run my-task`,
  `python scripts/run_git.py`, `node scripts/build.js`, and arbitrary local
  scripts are not semantically inspected for nested commands. To restrict those
  paths, users must deny the wrapper itself or use a narrow non-empty allowlist.

Builtin deny rules:

- privilege escalation commands: `sudo`, `su`, `doas`.
- destructive recursive delete:
  - normalized executable identity is `rm`.
  - any option token or short-option cluster includes `r` or `R`, including
    `-r`, `-R`, `-rf`, `-fr`, and `-Rf`.
  - any option token is `--recursive`.
- raw shell trampoline forms that reintroduce command-string execution:
  `sh -c`, `bash -c`, `zsh -c`, `cmd /c`, `powershell -Command`, and
  `pwsh -Command`.

Builtin deny matching uses normalized argv tokens. It is intentionally narrow
and auditable; it is a safety backstop, not a full sandbox.

Regular expressions are not supported in Phase 1. They are expressive but
harder to audit, easier to misconfigure, and less portable across macOS and
Windows command parsing.

## Path Authorization Matrix

This matrix defines the final authorization outcome for each tool category
under each approval mode, given path policy classification.

| tool category | risk level | path classification | normal | semi-auto | yolo |
|---|---|---|---|---|---|
| read-only native | `read` | trusted | auto-allow | auto-allow | auto-allow |
| read-only native | `read` | untrusted (not denied) | ask approval | auto-allow | auto-allow |
| write native | `write` | trusted | ask approval | auto-allow | auto-allow |
| write native | `write` | untrusted (not denied) | ask approval | ask approval | auto-allow |
| shell | `execute` | trusted + shell policy ok | ask approval | auto-allow | auto-allow |
| shell | `execute` | untrusted + shell policy ok | ask approval | ask approval | auto-allow |
| runtime control | `runtime_control` | n/a | ask approval | audit only | audit only |

`deny` (blacklist) overrides all cells: denied before approval is requested
in every mode, including `yolo`.

Known issue: `yolo` intentionally auto-allows untrusted write-native operations
and untrusted shell execution after schema validation, builtin/user path-policy
blacklist vetoes, shell policy, timeout, artifact handling, and audit pass. This
is risky by design. `yolo` is for explicitly user-selected high-autonomy local
runs, not the default safety posture.

## Git Access

The Phase 0 model-visible `git_status` native tool is removed from the model
tool list in Phase 1.

All model-initiated git access goes through `shell_exec` and shell policy.
For example, denying `["git"]` denies direct and runtime-normalized git
invocations, including supported transparent wrapper forms. It does not claim to
detect git invocations hidden inside opaque scripts, package-manager tasks, build
tools, or arbitrary binaries.

This does not affect CLI-owned commands such as `debug-agent status` or
`debug-agent trace`.

## Cross-Platform Shell Execution

Shell execution must support macOS and Windows.

Rules:

- tool input uses argv lists, not raw shell strings.
- default execution uses `shell=False`.
- command name normalization handles common Windows executable suffixes such as
  `.exe`, `.cmd`, and `.bat` while policy matching uses normalized names.
- default working directory is `workspace_root`; a requested `cwd` is resolved
  against `workspace_root` and checked by path policy.
- stdout and stderr use artifact/preview rules.
- `cwd` must be checked by path policy.
- argv tokens that can be classified as path-like must be checked by path
  policy.
- platform-specific behavior must be tested with fake runners when real OS
  coverage is unavailable.

## Shell Argv Path Classification

Generic `shell_exec` evaluates path policy for:

- `cwd`.
- path-qualified `argv[0]` executable paths.
- argv tokens that are syntactically path-like, such as `src/file.py`,
  `./src/file.py`, `../repo/file.py`, `/tmp/file`, or Windows drive/UNC paths.
- option values embedded in a single token only when the option name is in this
  Phase 1 runtime-owned path option list and the value is path-like, such as
  `--output=dist/result.txt`.
- option/value pairs only when the option name is in this same path option list:
  `--output`, `--out`, `--config`, `--file`, `--path`, `--cwd`, `--directory`,
  `--root`, `--input`, `--src`, `--source`, `--dest`, `--destination`,
  `-o`, `-c`, `-f`, `-C`, and `-I`.

Generic `shell_exec` does not treat URLs, pure flags, environment variable
expressions, non-path command names, or arbitrary free-form strings as paths.

If a classified argv path is blacklisted, `ToolBroker` denies before approval.
If a classified argv path is untrusted, approval-mode rules decide whether
interactive approval is required.

Known issue: argv classification cannot prove the command will not access paths
that are absent from argv or hidden behind tool-specific semantics. Phase 1
acknowledges this limitation. A future phase may need a real filesystem sandbox
or specialized command wrappers to enforce complete path isolation for shell
commands.

## Approval Grant Key

Session-local approval grants use:

```text
session_id
tool_name
risk_level
scope_signature
```

`scope_signature` is the deterministic approval scope for a reusable
session-local grant. It must be narrow enough that approving one concrete
operation does not authorize unrelated paths, commands, or runtime-control
targets.

Minimum Phase 1 signatures:

- `read_file`, `list_dir`, `search_text`, `write_file`, and `edit_file` use the
  canonical path plus requested access type. `edit_file` and `write_file`
  signatures must not widen from a file path to a directory path.
- `shell_exec` uses normalized argv, canonical cwd, effective timeout seconds,
  and the canonical set of runtime-classified argv path tokens. A grant for one
  argv prefix, cwd, or effective timeout must not apply to a different command,
  working directory, or effective timeout.
- `activate_skill` uses the skill `name` and `content_hash` so that a grant for
  one skill does not apply to a different skill or to the same skill after a
  source-file change.
- `load_skill_ref_file` uses the skill `name`, skill content hash, reference
  path, and reference content hash.

Approval grants are not path-policy or shell-policy declarations. Reusable
`approved_for_session` grants are represented to `PermissionEvaluator` as
session-local `PermissionRule` values with `source="approval_grant"` and
`effect="allow"`. They only skip future interactive approval for the same
session, tool, risk level, and exact scope signature after schema validation,
shell policy, path policy, builtin deny, timeout, artifact, and audit checks
still pass.

## Persistence

Phase 1 adds persisted approval audit records. A minimum table shape:

```sql
CREATE TABLE approval_grants (
  grant_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  scope_signature TEXT NOT NULL,
  decision TEXT NOT NULL,
  grant_scope TEXT NOT NULL,
  approval_request TEXT NOT NULL,
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL
);
```

Allowed `decision` values:

- `approved_once`
- `approved_for_session`
- `denied`

Allowed `grant_scope` values:

- `once`
- `session`
- `none`

The table is audit history. Only `approved_for_session` rows are reusable grant
cache entries, and only within the same session.

## TUI Approval Prompt

Phase 1 uses the existing prompt_toolkit `Application` architecture.

Approval is an inline controller state, not a popup and not a second command
lane.

When approval is required:

1. `ToolBroker` calls `ApprovalProvider.request(...)`.
2. `ReplController` appends a system block describing:
   - tool name.
   - risk level.
   - path or command preview.
   - grant scope.
3. The prompt input region switches to approval mode.
4. The user enters:
   - `y`: approve once.
   - `a`: approve for this session.
   - `n`: deny.
5. The approval decision is persisted.

If the user enters `y` or `a`, tool execution continues.

If the user enters `n`, `ToolBroker` returns a denied tool outcome carrying
`turn_aborted=true`. Runtime records the denial and `PromptAgentExecutor`
short-circuits the current turn without making a same-turn follow-up model call.
The denied tool result is recorded as a terminal observation in durable
LLM-visible conversation so future model calls after the next user input can see
that the user denied the requested tool operation. The REPL input region is
restored, and the user can enter the next prompt. Denial must not terminalize
the session.

## Plain And Non-Interactive Behavior

Plain REPL may ask the same approval question by writing to the output stream
and reading one input line when interactive input is available.

Non-interactive approval requests are denied with `policy_denied`.

The expected non-interactive case is one-shot execution configured to use
`normal` or `semi-auto` while a requested tool requires approval. This denial
prevents the process from hanging while waiting for unavailable user input.
For a long-lived REPL prompt run, it uses the same turn short-circuit behavior
as an interactive `n` decision. For a one-shot prompt run, it records the denial
facts, marks the one-shot run and session as terminal `failed` with
`error_class="policy_denied"`, and exits non-zero.

## Audit Events

Tool approval should be visible in trace and logs.

Minimum event kinds:

- `approval_requested`
- `approval_decision_recorded`

Tool denial due to approval uses existing tool denial semantics and includes
`error_class=policy_denied`.
