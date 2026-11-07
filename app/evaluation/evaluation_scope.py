"""Development-label capability shared by the current evaluation tools."""

from dataclasses import dataclass
from pathlib import Path

DEVELOPMENT_IDS = frozenset(
    "case-001 case-003 case-007 case-012 case-013 case-014 case-015 case-022 case-024 "
    "case-025 case-028 case-030 case-032 case-041 case-042 case-044 case-052 case-054".split()
)


@dataclass(frozen=True)
class DevelopmentScope:
    answers_file: Path
    case_ids: frozenset[str] = DEVELOPMENT_IDS

    def __post_init__(self):
        if not self.case_ids or not self.case_ids.issubset(DEVELOPMENT_IDS):
            raise ValueError("Scope includes an unapproved development case")
        object.__setattr__(self, "answers_file", Path(self.answers_file).resolve(strict=True))
