#!/usr/bin/env python3
"""Schedule boundaries, disabled Wi-Fi, and portal triggers without Jamf writes."""
import json
import logging
import sys
import unittest
from datetime import datetime, date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harrow_timebase import TimeBaseController, load_config
from timebase.controller.types import ControllerError, ConfigError, GroupInfo
from timebase.importer.handlers import JobContext, handle_attendance

TZ = ZoneInfo('Asia/Bangkok')

class ScheduleTests(unittest.TestCase):
    def controller(self, clock='08:10', school=True):
        c = TimeBaseController.__new__(TimeBaseController)
        c.cfg = {}
        c.logger = logging.getLogger('schedule-test')
        c.now = lambda: datetime.fromisoformat('2026-09-10T' + clock).replace(tzinfo=TZ)
        c.is_school_day = Mock(return_value=(school, 'school day' if school else 'holiday'))
        for name in ('preflight', 'set_all_in', 'set_all_out', 'apply_attendance', 'purge_expired_manual_overrides'):
            setattr(c, name, Mock())
        c.jamf = Mock()
        return c

    def test_boundaries_and_extra_runs(self):
        for clock, expected in [('07:00','set_all_out'), ('07:59:59','set_all_out'),
            ('08:00','set_all_in'), ('08:09:59','set_all_in'), ('08:10','apply_attendance'),
            ('09:10','apply_attendance'), ('14:00','apply_attendance'),
            ('15:59:59','apply_attendance'), ('16:00','set_all_out'), ('23:59','set_all_out')]:
            with self.subTest(clock=clock):
                c = self.controller(clock)
                c.reconcile()
                getattr(c, expected).assert_called_once()
                for other in {'set_all_in','set_all_out','apply_attendance'} - {expected}:
                    getattr(c, other).assert_not_called()
                self.assertEqual(c.jamf.mock_calls, [])

    def test_holiday_and_weekend_state(self):
        for clock in ('08:00','08:10','09:10','14:00','16:00'):
            c = self.controller(clock, school=False)
            c.reconcile()
            c.set_all_out.assert_called_once()
            c.apply_attendance.assert_not_called()
            self.assertEqual(c.jamf.mock_calls, [])

    def test_delayed_jobs_apply_current_phase(self):
        c = self.controller('08:11')
        c.action_school_start()
        c.apply_attendance.assert_called_once()
        c.set_all_in.assert_not_called()
        c = self.controller('16:01')
        c.action_attendance()
        c.set_all_out.assert_called_once()
        c.apply_attendance.assert_not_called()

    def test_disabled_wifi_action_does_nothing(self):
        c = self.controller()
        c.action_0810()
        c.preflight.assert_not_called()
        c.set_wifi_scope(True)
        c.set_wifi_scope(False)
        self.assertEqual(c.jamf.mock_calls, [])

    def test_preflight_does_not_require_wifi_profile(self):
        c = self.controller()
        del c.preflight
        c.cfg = {'safety': {'min_master_devices': 1, 'max_master_devices': 2}}
        c.cfg["groups"] = {'master':'master', 'in_harrow':'in', 'out_harrow':'out'}
        c.cfg["profiles"] = {'assure':'ASSURE'}  # No Wi-Fi profile at all.
        c.cfg["rooms"] = {'in_harrow':'100','out_harrow':'200'}
        c.groups = Mock(return_value={})
        c.require_group = lambda name: GroupInfo(1, name, name != 'master')
        c.jamf.group_members.return_value = {'serial'}
        c.write_master_cache = Mock()
        c._assert_room_criterion = Mock()
        c.profiles = Mock(return_value={'ASSURE':1})
        c.require_profile = Mock(return_value=1)
        c.jamf.profile_target_groups.return_value = {'in'}
        c.jamf.profile_exclusion_groups.return_value = {'out'}
        c.holiday_map = Mock(return_value={})
        result = c.preflight()
        self.assertIsNone(result['wifi_profile_id'])
        c.require_profile.assert_called_once_with('ASSURE')
        c.jamf.get_profile_xml.assert_called_once_with(1)

    def test_verify_does_not_require_wifi_profile(self):
        c = self.controller()
        c.master_members = Mock(return_value={'SERIAL'})
        c.current_in_out = Mock(return_value=({'SERIAL'}, set()))
        c.active_manual_overrides = Mock(return_value={})
        result = c.verify_current()
        self.assertFalse(result['wifi_management_enabled'])
        self.assertIsNone(result['wifi_target_in_harrow'])
        self.assertEqual(c.jamf.mock_calls, [])

    def test_timer_contract(self):
        from configparser import ConfigParser
        expected = {
            'school-start': ('Mon..Fri *-*-* 08:00:00 Asia/Bangkok', 'harrow-timebase@school-start.service'),
            'attendance': ('Mon..Fri *-*-* 08:10:00 Asia/Bangkok', 'harrow-timebase@attendance.service'),
            'reconcile': ('*-*-* *:00,30:00 Asia/Bangkok', 'harrow-timebase-reconcile.service'),
            'reconcile-extra': ('Mon..Fri *-*-* 09:10:00 Asia/Bangkok\nMon..Fri *-*-* 14:00:00 Asia/Bangkok', 'harrow-timebase-reconcile.service'),
        }
        for name, (calendar, unit) in expected.items():
            parser = ConfigParser()
            path = ROOT / f'systemd/harrow-timebase-{name}.timer'
            lines = path.read_text().splitlines()
            calendars = [line.removeprefix('OnCalendar=') for line in lines if line.startswith('OnCalendar=')]
            self.assertEqual('\n'.join(calendars), calendar)
            # systemd supports repeated OnCalendar keys; ConfigParser does not.
            parser.read_string('\n'.join(line for line in lines if not line.startswith('OnCalendar=')))
            timer = parser['Timer']
            self.assertEqual(timer['Unit'], unit)
            self.assertEqual(timer['AccuracySec'], '1s')
            self.assertEqual(timer['RandomizedDelaySec'], '0')
            self.assertNotIn('OnUnitActiveSec', timer)
            if name == 'reconcile': self.assertEqual(timer['OnBootSec'], '3min')
        for old in ('0700','0800','0810'):
            self.assertFalse((ROOT/f'systemd/harrow-timebase-{old}.timer').exists())

    def test_manual_override_window(self):
        for clock, allowed in [('07:59',False),('08:00',True),('15:59',True),('16:00',False)]:
            c = self.controller(clock)
            c.tz = TZ
            c.master_members = Mock(return_value={'SERIAL'})
            c._load_manual_override_state = Mock(return_value={'overrides':[]})
            c._save_manual_override_state = Mock()
            c.dry_run = True
            if not allowed:
                with self.assertRaises(ControllerError): c.set_manual_override('SERIAL')
            else:
                # Reaching the membership check proves the time guard accepted the request.
                c.master_members.side_effect = RuntimeError('accepted window')
                with self.assertRaisesRegex(RuntimeError, 'accepted window'): c.set_manual_override('SERIAL')

    def test_config_defaults_and_validation(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.json'
            cfg = json.loads((ROOT/'config.production.json').read_text())
            cfg.pop('features')
            path.write_text(json.dumps(cfg))
            self.assertNotIn('features', load_config(path))
            cfg['features'] = {'wifi_management_enabled':'false'}
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ConfigError): load_config(path)

    def test_portal_trigger_window_and_dates(self):
        cases = [('08:00',True,0,False),('08:09:59',True,0,False),('08:10',True,0,True),
                 ('15:59:59',True,0,True),('16:00',True,0,False),('09:10',False,0,False),
                 ('09:10',True,-1,False),('09:10',True,1,False)]
        from datetime import timedelta
        for clock, school, offset, should_run in cases:
            for action in ('upload','zero_absent'):
                with self.subTest(clock=clock, action=action, offset=offset, school=school), TemporaryDirectory() as tmp:
                    base = Path(tmp)
                    today = date(2026,9,10)
                    attendance_date = today + timedelta(days=offset)
                    (base/'input.csv').write_text('email_address\nstudent@example.com\n')
                    cfg = {'timezone':'Asia/Bangkok','paths':{'attendance_dir':tmp,'state_dir':tmp},
                           'safety':{'max_absent_fraction':1}}
                    ctx = JobContext(job={'action':action,'attendance_date':attendance_date.isoformat(),
                        'staged_filename':'input.csv'}, base={'job_id':'test'}, cfg=cfg, portal_cfg={},
                        logger=logging.getLogger('test'), started=datetime.fromisoformat(f'{today}T{clock}').replace(tzinfo=TZ),
                        staging_dir=base, archive_dir=base/'archive')
                    c = Mock()
                    c.master_members.return_value = {'SERIAL'}
                    c.resolve_absent_emails.return_value = ({'SERIAL'}, [], {})
                    c.attendance_resolution_path.return_value = base/'cache.json'
                    c.is_school_day.return_value = (school,'school day' if school else 'holiday')
                    with patch('timebase.importer.handlers.TimeBaseController',return_value=c), \
                         patch('timebase.importer.handlers.run_controller',return_value=Mock(returncode=0)) as run:
                        handle_attendance(ctx)
                        self.assertEqual(run.called, should_run)

if __name__ == '__main__':
    unittest.main()
