"""add cartoon models

Revision ID: 2580574d7c64
Revises: 7b966f11ee3a
Create Date: 2026-04-15 13:13:19.547258

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = '2580574d7c64'
down_revision = '7b966f11ee3a'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    # Legacy apps created `children` and `cartoon_avatars` outside Alembic.
    # On a fresh database those tables do not exist yet, so we create the
    # pre-voice_id shape here to keep the migration chain self-contained.
    if not inspector.has_table("children"):
        op.create_table(
            "children",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("photo_filename", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    if not inspector.has_table("cartoon_avatars"):
        op.create_table(
            "cartoon_avatars",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("child_id", sa.Integer(), nullable=False),
            sa.Column("task_id", sa.String(length=100), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=True),
            sa.Column("image_url", sa.String(length=500), nullable=True),
            sa.Column("style_name", sa.String(length=50), nullable=True),
            sa.Column("is_selected", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["child_id"], ["children.id"]),
            sa.PrimaryKeyConstraint("id"),
        )

    op.create_table('cartoons',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('story_prompt', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cartoon_character_links',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cartoon_id', sa.Integer(), nullable=False),
    sa.Column('character_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['cartoon_id'], ['cartoons.id'], ),
    sa.ForeignKeyConstraint(['character_id'], ['characters.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cartoon_participants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cartoon_id', sa.Integer(), nullable=False),
    sa.Column('child_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['cartoon_id'], ['cartoons.id'], ),
    sa.ForeignKeyConstraint(['child_id'], ['children.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cartoon_scenes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('cartoon_id', sa.Integer(), nullable=False),
    sa.Column('scene_number', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('visual_description', sa.Text(), nullable=True),
    sa.Column('duration_seconds', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['cartoon_id'], ['cartoons.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade():
    op.drop_table('cartoon_scenes')
    op.drop_table('cartoon_participants')
    op.drop_table('cartoon_character_links')
    op.drop_table('cartoons')
