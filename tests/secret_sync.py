"""No provider calls: verify explicit sync targeting and honest failure states."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('sync', Path(__file__).resolve().parents[1] / 'scripts/sync-gsm-railway.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class SecretSync(unittest.TestCase):
    def test_environment_never_falls_back_or_ambiguously_matches(self):
        fixture = {'project': {'id': 'p', 'environments': {'edges': [
            {'node': {'id': 'production-id', 'name': 'production'}},
            {'node': {'id': 'staging-id', 'name': 'staging'}}]}}}
        with patch.object(sync, 'run_graphql', return_value=fixture):
            self.assertEqual(sync.resolve_railway_environment('token', 'p', 'staging-id'), ('staging-id', 'staging'))
            for name in (None, '', 'typo', 'Staging'):
                with self.subTest(name=name), self.assertRaises(RuntimeError):
                    sync.resolve_railway_environment('token', 'p', name)

    def test_discovers_all_secret_names(self):
        result = sync.subprocess.CompletedProcess([], 0, stdout='[{"name":"projects/g/secrets/A"},{"name":"projects/g/secrets/B"}]')
        with patch.object(sync.subprocess, 'run', return_value=result) as run:
            self.assertEqual(sync.list_gsm_secrets('g'), ['A', 'B'])
            self.assertIn('--project=g', run.call_args.args[0])
            self.assertNotIn('--filter', run.call_args.args[0])

    def test_discovery_failure_prevents_writes(self):
        env = {'GCP_PROJECT_ID': 'g', 'RAILWAY_TOKEN': 't', 'RAILWAY_PROJECT_ID': 'p', 'RAILWAY_ENVIRONMENT_ID': 'stage'}
        with patch.dict(os.environ, env, clear=True), patch.object(sync, 'resolve_railway_environment', return_value=('stage', 'staging')), patch.object(sync.subprocess, 'run', side_effect=sync.subprocess.CalledProcessError(1, 'gcloud')), patch.object(sync, 'upsert_railway_shared_variables') as write:
            with self.assertRaisesRegex(RuntimeError, 'Cannot list GSM secrets'):
                sync.main()
            write.assert_not_called()

    def test_unreadable_key_prevents_all_writes_without_alias_fallback(self):
        env = {'GCP_PROJECT_ID': 'g', 'RAILWAY_TOKEN': 't', 'RAILWAY_PROJECT_ID': 'p',
               'RAILWAY_ENVIRONMENT_ID': 'stage'}
        with patch.dict(os.environ, env, clear=True), patch.object(sync, 'resolve_railway_environment', return_value=('stage', 'staging')), patch.object(sync, 'list_gsm_secrets', return_value=['A', 'B']), patch.object(sync, 'fetch_gsm_secret', side_effect=['exact\n', None]) as fetch, patch.object(sync, 'upsert_railway_shared_variables') as write:
            with self.assertRaises(RuntimeError):
                sync.main()
            self.assertEqual(fetch.call_count, 2)
            write.assert_not_called()

    def test_discovered_values_preserve_exact_secret_bytes(self):
        env = {'GCP_PROJECT_ID': 'g', 'RAILWAY_TOKEN': 't', 'RAILWAY_PROJECT_ID': 'p',
               'RAILWAY_ENVIRONMENT_ID': 'stage'}
        with patch.dict(os.environ, env, clear=True), patch.object(sync, 'resolve_railway_environment', return_value=('stage', 'staging')), patch.object(sync, 'list_gsm_secrets', return_value=['A']), patch.object(sync, 'fetch_gsm_secret', return_value=' whitespace\n'), patch.object(sync, 'upsert_railway_shared_variables') as write:
            sync.main()
            write.assert_called_once_with('t', 'p', 'stage', {'A': ' whitespace\n'})

    def test_partial_single_writes_raise_instead_of_reporting_success(self):
        with patch.object(sync, 'run_graphql', side_effect=[RuntimeError('batch'), {}, RuntimeError('single')]):
            with self.assertRaisesRegex(RuntimeError, 'Incomplete sync: 1/2'):
                sync.upsert_railway_shared_variables('t', 'p', 'stage', {'A': 'a', 'B': 'b'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
