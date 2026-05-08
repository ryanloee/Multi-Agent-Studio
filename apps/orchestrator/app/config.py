import platform

from pydantic_settings import BaseSettings


def _default_docker_socket() -> str:
    """Return the default Docker socket path for the current OS."""
    if platform.system() == "Windows":
        return "npipe:////./pipe/docker_engine"
    return "unix:///var/run/docker.sock"


class Settings(BaseSettings):
    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/multi_agent_studio"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "agent-workflow"

    # Docker
    docker_socket: str = _default_docker_socket()
    sandbox_image: str = "multi-agent-studio/sandbox-base:latest"

    # OpenCode
    opencode_stream_dir: str = "/workspace/.opencode"
    opencode_stream_file: str = "stream.jsonl"
    opencode_log_max_bytes: int = 50 * 1024 * 1024  # 50MB Log Bomb defense

    # Streaming
    stream_throttle_window_ms: int = 100

    model_config = {"env_file": ".env", "env_prefix": "MAS_"}


settings = Settings()
