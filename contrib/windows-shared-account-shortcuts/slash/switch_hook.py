"""Handle only the two explicitly invoked local Claude account commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ACCOUNTS = {
    "claude-account-a": ("1", "Account 1"),
    "claude-account-b": ("2", "Account 2"),
}
BASE = Path(__file__).resolve().parent.parent


def stop(message: str) -> dict:
    # Stop this command's model turn, leaving the interactive session open.
    return {"continue": False, "stopReason": message}


def account_problem() -> str | None:
    """Preflight shared by slash switching and PowerShell launchers."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir and Path(config_dir).expanduser().resolve() != (Path.home() / ".claude").resolve():
        return "目前使用獨立 Claude 設定目錄，此命令未切換帳號。"
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        file_env = settings.get("env", {})
        if not isinstance(file_env, dict):
            return "無法確認目前模型服務設定；未執行帳號切換。"
    except (OSError, ValueError, TypeError, AttributeError):
        return "無法讀取目前模型服務設定；未執行帳號切換。"
    sources = (os.environ, file_env)
    for source in sources:
        base_url = str(source.get("ANTHROPIC_BASE_URL", "")).strip().rstrip("/")
        if base_url and base_url != "https://api.anthropic.com":
            return "目前使用其他模型服務。請先在 CC Switch 選 Claude Official，再執行這個帳號命令。未切換帳號。"
    override_keys = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    if any(source.get(key) for source in sources for key in override_keys):
        return "目前有 API 金鑰或登入覆蓋設定。請先回到 Claude Official 並移除覆蓋，再切換官方帳號。未執行。"
    provider_keys = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
    if any(str(source.get(key, "")).lower() in ("1", "true") for source in sources for key in provider_keys):
        return "Cloud provider authentication is active; the subscription account was not changed."
    if settings.get("apiKeyHelper") or any(source.get("ANTHROPIC_PROFILE") for source in sources):
        return "Another credential source overrides subscription login. Check /status first; account unchanged."
    return None


def handle(payload: dict, runner=subprocess.run) -> dict | None:
    if payload.get("hook_event_name") != "UserPromptExpansion":
        return None
    name = str(payload.get("command_name", "")).lstrip("/").lower()
    if name not in ACCOUNTS:
        return None
    if str(payload.get("command_args", "")).strip():
        return stop(f"用法：/{name}（不需要參數）。未切換帳號。")
    problem = account_problem()
    if problem:
        return stop(problem)
    number, label = ACCOUNTS[name]
    executable = BASE / "venv" / "Scripts" / "cswap.exe"
    try:
        result = runner(
            [str(executable), "switch", number],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=75,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return stop("切換等待逾時，結果待確認。原對話保持開啟，請先核對登入狀態。")
    except OSError:
        return stop("無法啟動帳號切換工具；未確認切換成功。原對話保持開啟。")
    if result.returncode:
        return stop(f"帳號切換未完成（結束碼 {result.returncode}）。原對話保持開啟，請核對登入狀態。")
    return stop(f"已切換預設帳號至 {label}。原對話保留，直接傳送下一則訊息即可；會持續使用此帳號，直到你再次切換。")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    output = handle(payload)
    if output is not None:
        print(json.dumps(output, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
