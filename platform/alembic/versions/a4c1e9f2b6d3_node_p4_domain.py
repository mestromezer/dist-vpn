"""node p4_domain

Revision ID: a4c1e9f2b6d3
Revises: 37f79f378eea
Create Date: 2026-07-14 21:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4c1e9f2b6d3'
down_revision: Union[str, Sequence[str], None] = '37f79f378eea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable, no server_default needed (unlike the previous migration's NOT NULL
    # counters) - existing rows simply have no P4 domain until an admin sets one.
    op.add_column('nodes', sa.Column('p4_domain', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('nodes', 'p4_domain')
