"""あいのて 統一デザインシステム（単一の正本）。

作り直しの核: 旧来は serve.py 内に4つのCSS系統(STYLE / RI_OS_OVERRIDE / RI_OS_STYLE /
_CONSOLE_CSS)と3つのナビが混在し、8ナビの後半が旧ダッシュボードの別デザインへ飛んでいた。
本モジュールは **1つのCSS(APP_CSS) ＋ 1つのシェル(shell) ＋ 1つのサイドバー(sidebar)** だけを提供し、
serve.py の全 render_* がこれを通る。外部ネットワーク0(Webフォントは読み込まずシステムフォントへフォールバック)。

デザイン: 「Ledger & Paper（台帳と紙）」v1.0。台帳モード=金融グレードの密度（Stripe/Mercury参照・
罫のみの表・枠なしメトリクス・監査レール）／紙モード=A4紙面が主役（明朝は書類内のみ）。
機能色は2つだけ: 確定緑(#217645=確定/入金済のみ)・朱書き(#C0392B=要確認/期限超過のみ)。
インタラクションは濃紺(#0A2540)。多色pill・ゼブラ・グラデ・絵文字・装飾アクセントは禁止（GATE-PV tells）。

すべて stdlib のみ。HTML文字列を返す純関数。
"""
from __future__ import annotations

import html

# ナビ識別子。各 render_* は自分の active キーを shell() に渡す。
NAV_PRIMARY = [
    ("home", "/home", "ホーム", "home"),
    ("console", "/console", "ことばで頼む", "chat"),
    ("agent", "/agent", "AIの作業を確認", "agent"),
    ("properties", "/properties", "物件", "building"),
    ("juusetsu", "/juusetsu", "重説", "doc"),
    ("maisoku", "/maisoku", "マイソク", "layout"),
    ("customers", "/customers", "顧客", "users"),
    ("ledger", "/ledger", "台帳", "book"),
]

# 旧18業務ページ + 台帳は「業務詳細」として同一シェル内の二次ナビに集約(主役から降格・設計書§7)。
NAV_DETAIL = [
    ("keisan", "/keisan", "お金の計算", "calc"),
    ("today", "/today", "今日のタスク", "today"),
    ("approval", "/approval", "承認待ち", "check"),
    ("hold", "/hold", "保留", "pause"),
    ("inbox", "/inbox", "反響", "inbox"),
    ("audit", "/audit", "監査ログ", "shield"),
    ("fax", "/fax", "FAX物確", "fax"),
    ("calls", "/calls", "物確電話", "phone"),
    ("line", "/line", "LINE", "linechat"),
    ("reins", "/reins", "REINS", "book"),
    ("it", "/it", "IT重説", "check"),
    ("profile", "/profile", "業者情報", "building"),
    ("connections", "/connections", "接続設定", "gear"),
]

# 旧ルート → サイドバーで点灯させる active キー(断絶を作らず正しい所属を示す)。
ROUTE_ACTIVE = {
    "/": "home", "/home": "home", "/console": "console", "/agent": "agent",
    "/properties": "properties", "/research": "properties", "/documents": "properties",
    "/juusetsu": "juusetsu", "/maisoku": "maisoku", "/ads": "maisoku",
    "/customers": "customers", "/leads": "customers", "/inbox": "inbox",
    "/viewings": "customers", "/applications": "customers",
    "/keisan": "keisan",
    "/ledger": "ledger", "/audit": "audit", "/viewlog": "ledger",
    "/contracts": "ledger", "/money": "ledger", "/management": "ledger",
    "/reports": "ledger", "/today": "today", "/hold": "hold", "/approval": "approval",
    "/profile": "profile", "/connections": "connections", "/materials": "properties", "/renewals": "ledger", "/pm": "home",
    "/fax": "fax", "/calls": "calls", "/line": "line", "/reins": "reins",
}
for _r in ("crosswalk", "governance", "claims", "recurrence", "contract-version",
           "filenames", "originals", "approval-ledger"):
    ROUTE_ACTIVE["/ledger/" + _r] = "ledger"


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


# --- アイコン (20x20 ラインアイコン・currentColor・外部依存なし) ---------------
def _svg(inner: str) -> str:
    return ('<svg class="ic" viewBox="0 0 20 20" fill="none" stroke="currentColor" '
            'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
            + inner + "</svg>")


ICONS = {
    "home": _svg('<path d="M3.5 9.5 10 4l6.5 5.5"/><path d="M5 8.5V16h10V8.5"/><path d="M8.5 16v-3.5h3V16"/>'),
    "chat": _svg('<path d="M4 5.5h12a1 1 0 0 1 1 1V13a1 1 0 0 1-1 1H8l-3.2 2.6V14H4a1 1 0 0 1-1-1V6.5a1 1 0 0 1 1-1Z"/>'),
    "fax": _svg('<path d="M6 7V3.5h8V7"/><rect x="3.5" y="7" width="13" height="6.5" rx="1.2"/>'
                '<rect x="6" y="11.5" width="8" height="5"/><circle cx="14" cy="10" r="0.7"/>'),
    "phone": _svg('<path d="M6 3.5c.5 2 1.3 3.4 2.4 4.6 1.2 1.1 2.6 2 4.6 2.4l1.4-1.7 2.4 1v2.9c0 .8-.7 1.4-1.5 1.3'
                  'C10.6 13.4 6.6 9.4 6 3.6 5.9 2.8 6.5 2 7.3 2"/>'),
    "linechat": _svg('<path d="M10 4c3.6 0 6.5 2.2 6.5 5s-2.9 5-6.5 5c-.6 0-1.2-.06-1.8-.18L5 15.5l.7-2.4'
                     'C4.4 12.2 3.5 10.7 3.5 9 3.5 6.2 6.4 4 10 4Z"/>'),
    "agent": _svg('<path d="M10 3v2.2"/><rect x="4.5" y="5.5" width="11" height="9" rx="2.2"/>'
                  '<circle cx="8" cy="10" r="1"/><circle cx="12" cy="10" r="1"/><path d="M3 9v3M17 9v3"/>'),
    "building": _svg('<rect x="5" y="3.5" width="10" height="13" rx="1"/><path d="M8 6.5h1.5M11 6.5h1M8 9h1.5M11 9h1M8 11.5h1.5M11 11.5h1"/><path d="M8.5 16.5v-2h3v2"/>'),
    "doc": _svg('<path d="M6 3.5h5l3 3V16a.5.5 0 0 1-.5.5H6A.5.5 0 0 1 5.5 16V4a.5.5 0 0 1 .5-.5Z"/><path d="M11 3.5V7h3"/><path d="M7.5 10h5M7.5 12.5h5"/>'),
    "layout": _svg('<rect x="3.5" y="4.5" width="13" height="11" rx="1.5"/><path d="M3.5 8.5h13"/><path d="M8.5 8.5v7"/>'),
    "users": _svg('<circle cx="7.5" cy="8" r="2.3"/><path d="M3.5 16c0-2.2 1.8-3.6 4-3.6s4 1.4 4 3.6"/><path d="M13 6.2a2.2 2.2 0 0 1 0 4.2M13.5 12.6c1.8.2 3 1.5 3 3.4"/>'),
    "book": _svg('<path d="M5 4.5h7a2 2 0 0 1 2 2V16H7a2 2 0 0 0-2 2V4.5Z"/><path d="M5 16a2 2 0 0 1 2-2h7"/>'),
    "calc": _svg('<rect x="4" y="3" width="12" height="14" rx="1.8"/>'
                 '<path d="M6.5 6.5h7M6.5 10h2M9.5 10h2M12.5 10h1M6.5 13h2M9.5 13h2M12.5 13h1"/>'),
    "today": _svg('<rect x="4" y="5" width="12" height="11" rx="1.5"/><path d="M4 8.5h12M7.5 3.5v3M12.5 3.5v3"/><path d="M9 12l1 1 2-2.2"/>'),
    "check": _svg('<circle cx="10" cy="10" r="6.5"/><path d="M7.2 10.2l1.9 1.9 3.7-3.9"/>'),
    "pause": _svg('<circle cx="10" cy="10" r="6.5"/><path d="M8.5 8v4M11.5 8v4"/>'),
    "inbox": _svg('<path d="M4 5.5h12v9H4z"/><path d="M4 11.5h3l1 1.6h4l1-1.6h3"/>'),
    "shield": _svg('<path d="M10 3.5 15 5.5v4c0 3.3-2.2 5.7-5 6.8-2.8-1.1-5-3.5-5-6.8v-4Z"/><path d="M7.7 9.8l1.6 1.6 3-3.2"/>'),
}


def _icon(name: str) -> str:
    return ICONS.get(name, ICONS["doc"])


# --- サイドバー(単一ナビ) -----------------------------------------------------
def sidebar(active: str, *, counts=None, viewer_role=None, viewer_user=None,
            juusetsu_case: str = "", show_logout: bool = True) -> str:
    counts = counts or {}
    jqs = ("?case=" + esc(juusetsu_case)) if juusetsu_case else ""

    def item(key, href, label, icon, *, hot_key=None):
        if key == "juusetsu":
            href = href + jqs
        cls = "nav-i on" if key == active else "nav-i"
        current = ' aria-current="page"' if key == active else ""
        badge = ""
        n = counts.get(hot_key or key)
        if n:
            bcls = "nb hot" if (hot_key or key) in ("approval", "hold") else "nb"
            badge = f'<span class="{bcls}">{esc(n)}</span>'
        return (f'<a class="{cls}" href="{esc(href)}"{current}>{_icon(icon)}'
                f'<span class="nl">{esc(label)}</span>{badge}</a>')

    primary = "".join(item(k, h, l, ic) for k, h, l, ic in NAV_PRIMARY)
    detail = "".join(
        item(k, h, l, ic, hot_key=k) for k, h, l, ic in NAV_DETAIL
    )

    foot = ""
    if viewer_role:
        who = esc(viewer_user) + " · " if (viewer_user and viewer_user != "dev") else ""
        logout = ('<a class="sb-logout" href="/logout">ログアウト</a>' if show_logout else "")
        foot = (f'<div class="sb-foot"><div class="sb-who">{who}{esc(viewer_role)}</div>{logout}</div>')

    return (
        '<aside class="ri-rail">'
        '<a class="sb-brand" href="/home"><span class="sb-logo"><svg class="sb-mark" viewBox="0 0 100 100" aria-hidden="true"><g fill="none" stroke="#fff" stroke-width="13" stroke-linecap="round" transform="rotate(-10 50 50)"><path d="M 57.98 52.77 A 20 20 0 1 0 57.98 35.23"/><path d="M 42.02 64.77 A 20 20 0 1 1 42.02 47.23"/><path d="M 47.07 62.71 A 20 20 0 0 1 36.08 63.61" stroke="var(--ai-cobalt-deep)" stroke-width="20"/><path d="M 47.07 62.71 A 20 20 0 0 1 36.08 63.61"/></g></svg></span>'
        '<span class="sb-bt">あいのて<span class="sb-bs">しごとに、合いの手を。</span></span></a>'
        f'<nav class="sb-nav sb-primary" aria-label="主要メニュー">{primary}</nav>'
        '<div class="sb-detail-desktop"><div class="sb-gh">業務詳細</div>'
        f'<nav class="sb-nav" aria-label="業務詳細">{detail}</nav></div>'
        '<details class="sb-detail-mobile"><summary class="sb-detail-toggle">業務メニュー</summary>'
        f'<nav class="sb-nav" aria-label="業務メニュー">{detail}</nav></details>'
        f'{foot}'
        '</aside>'
    )


