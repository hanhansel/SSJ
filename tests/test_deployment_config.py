import tempfile
import unittest
from pathlib import Path

from controller.config import load_values
from controller.deployment import (
    build_postgresql_url,
    normalize_setup_values,
    normalize_smtp_values,
    prepare_initial_setup,
    validate_internal_port,
)


class DeploymentConfigurationTests(unittest.TestCase):
    def test_initial_setup_creates_managed_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / 'SSJData'
            config_path = data_root / 'config' / 'ssj.env'
            values = normalize_setup_values(
                data_root,
                '32425',
                'postgresql+psycopg2://ssj_app:test@127.0.0.1:5432/ssj_db',
            )

            prepare_initial_setup(values, config_path)

            self.assertTrue(config_path.is_file())
            self.assertTrue((data_root / 'uploads' / 'documents').is_dir())
            self.assertTrue((data_root / 'uploads' / 'print-templates').is_dir())
            self.assertTrue((data_root / 'backups').is_dir())
            # Plain HTTP only: no Caddy, certificate or Caddyfile anymore.
            self.assertFalse((data_root / 'caddy').exists())
            self.assertFalse((data_root / 'certificates').exists())
            self.assertFalse((data_root / 'config' / 'Caddyfile').exists())

            saved = load_values(config_path)
            self.assertEqual(saved['SSJ_INTERNAL_PORT'], '32425')
            self.assertEqual(saved['APP_ENV'], 'production')
            self.assertEqual(saved['TRUST_REVERSE_PROXY'], 'false')  # no proxy in front of Waitress
            for key in ('APP_HOSTNAME', 'HTTPS_ENABLED', 'TLS_MODE', 'DUCKDNS_API_TOKEN', 'ACME_EMAIL',
                        'CADDY_CONFIG_FILE'):
                self.assertNotIn(key, saved)
            self.assertEqual(
                Path(saved['BACKUP_FOLDER']).resolve(),
                (data_root / 'backups').resolve(),
            )
            self.assertTrue(bool(saved['SECRET_KEY']))

    def test_internal_port_rejects_ephemeral_range(self):
        with self.assertRaises(ValueError):
            validate_internal_port('55000')

    def test_postgresql_url_is_built_from_safe_user_facing_fields(self):
        url = build_postgresql_url('127.0.0.1', '5432', 'ssj_db', 'ssj_app', 'p@ss word/1')
        self.assertEqual(
            url,
            'postgresql+psycopg2://ssj_app:p%40ss%20word%2F1@127.0.0.1:5432/ssj_db',
        )
        with self.assertRaises(ValueError):
            build_postgresql_url('127.0.0.1', '5432', 'ssj_db', 'ssj_app', '')

    def test_smtp_is_required_and_allows_exactly_one_tls_mode(self):
        values = normalize_smtp_values(
            'smtp.example.com', '587', 'ssj@example.com', 'app-password',
            'ssj@example.com', 'SSJ', True, False,
        )
        self.assertEqual(values['SMTP_USE_TLS'], 'true')
        self.assertEqual(values['SMTP_USE_SSL'], 'false')
        with self.assertRaises(ValueError):
            normalize_smtp_values('', '587', 'ssj@example.com', 'secret', 'ssj@example.com')
        with self.assertRaises(ValueError):
            normalize_smtp_values(
                'smtp.example.com', '587', 'ssj@example.com', 'secret',
                'ssj@example.com', use_tls=True, use_ssl=True,
            )


if __name__ == '__main__':
    unittest.main()
