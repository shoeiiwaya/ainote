#!/usr/bin/env bash
# あいのて local Web sourceを127.0.0.1で起動する。停止は Ctrl-C。
# データ = ~/ainote-data/out。ri-hub 本体のデータとは分ける＝試用で汚さない。
set -euo pipefail
cd "$(dirname "$0")"

DATA="${AINOTE_DATA_DIR:-$HOME/ainote-data/out}"
PORT="${AINOTE_PORT:-8788}"
export RI_HUB_DISABLE_SOFFICE="${RI_HUB_DISABLE_SOFFICE:-1}"

# Python 3.12 以上を**その端末から探す**（特定の場所を決め打ちしない＝別のPCでもそのまま動く）。
# serve.py は PEP 701 の入れ子f-stringを使うため 3.11 以下では読み込めない。
find_python() {
  if [ -n "${AINOTE_PYTHON:-}" ]; then
    echo "$AINOTE_PYTHON"; return
  fi
  for c in python3.14 python3.13 python3.12 python3 python; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    [ -n "$p" ] || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then
      echo "$p"; return
    fi
  done
  for p in /opt/homebrew/bin/python3.1[2-9] /usr/local/bin/python3.1[2-9] \
           /Library/Frameworks/Python.framework/Versions/3.1[2-9]/bin/python3; do
    [ -x "$p" ] || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>/dev/null; then
      echo "$p"; return
    fi
  done
  echo ""
}

PY="$(find_python)"
if [ -z "$PY" ]; then
  cat >&2 <<'MSG'
Python 3.12 以上が見つかりませんでした。

このソフトは Python 3.12 以上で動きます。次のどちらかをしてください。
  1) python.org から Python をインストールする（https://www.python.org/downloads/）
  2) すでに入っている場合は、その場所を指定して起動する
       AINOTE_PYTHON=/path/to/python3 ./start_ainote.sh
MSG
  exit 1
fi
echo "使う Python: $PY ($("$PY" -V 2>&1))"

# 見本は明示指定時だけ。通常の初回起動は空の保存先から法人登録へ進む。
if [ "${AINOTE_DEMO_DATA:-0}" = "1" ] && { [ ! -d "$DATA" ] || [ -z "$(ls -A "$DATA" 2>/dev/null)" ]; }; then
  echo "初回起動: お試し用のデータを作ります → $DATA"
  mkdir -p "$DATA"
  if ! "$PY" -c "import serve,pathlib,sys; serve._write_fixture(pathlib.Path(sys.argv[1]))" "$DATA"; then
    echo "お試しデータ（台帳）を作れませんでした。空のまま起動します。" >&2
  fi
  "$PY" seed_demo.py --data-dir "$DATA" >/dev/null 2>&1 || \
    echo "お試しの書類は作れませんでした（台帳だけで起動します）。" >&2
  n_csv=$(ls "$DATA"/*.csv 2>/dev/null | wc -l | tr -d ' ')
  if [ "$n_csv" -eq 0 ]; then
    echo "警告: 台帳データが1件もありません。画面はからっぽで表示されます。" >&2
  else
    echo "お試しデータ: 台帳 ${n_csv} 件を用意しました。"
  fi
fi

if [ ! -d "$DATA" ] || [ -z "$(ls -A "$DATA" 2>/dev/null)" ]; then
  mkdir -p "$DATA"
  export RI_HUB_ONBOARD=1
  echo "初回起動: 空の保存先から会社登録を始めます → $DATA"
fi

# 既定は空の保存先から初期設定へ進む。既存データでも設定画面を明示したい時だけ
# AINOTE_ONBOARD=1 を付けて起動する。
if [ "${AINOTE_ONBOARD:-0}" = "1" ]; then
  export RI_HUB_ONBOARD=1
  echo "初期設定から始めます（/setup へ誘導されます）"
fi
echo "あいのて を起動します → http://127.0.0.1:${PORT}/"
echo "データ保存先: ${DATA}（同期・バックアップ設定は別途確認）／停止は Ctrl-C"
"$PY" serve.py --data-dir "$DATA" --port "$PORT"
