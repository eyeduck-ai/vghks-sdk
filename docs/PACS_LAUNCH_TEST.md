# PACS 本機開啟專項驗證

此程式查詢操作者指定的一名病人的歷年醫囑，列出已確認有 PACS 按鈕的檢查，選一筆交接本機 SmartIris。它是 SDK 的使用者，與既有 `pacs` JPEG 下載計畫分開；一般測試計畫不會啟動本機檢閱器。

## 院內執行

將 `dist/vghks-pacs-launch-test.exe` 複製到已有 DownloadApp／SmartIris 的工作站。先確認常駐 DownloadApp 已由原系統啟動；測試器不負責安裝或啟動這兩個程式，也不會操作瀏覽器。

直接雙擊 EXE，或執行：

```powershell
.\vghks-pacs-launch-test.exe
```

1. 輸入 Portal 帳號、密碼及本次授權病歷號。密碼以隱藏輸入取得，不寫入設定或回傳檔；不需要自行尋找或輸入 `reqno`。
2. 程式正常登入一次，以同一 SDK Session 查詢近 4000 天的全部類別（`*`）與各科（`OR`）醫囑，合併重複清單。它按最新日期，每批確認最多 30 筆不同醫囑的明細／必要報告，僅列出實際回傳原生 PACS 按鈕的候選。
3. 核對畫面上的病歷號，以及清單中的日期、檢查名稱、科別與檢查單號。輸入清單編號後，程式先檢查本機 DownloadApp；確認可用才取得該筆的啟動資訊，並交接 SmartIris 一次。
4. 在 SmartIris 核對病人與檢查，再回到測試視窗填寫觀察結果。
5. 畫面顯示結果 ZIP 路徑後，按 Enter 關閉視窗。取消、沒有候選或失敗也會保存可取得的結果，並停留等待 Enter。

第一批未找到目標且還有醫囑未確認時，輸入 `m` 查下一批；目前已有候選會保留。輸入 `0` 或直接 Enter 可取消。每批保留 SDK 預設請求間隔，查詢可能需要等候。只有 JPG 按鈕及其他病人的參照不會成為可開啟候選；沒有直接 PACS 按鈕的明確未執行醫囑會略過。不會以醫囑 `seqNo` 猜測 PACS 檢查單號。

0.24.1 的本機預檢只讀取 Windows 的檔案、程序及 TCP 監聽資訊，不發 HTTP、不測試連線、不修改系統。預設以 `C:\TEDPCOCX\DownloadApp.exe` 作為指定程式，另列該位置的檔案與程序狀態；`AVAILABLE` 表示讀取到該映像路徑的程序持有 `127.0.0.1:15516` 的 IPv4 監聽埠，不代表協定健康。檔案存在只代表指定 DownloadApp 路徑可見，不證明另外的 SmartIris 檢閱器已安裝或影像可載入。

確認沒有監聽或由其他程序持有時，結果為 `BRIDGE_UNAVAILABLE`；權限不足等無法確認的情況為 `BRIDGE_UNCONFIRMED`，不能把未知當成未安裝。缺少指定檔案也會獨立列為 `MISSING`。兩種阻擋狀態都會保存檢查結果，停止本次操作，不取得 SmartWonder 啟動頁，也不送出本機交接。請先確認工作站的既有 DownloadApp 已正常啟動。預檢是當時的快照，後續程序若停止，交接仍可能失敗。

若只想取得啟動資訊，使用：

```powershell
.\vghks-pacs-launch-test.exe --prepare-only
```

這仍提供醫囑選單，選號後不檢查或呼叫 DownloadApp。取消、沒有可選候選、登入失敗及缺少必要輸入也不執行本機預檢。若已知 PACS 按鈕的 `reqno`，也可跳過清單：

```powershell
.\vghks-pacs-launch-test.exe --mrn <授權病歷號> --request-no <PACS按鈕reqno> --open-viewer
```

互動選單預設開啟檢閱器，選號就是本次交接操作，不會再要求重複確認。`--open-viewer` 可明確表示相同意圖；它與 `--prepare-only` 不能同時使用。直接指定 `--request-no` 時保留舊行為，無論是否互動，預設只取得資訊，須加上 `--open-viewer` 才交接本機。非互動模式必須指定 `--mrn` 及 `--request-no`。逾時或未知回覆不會自動重送。