# --- ページ外枠 ---------------------------------------------------------------
def shell(active: str, title: str, body_main: str, *, right: str = "",
          scripts: str = "", counts=None, viewer_role=None, viewer_user=None,
          juusetsu_case: str = "", main_class: str = "ri-main") -> str:
    """サイドバー + メイン(+任意の右ペイン) を1つの app フレームにまとめて完成HTMLを返す。"""
    sb = sidebar(active, counts=counts, viewer_role=viewer_role, viewer_user=viewer_user,
                 juusetsu_case=juusetsu_case)
    right_html = f'<aside class="ri-right">{right}</aside>' if right else ""
    script_html = f'<script>{scripts}</script>' if scripts else ""
    cols = "ri-ws ri-ws3" if right else "ri-ws"
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>あいのて | {esc(title)}</title><style>{APP_CSS}</style></head><body>'
        f'<div class="{cols}">{sb}<main class="{esc(main_class)}">{body_main}</main>{right_html}</div>'
        f'{script_html}</body></html>'
    )


# --- 小物コンポーネント -------------------------------------------------------
def page_head(title: str, sub: str = "", actions: str = "") -> str:
    s = f'<p class="ph-sub">{esc(sub)}</p>' if sub else ""
    a = f'<div class="ph-actions">{actions}</div>' if actions else ""
    return f'<header class="ph"><div class="ph-l"><h1>{esc(title)}</h1>{s}</div>{a}</header>'


def section(label: str) -> str:
    return f'<div class="ri-sech">{esc(label)}</div>'


def empty(msg: str) -> str:
    return f'<div class="ri-empty">{esc(msg)}</div>'


