#!/usr/bin/env python3
"""Opt-in five-device integration test. Restores original Rooms in a finally block.

Run only against an authorized test tenant with all competing schedulers stopped.
Credentials come from the environment, never arguments. The work directory holds
private inventory snapshots and logs needed for recovery if the process is killed.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from urllib.parse import unquote
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("HARROW_RUNTIME_ROOT", str(ROOT)))
from harrow_timebase import TimeBaseController, load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--expected-group-id', type=int, required=True)
    parser.add_argument('--allow-room-writes', action='store_true')
    args = parser.parse_args()
    if not args.allow_room_writes:
        parser.error('Live Room changes require --allow-room-writes')
    os.umask(0o077)
    work = Path(args.work_dir)
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (work/'snapshot.json').exists():
        parser.error('Use a fresh work directory; existing recovery snapshot must not be overwritten')
    logger = logging.getLogger('live-jamf-test')
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.FileHandler(work/'private.log'))
    cfg = load_config(Path(args.config))
    assert cfg['safety']['min_master_devices'] == cfg['safety']['max_master_devices'] == 5
    assert cfg.get('features', {}).get('wifi_management_enabled', False) is False
    c = TimeBaseController(cfg, logger)
    master = c.master_members()
    assert len(master) == 5 and c.master_group().id == args.expected_group_id
    assert not c.master_group().is_smart
    c.preflight()
    snapshot = {serial: dict(zip(('email','username','room'), c._classic_location_for_serial(serial)))
                for serial in sorted(master)}
    assert len({row['email'] for row in snapshot.values() if row['email']}) == 5
    (work/'snapshot.json').write_text(json.dumps(snapshot))
    wifi_id = c.profiles().get('WiFi-Harrow')
    def wifi_hash():
        if wifi_id is None: return None
        scope = c.jamf.get_profile_xml(wifi_id).find('scope')
        return hashlib.sha256(ET.tostring(scope)).hexdigest()
    wifi_before = wifi_hash()
    real_request = c.jamf.request
    writes = []
    def guarded_request(method, path, **kwargs):
        if method.upper() in {'PUT','POST','PATCH','DELETE'}:
            prefix = '/JSSResource/mobiledevices/serialnumber/'
            assert method.upper() == 'PUT' and path.startswith(prefix), 'Only device Room PUTs allowed'
            serial = unquote(path[len(prefix):])
            assert serial in master, 'Write target outside the five-device snapshot'
            body = ET.fromstring(kwargs['data'])
            assert body.tag == 'mobile_device' and len(body) == 1
            assert body[0].tag == 'location' and len(body[0]) == 1 and body[0][0].tag == 'room'
            writes.append(serial)
        return real_request(method, path, **kwargs)
    c.jamf.request = guarded_request
    actual_now = c.now
    day = actual_now().date()
    for _ in range(14):
        if c.is_school_day(day)[0]: break
        day += timedelta(days=1)
    else: raise RuntimeError('No school day found for test')
    phases = []
    def clock(value, selected_day=day):
        c.now = lambda: datetime.fromisoformat(f'{selected_day}T{value}').replace(tzinfo=c.tz)
    def check(label, expected_in):
        actual_in, actual_out = c.current_in_out()
        assert len(actual_in & master) == expected_in, label
        assert len(actual_out & master) == 5-expected_in, label
        assert not (actual_in & actual_out & master), label
        phases.append({'phase':label,'in':expected_in,'out':5-expected_in})
        print(f'PASS {label}: In={expected_in}, Out={5-expected_in}', flush=True)
    absent = set(sorted(master)[:2])
    attendance = c.attendance_path(day)
    attendance.parent.mkdir(parents=True, exist_ok=True)
    saved_attendance = attendance.read_bytes() if attendance.exists() else None
    overrides = Path(cfg['paths']['manual_override_file'])
    saved_overrides = overrides.read_bytes() if overrides.exists() else None
    failures = []
    try:
        overrides.parent.mkdir(parents=True, exist_ok=True)
        overrides.write_text('{"overrides": {}}\n')
        attendance.write_text('email_address\n'+''.join(snapshot[s]['email']+'\n' for s in sorted(absent)))
        for value, expected in [('07:59',0),('08:00',5),('08:09',5),('08:10',3)]:
            clock(value)
            c.reconcile()
            check(value, expected)
        before = len(writes)
        for value in ('09:10','14:00'):
            clock(value)
            c.reconcile()
            check(value,3)
        assert len(writes) == before, 'Idempotent reconciliation issued Room PUTs'
        print('PASS unchanged attendance requires no repeated Room PUTs',flush=True)
        clock('10:21')
        serial = sorted(master-absent)[0]
        c.set_manual_override(serial, reason='Authorized five-device integration test')
        c.reconcile()
        check('manual override',2)
        c.clear_manual_override(serial)
        c.reconcile()
        check('manual clear',3)
        temporary = attendance.with_suffix('.incoming')
        temporary.write_text('email_address\n'+snapshot[sorted(absent)[0]]['email']+'\n')
        temporary.replace(attendance)
        clock('10:30')
        c.reconcile()
        check('direct CSV replacement invalidates cache',4)
        clock('16:00')
        c.reconcile()
        check('16:00',0)
        weekend = day + timedelta(days=(5-day.weekday()) % 7)
        clock('14:00', weekend)
        c.reconcile()
        check('weekend',0)
        holiday = next(iter(c.holiday_map()))
        clock('14:00',holiday)
        c.reconcile()
        check('holiday',0)
    except Exception as exc:
        logger.exception('Live test failed')
        failures.append(type(exc).__name__)
    finally:
        c.now = actual_now
        # Restore directly from the original Room snapshot, independent of test clock.
        for serial, row in snapshot.items():
            try:
                current = c._classic_location_for_serial(serial)[2]
                if current != row['room']: c.jamf.update_room(serial,row['room'])
            except Exception as exc:
                logger.exception('Room restoration failed')
                failures.append('restore:'+type(exc).__name__)
        try:
            for serial, row in snapshot.items():
                assert c._classic_location_for_serial(serial)[2] == row['room']
            assert wifi_hash() == wifi_before, 'Wi-Fi scope changed during test'
            print('PASS original Room values restored for all five devices; Wi-Fi scope unchanged',flush=True)
        except Exception as exc:
            logger.exception('Restoration verification failed')
            failures.append('verification:'+type(exc).__name__)
        for path, saved in ((attendance,saved_attendance),(overrides,saved_overrides)):
            if saved is None: path.unlink(missing_ok=True)
            else: path.write_bytes(saved)
    report = {'phases':phases,'room_puts':len(writes),'failures':failures,'wifi_scope_unchanged':wifi_hash()==wifi_before}
    (work/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'completed_phases':len(phases),'failures':failures}),flush=True)
    return bool(failures)

if __name__ == '__main__': raise SystemExit(main())
