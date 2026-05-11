"""add users and ownership

Revision ID: 1daddd7f8c1e
Revises: c94b9ea2a6d1
Create Date: 2026-05-11 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "1daddd7f8c1e"
down_revision = "c94b9ea2a6d1"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if not inspector.has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("email"),
        )
        op.create_index(op.f("ix_users_email"), "users", ["email"], unique=False)

    child_columns = (
        {col["name"] for col in inspector.get_columns("children")}
        if inspector.has_table("children")
        else set()
    )
    if "created_by_user_id" not in child_columns:
        with op.batch_alter_table("children", schema=None) as batch_op:
            batch_op.add_column(sa.Column("created_by_user_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_children_created_by_user_id_users",
                "users",
                ["created_by_user_id"],
                ["id"],
            )

    cartoon_columns = (
        {col["name"] for col in inspector.get_columns("cartoons")}
        if inspector.has_table("cartoons")
        else set()
    )
    if "created_by_user_id" not in cartoon_columns:
        with op.batch_alter_table("cartoons", schema=None) as batch_op:
            batch_op.add_column(sa.Column("created_by_user_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_cartoons_created_by_user_id_users",
                "users",
                ["created_by_user_id"],
                ["id"],
            )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    cartoon_columns = (
        {col["name"] for col in inspector.get_columns("cartoons")}
        if inspector.has_table("cartoons")
        else set()
    )
    if "created_by_user_id" in cartoon_columns:
        with op.batch_alter_table("cartoons", schema=None) as batch_op:
            batch_op.drop_constraint("fk_cartoons_created_by_user_id_users", type_="foreignkey")
            batch_op.drop_column("created_by_user_id")

    child_columns = (
        {col["name"] for col in inspector.get_columns("children")}
        if inspector.has_table("children")
        else set()
    )
    if "created_by_user_id" in child_columns:
        with op.batch_alter_table("children", schema=None) as batch_op:
            batch_op.drop_constraint("fk_children_created_by_user_id_users", type_="foreignkey")
            batch_op.drop_column("created_by_user_id")

    if inspector.has_table("users"):
        op.drop_index(op.f("ix_users_email"), table_name="users")
        op.drop_table("users")
