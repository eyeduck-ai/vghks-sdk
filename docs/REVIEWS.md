# 審查系統查詢 API

`sdk.reviews` 用同一個 Requests Session 連接 PCK 審查系統。它與 `sdk.audit` 的未簽病歷查詢是不同功能。此模組只提供查詢。

## 登入方式與證據

本次獨立登入 HAR 實際經過 Portal OAuth：`/Pck/HISLogin` → Portal `/oauth2Server.do` → `/oauth2ServerLogin.do` → PCK callback，最後建立 `HIS_IPD` Cookie。案件 API 使用 Cookie，沒有錄到需要額外 Bearer token 的請求。

SDK 先完成原有 Portal 登入，再開啟審查系統的 OAuth。若 Portal Session 可直接通過，沿用即可；若顯示 OAuth 登入表單，使用本次同一組 Portal 帳密與新頁面取得的隱藏欄位登入，不需要另提供審查帳密。

`sdk.reviews.get_login_info().authentication_mode` 會記錄：

| 值 | 實際路徑 |
| --- | --- |
| `portal_session` | OAuth 直接導回審查系統，沒有額外提交帳密 |
| `portal_credentials` | OAuth 要求登入，使用同一組帳密提交新表單 |
| `existing_review_session` | 本次 Session 已有可用的審查登入狀態 |

成功必須同時取得有效登入資訊與審查 Cookie，不能只看 HTTP 200。重新登入會清除舊狀態並重取 OAuth 欄位；不使用 HAR 的密碼、Cookie、code 或 state。HAR 中 callback 的 HTTP 網址會先升級成 HTTPS 才送出請求，對應錄製瀏覽器的 HSTS 行為；OAuth `redirect_uri` 欄位仍保留伺服器原值。

**2026-09-20 內網測試已確認 `portal_session`，可共用 Portal 登入。** OAuth 需重新輸入帳密的分支另有合成測試；表單欄位依新頁面讀取。若內網表單不同，會保留原始回應並回報 `REVIEW_OAUTH_FORM_*`，不猜填驗證碼或重播舊 code。

## 八個可組合的原子查詢

| 方法 | 結果 |
| --- | --- |
| `get_login_info()` | 登入資訊、實際認證路徑 |
| `get_options()` | 可選科別、醫師、案件與審查狀態等選項 |
| `get_doctors(department)` | 該科別可選醫師 |
| `get_cases(ReviewCaseFilter(...))` | 條件內全部案件清單 |
| `get_case(ref)` | 一件案件的主資料、申請內容與原始欄位 |
| `get_orders(ref)` | 該案醫囑及逐項審查資訊 |
| `get_attachments(ref)` | 該案附件清單與檔案中繼資料 |
| `get_pacs(ref)` | 該案 PACS 清單與影像中繼資料 |

清單中的 `reference` 是 `ReviewCaseRef`，含完整 `apply_seq` 字串，保留前導零。四種案件明細都可直接用這個參照呼叫，不依賴前一種明細成功。

`ReviewCaseFilter` 支援 `doctor_card`、`department`、`mrn`、`verify_code`、`apply_mode`、`start_date`、`end_date`；日期用 Python `date`，轉為錄製前端使用的西元 `YYYYMMDD`。至少提供一項條件。HAR 實際送過醫師＋科別條件；其他條件來自錄製的前端程式，仍需內網驗證。

```python
from datetime import date
from vghks_sdk import ReviewCaseFilter

# sdk 是已建立的 VghksSDK；username 是本次登入帳號。
cases = sdk.reviews.get_cases(ReviewCaseFilter(
    doctor_card=username,
    start_date=date(2026, 1, 1),
    end_date=date(2026, 9, 20),
))
for case in cases:
    detail = sdk.reviews.get_case(case.reference)
    orders = sdk.reviews.get_orders(case.reference)
    # 接續取得附件／PACS，或由上層流程各自捕捉 SDKError、保存結果。
```

`ReviewCase.fields` 保留原始業務欄位；`ReviewCasePart.rows` 保留各清單的原始列。醫囑中的 `VerifyCode`、`VerifyQty`、`VerifyRSN1/2`、`VerifyText` 等資訊由此讀取；有案件層級與醫囑層級兩種結果，不把兩者互相取代。附件／PACS 本次只錄到清單，尚未提供此模組的附件二進位下載。

錄製頁面採瀏覽器內分頁，`Data` 已包含 `Total` 件。SDK 檢查兩者相符、案件 id 不重複、明細案件 id 與輸入相符；截斷、身分不符、後端錯誤或未知頁面均回報錯誤，不當成空結果。未來若後端改採伺服器分頁，需依新錄製補上流程。

## 審查結果與送件狀態

| `verify_code` | `review_label` | `approved` |
| --- | --- | --- |
| `0` | 審查中 | `None` |
| `1` | 同意備查 | `True` |
| `2` | 不予同意 | `False` |
| `3` | 部分同意 | `None` |
| `4` | 補件 | `None` |
| `5` | 退件 | `None` |
| `7` | 改核 | `None` |
| 其他／空值 | 未辨識 | `None` |

`application_status` 對應 `ApplyStatus`，例如 `Y` 是案件完成、`T` 是暫存。`processing_status` 對應 `ApplyFinishFlag`，例如 `F` 是上傳完成。兩者均不能表示已審查通過。狀態標籤來自此 HAR 的選項；未知值保留原碼。

## 內網 EXE 測試

雙擊 EXE 直接使用內建參數，包含病人、眼科報告、兩條手術紀錄路徑、審查等 55 個唯讀操作，以及業績／專勤工作獎金兩份 MIS 報表。薪資的身分證字號與密碼在執行時輸入，不限筆數的每週 SOAP 組合流程預設關閉。

不用搬設定檔，旁置舊檔不會覆蓋雙擊預設。開發時若只測審查系統，可明確執行 `vghks-live-test.exe --config configs/review-system.example.json`。

審查預設使用登入帳號作為醫師，不限制申請日期。若可選科別只有一個，帶入該科別；多科別時不任選一個。完整清單全部保存，依審查結果與年份分組抽樣最多 8 件，每件分別查主資料、醫囑、附件、PACS。可用 `max_items` 調整 1–100；某件或某項失敗仍繼續其他項。

開發時如需指定條件，可在透過 `--config` 明確載入的設定內增加：

```json
{
  "review_query": {
    "doctor_card": "你的卡號",
    "start_date": "2026-01-01",
    "end_date": "2026-09-20"
  }
}
```

明確提供 `review_query` 時，不會自動追加登入醫師條件。`review_query` 是篩選條件，不接收密碼。實際採用條件在 ZIP 的 `parsed/inputs/review.cases.json`。

每種回應獨立保存於 `parsed/atomic/review.<操作>/0001.json` 等檔案；`review.login_info` 保留登入路徑。HTTP 原文在 `responses/`，`capture_manifest.jsonl` 可對照失敗的請求。ZIP 不加密、沒有 `.sha256`，放在 EXE 同目錄並含輸出時間；帶回 ZIP 即可。

## 目前驗證

HAR 離線解析涵蓋案件與四種明細；2026-09-20 內網回傳進一步確認 OAuth 共用登入、完整案件清單及八件抽樣的各項明細。有些案件的附件／PACS 合法為空。

合成測試與 localhost HTTPS EXE 驗證另涵蓋同帳密 OAuth、新表單欄位、HTTP callback 升級、Session 過期後復原、錯誤案件續跑、空 PACS 清單、檔案輸出及離線回放。成功樣本不表示每種篩選條件、權限與所有案件皆已驗證。
