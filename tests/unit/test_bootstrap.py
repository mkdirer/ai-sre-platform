"""Smoke coverage for the repository package and configuration foundation."""

from importlib import import_module

import pytest
from pydantic import ValidationError

from packages.config import Environment, Settings


@pytest.mark.parametrize(
    "module_name",
    [
        "apps.demo.gateway",
        "apps.demo.order_service",
        "apps.demo.inventory_service",
        "apps.demo.payment_service",
        "apps.demo.alert_receiver",
        "apps.incident_api",
        "apps.investigator_worker",
        "packages.agents",
        "packages.tools",
        "packages.rag",
        "packages.models",
        "packages.persistence",
        "packages.telemetry",
    ],
)
def test_architecture_namespaces_are_importable(module_name: str) -> None:
    """Every Python boundary documented for bootstrap can be imported."""

    assert import_module(module_name).__name__ == module_name


def test_settings_load_and_hide_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment values are typed and secret material is absent from representations."""

    password = "local-test-password"
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("POSTGRES_PORT", "5544")
    monkeypatch.setenv("POSTGRES_PASSWORD", password)

    settings = Settings(_env_file=None)

    assert settings.environment is Environment.TEST
    assert settings.postgres_port == 5544
    assert settings.postgres_password.get_secret_value() == password
    assert password not in repr(settings)
    assert password not in str(settings.model_dump())


def test_settings_reject_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configuration validation fails before an invalid network port can be used."""

    monkeypatch.setenv("POSTGRES_PORT", "70000")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
