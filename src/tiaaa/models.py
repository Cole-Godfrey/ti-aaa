"""Core data structures shared by discovery and persistence."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """One active listing document in a community-maintained repository."""

    key: str
    label: str
    repo_url: str
    branch: str
    path: str
    season: str

    @property
    def raw_url(self) -> str:
        owner_repo = self.repo_url.removeprefix("https://github.com/").rstrip("/")
        return f"https://raw.githubusercontent.com/{owner_repo}/{self.branch}/{self.path}"

    @property
    def document_key(self) -> str:
        return f"{self.key}:{self.path}"


@dataclass(slots=True)
class InternshipListing:
    """Normalized internship row extracted from an upstream document."""

    company: str
    role: str
    location: str
    application_url: str
    source_key: str
    source_label: str
    source_repo_url: str
    source_path: str
    category: str = "Tech"
    posting_date: str | None = None
    raw_date: str = ""
    closed: bool = False
    no_sponsorship: bool = False
    citizenship_required: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
