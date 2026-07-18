"""Guarded BigQuery access layer.

Adapted from the starter ``bq_client.py`` with the guards the design requires
(docs/ARCHITECTURE.md §4.5): a dry-run gate for free syntax/cost checking, a
hard ``maximum_bytes_billed`` cap on execution, and a row cap. Results are
plain lists of dicts rather than DataFrames — they feed the PII mask and the
LLM context directly, and stay JSON-serializable.
"""

import datetime
import decimal
import logging
from dataclasses import dataclass
from typing import Any, Optional

from google.cloud import bigquery

from . import config

logger = logging.getLogger(__name__)


class QueryFailed(Exception):
    """Raised for any query problem; message is fed back to the LLM for self-correction."""


@dataclass
class QueryResult:
    rows: list[dict[str, Any]]
    total_rows: int
    truncated: bool
    bytes_processed: int

    @property
    def empty(self) -> bool:
        return self.total_rows == 0


def _plain(value: Any) -> Any:
    """Convert BigQuery cell values to JSON-friendly Python types."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class BigQueryRunner:
    """A lean BigQuery client for executing SQL queries with cost/size guards."""

    def __init__(
        self,
        project_id: Optional[str] = None,
        dataset_id: str = config.DATASET_ID,
        max_bytes_billed: int = config.MAX_BYTES_BILLED,
        max_rows: int = config.MAX_RESULT_ROWS,
    ) -> None:
        logger.info("Initializing BigQuery client")
        self.client = bigquery.Client(project=project_id or config.GOOGLE_CLOUD_PROJECT or None)
        self.dataset_id = dataset_id
        self.max_bytes_billed = max_bytes_billed
        self.max_rows = max_rows

    def dry_run(self, sql_query: str) -> int:
        """Validate syntax and estimate cost without running the query (free).

        Returns the bytes-scanned estimate. Raises QueryFailed with BigQuery's
        error message (used as self-correction feedback) or with a budget
        message if the estimate exceeds the byte budget.
        """
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        try:
            job = self.client.query(sql_query, job_config=job_config)
        except Exception as e:  # noqa: BLE001 - error text goes back to the LLM
            raise QueryFailed(f"Query is invalid: {e}") from e
        estimate = job.total_bytes_processed or 0
        if estimate > self.max_bytes_billed:
            raise QueryFailed(
                f"Query would scan ~{estimate / 1024**3:.2f} GiB, over the "
                f"{self.max_bytes_billed / 1024**3:.2f} GiB budget. Narrow it: select "
                "fewer columns, filter on partitioned/clustered fields, or pre-aggregate."
            )
        return estimate

    def execute(self, sql_query: str) -> QueryResult:
        """Execute a query with a hard billing cap; dry-run first for free validation."""
        estimate = self.dry_run(sql_query)
        job_config = bigquery.QueryJobConfig(maximum_bytes_billed=self.max_bytes_billed)
        try:
            logger.info("Executing BigQuery query (est. %d bytes)", estimate)
            iterator = self.client.query(sql_query, job_config=job_config).result()
        except Exception as e:  # noqa: BLE001
            raise QueryFailed(f"Query failed at execution: {e}") from e
        rows: list[dict[str, Any]] = []
        for row in iterator:
            if len(rows) >= self.max_rows:
                break
            rows.append({k: _plain(v) for k, v in row.items()})
        total = iterator.total_rows if iterator.total_rows is not None else len(rows)
        logger.info("Query returned %d rows (%d kept)", total, len(rows))
        return QueryResult(
            rows=rows,
            total_rows=total,
            truncated=total > len(rows),
            bytes_processed=estimate,
        )

    def get_table_schema(self, table_name: str) -> list[dict[str, Any]]:
        """Schema info for one table (kept from the starter client)."""
        table = self.client.get_table(f"{self.dataset_id}.{table_name}")
        return [
            {
                "name": f.name,
                "type": f.field_type,
                "mode": f.mode,
                "description": f.description or "",
            }
            for f in table.schema
        ]

    def schema_context(self) -> str:
        """DDL-style description of all required tables, for the SQL-generation prompt."""
        blocks = []
        for table in config.TABLES:
            fields = self.get_table_schema(table)
            pii = config.PII_COLUMNS.get(table, set())
            lines = [f"CREATE TABLE `{self.dataset_id}.{table}` ("]
            for f in fields:
                note = " -- PII: never select" if f["name"] in pii else ""
                desc = f" -- {f['description']}" if f["description"] else ""
                lines.append(f"  {f['name']} {f['type']},{note}{desc}")
            lines.append(");")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
