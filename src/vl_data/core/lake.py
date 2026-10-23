"""A Hive-partitioned Parquet lake with per-drive partition replacement.

Layout: <root>/<table>/drive=<drive>/part-0.parquet. A partition is replaced by
writing a new folder, moving the old one aside, and renaming the new one into
place (two renames, not one atomic operation; a failure restores the old
partition). Temporary and replaced folders live in <root>/_swap, outside the
table folders, so globs and listings never see them.

An old-* folder in <root>/_swap may hold the only copy of a partition (after a
crash or a failed restore). They are never deleted automatically and must be
recovered or removed by hand.
"""

import re
import shutil
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from vl_data.core.lake_schemas import TABLE_SCHEMAS

PART_NAME = "part-0.parquet"
SWAP_DIR = "_swap"
# Upper bound of rows per Parquet row group. Row groups also break at every batch,
# so identical bytes need identical batch boundaries as well as this constant.
ROW_GROUP_ROWS = 65536
# A new-* folder older than this is taken for a crashed swap's leftover.
STALE_NEW_SECONDS = 3600
_DRIVE_RE = re.compile(r"[A-Za-z0-9_-]+")
_DRIVE_FIELD = pa.field("drive", pa.string())


class Lake:
    """Parquet lake under a root folder, one partition per (table, drive).

    The lake is single-writer: two swaps on the same root at the same time are
    not supported. The sweep at the start of a swap removes only stale new-*
    folders (older than STALE_NEW_SECONDS) and never touches old-* folders.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _table_dir(self, table: str) -> Path:
        if table not in TABLE_SCHEMAS:
            raise ValueError(f"unknown table {table!r}; expected one of {sorted(TABLE_SCHEMAS)}")
        return self.root / table

    def _partition_dir(self, table: str, drive: str) -> Path:
        table_dir = self._table_dir(table)
        if not _DRIVE_RE.fullmatch(drive):
            raise ValueError(f"invalid drive id {drive!r}: use letters, digits, '_' or '-'")
        return table_dir / f"drive={drive}"

    def swap_partition(self, table: str, drive: str, batches: Iterable[pa.RecordBatch]) -> Path:
        """Replace the partition of (table, drive) with the given batches; return its folder.

        Batches are streamed into a temp folder under <root>/_swap, then the old
        partition (if any) is moved aside and the new folder renamed into place.
        If anything fails before that, the old partition stays and the temp
        folder is removed. A batch whose schema differs from the table schema
        raises ValueError. Zero batches write a valid zero-row file.

        Files are byte-identical for identical input only with the same pyarrow
        version and the same batch boundaries (rows are written in row groups of
        at most ROW_GROUP_ROWS rows).
        """
        final = self._partition_dir(table, drive)
        schema = TABLE_SCHEMAS[table]
        swap_root = self.root / SWAP_DIR
        swap_root.mkdir(parents=True, exist_ok=True)
        _sweep_leftovers(swap_root)
        token = uuid.uuid4().hex
        new_dir = swap_root / f"new-{token}"
        old_dir = swap_root / f"old-{token}"
        moved_old = False
        try:
            new_dir.mkdir()
            _write_parquet(new_dir / PART_NAME, schema, batches)
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():
                moved_old = True  # set first: an interrupt during the rename must still restore
                final.rename(old_dir)
            new_dir.rename(final)
        except BaseException as exc:
            if moved_old and old_dir.exists() and not final.exists():
                try:
                    old_dir.rename(final)
                except OSError as restore_error:
                    exc.add_note(
                        f"restoring the old partition failed ({restore_error}); "
                        f"the old partition is still in {old_dir} "
                        "and is never removed automatically"
                    )
            raise
        finally:
            shutil.rmtree(new_dir, ignore_errors=True)
        shutil.rmtree(old_dir, ignore_errors=True)
        return final

    def drop_partition(self, table: str, drive: str) -> None:
        """Delete the partition of (table, drive); a missing one is a no-op."""
        partition = self._partition_dir(table, drive)
        if not partition.exists():
            return
        shutil.rmtree(partition)

    def list_drives(self, table: str) -> list[str]:
        """Return the sorted drive ids that have a part file in the table."""
        table_dir = self._table_dir(table)
        if not table_dir.is_dir():
            return []
        return sorted(
            p.name.removeprefix("drive=")
            for p in table_dir.iterdir()
            if p.name.startswith("drive=")
            and _DRIVE_RE.fullmatch(p.name.removeprefix("drive="))
            and (p / PART_NAME).is_file()
        )

    def read(self, table: str, drive: str | None = None) -> pa.Table:
        """Read one drive (or all drives) of a table.

        The result has the table schema plus a string column "drive" (last).
        An unknown drive or an empty table gives an empty table with that schema.
        Every requested drive is loaded into memory.
        """
        if drive is None:
            drives = self.list_drives(table)
        else:
            partition = self._partition_dir(table, drive)  # validates table and drive
            drives = [drive] if (partition / PART_NAME).is_file() else []
        tables = [self._read_one(table, d) for d in drives]
        if not tables:
            return _empty_table_with_drive(TABLE_SCHEMAS[table])
        return pa.concat_tables(tables)

    def _read_one(self, table: str, drive: str) -> pa.Table:
        path = self._partition_dir(table, drive) / PART_NAME
        with pq.ParquetFile(path) as f:
            data = f.read().replace_schema_metadata(None)
        return data.append_column(_DRIVE_FIELD, pa.array([drive] * data.num_rows, pa.string()))

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Return an in-memory DuckDB connection with one view per table.

        Each view has the table columns plus a VARCHAR "drive" column. Views
        are built at connect time: partitions added later are not seen by an
        open connection. A table with no partition gets an empty typed view.
        An open connection can hold the part files and block a later swap on
        Windows, so close it before swapping.
        """
        con = duckdb.connect(":memory:")
        for table, schema in TABLE_SCHEMAS.items():
            drives = self.list_drives(table)
            if drives:
                # hive parsing is off: it would read drive=/env= segments of the root path
                cols = ", ".join(f'"{n}"' for n in schema.names)
                parts = []
                for d in drives:
                    path = (self.root.resolve() / table / f"drive={d}" / PART_NAME).as_posix()
                    parts.append(
                        f"SELECT {cols}, '{d}'::VARCHAR AS drive FROM read_parquet("
                        f"'{path.replace(chr(39), chr(39) * 2)}', hive_partitioning = false)"
                    )
                con.execute(f"CREATE VIEW {table} AS " + " UNION ALL ".join(parts))
            else:
                con.register(table, _empty_table_with_drive(schema))
        return con


