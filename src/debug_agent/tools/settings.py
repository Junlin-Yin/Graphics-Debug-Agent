from __future__ import annotations


# Default page size for legacy lower-level native-tool seams.
DEFAULT_NATIVE_TOOL_LIMIT = 1000

# Phase 3.5 read_file line pagination contract.
READ_FILE_DEFAULT_LIMIT = 2000
READ_FILE_MAX_LIMIT = 2000

# Phase 3.5 list_dir entry pagination and ignore-list contract.
LIST_DIR_DEFAULT_LIMIT = 200
LIST_DIR_MAX_LIMIT = 1000
LIST_DIR_MAX_IGNORE_PATTERNS = 100

# Phase 3.5 find_file result pagination contract.
FIND_FILE_DEFAULT_MAX_RESULTS = 100
FIND_FILE_MAX_RESULTS = 1000

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

# Fixed maximum number of local images accepted by one view_image call.
MAX_VIEW_IMAGE_COUNT = 4

# Fixed maximum image edge accepted by the brokered view_image runtime.
MAX_VIEW_IMAGE_DIMENSION = 4096

# Fixed maximum image pixel count accepted by the brokered view_image runtime.
MAX_VIEW_IMAGE_PIXELS = 4096 * 2160

# Fixed maximum projected provider request body for brokered view_image calls.
MAX_VIEW_IMAGE_REQUEST_BODY_BYTES = 100_000_000
