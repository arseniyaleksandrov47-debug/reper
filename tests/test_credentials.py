import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from real_estate_engine.credentials import load_dadata_key, load_nvidia_key, save_dadata_key, save_nvidia_key


class CredentialTests(unittest.TestCase):
    def test_environment_override_does_not_read_local_file(self):
        with patch.dict(os.environ,{'NVIDIA_API_KEY':'test-environment-key','NVIDIA_API_KEY_FILE':'missing-file'}), \
                patch('real_estate_engine.credentials._protect',side_effect=AssertionError('unexpected file access')):
            self.assertEqual(load_nvidia_key(),'test-environment-key')

    def test_missing_local_key_is_empty_without_using_old_credential(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'NVIDIA_API_KEY':'','NVIDIA_API_KEY_FILE':str(Path(temp)/'missing')}):
            self.assertEqual(load_nvidia_key(),'')

    @unittest.skipUnless(os.name=='nt','Windows DPAPI storage')
    def test_local_storage_is_encrypted_and_readable_by_current_user(self):
        value='nvapi-controlled-placeholder-not-a-service-key'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'nvidia.dpapi'
            save_nvidia_key(value,path)
            self.assertNotIn(value.encode(),path.read_bytes())
            with patch.dict(os.environ,{'NVIDIA_API_KEY':'','NVIDIA_API_KEY_FILE':str(path)}):
                self.assertEqual(load_nvidia_key(),value)

    @unittest.skipUnless(os.name=='nt','Windows DPAPI storage')
    def test_dadata_key_dpapi_and_resolver_priority(self):
        from real_estate_engine.fns import resolve_dadata_key
        value='0123456789abcdef0123456789abcdef01234567'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'dadata.dpapi'
            save_dadata_key(value,path)
            self.assertNotIn(value.encode(),path.read_bytes())
            with patch.dict(os.environ,{'DADATA_API_KEY':'','DADATA_API_KEY_FILE':str(path)}):
                self.assertEqual(load_dadata_key(),value)
                self.assertEqual(resolve_dadata_key(),value)
                self.assertEqual(resolve_dadata_key('explicit-dadata-key-override'),'explicit-dadata-key-override')
            with patch.dict(os.environ,{'DADATA_API_KEY':'env-dadata-key-override','DADATA_API_KEY_FILE':str(path)}):
                self.assertEqual(resolve_dadata_key(),'env-dadata-key-override')


if __name__=='__main__':unittest.main()
