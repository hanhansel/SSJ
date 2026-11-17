"""Controller automation: backup schedule and retention, Task Scheduler definitions, server state, photo shrinking."""

import json
import logging
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine, text
from werkzeug.datastructures import FileStorage

from apps.core.uploads import MAX_PHOTO_SIDE, save_upload_file
from controller import backup, server_process, tasks
from controller.snapshot import SnapshotError

NS = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
NOW = datetime(2026, 10, 30, 9, 0)


def _parse(xml):
    return ET.fromstring(xml.encode('utf-16'))


class BackupScheduleTests(unittest.TestCase):
    def test_settings_are_validated(self):
        values = backup.normalize_backup_settings('monthly', '7:05', 1, 28, 1, True, r'D:\Backup SSJ')
        self.assertEqual(values['BACKUP_TIME'], '07:05')
        self.assertEqual(values['BACKUP_DAY'], '28')
        for arguments in (
            ('hourly', '12:00', 1, 1, 1, True, ''),       # unknown choice
            ('daily', '25:00', 1, 1, 1, True, ''),        # no such hour
            ('monthly', '12:00', 1, 31, 1, True, ''),     # not every month has day 31
            ('daily', '12:00', 1, 1, 1, True, r'\\nas\ssj'),  # tasks cannot open network shares
        ):
            with self.assertRaises(ValueError, msg=arguments):
                backup.normalize_backup_settings(*arguments)

    def test_retention_is_three_backup_intervals(self):
        self.assertEqual(backup.retention_cutoff('on_start', NOW), datetime(2026, 10, 27, 9, 0))
        self.assertEqual(backup.retention_cutoff('daily', NOW), datetime(2026, 10, 27, 9, 0))
        self.assertEqual(backup.retention_cutoff('weekly', NOW), datetime(2026, 10, 9, 9, 0))
        self.assertEqual(backup.retention_cutoff('monthly', NOW), datetime(2026, 7, 30, 9, 0))
        self.assertEqual(backup.retention_cutoff('yearly', NOW), datetime(2023, 10, 30, 9, 0))
        self.assertIsNone(backup.retention_cutoff('off', NOW))
        # 31 May minus 3 months has no 31st: the last day of February is used.
        self.assertEqual(backup.retention_cutoff('monthly', datetime(2026, 5, 31)), datetime(2026, 2, 28))

    def test_prune_deletes_only_expired_automatic_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            names = [
                'ssj_auto_2026-07-01_120000.ssjbak',       # older than 3 months: deleted
                'ssj_auto_2026-07-01_120000.ssjbak.tmp',   # leftover of a cut-off backup: deleted
                'ssj_auto_2026-08-15_120000.ssjbak',       # inside 3 months: kept
                'ssj_snapshot_2020-01-01_120000.ssjbak',   # manual: never deleted
                'ssj_pre-import_2020-01-01_120000.ssjbak',  # import safety copy: never deleted
                'notes.txt',
            ]
            for name in names:
                (Path(folder) / name).write_text('x')
            removed = backup.prune_auto_backups(folder, 'monthly', NOW)
            self.assertEqual(sorted(removed), names[:2])
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), sorted(names[2:]))

    def test_next_backup_time(self):
        def next_run(**values):
            return backup.next_backup_time({'BACKUP_TIME': '12:00', **values}, NOW)  # NOW is a Friday 09:00
        self.assertEqual(next_run(BACKUP_MODE='daily'), datetime(2026, 10, 30, 12, 0))
        self.assertEqual(next_run(BACKUP_MODE='daily', BACKUP_TIME='08:00'), datetime(2026, 10, 31, 8, 0))
        self.assertEqual(next_run(BACKUP_MODE='weekly', BACKUP_WEEKDAY='1'), datetime(2026, 11, 2, 12, 0))
        self.assertEqual(next_run(BACKUP_MODE='monthly', BACKUP_DAY='15'), datetime(2026, 11, 15, 12, 0))
        self.assertEqual(next_run(BACKUP_MODE='yearly', BACKUP_DAY='1', BACKUP_MONTH='3'), datetime(2027, 3, 1, 12, 0))
        self.assertIsNone(next_run(BACKUP_MODE='on_start'))


