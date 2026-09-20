# GitHub、wheel 與發布

來源 repo：<https://github.com/eyeduck-ai/vghks-sdk>，MIT 授權。GitHub 管理原始碼、文件及版本；wheel 是可供其他軟體安裝的套件，不包含自用 EXE 或測試病人資料。

```sh
python -m pip install "vghks-sdk @ git+https://github.com/eyeduck-ai/vghks-sdk.git@v0.18.0"
# 或安裝已建置 wheel
python -m pip install vghks_sdk-0.18.0-py3-none-any.whl
```

| 方式 | 適合用途 | 需求 |
| --- | --- | --- |
| Git 來源／固定 tag | 直接引用、參與開發、追蹤版本 | Git、Python、相依套件，安裝時建置 |
| wheel | 固定版本部署、搬入內網 | Python 3.10+ 與依賴，不需 Git，也不用重建 SDK |
| EXE | 院內一次測試及帶回 debug | Windows x64，不能作 Python library 匯入 |

目前 wheel 為純 Python `py3-none-any`，仍需 requests、beautifulsoup4，以及 Windows 的 truststore。完全離線安裝時，在與目標相符的 Python／OS 環境先準備依賴：

```sh
python -m pip download --only-binary=:all: --dest wheelhouse vghks_sdk-0.18.0-py3-none-any.whl
python -m pip install --no-index --find-links wheelhouse vghks-sdk==0.18.0
```

## 發布流程

1. 更新 `_version.py`、CHANGELOG 與公開文件；執行 tests、ruff、API 文件同步檢查。
2. 只 stage 公開目錄，執行 `python tools/check_public_tree.py --staged`，實際掃描 index 內容。
3. `python -m build --outdir output/package`，對 wheel／sdist 各執行 `--archive` 檢查並在獨立目錄安裝驗證。
4. 推送經核對的 commit／tag。CI 僅使用合成資料；不傳入醫院帳密或 raw capture。
5. 需要 Release wheel 時可從相同 tag 建置後發布；自用 EXE 有私有參數，不能附到公開 Release。

公開清單採白名單並另做敏感值檢查。`.gitignore` 只阻止日後新增，不能刪除既有 Git 歷史；若發現敏感資料誤推，停止繼續散布並依 [SECURITY](../SECURITY.md) 處理，不能只刪最新檔案就認為解決。

套件格式及建置依據：[Python Packaging 官方指南](https://packaging.python.org/en/latest/tutorials/packaging-projects/)。CI 使用 Python 版本矩陣與唯讀權限，參考 [GitHub 官方指南](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)。
