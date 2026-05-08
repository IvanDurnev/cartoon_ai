"""add voice_id to children and characters

Revision ID: c94b9ea2a6d1
Revises: bc60a267d1d3
Create Date: 2026-04-15 18:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c94b9ea2a6d1"
down_revision = "bc60a267d1d3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("children", schema=None) as batch_op:
        batch_op.add_column(sa.Column("voice_id", sa.String(length=120), nullable=True))

    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.add_column(sa.Column("voice_id", sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table("characters", schema=None) as batch_op:
        batch_op.drop_column("voice_id")

    with op.batch_alter_table("children", schema=None) as batch_op:
        batch_op.drop_column("voice_id")

