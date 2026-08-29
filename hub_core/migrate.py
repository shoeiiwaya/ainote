"""hub_core/migrate.py — いま使っている名簿を、そのまま持ってくる。

不動産屋さんは既に何かで顧客を管理している（Excel・別のCRM・紙の台帳をExcel化したもの）。
「使い始めるには全部入力し直し」では乗り換えられないので、**手元のファイルをそのまま読む**。

設計の芯:
- **列名を当てにいく**（「氏名」「お客様名」「名前」…はどれも顧客名）。当たらない列は捨てず、
  備考として残す（勝手に消さない）。
- **捏造しない**。名前が無い行は取り込まず、理由を添えて報告する。
- **確認してから入れる**: 先に「何件・どう入るか」を見せ、押して初めて台帳へ書く。
- **二度取り込んでも増えない**: 同じ人（名前＋連絡先）は1件に寄せる。
- **出どころを残す**: どのファイルのどの行から来たかを台帳の「元データ／元ツール」に書く。
  後から「これはどこから来た？」に答えられないと、実務では信用されない。
"""
from __future__ import annotations

import csv
import io
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Callable

# 台帳の列（cases.csv / customers.csv と一致）
CUSTOMER_COLS = ["顧客ID", "顧客名", "連絡先", "LINEユーザーID", "状態", "ゲート状態",
                 "保留種別", "元データ", "元ツール"]

# 相手のファイルにありがちな列名 → こちらの項目。NFKC正規化して小文字で比べる。
ALIASES: dict[str, tuple[str, ...]] = {
    "顧客名": ("顧客名", "氏名", "名前", "お客様名", "お客様", "会社名", "担当者名",
               "name", "customer", "customername", "client"),
    "連絡先": ("連絡先", "電話", "電話番号", "tel", "phone", "携帯", "携帯電話",
               "メール", "メールアドレス", "mail", "email", "e-mail"),
    "LINEユーザーID": ("lineユーザーid", "lineid", "line", "ラインid"),
    "備考": ("備考", "メモ", "note", "notes", "remarks", "コメント"),
}


class MigrateError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s or "")).strip().lower().replace(" ", "")


def guess_columns(headers: list[str]) -> dict[str, str]:
    """相手の列名 → こちらの項目 の対応を推測する。

    完全一致を部分一致より優先する。1つの見出しが複数項目に見える場合は、誤った顧客を
    作るより確認を求める方が安全なので推測しない。
    """
    candidates: dict[str, list[tuple[bool, int, str]]] = {field: [] for field in ALIASES}
    normalized = {
        field: tuple(dict.fromkeys(_norm(alias) for alias in aliases))
        for field, aliases in ALIASES.items()
    }
    for index, header in enumerate(headers):
        name = _norm(header)
        if not name:
            continue
        exact = {field for field, aliases in normalized.items() if name in aliases}
        partial = {
            field for field, aliases in normalized.items()
            if any(alias in name for alias in aliases if len(alias) >= 3)
        }
        matched = exact or partial
        if len(matched) != 1:
            continue
        field = next(iter(matched))
        candidates[field].append((field in exact, index, header))

    out: dict[str, str] = {}
    for field, choices in candidates.items():
        if not choices:
            continue
        # 完全一致が1つでもあれば、それ以前の部分一致を採らない。
        exact_choices = [choice for choice in choices if choice[0]]
        _exact, _index, header = min(exact_choices or choices, key=lambda choice: choice[1])
        out[header] = field
    return out


def _header_score(values) -> tuple[int, int, int]:
    headers = [str(value).strip() if value is not None else "" for value in values]
    mapping = guess_columns(headers)
    if "顧客名" not in mapping.values():
        return (-1, -1, -1)
    exact = 0
    for header, field in mapping.items():
        aliases = {_norm(alias) for alias in ALIASES[field]}
        exact += int(_norm(header) in aliases)
    nonempty = sum(bool(header) for header in headers)
    return (len(mapping), exact, nonempty)


def _select_header_row(matrix: list[tuple]) -> int:
    """先頭25行から実見出しを選ぶ。帳票タイトル行をデータとして取り込まない。"""
    if not matrix:
        raise MigrateError(400, "中身が空のファイルです。")
    ranked = [(_header_score(row), index) for index, row in enumerate(matrix[:25])]
    score, index = max(ranked, key=lambda item: (item[0], -item[1]))
    return index if score[0] >= 0 else 0


