from __future__ import annotations


# Phase 3 native tools use this default page size when callers omit limit.
DEFAULT_NATIVE_TOOL_LIMIT = 1000

# Shared inline threshold for brokered tool observations before artifact fallback.
LARGE_OUTPUT_THRESHOLD_BYTES = 16 * 1024

# Lower-level ToolBroker fallback when no frozen session config is available.
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0

# Default visual analysis prompt used when view_image.query is omitted.
DEFAULT_VIEW_IMAGE_QUERY = (
    "Describe the visible contents of the image(s), call out visual differences or "
    "anomalies when multiple images are provided, transcribe visible text when "
    "useful, and note uncertainty."
)

# Fixed maximum image edge accepted by the brokered view_image runtime.
MAX_VIEW_IMAGE_DIMENSION = 4096

# Fixed maximum image pixel count accepted by the brokered view_image runtime.
MAX_VIEW_IMAGE_PIXELS = 4096 * 2160

# Fixed maximum projected provider request body for brokered view_image calls.
MAX_VIEW_IMAGE_REQUEST_BODY_BYTES = 100_000_000
