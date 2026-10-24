from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpsPilotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPSPILOT_", env_file=".env", extra="ignore")

    # --- storage / API -----------------------------------------------------------------
    database_url: str = "postgresql+psycopg://opspilot:localdevonly@localhost:5432/opspilot"
    api_secret: SecretStr = SecretStr("")
    log_level: str = "INFO"

    # --- LLM (any OpenAI-compatible API) --------------------------------------------------
    llm_provider: Literal["groq", "gemini", "openai", "ollama", "custom"] = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = ""  # only for provider=custom
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 3
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 1500

    # --- agent budgets ------------------------------------------------------------------
    agent_max_tool_calls: int = 8
    agent_max_tokens: int = 40_000
    agent_timeout_s: float = 90.0
    tool_timeout_s: float = 15.0

    # --- MCP servers the agent may use ----------------------------------------------------
    mcp_servers: dict[str, str] = {
        "logs": "http://localhost:8101/mcp",
        "metrics": "http://localhost:8102/mcp",
        "git": "http://localhost:8103/mcp",
    }

    # --- worker / queue -----------------------------------------------------------------
    worker_poll_interval_s: float = 1.0
    worker_lease_s: int = 300
    job_max_attempts: int = 3
    default_window_minutes: int = 15


@lru_cache
def get_settings() -> OpsPilotSettings:
    return OpsPilotSettings()
