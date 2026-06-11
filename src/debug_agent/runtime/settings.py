from __future__ import annotations

from typing import Any


# Base prompt contract used when no phase-specific prompt profile overrides it.
PHASE_0_SYSTEM_PROMPT = (
    "You are debug-agent, a local debugging assistant. Answer concisely and use "
    "only tools exposed by the runtime."
)

# Built-in model-call defaults frozen into each session config snapshot.
NON_PROVIDER_DEFAULTS: dict[str, Any] = {
    "temperature": 0.2,
    "max_tokens": 8192,
    "timeout_seconds": 120,
    "system_prompt": PHASE_0_SYSTEM_PROMPT,
}

# Context-window policy defaults for runtime-owned omission and compression.
CONTEXT_DEFAULTS: dict[str, Any] = {
    "window_tokens": 200000,
    "omit_old_tool_results_at_ratio": 0.60,
    "compress_history_at_ratio": 0.80,
    "retain_recent_model_calls": 4,
    "compression_reserved_output_tokens": 10000,
}

# Shell execution and cancellation defaults frozen before tool execution.
EXECUTION_DEFAULTS: dict[str, Any] = {
    "default_shell_timeout_seconds": 300,
    "max_shell_timeout_seconds": 3600,
    "cancellation_timeout_seconds": 10,
}

# Multimodal provider defaults are frozen config values, not image safety limits.
MULTIMODAL_LIMIT_DEFAULTS: dict[str, int] = {
    "timeout_seconds": 60,
    "max_tokens": 4096,
    "max_query_chars": 8192,
    "max_analysis_chars": 8192,
}

# Development gate for incomplete Phase 3 prompt execution paths.
DEFAULT_ALLOW_INCOMPLETE_PHASE3_PROMPT_EXECUTION = False

# Phase 3 adapter loop bound; Phase 3.5 config wiring changes this later.
MAX_TOOL_CALL_ITERATIONS = 8

# Provider-visible safety instruction prepended by the LangChain adapter.
RUNTIME_SAFETY_PREFIX = (
    "runtime safety: use only runtime-provided tools and do not bypass ToolBroker."
)

# Runtime retry precondition symbols are fixed contract keys, not call-site strings.
RETRY_PRECONDITIONS = frozenset(
    {
        "none",
        "metadata_transient_true",
        "text_only_no_tool_fragment",
        "sqlite_no_partial_commit",
    }
)

# Runtime retry strategy symbols define the narrow allowed retry actions.
RETRY_STRATEGIES = frozenset({"repeat_call", "continue_generation"})

# Runtime retry backoff symbols keep retry timing reviewable and finite.
RETRY_BACKOFFS = frozenset({"none", "fixed"})

# Built-in path denies are a policy safety baseline that user policy cannot override.
BUILTIN_DIRECTORY_DENIES = (
    ".git",
    "node_modules",
    "build",
    "dist",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".sessions",
)

# Shell argv options whose following token is classified as a path-like argument.
PATH_OPTION_NAMES = frozenset(
    {
        "--output",
        "--out",
        "--config",
        "--file",
        "--path",
        "--cwd",
        "--directory",
        "--root",
        "--input",
        "--src",
        "--source",
        "--dest",
        "--destination",
        "-o",
        "-c",
        "-f",
        "-C",
        "-I",
    }
)

# Windows executable suffixes normalized during shell policy matching.
WINDOWS_SUFFIXES = (".exe", ".cmd", ".bat")

# Raw shell trampolines are denied so structured argv remains the execution boundary.
RAW_SHELL_TRAMPOLINES = {
    ("sh", "-c"),
    ("bash", "-c"),
    ("zsh", "-c"),
    ("cmd", "/c"),
    ("powershell", "-command"),
    ("pwsh", "-command"),
}

# Token estimator identity is persisted with estimates for auditability.
TOKEN_ESTIMATOR_VERSION = "deterministic-char-v1"

# Per-message structural estimate used by the deterministic token estimator.
TOKEN_ESTIMATOR_MESSAGE_STRUCTURAL_TOKENS = 4

# Per-tool-schema structural estimate used by the deterministic token estimator.
TOKEN_ESTIMATOR_TOOL_SCHEMA_STRUCTURAL_TOKENS = 8

# Per-frame structural estimate used by the deterministic token estimator.
TOKEN_ESTIMATOR_FRAME_STRUCTURAL_TOKENS = 2

# Marker keeps omitted historical tool results model-visible without payload replay.
OMITTED_TOOL_RESULT_MARKER = (
    "[Earlier tool result omitted for brevity. See artifact references or trace "
    "for full details.]"
)

# Compression output schema required for runtime continuity summaries.
COMPRESSION_REQUIRED_FIELDS = {
    "task_goal": str,
    "completed_work": list,
    "inspected_or_modified_files": list,
    "remaining_work": list,
    "next_plan": list,
    "key_decisions": list,
    "constraints": list,
}

# Optional compression fields may be included only when already visible.
COMPRESSION_OPTIONAL_VISIBLE_FIELDS = (
    "visible_artifact_refs",
    "visible_active_skills",
    "visible_loaded_skill_resources",
    "visible_policy_or_approval_facts",
)

# Prompt instructs the compression model to produce replacement continuity JSON.
COMPRESSION_INSTRUCTION_PROMPT = """You are producing a Phase 1 debug-agent continuity summary.
Return only a JSON object. Merge the previous summary and evicted history into
a complete replacement summary, not a delta. Preserve task goal, completed
work, inspected or modified files, remaining work, next plan, key decisions,
constraints, and visible artifact, loaded skill resource, approval, or policy facts
only when already visible in the previous summary or evicted history.
Required schema:
{
  "task_goal": "string",
  "completed_work": ["string"],
  "inspected_or_modified_files": ["string"],
  "remaining_work": ["string"],
  "next_plan": ["string"],
  "key_decisions": ["string"],
  "constraints": ["string"],
  "visible_artifact_refs": ["string"],
  "visible_active_skills": ["string"],
  "visible_loaded_skill_resources": ["string"],
  "visible_policy_or_approval_facts": ["string"]
}
"""

# Model outputs over this size are externalized before durable conversation write.
LARGE_MODEL_CONTENT_THRESHOLD_BYTES = 16 * 1024

# User-facing turn abort message for unresolvable context-window pressure.
CONTEXT_LIMIT_EXCEEDED_MESSAGE = (
    "Context window still exceeds the limit after compression. "
    "The current turn was aborted."
)

# User-facing message when compression has no eligible durable history to evict.
NO_COMPRESSIBLE_HISTORY_MESSAGE = "No compressible history."
