"""Public facade for review-gated visual enrichment."""

from .stage import STAGE_ID, VisualEnrichmentStageOutput, plan, run

__all__ = ["STAGE_ID", "VisualEnrichmentStageOutput", "plan", "run"]
