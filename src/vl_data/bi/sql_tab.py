"""SQL tab of the BI app: one read-only SELECT over the lake views, with example queries.

Units: rows are counts; the example queries carry cm and cm/s in their column aliases.
"""

import pyarrow as pa
import streamlit as st

from vl_data.bi.data import SQL_ROWS_SHOWN, example_queries, read_problem, run_query
from vl_data.sdk.client import VLClient

_SELECT_KEY = "sql_example"  # session_state entry: the chosen example label (or None)
_TEXT_KEY = "sql_text"  # session_state entry: the text of the query box
_RUN_KEY = "sql_run"


def _fill(examples: dict[str, str]) -> None:
    """Callback of the example list: copy the chosen example into the query box."""
    label = st.session_state[_SELECT_KEY]
    if label is not None:
        st.session_state[_TEXT_KEY] = examples[label]


def render_sql_tab(client: VLClient, drive: str) -> None:
    """Draw the example list, the query box and the Run button; a click shows rows or a problem.

    One SELECT only, over the lake views keypoints, quarantine, lineage and motion. Examples
    are built for the selected drive on each run; the box text is kept when the drive changes.
    At most SQL_ROWS_SHOWN rows are shown. Never raises.
    """
    try:
        examples = example_queries(drive)
    except ValueError as error:
        st.error(read_problem(error))
        examples = {}
    st.session_state.setdefault(_TEXT_KEY, "")
    st.selectbox(
        "Example query",
        list(examples),
        index=None,
        placeholder="Choose an example",
        key=_SELECT_KEY,
        on_change=_fill,
        args=(examples,),
    )
    query = st.text_area("SQL (one SELECT only)", key=_TEXT_KEY, height=150)
    st.caption(
        "Local tool: one SELECT only; it reads the lake views keypoints, quarantine, lineage "
        "and motion."
    )
    if st.button("Run", key=_RUN_KEY):
        outcome = run_query(client, query)
        if outcome.problem is not None:
            st.error(outcome.problem)
        elif outcome.table is not None:
            try:
                st.dataframe(outcome.table, width="stretch", hide_index=True)
            except (ValueError, TypeError, KeyError, pa.ArrowException):
                st.error("The result cannot be shown as a table")  # no exception text
                return
            total = outcome.total_rows
            if total > SQL_ROWS_SHOWN:
                st.caption(f"showing {SQL_ROWS_SHOWN:,} of {total:,} rows")
            else:
                st.caption(f"{total:,} rows")
