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
from dotenv import dotenv_values, load_dotenv, set_key, unset_key

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
        key="simplify-summer-2026",
        label="Simplify & Pitt CSC · Summer 2026",
        repo_url="https://github.com/SimplifyJobs/Summer2026-Internships",
        branch="dev",
        path="README.md",
        season="2026",
    ),
)


DEFAULT_SETTINGS: dict[str, Any] = {
    "poll_interval_seconds": 300,
    "service": {
        "enabled": True,
        "auto_prepare": True,
    },
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
        "auto_apply_new": False,
        "manual_auto_submit": False,
        "auto_apply_minimum_fit_score": 7,
        "auto_apply_use_preferences": False,
        "web_push_notifications": False,
        "allow_submission": False,
        "workers": 1,
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
    def resumes(self) -> Path:
        return self.root / "resumes"

    @property
    def previews(self) -> Path:
        return self.root / "live-previews"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def browser_profiles(self) -> Path:
        return self.root / "browser-profiles"

    @property
    def workers(self) -> Path:
        return self.root / "workers"

    @property
    def web_push_private_key(self) -> Path:
        return self.root / "web-push-private.pem"

    @property
    def employer_account_key(self) -> Path:
        return self.root / "employer-account.key"


def get_paths(root: Path | str | None = None) -> AppPaths:
    configured = root or os.environ.get("TIAAA_HOME") or (Path.home() / ".tiaaa")
    return AppPaths(Path(configured).expanduser().resolve())


def ensure_dirs(paths: AppPaths | None = None) -> AppPaths:
    paths = paths or get_paths()
    for directory in (
        paths.root,
        paths.packets,
        paths.resumes,
        paths.previews,
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
    return _normalize_settings(_deep_merge(DEFAULT_SETTINGS, loaded))


def _normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Discard retired settings when an existing installation is loaded or saved."""

    settings.pop("minimum_fit_score", None)
    settings.pop("notifications", None)
    preparation = settings.get("preparation")
    if isinstance(preparation, dict):
        preparation.pop("tailor_resumes", None)
    automation = settings.get("automation")
    if isinstance(automation, dict):
        legacy_submission_split = (
            "auto_apply_eligible_only" in automation or "enabled" in automation
        )
        if (
            legacy_submission_split
            and bool(automation.get("auto_apply_new"))
            and not bool(automation.get("allow_submission"))
        ):
            # Older releases had two separate switches. Do not reinterpret an old
            # discovery-only choice as permission for unattended final submission.
            automation["auto_apply_new"] = False
        automation.pop("auto_apply_eligible_only", None)
        automation.pop("enabled", None)
    return settings


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


def save_profile(profile: dict[str, Any], paths: AppPaths | None = None) -> Path:
    """Validate and persist the web/CLI profile with private file permissions."""

    paths = ensure_dirs(paths)
    required = ("personal", "education", "work_authorization", "preferences")
    missing = [key for key in required if not isinstance(profile.get(key), dict)]
    if missing:
        raise ValueError(f"Profile is missing required sections: {', '.join(missing)}")
    paths.profile.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with suppress(OSError):
        paths.profile.chmod(0o600)
    return paths.profile


def save_settings(settings: dict[str, Any], paths: AppPaths | None = None) -> Path:
    """Persist user settings after merging defaults and clamping unsafe runtime values."""

    paths = ensure_dirs(paths)
    merged = _normalize_settings(_deep_merge(DEFAULT_SETTINGS, settings))
    merged["poll_interval_seconds"] = max(30, int(merged.get("poll_interval_seconds", 300)))
    automation = merged["automation"]
    automation["auto_apply_minimum_fit_score"] = max(
        1, min(10, int(automation.get("auto_apply_minimum_fit_score", 7)))
    )
    automation["web_push_notifications"] = bool(
        automation.get("web_push_notifications", False)
    )
    automation["manual_auto_submit"] = bool(
        automation.get("manual_auto_submit", False)
    )
    automation["workers"] = max(1, min(8, int(automation.get("workers", 1))))
    automation["max_applications_per_cycle"] = max(
        1, min(50, int(automation.get("max_applications_per_cycle", 5)))
    )
    automation["max_applications_per_day"] = max(
        1, min(200, int(automation.get("max_applications_per_day", 25)))
    )
    automation["max_attempts"] = max(1, min(10, int(automation.get("max_attempts", 3))))
    automation["timeout_seconds"] = max(
        60, min(3600, int(automation.get("timeout_seconds", 600)))
    )
    paths.settings.write_text(
        yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    with suppress(OSError):
        paths.settings.chmod(0o600)
    return paths.settings


SECRET_NAMES = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
)


def secret_status(paths: AppPaths | None = None) -> dict[str, dict[str, str | bool]]:
    """Return write-only secret presence and a short suffix, never the secret itself."""

    paths = paths or get_paths()
    stored = dotenv_values(paths.env) if paths.env.exists() else {}
    result: dict[str, dict[str, str | bool]] = {}
    for name in SECRET_NAMES:
        value = os.environ.get(name) or str(stored.get(name) or "")
        result[name] = {
            "configured": bool(value),
            "suffix": value[-4:] if len(value) >= 4 else "",
        }
    return result


def update_secrets(
    updates: dict[str, str | None],
    *,
    clear: list[str] | None = None,
    paths: AppPaths | None = None,
) -> Path:
    """Update the fixed local secret set without returning or logging values."""

    paths = ensure_dirs(paths)
    unknown = (set(updates) | set(clear or [])) - set(SECRET_NAMES)
    if unknown:
        raise ValueError(f"Unknown secret fields: {', '.join(sorted(unknown))}")
    paths.env.touch(mode=0o600, exist_ok=True)
    for name in clear or []:
        unset_key(paths.env, name)
        os.environ.pop(name, None)
    for name, value in updates.items():
        if value:
            clean_value = value.strip()
            if "\n" in clean_value or "\r" in clean_value:
                raise ValueError(f"{name} must be a single-line value")
            set_key(paths.env, name, clean_value, quote_mode="always")
            os.environ[name] = clean_value
    with suppress(OSError):
        paths.env.chmod(0o600)
    return paths.env


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
