# 手術排程查詢

`sdk.surgery.get_schedule(card_no, start, end, department="OPH", mrn="", room="")` 依醫師卡號與日期區間查 OPPL 排程，可選科別、病歷號和手術室。回傳 `list[SurgeryRecord]`；查詢不建立、編輯或取消排程。

| `SurgeryRecord` 欄位 | 來源欄位 | 意義 |
| --- | --- | --- |
| `patient_mrn` | `orhisnum` | 病歷號。 |
| `case_no` | `orcaseno` | 排程所屬就診／案例號；不是手術申請單號。 |
| `surgery_date` | `orbgndt`，備援 `ordate` | 手術預定日期。 |
| `schedule_time` | `optime` | 網頁「手術日期」欄附帶顯示的原始時間文字；可能是 `TF`、`TF1` 等未定時間。 |
| `time_status` | 依 `optime` 判斷 | `UNCONFIRMED`：`TF`／`TF` 加數字；`CLOCK_TIME`：有效四位 24 小時時分；其他為 `UNKNOWN`。 |
| `start_time` | `orbgntm` | 底層開始時間；若 `optime` 為 `TF` 類型則回空字串，避免把來源的 `23:59:00` 佔位值誤認為實際時間。 |
| `end_time` | `orendtm` | 結束時間；未完成排程可為空。 |
| `ward` | `patient.hnursta`、`patient.hbedno` | 網頁「病房」欄；住院為病房與床號組合，門診為 `OPD`。 |
| `room` | `oproom`，備援 `oroproom` | 網頁「房間」欄；`oroproom` 可能是不同的內部房間代碼。 |
| `anesthesia` | `oropamed` | 網頁「麻醉」欄的原始代碼。 |
| `category` | `orfreqnc` | 網頁「類別」欄。 |
| `patient_name` | `patient.hnamec` | 網頁「姓名」欄的來源值；病人資料缺漏時不臆造姓名。 |
| `patient_sex` | `patient.hsexc` | 網頁「性別」欄的來源值。 |
| `department` | `orcatgy` | 網頁「科別」欄的來源代碼。 |
| `doctor_card` | `ordocno`，備援 `ordocnum` | 手術醫師卡號。 |
| `doctor_name` | `ordocnm`，備援 `ordocnam` | 網頁「主刀醫師」欄。 |
| `procedure` | `oropnm1`，備援 `oropmnm` | 主要術式名稱。 |
| `status` | `ornstats`，備援 `orstatus` | 來源系統狀態文字／代碼；SDK 不自行解釋或轉成完成手術。 |
| `request_no`／`sequence_no` | `orreqno`／`ordseqno` | 手術申請單號及來源列序號；與 `case_no` 分開。 |
| `case_type` | `orcasetp` | 排程案例類別的來源代碼，不自行擴寫含義。 |
| `internal_room_code` | `oroproom` | 與網頁「房間」分開保留的來源房間代碼；兩者不一定相同。 |
| `procedures` | `oropnc1`–`oropnc4`、`oropnm1`–`oropnm4` | 按來源位置保留手術碼及術式名稱的 `SurgeryScheduleProcedure(position, code, name)` 清單；空位不臆補。 |
| `diagnosis_codes`／`diagnosis_text` | `oropicd1`–`oropicd4`／`ordiag` | 來源提供的診斷碼與自由文字；不推論 ICD 版本或診斷主次。 |
| `extra` | 未選為舊版主要欄位的來源鍵 | 相容既有用法，仍保留 `orstatus` 等欄位，但不是完整原始回應。 |
| `source_fields` | 手術排程 JSON 中完整的一筆 `surgs` 原始列 | 供進階開發檢查其餘欄位及型別；例如 `oroproom` 與 `oproom` 可同時取得。 |

兩份私有 HAR 的排程回應只有頂層 `surgs` 清單，每筆來源列共有 125 種頂層鍵、巢狀 `patient` 物件共有 97 種鍵；不少鍵在本次樣本中為空。70 筆均有申請單號及至少一組術式碼／名稱；69 筆有診斷碼、59 筆有診斷文字，15 筆的 `oroproom` 與網頁顯示的 `oproom` 不同。70 筆的外層病歷號與巢狀病人病歷號均一致；新版若遇到兩者不一致會報 `OPPL_SURGERY_PATIENT_MISMATCH`，避免組出錯置的病人資訊。其餘如術前準備、麻醉、其他醫師與病人明細等來源欄位可從 `source_fields` 讀取，但來源代碼若沒有足夠的介面對照，SDK 不猜測其意義。

`source_fields` 包含病人身分、聯絡資料及可能的院內識別資訊；不要輸出到公開日誌、公開 repo 或分析範例。常用欄位應使用正式屬性；只有新增應用確實需要且確認來源含義後，再把其他鍵提升為穩定模型欄位。

錄製的排程 HAR 有 41 筆回應列；另份醫師排程 HAR 有 29 筆。兩份錄製的網頁程式均使用上述欄位組成可見清單。前者 38 筆 `TF` 類型時間的 `orbgntm` 是 `23:59:00`；網頁有時把 `TF` 暫放在 `08:30:00` 來定位月曆格子，這兩個值都不是已確定的手術開始時間。SDK 保留 `schedule_time` 原文及 `time_status`，不以佔位值填入 `start_time`。

修正前 `status` 因只讀 `ornstats` 而全部空白，實際回應使用 `orstatus`，該樣本值為來源代碼 `31`。新版 Parser 的離線重解析可保留 41 筆狀態，但尚無代碼對照表，不能推斷 `31` 的臨床或作業意義。當時只是離線重解析；其後的院內查詢結果見下段。`source_fields` 的鍵值未標準化，呼叫端不能假設每筆都有相同非空欄位。

0.20.1 增量 EXE 的院內回傳已另外取得 71 筆排程。每筆均有手術日期、病歷號、病房、房間、類別、姓名、性別、科別、主刀醫師與完整來源列；麻醉來源有 12 筆空白，不能填造。50 筆 `TF` 類型均標 `UNCONFIRMED` 且 `start_time` 為空，另外 21 筆為 `CLOCK_TIME`。這證實本輪查詢及上述欄位解析可運作，但沒有以每筆院內畫面逐一人工比對，也不代表來源代碼的業務含義已確認。
