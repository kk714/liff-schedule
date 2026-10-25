"""default visibility team_detail

2026-10-25 決策：使用者不需選擇分享範圍，新增的行程預設讓團隊看見（team_detail）。
只調整成員的預設值，不動已建立的行程。

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE users SET default_visibility = 'team_detail' WHERE default_visibility = 'busy_only'")


def downgrade() -> None:
    op.execute("UPDATE users SET default_visibility = 'busy_only' WHERE default_visibility = 'team_detail'")
