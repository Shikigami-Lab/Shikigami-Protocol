import unittest
from unittest.mock import patch

from src.agent.container_identity import container_identity


class ContainerIdentityTests(unittest.TestCase):
    def test_posix_owner_is_preserved_without_assuming_user_1000(self):
        for uid, gid in ((501, 20), (1001, 1001), (0, 0)):
            with self.subTest(uid=uid), patch('os.getuid', return_value=uid, create=True), \
                    patch('os.getgid', return_value=gid, create=True):
                self.assertEqual(container_identity(), (uid, gid))

    def test_desktop_file_sharing_keeps_image_default_without_posix_ids(self):
        with patch('os.getuid', None, create=True), patch('os.getgid', None, create=True):
            self.assertEqual(container_identity(), (1000, 1000))


if __name__ == '__main__':
    unittest.main()
