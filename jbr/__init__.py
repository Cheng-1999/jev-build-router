"""jbr: Jev build router.

Routes work packages to build engines (Claude Code subagent, agy, codex) with a
single TypeSafe/Jev batch question, drives the external engines headlessly and
turns review-workflow output into fix instructions. Pure Python 3.12 stdlib.
"""

__version__ = "0.1.0"

DEFAULT_ENGINE_KEYS = ("claude_subagent", "agy_claude_opus", "agy_gemini_pro", "codex_astra")
