"""Output utilities — truncate large output and save full content to disk."""
from __future__ import annotations

import os
import time


def truncate_output(
    content: str,
    max_chars: int = 5000,
    workspace: str | None = None,
    label: str = "output",
) -> str:
    """Truncate large output, saving the full text to a file when possible.

    If *content* fits within *max_chars* it is returned unchanged.  Otherwise
    the full content is written to ``{workspace}/.agent/outputs/{label}_{ts}.txt``
    and a truncated version (first 500 chars + hint + last 500 chars) is
    returned so the caller can see the beginning and end while knowing where
    to find the complete output.

    When *workspace* is ``None`` the file cannot be saved; in that case only
    the in-memory truncation (with a hint about truncation) is performed.
    """
    if len(content) <= max_chars:
        return content

    head = content[:500]
    tail = content[-500:]

    if workspace is not None:
        output_dir = os.path.join(workspace, ".agent", "outputs")
        os.makedirs(output_dir, exist_ok=True)

        timestamp = f"{time.time():.6f}"
        filename = f"{label}_{timestamp}.txt"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        hint = f"... (truncated, full output saved to {filepath})"
    else:
        hint = "... (truncated, full output not saved — no workspace provided)"

    return f"{head}\n\n{hint}\n\n{tail}"
