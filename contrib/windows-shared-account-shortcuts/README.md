# Windows 共用帳號短命令

這是 claude-swap 的外層整合範例，基準為 upstream v0.27.2（6d47eb1d5800e1979b0087a01159c4295015f3c5）。它使用既有 `cswap switch`，沒有修改 claude-swap 的核心認證或額度機制。

提供兩種入口：

| 入口 | 帳號 1 | 帳號 2 |
| --- | --- | --- |
| Claude 對話內切換 | `/claude-account-a` | `/claude-account-b` |
| PowerShell 選號後啟動 | `claude-account-a` | `claude-account-b` |

## 前提與安裝

- Windows、Python 3.12 以上，以及可在 CMD 執行 `claude` 的官方 Claude Code。
- 使用自己已授權的兩個帳號。帳號 1／2 指 cswap 保存的編號，並不固定訂閱方案。
- 使用預設 `.claude` 設定目錄。安裝會備份既有設定並新增命令、窄範圍 hook、使用者 bin 轉接檔及缺少的使用者 PATH 項目。
- 將本目錄保留在固定位置，因為已安裝入口會引用此處；不要把本機 venv 或帳號備份提交到版本庫。

在本目錄執行：

```powershell
py -3 -m venv venv
.\venv\Scripts\python.exe -m pip install "claude-swap==0.27.2"
```

先依上游流程登入並以 `cswap.cmd add` 保存兩個帳號，再執行：

```powershell
.\Install-Claude-Slash.cmd
if ($LASTEXITCODE -eq 0) { $env:Path += ";$env:USERPROFILE\bin" }
```

在原 Claude 視窗執行 `/reload-skills`，再使用上表命令。PowerShell 入口可傳遞原生 Claude 參數，例如 `claude-account-b --resume`。

## 行為與限制

- 兩個入口都更換同一套共用預設登入；現有共用設定視窗的下一則訊息也會受影響，並非每視窗獨立固定帳號。
- 斜線 hook 在本機完成切換並停止該次命令送往模型，不會退出原對話。保留 session 不代表已送出的請求能轉移帳號，失敗後可能仍須重送。
- 非官方服務網址、可辨識的憑證覆蓋、自訂設定目錄等狀態會停止切換；實際認證來源仍應以 `/status` 核對。
- 沒有加入自動額度輪換、代理服務、模型改寫或取消安全提示。不能保證第三方憑證管理獲 Anthropic 核准或沒有風控風險。
- 在已有其他 provider 管理器時，須由其設定保留此 hook；範例不會替 CC Switch 改寫公共配置。
- `--check` 不切換帳號、不啟動 Claude。使用者 PATH 屬於實際程序的 Windows 身分；沙箱帳戶的 HKCU 不能代表互動使用者的 PATH。

```powershell
.\Install-Claude-Slash.cmd --check
.\claude-account-a.cmd --check
.\Install-Claude-Slash.cmd --uninstall
```

解除安裝只移除仍與本整合相符的檔案與 hook，以及本安裝器實際新增的 PATH 項目，保留帳號備份和後來新增的其他 PATH 項目。編輯過的同名檔案會保留並停止操作。

## 驗證與出處

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

測試使用隔離檔案及記憶體登錄替身，不使用真實 OAuth 憑證。個人化原型曾在 Windows 上通過 44 項隔離檢查；使用者另實測 B 接續原對話、A 短命令切換及 Claude 啟動。本通用版另以 Windows / Python 3.14.4 執行 10 項 unittest，全部通過；測試含實際 A／B 批次入口及假下游程式，沒有真實切換帳號或啟動 Claude。

核心來源與授權：[realiti4/claude-swap](https://github.com/realiti4/claude-swap)，MIT。官方參考：[Authentication](https://code.claude.com/docs/en/authentication)、[Hooks](https://code.claude.com/docs/en/hooks)、[訂閱與第三方工具](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account)。
