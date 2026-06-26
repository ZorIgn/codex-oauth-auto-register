from __future__ import annotations

import os


class Settings:
    app_name: str = os.getenv("PORTAL_APP_NAME", "Customer Portal API")
    app_version: str = "0.2.0"
    jwt_secret: str = os.getenv("PORTAL_JWT_SECRET", "change-me-in-production")
    access_token_ttl_seconds: int = int(os.getenv("PORTAL_ACCESS_TOKEN_TTL_SECONDS", "7200"))
    refresh_token_ttl_seconds: int = int(os.getenv("PORTAL_REFRESH_TOKEN_TTL_SECONDS", str(30 * 24 * 3600)))
    seed_admin_username: str = os.getenv("PORTAL_ADMIN_USERNAME", "admin")
    seed_admin_password: str = os.getenv("PORTAL_ADMIN_PASSWORD", "admin123456")
    seed_admin_email: str = os.getenv("PORTAL_ADMIN_EMAIL", "admin@example.com")
    cors_origins: list[str] = [item.strip() for item in os.getenv("PORTAL_CORS_ORIGINS", "*").split(",") if item.strip()]


settings = Settings()
