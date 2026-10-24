import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from shop.main import create_app
from shop.settings import ShopSettings

ADMIN = {"X-Admin-Token": "test-admin-token"}


@pytest.fixture()
def shop_settings(tmp_path: Path) -> ShopSettings:
    return ShopSettings(
        database_url=f"sqlite:///{tmp_path / 'shop.db'}",
        bad_database_url=f"sqlite:///{tmp_path / 'missing-dir' / 'nope.db'}",
        admin_token=SecretStr("test-admin-token"),
        log_file=str(tmp_path / "shop.jsonl"),
        stats_interval_s=3600,
    )


@pytest.fixture()
def shop(shop_settings: ShopSettings) -> Iterator[TestClient]:
    with TestClient(create_app(shop_settings)) as client:
        yield client


@pytest.fixture()
def pg_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    return url
