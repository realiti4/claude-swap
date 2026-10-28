"""Security boundaries, tested exclusively with fake credentials and key stores."""
import base64
import os
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from claude_swap import secret_files
ORIGINAL_BACKEND = secret_files._backend
from claude_swap.backup_crypto import encrypt_backup, decrypt_backup, prompt_password
from claude_swap.exceptions import CredentialReadError, CredentialWriteError, SessionError, TransferError
from claude_swap.locking import FileLock
from claude_swap.models import Platform
from claude_swap.session import SessionManager, keychain_service_name, session_dir_for
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import export_accounts, import_accounts
from tests.test_transfer import _linux_switcher, _seed_account


def test_file_backup_encrypts_authenticates_and_binds_filename(tmp_path):
    path = tmp_path / 'account.enc'
    secret = '{"refreshToken":"secret-refresh"}'
    secret_files.write_secret(path, secret)
    text = path.read_text()
    assert text.startswith(secret_files.PREFIX)
    assert secret not in text and 'secret-refresh' not in text
    assert secret_files.unseal(path, text) == secret
    with pytest.raises(CredentialReadError):
        secret_files.unseal(tmp_path / 'other.enc', text)
    altered = text[:-5] + 'AAAAA'
    with pytest.raises(CredentialReadError):
        secret_files.unseal(path, altered)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_key_store_failure_never_overwrites_existing_backup(tmp_path, monkeypatch):
    path = tmp_path / 'account.enc'
    secret_files.write_secret(path, 'old-secret')
    previous = path.read_bytes()
    def unavailable():
        raise RuntimeError('potentially-secret-backend-error')
    monkeypatch.setattr(secret_files, '_backend', unavailable)
    with pytest.raises(CredentialReadError, match='refusing unencrypted') as exc:
        secret_files.write_secret(path, 'new-secret')
    assert 'potentially-secret' not in str(exc.value)
    assert path.read_bytes() == previous


def test_lost_key_does_not_generate_replacement(tmp_path, block_real_keychain):
    path = tmp_path / 'a.enc'
    secret_files.write_secret(path, 'secret')
    block_real_keychain.data.clear()
    with pytest.raises(CredentialReadError, match='unavailable'):
        secret_files.unseal(path, path.read_text())
    assert block_real_keychain.data == {}


def test_plaintext_keyring_is_rejected(monkeypatch):
    import keyring
    monkeypatch.setattr(secret_files.sys, 'platform', 'linux')
    monkeypatch.setattr(keyring, 'get_keyring', lambda: object(), raising=False)
    with pytest.raises(CredentialWriteError, match='OS credential store'):
        ORIGINAL_BACKEND()


def test_legacy_migration_is_idempotent_and_preserves_values(temp_home):
    s = _linux_switcher(temp_home)
    path = s.credentials_dir / '.creds-1-user@example.com.enc'
    prev = path.with_suffix('.enc.prev')
    config = s.configs_dir / '.claude-config-1-user@example.com.json'
    for p in (path, prev):
        p.write_text(base64.b64encode(b'legacy-secret').decode())
    config.write_text('{"primaryApiKey":"legacy-key"}')
    assert secret_files.migrate_backups(s) == 3
    assert secret_files.migrate_backups(s) == 0
    assert secret_files.unseal(path, path.read_text()) == 'legacy-secret'
    assert secret_files.unseal(prev, prev.read_text()) == 'legacy-secret'
    assert 'legacy-key' not in config.read_text()
    assert json.loads(s.read_account_config('1', 'user@example.com'))['primaryApiKey'] == 'legacy-key'


def test_credential_read_marks_missing_key_unreadable(temp_home, block_real_keychain):
    s = _linux_switcher(temp_home)
    s._write_account_credentials('1', 'user@example.com', 'secret')
    block_real_keychain.data.clear()
    assert s._store._read_account_credentials_ex('1', 'user@example.com') == ('', True)


@pytest.mark.parametrize('password', ['wrong password', ''])
def test_export_authentication_rejects_wrong_password(password):
    data = encrypt_backup({'version': 1, 'accounts': [{'token': 'secret'}]}, 'correct password')
    with pytest.raises(TransferError):
        decrypt_backup(data, password)


def test_backup_uses_random_salts_and_no_plaintext():
    payload = {'version': 1, 'accounts': [{'token': 'secret'}]}
    first = encrypt_backup(payload, 'a long password')
    second = encrypt_backup(payload, 'a long password')
    assert first != second
    assert 'secret' not in json.dumps(first)
    assert decrypt_backup(first, 'a long password') == payload
    first['algorithm'] = 'attacker-controlled-kdf'
    with pytest.raises(TransferError, match='Unsupported'):
        decrypt_backup(first, 'a long password')


def test_default_export_is_encrypted_and_import_is_atomic_on_wrong_password(temp_home):
    source = _linux_switcher(temp_home)
    _seed_account(source, 1, 'user@example.com')
    destination = temp_home / 'backup.cswap'
    export_accounts(source, str(destination), password='a long password')
    data = json.loads(destination.read_text())
    assert data['encrypted'] is True and 'accounts' not in data
    assert 'user@example.com' not in destination.read_text()
    before = source.sequence_file.read_bytes()
    with pytest.raises(TransferError):
        import_accounts(source, str(destination), force=True, password='wrong')
    assert source.sequence_file.read_bytes() == before
    import_accounts(source, str(destination), force=True, password='a long password')


def test_password_prompt_refuses_echo_fallback(monkeypatch):
    import getpass
    import warnings
    def fallback(*args, **kwargs):
        warnings.warn('echo', getpass.GetPassWarning)
        raise AssertionError('must not read input')
    monkeypatch.setattr(getpass, 'getpass', fallback)
    with pytest.raises(TransferError, match='interactive terminal'):
        prompt_password()


