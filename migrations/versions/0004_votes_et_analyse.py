"""C3 — votes et résultats d'analyse.

`vote` porte une contrainte UNIQUE (participant_id, statement_id) : une seule ligne
par couple, mise à jour au re-vote. C'est la forme que `generate_raw_matrix` de
red-dwarf attend — son `pivot()` casse sur les doublons.

Comme en 0003, le `downgrade` supprime explicitement le type ENUM PostgreSQL, qui
survivrait sinon aux tables et ferait échouer un nouvel `upgrade`.

Revision ID: 2576371f99d0
Revises: 0003_conversations_declarations
Create Date: 2026-09-01 18:19:33.450838
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

ANALYSIS_STATUS = sa.Enum(
    "running", "ok", "error", "insufficient_data", name="analysis_status"
)


revision: str = "0004_votes_et_analyse"
down_revision: str | None = "0003_conversations_declarations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('analysis_run',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('running', 'ok', 'error', 'insufficient_data', name='analysis_status'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('n_participants', sa.Integer(), nullable=True),
    sa.Column('n_statements', sa.Integer(), nullable=True),
    sa.Column('n_votes', sa.Integer(), nullable=True),
    sa.Column('k', sa.Integer(), nullable=True),
    sa.Column('params', sa.JSON(), nullable=True),
    sa.Column('error_text', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_analysis_run_conversation_id'), 'analysis_run', ['conversation_id'], unique=False)
    op.create_table('participant_projection',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('participant_id', sa.Integer(), nullable=False),
    sa.Column('x', sa.Float(), nullable=False),
    sa.Column('y', sa.Float(), nullable=False),
    sa.Column('cluster_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['participant_id'], ['participant.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['analysis_run.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_participant_projection_run_id'), 'participant_projection', ['run_id'], unique=False)
    op.create_table('statement_stat',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('statement_id', sa.Integer(), nullable=False),
    sa.Column('group_id', sa.Integer(), nullable=True),
    sa.Column('repness', sa.Float(), nullable=True),
    sa.Column('p_test', sa.Float(), nullable=True),
    sa.Column('repful_for', sa.String(length=16), nullable=True),
    sa.Column('n_agree', sa.Integer(), nullable=True),
    sa.Column('n_disagree', sa.Integer(), nullable=True),
    sa.Column('n_seen', sa.Integer(), nullable=True),
    sa.Column('priority', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['analysis_run.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['statement_id'], ['statement.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_statement_stat_run_id'), 'statement_stat', ['run_id'], unique=False)
    op.create_table('vote',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('participant_id', sa.Integer(), nullable=False),
    sa.Column('statement_id', sa.Integer(), nullable=False),
    sa.Column('value', sa.SmallInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('modified_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['participant_id'], ['participant.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['statement_id'], ['statement.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('participant_id', 'statement_id', name='uq_vote_participant_statement')
    )
    op.create_index('ix_vote_participant_id', 'vote', ['participant_id'], unique=False)
    op.create_index('ix_vote_statement_id', 'vote', ['statement_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_vote_statement_id', table_name='vote')
    op.drop_index('ix_vote_participant_id', table_name='vote')
    op.drop_table('vote')
    op.drop_index(op.f('ix_statement_stat_run_id'), table_name='statement_stat')
    op.drop_table('statement_stat')
    op.drop_index(op.f('ix_participant_projection_run_id'), table_name='participant_projection')
    op.drop_table('participant_projection')
    op.drop_index(op.f('ix_analysis_run_conversation_id'), table_name='analysis_run')
    op.drop_table('analysis_run')

    bind = op.get_bind()
    ANALYSIS_STATUS.drop(bind, checkfirst=True)
