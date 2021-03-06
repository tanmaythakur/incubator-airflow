# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import datetime
import unittest

from airflow import configuration, DAG
from airflow.contrib.operators import cloudml_operator_utils
from airflow.contrib.operators.cloudml_operator_utils import create_evaluate_ops
from airflow.exceptions import AirflowException

from mock import ANY
from mock import patch

DEFAULT_DATE = datetime.datetime(2017, 6, 6)


class CreateEvaluateOpsTest(unittest.TestCase):

    INPUT_MISSING_ORIGIN = {
        'dataFormat': 'TEXT',
        'inputPaths': ['gs://legal-bucket/fake-input-path/*'],
        'outputPath': 'gs://legal-bucket/fake-output-path',
        'region': 'us-east1',
    }
    SUCCESS_MESSAGE_MISSING_INPUT = {
        'jobId': 'eval_test_prediction',
        'predictionOutput': {
            'outputPath': 'gs://fake-output-path',
            'predictionCount': 5000,
            'errorCount': 0,
            'nodeHours': 2.78
        },
        'state': 'SUCCEEDED'
    }

    def setUp(self):
        super(CreateEvaluateOpsTest, self).setUp()
        configuration.load_test_config()
        self.dag = DAG(
            'test_dag',
            default_args={
                'owner': 'airflow',
                'start_date': DEFAULT_DATE,
                'end_date': DEFAULT_DATE,
            },
            schedule_interval='@daily')
        self.metric_fn = lambda x: (0.1,)
        self.metric_fn_encoded = cloudml_operator_utils.base64.b64encode(
            cloudml_operator_utils.dill.dumps(self.metric_fn, recurse=True))


    def testSuccessfulRun(self):
        input_with_model = self.INPUT_MISSING_ORIGIN.copy()
        input_with_model['modelName'] = (
            'projects/test-project/models/test_model')

        pred, summary, validate = create_evaluate_ops(
            task_prefix='eval-test',
            project_id='test-project',
            job_id='eval-test-prediction',
            region=input_with_model['region'],
            data_format=input_with_model['dataFormat'],
            input_paths=input_with_model['inputPaths'],
            prediction_path=input_with_model['outputPath'],
            model_name=input_with_model['modelName'].split('/')[-1],
            metric_fn_and_keys=(self.metric_fn, ['err']),
            validate_fn=(lambda x: 'err=%.1f' % x['err']),
            dataflow_options=None,
            dag=self.dag)

        with patch('airflow.contrib.operators.cloudml_operator.'
                   'CloudMLHook') as mock_cloudml_hook:

            success_message = self.SUCCESS_MESSAGE_MISSING_INPUT.copy()
            success_message['predictionInput'] = input_with_model
            hook_instance = mock_cloudml_hook.return_value
            hook_instance.create_job.return_value = success_message
            result = pred.execute(None)
            mock_cloudml_hook.assert_called_with('google_cloud_default', None)
            hook_instance.create_job.assert_called_once_with(
                'test-project',
                {
                    'jobId': 'eval_test_prediction',
                    'predictionInput': input_with_model
                }, ANY)
            self.assertEqual(success_message['predictionOutput'], result)

        with patch('airflow.contrib.operators.dataflow_operator.'
                   'DataFlowHook') as mock_dataflow_hook:

            hook_instance = mock_dataflow_hook.return_value
            hook_instance.start_python_dataflow.return_value = None
            summary.execute(None)
            mock_dataflow_hook.assert_called_with(
                gcp_conn_id='google_cloud_default', delegate_to=None)
            hook_instance.start_python_dataflow.assert_called_once_with(
                'eval-test-summary',
                {
                    'prediction_path': 'gs://legal-bucket/fake-output-path',
                    'metric_keys': 'err',
                    'metric_fn_encoded': self.metric_fn_encoded,
                },
                'airflow.contrib.operators.cloudml_prediction_summary',
                ['-m'])

        with patch('airflow.contrib.operators.cloudml_operator_utils.'
                   'GoogleCloudStorageHook') as mock_gcs_hook:

            hook_instance = mock_gcs_hook.return_value
            hook_instance.download.return_value = '{"err": 0.9, "count": 9}'
            result = validate.execute({})
            hook_instance.download.assert_called_once_with(
                'legal-bucket', 'fake-output-path/prediction.summary.json')
            self.assertEqual('err=0.9', result)

    def testFailures(self):
        input_with_model = self.INPUT_MISSING_ORIGIN.copy()
        input_with_model['modelName'] = (
            'projects/test-project/models/test_model')

        other_params_but_models = {
            'task_prefix': 'eval-test',
            'project_id': 'test-project',
            'job_id': 'eval-test-prediction',
            'region': input_with_model['region'],
            'data_format': input_with_model['dataFormat'],
            'input_paths': input_with_model['inputPaths'],
            'prediction_path': input_with_model['outputPath'],
            'metric_fn_and_keys': (self.metric_fn, ['err']),
            'validate_fn': (lambda x: 'err=%.1f' % x['err']),
            'dataflow_options': None,
            'dag': self.dag,
        }

        with self.assertRaisesRegexp(ValueError, 'Missing model origin'):
            _ = create_evaluate_ops(**other_params_but_models)

        with self.assertRaisesRegexp(ValueError, 'Ambiguous model origin'):
            _ = create_evaluate_ops(model_uri='abc', model_name='cde',
                                    **other_params_but_models)

        with self.assertRaisesRegexp(ValueError, 'Ambiguous model origin'):
            _ = create_evaluate_ops(model_uri='abc', version_name='vvv',
                                    **other_params_but_models)

        with self.assertRaisesRegexp(AirflowException,
                                     '`metric_fn` param must be callable'):
            params = other_params_but_models.copy()
            params['metric_fn_and_keys'] = (None, ['abc'])
            _ = create_evaluate_ops(model_uri='gs://blah', **params)

        with self.assertRaisesRegexp(AirflowException,
                                     '`validate_fn` param must be callable'):
            params = other_params_but_models.copy()
            params['validate_fn'] = None
            _ = create_evaluate_ops(model_uri='gs://blah', **params)


if __name__ == '__main__':
    unittest.main()
