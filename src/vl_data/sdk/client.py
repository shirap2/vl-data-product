"""VLClient: read-only access to the lake tables as Arrow tables, plus SQL.

Every table method returns the table schema from TABLE_SCHEMAS exactly: the
"drive" column that Lake.read adds is dropped, because the drive is the
argument. A valid but unknown drive gives an empty table with that schema; an
invalid drive id (for example "a/b" or "") raises ValueError from the Lake. In the SQL
views the "drive" column is still present (see Lake.connect).
"""

import duckdb
import pyarrow as pa
import pyarrow.compute as pc

from vl_data.core.lake import Lake

_DRIVE_COLUMN = "drive"


class VLClient:
    """Read-only access to the lake tables."""

    def __init__(self, lake: Lake) -> None:
        self._lake = lake

    def drives(self) -> list[str]:
        """Drive ids that have keypoints, sorted."""
        return self._lake.list_drives("keypoints")

    def keypoints(self, drive: str, keypoint: str | None = None) -> pa.Table:
        """Keypoint rows of a drive in cm, ordered by frame_id, optionally one keypoint.

        Ties within a frame are ordered by keypoint name.
        """
        table = self._read("keypoints", drive)
        if keypoint is not None:
            mask = pc.equal(table.column("keypoint"), keypoint)
            table = table.filter(mask)
        return table.sort_by([("frame_id", "ascending"), ("keypoint", "ascending")])

    def motion(self, drive: str) -> pa.Table:
        """Motion rows of a drive: x in cm, velocity in cm/s, acceleration in cm/s^2."""
        return self._read("motion", drive)

    def lineage(self, drive: str) -> pa.Table:
        """Lineage row of a drive: scenario hash, status and counts."""
        return self._read("lineage", drive)

    def quarantine(self, drive: str) -> pa.Table:
        """Quarantine rows of a drive."""
        return self._read("quarantine", drive)

    def sql(self, query: str) -> pa.Table:
        """Run exactly one SELECT statement over the table views; return an Arrow table.

        Anything else raises ValueError. The connection is closed after every
        query so that a later partition swap works on Windows. A SELECT can
        still call read_parquet on any path (known limit of a local tool).
        """
        try:
            statements = duckdb.extract_statements(query)
        except duckdb.Error as exc:
            raise ValueError(f"cannot parse the query: {exc}") from exc
        if len(statements) != 1:
            raise ValueError(f"expected exactly one SELECT statement, got {len(statements)}")
        if statements[0].type != duckdb.StatementType.SELECT:
            raise ValueError("only a SELECT statement is allowed")
        con = self._lake.connect()
        try:
            return con.sql(query).to_arrow_table()
        finally:
            con.close()

    def _read(self, table: str, drive: str) -> pa.Table:
        return self._lake.read(table, drive).drop_columns([_DRIVE_COLUMN])