def _matrix_to_rows(matrix: list[tuple], header_index: int) -> tuple[list[str], list[dict]]:
    headers = [str(value).strip() if value is not None else "" for value in matrix[header_index]]
    rows = []
    for source_index, values in enumerate(matrix[header_index + 1:], start=header_index + 2):
        if not any(value not in (None, "") for value in values):
            continue
        row = {
            headers[i]: _cell(value, headers[i])
            for i, value in enumerate(values)
            if i < len(headers) and headers[i]
        }
        row["__source_row__"] = source_index
        rows.append(row)
    return headers, rows


def read_table(raw: bytes, filename: str = "") -> tuple[list[str], list[dict]]:
    """CSV / Excel を読んで (列名, 行) を返す。読めなければ理由を添えて断る。"""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            raise MigrateError(501, "Excelファイルを読む部品がこの端末にありません。"
                                    "CSV形式で書き出してから取り込んでください。")
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except Exception:                      # noqa: BLE001
            raise MigrateError(400, "Excelファイルとして読めませんでした。"
                                    "ファイルが壊れていないかご確認ください。")
        try:
            matrix = list(wb.active.iter_rows(values_only=True))
        finally:
            wb.close()
        header_index = _select_header_row(matrix)
        return _matrix_to_rows(matrix, header_index)

    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise MigrateError(400, "文字コードを判別できませんでした。"
                                "UTF-8 か Shift_JIS で保存し直してください。")
    matrix = list(csv.reader(io.StringIO(text)))
    if not matrix:
        raise MigrateError(400, "1行目に列の見出しが要ります（氏名・電話 など）。")
    header_index = _select_header_row([tuple(row) for row in matrix])
    return _matrix_to_rows([tuple(row) for row in matrix], header_index)


