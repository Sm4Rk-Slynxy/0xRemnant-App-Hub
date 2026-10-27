"""Opt-in integration check using a real manager container and Docker daemon.

Run only in a disposable Linux VM/CI runner:
    docker build -t apphub-manager-ci .
    python3 -m tests.docker_smoke
All resources created here use a unique network, volume, and allocated app IDs.
"""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, timeout=180):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Do not dump command arguments or application environment values.
        raise RuntimeError('Docker command failed: '+args[0]+'\n'+result.stderr[-2000:])
    return result.stdout.strip()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    prefix = 'apphub-smoke-'+secrets.token_hex(5)
    network, volume = prefix+'-net', prefix+'-registry'
    port = free_port()
    app_port = free_port()
    while app_port == port or app_port > 65530:
        app_port = free_port()
    base = f'http://127.0.0.1:{port}'
    token = ''
    app_ids = []
    images = []

    def request(path, body=None, *, headers=None, address=base):
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        h = {'Authorization':'Bearer '+token, 'Content-Type':'application/json', **(headers or {})}
        with urllib.request.urlopen(urllib.request.Request(address+path, data=data, headers=h), timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw

    def wait_job(job):
        deadline = time.monotonic()+1000
        while time.monotonic() < deadline:
            current = request('/api/jobs/'+job)
            if current['status'] == 'failed':
                raise AssertionError('Deployment failed: '+current['log'])
            if current['status'] == 'succeeded':
                return
            time.sleep(.5)
        raise AssertionError('Deployment timed out')

    def deploy(filename, app_id=None):
        settings = request('/api/upload-settings', {'env':''})
        query = {'name':'CI example', 'filename':Path(filename).name}
        if app_id:
            query['id'] = app_id
        result = request('/api/deploy?'+urllib.parse.urlencode(query), Path(filename).read_bytes(), headers={'X-AppHub-Settings':settings['id'], 'Content-Type':'application/octet-stream'})
        if result['app_id'] not in app_ids:
            app_ids.append(result['app_id'])
        wait_job(result['job_id'])
        app = next(app for app in request('/api/apps')['apps'] if app['id'] == result['app_id'])
        images.extend('apphub/'+app['id']+':'+release['version'] for release in app['versions'])
        return app

    try:
        docker('network','create',network)
        docker('volume','create',volume)
        docker('run','-d','--name',prefix,'--network',network,
            '-p',f'127.0.0.1:{port}:8080',
            '-e','APPHUB_NETWORK='+network,'-e','APPHUB_BIND_IP=127.0.0.1',
            '-e',f'APPHUB_PUBLIC_PORT={port}',
            '-e',f'APPHUB_PORT_START={app_port}','-e',f'APPHUB_PORT_END={app_port+3}',
            '-v',volume+':/data','-v','/var/run/docker.sock:/var/run/docker.sock',
            os.environ.get('APPHUB_SMOKE_IMAGE','apphub-manager-ci'))
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            try:
                request('/healthz')
                break
            except (OSError, urllib.error.URLError):
                time.sleep(.2)
        else:
            raise AssertionError('Manager failed to start')
        setup_token = docker('exec',prefix,'python','-m','apphub.server','--show-setup-token')
        token = request('/api/setup', {'password':secrets.token_urlsafe(24), 'setup_token':setup_token})['token']
        static = deploy(ROOT/'examples/hello-app.html')
        static_url = f"http://127.0.0.1:{static['port']}"
        assert b'Little Notes' in request('/',address=static_url)
        static_updated = deploy(ROOT/'examples/hello-app.zip',static['id'])
        assert static_updated['port'] == static['port']
        previous = next(v for v in static_updated['versions'] if v['version'] != static_updated['current_version'])
        wait_job(request('/api/apps/'+static['id']+'/rollback', {'version':previous['version']})['job_id'])
        assert b'Little Notes' in request('/',address=static_url)
        counter = deploy(ROOT/'examples/python-counter.zip')
        counter_url = f"http://127.0.0.1:{counter['port']}"
        assert request('/api/count',{},address=counter_url)['count'] == 1
        request('/api/apps/'+counter['id']+'/restart',{})
        # Give the real HTTP service a moment to listen after Docker restart.
        for attempt in range(50):
            try:
                assert request('/api/count',address=counter_url)['count'] == 1
                break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError('Counter did not restart')
        counter_updated = deploy(ROOT/'examples/python-counter.zip',counter['id'])
        assert counter_updated['port'] == counter['port']
        assert request('/api/count',address=counter_url)['count'] == 1
        docker('restart',prefix)
        for attempt in range(100):
            try:
                assert len(request('/api/apps')['apps']) == 2
                break
            except OSError:
                time.sleep(.2)
        else:
            raise AssertionError('Registry did not recover after manager restart')
        print('PASS: real manager image, terminal-token setup, HTML/ZIP builds, update, rollback, SQLite volume persistence, and manager restart.')
    finally:
        def cleanup(*args):
            subprocess.run(['docker',*args],capture_output=True,text=True,timeout=60)
        for app_id in app_ids:
            containers = docker('ps','-aq','--filter','label=com.apphub.managed=1','--filter','label=com.apphub.app='+app_id).split()
            if containers:
                cleanup('rm','-f',*containers)
            cleanup('volume','rm','-f','apphub-data-'+app_id)
            images.extend(docker('images','-q','--filter','label=com.apphub.app='+app_id).split())
        cleanup('rm','-f',prefix)
        cleanup('volume','rm','-f',volume)
        cleanup('network','rm',network)
        if images:
            cleanup('image','rm','-f',*sorted(set(images)))


if __name__ == '__main__':
    main()
