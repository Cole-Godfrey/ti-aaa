"""Runtime paths, source registry, and user configuration."""

from __future__ import annotations

import json
import os
import platform
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from tiaaa.models import SourceDocument

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"


SOURCE_DOCUMENTS: tuple[SourceDocument, ...] = (
    SourceDocument(
        key="sndsh404-summer-2027",
        label="sndsh404 · Summer 2027",
        repo_url="https://github.com/sndsh404/summer-2027-internships",
        branch="main",
        path="README.md",
        season="2027",
    ),
    SourceDocument(
        key="vanshb03-summer-2027",
        label="Vansh · Summer 2027",
        repo_url="https://github.com/vanshb03/Summer2027-Internships",
        branch="dev",
        path="README.md",
        season="2027",
    ),
    SourceDocument(
        key="vanshb03-summer-2027",
        label="Vansh · 2027 Off-season",
        repo_url="https://github.com/vanshb03/Summer2027-Internships",
        branch="dev",
        path="OFFSEASON_README.md",
        season="2027 off-season",
    ),
    SourceDocument(
        key="simplify-summer-2026",
        label="Simplify & Pitt CSC · Summer 2026",
        repo_url="https://github.com/SimplifyJobs/Summer2026-Internships",
        branch="dev",
        path="README.md",
        season="2026",
    ),
    SourceDocument(
        key="simplify-summer-2026",
        label="Simplify & Pitt CSC · 2026 Off-season",
        repo_url="https://github.com/SimplifyJobs/Summer2026-Internships",
        branch="dev",
        path="README-Off-Season.md",
        season="2026 off-season",
    ),
)


DEFAULT_SETTINGS: dict[str, Any] = {
    "poll_interval_seconds": 300,
    "minimum_fit_score": 5,
    "initial_sync": "baseline",
    "filters": {
        "include_role_keywords": [],
        "exclude_keywords": [],
        "allowed_locations": [],
        "remote_only": False,
    },
    "preparation": {
        "use_llm": False,
        "generate_cover_letters": True,
    },
    "automation": {
        "allow_submission": False,
        "max_applications_per_cycle": 5,
        "max_applications_per_day": 25,
        "max_attempts": 3,
        "claude_model": "sonnet",
        "headless": False,
        "timeout_seconds": 600,
    },
    "dashboard": {"host": "127.0.0.1", "port": 8787},
}


@dataclass(frozen=True, slots=True)
class AppPaths:
    """All mutable user data lives outside the source checkout."""

    root: Path

    @property
    def database(self) -> Path:
        return self.root / "tiaaa.db"

    @property
    def profile(self) -> Path:
        return self.root / "profile.json"

    @property
    def settings(self) -> Path:
        return self.root / "settings.yaml"

    @property
    def env(self) -> Path:
        return self.root / ".env"

    @property
    def resume_text(self) -> Path:
        return self.root / "resume.txt"

    @property
    def resume_pdf(self) -> Path:
        return self.root / "resume.pdf"

    @property
    def packets(self) -> Path:
        return self.root / "application-packets"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def browser_profiles(self) -> Path:
        return self.root / "browser-profiles"

    @property
    def workers(self) -> Path:
        return self.root / "workers"


def get_paths(root: Path | str | None = None) -> AppPaths:
    configured = root or os.environ.get("TIAAA_HOME") or (Path.home() / ".tiaaa")
    return AppPaths(Path(configured).expanduser().resolve())


def ensure_dirs(paths: AppPaths | None = None) -> AppPaths:
    paths = paths or get_paths()
    for directory in (
        paths.root,
        paths.packets,
        paths.logs,
        paths.browser_profiles,
        paths.workers,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            directory.chmod(0o700)
    return paths


def load_environment(paths: AppPaths | None = None) -> None:
    paths = paths or get_paths()
    if paths.env.exists():
        load_dotenv(paths.env, override=False)
    load_dotenv(override=False)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in base.items():
        if isinstance(value, dict):
            incoming = override.get(key, {})
            merged[key] = _deep_merge(value, incoming if isinstance(incoming, dict) else {})
        else:
            merged[key] = override.get(key, value)
    for key, value in override.items():
        if key not in merged:
            merged[key] = value
    return merged


def load_settings(paths: AppPaths | None = None) -> dict[str, Any]:
    paths = paths or get_paths()
    if not paths.settings.exists():
        return _deep_merge(DEFAULT_SETTINGS, {})
    loaded = yaml.safe_load(paths.settings.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Settings must be a YAML mapping: {paths.settings}")
    return _deep_merge(DEFAULT_SETTINGS, loaded)


def load_profile(paths: AppPaths | None = None) -> dict[str, Any]:
    paths = paths or get_paths()
    if not paths.profile.exists():
        raise FileNotFoundError(f"Profile not found at {paths.profile}. Run `tiaaa init` first.")
    profile = json.loads(paths.profile.read_text(encoding="utf-8"))
    required = ("personal", "education", "work_authorization", "preferences")
    missing = [key for key in required if not isinstance(profile.get(key), dict)]
    if missing:
        raise ValueError(f"Profile is missing required sections: {', '.join(missing)}")
    return profile


def initialize_user_files(paths: AppPaths | None = None, force: bool = False) -> list[Path]:
    """Install editable templates without overwriting user data by default."""

    paths = ensure_dirs(paths)
    mappings = (
        (TEMPLATE_DIR / "profile.example.json", paths.profile),
        (TEMPLATE_DIR / "settings.example.yaml", paths.settings),
    )
    created: list[Path] = []
    for source, destination in mappings:
        if force or not destination.exists():
            shutil.copyfile(source, destination)
            with suppress(OSError):
                destination.chmod(0o600)
            created.append(destination)
    if force or not paths.env.exists():
        paths.env.touch(mode=0o600, exist_ok=True)
        with suppress(OSError):
            paths.env.chmod(0o600)
        created.append(paths.env)
    return created


def get_chrome_path() -> str:
    override = os.environ.get("CHROME_PATH")
    if override and Path(override).is_file():
        return override

    system = platform.system()
    candidates: list[Path] = []
    if system == "Darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            ]
        )
    elif system == "Windows":
        for base in (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base:
                candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
    else:
        for command in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            if resolved := shutil.which(command):
                candidates.append(Path(resolved))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("Chrome/Chromium was not found. Install it or set CHROME_PATH.")
