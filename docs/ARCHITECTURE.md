# あいのて 現状アーキテクチャ — 正本

> 作成: 2026-06-25 ／ 更新: 2026-08-10 ／ これは**実装の現状**を表す正本。
> 矛盾があれば実コードを優先する。旧設計資料は公開配布に含めない。
> `RI_HUB_*` などの旧識別子は互換性のため残している。

あいのては、各社PCで `serve.py` を起動し `127.0.0.1` のブラウザで使うlocal Web業務OS。
SaaS化もnative app化もせず、正本は検査済みsource ZIPと起動手順である。`serve.py` を中心に、
単一デザインシステム、用途別の書込境界、書類エンジン、会話ブリッジ、HMAC監査で構成する。

---

## 1. 配信層（`serve.py`）

- `ThreadingHTTPServer`（stdlib）。127.0.0.1 バインド。リクエスト毎にスレッド＋DBコネクション分離。
- 会話は `/chat/stream`（SSE）。画面表示と単一成果物の取得はGET、状態変更・設定・取込・外部連携はPOST。
  PUT/DELETE/PATCHは実装せず、破壊的操作に汎用HTTP APIを開放しない。POSTはOrigin・Host・CSRFと権限を各経路で検査する。
- 認証: source ZIPの標準起動は `RI_HUB_AUTH=on` に固定。`auth/company.json` または
  `auth/users.json`（data_dir の親）が存在すれば、空・破損を含め認証必須へ倒す。
  両方が無い完全な未設定だけがdevモード（代表）で、`RI_HUB_AUTH=off` は明示的な開発用途に限る。
- ローカル認証セッションはサーバ側メモリを正本とし、作成から8時間または無操作30分で失効する。
  Cookie は `HttpOnly`・`SameSite=Strict`・`Path=/` のHost-only cookieで、絶対寿命と同じ8時間を上限にする。
  HTTP Cookieにはポート境界がなく、`Path` も同一Host上のポートを分離しない。したがってCookie属性だけを
  ローカルサービス間の隔離とはみなさず、待受はloopback限定、全要求の`Host`は実際の待受ポートまで一致、
  ブラウザの状態変更要求はscheme・host・portが完全一致するsame-origin検査を別の境界として強制する。
- CSP: 既定 `default-src 'none'`。`frame-src 'self'` は `/maisoku`・`/maisoku/edit`（同一オリジン iframe プレビュー）のみ。`form-action 'self'` は操作画面のみ。

## 2. 単一デザインシステム（`hub_core/ui.py`）

- **過去の失敗（偽収束）**: かつて serve.py 内に4CSS系統×3ナビが混在し、ナビ後半が旧デザインへ飛んでいた。
- 現状は管理画面の主要 `render_*` を **1つの `APP_CSS` ＋ `shell()` ＋ `sidebar()`** に統一。
  初回設定と利用会社ブランドの顧客ポータルは、同じトークン規律の用途別シェルを使う。
- 管理画面は冷白・graphite・cobaltを軸にし、注意の朱と成功の緑を機能色に限定する。
  cobalt `#1B4DFF` は面・線、白地の文字と主ボタンは `#1638DB` を使い、視認性基準を分離する。
- 顧客に届く書類・LINEカード・顧客ポータルは製品色を使わず、利用会社のプロファイルまたは中立色から描画する。
- 検証では、全ナビが同一シェルを使い、偽枠・旧CSSがなく、実アイコンとactive表示が一致することを確認する。

## 3. データ正本・書込境界

- **業務台帳**: `out/hub.db`（SQLite WAL・`BEGIN IMMEDIATE`）が存在する時は、画面と業務操作がその現在状態を読む。
  初回取込などDB未作成の時は `out/*.csv` 台帳からDBを再構築する互換経路がある。DB作成後にCSVを手作業で書き換えない。
- **書類**: ファイル正本（1書類1dir/1版1file・`content_sha256`）＝`hub_core/documents.py`。portable backupはDBのonline snapshot、CSV、書類を同じ世代として収載する。
- **業務状態**: 標準の状態遷移は `hub_core/operations.apply_operation`を通し、RBACとHMAC監査を適用する。
- **外部送信試行**: `external_send_attempts.sqlite3` の一意なidempotency keyと
  `BEGIN IMMEDIATE` をprocess横断の正本にし、provider呼出前に予約する。FAX承認値は保存せず一回のPOSTだけに束縛する。
