# 人事／醫師目錄

`sdk.personnel` 使用本次 HAR 的「查詢醫師資料」入口，沿用 Portal 帳密與 SSO，不需要另一組密碼或瀏覽器。查詢範圍是該入口提供的人員（含畫面列出的助理職稱），不代表已串接所有人事系統。

## 原子功能

| 呼叫 | 用途與回傳 |
| --- | --- |
| `get_options()` | `PersonnelOptions`：當次可用的 `titles`、`units`；每個選項有 `value` 與 `label` |
| `search(PersonnelFilter(...))` | 依多條件送出伺服器查詢，回傳 `list[PersonnelRecord]`；不擅自限制筆數 |
| `get_by_card(card_no)` | 以員工帳號查詢，再精確核對 `employee_id`；回傳一筆或 `None`，不取模糊搜尋的第一筆 |

`PersonnelFilter` 的 `name`、`employee_id`、`title`、`unit` 可單用或同時送出；`include_subunits=True` 對應「包含下層單位」，須同時指定 `unit`。姓名與員工編號遵守表單的 10 字／6 字上限。姓名是否部分匹配由伺服器決定，需要唯一身分時使用 `get_by_card`。

```python
from vghks_sdk import PersonnelFilter

# sdk 為已建立的 VghksSDK；每個動作按需登入。
options = sdk.personnel.get_options()
people = sdk.personnel.search(PersonnelFilter(
    name="欲搜尋姓名",
    title="D",       # 當次 options 中的主治醫師代碼
    unit="450",      # 當次 options 中的眼科部代碼
    include_subunits=True,
))
people = sdk.personnel.search(PersonnelFilter(employee_id=employee_id))
```

職稱／單位選項由表單動態讀取，查詢前確認代碼仍存在。未指定條件會拋出 `PERSONNEL_FILTER_REQUIRED`；明確需要全目錄時使用 `PersonnelFilter(allow_all=True)`。`allow_all` 是本機防止意外查全表的選項，不是伺服器參數。

## 卡號、姓名與就診紀錄

**員工編號 `employee_id` 與醫師章號 `doctor_stamp_no` 是不同欄位。** 不把章號當成登入帳號。`get_by_card` 接受員工帳號，另支援已觀察到的「四碼帳號 + F」門診卡號別名；只有該格式會移除 F，其他代碼不截短。其他系統的代碼應先確認識別碼種類。

```python
from vghks_sdk import VisitFilter

person = sdk.personnel.get_by_card(doctor_card)
if person is None:
    raise LookupError("人事目錄沒有精確符合的員工，請確認卡號")

visits = sdk.records.find_visit_cases(
    mrn,
    VisitFilter(
        all_sections=True,
        case_types=("O", "A", "E"),
        doctor_names=(person.name,),
    ),
)
# 再選定支援的門診 VisitCase 串接 get_soap / get_case_orders 等功能。
```

這個組合明確先查人事、再依姓名篩就診，沒有為每筆就診額外查人事。可重用同一筆人事結果。若不同員工同名，單靠就診姓名仍無法證明是哪位醫師，需結合科別、日期等已知條件確認。

無精確員工編號回 `None`；重複／衝突身分或未知頁面拋出 SDKError。舊 `VisitFilter.doctor_cards` 僅為可選的原始 `vsNo` 欄位保留相容，不是完整員工卡號轉換功能，也不適合作為門診必要欄位。

## 回傳資料與邊界

`PersonnelRecord` 保留 `employee_id`、`name`、`doctor_stamp_no`、`title`（顯示名稱）、`unit`（顯示名稱）、`extension`、依原順序排列的 `phone_numbers` 三欄，及以原標頭為鍵的 `fields`。空欄保留空字串；顯示名稱不冒充選項代碼。

「門號」欄在 HAR 中被隱藏並以 JavaScript 遮蔽顯示，SDK 不執行該段 JS；以 `unrendered_fields` 記錄未渲染欄位。這不影響姓名／編號轉換。頁尾統計及簡訊按鈕不算人員，也不會提交「傳呼簡訊」。「詳細資料」按鈕雖有處理函式，但兩份 HAR 未錄到明細回應，本次提供的是清單資料。

查詢表單使用 Big5 編碼，結果頁可為 UTF-8；SDK 分別處理，沿用共用的循序 Session、0.8–1.8 秒隨機節流、瀏覽器標頭、有限讀取重試、一次登入恢復與自動 TLS 策略。

## 測試與擴充

新增 `personnel.options`、`personnel.search` 到查詢目錄與離線 HAR／ZIP 重解析。測試器可單獨選這兩項，登入帳號會作為預設員工編號：

```sh
vghks-live-test --profile atomic --only personnel.options --only personnel.search
```

兩份 HAR 已離線解析出 8 個職稱、73 個單位及 323 筆人事資料。四次查詢中，姓名兩次及員工編號一次沒有錄到 response body；職稱查詢有完整 body。因此合成測試與離線重解析可證明請求結構及解析，**新版 SDK 的 SSO、姓名／員工編號／組合條件仍待內網實測**。單位及下層單位目前依表單實作，尚無該條件的實際查詢錄製。

後續若需完整人員明細，錄製點擊「詳細資料」直到回應完成並保存 body，再新增獨立模型、Parser、Service 與合成測試。HAR、查詢結果及衍生資料皆為本機私有資料。
