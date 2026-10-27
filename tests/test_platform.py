"""Exercise installer dispatch and SELinux selection without changing the host.

Every package/service command is replaced by a temporary recording stub. The
installer reads a fixture os-release and cannot reach the real package manager.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).parents[1]
STUB = r'''import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TEST_CALLS'], 'a') as out:
    out.write(json.dumps([name, *args]) + '\n')
if name == 'id':
    print(os.environ.get('TEST_UID', '0'))
elif name == 'rpm':
    sys.exit(0 if args[-1] in os.environ.get('TEST_PACKAGES', '').split(',') else 1)
elif name == 'docker':
    if args == ['compose', 'version']:
        sys.exit(0 if os.environ.get('TEST_DOCKER') == '1' else 1)
elif name == 'getenforce':
    print(os.environ.get('TEST_SELINUX', 'Disabled'))
elif name == 'dirname':
    print(str(Path(args[-1]).parent))
'''


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='apphub-platform-')
        self.directory = Path(self.temp.name)
        self.bin = self.directory/'bin'
        self.bin.mkdir()
        self.calls_file = self.directory/'calls.jsonl'
        self.environment = {**os.environ, 'PATH': str(self.bin), 'TEST_ROOT': str(self.directory), 'TEST_CALLS': str(self.calls_file)}
        for name in ('id','rpm','dnf','docker','systemctl','getenforce','dirname'):
            self.command(name)
        (self.directory/'os-release').write_text('ID=fedora\nVERSION_ID=44\n')

    def tearDown(self):
        self.temp.cleanup()

    def command(self, name):
        path = self.bin/name
        path.write_text('#!' + sys.executable + '\n' + STUB)
        path.chmod(0o755)

    def run_script(self, name, *args, **environment):
        source = (ROOT/name).read_text()
        if name == 'install-docker.sh':
            source = source.replace('. /etc/os-release', '. "$TEST_ROOT/os-release"')
            source = source.replace('/etc/pve', self.directory.as_posix()+'/pve')
            source = source.replace('/run/ostree-booted', self.directory.as_posix()+'/ostree-booted')
        else:
            source = source.replace('/sys/fs/selinux/enforce', self.directory.as_posix()+'/selinux-enforce')
        script = self.directory/name
        script.write_text(source)
        result = subprocess.run(['/bin/sh',str(script),*args],env={**self.environment,**environment},capture_output=True,text=True,timeout=10)
        calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()] if self.calls_file.exists() else []
        return result,calls

    def test_fedora_uses_dnf5_repo_and_starts_docker(self):
        self.command('dnf5')
        result,calls = self.run_script('install-docker.sh')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(['dnf5','config-manager','addrepo','--from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo','--overwrite'],calls)
        self.assertIn(['dnf5','-y','install','docker-ce','docker-ce-cli','containerd.io','docker-buildx-plugin','docker-compose-plugin'],calls)
        self.assertIn(['systemctl','enable','--now','docker'],calls)
        self.assertFalse(any(c[0] == 'dnf' for c in calls))

    def test_fedora_dnf4_uses_legacy_config_manager(self):
        result,calls = self.run_script('install-docker.sh')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(['dnf','config-manager','--add-repo','https://download.docker.com/linux/fedora/docker-ce.repo'],calls)
        self.assertIn(['dnf','-y','install','ca-certificates','dnf-plugins-core'],calls)

    def test_podman_shim_is_rejected_without_package_changes(self):
        result,calls = self.run_script('install-docker.sh',TEST_PACKAGES='podman-docker',TEST_DOCKER='1')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('podman-docker',result.stdout)
        self.assertFalse(any(c[0] in {'dnf','dnf5','systemctl'} for c in calls))

    def test_standalone_podman_can_coexist(self):
        result,calls = self.run_script('install-docker.sh',TEST_PACKAGES='podman')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(any(c[0] == 'dnf' for c in calls))

    def test_existing_conflicting_package_is_not_removed(self):
        result,calls = self.run_script('install-docker.sh',TEST_PACKAGES='moby-engine')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('moby-engine',result.stdout)
        self.assertFalse(any(c[0] in {'dnf','dnf5','systemctl'} for c in calls))

    def test_start_also_rejects_podman_compatibility_command(self):
        result,calls = self.run_script('start.sh',TEST_PACKAGES='podman-docker',TEST_DOCKER='1')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Podman compatibility',result.stdout)
        self.assertFalse(any(c[0] == 'docker' for c in calls))

    def test_existing_docker_and_compose_need_no_reinstall(self):
        result,calls = self.run_script('install-docker.sh',TEST_DOCKER='1')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(any(c[0] in {'dnf','dnf5','systemctl'} for c in calls))

    def test_immutable_fedora_is_rejected(self):
        (self.directory/'ostree-booted').touch()
        result,calls = self.run_script('install-docker.sh')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('rpm-ostree',result.stdout)
        self.assertFalse(any(c[0] in {'dnf','dnf5'} for c in calls))

    def test_wrapper_selects_override_for_enabled_selinux(self):
        for mode in ('Enforcing','Permissive'):
            with self.subTest(mode=mode):
                result,calls = self.run_script('compose.sh','logs','--tail=50','hub',TEST_SELINUX=mode)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(calls[-1],['docker','compose','-f','compose.yaml','-f','compose.selinux.yaml','logs','--tail=50','hub'])

    def test_wrapper_keeps_base_configuration_when_selinux_disabled(self):
        result,calls = self.run_script('compose.sh','up','-d','--build')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(calls[-1],['docker','compose','-f','compose.yaml','up','-d','--build'])

    def test_kernel_selinux_indicator_works_without_getenforce(self):
        (self.bin/'getenforce').unlink()
        (self.directory/'selinux-enforce').touch()
        result,calls = self.run_script('compose.sh','ps')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('compose.selinux.yaml',calls[-1])


if __name__ == '__main__':
    unittest.main()
