#!/usr/bin/env python3
"""Execute hotfix in a temporary server tree with mocked systemd/privileged commands.

No production paths, users, services or Jamf endpoints are touched. Calendar syntax
is additionally validated by systemd-analyze on the real server before deployment.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'portal'))
from auth_store import AuthStore, initialize_users
OLD = ['harrow-timebase-0700.timer', 'harrow-timebase-0800.timer', 'harrow-timebase-0810.timer']
NEW = ['harrow-timebase-school-start.timer','harrow-timebase-attendance.timer','harrow-timebase-reconcile-extra.timer']
KEEP = ['harrow-timebase-1600.timer','harrow-timebase-reconcile.timer']
SERVICES = ['harrow-attendance-import.path','harrow-attendance-portal.service','harrow-device-query.service']

MOCK = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ['FAKE_SERVER'])
if name != 'systemctl':
    sys.exit(0)
state_path = root/'state.json'
state = json.loads(state_path.read_text())
with (root/'commands.log').open('a') as f: f.write(' '.join(args)+'\\n')
cmd = args[0]
units = [x for x in args[1:] if not x.startswith('-')]
def save(): state_path.write_text(json.dumps(state))
if cmd in ('is-enabled','is-active'):
    u = units[-1]
    value = state.get(u, {}).get('enabled' if cmd == 'is-enabled' else 'active', 'not-found' if cmd == 'is-enabled' else 'inactive')
    if '--quiet' not in args: print(value)
    sys.exit(0 if value in ('enabled','enabled-runtime','active') else 1)
if cmd == 'daemon-reload' and (root/'fail-once').exists():
    (root/'fail-once').unlink()
    sys.exit(1)
if cmd == 'list-units':
    if (root/'busy-once').exists():
        print('harrow-timebase-reconcile.service loaded activating start')
        (root/'busy-once').unlink()
    sys.exit(0)
for u in units:
    if cmd in ('start','stop','enable','disable'):
        row = state.setdefault(u, {'active':'inactive','enabled':'disabled'})
        if cmd == 'start': row['active']='active'
        if cmd == 'stop': row['active']='inactive'
        if cmd == 'enable': row['enabled']='enabled-runtime' if '--runtime' in args else 'enabled'
        if cmd == 'disable': row['enabled']='disabled'
save()
'''

class HotfixTests(unittest.TestCase):
    def setup_server(self, root, enabled=True):
        target=root/'opt/harrow-timebase'
        target.mkdir(parents=True)
        for name in ('harrow_timebase.py','attendance_importer.py','device_query_service.py',
                     'attendance_common.py','holiday_common.py','requirements.txt'):
            shutil.copy2(ROOT/name,target/name)
        for name in ('timebase','portal'):
            shutil.copytree(ROOT/name,target/name,ignore=shutil.ignore_patterns('__pycache__'))
        with (target/'harrow_timebase.py').open('a') as f: f.write('\n# PREVIOUS RUNTIME\n')
        config=root/'etc/harrow-timebase'
        config.mkdir(parents=True)
        cfg=json.loads((ROOT/'config.production.json').read_text())
        cfg.pop('features')
        cfg['attendance'].pop('unmatched_email_policy', None)
        cfg['paths']['lock_file']=str(root/'controller.lock')
        (config/'config.json').write_text(json.dumps(cfg)+'\n')
        (config/'portal.json').write_text('{"production_setting": "keep"}\n')
        (config/'harrow-timebase.env').write_text('PRESERVE_TEST_CREDENTIAL=value\n')
        units=root/'etc/systemd/system'
        units.mkdir(parents=True)
        for name in OLD+KEEP:
            (units/name).write_text('# PREVIOUS '+name+'\n')
        (root/'run/lock').mkdir(parents=True)
        auth = root/'var/lib/harrow-timebase/portal-auth'
        initialize_users(auth, ['existing-admin'], 'Existing-Secure-Test-Password')
        self.auth_before = {name:(auth/name).read_bytes() for name in ('users.json','session.key')}
        self.session = AuthStore(auth).create_session('existing-admin', ttl_seconds=300)
        state={u:{'enabled':'enabled' if enabled else 'disabled','active':'active' if enabled else 'inactive'} for u in OLD+KEEP}
        state.update({u:{'enabled':'enabled','active':'active'} for u in SERVICES})
        (root/'state.json').write_text(json.dumps(state))
        bin_dir=root/'bin';bin_dir.mkdir()
        for name in ('systemctl','systemd-analyze','flock','chown','runuser','sleep','curl'):
            path=bin_dir/name
            path.write_text(MOCK.replace('#!/usr/bin/env python3',f'#!{sys.executable}'))
            path.chmod(0o755)
        script=(ROOT/'apply-hotfix.sh').read_text()
        script=script.replace('SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',f'SRC_DIR="{ROOT}"')
        script=script.replace('[[ $EUID -eq 0 ]]','[[ 1 -eq 1 ]]')
        for prefix in ('/opt/harrow-timebase','/etc/harrow-timebase','/etc/systemd/system','/run/lock','/var/lib/harrow-timebase'):
            script=script.replace(prefix,str(root/prefix.lstrip('/')))
        runner=root/'run-hotfix.sh'; runner.write_text(script)
        return target, config, units, state, runner

    def run_hotfix(self, root, runner, success=True):
        proc=subprocess.run(['bash',str(runner)],text=True,capture_output=True,
            env={**os.environ,'FAKE_SERVER':str(root),'PATH':str(root/'bin')+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH']})
        if success:
            self.assertEqual(proc.returncode,0,proc.stdout+'\n'+proc.stderr)
        else:
            self.assertNotEqual(proc.returncode,0)
        auth = root/'var/lib/harrow-timebase/portal-auth'
        if (auth/'session.key').exists():
            self.assertEqual(self.auth_before, {name:(auth/name).read_bytes() for name in self.auth_before})
            self.assertEqual(AuthStore(auth).verify_session(self.session), 'existing-admin')
        return proc

    def test_upgrade_and_repeat_preserve_config_and_states(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            target,config,units,prior,runner=self.setup_server(root)
            before={p.name:p.read_bytes() for p in config.iterdir()}
            (root/'busy-once').touch()
            proc = self.run_hotfix(root,runner)
            self.assertIn('Attendance unmatched email policy after upgrade: skip', proc.stdout)
            self.assertEqual(before,{p.name:p.read_bytes() for p in config.iterdir()})
            state=json.loads((root/'state.json').read_text())
            for u in OLD: self.assertFalse((units/u).exists())
            for u in NEW+KEEP: self.assertEqual(state[u],{'enabled':'enabled','active':'active'})
            self.assertNotIn('PREVIOUS RUNTIME',(target/'harrow_timebase.py').read_text())
            self.assertEqual((target/'portal/auth_store.py').read_bytes(), (ROOT/'portal/auth_store.py').read_bytes())
            backups = list((target/'backups').glob('hotfix-*'))
            self.assertEqual(len(backups), 1)
            for name, data in self.auth_before.items():
                self.assertEqual((backups[0]/'portal-auth'/name).read_bytes(), data)
            # A deliberate later pause of extra runs must survive a repeat upgrade.
            state[NEW[-1]]={'enabled':'disabled','active':'inactive'}
            (root/'state.json').write_text(json.dumps(state))
            self.run_hotfix(root,runner)
            self.assertEqual(json.loads((root/'state.json').read_text())[NEW[-1]],state[NEW[-1]])
            self.assertEqual(before,{p.name:p.read_bytes() for p in config.iterdir()})

    def test_disabled_pilot_remains_disabled(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            *_,runner=self.setup_server(root,enabled=False)
            self.run_hotfix(root,runner)
            state=json.loads((root/'state.json').read_text())
            for u in NEW+KEEP:
                self.assertNotEqual(state.get(u,{}).get('enabled'),'enabled')
                self.assertNotEqual(state.get(u,{}).get('active'),'active')

    def test_failed_deployment_restores_runtime_and_timers(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            target,config,units,prior,runner=self.setup_server(root)
            before=(target/'harrow_timebase.py').read_bytes()
            (root/'fail-once').touch()
            self.run_hotfix(root,runner,success=False)
            self.assertEqual((target/'harrow_timebase.py').read_bytes(),before)
            state=json.loads((root/'state.json').read_text())
            for u in OLD+KEEP+SERVICES: self.assertEqual(state[u],prior[u])
            for u in NEW: self.assertFalse((units/u).exists())

    def test_explicit_strict_policy_preserved(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, units, prior, runner = self.setup_server(root)
            path = config/'config.json'
            cfg = json.loads(path.read_text())
            cfg['attendance']['unmatched_email_policy'] = 'error'
            path.write_text(json.dumps(cfg))
            before = path.read_bytes()
            proc = self.run_hotfix(root, runner)
            self.assertIn('Attendance unmatched email policy after upgrade: error', proc.stdout)
            self.assertEqual(path.read_bytes(), before)

    def test_invalid_policy_aborts_before_pausing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, units, prior, runner = self.setup_server(root)
            path = config/'config.json'
            cfg = json.loads(path.read_text())
            cfg['attendance']['unmatched_email_policy'] = 'typo'
            path.write_text(json.dumps(cfg))
            self.run_hotfix(root, runner, success=False)
            self.assertEqual(json.loads((root/'state.json').read_text()), prior)
            self.assertFalse((target/'backups').exists())

    def test_missing_r4_auth_aborts_before_pausing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, units, prior, runner = self.setup_server(root)
            (root/'var/lib/harrow-timebase/portal-auth/session.key').unlink()
            before = (target/'harrow_timebase.py').read_bytes()
            self.run_hotfix(root, runner, success=False)
            self.assertEqual((target/'harrow_timebase.py').read_bytes(), before)
            self.assertEqual(json.loads((root/'state.json').read_text()), prior)
            self.assertFalse((target/'backups').exists())

if __name__ == '__main__': unittest.main()
