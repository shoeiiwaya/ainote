#!/usr/bin/env python3
"""あいのて local Web sourceを127.0.0.1で起動する。

やること:
  1. Python が 3.12 以上かを確かめる（下だと serve.py を読み込めない）
  2. 初回は空の保存先から法人登録へ進む（見本は--demo-data明示時だけ）
  3. サーバを起動してブラウザを開く

使い方:
    python3 start_ainote.py
    python3 start_ainote.py --port 8790 --data-dir /path/to/out
    python3 start_ainote.py --onboard      # 「はじめての設定」から試す
    python3 start_ainote.py --demo-data    # 明示的に見本データで試す
    python3 start_ainote.py --no-browser   # ブラウザを開かない
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MIN = (3, 12)


def default_data_dir() -> Path:
    return Path(os.environ.get("AINOTE_DATA_DIR") or (Path.home() / "ainote-data" / "out"))


def check_python() -> None:
    if sys.version_info < MIN:
        print(
            f"このソフトは Python {MIN[0]}.{MIN[1]} 以上で動きます"
            f"（いまは {sys.version.split()[0]}）。\n"
            "  https://www.python.org/downloads/ から新しい Python を入れてから、\n"
            "  もう一度 python3 start_ainote.py と実行してください。",
            file=sys.stderr)
        raise SystemExit(1)


def seed_if_empty(data_dir: Path) -> None:
    """初回だけお試しデータを作る。**作れたかを数えて表示する**（作ったつもりで空、を避ける）。"""
    if data_dir.is_dir() and any(data_dir.iterdir()):
        return
    print(f"初回起動: お試し用のデータを用意します → {data_dir}", flush=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        import serve
        serve._write_fixture(data_dir)          # 台帳（物件・お客様・タスク等のCSV）
    except Exception as exc:                    # noqa: BLE001
        print(f"  台帳データを作れませんでした（{exc}）。空のまま起動します。", file=sys.stderr)
    try:
        import seed_demo
        seed_demo.seed(data_dir)                # 書類（重説・マイソクの見本）
    except Exception:                           # noqa: BLE001
        print("  見本の書類は作れませんでした（台帳だけで起動します）。", file=sys.stderr)
    n_csv = len(list(data_dir.glob("*.csv")))
    if n_csv:
        print(f"  お試しデータ: 台帳 {n_csv} 件を用意しました。", flush=True)
    else:
        print("  警告: 台帳データが1件もありません。画面はからっぽで表示されます。", file=sys.stderr)


def main() -> int:
    check_python()
    # 通常運用はブラウザ印刷PDF。LibreOffice実変換は人間承認済み単発ゲートへ分離する。
    os.environ.setdefault("RI_HUB_DISABLE_SOFFICE", "1")
    ap = argparse.ArgumentParser(description="あいのて を起動します")
    ap.add_argument("--port", type=int, default=int(os.environ.get("AINOTE_PORT", "8788")))
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--onboard", action="store_true", help="「はじめての設定」から始める")
    ap.add_argument("--demo-data", action="store_true", help="明示的に見本データを用意する")
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()

    empty = not data_dir.is_dir() or not any(data_dir.iterdir())
    if args.demo_data:
        seed_if_empty(data_dir)
    elif empty:
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["RI_HUB_ONBOARD"] = "1"
        print(f"初回起動: 空の保存先から会社登録を始めます → {data_dir}", flush=True)
    if args.onboard:
        os.environ["RI_HUB_ONBOARD"] = "1"
        print("「はじめての設定」から始めます。", flush=True)

    url = f"http://127.0.0.1:{args.port}/"
    print(f"あいのて を起動します → {url}", flush=True)
    print(
        f"データ保存先: {data_dir}（同期・バックアップ設定は別途確認）／止めるときは Ctrl-C",
        flush=True,
    )

    if not args.no_browser:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    import serve
    try:
        serve.serve(data_dir, args.port)
    except AttributeError:
        # serve.py の入口名が違う版でも動くように、コマンドラインとして呼び直す
        import subprocess
        return subprocess.call([sys.executable, str(here / "serve.py"),
                                "--data-dir", str(data_dir), "--port", str(args.port)])
    except KeyboardInterrupt:
        print("\n終了しました。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
