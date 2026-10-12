"""Asynchronous clone jobs; one production operation at a time."""
import concurrent.futures
import os
import subprocess
import sys
import threading
import time

LOCK = threading.Lock()
JOBS = {}


def start(server, request):
    import clone
    operation = request.get('operation')
    if operation not in ('read', 'write'):
        raise ValueError('Operation must be read or write')
    name = request.get('name', '')
    clone.image_paths(name)
    devices = request.get('devices', [])
    if not isinstance(devices, list) or not devices or len(devices) > 10 or len(set(devices)) != len(devices):
        raise ValueError('Select 1-10 distinct cards')
    allowed = {d['device'] for d in server.list_devices()}
    if any(d not in allowed for d in devices):
        raise ValueError('Only detected removable cards may be selected')
    if operation == 'read' and len(devices) != 1:
        raise ValueError('Select exactly one master card')
    prefix = request.get('prefix', 'supernova')
    first = int(request.get('first', 1))
    if first < 1:
        raise ValueError('Starting number must be positive')
    hostnames = [f'{prefix}-{first+i:03d}' for i in range(len(devices))]
    if operation == 'write':
        from firstrun_gen import make_identity_firstrun
        for hostname in hostnames:
            make_identity_firstrun(hostname)
        img, meta = clone.image_paths(name)
        if not os.path.isfile(img) or not os.path.isfile(meta):
            raise ValueError('Select a saved master image')
    if operation == 'write' and request.get('tailscale', False):
        config = server.load_tailscale_config()
        if not config.get('mint_endpoint') and not config.get('ts_apikey_file'):
            raise ValueError('Configure Tailscale settings before enabling injection')
    with LOCK:
        if server.FLASH_ACTIVE.is_set():
            raise ValueError('Another card operation is running')
        server.FLASH_ACTIVE.set()
        JOBS.clear()
        for device, hostname in zip(devices, hostnames):
            server.clear_cancel(device)
            server.set_job(device, state='queued', percent=0, hostname=hostname, message='Queued…', detail='', master_image=operation == 'write')
            JOBS[device] = {'state': 'queued', 'hostname': hostname, 'log': [], 'seconds': 0}

    def worker(device, hostname):
        started = time.monotonic()
        command = [sys.executable, os.path.join(server.BASE_DIR, 'clone.py'), operation,
                   device, '--name', name]
        if operation == 'write':
            command += ['--hostname', hostname]
        try:
            with LOCK:
                JOBS[device]['state'] = 'running'
            server.check_cancelled(device)
            server.set_job(device, state='writing', percent=0, message='Saving master image…' if operation == 'read' else 'Writing installed image; partition stage skipped…')
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
            server.register_proc(device, process)
            phase_started = time.monotonic()
            partition = ''
            for line in process.stdout:
                text = line.rstrip()
                if text.startswith('COPY '):
                    partition = text.split()[1]
                    phase_started = time.monotonic()
                    server.set_job(device, percent=0, message=f'Copying {partition}; estimating time…')
                elif text.startswith('METRICS '):
                    _, done, total, elapsed, remaining, speed = text.split()
                    server.set_job(device, elapsed_s=float(elapsed), eta_s=float(remaining),
                                   message=f'Writing mapped blocks · {float(done)/1e9:.2f}/{float(total)/1e9:.2f} GB · {float(speed)/1e6:.1f} MB/s · ETA {server.format_duration(float(remaining)) if float(remaining)>=0 else "estimating"}')
                elif text.startswith('STEP '):
                    phase = text[5:]
                    server.set_job(device, percent=0, state='verifying' if phase == 'verify' else 'writing', message=f'{phase}: {"master capture" if operation == "read" else "installed image"}')
                elif text.startswith('PROGRESS '):
                    percent = float(text.split()[-1])
                    elapsed = time.monotonic() - phase_started
                    remaining = elapsed * (100 - percent) / percent if percent > 0 else None
                    estimate = server.format_duration(remaining) if remaining is not None else 'estimating…'
                    server.set_job(device, percent=percent, elapsed_s=round(time.monotonic()-started), eta_s=round(remaining) if remaining is not None else None, message=f'Copying {partition}: {percent:.1f}% · elapsed {server.format_duration(time.monotonic()-started)} · partition ETA {estimate}')
                with LOCK:
                    JOBS[device]['log'] = (JOBS[device]['log'] + [line.rstrip()])[-80:]
                    JOBS[device]['seconds'] = round(time.monotonic() - started, 1)
            code = process.wait()
            server.unregister_proc(device)
            server.check_cancelled(device)
            if code:
                raise RuntimeError('Card operation failed; see details')
            if operation == 'write' and request.get('tailscale', False):
                server.tailscale_provision_device(device)
                with server.JOBS_LOCK:
                    state = server.JOBS[device]['state']
                if state != 'done':
                    raise RuntimeError('Image written, but Tailscale injection failed or was cancelled')
            server.set_job(device, state='done', percent=100,
                           message='Master image saved — select it in OS image' if operation == 'read' else 'Installed image ready; partitions preserved' + ('; Tailscale injected' if request.get('tailscale', False) else '; continue to Tailscale'))
            with LOCK:
                JOBS[device]['state'] = 'done' if code == 0 else 'failed'
        except Exception as error:
            server.unregister_proc(device)
            with LOCK:
                detail = '\n'.join(JOBS[device]['log'])
            server.set_job(device, state='cancelled' if server.is_cancelled(device) else 'error', percent=0, message=str(error), detail=detail)
            with LOCK:
                JOBS[device]['state'] = 'failed'
                JOBS[device]['log'].append(str(error))
        finally:
            with LOCK:
                JOBS[device]['seconds'] = round(time.monotonic() - started, 1)

    def runner():
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                list(pool.map(worker, devices, hostnames))
        finally:
            server.FLASH_ACTIVE.clear()
    threading.Thread(target=runner, daemon=True).start()


def snapshot():
    with LOCK:
        return {device: {**job, 'log': list(job['log'])} for device, job in JOBS.items()}
