from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NewApiCreds:
    api_key: str
    api_secret: str
    api_passphrase: str


@dataclass
class OldApiCreds:
    api_key: str
    secret: str
    passphrase: str


def test_new_api_creds_shape_matches_installed_client() -> None:
    creds = NewApiCreds(api_key="key", api_secret="secret", api_passphrase="pass")
    assert creds.api_secret == "secret"
    assert creds.api_passphrase == "pass"


def test_old_api_creds_shape_documentation_reference() -> None:
    creds = OldApiCreds(api_key="key", secret="secret", passphrase="pass")
    assert creds.secret == "secret"
    assert creds.passphrase == "pass"
