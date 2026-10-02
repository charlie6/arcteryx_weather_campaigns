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
"""Tests for the sync_config_sheet.py seed-account command (adding an account)."""

import importlib.util
import pathlib
from typing import Any, Dict, Optional, Tuple

from google.api_core import exceptions as gexc
import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "infra" / "scripts" / "config" / "sync_config_sheet.py"
)
_spec = importlib.util.spec_from_file_location("sync_config_sheet", _SCRIPT)
sync_config_sheet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_config_sheet)

SOURCE = "5341114500"
NEW = "1112223333"


class _FakeSnapshot:
  def __init__(self, data: Optional[Dict[str, Any]]):
    self._data = data
    self.exists = data is not None

  def to_dict(self) -> Optional[Dict[str, Any]]:
    return dict(self._data) if self._data is not None else None


class _FakeDoc:
  def __init__(self, store: Dict[Tuple[str, str], Any], key: Tuple[str, str]):
    self._store = store
    self._key = key

  def get(self) -> _FakeSnapshot:
    return _FakeSnapshot(self._store.get(self._key))

  def create(self, data: Dict[str, Any]) -> None:
    if self._key in self._store:
      raise gexc.AlreadyExists("exists")
    self._store[self._key] = dict(data)


class _FakeCollection:
  def __init__(self, store: Dict[Tuple[str, str], Any], name: str):
    self._store = store
    self._name = name

  def document(self, doc_id: str) -> _FakeDoc:
    return _FakeDoc(self._store, (self._name, doc_id))


class _FakeDb:
  def __init__(self, store: Dict[Tuple[str, str], Any]):
    self.store = store

  def collection(self, name: str) -> _FakeCollection:
    return _FakeCollection(self.store, name)


@pytest.fixture(name="db")
def _db(monkeypatch: pytest.MonkeyPatch) -> _FakeDb:
  db = _FakeDb({("CustomerInstructions", SOURCE): {"instruction": "Weather playbooks..."}})
  monkeypatch.setattr(sync_config_sheet, "_firestore_client", lambda project_id, database: db)
  monkeypatch.delenv("CONFIG_SHEET_ID", raising=False)  # seed-account needs no sheet
  return db


def test_seed_account_copies_instructions(db: _FakeDb) -> None:
  code = sync_config_sheet.main(
      ["seed-account", "--customer_id", "111-222-3333", "--from_customer_id", SOURCE]
  )
  assert code == 0
  assert db.store[("CustomerInstructions", NEW)] == {"instruction": "Weather playbooks..."}


def test_seed_account_leaves_an_existing_document(db: _FakeDb) -> None:
  db.store[("CustomerInstructions", NEW)] = {"instruction": "edited by hand"}
  code = sync_config_sheet.main(["seed-account", "--customer_id", NEW, "--from_customer_id", SOURCE])
  assert code == 0
  assert db.store[("CustomerInstructions", NEW)] == {"instruction": "edited by hand"}


def test_seed_account_fails_when_the_source_is_missing(db: _FakeDb) -> None:
  code = sync_config_sheet.main(["seed-account", "--customer_id", NEW, "--from_customer_id", "9998887777"])
  assert code == 1
  assert ("CustomerInstructions", NEW) not in db.store


def test_seed_account_requires_both_ids(db: _FakeDb) -> None:
  with pytest.raises(SystemExit) as exit_info:
    sync_config_sheet.main(["seed-account", "--customer_id", NEW])
  assert exit_info.value.code == 2
  assert ("CustomerInstructions", NEW) not in db.store
