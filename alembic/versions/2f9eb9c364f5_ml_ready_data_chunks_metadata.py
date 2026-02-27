"""ml ready data_chunks metadata

Revision ID: 2f9eb9c364f5
Revises: 37f5d090b393
Create Date: 2026-02-25 18:19:32.195725

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2f9eb9c364f5"
down_revision: Union[str, None] = "37f5d090b393"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # --- data_chunks: metadata para ML ---
    op.add_column("data_chunks", sa.Column("sample_id", sa.Integer(), nullable=True))
    op.add_column("data_chunks", sa.Column("chunk_idx", sa.Integer(), nullable=True))
    op.add_column("data_chunks", sa.Column("channel_idx", sa.SmallInteger(), nullable=True))
    op.add_column("data_chunks", sa.Column("electrode_name", sa.String(length=64), nullable=True))
    op.add_column("data_chunks", sa.Column("chunk_start_ts", sa.DateTime(), nullable=True))
    op.add_column(
        "data_chunks",
        sa.Column("fs_hz", sa.SmallInteger(), nullable=False, server_default="256"),
    )

    op.create_foreign_key(
        "fk_data_chunks_sample_id",
        "data_chunks",
        "samples",
        ["sample_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # índices para queries típicas ML/QC
    op.create_index(
        "idx_data_chunks_ml_key",
        "data_chunks",
        ["patient_id", "seizure_state", "data_type", "sample_id", "chunk_idx", "channel_idx"],
        unique=False,
    )
    op.create_index(
        "idx_data_chunks_patient_time",
        "data_chunks",
        ["patient_id", "chunk_start_ts"],
        unique=False,
    )

    # UNIQUE válido en particionada: debe incluir partition key (patient_id)
    op.create_unique_constraint(
        "uq_data_chunks_ml_key",
        "data_chunks",
        ["patient_id", "seizure_state", "data_type", "sample_id", "chunk_idx", "channel_idx"],
    )

    # --- samples: unique(data_file) (recomendado) ---
    op.create_unique_constraint("uq_samples_data_file", "samples", ["data_file"])

    # --- seizures: unique(pat_id, onset, offset) (recomendado) ---
    # Nota: "offset" es keyword SQL, pero el nombre de columna en SQLAlchemy/Alembic es válido.
    op.create_unique_constraint(
        "uq_seizures_pat_onset_offset",
        "seizures",
        ["pat_id", "onset", "offset"],
    )


def downgrade():
    # --- seizures / samples ---
    op.drop_constraint("uq_seizures_pat_onset_offset", "seizures", type_="unique")
    op.drop_constraint("uq_samples_data_file", "samples", type_="unique")

    # --- data_chunks ---
    op.drop_constraint(
        "uq_data_chunks_ml_key",
        "data_chunks",
        type_="unique",
    )
    op.drop_index("idx_data_chunks_patient_time", table_name="data_chunks")
    op.drop_index("idx_data_chunks_ml_key", table_name="data_chunks")
    op.drop_constraint("fk_data_chunks_sample_id", "data_chunks", type_="foreignkey")

    op.drop_column("data_chunks", "fs_hz")
    op.drop_column("data_chunks", "chunk_start_ts")
    op.drop_column("data_chunks", "electrode_name")
    op.drop_column("data_chunks", "channel_idx")
    op.drop_column("data_chunks", "chunk_idx")
    op.drop_column("data_chunks", "sample_id")