def _cell(v, header: str) -> str:
    """Excelのセル値を文字にする。

    電話番号や郵便番号は数値セルだと **先頭のゼロが落ちる**（09012345678 → 9012345678）。
    落ちたまま入れると同じ人が別人として二重登録される。連絡先らしい列で、整数として
    入っていて桁数が電話番号相当なら 0 を戻す。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int) or (isinstance(v, float) and float(v).is_integer()):
        n = str(int(v))
        h = _norm(header)
        looks_contact = any(a in h for a in ("電話", "tel", "phone", "携帯", "fax", "郵便", "zip"))
        if looks_contact and 9 <= len(n) <= 10:
            return "0" + n
        return n
    return str(v).strip()


def _key(name: str, contact: str) -> str:
    """同一人物の判定キー。

    連絡先が空のときは**名前だけで同一人物と決めない**（行番号で必ず別扱いにする）。
    佐藤・鈴木＋よくある名前の同姓同名は実在し、連絡先欠けの行も名簿では普通にある。
    ここで潰すと、別のお客様が黙って1人に消える（取り込み側からは気づけない）。
    """
    c = re.sub(r"[^0-9a-z@.]", "", _norm(contact))
    if not c:
        return ""          # 空キー＝突合しない（呼び出し側が行ごとに別扱いする）
    return _norm(name) + "|" + c


def _csv_safe(v: str) -> str:
    """表計算ソフトで数式として実行される先頭文字を無害化する。

    取り込んだ値はバックアップZIPのCSVとして書き出され、利用者がExcelで開く。
    `=cmd|...`・`+`・`-`・`@` 始まりのセルはそこで数式になる（CSVインジェクション）。
    値は消さず、先頭にアポストロフィを置いて文字列として扱わせる。
    """
    v = str(v or "")
    return ("'" + v) if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def plan(raw: bytes, filename: str, existing: list[dict] | None = None,
         mapping: dict[str, str] | None = None) -> dict:
    """取り込む前の下見。**何も書かない**。

    返り: {"mapping": 列の対応, "headers": 列名, "total": 読めた行数,
           "ready": 入れられる行, "skipped": [(行番号, 理由)], "dup_existing": 既にいる人の数,
           "unmapped": 対応づかなかった列}
    """
    headers, rows = read_table(raw, filename)
    m = mapping or guess_columns(headers)
    if "顧客名" not in m.values():
        raise MigrateError(400, "お名前の列が見つかりませんでした。"
                                "1行目の見出しに「氏名」や「お客様名」があるかご確認ください。")
    have = {_key(str(r.get("顧客名") or ""), str(r.get("連絡先") or ""))
            for r in (existing or [])}
    # 連絡先が無い既存客は、名前ごとの人数で数える（名前だけで寄せると別人を潰す）。
    have_noc: dict[str, int] = {}
    for r in (existing or []):
        if not _key(str(r.get("顧客名") or ""), str(r.get("連絡先") or "")):
            nk = _norm(str(r.get("顧客名") or ""))
            have_noc[nk] = have_noc.get(nk, 0) + 1
    seen_noc: dict[str, int] = {}
    ready, skipped, seen, dup_existing = [], [], set(), 0
    for fallback_i, r in enumerate(rows, start=2):
        i = int(r.get("__source_row__") or fallback_i)
        rec = {"顧客名": "", "連絡先": "", "LINEユーザーID": "", "備考": ""}
        extras = []
        for src, val in r.items():
            if src == "__source_row__":
                continue
            field = m.get(src)
            v = str(val or "").strip()
            if not v:
                continue
            if field and field in rec:
                rec[field] = (rec[field] + " / " + v) if rec[field] else v
            elif not field:
                extras.append(f"{src}: {v}")   # 対応づかない列も捨てない
        if not rec["顧客名"]:
            skipped.append((i, "お名前が空のため"))
            continue
        if extras:
            rec["備考"] = (rec["備考"] + " / " if rec["備考"] else "") + " / ".join(extras)
        k = _key(rec["顧客名"], rec["連絡先"])
        if k and k in seen:
            skipped.append((i, "同じファイル内に同じ方がいるため"))
            continue
        if k:
            seen.add(k)
        else:
            # 連絡先が無い＝同一人物と断定できない。取り込むが、確認を促す印を残す。
            rec["備考"] = ((rec["備考"] + " / ") if rec["備考"] else "") + "連絡先なし（要確認）"
        if not k:
            # 連絡先が無い＝同姓同名を別人と断定できないので名前だけでは寄せない。
            # ただし**同じ名前が同じ人数だけ既にいる**なら、それは前回の取り込み分。
            # ここを見ないと、取り込むたびに同じ人が増え続ける。
            nk = _norm(rec["顧客名"])
            seen_noc[nk] = seen_noc.get(nk, 0) + 1
            if seen_noc[nk] <= have_noc.get(nk, 0):
                dup_existing += 1
                skipped.append((i, "すでに同じお名前で登録済みのため"
                                   "（連絡先が無いため別人か判別できません）"))
                continue
        if k and k in have:
            dup_existing += 1
            # 同じ方が賃貸のあとに購入、は実務で普通。新規でなく「お取引のある方」として扱う。
            skipped.append((i, "すでにお取引のある方のため（新しいお取引はこの方に紐づきます）"))
            continue
        rec["_row"] = i
        ready.append(rec)
    return {"mapping": m, "headers": headers, "total": len(rows), "ready": ready,
            "skipped": skipped, "dup_existing": dup_existing,
            "unmapped": [h for h in headers if h not in m]}


def apply(data_dir, raw: bytes, filename: str, *, source_tool: str = "",
          mapping: dict[str, str] | None = None,
          audit_commit: Callable[[dict], None] | None = None) -> dict:
    """下見の結果を台帳へ書く。

    CSV・SQLite・監査記録のいずれかが失敗した場合は、顧客台帳を取り込み前へ戻す。
    HTTP 層は ``audit_commit`` に監査追記を渡し、監査だけ欠けた成功を作らない。
    """
    d = Path(data_dir)
    path = d / "customers.csv"
    existing: list[dict] = []
    if path.is_file():
        with path.open(encoding="utf-8", newline="") as fh:
            existing = list(csv.DictReader(fh))
    p = plan(raw, filename, existing=existing, mapping=mapping)
    if not p["ready"]:
        return {"added": 0, **p}

    used = {str(r.get("顧客ID") or "") for r in existing}
    n = 1
    added = []
    for rec in p["ready"]:
        while f"CUST-M{n:04d}" in used:
            n += 1
        cid = f"CUST-M{n:04d}"
        used.add(cid)
        added.append({
            "顧客ID": cid, "顧客名": _csv_safe(rec["顧客名"]),
            "連絡先": _csv_safe(rec["連絡先"]),
            "LINEユーザーID": _csv_safe(rec["LINEユーザーID"]), "状態": "取り込み",
            "ゲート状態": "", "保留種別": "",
            "元データ": _csv_safe(f"{filename}#{rec['_row']}"
                                  + (f"／{rec['備考']}" if rec["備考"] else "")),
            "元ツール": _csv_safe(source_tool or "取り込み"),
        })
    d.mkdir(parents=True, exist_ok=True)
    before_csv = path.read_bytes() if path.is_file() else None
    db = d / "hub.db"
    db_existed = db.is_file()
    before_db_rows = _db_customers(db) if db_existed else []
    result = {"added": len(added), **p}

    try:
        _write_customers_atomic(path, existing + added)
        _sync_to_db(d, existing + added)
        if audit_commit is not None:
            audit_commit(result)
    except Exception as exc:  # noqa: BLE001 - 3正本を一手としてロールバックする
        rollback_errors = _rollback_import(
            path, before_csv, db, db_existed, before_db_rows)
        if isinstance(exc, MigrateError):
            raise
        from hub_core.audit import AuditChainError
        if isinstance(exc, AuditChainError):
            raise MigrateError(
                409,
                "監査ログへ記録できなかったため、取り込みは行いませんでした。"
                "監査ログを保全して管理者へ連絡してください。",
            ) from exc
        detail = f"（復旧にも失敗: {' / '.join(rollback_errors)}）" if rollback_errors else ""
        raise MigrateError(
            500,
            "お客様名簿の取り込みを完了できませんでした。変更は取り込み前に戻しました。"
            + detail,
        ) from exc
    return result


def _write_customers_atomic(path: Path, rows: list[dict]) -> None:
    """customers.csv を同一ディレクトリ内で原子的に置換する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CUSTOMER_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _db_customers(db: Path) -> list[dict]:
    if not db.is_file():
        return []
    from hub_core.store import SqliteStore
    return SqliteStore(db).query("customers")


