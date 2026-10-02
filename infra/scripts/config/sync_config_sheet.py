#!/usr/bin/env python3
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
"""Command-line tool for the ADSTA configuration sheet.

Scheduled runs sync the sheet automatically when CONFIG_SHEET_ID is set on the
Cloud Run service. This tool is for setting the sheet up and for previewing
edits before the next run picks them up.

    cd agentic_dsta
    gcloud auth application-default login \\
      --scopes=https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/cloud-platform

    # 1. Fill a new (empty) spreadsheet from the live Firestore configuration.
    python3 infra/scripts/config/sync_config_sheet.py export \\
      --sheet_id SHEET_ID --project_id PROJECT --database DB

    # 2. Preview what the sheet would change. Read-only.
    python3 infra/scripts/config/sync_config_sheet.py plan \\
      --sheet_id SHEET_ID --project_id PROJECT --database DB

    # 3. Apply now instead of waiting for the next scheduled run.
    python3 infra/scripts/config/sync_config_sheet.py apply \\
      --sheet_id SHEET_ID --project_id PROJECT --database DB

    # Adding an account: copy a working account's CustomerInstructions
    # (create-only; no sheet needed). Runs abort without this document.
    python3 infra/scripts/config/sync_config_sheet.py seed-account \\
      --customer_id NEW_ID --from_customer_id EXISTING_ID --project_id PROJECT --database DB

``plan`` and ``apply`` read live Google Ads budgets, which needs the same Google
Ads credentials as the service (environment variables or Secret Manager). Pass
``--no_budgets`` to sync only Firestore configuration. ``apply`` honours
ADSTA_DRY_RUN for budget pushes. With ``--customer_id``, errors in other
accounts' rows are reported but do not block, exactly like a scheduled run.
"""

import argparse
import logging
import os
import pathlib
import sys
from typing import List, Optional

# Make the agentic_dsta package importable when run from the repository.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from agentic_dsta.config_sync import schema  # pylint: disable=wrong-import-position
from agentic_dsta.config_sync import sync  # pylint: disable=wrong-import-position

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sync_config_sheet")

_SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _sheets_service():
  """Builds a Sheets API client from Application Default Credentials."""
  import google.auth  # pylint: disable=import-outside-toplevel
  from googleapiclient.discovery import build  # pylint: disable=import-outside-toplevel

  credentials, _ = google.auth.default(scopes=_SHEETS_SCOPES)
  return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _firestore_client(project_id: Optional[str], database: Optional[str]):
  """Builds a Firestore client."""
  from google.cloud import firestore  # pylint: disable=import-outside-toplevel

  return firestore.Client(project=project_id, database=database)


def main(argv: Optional[List[str]] = None) -> int:
  """Command-line entry point.

  Args:
    argv: Optional argument list (defaults to sys.argv).

  Returns:
    Process exit code: 0 on success, 1 on failure or validation errors, 2 when
    a sync applied with conflicts or failed budget pushes.
  """
  parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
  parser.add_argument("command", choices=["export", "plan", "apply", "seed-account"])
  parser.add_argument(
      "--sheet_id",
      default=os.environ.get(sync.CONFIG_SHEET_ID_ENV),
      help="Spreadsheet id (from its URL). Defaults to $CONFIG_SHEET_ID.",
  )
  parser.add_argument(
      "--project_id", default=os.environ.get("GOOGLE_CLOUD_PROJECT"), help="Google Cloud project id"
  )
  parser.add_argument(
      "--database", default=os.environ.get("FIRESTORE_DB"), help="Firestore database name"
  )
  parser.add_argument(
      "--customer_id",
      help="plan/apply: limit accounts and campaigns to one customer. seed-account: the new account",
  )
  parser.add_argument(
      "--from_customer_id", help="seed-account: existing account whose CustomerInstructions are copied"
  )
  parser.add_argument(
      "--no_budgets", action="store_true", help="plan/apply: skip reading and pushing Google Ads budgets"
  )
  parser.add_argument(
      "--tabs",
      nargs="+",
      choices=[schema.ACCOUNTS_TAB, schema.CAMPAIGNS_TAB, schema.CITIES_TAB, schema.SYNC_LOG_TAB],
      help="export: only these tabs",
  )
  parser.add_argument("--force", action="store_true", help="export: overwrite tabs that already have data")
  args = parser.parse_args(argv)

  customer_id = args.customer_id.replace("-", "").strip() if args.customer_id else None
  if args.command == "seed-account":
    if not customer_id or not args.from_customer_id:
      parser.error("seed-account needs --customer_id (new account) and --from_customer_id (existing account)")
    try:
      db = _firestore_client(args.project_id, args.database)
      created = sync.copy_customer_instructions(db, args.from_customer_id, customer_id)
    except Exception as err:  # pylint: disable=broad-except
      logger.error("Failed: %s", err)
      return 1
    logger.info(
        "CustomerInstructions/%s %s", customer_id,
        "created" if created else "already exists (left unchanged)",
    )
    return 0

  if not args.sheet_id:
    parser.error("--sheet_id is required (or set CONFIG_SHEET_ID)")

  try:
    db = _firestore_client(args.project_id, args.database)
    service = _sheets_service()

    if args.command == "export":
      rows = sync.build_export_rows(db, {customer_id} if customer_id else None)
      written = sync.export_to_sheet(service, args.sheet_id, rows, tabs=args.tabs, force=args.force)
      for tab in written:
        logger.info("Wrote %s (%d data rows)", tab, len(rows[tab]) - 1)
      return 0

    budgets = None if args.no_budgets else sync.GoogleAdsBudgetGateway()
    plan = sync.plan_from_sheet(service, args.sheet_id, db, budgets, customer_id)
    print(plan.describe())
    if args.command == "plan":
      return 1 if plan.errors else 0

    result = sync.apply_plan(plan, db, budgets, source="cli")
    sync.log_result(result, customer_id)
    sync.append_sync_log(service, args.sheet_id, result, customer_id=customer_id)
    print(
        f"Outcome: {result.outcome}; {result.changes} config change(s), "
        f"{result.budget_pushes} budget push(es), {result.conflicts} conflict(s)."
    )
    if result.outcome == sync.OUTCOME_INVALID:
      return 1
    return 2 if result.needs_attention else 0
  except Exception as err:  # pylint: disable=broad-except
    logger.error("Failed: %s", err)
    return 1


if __name__ == "__main__":
  sys.exit(main())
