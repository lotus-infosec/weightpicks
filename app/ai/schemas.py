"""The JSON Mode schema for prop proposals and its Pydantic mirror (BUILD_PLAN §1.4.4).

`params` is one flat object (the model copes better than with per-template unions);
`Menu.form` checks which keys a template needs and whether each value is allowed.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

MAX_PROPOSALS = 3
TITLE_MAX = 80
BLURB_MAX = 160

PROPOSALS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "maxItems": MAX_PROPOSALS,
            "items": {
                "type": "object",
                "properties": {
                    "template": {
                        "type": "string",
                        "enum": [
                            "milestone_by",
                            "streak_reaches",
                            "beat_last_week",
                            "future_total_change",
                        ],
                    },
                    "params": {
                        "type": "object",
                        "properties": {
                            "threshold": {"type": "number"},
                            "deadline": {"type": "string"},
                            "kind": {"type": "string", "enum": ["weigh_in", "down"]},
                            "n": {"type": "integer"},
                            "metric": {"type": "string"},
                            "day": {"type": "string"},
                        },
                    },
                    "title": {"type": "string", "maxLength": TITLE_MAX},
                    "blurb": {"type": "string", "maxLength": BLURB_MAX},
                },
                "required": ["template", "params", "title", "blurb"],
            },
        }
    },
    "required": ["proposals"],
}


class Proposal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    template: str = Field(max_length=64)
    params: dict[str, Any]
    title: str = Field(max_length=TITLE_MAX)
    blurb: str = Field(max_length=BLURB_MAX)
