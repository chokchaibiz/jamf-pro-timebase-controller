#!/usr/bin/env python3
"""Offline integration checks for partial attendance resolution."""
import json
import logging
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harrow_timebase import TimeBaseController, load_config
from timebase.controller.types import AttendanceError, ConfigError
from timebase.importer.handlers import JobContext, handle_attendance

class UnmatchedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.c = TimeBaseController.__new__(TimeBaseController)
        self.c.cfg = json.loads((ROOT/'config.production.json').read_text())
        self.c.cfg['attendance'].pop('unmatched_email_policy')
        self.c.cfg['paths'].update(attendance_dir=str(self.base), state_dir=str(self.base))
        self.c.cfg['safety']['max_absent_fraction'] = 1
        self.c.logger = logging.getLogger('unmatched-test')
        self.c.dry_run = False
        self.c.now = lambda: datetime(2026,9,11,9,30,tzinfo=ZoneInfo('Asia/Bangkok'))
        self.c._master_inventory_email_index = Mock(return_value={'one@example.com': {'A'}})
        self.master = {'A','B','C'}
        self.path = self.c.attendance_path()
        self.path.write_text('email_address\none@example.com\ntwo@example.com\nthree@example.com\n')

    def test_direct_partial_and_inventory_recovery(self):
        with self.assertLogs('unmatched-test', level='WARNING') as logs:
            self.assertEqual(self.c.read_absent_serials(self.master)[0], {'A'})
        self.assertIn('skipped_emails=2', '\n'.join(logs.output))
        self.c._master_inventory_email_index.return_value['two@example.com'] = {'B'}
        self.assertEqual(self.c.read_absent_serials(self.master)[0], {'A','B'})
        self.assertEqual(self.c._master_inventory_email_index.call_count, 2)
        self.c.cfg['attendance']['unmatched_email_policy'] = 'error'
        with self.assertRaises(AttendanceError): self.c.read_absent_serials(self.master)

    def test_zero_matches(self):
        self.c._master_inventory_email_index.return_value = {}
        with self.assertLogs('unmatched-test', level='WARNING') as logs:
            self.assertEqual(self.c.read_absent_serials(self.master)[0], set())
        self.assertIn('No attendance emails matched', '\n'.join(logs.output))

    def test_ambiguous_and_safety_still_block(self):
        self.c._master_inventory_email_index.return_value = {'one@example.com': {'A','B'}}
        with self.assertRaises(AttendanceError): self.c.read_absent_serials(self.master)
        self.c._master_inventory_email_index.return_value = {'one@example.com': {'A'}}
        self.c.cfg['safety']['max_absent_fraction'] = .1
        with self.assertRaises(AttendanceError): self.c.read_absent_serials(self.master)
        self.path.write_text('email_address\ninvalid\n')
        with self.assertRaises(AttendanceError): self.c.read_absent_serials(self.master)

    def test_full_resolution_cache(self):
        self.path.write_text('email_address\none@example.com\n')
        self.c.read_absent_serials(self.master)
        self.c.read_absent_serials(self.master)
        self.c._master_inventory_email_index.assert_called_once()

    def test_portal_partial_preserves_csv_and_triggers_reconcile(self):
        staged = self.base/'upload.csv'
        staged.write_bytes(self.path.read_bytes())
        self.c.preflight = Mock()
        self.c.master_members = Mock(return_value=self.master)
        self.c.is_school_day = Mock(return_value=(True,'school day'))
        ctx = JobContext(job={'action':'upload','attendance_date':'2026-09-11','staged_filename':staged.name},
            base={'job_id':'test'}, cfg=self.c.cfg, portal_cfg={}, logger=self.c.logger,
            started=self.c.now(), staging_dir=self.base, archive_dir=self.base/'archive')
        with patch('timebase.importer.handlers.TimeBaseController', return_value=self.c), patch(
            'timebase.importer.handlers.run_controller', return_value=Mock(returncode=0)) as run:
            result = handle_attendance(ctx)
        run.assert_called_once_with(['reconcile'])
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['matched_email_count'], 1)
        self.assertEqual(result['skipped_email_count'], 2)
        self.assertEqual(result['absent_device_count'], 1)
        self.assertIn('three@example.com', self.path.read_text())
        self.assertIsNone(self.c.load_attendance_resolution_cache(self.master, self.path, self.c.file_sha256(self.path)))

    def test_portal_strict_failure_preserves_existing_file(self):
        staged = self.base/'upload.csv'
        staged.write_text('email_address\nunknown@example.com\n')
        before = self.path.read_bytes()
        self.c.cfg['attendance']['unmatched_email_policy'] = 'error'
        self.c.preflight = Mock()
        self.c.master_members = Mock(return_value=self.master)
        ctx = JobContext(job={'action':'upload','attendance_date':'2026-09-11','staged_filename':staged.name},
            base={'job_id':'test'}, cfg=self.c.cfg, portal_cfg={}, logger=self.c.logger,
            started=self.c.now(), staging_dir=self.base, archive_dir=self.base/'archive')
        with patch('timebase.importer.handlers.TimeBaseController', return_value=self.c), patch(
            'timebase.importer.handlers.run_controller') as run:
            with self.assertRaises(AttendanceError): handle_attendance(ctx)
        run.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_inventory_failure_is_not_skipped(self):
        self.c._master_inventory_email_index.side_effect = AttendanceError('coverage too low')
        with self.assertRaisesRegex(AttendanceError, 'coverage too low'):
            self.c.read_absent_serials(self.master)

    def test_warning_templates_escape_and_support_older_jobs(self):
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env = Environment(loader=FileSystemLoader(ROOT/'portal/templates'), autoescape=select_autoescape())
        from types import SimpleNamespace
        env.globals['request'] = SimpleNamespace(state=SimpleNamespace(user='tester', csrf_token='test'))
        job = dict(status='SUCCESS', job_id='test', job_type='attendance', message='done',
                   skipped_email_count=1, matched_email_count=0, skipped_emails=['<test>@example.com'])
        html = env.get_template('status.html').render(job=job, terminal=True)
        self.assertIn('&lt;test&gt;@example.com', html)
        self.assertIn('Zero devices', html)
        html = env.get_template('history.html').render(rows=[job])
        self.assertIn('1 skipped', html)
        env.get_template('status.html').render(job={'status':'SUCCESS'}, terminal=True)

    def test_config(self):
        path = self.base/'config.json'
        for policy in (None,'skip','error','bad'):
            if policy is not None: self.c.cfg['attendance']['unmatched_email_policy'] = policy
            path.write_text(json.dumps(self.c.cfg))
            if policy == 'bad':
                with self.assertRaises(ConfigError): load_config(path)
            else: self.assertEqual(load_config(path)['attendance']['unmatched_email_policy'], policy or 'skip')

if __name__ == '__main__': unittest.main()
