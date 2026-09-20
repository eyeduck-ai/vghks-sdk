"""Local-only structural audit of a newly recorded HAR batch."""
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vghks_sdk.contracts.check import discover_har_files
from vghks_sdk.local_io import write_json_atomic
from vghks_sdk.offline.replay import replay_hars


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("output/new-har-analysis"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    inventory = []
    har_files, _ = discover_har_files(args.input)
    for path in har_files:
        entries = json.loads(path.read_text(encoding="utf-8-sig"))["log"]["entries"]
        inventory.append(
            {
                "file": path.name,
                "entries": len(entries),
                "missing_response_bodies": sum(
                    not entry.get("response", {}).get("content", {}).get("text")
                    for entry in entries
                ),
                "empty_recording": not entries,
            }
        )
    report = replay_hars(args.input, output_path=args.output / "replay.json")
    write_json_atomic(args.output / "files.json", inventory)
    lines = [
        "# 新 HAR 離線分析",
        "",
        "所有驗證均只讀本機 HAR，不發送網路請求。",
        "",
        "| 檔案 | HTTP 筆數 | 空 HAR |",
        "| --- | ---: | --- |",
    ]
    lines.extend(
        f"| {row['file']} | {row['entries']} | {'是' if row['empty_recording'] else '否'} |"
        for row in inventory
    )
    lines += ["", "## 回放結果", "", f"狀態：`{report['status']}`", ""]
    lines.extend(f"- `{key}`：{count}" for key, count in report["status_counts"].items())
    text_statuses = Counter(
        row["report_data_status"] for row in report["exchanges"] if "report_data_status" in row
    )
    lines += [
        "",
        "## 報告正文",
        "",
        "PARSED 可包含報告基本欄位，不能單憑它判定已取得檢查結果。正文另行判定：",
        "",
    ]
    lines.extend(f"- `{key}`：{count}" for key, count in text_statuses.items())
    lines += [
        "",
        "TEXT_AVAILABLE 表示已取得報告文字，不要求 PDF 下載成功；ATTACHMENT_ONLY 表示仍只有附件參照，尚未擷取正文。",
    ]
    lines += [
        "",
        "`RECORDED_ACK` 僅表示 HAR 內的異動成功回覆被辨識，沒有實際執行異動。",
        "`EXPECTED_NEGATIVE` 是已辨識的預期負向回應，例如 PDF 檢視器 HTML 或登入前導向登入頁；詳見 replay.json 的 operation 與 error_code。",
        "`UNAVAILABLE` 是 HAR 未帶回回應內容；不能當成解析或院內系統失敗。",
        "",
        "本結果不等於新版 SDK 已在內網驗證。功能見 docs/API_REFERENCE.md；驗證範圍見 docs/VALIDATION.md。",
    ]
    (args.output / "analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "archives": len(inventory),
                "status": report["status"],
                "counts": report["status_counts"],
            }
        )
    )


if __name__ == "__main__":
    main()
