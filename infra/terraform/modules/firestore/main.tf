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

resource "google_firestore_database" "database" {
  project     = var.project_id
  name        = var.database_name
  location_id = var.location_id
  type        = var.database_type

  # Without this the provider default abandons the database on destroy: the
  # resource leaves Terraform state but the database stays in the project. Any
  # change that forces replacement (for example changing the region, which
  # feeds location_id) then fails with "Database already exists".
  deletion_policy = "DELETE"
}


