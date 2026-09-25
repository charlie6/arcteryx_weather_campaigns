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
"""Tests for the safe-by-default Firestore seed upload."""

import importlib.util
import pathlib
from typing import Any, Dict, Tuple

from google.api_core import exceptions as gexc
import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "infra" / "scripts" / "deployment" / "upload_config.py"
)
_spec = importlib.util.spec_from_file_location("upload_config", _SCRIPT)
upload_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(upload_config)


class _FakeDoc:
  """Minimal DocumentReference supporting set() and create()."""

  def __init__(self, store: Dict[Tuple[str, str], Any], key: Tuple[str, str]):
    self._store = store
    self._key = key

  def set(self, data: Any) -> None:
    self._store[self._key] = data

  def create(self, data: Any) -> None:
    if self._key in self._store:
      raise gexc.AlreadyExists("exists")
    self._store[self._key] = data


class _FakeCollection:

  def __init__(self, store: Dict[Tuple[str, str], Any], name: str):
    self._store = store
    self._name = name

  def document(self, doc_id: str) -> _FakeDoc:
    return _FakeDoc(self._store, (self._name, doc_id))


class _FakeDb:
  """In-memory stand-in for firestore.Client."""

  def __init__(self, store: Dict[Tuple[str, str], Any]):
    self.store = store

  def collection(self, name: str) -> _FakeCollection:
    return _FakeCollection(self.store, name)


_SEED = [
    {"collection_name": "Playbooks", "document_id": "weather_asset_groups",
     "data": {"v": "new"}},
    {"collection_name": "ClimateBaselines", "document_id": "Vancouver BC",
     "data": {"v": "new"}},
    {"collection_name": "GoogleAdsConfig", "document_id": "123",
     "data": {"v": "seed"}},
    {"collection_name": "CampaignBudgetState", "document_id": "123_456",
     "data": {"v": "seed"}},
    {"collection_name": "CustomerInstructions",
     "documents": [{"id": "123", "data": {"v": "seed"}}]},
]


@pytest.fixture
def live_db() -> _FakeDb:
  """A database that already holds live edits for every seeded document."""
  return _FakeDb({
      ("Playbooks", "weather_asset_groups"): {"v": "old"},
      ("ClimateBaselines", "Vancouver BC"): {"v": "old"},
      ("GoogleAdsConfig", "123"): {"v": "live"},
      ("CampaignBudgetState", "123_456"): {"v": "live"},
      ("CustomerInstructions", "123"): {"v": "live"},
  })


def test_safe_mode_keeps_operator_config_and_state(live_db):
  counts = upload_config.upload_batch(live_db, _SEED)
  assert live_db.store[("GoogleAdsConfig", "123")] == {"v": "live"}
  assert live_db.store[("CampaignBudgetState", "123_456")] == {"v": "live"}
  assert live_db.store[("CustomerInstructions", "123")] == {"v": "live"}
  assert counts == {"written": 2, "created": 0, "skipped": 3}


def test_safe_mode_always_refreshes_code_owned_collections(live_db):
  upload_config.upload_batch(live_db, _SEED)
  assert live_db.store[("Playbooks", "weather_asset_groups")] == {"v": "new"}
  assert live_db.store[("ClimateBaselines", "Vancouver BC")] == {"v": "new"}


def test_safe_mode_creates_missing_documents():
  db = _FakeDb({})
  counts = upload_config.upload_batch(db, _SEED)
  assert db.store[("GoogleAdsConfig", "123")] == {"v": "seed"}
  assert db.store[("CustomerInstructions", "123")] == {"v": "seed"}
  assert counts == {"written": 2, "created": 3, "skipped": 0}


def test_overwrite_all_replaces_everything(live_db):
  counts = upload_config.upload_batch(live_db, _SEED, overwrite_all=True)
  assert live_db.store[("GoogleAdsConfig", "123")] == {"v": "seed"}
  assert live_db.store[("CampaignBudgetState", "123_456")] == {"v": "seed"}
  assert counts == {"written": 5, "created": 0, "skipped": 0}


def test_unknown_collection_is_create_only(live_db):
  live_db.store[("SomethingNew", "x")] = {"v": "live"}
  upload_config.upload_batch(
      live_db,
      [{"collection_name": "SomethingNew", "document_id": "x",
        "data": {"v": "seed"}}],
  )
  assert live_db.store[("SomethingNew", "x")] == {"v": "live"}


def test_invalid_items_are_skipped():
  db = _FakeDb({})
  items = list(upload_config.iter_batch_items([
      {"collection_name": "GoogleAdsConfig", "data": {}},
      {"document_id": "1", "data": {}},
      {"collection_name": "C", "documents": [{"id": "a"}]},
  ]))
  assert items == []
  assert upload_config.upload_batch(db, []) == {
      "written": 0, "created": 0, "skipped": 0}


def test_main_passes_overwrite_flag(tmp_path, monkeypatch, live_db):
  seed = tmp_path / "seed.json"
  seed.write_text(
      '[{"collection_name": "GoogleAdsConfig", "document_id": "123",'
      ' "data": {"v": "seed"}}]'
  )
  monkeypatch.setattr(upload_config, "make_client", lambda *a, **k: live_db)
  base = ["--project_id", "p", "--database", "d", "--config", str(seed)]

  assert upload_config.main(base) == 0
  assert live_db.store[("GoogleAdsConfig", "123")] == {"v": "live"}

  assert upload_config.main(base + ["--overwrite_all"]) == 0
  assert live_db.store[("GoogleAdsConfig", "123")] == {"v": "seed"}
