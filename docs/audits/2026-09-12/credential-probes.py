"""Synthetic security probes. Run with PYTHONPATH=src .venv/bin/python /tmp/claude_swap_credential_audit_probes.py.
No real secrets are loaded; subprocess/network are blocked except for mock argument capture.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request
from unittest.mock import patch
from claude_swap import macos_keychain
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import import_accounts

payload = json.dumps({'claudeAiOauth': {'accessToken':'SYNTHETIC_ACCESS', 'refreshToken':'SYNTHETIC_REFRESH'}, 'pluginSecrets': {'padding': 'x'*2200}})
with patch.object(macos_keychain.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'','')) as proc:
    macos_keychain.set_password('claude-swap','account-1-synthetic@example.com',payload)
    argv = proc.call_args.args[0]
    recovered = bytes.fromhex(argv[argv.index('-X')+1]).decode()
    assert recovered == payload
    print('PASS exposure reproduced: full',len(payload),'byte synthetic credential recoverable from argv')
request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage',headers={'Authorization':'Bearer SYNTHETIC_ACCESS'})
redirect = urllib.request.HTTPRedirectHandler().redirect_request(request,None,302,'Found',{},'http://other.invalid/collect')
assert redirect.get_header('Authorization') == 'Bearer SYNTHETIC_ACCESS' and redirect.type == 'http'
print('PASS redirect policy reproduced: HTTPS Authorization forwarded to different-origin HTTP URL (no request sent)')

with tempfile.TemporaryDirectory(prefix='cswap-import-review-') as td:
    root = Path(td)
    # Patch every path source before constructing the switcher. No shell env HOME changes.
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch('pathlib.Path.home', return_value=root))
        stack.enter_context(patch.object(Platform,'detect',return_value=Platform.LINUX))
        stack.enter_context(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR':'', 'CLAUDE_SECURESTORAGE_CONFIG_DIR':'', 'XDG_DATA_HOME':str(root/'data')}))
        stack.enter_context(patch('subprocess.run',side_effect=AssertionError('External process forbidden by audit probe')))
        stack.enter_context(patch('urllib.request.urlopen',side_effect=AssertionError('Network forbidden by audit probe')))
        s=ClaudeAccountSwitcher()
        assert s.backup_dir.is_relative_to(root)
        injected={'review-only': {'type':'stdio','command':'/AUDIT_DO_NOT_EXECUTE','args':['synthetic']}}
        envelope={'version':1,'encrypted':False,'activeAccountNumber':1,'accounts':[{'number':1,'email':'synthetic@example.com','uuid':'synthetic','credentials':{'claudeAiOauth': {'accessToken':'SYNTHETIC','refreshToken':'SYNTHETIC_REFRESH','expiresAt':4102444800000}},'config': {'oauthAccount': {'emailAddress':'synthetic@example.com','accountUuid':'synthetic','organizationUuid':''},'mcpServers':injected}}]}
        source=root/'untrusted.cswap'
        source.write_text(json.dumps(envelope))
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            import_accounts(s,str(source))
            s._perform_switch('1',force_activate=True,emit_output=False)
        config=s._get_claude_config_path()
        assert config.is_relative_to(root)
        assert json.loads(config.read_text())['mcpServers']==injected
        print('PASS import capability reproduced: ordinary import + activation installs arbitrary mcpServers on fresh profile, no --full/import trust flag; command was NOT executed')
