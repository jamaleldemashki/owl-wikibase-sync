"""
Structured logging configuration.

Every log record is written both to the notebook cell output and to
``logs/pipeline.log``, tagged with the current run id so multiple runs'
history can be told apart in the shared log file. Credentials are never
passed to the logger -- see ``src/config.WikibaseCredentials.describe_safe``.
"""

from __future__ import annotations

import logging
from pathlib import Path


class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def configure_logging(log_dir: Path, run_id: str, level: int = logging.INFO) -> logging.Logger:
    """Configure the ``owl_wikibase_sync`` logger hierarchy for one run.

    Safe to call multiple times (e.g. once per notebook re-run of the setup
    cell): existing handlers on the root pipeline logger are replaced rather
    than duplicated.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "pipeline.log"

    logger = logging.getLogger("owl_wikibase_sync")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | run=%(run_id)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    run_id_filter = _RunIdFilter(run_id)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(run_id_filter)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(run_id_filter)
    logger.addHandler(file_handler)

    return logger