def _rollback_import(path: Path, before_csv: bytes | None, db: Path, db_existed: bool,
                     before_db_rows: list[dict]) -> list[str]:
    errors = []
    try:
        if before_csv is None:
            path.unlink(missing_ok=True)
        else:
            fd, raw_tmp = tempfile.mkstemp(prefix=path.name + ".rollback.", suffix=".tmp",
                                           dir=path.parent)
            tmp = Path(raw_tmp)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(before_csv)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - 元例外を失わず復旧失敗を通知する
        errors.append(f"CSV: {exc}")

    try:
        if db_existed:
            from hub_core.schema_cols import COLS
            from hub_core.store import SqliteStore
            cols = [key for _label, key in COLS["customers"]]
            SqliteStore(db).sync({"customers": (cols, before_db_rows)})
        else:
            for suffix in ("", "-wal", "-shm"):
                Path(str(db) + suffix).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"DB: {exc}")
    return errors


def _sync_to_db(data_dir: Path, rows: list) -> None:
    """hub.db にも書く。**画面は DB があれば DB だけを読む**ため、CSVだけだと消えて見える。

    最初の業務操作で hub.db が作られると、それ以降 /customers は DB を読む
    （serve.load_source → views.query_page）。CSVにしか書かないと、取り込んだお客様が
    画面から丸ごと消える（データは残っているのに見えない＝いちばん怖い壊れ方）。
    DBがまだ無い場合は、最初の業務操作が ``customers.csv`` 全件を読んでから作る。
    ここで空のDBを先に作るとその初期化判定を失うため、作成はしない。
    すでにDBがある場合はDBだけにいる顧客を保持したまま、CSV側の不足分を足す。
    """
    db = Path(data_dir) / "hub.db"
    if not db.is_file() or not rows:
        return
    from hub_core.schema_cols import COLS
    from hub_core.store import SqliteStore

    st = SqliteStore(db)
    cols = [key for _label, key in COLS["customers"]]
    labels = {label: key for label, key in COLS["customers"]}
    merged = st.query("customers")
    by_id = {str(r.get("customer_id") or ""): i for i, r in enumerate(merged)}
    for row in rows:
        eng = {key: str(row.get(label) or "") for label, key in labels.items()}
        cid = eng["customer_id"]
        if not cid:
            continue
        if cid in by_id:
            # 移行CSVは出どころの正本。空値で既存DBの補足情報を消さない。
            current = merged[by_id[cid]]
            merged[by_id[cid]] = {
                key: (eng[key] if eng[key] != "" else str(current.get(key) or ""))
                for key in cols
            }
        else:
            by_id[cid] = len(merged)
            merged.append(eng)
    st.sync({"customers": (cols, merged)})
