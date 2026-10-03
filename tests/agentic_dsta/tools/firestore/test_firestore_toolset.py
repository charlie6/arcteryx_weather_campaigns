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

import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import os

from agentic_dsta.core import dry_run
from agentic_dsta.tools.firestore.firestore_toolset import FirestoreToolset

class TestFirestoreToolset(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_environ = patch.dict(os.environ, {
            "GOOGLE_CLOUD_PROJECT": "test_project",
            "FIRESTORE_DB": "dsta-agentic-firestore"
        })
        self.mock_environ.start()
        self.addCleanup(self.mock_environ.stop)

    def test_init(self):
        toolset = FirestoreToolset()
        self.assertEqual(toolset._project_id, "test_project")
        self.assertEqual(toolset._database_id, "dsta-agentic-firestore") # This line is correct

    @patch('agentic_dsta.tools.firestore.firestore_toolset.firestore.Client')
    def test_get_client(self, mock_client):
        toolset = FirestoreToolset(project_id="test_project")
        client = toolset._get_client()
        mock_client.assert_called_with(project="test_project", database="dsta-agentic-firestore") # This line is correct
        self.assertIsNotNone(client)

        # Test client reuse
        client2 = toolset._get_client()
        self.assertIs(client, client2)
        mock_client.assert_called_once()

    async def test_get_tools(self):
        toolset = FirestoreToolset()
        tools = await toolset.get_tools()
        self.assertEqual(len(tools), 5)

    @patch('agentic_dsta.tools.firestore.firestore_toolset.firestore.Client')
    def test_get_document_exists(self, mock_client):
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.id = "doc1"
        mock_doc.to_dict.return_value = {"key": "value"}

        mock_doc_ref = MagicMock()
        mock_doc_ref.get.return_value = mock_doc

        mock_coll_ref = MagicMock()
        mock_coll_ref.document.return_value = mock_doc_ref

        mock_client_instance = MagicMock()
        mock_client_instance.collection.return_value = mock_coll_ref
        mock_client.return_value = mock_client_instance

        toolset = FirestoreToolset()
        result = toolset.get_document("test_coll", "doc1")

        self.assertTrue(result["exists"])
        self.assertEqual(result["data"], {"key": "value"})

    @patch('agentic_dsta.tools.firestore.firestore_toolset.firestore.Client')
    def test_get_document_not_exists(self, mock_client):
        mock_doc = MagicMock()
        mock_doc.exists = False

        mock_doc_ref = MagicMock()
        mock_doc_ref.get.return_value = mock_doc

        mock_coll_ref = MagicMock()
        mock_coll_ref.document.return_value = mock_doc_ref

        mock_client_instance = MagicMock()
        mock_client_instance.collection.return_value = mock_coll_ref
        mock_client.return_value = mock_client_instance

        toolset = FirestoreToolset()
        result = toolset.get_document("test_coll", "doc1")

        self.assertFalse(result["exists"])

    @patch('agentic_dsta.tools.firestore.firestore_toolset.firestore.Client')
    def test_query_collection(self, mock_client):
        mock_doc = MagicMock()
        mock_doc.id = "doc1"
        mock_doc.to_dict.return_value = {"key": "value"}

        mock_query = MagicMock()
        # Mock chaining: query.limit(x) returns query
        mock_query.limit.return_value = mock_query
        mock_query.where.return_value = mock_query
        mock_query.stream.return_value = [mock_doc]

        mock_client_instance = MagicMock()
        mock_client_instance.collection.return_value = mock_query
        mock_client.return_value = mock_client_instance

        toolset = FirestoreToolset()
        result = toolset.query_collection("test_coll")

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["documents"][0]["id"], "doc1")

    @patch('agentic_dsta.tools.firestore.firestore_toolset.firestore.Client')
    def test_set_document(self, mock_client):
        mock_doc_ref = MagicMock()

        mock_coll_ref = MagicMock()
        mock_coll_ref.document.return_value = mock_doc_ref

        mock_client_instance = MagicMock()
        mock_client_instance.collection.return_value = mock_coll_ref
        mock_client.return_value = mock_client_instance

        toolset = FirestoreToolset()
        result = toolset.set_document("test_coll", "doc1", {"key": "value"})

        mock_doc_ref.set.assert_called_with({"key": "value"})
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "set")

    @patch('agentic_dsta.tools.firestore.firestore_toolset.firestore.Client')
    def test_delete_document(self, mock_client):
        mock_doc_ref = MagicMock()

        mock_coll_ref = MagicMock()
        mock_coll_ref.document.return_value = mock_doc_ref

        mock_client_instance = MagicMock()
        mock_client_instance.collection.return_value = mock_coll_ref
        mock_client.return_value = mock_client_instance

        toolset = FirestoreToolset()
        result = toolset.delete_document("test_coll", "doc1")

        mock_doc_ref.delete.assert_called_once()
        self.assertTrue(result["success"])

    @patch('agentic_dsta.tools.firestore.firestore_toolset.firestore.Client')
    def test_list_collections(self, mock_client):
        mock_coll_ref1 = MagicMock()
        mock_coll_ref1.id = "coll1"
        mock_coll_ref2 = MagicMock()
        mock_coll_ref2.id = "coll2"

        mock_client_instance = MagicMock()
        mock_client_instance.collections.return_value = [mock_coll_ref1, mock_coll_ref2]
        mock_client.return_value = mock_client_instance

        toolset = FirestoreToolset()
        result = toolset.list_collections()

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["collections"], ["coll1", "coll2"])


