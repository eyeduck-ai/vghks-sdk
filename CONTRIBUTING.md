# 參與開發

先閱讀 [README](README.md) 和 [架構](docs/ARCHITECTURE.md)。新增功能依 [HAR 錄製與擴充流程](docs/HAR_RECORDING.md)；完整操作指南供人與 agents 共用，見 [AGENTS](AGENTS.md)。

請維持 Service／Adapter／Parser／模型分層，使用合成測試，補上 API 用途及組合範例。PR 說明問題、改變後行為、測試與仍待內網驗證的部分。

提交前執行 tests、ruff、API 文件同步及公開內容檢查。不要提交 HAR、return、帳密、患者或薪資資料；詳細邊界見 [SECURITY](SECURITY.md)。