收到本機 HTTP 回應後，請在 SmartIris 實際核對病人及檢查，再回答觀察結果：

| 輸入 | 記錄值 | 意義 |
| --- | --- | --- |
| `1` | `MATCHED` | 操作者確認指定病人、指定檢查影像已顯示 |
| `2` | `NOT_LOADED` | 影像尚未載入／未顯示 |
| `3` | `WRONG_STUDY` | 顯示的病人或檢查不符 |
| `0`／Enter | `NOT_CHECKED` | 沒有足夠畫面證據確認 |

`HANDOFF_SENT` 只表示本機交接序列取得 HTTP 回應。`native_result.viewer_loaded` 維持未知；人工觀察獨立保存，不能由 HTTP 200 推算影像完成。

若第一階段失敗，後續不會呼叫本機檢閱器。若本機中途失敗，後續步驟停止。Ctrl+C 會保存已完成階段並嘗試建立 ZIP；若在 `/cmove` 期間中斷，交接結果可能不明，請先查看 SmartIris，避免自行連續重按。

## 回傳資料

預設輸出在 EXE 旁的 `pacs-launch-results`；來源版則在執行目錄。每次獨立建立 `runs/<run_id>` 與 `archives/*.zip`，不覆寫舊回傳。也可以使用 `--output-root <目錄>`。

| 檔案 | 用途 |
| --- | --- |
| `run_summary.json` | 安全階段結果、`bridge_availability` 本機檢查狀態、錯誤碼、交接狀態、獨立人工觀察 |
| `test_plan.json` | 單一病人／所選檢查、歷年範圍、每批上限與不重試契約 |
| `run_config.json`、`private/selected_order.json`、`private/selected_candidate.json` | 查詢與所選病歷號、醫囑及檢查單號；不含密碼 |
| `private/pacs_discovery/` | 歷年清單、已查明細／報告、候選與逐步查詢結果 |
| `parsed/launch_context_summary.json` | 不含識別值與網址的 context 摘要 |
| `parsed/pacs_launch_execution.json` | 封裝前獨立保存的交接與人工觀察結果；ZIP 失敗時仍可從原始目錄取得 |
| `requests/`、`responses/`、`capture_manifest.jsonl` | 醫囑探索頁、`qryExternalAP.jsp` 及 `vwrIris.jsp` 的原始請求／回應 |
| `private/native/*.json`、`*.body` | DownloadApp 每次請求網址、回應標頭與原始回應 bytes，包括原先 HAR 缺少的兩個位元組 |
| `diagnostics/`、`errors.jsonl` | 安全診斷與操作對照 |

原始頁面、Cookie、UID 及本機請求網址屬敏感資料。ZIP 未加密，只留院內／授權的本機回傳流程，不加入公開 Git、Issue 或 CI artifact。此專項只保存醫囑探索及兩個 PACS 頁面的原始 HTTP，不保存 Portal 密碼 POST；登入與 SSO 仍保留安全診斷。

`run_summary.status` 的 `PREPARED` 表示完成取得資訊，`HANDOFF_SENT` 表示完成本機 HTTP 交接。`NO_SAMPLE` 表示掃描完畢仍無可用按鈕，`CANCELLED` 表示操作者取消。清單中仍有未掃醫囑時，`discovery.pending` 保留數量，成功交接的封裝狀態為 `COMPLETED_WITH_GAPS`；這不表示已查完所有醫囑。

個別明細／報告失敗會保留錯誤，其他已確認候選仍可選擇。若其後交接成功，狀態為 `HANDOFF_SENT_WITH_GAPS`、程式結束碼為 `2`、封裝為 `COMPLETED_WITH_ERRORS`，同時保留 `handoff=HANDOFF_SENT` 與人工觀察。只取得資訊時對應 `PREPARED_WITH_GAPS`。沒有候選且探索失敗為 `DISCOVERY_FAILED`；來源報告解析有缺漏、仍無可確認候選則為 `DISCOVERY_INCOMPLETE`，不視為已確認沒有 PACS。`bundle_status` 不取代所選檢查的人工影像核對。

## 來源版、建置及離線驗證

