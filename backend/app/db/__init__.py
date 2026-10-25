from .base import Base
from . import models  # noqa: F401  註冊所有資料表到 Base.metadata

__all__ = ["Base", "models"]