class TestChangeLogDryRunMode(unittest.TestCase):
    """ChangeLog rows written during a dry run must read 'log-only'."""

    def setUp(self):
        env = patch.dict(os.environ, {
            "GOOGLE_CLOUD_PROJECT": "test_project",
            "FIRESTORE_DB": "dsta-agentic-firestore",
            dry_run.DRY_RUN_ENV_VAR: "",
        })
        env.start()
        self.addCleanup(env.stop)
        client_patch = patch('agentic_dsta.tools.firestore.firestore_toolset.firestore.Client')
        client_cls = client_patch.start()
        self.addCleanup(client_patch.stop)
        self.doc_ref = client_cls.return_value.collection.return_value.document.return_value
        self.toolset = FirestoreToolset()

    def _written(self):
        """The data passed to the last Firestore set() call."""
        return self.doc_ref.set.call_args.args[0]

    def test_live_row_becomes_log_only_for_a_dry_run_campaign(self):
        row = {"campaignId": "24252893412", "mode": "live"}
        with dry_run.campaign_dry_run(True):
            self.toolset.set_document("ChangeLog", "run_24252893412", row)
        self.assertEqual(self._written()["mode"], "log-only")
        self.assertEqual(self._written()["campaignId"], "24252893412")
        self.assertEqual(row["mode"], "live", "the caller's dict must not be modified")

    def test_missing_or_blank_mode_becomes_log_only(self):
        for row in ({"campaignId": "1"}, {"campaignId": "1", "mode": ""}, {"campaignId": "1", "mode": " LIVE "}):
            with self.subTest(row=row), dry_run.campaign_dry_run(True):
                self.toolset.set_document("ChangeLog", "doc", row)
                self.assertEqual(self._written()["mode"], "log-only")

    def test_deployment_dry_run_also_marks_rows(self):
        with patch.dict(os.environ, {dry_run.DRY_RUN_ENV_VAR: "true"}):
            self.toolset.set_document("ChangeLog", "doc", {"mode": "live"})
        self.assertEqual(self._written()["mode"], "log-only")

    def test_other_modes_are_kept(self):
        with dry_run.campaign_dry_run(True):
            self.toolset.set_document("ChangeLog", "doc", {"mode": "skipped"})
        self.assertEqual(self._written()["mode"], "skipped")

    def test_merge_without_mode_is_untouched(self):
        with dry_run.campaign_dry_run(True):
            self.toolset.set_document("ChangeLog", "doc", {"notes": "x"}, merge=True)
        self.doc_ref.set.assert_called_with({"notes": "x"}, merge=True)

    def test_merge_with_live_mode_is_marked(self):
        with dry_run.campaign_dry_run(True):
            self.toolset.set_document("ChangeLog", "doc", {"mode": "live"}, merge=True)
        self.doc_ref.set.assert_called_with({"mode": "log-only"}, merge=True)

    def test_live_campaign_rows_are_untouched(self):
        with dry_run.campaign_dry_run(False):
            self.toolset.set_document("ChangeLog", "doc", {"mode": "live"})
        self.assertEqual(self._written()["mode"], "live")

    def test_other_collections_are_untouched(self):
        with dry_run.campaign_dry_run(True):
            self.toolset.set_document("CampaignBudgetState", "doc", {"mode": "live"})
        self.assertEqual(self._written()["mode"], "live")


if __name__ == '__main__':
    unittest.main()
