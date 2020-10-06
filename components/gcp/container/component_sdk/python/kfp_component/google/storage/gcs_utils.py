# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from google.cloud import storage
from ._common_ops import parse_blob_path

def upload_blob(source_file_path, destination_gcs_path):
    """Uploads a local file to a blob.

    Args:
        source_file_path (str): the source file path to be upload.
        destination_gcs_path (str): the gcs path to upload to.
    """
    bucket_name, blob_name = parse_blob_path(destination_gcs_path)
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(source_file_path)
