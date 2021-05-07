# Copyright 2021 The Kubeflow Authors
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
"""Module for AIPlatformPipelines API client."""

import datetime
import json
import re
import subprocess
from typing import Any, Dict, List, Mapping, Optional

from absl import logging
from google import auth
from google.auth import exceptions
from google.oauth2 import credentials
from google.protobuf import json_format
#from googleapiclient import discovery

from kfp.v2.google.client import client_utils
from kfp.v2.google.client import runtime_config_builder
from kfp.v2.google.client.schedule import create_from_pipeline_file

from google.cloud import aiplatform
from google.cloud import aiplatform_v1beta1
from google.cloud.aiplatform_v1beta1.types import pipeline_job as gca_pipeline_job


# AIPlatformPipelines API endpoint.
DEFAULT_ENDPOINT_FORMAT = '{region}-aiplatform.googleapis.com'

# AIPlatformPipelines API version.
DEFAULT_API_VERSION = 'v1beta1'

# If application default credential does not exist, we fall back to whatever
# previously provided to `gcloud auth`. One can run `gcloud auth login` to
# provide identity for this token.
_AUTH_ARGS = ('gcloud', 'auth', 'print-access-token')

# AIPlatformPipelines service API job name relative name prefix pattern.
_JOB_NAME_PATTERN = '{parent}/pipelineJobs/{job_id}'

# Pattern for valid names used as a uCAIP resource name.
_VALID_NAME_PATTERN = re.compile('^[a-z][-a-z0-9]{0,127}$')

# Cloud Console UI link of a pipeline run.
UI_PIPELINE_JOB_LINK_FORMAT = (
    'https://console.cloud.google.com/ai/platform/locations/{region}/pipelines/'
    'runs/{job_id}?project={project_id}')

# Display UI link HTML snippet
UI_LINK_HTML_FORMAT = (
    'See the Pipeline job <a href="{}" target="_blank" >here</a>.')


class _AccessTokenCredentials(credentials.Credentials):
  """Credential class to provide token-based authentication."""

  def __init__(self, token):
    super().__init__(token=token)

  def refresh(self, request):
    # Overrides refresh method in the base class because by default token is
    # not refreshable.
    pass


def _get_gcp_credential() -> Optional[credentials.Credentials]:
  """Returns the GCP OAuth2 credential.

  Returns:
    The credential. By default the function returns the current Application
    Default Credentials, which is located at $GOOGLE_APPLICATION_CREDENTIALS. If
    not set, this function returns the current login credential, whose token is
    created by running 'gcloud auth login'.
    For more information, see
    https://cloud.google.com/sdk/gcloud/reference/auth/application-default/print-access-token
  """
  result = None
  try:
    result, _ = auth.default()
  except exceptions.DefaultCredentialsError as e:
    logging.warning(
        'Failed to get GCP access token for application '
        'default credential: (%s). Using end user credential.', e)
    try:
      token = subprocess.check_output(_AUTH_ARGS).rstrip().decode('utf-8')
      result = _AccessTokenCredentials(token=token)
    except subprocess.CalledProcessError as e:
      # _AccessTokenCredentials won't throw CalledProcessError, so this
      # exception implies that the subprocess call is failed.
      logging.warning('Failed to get GCP access token: %s', e)
  else:
    return result


def _get_current_time() -> datetime.datetime:
  """Gets the current timestamp."""
  return datetime.datetime.now()


def _is_ipython() -> bool:
  """Returns whether we are running in notebook."""
  try:
    import IPython  # pylint: disable=g-import-not-at-top
    ipy = IPython.get_ipython()
    if ipy is None:
      return False
  except ImportError:
    return False

  return True


def _extract_job_id(job_name: str) -> Optional[str]:
  """Extracts job id from job name.

  Args:
   job_name: The full job name.

  Returns:
   The job id or None if no match found.
  """
  p = re.compile(
      'projects/(?P<project_id>.*)/locations/(?P<region>.*)/pipelineJobs/(?P<job_id>.*)'
  )
  result = p.search(job_name)
  return result.group('job_id') if result else None


def _set_enable_caching_value(pipeline_spec: Dict[str, Any],
                              enable_caching: bool) -> None:
  """Sets pipeline tasks caching options.

  Args:
   pipeline_spec: The dictionary of pipeline spec.
   enable_caching: Whether to enable caching.
  """
  for component in [pipeline_spec['root']] + list(
      pipeline_spec['components'].values()):
    if 'dag' in component:
      for task in component['dag']['tasks'].values():
        task['cachingOptions'] = {'enableCache': enable_caching}


