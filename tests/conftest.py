from __future__ import annotations

from copy import deepcopy

import pytest

from tiaaa.config import DEFAULT_SETTINGS


@pytest.fixture
def profile() -> dict:
    return {
        "personal": {
            "full_name": "Avery Student",
            "email": "avery@example.com",
            "phone": "555-0100",
        },
        "education": {
            "school": "Example University",
            "degree": "Bachelor of Science",
            "major": "Computer Science",
            "current_year": "sophomore",
        },
        "work_authorization": {
            "legally_authorized_to_work_us": True,
            "requires_sponsorship": False,
            "us_citizen": False,
        },
        "preferences": {
            "roles": ["software", "backend", "machine learning"],
            "locations": ["remote", "seattle"],
        },
        "skills": {"languages": ["Python"]},
        "eeo_voluntary": {},
    }


@pytest.fixture
def settings() -> dict:
    return deepcopy(DEFAULT_SETTINGS)