以 wheel 安裝 SDK 後，也可以使用 `vghks-pacs-launch-test`，或 `python -m vghks_sdk.live.pacs_launch`；這些入口使用同一個測試流程。`--plan` 與 `--self-check` 不連線、不停留等待 Enter；`--interactive` 可讓重導 stdin 的合成測試明確啟用選單。

```powershell
python run_pacs_launch_test.py --plan --interactive
python run_pacs_launch_test.py --self-check
python run_pacs_launch_test.py
python run_tests.py --pattern test_pacs_launch_live.py
python tools/verify_pacs_launch_exe.py --source
python tools/build_pacs_launch_exe.py
python tools/verify_pacs_launch_exe.py
```

EXE 建置固定 Windows x64、Python 3.10、PyInstaller 6.14.2、truststore 0.10.4，不內嵌帳密或私有預設。來源版／EXE 的 localhost verifier 使用合成登入、歷年清單、明細、初始頁面與臨時埠上的橋接服務，不接觸院內及真實 `15516`。保留只取得資訊、完整交接、登入失敗、病人不符、缺 UID、上游轉址、本機轉址、本機 HTTP 失敗及回應中斷；另外涵蓋選單開啟、空清單、取消、錯誤選號後重選、部分探索失敗、跨病人參照、只取得資訊、分批續查、未掃完即選擇，以及非互動缺少檢查單號。

0.24.1 另加入缺少指定程式及監聽埠由其他程式持有兩種情境；以 verifier 自己的 Python 程序及臨時埠核對 Windows 監聽擁有者，不要求測試機安裝 DownloadApp，也不使用真實 `15516`。來源版及實際 EXE 各 21 種情境已通過；這些檢查屬合成／localhost 證據，不能當成院內新版預檢成功。

## 離線分析回傳

0.24.2 起可使用 SDK 的通用離線入口分析此專項的 ZIP 或原始 run 目錄：

```powershell
python -m vghks_sdk analyze-bundle --input <回傳ZIP或run目錄> --output output/pacs-analysis
```

分析只讀取本機存檔，不登入、不重送院方或 DownloadApp 請求。它核對醫囑探索、所選來源、準備頁配對與原生交換，再分別呈現本機可用性、HTTP 交接和人工影像觀察。`analysis.json`／`analysis.md` 的分析狀態不覆寫原始實測結果；未掃描醫囑是缺口，缺檔、參照矛盾或實際失敗則另列問題。較舊 0.24.0 沒有預檢紀錄，不由後續成功倒推為 `AVAILABLE`。

這個專項的 `retest-config.json` 只供人工檢閱，不含病人參照，也不授權自動登入或交接。重新開啟檢查仍須由操作者在測試器選擇。完成分析後，回傳保留／清理依 [本機資料整理](DEVELOPMENT.md#本機資料整理) 與當次授權辦理。

## 已有院內證據與新版範圍

0.24.0 的兩份院內回傳包含一次明確登入拒絕，以及一次完整的選單、啟動資訊、原生交接與人工核對成功。拒絕回傳未保存 Portal 原頁，無法由 ZIP 回推具體拒絕原因；該次未執行後續查詢或本機交接。

成功回傳的兩類清單共 144 筆來源列，合併為 112 筆不同醫囑；第一批已查 30 筆並確認 30 個 PACS 候選，操作者選擇其中一筆。兩份清單及 30 份明細的禁止 socket 重解析與保存模型一致，候選及所選啟動參照均與原頁綁定相符。剩餘 82 筆未掃描是封裝 `COMPLETED_WITH_GAPS` 的原因，已執行範圍的查詢及解析錯誤數為 0。

四個本機回應均為 HTTP 200，完整 body 都是兩個 ASCII 位元組 `ok`；操作者另記錄 `MATCHED`。這證明本次 SDK 流程及指定檢查的人工核對成功，沒有使用瀏覽器。它不代表未掃描醫囑、其他工作站或影像類型均已驗證；0.24.1 的後續回傳另確認 DownloadApp 預檢，見下段。

0.24.1 後續兩份院內回傳皆為 OK：每份兩類清單共 14 來源列／7 筆不同醫囑，7 筆全數掃描並確認 PACS 按鈕，無待掃描或解析缺漏。DownloadApp 的檔案、程序、監聽及持有者均符合設定；同一病人選定兩個不同檢查，四步交接及人工 MATCHED 均成功。這是當次所選檢查的證據，不表示七筆影像都曾開啟。
