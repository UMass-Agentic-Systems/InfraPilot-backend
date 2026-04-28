from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/infrapilot"
    SECRET_KEY: SecretStr = SecretStr("change-me")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    GOOGLE_API_KEY: SecretStr = SecretStr("")
    KUBECONFIG_PATH: str = "~/.kube/config"
    SRE_SCAN_INTERVAL_SECONDS: int = 120

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
