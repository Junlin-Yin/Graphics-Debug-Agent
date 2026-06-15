from __future__ import annotations


# CLI usage string is a presentation constant, not runtime truth.
USAGE = (
    "Usage:\n"
    "  debug-agent [--approval-mode normal|semi-auto|yolo]  # REPL\n"
    '  debug-agent [--approval-mode normal|semi-auto|yolo] -p "prompt"\n'
    "  debug-agent status <session_id>\n"
    "  debug-agent trace <session_id>\n"
    "  debug-agent resume <session_id>"
)

# CLI parser accepts only the runtime approval modes exposed to users.
APPROVAL_MODES = {"normal", "semi-auto", "yolo"}

# REPL guard message shown when input arrives during active execution.
BUSY_MESSAGE = "Prompt run is already executing. Input is disabled."

# REPL presentation notice when streaming falls back to authoritative run().
STREAMING_FALLBACK_MESSAGE = (
    "streaming unavailable for this model; using non-streaming response."
)

# Markdown rendering cap keeps TUI presentation responsive for large messages.
MAX_MARKDOWN_RENDER_CHARS = 50_000

# Streaming flush cadence balances UI freshness with terminal redraw cost.
STREAM_FLUSH_INTERVAL_SECONDS = 0.25

# Line scroll step for keyboard and mouse wheel movement in the TUI message pane.
MESSAGE_SCROLL_STEP_LINES = 2

# Page scroll step for larger movement in the TUI message pane.
MESSAGE_SCROLL_STEP_PAGE = 10

# Escape-key timeout keeps prompt-toolkit interactions responsive.
ESCAPE_SEQUENCE_TIMEOUT_SECONDS = 0.05