class AIPlatformClient(object):
  """AIPlatformPipelines Unified API Client."""

  def __init__(
      self,
      project_id: str,
      region: str,
  ):
    """Constructs an AIPlatformPipelines API client.

    Args:
      project_id: GCP project ID.
      region: GCP project region.
      endpoint: AIPlatformPipelines service endpoint. Defaults to
        'us-central1-aiplatform.googleapis.com'.
    """
    if not project_id:
      raise ValueError('A valid GCP project ID is required to run a pipeline.')
    if not region:
      raise ValueError('A valid GCP region is required to run a pipeline.')

    self._project_id = project_id
    self._region = region
    self._parent = 'projects/{}/locations/{}'.format(project_id, region)

    client_options = aiplatform.initializer.global_config.get_client_options()
    #parent = aiplatform.initializer.global_config.common_location_path()

    self._client = aiplatform_v1beta1.PipelineServiceClient(
        client_options=client_options,
        credentials=_get_gcp_credential(),
    )

  def _display_job_link(self, job_id: str):
    """Display an link to UI."""
    url = UI_PIPELINE_JOB_LINK_FORMAT.format(
        region=self._region, job_id=job_id, project_id=self._project_id)
    if _is_ipython():
      import IPython  # pylint: disable=g-import-not-at-top
      html = UI_LINK_HTML_FORMAT.format(url)
      IPython.display.display(IPython.display.HTML(html))
    else:
      print('See the Pipeline job here:', url)

  def _submit_job(self,
                  pipeline_job: gca_pipeline_job.PipelineJob,
                  job_id: Optional[str] = None) -> gca_pipeline_job.PipelineJob:
    """Submits a pipeline job to run on AIPlatformPipelines service.

    Args:
      pipeline_job: An instance of an AIPlatform PipelineJob.
      job_id: Optional user-specified ID of this pipelineJob. If not provided,
        pipeline service will automatically generate a random numeric ID.

    Returns:
      The service returned PipelineJob instance.

    Raises:
      RuntimeError: If AIPlatformPipelines service returns unexpected response
      or empty job name.
    """

    #logging.info('Compiled PipelineJob request: %s', pipeline_job)
    response = self._client.create_pipeline_job(parent=self._parent,
                                               pipeline_job=pipeline_job,
                                               pipeline_job_id=job_id)
    #logging.info('Received server response: %s', response)
    if not response or not response.name:
      raise RuntimeError('Unexpected response received. Response: %s' %
                         response)
    logging.info('Execution triggered. Job name: %s', response.name)
    job_id = _extract_job_id(response.name)
    if job_id:
      self._display_job_link(job_id)
    return response

  def get_job(self, job_id: Optional[str] = None) -> gca_pipeline_job.PipelineJob:
    """Gets an existing pipeline job on AIPlatformPipelines service.

    Args:
      job_id: The relative ID of this pipelineJob. The full qualified name will
        generated according to the project ID and region specified for the
        client.

    Returns:
      The service returned PipelineJob instance.
    """
    full_name = _JOB_NAME_PATTERN.format(
        parent=self._parent, job_id=job_id)
    return self._client.get_pipeline_job(name=full_name)

  def list_jobs(self) -> List[gca_pipeline_job.PipelineJob]:
    """Gets an existing pipeline job on AIPlatformPipelines service.

    Returns:
      The list of PipelineJob instances.
    """
    return self._client.list_pipeline_jobs(parent=self._parent)

  def create_run_from_job_spec(
      self,
      job_spec_path: str,
      job_id: Optional[str] = None,
      pipeline_root: Optional[str] = None,
      parameter_values: Optional[Mapping[str, Any]] = None,
      enable_caching: bool = True,
      cmek: Optional[str] = None,
      service_account: Optional[str] = None,
      network: Optional[str] = None,
      labels: Optional[Mapping[str, str]] = None) -> str:
    """Runs a pre-compiled pipeline job on AIPlatformPipelines service.

    Args:
      job_spec_path: The path of PipelineJob JSON file. It can be a local path
        or a GS URI.
      job_id: Optionally, the user can provide the unique ID of the job run. If
        not specified, pipeline name + timestamp will be used.
      pipeline_root: Optionally the user can override the pipeline root
        specified during the compile time.
      parameter_values: The mapping from runtime parameter names to its values.
      enable_caching: Whether to turn on caching for the run. Defaults to True.
      cmek: The customer-managed encryption key for a pipelineJob. If set, the
        pipeline job and all of its sub-resources will be secured by this key.
      service_account: The service account that the pipeline workload runs as.
      network: The network configuration applied for pipeline jobs. If left
        unspecified, the workload is not peered with any network.
      labels: The user defined metadata to organize PipelineJob.

    Returns:
      Full AIPlatformPipelines job name.

    Raises:
      ParseError: On JSON parsing problems.
      RuntimeError: If AIPlatformPipelines service returns unexpected response
      or empty job name.
    """
    job_spec = client_utils.load_json(job_spec_path)
    pipeline_name = job_spec['pipelineSpec']['pipelineInfo']['name']
    job_id = job_id or '{pipeline_name}-{timestamp}'.format(
        pipeline_name=re.sub('[^-0-9a-z]+', '-',
                             pipeline_name.lower()).lstrip('-').rstrip('-'),
        timestamp=_get_current_time().strftime('%Y%m%d%H%M%S'))
    if not _VALID_NAME_PATTERN.match(job_id):
      raise ValueError(
          'Generated job ID: {} is illegal as a uCAIP pipelines job ID. '
          'Expecting an ID following the regex pattern '
          '"[a-z][-a-z0-9]{{0,127}}"'.format(job_id))

    job_name = _JOB_NAME_PATTERN.format(
        parent=self._parent, job_id=job_id)

    builder = runtime_config_builder.RuntimeConfigBuilder.from_job_spec_json(
        job_spec)
    builder.update_pipeline_root(pipeline_root)
    builder.update_runtime_parameters(parameter_values)

    runtime_config_dict = builder.build()
    runtime_config = gca_pipeline_job.PipelineJob.RuntimeConfig()._pb
    json_format.ParseDict(runtime_config_dict, runtime_config)

    _set_enable_caching_value(job_spec['pipelineSpec'], enable_caching)

    pipeline_job = gca_pipeline_job.PipelineJob(
        name=job_name,
        display_name=job_id,
        pipeline_spec=job_spec['pipelineSpec'],
        runtime_config=runtime_config
    )

    if cmek is not None:
      pipeline_job.encryption_spec = {'kms_key_name': cmek}
    if service_account is not None:
      pipeline_job.service_account = service_account
    if network is not None:
      pipeline_job.network = network

    if labels:
      if not isinstance(labels, Mapping):
        raise ValueError(
            'Expect labels to be a mapping of string key value pairs. '
            'Got "{}" of type "{}"'.format(labels, type(labels)))
      for k, v in labels.items():
        if not isinstance(k, str) or not isinstance(v, str):
          raise ValueError(
              'Expect labels to be a mapping of string key value pairs. '
              'Got "{}".'.format(labels))

      pipeline_job.labels = labels

    return self._submit_job(pipeline_job=pipeline_job, job_id=job_id)

  def create_schedule_from_job_spec(
      self,
      job_spec_path: str,
      schedule: str,
      time_zone: str = 'US/Pacific',
      pipeline_root: Optional[str] = None,
      parameter_values: Optional[Mapping[str, Any]] = None) -> dict:
    """Creates schedule for compiled pipeline file.

    This function creates scheduled job which will run the provided pipeline on
    schedule. This is implemented by creating a Google Cloud Scheduler Job.
    The job will be visible in https://console.google.com/cloudscheduler and can
    be paused/resumed and deleted.

    To make the system work, this function also creates a Google Cloud Function
    which acts as an intermediare between the Scheduler and Pipelines. A single
    function is shared between all scheduled jobs.
    The following APIs will be activated automatically:
    * cloudfunctions.googleapis.com
    * cloudscheduler.googleapis.com
    * appengine.googleapis.com

    Args:
      job_spec_path: Path of the compiled pipeline file.
      schedule: Schedule in cron format. Example: "45 * * * *"
      time_zone: Schedule time zone. Default is 'US/Pacific'
      parameter_values: Arguments for the pipeline parameters
      pipeline_root: Optionally the user can override the pipeline root
        specified during the compile time.

    Returns:
      Created Google Cloud Scheduler Job object dictionary.
    """
    return create_from_pipeline_file(
        pipeline_path=job_spec_path,
        schedule=schedule,
        project_id=self._project_id,
        region=self._region,
        time_zone=time_zone,
        parameter_values=parameter_values,
        pipeline_root=pipeline_root)
