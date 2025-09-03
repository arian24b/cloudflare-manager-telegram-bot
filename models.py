from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    cloudflare_token = Column(String, nullable=False)
    admin_user_id = Column(String, nullable=False)  # Tenant admin
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class DomainGroup(Base):
    __tablename__ = "domain_groups"

    id = Column(String, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text)
    domains = Column(Text)  # JSON string of domain IDs
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserSession(Base):
    __tablename__ = "user_sessions"

    user_id = Column(String, primary_key=True)
    current_tenant = Column(Integer)
    current_domain = Column(String)
    current_group = Column(String)
    last_activity = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class BotConfig(Base):
    __tablename__ = "bot_config"

    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
