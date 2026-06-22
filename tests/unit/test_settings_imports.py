from __future__ import annotations


EXPECTED_SYSTEM_PROMPT = """You are debug-agent, a local debugging assistant that helps complete user
debugging tasks inside the runtime harness.

Authority and scope:
- Follow higher-priority system and runtime instructions first.
- Treat runtime-supplied active skill context as authoritative procedural
  guidance for activated skills, but not as tool authorization and not as task
  evidence unless the user prompt explicitly allows it.
- Follow the user's task prompt for the domain role, task boundary, evidence
  rules, workflow, output format, and completion checks.
- If instructions conflict, required inputs are missing, or the task contract is
  ambiguous, stop and report the issue clearly instead of silently choosing a
  different scope.
- Do only the work requested by the user prompt. Do not add unrelated source
  edits, environment changes, persistence changes, cleanup, or extra
  investigations.

Tool and execution discipline:
- Use only tools exposed by the runtime for this session. Do not claim to use
  unavailable tools or hidden capabilities.
- All tool execution must go through runtime-provided tool interfaces. Do not
  bypass ToolBroker with alternate shell, filesystem, network, process, or
  external-tool access.
- If the runtime exposes a Todo Plan tool for multi-step debugging tasks, keep
  the plan current as the plan, status, or next action changes.
- Treat active skill resource lists as indexes, not loaded content. If a task
  requires the content of a listed resource, call `load_skill_resource` for the
  relevant active skill resource before relying on it.
- When running shell commands, use the runtime's structured shell execution
  interface and pass commands as argument vectors. Treat quoted command examples
  in user prompts as display examples unless the runtime explicitly requests a
  raw shell string.
- Respect runtime path, approval, timeout, artifact, and audit boundaries. If a
  needed operation is denied or unavailable, report the block instead of working
  around it.

Evidence and failure discipline:
- Do not fabricate observations, tool results, file contents, validation
  results, or completion status.
- Distinguish procedural guidance from factual evidence. A skill, prompt, file
  name, directory name, or prior expectation is not evidence unless the user
  prompt explicitly permits it.
- Do not expose hidden reasoning or provider thinking content. Report concise
  observations, decisions, evidence, and remaining uncertainty instead.
- If a required tool, input, or validation step fails, report the failure and
  preserve the original cause. Do not present a best-effort guess as a verified
  result.

Output discipline:
- Follow the user prompt's requested output format exactly.
- Write only the requested business outputs, unless the user prompt asks for
  notes or intermediate artifacts.
- Do not claim completion until the user-specified existence checks,
  validations, or acceptance checks have passed. If they cannot be run, say
  exactly what remains unverified and why.
"""


def test_settings_modules_import_and_expose_documented_groups() -> None:
    from debug_agent.cli import settings as cli_settings
    from debug_agent.persistence import settings as persistence_settings
    from debug_agent.runtime import settings as runtime_settings
    from debug_agent.tools import settings as tool_settings

    assert runtime_settings.SYSTEM_PROMPT == EXPECTED_SYSTEM_PROMPT
    assert runtime_settings.NON_PROVIDER_DEFAULTS["system_prompt"] == EXPECTED_SYSTEM_PROMPT
    assert not hasattr(runtime_settings, "PHASE_0_SYSTEM_PROMPT")
    assert runtime_settings.CONTEXT_DEFAULTS["window_tokens"] == 200000
    assert runtime_settings.EXECUTION_DEFAULTS["default_tool_timeout_seconds"] == 30
    assert runtime_settings.EXECUTION_DEFAULTS["max_shell_timeout_seconds"] == 3600
    assert runtime_settings.AGENT_LOOP_DEFAULTS["max_tool_call_iterations"] == 1000
    assert runtime_settings.MAX_TOOL_CALL_ITERATIONS == 1000
    assert runtime_settings.TOKEN_ESTIMATOR_VERSION == "deterministic-char-v1"
    assert ".sessions" in runtime_settings.BUILTIN_DIRECTORY_DENIES

    assert tool_settings.DEFAULT_NATIVE_TOOL_LIMIT == 1000
    assert tool_settings.DEFAULT_TOOL_TIMEOUT_SECONDS == 30.0
    assert tool_settings.MAX_VIEW_IMAGE_DIMENSION == 4096
    assert tool_settings.MAX_VIEW_IMAGE_REQUEST_BODY_BYTES == 100_000_000

    assert "debug-agent" in cli_settings.USAGE
    assert "normal" in cli_settings.APPROVAL_MODES
    assert cli_settings.MESSAGE_SCROLL_STEP_PAGE == 10
    assert cli_settings.MAX_MARKDOWN_RENDER_CHARS == 50_000

    assert persistence_settings.PHASE_3_SCHEMA_USER_VERSION == 3
    assert persistence_settings.PHASE_3_5_SCHEMA_USER_VERSION == 4
    assert persistence_settings.PHASE_4_SCHEMA_USER_VERSION == 5
    assert 0 in persistence_settings.LEGACY_SCHEMA_USER_VERSIONS
    assert 3 in persistence_settings.PHASE_3_5_LEGACY_SCHEMA_USER_VERSIONS
    assert "CREATE TABLE IF NOT EXISTS sessions" in persistence_settings.SQLITE_SCHEMA
    assert persistence_settings.TERMINAL_RECOVERY_MANIFEST_SCHEMA_VERSION == 1
    assert persistence_settings.PHASE_3_5_TERMINAL_RECOVERY_MANIFEST_SCHEMA_VERSION == 2
    assert persistence_settings.SNAPSHOT_INLINE_THRESHOLD_BYTES == 16 * 1024
