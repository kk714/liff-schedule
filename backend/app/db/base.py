"""ORM 基底：命名慣例、共用型別與欄位。

型別刻意採可攜寫法（Uuid、JSON＋PostgreSQL JSONB 變體、列舉存為字串＋CHECK），
讓 D8 的 SQLite 測試與正式 PostgreSQL 使用同一份模型與 migration。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, MetaData, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# 固定約束名稱，Alembic 才能穩定地產生與比對 migration
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator[datetime]):
    """一律以 UTC 寫入、讀出帶 UTC 時區的 datetime。

    SQLite 會丟掉 offset（10:00+08:00 存成 10:00），不轉 UTC 會讓範圍查詢錯位；
    PostgreSQL 上等同 timestamptz。拒絕不帶時區的值，避免誤存當地時間。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed; pass an aware datetime")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def str_enum(name: str, *values: str) -> Enum:
    """存成 VARCHAR＋CHECK，不建立 PostgreSQL 原生 ENUM（新增值不必改型別）。"""
    return Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        uuid.UUID: Uuid(),
        datetime: UTCDateTime(),
        dict[str, Any]: JSONType,
        list[Any]: JSONType,
    }


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
