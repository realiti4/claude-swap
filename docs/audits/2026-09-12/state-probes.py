"""Isolated audit proofs. Run from repo: PYTHONPATH=src .venv/bin/python /tmp/cswap_state_audit_repros.py.
Only temporary fake data. Native Keychain and legacy sweep are mocked.
"""
import errno
import json
import logging
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch
from claude_swap import paths
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.models import Platform
from claude_swap.locking import FileLock


def migration():
    with tempfile.TemporaryDirectory(prefix='cswap-audit-') as d:
        root = Path(d); legacy = root / 'legacy'; target = root / 'new'
        legacy.mkdir()
        (legacy / 'credential.enc').write_text('synthetic credential')
        (legacy / 'roster.json').write_text('{}')
        real_rmtree = shutil.rmtree
        def interrupted_cleanup(path, *a, **kw):
            if Path(path) == legacy:
                (legacy / 'credential.enc').unlink()
                raise OSError('simulated interruption during post-copy source deletion')
            return real_rmtree(path, *a, **kw)
        with patch.object(paths, 'get_legacy_backup_root', return_value=legacy):
            with patch('shutil.os.rename', side_effect=OSError(errno.EXDEV, 'cross-device')), patch('shutil.rmtree', side_effect=interrupted_cleanup):
                try:
                    paths.migrate_legacy_backup_dir(target)
                except Exception as e:
                    print('migration first attempt:', type(e).__name__)
            assert not (legacy / 'credential.enc').exists()
            assert (target / 'credential.enc').exists()
            paths.migrate_legacy_backup_dir(target)
            assert not (target / 'credential.enc').exists()
            assert (target / 'roster.json').exists()
            print('CONFIRMED: retry destroyed sole credential copy in committed destination')


def removal():
    with tempfile.TemporaryDirectory(prefix='cswap-audit-') as d:
        root = Path(d)
        s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
        s.sequence_file = root / 'sequence.json'; s.lock_file = root / '.lock'
        s._logger = logging.getLogger('audit')
        s.sequence_file.write_text(json.dumps({'accounts': {'1': {'email': 'one@example.test', 'organizationUuid': ''}, '2': {'email': 'two@example.test', 'organizationUuid': ''}}, 'sequence': [1, 2], 'activeAccountNumber': 2}))
        def concurrent_update(_):
            # Deterministically schedule another writer while the remove prompt is waiting.
            data = json.loads(s.sequence_file.read_text())
            data['accounts']['2']['alias'] = 'new-alias'
            s.sequence_file.write_text(json.dumps(data))
            return 'y'
        with patch.object(s, '_refuse_session_shell'), patch.object(s, '_ensure_no_live_session'), patch.object(s, '_delete_account_files'), patch.object(s, '_prune_mappings'), patch('builtins.input', side_effect=concurrent_update):
            with FileLock(s.lock_file):
                s.remove_account('1')
                print('CONFIRMED: removal completed despite held global account lock')
        assert json.loads(s.sequence_file.read_text())['accounts']['2'].get('alias') is None
        print('CONFIRMED: removal lost concurrent update to unrelated account alias')


def purge(fail_delete=False):
    with tempfile.TemporaryDirectory(prefix='cswap-audit-') as d:
        root = Path(d)
        s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
        s.backup_dir = root / 'backup'; s.backup_dir.mkdir()
        s.credentials_dir = s.backup_dir / 'credentials'; s.credentials_dir.mkdir()
        s.sequence_file = s.backup_dir / 'sequence.json'
        s.platform = Platform.MACOS; s._logger = logging.getLogger('audit')
        s.sequence_file.write_text(json.dumps({'accounts': {'1': {'email': 'one@example.test'}}, 'sequence': [1]}))
        with patch.object(s, '_refuse_session_shell'), patch('claude_swap.switcher.get_legacy_backup_root', return_value=s.backup_dir), patch('claude_swap.switcher.macos_keychain.delete_password', side_effect=OSError('synthetic keychain locked') if fail_delete else None) as delete, patch('claude_swap.switcher._sweep_legacy_keyring'), patch('builtins.input', return_value='y'):
            s.purge()
            requested = [c.args for c in delete.call_args_list]
        assert not any(user.endswith('.prev') for _, user in requested)
        assert not s.backup_dir.exists()
        print('CONFIRMED: purge never requests .prev Keychain deletion; failed deletes=', fail_delete, '; roster still destroyed')

if __name__ == '__main__':
    migration()
    removal()
    purge()
    purge(fail_delete=True)

if __name__ == '__main__':
    import os
    import stat
    with tempfile.TemporaryDirectory(prefix='cswap-audit-') as d:
        s = ClaudeAccountSwitcher.__new__(ClaudeAccountSwitcher)
        target = Path(d) / '.claude.json'
        chmod_real = os.chmod
        observed = []
        def observe_permissions(path, mode, *args, **kwargs):
            observed.append(stat.S_IMODE(Path(path).stat().st_mode))
            assert 'synthetic-secret' in Path(path).read_text()
            return chmod_real(path, mode, *args, **kwargs)
        old_umask = os.umask(0o022)
        try:
            with patch('claude_swap.switcher.os.chmod', side_effect=observe_permissions):
                s._write_json(target, {'mcpServers': {'example': {'env': {'TOKEN': 'synthetic-secret'}}}})
        finally:
            os.umask(old_umask)
        assert observed == [0o644]
        print('CONFIRMED: full config temp initially 0644 before chmod; final=', oct(stat.S_IMODE(target.stat().st_mode)))
