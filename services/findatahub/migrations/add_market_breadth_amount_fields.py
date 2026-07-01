"""添加两市成交额字段到 market_breadth_snapshots 表

迁移日期: 2026-05-21
"""

from alembic import op
import sqlalchemy as sa


def upgrade():
    # 添加新字段到 market_breadth_snapshots 表
    op.add_column('market_breadth_snapshots', sa.Column('total_amount', sa.Float(), nullable=True))
    op.add_column('market_breadth_snapshots', sa.Column('total_amount_billion', sa.Float(), nullable=True))
    op.add_column('market_breadth_snapshots', sa.Column('total_volume', sa.Float(), nullable=True))


def downgrade():
    # 回滚：删除新添加的字段
    op.drop_column('market_breadth_snapshots', 'total_volume')
    op.drop_column('market_breadth_snapshots', 'total_amount_billion')
    op.drop_column('market_breadth_snapshots', 'total_amount')