def test_nested_run_refuses_before_resolving_or_refreshing(monkeypatch):
    manager = SessionManager(SimpleNamespace(backup_dir=Path('/unused'), _logger=Mock()))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', '/another/session')
    with patch('shutil.which', return_value='/fake/claude'):
        with pytest.raises(SessionError, match='Nested'):
            manager.run('2', [])


def test_auto_rechecks_reserved_profile_before_switch(temp_home):
    from claude_swap.autoswitch import AutoSwitchEngine, TickOutcome
    s = _linux_switcher(temp_home)
    _seed_account(s, 1, 'user@example.com')
    from claude_swap.settings import AutoSwitchSettings
    engine = AutoSwitchEngine(s, AutoSwitchSettings(), lambda event: None)
    directory = session_dir_for(s.backup_dir, '1', 'user@example.com')
    directory.mkdir(parents=True)
    with patch.object(s, 'switch_to') as switch:
        assert engine._perform('1', 'user@example.com', 'proactive', (None, float('inf'))) == TickOutcome.NO_ACTION
        assert engine._freshen_target('1', 'user@example.com') == 'skip-live-session'
        switch.assert_not_called()


def test_macos_bootstrap_does_not_seed_plaintext(temp_home, block_real_keychain):
    s = _linux_switcher(temp_home)
    _seed_account(s, 2, 'user@example.com')
    s.platform = Platform.MACOS
    manager = SessionManager(s)
    directory = session_dir_for(s.backup_dir, '2', 'user@example.com')
    manager._bootstrap(directory, '2', 'user@example.com', '')
    assert not (directory / '.credentials.json').exists()
    value = block_real_keychain.get_password(
        keychain_service_name(str(directory)),
        __import__('claude_swap.macos_keychain', fromlist=['']).keychain_account_name(),
    )
    assert value and 'tok-1' in value


def test_macos_active_write_refuses_plaintext_fallback(temp_home, monkeypatch):
    s = _linux_switcher(temp_home)
    s.platform = Platform.MACOS
    monkeypatch.setattr(s._store, '_use_keychain', lambda: False)
    from claude_swap.exceptions import CredentialWriteError
    with pytest.raises(CredentialWriteError, match='refusing plaintext'):
        s._store._write_oauth_credentials('secret')
    with pytest.raises(CredentialWriteError, match='refusing plaintext'):
        s._store._write_managed_credentials('sk-ant-api03-secret')
    assert not (temp_home / '.claude' / '.credentials.json').exists()


def test_manual_activation_rechecks_reservation_under_store_lock(temp_home, monkeypatch):
    from claude_swap.exceptions import SwitchError
    s = _linux_switcher(temp_home)
    _seed_account(s, 1, 'user@example.com')
    directory = session_dir_for(s.backup_dir, '1', 'user@example.com')
    def reserve_during_prefetch():
        directory.mkdir(parents=True, exist_ok=True)
        (directory / '.cswap-session-reservation').write_text('reserved\n')
        return {'live': None, 'resolved': None}
    monkeypatch.setattr(s, '_prefetch_live_identity', reserve_during_prefetch)
    with patch.object(s, '_write_credentials') as write:
        with pytest.raises(SwitchError, match='reserved'):
            s._perform_switch('1', emit_output=False)
        write.assert_not_called()


def test_writing_after_key_loss_cannot_replace_key_or_backup(tmp_path, block_real_keychain):
    path = tmp_path / 'old.enc'
    secret_files.write_secret(path, 'secret')
    before = path.read_bytes()
    block_real_keychain.data.clear()
    with pytest.raises(CredentialReadError, match='refusing to replace'):
        secret_files.write_secret(tmp_path / 'new.enc', 'new-secret')
    assert path.read_bytes() == before
    assert block_real_keychain.data == {}
    assert not (tmp_path / 'new.enc').exists()


def test_session_seed_excludes_mcp_and_device_tokens(temp_home):
    s = _linux_switcher(temp_home)
    _seed_account(s, 2, 'user@example.com', creds={
        'claudeAiOauth': {'accessToken': 'own-login'},
        'mcpOAuth': {'token': 'another-profile-secret'},
        'trustedDeviceToken': 'device-secret',
    })
    manager = SessionManager(s)
    directory = session_dir_for(s.backup_dir, '2', 'user@example.com')
    manager._bootstrap(directory, '2', 'user@example.com', '')
    assert json.loads((directory / '.credentials.json').read_text()) == {
        'claudeAiOauth': {'accessToken': 'own-login'}
    }


def test_reserved_profile_prevents_background_token_consumption(temp_home):
    s = _linux_switcher(temp_home)
    _seed_account(s, 2, 'user@example.com')
    directory = session_dir_for(s.backup_dir, '2', 'user@example.com')
    directory.mkdir(parents=True)
    (directory / '.cswap-session-reservation').write_text('reserved\n')
    with patch('claude_swap.oauth.try_refresh_oauth_credentials') as refresh:
        outcome = s.consume_backup_grant('2', 'user@example.com', 'old-snapshot')
        assert outcome.error == 'session-owned'
        refresh.assert_not_called()


def test_rejected_keyring_during_decryption_is_unreadable(temp_home, monkeypatch):
    s = _linux_switcher(temp_home)
    s._write_account_credentials('1', 'user@example.com', 'secret')
    def rejected_backend():
        raise CredentialWriteError('OS credential store required')
    monkeypatch.setattr(secret_files, '_backend', rejected_backend)
    assert s._store._read_account_credentials_ex('1', 'user@example.com') == ('', True)
