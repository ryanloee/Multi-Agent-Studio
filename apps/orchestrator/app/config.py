from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database (SQLite by default)
    database_url: str = "sqlite+aiosqlite:///./data/mas.db"

    # Sandbox (local filesystem)
    sandbox_root: str = ".sandboxes"

    # Streaming
    stream_throttle_window_ms: int = 100

    # Optional LAN access password. When empty, authentication is disabled.
    access_password: str = ""

    model_config = {"env_file": ".env", "env_prefix": "MAS_", "extra": "ignore"}


settings = Settings()