# ==============================================================================
# APP_CSS — 唯一のスタイルシート「Ledger & Paper」(台帳=罫のみの密度 / 紙=A4が主役 / 機能色は緑・朱書きの2つ)
# ==============================================================================
APP_CSS = r"""
:root{
 /* ============================================================================
    あいのて デザイントークン。
    面と文字の分割:
      cobalt #1B4DFF は白地で実測 5.91:1 しかなく、主要動線の 7:1 にどの文字色でも届かない。
      よって #1B4DFF は「文字を載せない面」（線・帯・罫・下線）専用とし、
      文字と主ボタンの塗りは同色相を暗くした #1638DB（白地 8.01:1）に落とす。朱も同じ扱い。
      画面上は同じ青／同じ朱に見える。
    ========================================================================== */
 --ai-cobalt:#1B4DFF;          /* 面の青: 線・帯・罫・下線。文字に使わない */
 --ai-cobalt-deep:#1638DB;     /* 文字の青: 主ボタン塗り(白文字8.01:1)・操作文字・金額 */
 --ai-cobalt-press:#0F2CB4;    /* 主ボタンの押下/hover */
 --ai-seal:#B23A2E;            /* 面の朱: 期限超過帯・滞留行の反転。文字に使わない。
                                  確定の信頼はHMAC監査・hash束縛・署名者本人束縛が担保する（印影ではない）。
                                  朱は期限超過と要確認だけを示す機能色。トークン名は互換のため据え置き。 */
 --ai-seal-deep:#9A2E23;       /* 文字の朱: 要確認・期限超過(白地7.53:1) */
 --ai-ink:#0E1116;             /* 本文・見出し(白地18.91:1) */
 --ai-muted:#5B6470;           /* 単独の補助テキストのみ(6.00:1・C3の補助4.5:1枠) */
 /* 主要動線の中にある弱い文字（ボタン内の説明・表頭・KPIラベル）は 6.00:1 では 7:1 を割る。
    cobalt/朱と同じ「面/文字」の論理で、muted にも文字用の濃い兄弟を置く。
    実測: 白 8.05:1 / 冷白 7.51:1 / accent-soft 7.20:1 ＝使う3地すべてで 7:1 を満たす。 */
 --ai-muted-strong:#4A5158;
 --ai-graphite:#2A2E37;        /* 図面・台帳の太罫(白地13.60:1) */
 --ai-paper:#F6F7F9;           /* 地(冷白。クリーム地禁止) */
 --ai-surface:#FFFFFF;         /* カード・台帳・札 */
 --ai-soft:#EEF2FF;            /* cobalt淡(選択中・いまの行) */
 --ai-rule:#E7E9EE;--ai-rule-strong:#CBD0D8;
 /* a11y の下限 */
 --ai-text:18px;               /* 本文の下限 */
 --ai-hit:48px;                /* 操作の当たり判定の下限 */

 /* ---- 旧トークン名は残し、値だけ あいのて パレットへ寄せる（全画面の回帰を避ける） ---- */
 --sumi:var(--ai-ink);      /* 旧=濃紺#0a2540。見出しは ink へ(青い見出しにしない) */
 --ink:var(--ai-ink);--ink2:var(--ai-ink);   /* 旧の二段目本文も ink へ＝7:1を割らせない */
 --bg:var(--ai-surface);--panel:var(--ai-surface);--panel2:var(--ai-paper);
 --desk:var(--ai-paper);
 --line:var(--ai-rule);--line2:#F1F3F6;
 /* 旧 --muted は表頭・ラベル・注記など主要動線の中でも広く使われているため、
    7:1 を満たす muted-strong へ寄せる（単独の補助だけ --ai-muted を明示的に使う）。 */
 --muted:var(--ai-muted-strong);--muted2:var(--ai-muted-strong);
 --accent:var(--ai-cobalt-deep);--accent-ink:#fff;--accent-bg:var(--ai-soft);
 --accent-line:var(--ai-rule-strong);
 --vermi:var(--ai-seal-deep);--vermi-bg:#FAF0EE;   /* 朱書き=要確認・期限超過のみ */
 --warn:var(--ai-muted-strong);--warn-bg:#EEF1F5;  /* 多色pillを作らない。
    文字は muted(5.29:1) でなく muted-strong(7.10:1)＝薄い地の上でも7:1を満たす */
 --bad:var(--ai-seal-deep);--bad-bg:#FAF0EE;
 --ok:#217645;--ok-bg:#eef7f1;            /* 確定緑=確定/入金済のみ */
 --p0:var(--ai-seal-deep);--gold:var(--ai-muted);--gold-bg:#EEF1F5;
 --r:4px;--r2:10px;--sb:232px;            /* 台帳4px / 操作10px の二層radius */
 /* 見出しの声=明朝。UIの声=ゴシック。台帳の声=mono(ラテンのみに当てる)。 */
 --head:"Hiragino Sans","Hiragino Kaku Gothic ProN",system-ui,sans-serif;
 --display:"Toppan Bunkyu Midashi Mincho","Hiragino Mincho ProN","Yu Mincho",serif;
 --body:"Hiragino Sans","Hiragino Kaku Gothic ProN",system-ui,sans-serif;
 --serif:"Toppan Bunkyu Midashi Mincho","Hiragino Mincho ProN","Yu Mincho","Noto Serif JP",serif;
 --mono:ui-monospace,"SF Mono",Menlo,monospace;
}
*{box-sizing:border-box}
.sr-only{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;
 overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
html,body{margin:0;padding:0;height:100%}
body{font-family:var(--body);background:var(--bg);color:var(--ink);font-size:var(--ai-text);line-height:1.7;
 -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
 font-feature-settings:"tnum";font-variant-numeric:tabular-nums}
a{color:inherit;text-decoration:none}
h1,h2,h3{font-family:var(--head);margin:0;letter-spacing:0;font-weight:700;color:var(--sumi)}
.muted{color:var(--muted)}.muted2{color:var(--muted2)}
mark{background:var(--accent-bg);padding:0 2px;color:var(--sumi)}
.kbd{font-family:var(--mono);font-size:18px;color:var(--muted);border:1px solid var(--line);border-radius:3px;padding:0 5px;line-height:16px;background:var(--panel)}

/* ---- App frame ---- */
.ri-ws{display:grid;grid-template-columns:var(--sb) 1fr;min-height:100vh}
.ri-ws3{grid-template-columns:var(--sb) minmax(0,1fr) 300px}
.ri-main{min-width:0;padding:22px 32px 56px;overflow-x:hidden}
.ri-right{border-left:1px solid var(--line);background:var(--panel);overflow:auto;padding:18px 16px}

/* ---- Sidebar (薄色レール・濃紺テキスト) ---- */
.ri-rail{background:var(--panel);color:var(--ink2);height:100vh;position:sticky;top:0;
 display:flex;flex-direction:column;padding:16px 10px;overflow-y:auto;border-right:1px solid var(--line)}
.sb-brand{display:flex;align-items:center;gap:10px;padding:2px 8px 14px;margin-bottom:6px;border-bottom:1px solid var(--line)}
.sb-logo{width:36px;height:36px;border-radius:10px;flex:none;display:flex;align-items:center;justify-content:center;background:var(--ai-cobalt-deep)}
.sb-mark{width:26px;height:26px;display:block}
/* ---- 窓口型ウィザード（1画面1動作・高齢者対応） ---- */
.ms-wrap{max-width:720px;margin:0 auto;padding:8px 4px 40px}
.ms-steps{display:flex;align-items:center;gap:14px;margin:6px 0 22px}
.ms-dots{display:flex;gap:8px}
.ms-dot{width:14px;height:14px;border-radius:50%;background:var(--ai-rule-strong);display:block}
.ms-dot.on{background:var(--ai-cobalt)}
.ms-count{font-size:18px;color:var(--ai-muted-strong);font-variant-numeric:tabular-nums}
.ms-h{font-family:var(--head);font-size:34px;line-height:1.35;color:var(--ai-ink);margin:0 0 10px}
.ms-help{font-size:18px;line-height:1.7;color:var(--ai-muted-strong);margin:0 0 26px}
.ms-form{display:flex;flex-direction:column;gap:20px}
.ms-row{display:flex;flex-direction:column;gap:8px}
.ms-l{font-size:20px;font-weight:600;color:var(--ai-ink)}
.ms-i{font-size:20px;line-height:1.5;padding:12px 14px;min-height:var(--ai-hit);
 border:2px solid var(--ai-rule-strong);border-radius:10px;background:var(--ai-surface);color:var(--ai-ink)}
.ms-i:focus{outline:3px solid var(--ai-cobalt);outline-offset:1px;border-color:var(--ai-cobalt)}
.ms-actions{display:flex;align-items:center;gap:20px;margin-top:14px;flex-wrap:wrap}
.ms-go{font-size:22px;font-weight:700;color:#fff;background:var(--ai-cobalt-deep);border:0;
 border-radius:12px;padding:0 34px;min-height:56px;min-width:180px;cursor:pointer}
.ms-go:hover{background:var(--ai-cobalt-press)}
.ms-go:focus-visible{outline:3px solid var(--ai-ink);outline-offset:2px}
.ms-back{font-size:18px;color:var(--ai-cobalt-deep);text-decoration:underline;
 display:inline-flex;align-items:center;min-height:var(--ai-hit)}
/* 選択肢の札（ラジオを大きな押せる面にする・高齢者対応の48px床） */
.ms-choice{display:inline-flex;align-items:center;gap:10px;min-height:52px;padding:0 20px;
 border:2px solid var(--ai-rule-strong);border-radius:12px;background:var(--ai-surface);
 font-size:20px;font-weight:600;color:var(--ai-ink);cursor:pointer}
.ms-choice:hover{border-color:var(--ai-cobalt)}
.ms-choice input{width:24px;height:24px;accent-color:var(--ai-cobalt-deep);margin:0}
.ms-choice:focus-within{outline:3px solid var(--ai-cobalt);outline-offset:2px}
.ms-file-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.ms-file-pick{display:inline-flex;align-items:center;justify-content:center;min-height:var(--ai-hit);
 padding:0 20px;border:2px solid var(--ai-rule-strong);border-radius:10px;background:var(--ai-surface);
 color:var(--ai-cobalt-deep);font-size:19px;font-weight:700;cursor:pointer}
.ms-file-pick:hover{border-color:var(--ai-cobalt)}
.ms-file-pick:focus-within{outline:3px solid var(--ai-ink);outline-offset:2px}
.ms-file-name{font-size:18px;color:var(--ai-muted-strong);overflow-wrap:anywhere}
/* 会社情報の変更履歴（戻せることを利用者の手に置く） */
/* 物件・反響を「仕事が生えるカード」で見せる（表は見えるだけで次が分からない） */
/* 反響カードの「次にやること」 */
.lc-body{display:block;color:inherit}
.lc-act{display:inline-flex;align-items:center;min-height:var(--ai-hit);margin-top:10px;
 padding:0 18px;border-radius:10px;background:var(--ai-cobalt-deep);color:#fff;
 font-size:18px;font-weight:700}
.lc-act:hover{background:var(--ai-cobalt-press);color:#fff}
.lc-act:focus-visible{outline:3px solid var(--ai-ink);outline-offset:2px}
/* お客様と、そのお客様とのお取引の履歴（リピーターを取りこぼさない） */
.cu-list{display:flex;flex-direction:column;gap:14px;margin-top:8px}
.cu{border:1px solid var(--ai-rule);border-radius:12px;padding:18px 20px;background:var(--ai-surface)}
.cu-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.cu-name{font-family:var(--head);font-size:23px;font-weight:700;color:var(--ai-ink)}
.cu-repeat{font-size:18px;font-weight:700;color:#fff;background:var(--ai-cobalt-deep);
 border-radius:999px;padding:5px 14px}
.cu-once{font-size:18px;color:var(--ai-muted-strong)}
.cu-deals{display:flex;flex-direction:column;gap:8px;margin-top:12px}
.cu-deal{display:flex;align-items:center;gap:10px;min-height:var(--ai-hit);padding:0 14px;
 border-left:4px solid var(--ai-cobalt);background:var(--ai-paper);border-radius:0 8px 8px 0;
 font-size:19px;color:var(--ai-ink)}
.cu-deal:hover{background:var(--ai-soft);color:var(--ai-ink)}
.cu-kind{font-size:18px;font-weight:700;color:var(--ai-cobalt-deep);min-width:3.2em}
.cu-src{margin-top:10px;font-size:18px;color:var(--ai-muted-strong)}
.pc-list{display:flex;flex-direction:column;gap:14px;margin-top:8px}
.pc{border:1px solid var(--ai-rule);border-radius:12px;padding:18px 20px;background:var(--ai-surface)}
.pc-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.pc-name{font-family:var(--head);font-size:24px;font-weight:700;color:var(--ai-ink)}
.pc-meta{font-size:18px;color:var(--ai-muted-strong);margin-top:6px}
.pc-acts{display:flex;flex-wrap:wrap;gap:12px;margin-top:14px}
.pc-go{display:inline-flex;align-items:center;min-height:var(--ai-hit);padding:0 20px;
 border-radius:10px;background:var(--ai-cobalt-deep);color:#fff;font-size:19px;font-weight:700}
.pc-go:hover{background:var(--ai-cobalt-press);color:#fff}
.pc-go:focus-visible{outline:3px solid var(--ai-ink);outline-offset:2px}
.pc-go.ghost{background:var(--ai-surface);color:var(--ai-cobalt-deep);
 border:2px solid var(--ai-rule-strong);font-weight:600}
.pc-go.ghost:hover{border-color:var(--ai-cobalt);color:var(--ai-cobalt-deep)}
.pc-id{margin-top:12px;font-size:18px;color:var(--ai-muted-strong);font-variant-numeric:tabular-nums}
.bh-list{display:flex;flex-direction:column;gap:14px;margin-top:6px}
.bh-row{border:1px solid var(--ai-rule);border-radius:12px;padding:16px 18px;background:var(--ai-surface)}
.bh-row.now{border-color:var(--ai-cobalt);background:var(--ai-soft)}
.bh-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:18px}
.bh-v-no{font-weight:700;color:var(--ai-ink)}
.bh-sw{width:22px;height:22px;border-radius:6px;border:1px solid var(--ai-rule-strong);display:inline-block}
.bh-when{color:var(--ai-muted-strong);font-variant-numeric:tabular-nums}
.bh-src{color:var(--ai-muted-strong)}
.bh-now{margin-left:auto;font-weight:700;color:var(--ai-cobalt-deep)}
.bh-chips{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
.bh-chip{display:inline-flex;gap:8px;align-items:baseline;font-size:18px;
 border:1px solid var(--ai-rule);border-radius:8px;padding:6px 12px}
.bh-k{color:var(--ai-muted-strong)}
.bh-v{color:var(--ai-ink);font-weight:600}
.bh-go{margin-top:14px;font-size:19px;font-weight:700;color:#fff;background:var(--ai-cobalt-deep);
 border:0;border-radius:10px;padding:0 24px;min-height:var(--ai-hit);cursor:pointer}
.bh-go:hover{background:var(--ai-cobalt-press)}
.bh-go:focus-visible{outline:3px solid var(--ai-ink);outline-offset:2px}
.ms-note{margin-top:26px;font-size:18px;line-height:1.7;color:var(--ai-muted-strong);
 border-left:4px solid var(--ai-cobalt);padding:10px 0 10px 14px}
/* 売り文句の候補（マイソク作成の最後の画面） */
.cs-wrap{margin:6px 0 26px}
.cs-h{font-size:19px;font-weight:700;color:var(--ai-ink);display:flex;align-items:baseline;
 gap:10px;flex-wrap:wrap;margin-bottom:10px}
.cs-sub{font-size:18px;font-weight:400;color:var(--ai-muted-strong)}
.cs-chips{display:flex;flex-direction:column;gap:10px}
.cs-chip{display:flex;flex-direction:column;align-items:flex-start;gap:4px;width:100%;
 min-height:var(--ai-hit);padding:12px 16px;border:2px solid var(--ai-rule-strong);border-radius:12px;
 background:#fff;cursor:pointer;text-align:left}
.cs-chip:hover{border-color:var(--ai-cobalt);background:var(--ai-soft)}
.cs-chip:focus-visible{outline:3px solid var(--ai-ink);outline-offset:2px}
.cs-chip.on{border-color:var(--ai-cobalt-deep);background:var(--ai-soft)}
.cs-kind{font-size:18px;font-weight:700;color:var(--ai-cobalt-deep)}
.cs-text{font-size:20px;font-weight:600;color:var(--ai-ink);line-height:1.5}
.cs-basis{font-size:18px;color:var(--ai-muted-strong);line-height:1.5}
.cs-note,.cs-empty{margin-top:10px;font-size:18px;line-height:1.7;color:var(--ai-muted-strong)}
/* 広告としての表示チェック（印刷の手前） */
.adc-wrap{margin-top:4px}
.adc-ok{font-size:19px;font-weight:600;color:var(--ai-ink);background:var(--ai-soft);
 border-left:5px solid var(--ai-cobalt-deep);border-radius:6px;padding:14px 18px;line-height:1.7}
.adc-ng{font-size:19px;font-weight:700;color:#8a1b12;background:#fdeceb;
 border-left:5px solid #b3261e;border-radius:6px;padding:14px 18px;line-height:1.7}
.adc-list{list-style:none;margin:14px 0 0;padding:0;display:flex;flex-direction:column;gap:12px}
.adc-i{border:2px solid var(--ai-rule-strong);border-radius:12px;padding:14px 18px;background:#fff}
.adc-i.adc-block{border-color:#b3261e}
.adc-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.adc-badge{font-size:18px;font-weight:700;color:#fff;background:var(--ai-muted-strong);
 border-radius:999px;padding:3px 12px}
.adc-block .adc-badge{background:#b3261e}
.adc-where{font-size:18px;color:var(--ai-muted-strong)}
.adc-term{font-size:20px;color:var(--ai-ink)}
.adc-why,.adc-sug{font-size:18px;line-height:1.7;color:var(--ai-ink)}
.adc-sug{margin-top:6px;color:var(--ai-cobalt-deep);font-weight:600}
.adc-src{margin-top:6px;font-size:18px;color:var(--ai-muted-strong)}
.adc-foot{margin-top:14px;font-size:19px;line-height:1.7;display:flex;align-items:center;
 gap:12px;flex-wrap:wrap}
.adc-note{margin-top:16px;font-size:18px;line-height:1.7;color:var(--ai-muted-strong);
 border-left:4px solid var(--ai-rule);padding:8px 0 8px 14px}
.sb-bt{font-family:var(--head);font-weight:700;font-size:19px;color:var(--sumi);display:flex;flex-direction:column;line-height:1.15;letter-spacing:0}
.sb-bs{font-weight:400;font-size:18px;color:var(--ai-muted-strong);letter-spacing:0;margin-top:4px;line-height:1.45}
.sb-nav{display:flex;flex-direction:column;gap:1px;margin-top:8px}
.nav-i{display:flex;align-items:center;gap:10px;padding:0 10px;min-height:var(--ai-hit);border-radius:4px;color:var(--ink2);
 font-size:18px;font-weight:500;position:relative;transition:background .12s ease,color .12s ease}
.nav-i .ic{width:18px;height:18px;color:var(--muted);flex:none;transition:color .12s ease}
.nav-i:hover{background:var(--panel2);color:var(--sumi)}
.nav-i:hover .ic{color:var(--sumi)}
.nav-i.on{background:var(--accent-bg);color:var(--sumi);font-weight:700;box-shadow:inset 3px 0 0 var(--sumi)}
.nav-i.on .ic{color:var(--sumi)}
.nav-i .nl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nav-i .nb{font-family:var(--head);font-size:18px;font-weight:700;color:var(--ink2);background:var(--line2);
 border-radius:3px;padding:0 6px;min-width:18px;text-align:center;font-variant-numeric:tabular-nums}
.nav-i .nb.hot{background:var(--vermi);color:#fff}
.nav-i.on .nb{background:var(--ai-cobalt-deep);color:#fff}
.sb-gh{font-family:var(--head);font-size:18px;font-weight:700;letter-spacing:.06em;color:var(--muted);padding:16px 10px 6px}
.sb-detail-desktop{display:contents}
.sb-detail-mobile{display:none}
.sb-foot{margin-top:auto;padding:12px 10px 4px;border-top:1px solid var(--line);font-size:18px}
.sb-who{color:var(--sumi);font-weight:600;margin-bottom:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-logout{color:var(--muted);font-size:18px;display:inline-flex;align-items:center;min-height:var(--ai-hit)}.sb-logout:hover{color:var(--sumi)}

/* ---- Page header ---- */
.ph{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin:0 0 20px;
 padding-bottom:12px;border-bottom:1px solid var(--line)}
.ph h1{font-size:20px;font-weight:700;letter-spacing:0}
.ph-sub{color:var(--muted);font-size:18px;margin:4px 0 0;line-height:1.6}
.ph-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center}

/* ---- Command / quick chat (home) ---- */
.hello{font-size:18px;color:var(--ink2);margin-bottom:10px;font-weight:500}
.ri-cmd{display:flex;align-items:center;gap:12px;background:var(--panel);border:1px solid var(--line);
 border-radius:var(--r2);padding:12px 14px}
.ri-cmd:focus-within{border-color:var(--sumi);box-shadow:0 0 0 3px var(--accent-bg)}
.ri-ai{width:30px;height:30px;border-radius:4px;flex:none;position:relative;background:var(--ai-cobalt-deep)}
.ri-ai::after{content:"あ";position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#fff;font-family:var(--head);font-weight:800;font-size:19px}
.ri-cmd-text{flex:1;font-size:19px;color:var(--muted);border:none;background:none;outline:none;font-family:var(--body)}
input.ri-cmd-text::placeholder{color:var(--muted2)}
.ri-send{width:36px;height:36px;border-radius:5px;background:var(--ai-cobalt-deep);color:#fff;flex:none;border:none;cursor:pointer;
 display:flex;align-items:center;justify-content:center}
.ri-send::after{content:"";width:8px;height:8px;border-top:2px solid #fff;border-right:2px solid #fff;transform:rotate(45deg);margin-left:-3px}
.ri-ai-state{font-size:18px;color:var(--muted);margin:8px 2px 16px}
.ri-examples{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:28px}
.ri-chip{font-size:18px;border:1px solid var(--line);border-radius:5px;padding:7px 12px;min-height:var(--ai-hit);display:inline-flex;align-items:center;color:var(--ink2);background:var(--panel);font-weight:500;transition:border-color .12s ease,color .12s ease,background .12s ease}
.ri-chip:hover{border-color:var(--sumi);color:var(--sumi);background:var(--accent-bg)}

/* ---- Sections ---- */
.ri-sech{font-family:var(--head);font-size:18px;font-weight:700;letter-spacing:.04em;color:var(--sumi);margin:26px 0 10px}
.ri-sech:first-child{margin-top:0}
.ri-grid2{display:grid;grid-template-columns:1fr 1fr;gap:26px;margin-bottom:8px}

/* ---- KPIs (枠なしメトリクス=Stripe式) ---- */
.ri-kpis,.kpis{display:flex;gap:44px;flex-wrap:wrap;margin:4px 0 26px}
.kpi{display:inline-block;background:none;border:none;padding:0;color:inherit;min-width:var(--ai-hit);min-height:var(--ai-hit)}  /* 数字が短くても押せる幅を保つ */
.kpi .n{font-family:var(--head);font-weight:700;font-size:26px;line-height:1.1;color:var(--sumi);font-variant-numeric:tabular-nums;letter-spacing:0}
.kpi .l{font-size:18px;color:var(--muted);margin-top:4px;font-weight:500}
.kpi.red .n{color:var(--vermi)}
.kpi.org .n{color:var(--sumi)}
.kpi.green .n{color:var(--sumi)}
.kpi.blue .n{color:var(--sumi)}

/* ---- Cards / tasks ---- */
.ri-card{display:block;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
 padding:13px 15px;margin-bottom:10px;color:inherit;transition:border-color .12s ease}
a.ri-card:hover{border-color:var(--sumi)}
.ri-card .ct{font-family:var(--head);font-weight:700;font-size:19px;display:flex;justify-content:space-between;
 align-items:center;gap:10px;color:var(--sumi)}
.ri-card .cm{font-size:18px;color:var(--muted);margin-top:5px;line-height:1.6}
.ri-tasks{padding:0}
.ri-task{display:flex;align-items:center;gap:11px;padding:10px 2px;border-bottom:1px solid var(--line);color:inherit;min-height:36px}
.ri-task:last-child{border-bottom:none}
.ri-tk{width:6px;height:6px;border-radius:1px;background:var(--ai-cobalt);flex:none}
.ri-task .tt{font-size:18px;min-width:0;overflow-wrap:anywhere;color:var(--ink)}
.ri-task .tm{margin-left:auto;font-size:18px;color:var(--muted);white-space:nowrap;font-weight:500;font-variant-numeric:tabular-nums}
.ri-empty{color:var(--muted);background:var(--panel);border:1px dashed var(--line);border-radius:var(--r);
 padding:22px;text-align:center;font-size:18px}
.ri-trust{display:flex;gap:7px;flex-wrap:wrap;margin-top:20px}
.trust{font-size:18px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:4px 10px;background:var(--panel)}
.ri-guide{border:1px solid var(--line);border-radius:var(--r);background:var(--panel);padding:14px 16px}
.ri-guide .gh{font-family:var(--head);font-weight:700;font-size:18px;margin-bottom:6px;color:var(--sumi)}
.ri-guide .gb{font-size:18px;color:var(--ink2);line-height:1.7}
.gn{font-size:18px;color:var(--muted);margin-top:5px}

/* ---- Badges (状態=無彩スレート＋機能色2つ: 確定=緑/要確認・超過=朱書き) ---- */
.ri-badge,.badge,.pill,.qchip,.prio{display:inline-block;font-family:var(--head);font-size:18px;font-weight:600;
 border-radius:3px;padding:2px 7px;white-space:nowrap;line-height:1.5}
.ri-badge.ok,.badge.ok,.pill.done{background:var(--ok-bg);color:var(--ok)}
.ri-badge.warn,.badge,.pill.draft{background:var(--warn-bg);color:var(--warn)}
.ri-badge.bad,.badge.bad{background:var(--vermi-bg);color:var(--vermi)}
.qchip{background:var(--line2);color:var(--ink2)}
.b-red{background:var(--vermi-bg);color:var(--vermi)}.b-org{background:var(--warn-bg);color:var(--warn)}
.b-green{background:var(--ok-bg);color:var(--ok)}.b-yellow{background:var(--warn-bg);color:var(--warn)}
.b-blue{background:var(--line2);color:var(--ink2)}.b-gray{background:var(--line2);color:var(--muted)}
.prio.p0{background:var(--vermi);color:#fff}.prio.p1{background:var(--warn-bg);color:var(--warn)}.prio.p2{background:var(--line2);color:var(--muted)}
.g-send{background:var(--line2);color:var(--ink2)}.g-publish{background:var(--accent-bg);color:var(--sumi)}.g-contract{background:var(--line2);color:var(--ink2)}
.g-money{background:var(--ok-bg);color:var(--ok)}.g-privacy{background:var(--warn-bg);color:var(--warn)}.g-prof{background:var(--line2);color:var(--ink2)}
.g-doc{background:var(--line2);color:var(--ink2)}.g-tos{background:var(--line2);color:var(--muted)}.g-optin{background:var(--line2);color:var(--ink2)}
.g-warn{background:var(--warn-bg);color:var(--warn)}.g-gray{background:var(--line2);color:var(--muted)}
.due-over{color:var(--vermi);border-bottom:1px solid var(--vermi);padding-bottom:1px}

/* ---- Tables (台帳: 罫のみ・ゼブラ禁止・上下実線) ---- */
.tablewrap{overflow-x:auto;border:none;border-radius:0;background:var(--panel);
 border-top:1px solid var(--sumi);border-bottom:1px solid var(--sumi)}
table{border-collapse:collapse;width:100%;font-size:18px}
thead th{background:var(--panel);color:var(--muted);position:sticky;top:0;z-index:1;text-align:left;padding:8px 10px;
 white-space:nowrap;font-family:var(--head);font-weight:500;font-size:18px;border-bottom:1px solid var(--line)}
thead th a{color:var(--muted)}thead th a .arr{color:var(--sumi);font-size:18px}
tbody td{padding:0 10px;height:36px;border-bottom:1px solid var(--line);vertical-align:middle;max-width:340px;word-break:break-word;color:var(--ink)}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--panel2)}
tr.p0row{box-shadow:inset 3px 0 0 var(--vermi)}tr.p0row td:first-child{font-weight:700}
.caselink{font-family:var(--mono);font-size:18px;color:var(--muted);font-weight:500}
.case-sec{margin-bottom:20px}
.case-sec h3{font-size:19px;margin:0 0 8px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.facets{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px;align-items:center}
.facets .flabel{font-size:18px;color:var(--muted)}
.facet{display:inline-flex;align-items:center;min-height:var(--ai-hit);padding:0 14px;border:1px solid var(--line);border-radius:4px;font-size:18px;background:var(--panel);color:var(--ink2)}
.facet:hover{border-color:var(--sumi)}
.facet.on{background:var(--ai-cobalt-deep);color:#fff;border-color:var(--ai-cobalt-deep)}
.facet .fc{color:var(--muted2);margin-left:4px;font-size:18px}.facet.on .fc{color:#c9d6e4}
.clearf{font-size:18px;color:var(--sumi);margin-left:6px;text-decoration:underline}
.empty{color:var(--muted);padding:22px;text-align:center;background:var(--panel2);border:1px dashed var(--line);border-radius:var(--r)}
.pcount{font-size:18px;color:var(--muted);font-weight:400}
.pdesc{color:var(--muted);font-size:18px;margin:2px 0 14px}
h2.page{font-size:20px;margin:0 0 2px}

/* ---- Panels ---- */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:13px 15px;margin-bottom:16px}
.panel h3{margin:0 0 10px;font-size:18px}
.alist{list-style:none;margin:0;padding:0}
.alist li{padding:7px 0;border-bottom:1px solid var(--line);font-size:18px;display:flex;gap:8px;align-items:flex-start}
.alist li:last-child{border-bottom:none}
.alist .a-meta{color:var(--muted);font-size:18px;font-variant-numeric:tabular-nums}

/* ---- Buttons (濃紺=主・確定のみ緑) ---- */
.ri-btn,.rc-btn{font-family:var(--head);font-weight:600;border:1px solid var(--ai-cobalt-deep);border-radius:4px;
 padding:8px 14px;background:var(--ai-cobalt-deep);color:#fff;font-size:18px;cursor:pointer;white-space:nowrap;display:inline-block}
.ri-btn:hover,.rc-btn:hover{background:#12395f}
.rc-btn.ghost{background:var(--panel);color:var(--sumi)}.rc-btn.ghost:hover{background:var(--panel2)}
.ri-go{font-family:var(--head);font-weight:600;border:1px solid var(--ai-cobalt-deep);background:var(--ai-cobalt-deep);color:#fff;
 border-radius:4px;padding:7px 12px;cursor:pointer;font-size:18px;margin:6px 6px 0 0}
.ri-go:hover{background:#12395f}
.ri-go.ghost{background:var(--panel);color:var(--sumi);border-color:var(--line)}
.ri-go.ghost:hover{background:var(--panel2);border-color:var(--sumi)}
.ri-go.confirm,.ri-btn.confirm{background:var(--ok);border-color:var(--ok)}
.ri-actform{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
.ri-actform input,.ri-actform select{border:1px solid var(--line);border-radius:4px;padding:7px 9px;font-family:var(--body);font-size:18px;background:var(--panel);color:var(--ink)}
.ri-actform input:focus,.ri-actform select:focus{outline:none;border-color:var(--sumi)}
.ri-actform .lbl{font-size:18px;color:var(--muted);font-weight:600}

/* ---- 重説 (juusetsu: 左=項目台帳 右=紙面の思想) ---- */
.ri-ju{display:grid;grid-template-columns:1.15fr .85fr;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.ri-doc{padding:26px 30px;border-right:1px solid var(--line)}
.ri-doc .dh{display:flex;justify-content:space-between;align-items:center;gap:18px;margin-bottom:18px;padding-bottom:10px;border-bottom:1px solid var(--sumi)}
.ri-doc h2{font-size:20px}
.ri-clause{margin-bottom:16px}
.ri-clause .cn{font-family:var(--head);font-size:18px;color:var(--sumi);font-weight:700;margin-bottom:5px}
.ri-clause .cb{font-family:var(--serif);font-size:18px;line-height:1.95;color:var(--ink);overflow-wrap:anywhere}
.ri-clause .hl{background:var(--accent-bg)}
.ri-insp{padding:24px 22px;background:var(--panel2)}
.ri-insp h3{font-size:19px;margin-bottom:4px}
.ri-insp .ih{font-size:18px;color:var(--muted);margin-bottom:16px}
.ri-item{display:flex;gap:10px;padding:11px 0;border-bottom:1px solid var(--line)}
.tick{width:17px;height:17px;border-radius:3px;background:var(--ok-bg);flex:none;position:relative}
.tick::after{content:"";position:absolute;left:6px;top:4px;width:4px;height:7px;border:solid var(--ok);border-width:0 2px 2px 0;transform:rotate(45deg)}
.tick.q{background:var(--vermi-bg)}
.tick.q::after{border:none;content:"!";color:var(--vermi);left:6px;top:0;font-weight:800;font-size:18px;font-family:var(--head)}
.ri-item .it{font-size:18px;font-weight:600;color:var(--ink)}
.ri-item .id{font-size:18px;color:var(--muted);margin-top:3px;line-height:1.5}
.src{font-size:18px;color:var(--muted2);margin-top:4px;overflow-wrap:anywhere}
.hazard{margin-top:10px;padding:12px 14px;border:1px solid var(--line);border-radius:var(--r);background:var(--panel)}
.hazard .hl{font-family:var(--head);font-size:18px;color:var(--muted);margin-bottom:5px}
.hazard .hv{font-size:18px;line-height:1.6}
.soon{font-family:var(--head);font-size:18px;color:var(--muted2);border:1px solid var(--line);border-radius:3px;padding:1px 7px;margin-left:6px;white-space:nowrap}
.ri-gate{grid-column:1 / -1;border-top:1px solid var(--sumi);background:var(--panel);padding:18px 30px;display:flex;align-items:center;gap:20px}
.ri-gate .gt{font-family:var(--head);font-weight:700;font-size:19px;color:var(--sumi)}
.ri-gate .gd{font-size:18px;color:var(--muted);margin-top:4px;line-height:1.5;max-width:470px}
.sig{display:flex;gap:8px;margin-left:auto;align-items:center}
.sig input{width:156px;border:1px solid var(--line);border-radius:4px;padding:9px 11px;font-family:var(--body);font-size:18px}
.audit{grid-column:1 / -1;padding:0 30px 14px}
.auditline{font-family:var(--mono);font-size:18px;color:var(--ok);background:var(--ok-bg);border-radius:4px;padding:10px 13px;overflow-wrap:anywhere}
.ri-alert{grid-column:1 / -1;margin:0 30px 14px;border-radius:4px;padding:10px 13px;font-size:18px}
.ri-alert.err{background:var(--vermi-bg);color:var(--vermi)}.ri-alert.ok{background:var(--ok-bg);color:var(--ok)}
.credit{grid-column:1 / -1;font-size:18px;color:var(--muted2);padding:6px 30px 16px}
.pill.draft{background:var(--warn-bg);color:var(--warn)}.pill.done{background:var(--ok-bg);color:var(--ok)}

/* ---- Console 3ペイン(会話 | 書類 | 承認) ---- */
.rc-main{padding:0;display:grid;grid-template-columns:308px minmax(0,1fr) 296px;height:100vh;min-height:0}
.rc-left{border-right:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;min-height:0}
.rc-right{border-left:1px solid var(--line);background:var(--panel);overflow:auto;padding:16px 14px;min-height:0}
.rc-right .ri-right-h{margin-top:0}
.rc-threads{border-bottom:1px solid var(--line);padding:12px 12px 9px;display:flex;flex-direction:column;min-height:0}
.rc-threads-h{display:flex;align-items:center;justify-content:space-between;font-family:var(--head);font-size:18px;font-weight:700;
 color:var(--sumi);margin-bottom:7px}
.rc-new{font-size:18px;color:var(--sumi);font-weight:700;text-decoration:underline;display:inline-flex;align-items:center;min-height:48px;padding:0 6px}
.rc-thread-list{overflow:auto;max-height:188px;display:flex;flex-direction:column;gap:2px}
.rc-thread{display:block;border:1px solid transparent;border-radius:4px;padding:6px 8px;color:inherit}
.rc-thread:hover{background:var(--panel2);border-color:var(--line)}
.rc-thread.on{background:var(--accent-bg);border-color:var(--accent-line)}
.rc-tt{font-size:18px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink)}
.rc-tm{font-size:18px;color:var(--muted2);margin-top:2px;font-variant-numeric:tabular-nums}
.rc-chat{flex:1;display:flex;flex-direction:column;min-height:0;padding:14px}
.rc-cv .turn{margin:10px 0}.rc-cv .turn b{font-family:var(--head);font-size:18px;color:var(--sumi)}
.rc-cv .turn .tx{white-space:pre-wrap;margin-top:3px;color:var(--ink)}
.rc-chat-h{font-family:var(--head);font-size:18px;font-weight:700;color:var(--sumi);margin-bottom:8px}
.rc-mode{font-size:18px;color:var(--muted);line-height:1.5;margin-bottom:9px}
.rc-cv{flex:1;overflow:auto;border:1px solid var(--line);border-radius:var(--r);padding:13px;background:var(--panel2);font-size:18px;line-height:1.7}
.rc-cv .ph{color:var(--muted);font-size:18px;line-height:1.8;border:none;display:block;margin:0;padding:0}
.rc-form{display:flex;gap:8px;margin-top:10px}
.rc-form input{flex:1;border:1px solid var(--line);border-radius:5px;padding:10px;font-family:var(--body);font-size:18px}
.rc-form input:focus{outline:none;border-color:var(--sumi)}
.rc-cost{font-size:18px;color:var(--muted);margin-top:6px;min-height:14px;font-variant-numeric:tabular-nums}
.rc-center{overflow:auto;padding:22px 26px;min-width:0}
.rc-h{font-family:var(--head);font-size:20px;margin:0 0 6px;color:var(--sumi)}
.rc-lead{color:var(--muted);font-size:18px;line-height:1.7;margin-bottom:13px}
.rc-sech{font-family:var(--head);font-size:18px;font-weight:700;letter-spacing:.04em;color:var(--sumi);margin:18px 0 8px}
.rc-docgrid{display:grid;grid-template-columns:1fr 1fr;gap:11px}
.rc-doccard{display:block;border:1px solid var(--line);border-radius:var(--r);background:var(--panel);padding:13px;color:inherit}
.rc-doccard:hover{border-color:var(--sumi)}
.rc-doctitle{font-family:var(--head);font-size:19px;margin-bottom:4px;color:var(--sumi);font-weight:700}
.rc-card{border:1px solid var(--line);border-radius:var(--r);background:var(--panel);padding:12px;margin-bottom:9px}
.rc-cardh{font-family:var(--head);font-size:18px;color:var(--sumi);font-weight:700}
.rc-cardm{color:var(--muted);font-size:18px;margin:4px 0 8px;line-height:1.6}
.rc-empty{color:var(--muted2);font-size:18px;padding:8px 2px}
.rc-doc{white-space:pre-wrap;font-family:var(--serif);font-size:19px;line-height:1.9;border:1px solid var(--line);border-radius:var(--r);background:var(--panel);padding:22px;color:var(--ink)}
.rc-diff{white-space:pre-wrap;font-size:18px;background:var(--panel2);border:1px solid var(--line);border-radius:4px;padding:11px;overflow:auto;font-family:var(--mono)}
textarea.rc-edit{width:100%;min-height:54vh;border:1px solid var(--line);border-radius:var(--r);padding:14px;font-family:var(--mono);font-size:18px;line-height:1.7;resize:vertical}
.rc-actions{display:flex;gap:8px;margin:6px 0 14px}
.rc-toast{background:var(--accent-bg);color:var(--sumi);border-radius:4px;padding:9px 13px;font-size:18px;margin-bottom:11px;font-weight:600}
.rc-md h1{font-size:20px;margin:2px 0 9px}.rc-md h2{font-size:19px;margin:14px 0 7px}.rc-md h3{font-size:18px;margin:12px 0 6px}
.rc-md p{margin:0 0 9px}.rc-md ul{padding-left:1.3em;margin:0 0 9px}.rc-md li{margin:3px 0}
.rc-md table.md-tbl{border-collapse:collapse;width:100%;margin:9px 0}.rc-md .md-tbl td{border:1px solid var(--line);padding:6px 9px;font-size:18px}
.rc-finform{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:4px}
.rc-finform input{border:1px solid var(--line);border-radius:4px;padding:7px 9px;font-family:var(--body);font-size:18px}
.chip{font-family:var(--head);font-size:18px;color:var(--ink2);border:1px solid var(--line);border-radius:4px;padding:4px 10px;background:var(--panel)}
.chip.on{background:var(--ai-cobalt-deep);color:#fff;border-color:var(--ai-cobalt-deep)}
.ms-tabs .chip{display:inline-flex;align-items:center;min-height:48px;padding:5px 12px}
.chip-auto{display:inline-flex;gap:6px;align-items:center;font-size:18px;color:var(--muted);border:1px solid var(--line);border-radius:3px;padding:2px 8px}

/* ---- PRS災害リスクブロック(第一級・物件詳細) ---- */
.prs{display:flex;gap:40px;align-items:flex-end;padding:14px 0 8px;flex-wrap:wrap}
.prs .peril .name{font-size:18px;color:var(--ink2);margin-bottom:2px}
.prs .peril .score{font-size:28px;font-weight:700;color:var(--sumi);line-height:1;font-variant-numeric:tabular-nums}
.prs .peril .score small{font-size:18px;color:var(--muted);font-weight:400}
.prs .peril .bar{height:3px;background:var(--line);margin-top:6px;width:96px}
.prs .peril .bar i{display:block;height:3px;background:var(--ai-cobalt)}
.prs .peril .cal{font-size:18px;color:var(--muted);margin-top:4px}
.prs .peril.na .score{color:var(--muted);font-size:19px;padding:7px 0}
.prs-note{font-size:18px;color:var(--muted);padding-bottom:12px;border-bottom:1px solid var(--line);margin-bottom:14px}

/* ---- 監査レール(全画面共通・右端) ---- */
.aev{padding:7px 0;border-bottom:1px solid var(--line);font-size:18px;line-height:1.5}
.aev .t{color:var(--muted)}
.aev .h{color:var(--muted2);font-size:18px;font-family:var(--mono)}
.aev .act{color:var(--ink)}
.aev.fin .act{color:var(--ok);font-weight:700}

/* ---- 紙モード(A4紙面) ---- */
.deskbg{background:var(--desk)}
.sheet{background:var(--panel);box-shadow:0 2px 8px rgba(10,37,64,.10);margin:0 auto;
 font-family:var(--serif);color:#1f2933}
.proof{border-bottom:2px solid var(--vermi)}
.pnum{color:var(--vermi);font-size:18px;vertical-align:super;font-family:var(--head)}

/* ---- Maisoku preview ---- */
.ms-tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.ms-preview-tools{display:flex;align-items:center;gap:14px;min-height:var(--ai-hit);margin:0 0 10px}
.ms-preview-open{flex:0 0 auto;min-width:220px;min-height:var(--ai-hit);font-size:18px}
.ms-preview-note{font-size:18px;line-height:1.5;color:var(--ai-muted-strong)}
.ms-frame{width:100%;height:660px;border:1px solid var(--line);border-radius:var(--r);background:var(--desk);display:block}
.ms-mobile-summary{display:none}
.msm-kicker{font-size:18px;font-weight:700;color:var(--ai-cobalt-deep);letter-spacing:.02em}
.msm-name{font-size:26px;line-height:1.35;margin:5px 0 2px;color:var(--ai-ink)}
.msm-variant{font-size:18px;color:var(--ai-muted-strong);padding-bottom:12px;border-bottom:1px solid var(--ai-rule)}
.msm-facts{margin:0}.msm-row{display:grid;grid-template-columns:76px minmax(0,1fr);gap:12px;padding:10px 0;border-bottom:1px solid var(--ai-rule)}
.msm-row dt{font-size:18px;color:var(--ai-muted-strong)}.msm-row dd{margin:0;font-size:19px;font-weight:600;overflow-wrap:anywhere}
.msm-actions{display:grid;gap:10px;margin-top:16px}.msm-actions .ri-go{width:100%;justify-content:center;text-align:center}
.msm-note{font-size:18px;line-height:1.65;color:var(--ai-muted-strong);margin:14px 0 0}
.ms-pa{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 4px}
/* ---- Maisoku/Doc form editor ---- */
.mf-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:24px;align-items:start}
.mf-left{min-width:0}.mf-right{min-width:0;position:sticky;top:16px}
.mf-top{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.mf-sec{margin-bottom:16px}
.mf-row{display:grid;grid-template-columns:140px 1fr;gap:12px;align-items:start;margin-bottom:8px}
.mf-row .mf-l{font-size:18px;color:var(--ink2);padding-top:8px;font-weight:500}
.mf-row input,.mf-row textarea,.mf-row select{width:100%;border:1px solid var(--line);border-radius:4px;
 padding:8px 10px;font-family:var(--body);font-size:18px;background:var(--panel);color:var(--ink)}
.mf-row input:focus,.mf-row textarea:focus,.mf-row select:focus{outline:none;border-color:var(--sumi)}
.mf-row textarea{resize:vertical;line-height:1.6}
.mf-actions{display:flex;gap:8px;align-items:center;margin-top:6px}
@media(max-width:1024px){.mf-grid{grid-template-columns:1fr}.mf-right{position:static}}

/* ---- Right rail ---- */
.ri-right .rc-card{margin-bottom:9px}
.ri-right-h{font-family:var(--head);font-size:18px;font-weight:700;letter-spacing:.04em;color:var(--sumi);margin:0 0 10px}

/* ---- Search ---- */
.search input{width:100%;padding:8px 11px;border:1px solid var(--line);border-radius:4px;font-size:18px;background:var(--panel)}
.search input:focus{outline:none;border-color:var(--sumi)}
.ro{font-size:18px;color:var(--muted);background:var(--panel2);border:1px solid var(--line);border-radius:4px;padding:2px 8px;white-space:nowrap}
.updated{font-size:18px;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}

/* ---- 一覧リスト行（today等・カード乱立でなく罫リズムの行） ---- */
.lguide{font-size:18px;color:var(--ink2);margin:2px 0 14px;line-height:1.7;max-width:72ch}
.lguide-sub{display:block;font-size:18px;color:var(--muted);margin-top:3px}
.lcards{border-top:1px solid var(--sumi);border-bottom:1px solid var(--sumi)}
.lcard{display:block;padding:9px 2px;border-bottom:1px solid var(--line);color:inherit}
.lcard:last-child{border-bottom:none}
.lcard:hover{background:var(--panel2)}
.lc-head{display:flex;align-items:center;gap:9px;min-width:0}
.lc-prio{font-family:var(--head);font-size:18px;font-weight:700;border-radius:3px;padding:1px 6px;background:var(--line2);color:var(--muted);flex:none}
.lc-prio.p0{background:var(--vermi);color:#fff}
.lc-prio.p1{background:var(--warn-bg);color:var(--warn)}
.lc-title{font-size:18px;font-weight:600;color:var(--ink);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lc-meta{font-size:18px;color:var(--muted);margin:3px 0 0 0;padding-left:0}
.lref{font-family:var(--mono);font-size:18px;color:var(--muted2);margin-right:6px}
/* helper文の行長制御（字面craft） */
.prs-note,.gn,.pdesc{max-width:72ch}
/* モバイルで隠す補助列 */
@media(max-width:700px){.hm{display:none}}
/* 監査レールのグループ件数 */
.aev .n{font-family:var(--head);font-size:18px;font-weight:700;color:var(--muted);background:var(--line2);border-radius:3px;padding:0 5px;margin-left:4px}

/* ---- ページ拡張（case串刺し cw-* / タイムライン tl-* / パイプライン pl-*）＝単一正本へ統合 ---- */
.ri-card .ca{margin-top:11px}
.ri-where{font-size:18px;color:var(--muted)}
.cw-wrap{display:flex;flex-direction:column;gap:14px}
.cw-sec{border:1px solid var(--line);border-radius:4px;overflow:hidden}
.cw-h{font-family:var(--head);font-size:18px;background:var(--panel2);padding:10px 14px;border-bottom:1px solid var(--line)}
.cw-tw{overflow-x:auto}
.cw-sec table{border-collapse:collapse;width:100%;font-size:18px}
.cw-sec th,.cw-sec td{border-bottom:1px solid var(--line);padding:7px 10px;text-align:left;white-space:nowrap;overflow-wrap:anywhere}
.cw-sec th{color:var(--muted);font-family:var(--head);font-weight:600}
.tl-wrap{max-width:none}
.tl-stages{display:flex;flex-wrap:wrap;gap:0;border:1px solid var(--line);border-radius:4px;background:var(--panel);padding:4px;margin-bottom:20px}
.tl-st{flex:1 1 auto;min-width:78px;text-align:center;font-size:18px;padding:9px 6px;border-radius:7px;color:var(--muted2);position:relative;font-family:var(--head)}
.tl-st.done{color:var(--ink)}
.tl-st.done::before{content:"";position:absolute;left:0;right:0;top:50%;height:2px;background:var(--line2);z-index:0}
.tl-st.now{background:var(--accent);color:#fff;font-weight:700;box-shadow:0 1px 3px rgba(191,46,46,.3)}
.tl-st .d{font-size:18px;opacity:.7;display:block}
.tl-next{border:1px solid var(--line);border-left:4px solid var(--ink);border-radius:4px;background:var(--panel);padding:15px 18px;margin-bottom:22px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.tl-next.warn{border-left-color:var(--warn)}
.tl-next.hot{border-left-color:var(--accent);background:var(--accent-bg)}
.tl-next .na{font-family:var(--head);font-weight:700;font-size:19px;color:var(--ink)}
.tl-next .nd{font-size:18px;color:var(--muted);margin-top:3px}
.tl-due{font-size:18px;font-weight:700;white-space:nowrap;padding:5px 11px;border-radius:999px;font-variant-numeric:tabular-nums}
.tl-due.ok{background:var(--panel2);color:var(--muted)}
.tl-due.warn{background:var(--warn-bg);color:var(--warn)}
.tl-due.hot{background:var(--accent);color:#fff}
.tl-line{position:relative;margin-left:8px;padding-left:22px;border-left:2px solid var(--line2)}
.tl-ev{position:relative;padding:0 0 18px 4px}
.tl-ev::before{content:"";position:absolute;left:-29px;top:3px;width:11px;height:11px;border-radius:50%;background:var(--panel);border:2px solid var(--muted2)}
.tl-ev.law::before,.tl-ev.money::before{border-color:var(--accent);background:var(--accent)}
.tl-ev .et{font-family:var(--head);font-weight:600;font-size:18px;color:var(--ink)}
.tl-ev .em{font-size:18px;color:var(--muted);margin-top:2px}
.tl-ev .ew{font-size:18px;color:var(--muted2);margin-top:2px}
.tl-gate{font-size:18px;font-weight:700;padding:1px 7px;border-radius:4px;margin-left:6px;background:var(--accent-bg);color:var(--accent)}
.tl-attr{margin-bottom:5px}
.tl-attr .ag{font-family:var(--head);font-size:18px;font-weight:700;color:var(--ink2);letter-spacing:.05em;margin:14px 0 6px}
.tl-attr .ar{display:flex;justify-content:space-between;gap:10px;font-size:18px;padding:5px 0;border-bottom:1px solid var(--line2)}
.tl-attr .ak{color:var(--muted)}
.tl-attr .av{color:var(--ink);font-weight:600;text-align:right}
.tl-calc{background:var(--panel2);border-radius:4px;padding:10px 12px;margin:10px 0;font-size:18px}
.tl-calc b{color:var(--accent);font-size:19px}
.tl-doc{display:flex;align-items:center;gap:8px;font-size:18px;padding:5px 0}
.tl-doc .dc{width:15px;height:15px;border-radius:4px;border:1.5px solid var(--muted2);flex:none;font-size:18px;text-align:center;line-height:13px}
.tl-doc.ok .dc{background:var(--ok);border-color:var(--ok);color:#fff}
.tl-doc.miss .dc{border-color:var(--accent)}
.tl-doc.miss{color:var(--accent)}
.tl-prop{border:1px solid var(--line);border-radius:4px;padding:10px 12px;margin-bottom:9px}
.tl-prop .pn{font-family:var(--head);font-weight:700;font-size:18px}
.tl-prop .pm{font-size:18px;color:var(--muted);margin-top:3px}
.tl-prop .pr{font-size:18px;font-weight:700;padding:1px 7px;border-radius:4px;background:var(--ok-bg);color:var(--ok);margin-top:6px;display:inline-block}
.tl-folder{font-size:18px;color:var(--muted);background:var(--panel2);border-radius:7px;padding:9px 12px;margin:4px 0 12px}
.tl-folder code{color:var(--ink);font-weight:700;font-family:ui-monospace,Menlo,monospace}
.tl-up{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
.tl-up input[type=file]{font-size:18px;font-family:var(--body)}
.tl-photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(116px,1fr));gap:9px;margin-bottom:14px}
.tl-photos a{display:block;border:1px solid var(--line);border-radius:4px;overflow:hidden;background:var(--panel2);position:relative}
.tl-ph{aspect-ratio:4/3}
.tl-photos img{width:100%;height:100%;object-fit:cover;display:block}
.tl-photos .cap{position:absolute;left:0;right:0;bottom:0;background:rgba(22,24,29,.72);color:#fff;font-size:18px;padding:3px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tl-fr{display:flex;align-items:center;gap:10px;font-size:18px;padding:9px 12px;border:1px solid var(--line);border-radius:4px;margin-bottom:7px;background:var(--panel)}
.tl-fr .fk{font-size:18px;font-weight:700;padding:1px 7px;border-radius:4px;background:var(--accent-bg);color:var(--accent);white-space:nowrap}
.tl-fr .fs{margin-left:auto;font-size:18px;color:var(--muted2);white-space:nowrap}
.tl-fr a{font-weight:600;color:var(--accent)}
.tl-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:-10px 0 22px}
.tl-clog{display:inline-flex;gap:7px;align-items:center;flex-wrap:wrap}
.tl-clog select,.tl-clog input{border:1px solid var(--line);border-radius:6px;padding:7px 9px;font-size:18px;font-family:var(--body);background:var(--panel);color:var(--ink)}
.tl-clog input{min-width:160px}
.tl-head{display:flex;align-items:baseline;gap:13px;margin:0 0 20px;padding-bottom:14px;border-bottom:2px solid var(--sumi);position:relative;flex-wrap:wrap}
.tl-head::after{content:"";position:absolute;left:0;bottom:-2px;width:56px;height:2px;background:var(--accent)}
.tl-head .nm{font-family:var(--display);font-size:24px;font-weight:600;letter-spacing:.04em;color:var(--ink)}
.tl-head .lb{font-size:18px;color:var(--muted2);letter-spacing:.1em;margin-left:auto}
.pl-alert{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 18px}
.pl-a{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:4px;padding:9px 14px;background:var(--panel);font-size:18px;}
.pl-a b{font-family:var(--head);font-size:19px;font-variant-numeric:tabular-nums}
.pl-a.hot{border-left:4px solid var(--accent)}
.pl-a.hot b{color:var(--accent)}
.pl-a.warn{border-left:4px solid var(--warn)}
.pl-a.warn b{color:var(--warn)}
.pl-a.calm{border-left:4px solid var(--ok)}
.pl-a.calm b{color:var(--ok)}
.pl-row{display:flex;align-items:center;gap:13px;padding:11px 15px;border:1px solid var(--line);border-radius:4px;margin-bottom:8px;background:var(--panel);transition:border-color .14s ease,box-shadow .14s ease;}
.pl-row:hover{border-color:var(--accent);box-shadow:0 3px 12px rgba(22,24,29,.08)}
.pl-row.hot{border-left:4px solid var(--accent)}
.pl-row.warn{border-left:4px solid var(--warn)}
.pl-nm{font-family:var(--display);font-size:19px;font-weight:600;color:var(--ink);min-width:120px}
.pl-stage{font-size:18px;color:var(--ink2);background:var(--panel2);border-radius:999px;padding:3px 11px;font-weight:600;white-space:nowrap}
.pl-ondo{font-size:18px;font-weight:700;border-radius:4px;padding:2px 8px;white-space:nowrap}
.pl-ondo.a{background:var(--accent-bg);color:var(--accent)}
.pl-ondo.b{background:var(--warn-bg);color:var(--warn)}
.pl-ondo.c{background:var(--panel2);color:var(--muted)}
.pl-meta{margin-left:auto;font-size:18px;color:var(--muted);text-align:right;white-space:nowrap}
.pl-due{font-weight:700}
.pl-due.hot{color:var(--accent)}
.pl-due.warn{color:var(--warn)}
.pl-deal{font-size:18px;color:var(--muted2);border:1px solid var(--line);border-radius:4px;padding:1px 6px;white-space:nowrap}

/* ---- ワークリスト（今週アプローチ・/today冒頭） ---- */
.wl{border-top:1px solid var(--sumi);border-bottom:1px solid var(--sumi);margin-bottom:8px}
.wl-row{display:flex;align-items:center;gap:12px;padding:8px 2px;border-bottom:1px solid var(--line)}
.wl-row:last-child{border-bottom:none}
.wl-row:hover{background:var(--panel2)}
.wl-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.wl-title{font-size:18px;font-weight:600;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wl-reason{font-size:18px;color:var(--muted)}
.wl-meta{font-size:18px;color:var(--muted);white-space:nowrap;display:flex;gap:8px;align-items:center}
.wl-row form{margin:0!important}
.wl-row .ri-go{padding:3px 8px;font-size:18px;margin:0;background:var(--panel);color:var(--muted);border-color:var(--line)}
.wl-row .ri-go:hover{border-color:var(--sumi)}
.wl-det{margin:6px 0 14px;font-size:18px;color:var(--muted)}
.wl-det summary{cursor:pointer}
.wl-snoozed{display:flex;gap:10px;align-items:center;padding:5px 0;border-bottom:1px solid var(--line2)}
.wl-snoozed form{margin:0!important}
.wl-snoozed .ri-go{padding:3px 8px;font-size:18px;margin:0;background:var(--panel);color:var(--sumi);border-color:var(--line)}

/* ---- カンバン（/properties?view=kanban・読み取り専用切替） ---- */
.kb-board{display:grid;grid-template-columns:repeat(5,minmax(176px,1fr));gap:10px;overflow-x:auto;padding-bottom:6px}
.kb-col{background:var(--panel2);border:1px solid var(--line);border-radius:var(--r);padding:8px;min-width:0}
.kb-h{font-family:var(--head);font-size:18px;font-weight:700;color:var(--sumi);display:flex;justify-content:space-between;align-items:center;padding:2px 4px 8px}
.kb-card{display:block;background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:8px 10px;margin-bottom:6px;color:inherit}
.kb-card:hover{border-color:var(--sumi)}
.kb-t{font-size:18px;font-weight:600;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.kb-m{font-size:18px;color:var(--muted);margin-top:3px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.kb-empty{color:var(--muted2);font-size:18px;text-align:center;padding:10px 0}
.kb-more{font-size:18px;color:var(--muted);text-align:center;padding:4px 0}

/* ---- 抽出値の出典チップ（AIS-06型・hoverで出典詳細） ---- */
.ex-chip{font-family:var(--mono);font-size:18px;color:var(--muted);border:1px solid var(--line);
 border-radius:3px;padding:1px 6px;white-space:nowrap;cursor:help;border-bottom:1px dotted var(--sumi)}
.ex-chip:hover{color:var(--sumi);border-color:var(--sumi)}

/* ---- 一括操作UI（BULK-01） ---- */
.bulk-bar{display:flex;align-items:center;gap:14px;padding:8px 0;border-bottom:1px solid var(--sumi);flex-wrap:wrap}
.bulk-all{font-size:18px;color:var(--ink2);display:flex;align-items:center;gap:6px}
.bulk-bar .ri-go{margin:0}
.bulk-bar .ri-go:disabled{opacity:.45;cursor:not-allowed}
.bulk-list{border-bottom:1px solid var(--sumi);margin-bottom:8px}
.bulk-row{display:flex;align-items:center;gap:10px;padding:7px 2px;border-bottom:1px solid var(--line);cursor:pointer}
.bulk-row:last-child{border-bottom:none}
.bulk-row:hover{background:var(--panel2)}
.bulk-t{font-size:18px;color:var(--ink);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.bulk-m{font-size:18px;color:var(--muted);white-space:nowrap}

/* ---- Responsive ---- */
@media(max-width:1180px){.ri-ws3{grid-template-columns:var(--sb) 1fr}.ri-ws3 .ri-right{display:none}}
@media(max-width:1240px){.rc-main{grid-template-columns:288px minmax(0,1fr)}.rc-main .rc-right{display:none}}
@media(max-width:1080px){.rc-main{grid-template-columns:1fr;height:auto}.rc-left{border-right:none;border-bottom:1px solid var(--line)}.rc-docgrid{grid-template-columns:1fr}}
@media(max-width:900px){
 .ri-ws,.ri-ws3{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr);align-content:start}
 .ri-rail{position:static;height:auto;display:block;width:100%;border-bottom:1px solid var(--line);border-right:none;overflow:visible;padding:14px 10px 8px}
 .sb-brand{padding:4px 8px 12px;margin:0;border-bottom:none}.sb-foot{display:none}
 .sb-primary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));width:100%;gap:2px;margin-top:3px}
 .sb-primary .nav-i{min-width:0}
 /* 幅が狭くても**文字を消さない**。アイコンだけの列は、パソコンが苦手な人には
    何のボタンか分からない（画面が小さいほど困るのはその人たち）。
    横に並べて折り返し、文字は残す。 */
 .nav-i{padding:8px 12px;min-height:var(--ai-hit);gap:8px;justify-content:flex-start}
 .nav-i .nl{display:inline;font-size:18px}
 .ri-main{padding:18px 16px 44px}
 .ph{align-items:flex-start;flex-direction:column}
 .ph-actions{width:100%}
 .ri-kpis,.kpis{gap:24px}
 .ri-grid2,.grid2{grid-template-columns:1fr}
 .ri-ju{grid-template-columns:1fr}.ri-doc{border-right:none;border-bottom:1px solid var(--line)}
 .ri-cmd{flex-wrap:wrap}.ri-cmd-text{min-width:0}
 .ri-actform{width:100%}
 .ri-actform input,.ri-actform select{flex:1 1 100%;width:100%;min-width:0}
 .ri-actform button{flex:1 1 auto}
 .ri-guide .gb,.ri-card{overflow-wrap:anywhere}
 .ri-examples{width:100%}.ri-examples .ri-chip{flex:0 1 auto}
 .sb-detail-desktop{display:none}
 .sb-detail-mobile{display:block;width:100%;margin-top:4px;border-top:1px solid var(--line)}
 .sb-detail-toggle{display:flex;align-items:center;justify-content:space-between;min-height:var(--ai-hit);
  padding:0 12px;list-style:none;cursor:pointer;font-size:18px;font-weight:700;color:var(--ai-ink)}
 .sb-detail-toggle::-webkit-details-marker{display:none}
 .sb-detail-toggle::after{content:"";width:9px;height:9px;border-right:2px solid currentColor;
  border-bottom:2px solid currentColor;transform:rotate(45deg);margin:0 5px 5px 12px}
 .sb-detail-mobile[open]>.sb-detail-toggle::after{transform:rotate(225deg);margin-top:7px;margin-bottom:0}
 .sb-detail-mobile>.sb-nav{margin-top:0;padding-top:5px;border-top:1px solid var(--line)}
 .sb-detail-mobile>.sb-nav{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:2px}
 .sb-detail-mobile:not([open])>.sb-nav{display:none}
 .ms-preview-tools{align-items:stretch;flex-direction:column;gap:6px}
 .ms-preview-open{width:100%;min-width:0;text-align:center}
 .kb-board{grid-template-columns:repeat(5,220px)}
}
/* モバイルのワークリスト: flexのmin-content連鎖で横溢れするためブロック積みへ（CTA=リスケを確実に画面内へ） */
@media(max-width:700px){
 html,body{max-width:100%;overflow-x:hidden}
 .ri-main,.ri-main>*{min-width:0;max-width:100%}
 .ri-ju{grid-template-columns:minmax(0,1fr)}
 .ri-ju>*,.ri-item>div{min-width:0;max-width:100%}
 .ri-doc,.ri-insp{padding:18px 16px}
 .ri-doc .dh{align-items:flex-start;flex-wrap:wrap;gap:8px}
 .ri-gate{padding:16px;flex-wrap:wrap}
 .credit{padding:6px 16px 14px}
 .soon{display:inline-block;margin-left:0;white-space:normal}
 .ms-frame{display:none}
 .ms-mobile-summary{display:block;background:var(--ai-surface);border:1px solid var(--ai-rule-strong);border-left:5px solid var(--ai-cobalt);padding:18px 16px;margin:0 0 16px}
 .mf-row{grid-template-columns:1fr!important;gap:3px}.mf-row .mf-l{padding-top:0}
 .mf-top,.mf-actions,.ms-pa{align-items:stretch;flex-direction:column}
 .mf-top>*,.mf-actions>*,.ms-pa>*{width:100%;min-width:0}
 .sb-primary,.sb-detail-mobile>.sb-nav{grid-template-columns:repeat(2,minmax(0,1fr))}
 .wl-row{display:block}
 .wl-title{white-space:normal}
 .wl-meta{white-space:normal;margin:3px 0 0;display:block}
 .wl-meta .lref{display:inline-block;margin-top:2px}
 .wl-row form{margin:7px 0 2px!important;display:block}
}
.sov-badge{display:inline-flex;align-items:center;gap:7px;font-size:18px;font-weight:600;padding:5px 11px;border-radius:6px;margin:6px 0 2px}
.sov-badge .sov-dot{width:7px;height:7px;border-radius:50%}
.sov-ok{background:var(--ok-bg);color:var(--ok)}.sov-ok .sov-dot{background:var(--ok)}
.sov-warn{background:var(--ai-paper);color:var(--ai-seal-deep)}.sov-warn .sov-dot{background:var(--ai-seal)}
.ri-quick{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:14px 0 4px}
.ri-quick-l{font-size:18px;color:var(--muted);margin-right:2px}
.ri-qbtn{display:inline-flex;align-items:center;min-height:var(--ai-hit);padding:0 16px;border:1px solid var(--line);border-radius:7px;font-size:18px;font-weight:600;color:var(--sumi);background:#fff;text-decoration:none}
.ri-qbtn:hover{border-color:var(--sumi);background:var(--panel2)}
.conn-wrap{max-width:680px}
.conn-card{border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin:0 0 14px}
.conn-h{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.conn-t{font-family:var(--head);font-weight:700;font-size:19px;color:var(--sumi)}
.conn-d{font-size:18px;color:var(--muted);margin-bottom:10px}
.conn-note{font-size:18px;color:var(--ok);margin-bottom:8px}
.conn-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 16px;margin-bottom:8px}
.conn-row{display:flex;flex-direction:column;margin-bottom:8px}
.conn-row input{padding:8px 11px;border:1px solid var(--line);border-radius:6px;font-size:19px;background:#fff;color:var(--ink)}
.conn-row input:focus{outline:2px solid var(--sumi);outline-offset:-1px}
.conn-guide{font-size:18px;color:var(--ink2);line-height:1.6;margin:6px 0 10px;background:var(--panel2);padding:9px 12px;border-radius:6px}
.conn-guide code{font-family:var(--mono);background:#fff;padding:1px 5px;border-radius:3px;border:1px solid var(--line)}
.conn-guide a{color:var(--sumi);text-decoration:underline}
.conn-result{display:inline-block;margin-left:10px;font-size:18px;font-weight:600}
.conn-ok{color:var(--ok)}.conn-ng{color:var(--vermi)}
@media(max-width:640px){.conn-grid{grid-template-columns:1fr}}
/* 業者情報フォーム(/profile) */
.pf-wrap{max-width:640px}
.pf-set{border:1px solid var(--line);border-radius:8px;padding:16px 18px 4px;margin:0 0 16px}
.pf-set legend{font-family:var(--head);font-weight:700;font-size:18px;color:var(--sumi);padding:0 6px}
.pf-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px 20px}
.pf-f{display:flex;flex-direction:column;margin-bottom:12px}
.pf-f-color{grid-column:1/-1}
.pf-l{font-size:18px;font-weight:600;color:var(--sumi);margin-bottom:5px}
.pf-f input[type=text],.pf-f select{padding:8px 11px;border:1px solid var(--line);border-radius:6px;font-size:19px;background:#fff;color:var(--ink);width:100%}
.pf-f input:focus,.pf-f select:focus{outline:2px solid var(--sumi);outline-offset:-1px;border-color:var(--sumi)}
.pf-color{display:flex;align-items:center;gap:10px}
.pf-color input[type=color]{width:44px;height:32px;border:1px solid var(--line);border-radius:6px;padding:2px;background:#fff;cursor:pointer}
.pf-color-v{font-family:var(--mono);font-size:18px;color:var(--muted)}
.pf-hint{font-size:18px;color:var(--muted);margin-top:4px}
.pf-saved{background:var(--ok-bg);color:var(--ok);font-size:18px;font-weight:600;padding:9px 14px;border-radius:6px;margin-bottom:16px}
.pf-actions{margin-top:4px}
/* 主要操作の当たり判定の下限（C3=48x48）。個別の見た目は各クラスが決め、下限だけをここで担保する。 */
.ri-go,.ri-btn,.ri-qbtn,.facet,button,summary,
input:not([type=hidden]):not([type=checkbox]):not([type=radio]),
select,textarea{min-height:var(--ai-hit)}
.ri-go,.ri-btn,button,input[type=submit]{min-width:var(--ai-hit)}
@media(max-width:640px){.pf-grid{grid-template-columns:1fr}}

/* ============================================================================
   あいのて 窓口型 UI
   ruling idea: 画面は店の受付台。やりたいことが書かれた大きな札を1枚選ぶと、
   その仕事だけが目の前に出る。boldness は「大札」1点に集中させ、周りは静かにする。
   ========================================================================== */

/* 見出し（窓口の問い） */
.ai-ask{font-family:var(--display);font-size:31px;font-weight:400;line-height:1.4;
 color:var(--ai-ink);margin:2px 0 4px;letter-spacing:.01em}
.ai-ask-s{font-size:var(--ai-text);color:var(--ai-muted-strong);margin:0 0 20px}

/* --- signature: 大札（おおふだ）--- 受付台に差さった木札。下辺の青が差し込み口 --- */
.ai-fudas{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:0 0 30px}
.ai-fuda{display:block;position:relative;background:var(--ai-surface);
 border:1px solid var(--ai-rule-strong);border-radius:4px;padding:20px 22px 50px;
 min-height:156px;color:var(--ai-ink);transition:transform .16s ease,border-color .16s ease,background .16s ease}
.ai-fuda .t{font-family:var(--display);font-size:28px;font-weight:400;line-height:1.34;margin:0 0 8px}
.ai-fuda .d{margin:0;font-size:var(--ai-text);color:var(--ai-muted-strong);line-height:1.6}
.ai-fuda .go{position:absolute;left:24px;bottom:17px;font-size:var(--ai-text);font-weight:700;
 color:var(--ai-cobalt-deep)}
.ai-fuda:hover{transform:translateY(-2px);border-color:var(--ai-cobalt);background:var(--ai-soft)}
.ai-fuda:active{transform:translateY(1px)}

/* --- signature: 結び線 ---
   2つの輪が重なる結び目を、一本のcobalt線が次の仕事へ渡す。進捗装飾ではなく
   「会社→物件→顧客→書類」の実データ状態を表す、あいのて固有の現在地。 */
.ai-first{border:1px solid var(--ai-rule-strong);background:var(--ai-surface);padding:18px 20px 16px;
 border-radius:4px;margin:0 0 24px;overflow:hidden}
.ai-first-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.ai-first-title{font-family:var(--display);font-size:23px;color:var(--ai-ink)}
.ai-first-next{font-size:18px;color:var(--ai-muted-strong)}
.ai-first-next a{display:inline-flex;align-items:center;min-height:var(--ai-hit);color:var(--ai-cobalt-deep);
 font-weight:700;text-decoration:underline;text-underline-offset:4px}
.ai-rail4,.ai-knotline{display:flex;list-style:none;margin:0;padding:0;overflow-x:auto;scrollbar-width:thin}
.ai-rail4{margin:0 0 22px;padding:15px 16px 13px;border:1px solid var(--ai-rule-strong);border-radius:4px}
.ai-rail4 li,.ai-knotline li{position:relative;flex:1 0 112px;min-width:112px;color:var(--ai-muted-strong);
 font-size:18px;text-align:center;padding:35px 6px 0;white-space:nowrap}
.ai-rail4 li::after,.ai-knotline li::after{content:"";position:absolute;top:15px;left:calc(50% + 16px);
 width:calc(100% - 32px);height:3px;background:var(--ai-rule-strong)}
.ai-rail4 li:last-child::after,.ai-knotline li:last-child::after{display:none}
.ai-knot{position:absolute;top:2px;left:50%;width:34px;height:27px;transform:translateX(-50%);z-index:1;
 background:var(--ai-surface)}
.ai-knot i{position:absolute;top:5px;width:19px;height:15px;border:3px solid var(--ai-rule-strong);border-radius:9px}
.ai-knot i:first-child{left:1px;transform:rotate(-12deg)}
.ai-knot i:last-child{right:1px;transform:rotate(12deg)}
.ai-rail4 li.done,.ai-knotline li.done{color:var(--ai-ink)}
.ai-rail4 li.done::after,.ai-knotline li.done::after{background:var(--ai-cobalt)}
.ai-rail4 li.done .ai-knot i,.ai-knotline li.done .ai-knot i,
.ai-rail4 li.now .ai-knot i,.ai-knotline li.now .ai-knot i{border-color:var(--ai-cobalt)}
.ai-rail4 li.now,.ai-knotline li.now{color:var(--ai-ink);font-weight:700}
.ai-rail4 li.now .ai-knot,.ai-knotline li.now .ai-knot{background:var(--ai-soft);outline:5px solid var(--ai-soft)}
.ai-held{display:flex;align-items:center;gap:14px;flex-wrap:wrap;background:var(--ai-soft);
 border:1px solid var(--ai-rule);border-radius:var(--r);padding:13px 18px;margin:0 0 20px}
.ai-held .lab{font-size:var(--ai-text);color:var(--ai-muted-strong)}
.ai-held .nm{font-family:var(--display);font-size:24px;line-height:1.25}
.ai-held .bk{margin-left:auto}

/* --- 接ぎ木①: 締切の帯（残り時間=長さ・超過で朱に反転。数字を読ませない）--- */
.ai-dl{display:flex;align-items:center;gap:11px;margin:8px 0 0}
.ai-dl .bar{flex:0 0 132px;height:11px;background:var(--ai-rule);border-radius:2px;overflow:hidden}
.ai-dl .bar i{display:block;height:100%;background:var(--ai-cobalt)}
.ai-dl .lb{font-size:var(--ai-text);color:var(--ai-muted-strong)}
.ai-dl.over .bar{background:#F2DAD7}
.ai-dl.over .bar i{background:var(--ai-seal)}
.ai-dl.over .lb{color:var(--ai-seal-deep);font-weight:700}

/* --- 手が止まっているもの（締切の帯つきの行）--- */
.ai-stuck{background:var(--ai-surface);border:1px solid var(--ai-rule);border-radius:var(--r);
 margin:0 0 28px;overflow:hidden}
.ai-stuck .row{display:block;padding:15px 18px;border-bottom:1px solid var(--ai-rule);color:var(--ai-ink)}
.ai-stuck .row:last-child{border-bottom:0}
.ai-stuck .row:hover{background:var(--ai-soft)}
.ai-stuck .rt{font-size:var(--ai-text);font-weight:700;margin:0 0 3px;line-height:1.5}
.ai-stuck .rm{margin:0;font-size:var(--ai-text);color:var(--ai-muted-strong)}

/* --- 接ぎ木③: 台帳SPEC部品（横罫のみ・右寄せ等幅値・cobalt明朝の金額）---
   mono はラテン内容にだけ当てる。和文に当てても等幅グリフが無くゴシックへ落ち、
   同じラベル列で書体が不揃いになる（2026-07-22 スクショ検証で確認ずみの tell）。 */
.ai-spec{width:100%;border-collapse:collapse;background:var(--ai-surface)}
.ai-spec thead th{font-size:var(--ai-text);font-weight:700;color:var(--ai-muted-strong);text-align:left;
 padding:12px 15px;border-bottom:2px solid var(--ai-graphite);white-space:nowrap}
.ai-spec thead th.num{text-align:right}
.ai-spec tbody td{padding:15px;border-bottom:1px solid var(--ai-rule);font-size:var(--ai-text)}
.ai-spec tbody tr:hover td{background:var(--ai-soft)}
.ai-spec .nm{font-family:var(--display);font-size:22px;line-height:1.3;display:block}
.ai-spec .sub{font-size:var(--ai-text);color:var(--ai-muted-strong);display:block;margin-top:2px}
.ai-spec .yen{font-family:var(--display);font-size:24px;color:var(--ai-cobalt-deep);
 text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.ai-spec .mono{font-family:var(--mono);white-space:nowrap}
.ai-specwrap{border:1px solid var(--ai-rule-strong);border-top:3px solid var(--ai-graphite);
 border-radius:0 0 var(--r) var(--r);overflow-x:auto;margin:0 0 26px}
.audit-events{min-width:760px}
.audit-events .audit-seq,.audit-events .audit-time,.audit-events .audit-actor,
.audit-events .audit-action,.audit-events .audit-status{white-space:nowrap;word-break:normal}

/* --- 操作の下限 48x48（C3確定値）。段は3つより増やさない --- */
.ai-btn{display:inline-flex;align-items:center;justify-content:center;gap:9px;
 min-height:var(--ai-hit);min-width:var(--ai-hit);padding:0 26px;font-family:var(--body);
 font-size:var(--ai-text);font-weight:700;border:0;border-radius:var(--r2);cursor:pointer;
 background:var(--ai-cobalt-deep);color:#fff}
.ai-btn:hover{background:var(--ai-cobalt-press)}
.ai-btn.sub{background:var(--ai-surface);color:var(--ai-cobalt-deep);border:2px solid var(--ai-cobalt-deep)}
.ai-btn.sub:hover{background:var(--ai-soft)}
.ai-btn.quiet{background:var(--ai-surface);color:var(--ai-ink);border:2px solid var(--ai-muted)}
.ai-btn.quiet:hover{background:var(--ai-paper)}

/* 見出し（節） */
.ai-sech{font-family:var(--display);font-size:22px;font-weight:400;color:var(--ai-ink);
 margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid var(--ai-rule-strong)}

/* キーボードフォーカスは常に見えるようにする（高齢者・キーボード運用の生命線） */
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,
textarea:focus-visible,summary:focus-visible,[tabindex]:focus-visible{
 outline:3px solid var(--ai-cobalt-deep);outline-offset:2px;border-radius:3px}

@media (prefers-reduced-motion:reduce){
 .ai-fuda:hover{transform:none}
 *{animation-duration:.001ms !important;transition-duration:.001ms !important}
}
@media(max-width:640px){
 .ai-fudas{grid-template-columns:1fr}
 .ai-ask{font-size:26px}
 .ai-held .bk{margin-left:0;width:100%}
 .ai-first{padding:16px 14px}
 .ai-first-head{display:block}
 .ai-first-title{font-size:21px}
 .ai-rail4 li{flex-basis:96px;min-width:96px}
 /* 390pxでも6つの結び目を一本の線として全体確認できる。横スクロールに逃がさない。 */
 .ai-knotline{width:100%;overflow:visible}
 .ai-knotline li{flex:1 1 0;min-width:0;padding-left:0;padding-right:0;font-size:16px}
 /* 台帳もA4の縮小表示にせず、見出し付きの縦カードへ組み替える。 */
 .ai-specwrap{overflow-x:visible}
 .ai-spec,.ai-spec tbody,.ai-spec tr{display:block;width:100%}
 .ai-spec thead{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0,0,0,0);white-space:nowrap;border:0}
 .ai-spec tbody tr{padding:8px 0;border-bottom:1px solid var(--ai-rule)}
 .ai-spec tbody tr:last-child{border-bottom:0}
 .ai-spec tbody td{display:grid;width:100%;grid-template-columns:minmax(76px,.38fr) minmax(0,1fr);
  gap:10px;padding:7px 12px;border-bottom:0;white-space:normal;overflow-wrap:anywhere}
 .ai-spec tbody td::before{content:attr(data-label);font-size:16px;font-weight:700;color:var(--ai-muted-strong)}
 .ai-spec .mono,.ai-spec .yen{white-space:normal;text-align:left;overflow-wrap:anywhere}
 .audit-events{min-width:0}
}
"""
