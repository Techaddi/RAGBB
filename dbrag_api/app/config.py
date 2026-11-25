from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    api_keys: str = ""
    admin_api_key: str = ""
    rate_limit_per_minute: int = 60

    # SQL Server
    db_server: str = "localhost"
    db_name: str = "Local_Media"
    db_driver: str = "ODBC Driver 18 for SQL Server"
    db_user: str = ""
    db_password: str = ""
    db_trust_server_cert: bool = True
    db_query_timeout_sec: int = 15
    db_login_timeout_sec: int = 10
    db_read_only_intent: bool = False
    db_concurrency: int = 8

    # LLM
    llm_provider: str = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5-coder:latest"
    llm_api_key: str = ""
    llm_timeout_sec: int = 120
    llm_concurrency: int = 2
    llm_num_ctx: int = 8192
    llm_max_tokens: int = 900

    # pipeline
    top_k_tables: int = 6
    max_rows: int = 200
    max_attempts: int = 3
    plan_cache_ttl_sec: int = 3600
    plan_cache_size: int = 2000
    max_question_chars: int = 500
    schema_file: str = "data/schema.json"
    descriptions_file: str = "data/table_descriptions.json"
    chroma_dir: str = "data/chroma"
    blocked_columns: str = (
        "password_hash,otp_hash,token_hash,refresh_token_hash,replaced_by_hash,fcm_token"
    )
    max_new_tables: int = 5

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def blocked_column_set(self) -> set[str]:
        return {c.strip().lower() for c in self.blocked_columns.split(",") if c.strip()}

    @property
    def is_prod(self) -> bool:
        return self.env.lower() == "production"

    def odbc_conn_str(self) -> str:
        parts = [
            f"DRIVER={{{self.db_driver}}}",
            f"SERVER={self.db_server}",
            f"DATABASE={self.db_name}",
            f"TrustServerCertificate={'yes' if self.db_trust_server_cert else 'no'}",
            "Encrypt=yes",
        ]
        if self.db_user:
            parts += [f"UID={self.db_user}", f"PWD={self.db_password}"]
        else:
            parts.append("Trusted_Connection=yes")
        if self.db_read_only_intent:
            parts.append("ApplicationIntent=ReadOnly")
        return ";".join(parts) + ";"


@lru_cache
def get_settings() -> Settings:
    return Settings()
