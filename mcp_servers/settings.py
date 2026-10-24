from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_", env_file=".env", extra="ignore")

    backend: Literal["local", "aws"] = "local"  # logs + metrics
    git_backend: Literal["local", "github"] = "local"
    host: str = "127.0.0.1"
    port: int = 8101
    # Host headers accepted by the HTTP transport (DNS-rebinding protection).
    allowed_hosts: list[str] = ["localhost:*", "127.0.0.1:*"]
    log_level: str = "INFO"

    # local backends
    log_file: str = "var/logs/shop.jsonl"
    git_repo_path: str = "var/deploy-history"

    # aws backends
    aws_region: str = "ap-south-1"
    log_group: str = "/opspilot/shop"
    metrics_namespace: str = "OpsPilot/Shop"
    github_repo: str = ""  # "owner/name"
    github_token: SecretStr = SecretStr("")
