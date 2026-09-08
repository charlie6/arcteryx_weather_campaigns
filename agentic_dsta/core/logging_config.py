# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Centralized structured logging configuration for Agentic DSTA."""

import datetime
import json
import logging
import os
import sys
from typing import Any, Dict, Optional


class FlushingStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes after every emit to ensure immediate Cloud Run delivery."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


class JsonFormatter(logging.Formatter):
    """Formats log records as JSON objects optimized for Google Cloud Logging."""

    # Map Python logging levels to Google Cloud Logging severities
    SEVERITY_MAP = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        """Formats a single LogRecord into a Cloud Logging compatible JSON string."""
        severity = self.SEVERITY_MAP.get(record.levelno, record.levelname.upper())

        # Base Cloud Logging payload
        log_record: Dict[str, Any] = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "severity": severity,
            "message": record.getMessage(),
            "logger_name": record.name,
            "logging.googleapis.com/sourceLocation": {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            },
        }

        # Include exception traceback if present
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        # Include stack info if present
        if record.stack_info:
            log_record["stack_info"] = self.formatStack(record.stack_info)

        # Standard LogRecord internal attributes to exclude from extra_fields
        excluded_keys = {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "taskName",
            "thread",
            "threadName",
        }

        # Collect user-supplied 'extra' dictionary items safely
        extra_fields = {}
        for key, value in record.__dict__.items():
            if key not in excluded_keys and key not in log_record:
                extra_fields[key] = value

        if extra_fields:
            log_record["extra"] = extra_fields

        try:
            return json.dumps(log_record, default=str)
        except Exception:
            # Fallback guarantee: never crash or drop a log line
            return json.dumps(
                {
                    "severity": severity,
                    "message": str(record.getMessage()),
                    "logger_name": record.name,
                },
                default=str,
            )


def setup_logging(level: Optional[str] = None) -> None:
    """Configures centralized structured logging for all application and library loggers.

    Args:
        level: Optional log level override (e.g. 'DEBUG', 'INFO'). Defaults to
          the LOG_LEVEL environment variable or 'INFO'.
    """
    # Ensure stdout/stderr are line-buffered so Cloud Run gets logs immediately
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
            sys.stderr.reconfigure(line_buffering=True)
        except Exception:
            pass

    log_level_str = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    numeric_level = getattr(logging, log_level_str, logging.INFO)

    # Create handler with flushing and structured JSON formatter
    handler = FlushingStreamHandler(sys.stdout)
    formatter = JsonFormatter()
    handler.setFormatter(formatter)
    handler.setLevel(numeric_level)

    # Configure root logger so all libraries (uvicorn, fastapi, google.adk, etc.) are captured
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Explicitly configure application logger hierarchy
    app_logger = logging.getLogger("agentic_dsta")
    app_logger.setLevel(numeric_level)
    app_logger.handlers.clear()
    app_logger.propagate = True

    # Ensure key third-party loggers propagate or have appropriate level
    for lib_name in [
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "fastapi",
        "google.adk",
        "google.genai",
        "google.ads",
    ]:
        lib_logger = logging.getLogger(lib_name)
        lib_logger.setLevel(numeric_level)
        lib_logger.handlers.clear()
        lib_logger.propagate = True

    app_logger.info(
        "Structured logging initialized for Agentic DSTA (level: %s, unbuffered: True)",
        log_level_str,
    )

