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
"""Uploads a Firestore seed file.

A seed file is either a single JSON document (uploaded to --collection_name /
--document_id) or a list of items:

  {"collection_name": ..., "document_id": ..., "data": {...}}
  {"collection_name": ..., "documents": [{"id": ..., "data": {...}}, ...]}

Batch uploads are safe by default so that a redeploy never wipes live edits:

* Collections in OVERWRITE_COLLECTIONS (Playbooks, ClimateBaselines) are owned
  by the code release and are always replaced.
* Every other document (GoogleAdsConfig, CustomerInstructions, run state such
  as CampaignBudgetState and AssetGroupState, and any unknown collection) is
  created only if it does not exist yet. Existing documents are left untouched.

Pass --overwrite_all to replace every document in the seed (the old behaviour,
for example to reset a test account). A single-document upload always writes,
because the operator named the target explicitly.
"""

import argparse
import json
import logging
import os
import sys
from typing import Any, Iterable, Iterator, Optional, Tuple

from google.api_core import exceptions as gexc
from google.cloud import firestore
from google.oauth2 import credentials

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Collections whose content ships with the code and must track each release.
OVERWRITE_COLLECTIONS = frozenset({"Playbooks", "ClimateBaselines"})

# Outcomes returned by upload_document.
WRITTEN = "written"
CREATED = "created"
SKIPPED = "skipped"


def make_client(
    project_id: str, database: str, access_token: Optional[str] = None
) -> firestore.Client:
  """Builds a Firestore client.

  Args:
    project_id: Google Cloud project id.
    database: Firestore database name.
    access_token: Optional OAuth2 access token (for impersonation). Default
      credentials are used when omitted.

  Returns:
    A Firestore client.
  """
  if access_token:
    creds = credentials.Credentials(token=access_token)
    return firestore.Client(
        project=project_id, database=database, credentials=creds
    )
  return firestore.Client(project=project_id, database=database)


def upload_document(
    db: Any,
    collection_name: str,
    document_id: str,
    data: dict,
    overwrite: bool,
) -> str:
  """Writes one document, either replacing it or creating it only if missing.

  Args:
    db: Firestore client.
    collection_name: Target collection.
    document_id: Target document id.
    data: Document body.
    overwrite: True replaces the whole document. False creates it only if it
      does not exist (atomic, via DocumentReference.create).

  Returns:
    WRITTEN, CREATED or SKIPPED.
  """
  doc_ref = db.collection(collection_name).document(str(document_id))
  if overwrite:
    doc_ref.set(data)
    logger.info("Overwrote %s/%s", collection_name, document_id)
    return WRITTEN
  try:
    doc_ref.create(data)
  except gexc.AlreadyExists:
    logger.info(
        "Kept existing %s/%s (create-only; use --overwrite_all to replace)",
        collection_name,
        document_id,
    )
    return SKIPPED
  logger.info("Created %s/%s", collection_name, document_id)
  return CREATED


def should_overwrite(collection_name: str, overwrite_all: bool) -> bool:
  """Returns True if documents in this collection are replaced on upload."""
  return overwrite_all or collection_name in OVERWRITE_COLLECTIONS


def iter_batch_items(
    items: Iterable[dict],
) -> Iterator[Tuple[str, str, dict]]:
  """Flattens a seed list into (collection, document_id, data) tuples.

  Invalid items are logged and skipped.

  Args:
    items: The parsed seed list.

  Yields:
    (collection_name, document_id, data) for each valid document.
  """
  for item in items:
    collection = item.get("collection_name")
    if isinstance(item.get("documents"), list):
      for nested in item["documents"]:
        doc_id = nested.get("id") or nested.get("document_id")
        data = nested.get("data")
        if not collection or not doc_id or data is None:
          logger.warning(
              "Skipping invalid nested item in %s: %s",
              collection,
              list(nested.keys()),
          )
          continue
        yield collection, str(doc_id), data
      continue
    doc_id = item.get("document_id")
    data = item.get("data")
    if not collection or not doc_id or data is None:
      logger.warning("Skipping invalid batch item: %s", list(item.keys()))
      continue
    yield collection, str(doc_id), data


def upload_batch(
    db: Any, items: Iterable[dict], overwrite_all: bool = False
) -> dict:
  """Uploads a seed list using the safe-by-default policy.

  Args:
    db: Firestore client.
    items: The parsed seed list.
    overwrite_all: Replace every document, ignoring the create-only policy.

  Returns:
    Counts keyed by WRITTEN, CREATED and SKIPPED.
  """
  counts = {WRITTEN: 0, CREATED: 0, SKIPPED: 0}
  for collection, doc_id, data in iter_batch_items(items):
    outcome = upload_document(
        db,
        collection,
        doc_id,
        data,
        overwrite=should_overwrite(collection, overwrite_all),
    )
    counts[outcome] += 1
  logger.info(
      "Seed upload done: %d overwritten, %d created, %d kept existing",
      counts[WRITTEN],
      counts[CREATED],
      counts[SKIPPED],
  )
  return counts


def upload_config(
    project_id: str,
    database: str,
    collection_name: str,
    document_id: str,
    config_path: str,
    access_token: Optional[str] = None,
) -> None:
  """Uploads a single JSON document to Firestore, replacing it.

  Args:
    project_id: Google Cloud project id.
    database: Firestore database name.
    collection_name: Target collection.
    document_id: Target document id.
    config_path: Path to the JSON document.
    access_token: Optional OAuth2 access token.
  """
  with open(config_path, "r") as f:
    config_data = json.load(f)
  db = make_client(project_id, database, access_token)
  upload_document(db, collection_name, document_id, config_data, overwrite=True)


def main(argv: Optional[list] = None) -> int:
  """Command-line entry point.

  Args:
    argv: Optional argument list (defaults to sys.argv).

  Returns:
    Process exit code.
  """
  parser = argparse.ArgumentParser(
      description="Upload a Firestore seed file."
  )
  parser.add_argument("--project_id", required=True, help="Google Cloud project id")
  parser.add_argument("--database", required=True, help="Firestore database name")
  parser.add_argument(
      "--collection_name",
      default="GoogleAdsConfig",
      help="Collection for single-document mode",
  )
  parser.add_argument(
      "--document_id",
      default="4086619433",
      help="Document id for single-document mode",
  )
  parser.add_argument("--config", required=True, help="Path to the JSON seed file")
  parser.add_argument("--access_token", help="Optional OAuth2 access token")
  parser.add_argument(
      "--overwrite_all",
      action="store_true",
      help=(
          "Replace every document in a batch seed, including operator config"
          " and run state. Default: only Playbooks and ClimateBaselines are"
          " replaced; other documents are created only if missing."
      ),
  )
  args = parser.parse_args(argv)
  token = args.access_token or os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")

  try:
    with open(args.config, "r") as f:
      config_data = json.load(f)
    if isinstance(config_data, list):
      logger.info(
          "Batch seed %s (%s mode)",
          args.config,
          "overwrite-all" if args.overwrite_all else "safe",
      )
      db = make_client(args.project_id, args.database, token)
      upload_batch(db, config_data, overwrite_all=args.overwrite_all)
    else:
      upload_config(
          project_id=args.project_id,
          database=args.database,
          collection_name=args.collection_name,
          document_id=args.document_id,
          config_path=args.config,
          access_token=token,
      )
  except Exception as e:  # pylint: disable=broad-except
    logger.error("Execution failed: %s", e)
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
