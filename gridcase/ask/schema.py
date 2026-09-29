"""What the question box returns: sentences in the conclusion's own Claim shape."""

from __future__ import annotations

from pydantic import BaseModel, Field

from gridcase.explain.schema import Claim


class Answer(BaseModel):
    sentences: list[Claim] = Field(default_factory=list)
    # true when the question is not about this page; sentences are then empty
    refused: bool = False