- **専用ストア**: 書類、会社プロファイル、移行、暗号化バックアップはそれぞれの版管理・排他・rollback処理を持つ。
  「全書込が1つの関数を通る」構成ではない。
- **監査**: `audit_log.jsonl`（HMAC append-onlyチェーン・`prev_hash`/`entry_hash`）と、隣接anchor・鍵側anchor状態で末尾巻戻しも検知する。
  SQLite内の監査テーブルはUPDATE/DELETEをトリガで拒否するが、外部WORMや第三者タイムスタンプの代替ではない。

## 4. AI・会話層

- `hub_core/chat_bridge.py` が同一プロセス内で読み取り・下書き・操作をdispatchする。読み取りはviewer認可、操作は画面利用者のroleに束縛する。
- 一般配布版の選択肢は①AIなし（既定）②ローカルOllama ③利用者のAnthropic APIキーの3つ。
  OpenAI互換経路はloopbackのローカルLLMだけを受け付け、任意の外部URLとサブスクライバーCLIには接続しない。
- 外部AIはコスト上限とPII redactを適用し、ツール結果はデータ封筒に入れて間接プロンプトインジェクションと分離する。
- **会話履歴**: `hub_core/chat_history.py`（スレッド単位 `out/chat_threads/<id>.jsonl`・全文・所有者スコープ）。Console でリロード復元・過去スレ一覧。

## 5. 書類エンジン（重説・マイソク）

→ 詳細は `書類エンジン.md`。マイソクは `build_maisoku_template.py` がblank workbookから生成する
A=標準A4横（既定）・C=業者間FAX白黒・B=買主向けA4縦の独自XLSXを正本とし、named rangesへ値・会社色・書体・写真・間取りを反映する。
重説は保存済み本文と `juusetsu_schema.json` から `python-docx` で生成する。通常のPDF保存はHTMLプレビューのブラウザ印刷を使う。
`RI_HUB_DISABLE_SOFFICE=1` では `_find_soffice()` が必ず `None` を返し、LibreOffice実変換は人間承認済みの単発・非並列ゲートに隔離する。

物件住所からの災害リスク欄は、明示設定したloopback/HTTPSの `juusetsu-hazard-v1` だけを下書き主経路として使う。
土砂・津波・造成宅地・洪水・内水・高潮を別行で受け、原典、版、確認日、document digest、確認先を検証する。
未接続、契約不一致、`NEEDS_PRIMARY_SOURCE` は推測で埋めず、空欄と要確認を残す。A33/A40/A54、大規模盛土、ハザードポータル図版を法定根拠へ流用しない。

## 6. 安全境界

- RBAC 5ロール＋Viewer 行スコープ＋PII列マスク＋自由文の電話/メール redact。
- 会話スレッドは**所有者スコープ**（他ユーザー不可視/不可読/不可書＝IDOR防止）、thread_id は16バイト乱数。
- `<script>` への JSON 埋込は `_js_json` でエスケープ（`</script>` 脱出封鎖＝XSS防止）。
- `is_export_request` は汎用的な一括エクスポートのpath/queryをpercent-decode・case-fold後に403で拒否する。
  利用者が選んだ単一書類の正規出力は、認可された専用経路で分離する。
- 不可逆・外向き操作は人間ゲート。記名確定（finalize）は、管理済み氏名・宅建士登録番号を持つ
  ログイン中の宅建士本人だけに限定し、serve・mcp双方で書類ID・版・case・`content_hash`へ束縛する。

## 7. テスト

unittest群＋ `serve.py --selftest`。UI統一、書類、書込・監査・認可、初回起動、配布物を分離して検査する。
`--selftest` は隔離fixtureで主要画面の応答・データ表示・外部URL混入・未実装HTTPメソッドを見るスモークであり、全POST操作の回帰検査は個別のHTTPテストと `demo_walkthrough.py` が担う。
