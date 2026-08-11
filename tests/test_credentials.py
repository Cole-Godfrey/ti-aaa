from __future__ import annotations

import re
import stat

import pytest

from tiaaa.config import AppPaths
from tiaaa.credentials import application_account_password


def test_application_account_password_is_stable_and_scoped_to_portal_and_email(
    tmp_path,
) -> None:
    paths = AppPaths(tmp_path)

    password = application_account_password(
        paths=paths,
        application_url="https://careers.example.com/jobs/one",
        email="Avery@example.com",
    )

    assert password == application_account_password(
        paths=paths,
        application_url="https://careers.example.com/jobs/two",
        email="avery@example.com",
    )
    assert password != application_account_password(
        paths=paths,
        application_url="https://jobs.other.example/apply",
        email="avery@example.com",
    )
    assert password != application_account_password(
        paths=paths,
        application_url="https://careers.example.com/jobs/one",
        email="other@example.com",
    )
    assert re.fullmatch(r"(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*!)\S{20}", password)


def test_application_account_key_is_private_and_does_not_store_the_password(
    tmp_path,
) -> None:
    paths = AppPaths(tmp_path)
    password = application_account_password(
        paths=paths,
        application_url="https://careers.example.com/apply",
        email="avery@example.com",
    )

    assert stat.S_IMODE(paths.employer_account_key.stat().st_mode) == 0o600
    assert password.encode() not in paths.employer_account_key.read_bytes()


@pytest.mark.parametrize(
    ("url", "email"),
    [("not-a-url", "avery@example.com"), ("https://jobs.example", "")],
)
def test_application_account_password_requires_a_portal_and_candidate_email(
    tmp_path, url, email
) -> None:
    with pytest.raises(ValueError):
        application_account_password(
            paths=AppPaths(tmp_path),
            application_url=url,
            email=email,
        )