def _sweep_leftovers(swap_root: Path) -> None:
    """Best-effort removal of stale new-* folders left by a crashed earlier swap.

    Only plain new-* directories last modified more than STALE_NEW_SECONDS ago
    are removed; fresh ones may belong to a live swap. old-* folders are never
    deleted automatically because they may hold the only copy of a partition;
    they must be recovered or removed by hand.
    """
    cutoff = time.time() - STALE_NEW_SECONDS
    for p in swap_root.iterdir():
        if not p.name.startswith("new-") or p.is_symlink() or not p.is_dir():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            continue


def _empty_table_with_drive(schema: pa.Schema) -> pa.Table:
    """Return a zero-row table with the table schema plus the string "drive" column."""
    return schema.append(_DRIVE_FIELD).empty_table()


def _write_parquet(path: Path, schema: pa.Schema, batches: Iterable[pa.RecordBatch]) -> None:
    """Stream batches to one Parquet file with pinned write options."""
    with pq.ParquetWriter(
        path,
        schema,
        version="2.6",
        compression="snappy",
        data_page_version="1.0",
        use_dictionary=True,
        write_statistics=True,
        store_schema=True,
        write_page_index=False,
        write_page_checksum=False,
    ) as writer:
        for batch in batches:
            if not batch.schema.equals(schema):
                raise ValueError(f"batch schema does not match the table schema: {batch.schema}")
            writer.write_batch(batch, row_group_size=ROW_GROUP_ROWS)
