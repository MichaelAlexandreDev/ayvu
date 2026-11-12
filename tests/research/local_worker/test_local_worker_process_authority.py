"""Evidência sintética #167. Não importa nem habilita um runner em produção."""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
from uuid import uuid4
import pytest
from tests.research.local_worker.local_worker_spike_support import bounded_case, exited_and_reap, reaper, root
from tests.research.local_worker.local_worker_spike_support import spike


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_launch_allowlist_environment_descriptors_cwd_and_residual_authority(root, monkeypatch, transport):
    outside = root.parent / 'outside'
    outside.mkdir()
    canary = uuid4().hex
    (outside / 'canary').write_text(canary)
    monkeypatch.setenv('AYVU_SYNTHETIC_CANARY', canary)
    descriptor = os.open(outside / 'canary', os.O_RDONLY)
    os.set_inheritable(descriptor, True)
    try:
        with spike.Session(root, transport) as session:
            session.command('inheritance', port=None, descriptor=descriptor)
            authority = session.receive()['body']
            session.finish()
            assert authority == dict(environment_inherited=False, descriptor_inherited=False,
                                     cwd_explicit=True, filesystem_readable=True, filesystem_writable=True,
                                     loopback_reachable=None, process_spawn=True)
            assert canary not in json.dumps(authority) + repr(session)
    finally:
        os.close(descriptor)
    with spike.Session(root) as session:
        with pytest.raises(spike.Refusal, match='^launch_refused$'):
            session.command('arbitrary_command')
    with pytest.raises(spike.Refusal, match='^launch_refused$'):
        spike.Session(root, 'shell')


def test_default_subprocess_inherits_environment_descriptor_and_cwd(root):
    # Contraprova sem a mitigação: somente um canário sintético; imprime booleanos.
    environment = {**spike.minimal_environment(), 'AYVU_SYNTHETIC_CANARY': uuid4().hex}
    fd = os.open(root / 'cwd_marker', os.O_RDONLY)
    os.set_inheritable(fd, True)
    code = ('import os,json; print(json.dumps(["AYVU_SYNTHETIC_CANARY" in os.environ,'
            f'os.fstat({fd}).st_size == 1,os.path.isfile("cwd_marker")]))')
    # Um pai intermediário recebe SÓ a fixture. Seu filho usa os defaults reais,
    # sem copiar o ambiente ou descritores reais do processo de pytest.
    parent_code = ('import subprocess,sys; '
                   'r=subprocess.run([sys.executable,"-I","-S","-c",sys.argv[1]],'
                   'close_fds=False,capture_output=True,timeout=3); '
                   'sys.stdout.buffer.write(r.stdout); sys.exit(r.returncode)')
    try:
        result = subprocess.run([sys.executable, '-I', '-S', '-c', parent_code, code], cwd=root,
                                env=environment, close_fds=True, pass_fds=(fd,),
                                capture_output=True, timeout=spike.STARTUP)
        assert result.returncode == 0
        assert json.loads(result.stdout) == [True, True, True]
    finally:
        os.close(fd)


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_force_cleanup_includes_child_and_grandchild(root, transport, reaper):
    pids = []
    try:
        with spike.Session(root, transport) as session:
            session.command('tree')
            pids = session.receive()['body']['pids']
            assert pids[0] == session.process.pid and len(set(pids)) == 3
            session.terminate()
            exited_and_reap(pids[1:])
            assert session.process.returncode == -signal.SIGKILL
    finally:
        # Contenção do harness continua válida mesmo quando mutamos o cleanup.
        if pids:
            try:
                os.killpg(pids[0], signal.SIGKILL)
            except ProcessLookupError:
                pass
            exited_and_reap(pids[1:])


@pytest.mark.parametrize('transport', ['socketpair', 'pipes'])
def test_parent_crash_eof_cleans_worker_child_and_grandchild(root, transport, reaper):
    code = ('import importlib.util,os,json,sys; '
            'spec=importlib.util.spec_from_file_location("p",sys.argv[1]); '
            'p=importlib.util.module_from_spec(spec); spec.loader.exec_module(p); '
            's=p.Session(p.Path.cwd(),sys.argv[2]); s.command("tree"); '
            'data=s.receive()["body"]; print(json.dumps(data),flush=True); '
            'os._exit(29)')
    parent = subprocess.Popen([sys.executable, '-I', '-S', '-c', code, str(spike.ENTRYPOINT), transport],
                              cwd=root, env=spike.minimal_environment(), shell=False, close_fds=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = parent.communicate(timeout=spike.STARTUP + spike.KILL_GRACE)
        assert parent.returncode == 29 and not errors
        pids = json.loads(output)['pids']
        exited_and_reap(pids)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=spike.KILL_GRACE)


def test_peer_injection_and_stale_endpoint_cannot_address_anonymous_channel(root):
    # Endpoint sem nome não pode ser conectado nem substituído por um pathname antigo.
    stale = root / 'stale.sock'
    stale.write_bytes(b'synthetic-stale')
    with spike.Session(root) as session:
        # Outro processo tenta usar o número do descritor sem receber a autoridade.
        code = "import os,sys\ntry:\n os.write(int(sys.argv[1]),b'injection')\nexcept OSError:\n sys.exit(31)"
        attacker = subprocess.run([sys.executable, '-I', '-S', '-c', code,
                                   str(session.wire.write_fd)], close_fds=True,
                                  env=spike.minimal_environment(), cwd=root,
                                  capture_output=True, timeout=spike.STARTUP)
        assert attacker.returncode == 31 and not attacker.stdout and not attacker.stderr
        assert stale.read_bytes() == b'synthetic-stale'
        session.command('success')
        session.receive()
        assert session.receive()['kind'] == 'result'
        session.finish()
    assert stale.is_file()  # limpeza nunca remove endpoint alheio
    assert session.parent_socket.fileno() == -1