class TaskDefinitionTests(unittest.TestCase):
    def test_server_task_starts_at_boot_and_never_stops(self):
        task = _parse(tasks.server_task_xml('KANTOR\\admin'))
        self.assertEqual(task.find('.//t:BootTrigger/t:Delay', NS).text, 'PT1M')
        settings = task.find('t:Settings', NS)
        self.assertEqual(settings.find('t:ExecutionTimeLimit', NS).text, 'PT0S')  # not the default 3 days
        self.assertEqual(settings.find('t:DisallowStartIfOnBatteries', NS).text, 'false')
        self.assertEqual(settings.find('t:StopIfGoingOnBatteries', NS).text, 'false')
        self.assertEqual(settings.find('t:Priority', NS).text, '5')  # normal, not below-normal 7
        principal = task.find('.//t:Principal', NS)
        self.assertEqual(principal.find('t:UserId', NS).text, 'KANTOR\\admin')
        self.assertEqual(principal.find('t:LogonType', NS).text, 'S4U')
        self.assertTrue(task.find('.//t:Exec/t:Arguments', NS).text.endswith('--autostart'))

    def test_backup_task_triggers_follow_the_schedule(self):
        def trigger(**values):
            xml = tasks.backup_task_xml({'BACKUP_TIME': '07:30', 'BACKUP_RUN_MISSED': 'true', **values},
                                        'KANTOR\\admin', today=NOW.date())
            return _parse(xml)

        daily = trigger(BACKUP_MODE='daily')
        self.assertEqual(daily.find('.//t:StartBoundary', NS).text, '2026-10-30T07:30:00')
        self.assertIsNotNone(daily.find('.//t:ScheduleByDay', NS))
        self.assertEqual(daily.find('.//t:StartWhenAvailable', NS).text, 'true')
        self.assertEqual(daily.find('.//t:ExecutionTimeLimit', NS).text, 'PT2H')

        weekly = trigger(BACKUP_MODE='weekly', BACKUP_WEEKDAY='3')
        self.assertIsNotNone(weekly.find('.//t:DaysOfWeek/t:Wednesday', NS))

        monthly = trigger(BACKUP_MODE='monthly', BACKUP_DAY='15')
        self.assertEqual(monthly.find('.//t:DaysOfMonth/t:Day', NS).text, '15')
        self.assertEqual(len(monthly.find('.//t:Months', NS)), 12)

        yearly = trigger(BACKUP_MODE='yearly', BACKUP_DAY='1', BACKUP_MONTH='6', BACKUP_RUN_MISSED='false')
        self.assertEqual([m.tag.split('}')[1] for m in yearly.find('.//t:Months', NS)], ['June'])
        self.assertEqual(yearly.find('.//t:StartWhenAvailable', NS).text, 'false')

    def test_task_files_follow_the_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            original = os.environ.get('SSJ_ENV_FILE')
            os.environ['SSJ_ENV_FILE'] = str(Path(folder) / 'config' / 'ssj.env')
            try:
                values = {'AUTOSTART_ENABLED': 'true', 'BACKUP_MODE': 'daily', 'BACKUP_TIME': '12:00',
                          'SSJ_INTERNAL_PORT': '32425'}
                tasks.write_task_files(values, user='KANTOR\\admin')
                self.assertEqual(sorted(p.name for p in tasks.tasks_dir().iterdir()),
                                 ['backup.xml', 'port.txt', 'server.xml'])
                # A backup on server start needs no task; turning auto-start off removes its task.
                tasks.write_task_files({**values, 'AUTOSTART_ENABLED': 'false', 'BACKUP_MODE': 'on_start'},
                                       user='KANTOR\\admin')
                self.assertEqual([p.name for p in tasks.tasks_dir().iterdir()], ['port.txt'])
            finally:
                if original is None:
                    os.environ.pop('SSJ_ENV_FILE', None)
                else:
                    os.environ['SSJ_ENV_FILE'] = original


class ServerStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.values = {'SSJ_DATA_ROOT': self.tmp.name, 'LOG_ROOT': str(Path(self.tmp.name) / 'logs')}

    def tearDown(self):
        # A real backup is one short process; here the log file must be released before cleanup.
        logger = logging.getLogger('ssj.backup')
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        self.tmp.cleanup()

    def _state(self, pid):
        (server_process.run_dir(self.values) / 'server.json').write_text(json.dumps({'pid': pid}))

    def test_running_supervisor_is_found_and_a_dead_one_is_forgotten(self):
        self._state(os.getpid())
        self.assertTrue(server_process.is_running(self.values))
        self._state(999999)  # no such process
        self.assertFalse(server_process.is_running(self.values))
        self.assertFalse((server_process.run_dir(self.values) / 'server.json').exists())

    def test_backup_and_import_never_overlap(self):
        (server_process.run_dir(self.values) / 'snapshot.lock').write_text(str(os.getppid()))
        with self.assertRaises(SnapshotError):
            with backup.snapshot_lock(self.values):
                pass
        (server_process.run_dir(self.values) / 'snapshot.lock').write_text('999999')  # left by a dead process
        with backup.snapshot_lock(self.values):
            pass
        self.assertFalse((server_process.run_dir(self.values) / 'snapshot.lock').exists())

    def test_backup_run_writes_an_automatic_snapshot_and_prunes_old_ones(self):
        database = Path(self.tmp.name) / 'ssj.db'
        engine = create_engine(f'sqlite:///{database.as_posix()}')
        with engine.begin() as conn:
            conn.execute(text('CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)'))
            conn.execute(text("INSERT INTO notes (body) VALUES ('satu')"))
        engine.dispose()
        folder = Path(self.tmp.name) / 'backups'
        folder.mkdir()
        (folder / 'ssj_auto_2026-10-01_120000.ssjbak').write_text('old')
        values = {**self.values, 'DATABASE_URL': f'sqlite:///{database.as_posix()}', 'UPLOAD_FOLDER': '',
                  'BACKUP_MODE': 'daily', 'SNAPSHOT_FOLDER': str(folder)}

        self.assertEqual(backup.run_backup(values), 0)

        files = [p.name for p in folder.iterdir()]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith('ssj_auto_'))
        status = backup.read_status(values)
        self.assertTrue(status['ok'])
        self.assertEqual(status['removed'], ['ssj_auto_2026-10-01_120000.ssjbak'])

        self.assertEqual(backup.run_backup({**values, 'DATABASE_URL': ''}), 1)
        self.assertFalse(backup.read_status(values)['ok'])


class PhotoShrinkTests(unittest.TestCase):
    def _upload(self, data, name):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / name
            taken_at = save_upload_file(FileStorage(BytesIO(data), filename=name), str(path))
            return path.read_bytes(), taken_at

    def test_large_phone_photo_is_shrunk_turned_upright_and_stripped(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # stored sideways: viewers rotate it 90 degrees
        exif.get_ifd(0x8769)[0x9003] = '2026:10:29 09:12:30'
        exif.get_ifd(0x8825)[1] = 'S'  # GPS
        buffer = BytesIO()
        Image.effect_noise((4000, 3000), 60).convert('RGB').save(buffer, 'JPEG', quality=95, exif=exif)
        original = buffer.getvalue()
        with Image.open(BytesIO(original)) as image:
            self.assertIn(1, image.getexif().get_ifd(0x8825))  # the GPS tag really was there

        stored, taken_at = self._upload(original, 'foto.jpg')

        self.assertEqual(taken_at, datetime(2026, 10, 29, 9, 12, 30))
        self.assertLess(len(stored), len(original) // 3)
        with Image.open(BytesIO(stored)) as image:
            self.assertEqual(image.size, (MAX_PHOTO_SIDE * 3 // 4, MAX_PHOTO_SIDE))  # portrait, as seen on the phone
            self.assertEqual(dict(image.getexif()), {})

    def test_photo_without_metadata_and_other_files(self):
        pdf = b'%PDF-1.4 bukti HO'
        self.assertEqual(self._upload(pdf, 'ho.pdf'), (pdf, None))
        broken = b'\xff\xd8bukan-jpeg'
        self.assertEqual(self._upload(broken, 'foto.jpg'), (broken, None))
        buffer = BytesIO()
        Image.new('RGB', (40, 30), 'white').save(buffer, 'PNG')
        stored, taken_at = self._upload(buffer.getvalue(), 'ttd.png')
        self.assertIsNone(taken_at)
        with Image.open(BytesIO(stored)) as image:
            self.assertEqual(image.size, (40, 30))


if __name__ == '__main__':
    unittest.main()
