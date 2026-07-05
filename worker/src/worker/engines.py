"""Engine identifiers written into every job result's `engine` field.

These MUST mirror the Zod enum in packages/shared/src/index.ts (JobResult.engine):
`z.enum(["pp-ocrv6", "paddleocr-vl", "docling", "none"])`. Keep the two in sync —
the TS server validates the worker's result against that enum.
"""

from __future__ import annotations

ENGINE_PPOCR = "pp-ocrv6"
ENGINE_VL = "paddleocr-vl"
ENGINE_DOCLING = "docling"
ENGINE_NONE = "none"
