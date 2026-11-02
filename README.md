# 選課系統

Django 迷你選課系統（Google OAuth 登入，限 @ntub.edu.tw）：瀏覽課程、加退選（自動擋衝堂、超學分）、額滿等待名單與自動遞補、週課表。

## 功能

- **課程列表** — 每行一個班別；關鍵字搜尋課名／課號／老師，依星期篩選，顯示每班已選人數與剩餘名額。
- **一課多班** — 同一門課可有 A、B…多個班別（不同時間、教室、名額）；選課一律對「班別」進行。
- **加退選** — 加選前檢查：選課時間窗、同課已選其他班別、先修課程（過去學期同課號才算修過）、衝堂（以班別時段比對）、學期學分上限；已加選的班別會標示。列表／詳情頁的按鈕預示加選結果：額滿顯示「加入等待」，缺先修、衝堂、超學分、已選此課另一班則置灰並標明原因。
- **選課開放階段** — 學期可設開放／截止時間：未開放、加選中、已截止三種狀態，列表與詳情頁顯示狀態列；截止後加選被擋（按鈕置灰），已加選者仍可退選。
- **等待名單** — 額滿班別加選時自動進入等待（顯示第幾位），有人退選就自動遞補（遞補前重查同課與衝堂，不合格者移出順延）、頁面橫幅通知，點「知道了」才消失；等待中可隨時取消。
- **先修課程** — 課程可設 1–2 門先修；列表/詳情標「先修：CS201」，登入者標已修✓／未修，未修加選被擋。
- **官方開課的班別修課政策** — 官方開課（`NtubOfferingSource`）必須由課程維護員在後台發布「班別修課政策」才對學生開放加選：政策綁定「開課班別 × 學生班級」，狀態要 `PUBLISHED` 且「可自行加選」才開放。沒有政策、還是草稿、或不開放自行加選，都不能加選；看得到開課，但會標出原因並停用按鈕。同一門課可以對不同班級有不同政策（例如 115-1「網站應用程式設計」對二甲是本班必修、對三乙是跨班專選），三乙政策發布後三乙學生就能加選同一個班別。政策撤回後已加選者仍可退選、已排隊者仍可取消等待。
- **官方課表衝堂** — 學生的班級官方課表也是加選的時間基線：撞到**不同課號**的官方課程就擋（官方課表本來就有同一門課時不算衝堂，官方活動如共同空堂不阻擋）；候補遞補時用同一判準重新驗證。
- **篩選** — 課程列表可依關鍵字、星期、時段（上午/中午/下午/晚上）疊加過濾。
- **我的課表** — 週一至週五網格，節點標示班別與教室，附總學分（每門課只算一次）。班級官方課表與本站加選合併顯示（同一門課只佔一個時段，兩邊資料都保留）。
- **登入** — Google OAuth（`/login/` → `/accounts/google/login/`）：只有經 Google 驗證的 `@ntub.edu.tw` 信箱能登入，檢查在伺服器端（`courses/adapters.py`）。課程瀏覽免登入，加選才需要。Django Admin 維持原本的帳密登入（管理員／老師）。
- **老師專區** — 後台把 Teacher 綁到登入帳號後，該帳號可看到「我的課程」與自己課程的選課名單（姓名、信箱、班別、加選時間）。

## 快速開始

需要 Python 3.12+ 與 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                        # 建 .venv 並安裝依賴
uv run manage.py migrate
uv run --env-file .env manage.py runserver
```

開 <http://localhost:8000>。要進 `/admin/` 先跑一次 `uv run manage.py createsuperuser`。

### 示範資料（選用）

`seed` 指令會載入 CS101 等 10 門**示範用**假課程與示範學期，只在需要離線示範
加選／候補流程時才需要跑：

```bash
uv run manage.py seed          # 載入示範課程與班別，可重複執行
```

正式的本機開發資料庫**不應該**跑這個指令：示範課程會出現在學生搜尋結果裡，
示範學期也可能蓋掉真正的當前學期（`seed` 現在不會搶走已存在的當前學期，但仍
會留下假課程）。

已經跑過、想清掉示範資料時：

```bash
uv run manage.py cleanup_demo_data            # dry-run：只列出會刪什麼，不寫入
uv run manage.py cleanup_demo_data --apply    # 真的刪除
```

這個指令只刪 `seed.py` 建立的資料（以課號＋課名＋老師＋學期多重比對確認），
不會碰真實的 NTUB 課程、官方課表、學籍目錄、Google 登入帳號或管理員權限。

## 登入（Google OAuth）

`/login/` 只有一個入口：「使用 NTUB Google 帳號登入」，按下去跳 Google 授權頁。登入後由伺服器端檢查：

1. 信箱網域必須是 `@ntub.edu.tw`（其他網域一律拒絕）。
2. Google 回傳的信箱必須是已驗證狀態（`email_verified`／`verified_email`），未驗證一律拒絕。

檢查寫在 `courses/adapters.py`（allauth 的 `SOCIALACCOUNT_ADAPTER`），資料來自 Google 的 token／userinfo，不是使用者自己填的表單，所以繞過不了。驗證失敗會回到登入頁並顯示原因。

Google 憑證放在 `.env`（`GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`），`config/settings.py` 讀取後交給 allauth；`SOCIALACCOUNT_ONLY = True` 關掉 allauth 的密碼註冊／改密／密碼重設，學生只能走 Google。管理員與老師另外走 Django Admin 的帳密登入（`/admin/`）。

## 角色與權限

| 角色 | 怎麼來的 | 多了什麼 |
| --- | --- | --- |
| 訪客 | 未登入 | 課程列表、課程詳情 |
| 學生 | Google OAuth（@ntub.edu.tw 且信箱經 Google 驗證） | 加選／退選、我的課表 |
| 老師 | 後台把 Teacher 綁到登入帳號 | 我的課程、自己課程的選課名單 |
| 管理員 | `is_staff` | Django admin 的完整 CRUD |

老師不是 staff。staff 等於整個 admin 的 CRUD 權限，老師只需要自己課程的名單，所以身分由 `Teacher.user` 這個一對一關聯推導，沒有額外的角色布林值。

把某人變成老師：`/admin/courses/teacher/` → 選一筆 Teacher → 「登入帳號」選他的帳號 → 儲存。該帳號下次登入就會在側欄看到「我的課程」。沒綁定的人開 `/teaching/` 只會看到空頁面，直接輸入別人的名單網址會拿到 404。

## 開發流程

新功能貢獻流程（分支、PR、合併前檢查、業務規則文件同步、commit 格式）見 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 測試

```bash
uv run manage.py test
```

涵蓋加選的各種結果（成功、同課另一班、缺先修、衝堂、額滿轉等待、超學分）、選課時間窗（未開放／加選中／已截止三態、時間窗優先於其他檢查、退選與課表不受影響）、等待名單（遞補、不越序、遞補重查同課與衝堂、重新排隊、取消、同課互斥、橫幅需明確確認）、先修顯示（訪客不標、登入標✓/未修、本學期加選不算已修）、時段篩選四段＋與星期疊加、退選後改選、開放轉址防護、老師名單的可見性（本人 200、他人 404、匿名轉登入）、頁面渲染，以及班別的欄位與唯一約束。

Google 登入另有 `courses/tests/test_auth.py`：登入成功、非 NTUB 信箱拒絕、未驗證信箱拒絕、登出、Admin 帳密登入。測試會假造 Google 的回應（mock 掉換 token 與 id_token 解析），不連真實 Google。

## UI 品牌資源

側欄／頂部 Logo 目前引用一張公開縮圖（見 `courses/templates/courses/_brand.html`）。若該圖無法載入，會改顯示內嵌佔位 SVG。**請替換為國立臺北商業大學官方 Logo 檔案**（建議放到 `courses/static/img/ntub-logo.png` 後改 template 路徑）。

設計 token 集中在 `courses/static/css/tokens.css`（北商紫 `#5B2A86`、語意色、課表色票、圓角與陰影）。共用元件樣式在 `courses/static/css/components.css`，互動腳本在 `courses/static/js/ui.js`（`showToast({type, title, message})`）。

## 專案結構

```
config/     settings、根路由（/admin/、/accounts/、/login/、/logout/、courses）
courses/    models（Term / Teacher / Course / Section / Enrollment / SectionClassPolicy）、services、views、urls、templates、static
courses/adapters.py        Google 登入的伺服器端把關（NTUB 網域 + 信箱已驗證）
courses/enrollment_policy.py   班別修課政策與官方課表衝堂判準（加選、候補、頁面共用）
courses/standing.py        學生在本學期的已選／等待／學分／班級／政策／官方課表
courses/management/commands/seed.py    範例學期、課程與班別資料（選用，僅供離線示範）
courses/management/commands/cleanup_demo_data.py    清除 seed 建立的示範資料（預設 dry-run）
```

## 可調設定

| 位置 | 項目 |
| --- | --- |
| `config/settings.py` | `MAX_CREDITS = 25`：每學期學分上限 |
| `.env` | `SECRET_KEY`、`DEBUG`、`ALLOWED_HOSTS`（上線時設 `DEBUG=0` 且填 `ALLOWED_HOSTS`） |
| `.env` | `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`：Google OAuth 憑證（`config/settings.py` 讀取後交給 allauth） |
| `.env` | `USE_NGROK=1` 與 `NGROK_ORIGIN=https://你的固定網域` 才啟用 ngrok 的 https 與 CSRF 設定，預設關閉 |

## 業務規則

選課系統的完整業務邏輯（加選四條規則、衝堂判定、學分計算、角色權限、已知限制與 backlog 對照）見 [`docs/business-rules.md`](docs/business-rules.md)。

## 已知限制

- 名額檢查在 SQLite 上取不到列鎖，高併發下可能超收；換 PostgreSQL 後 `select_for_update()` 才會真正生效（`courses/services.py`）。
- 名額是**本站模擬選課數**（畫面標示「本站模擬」），不是學校即時選課人數；本站是模擬選課系統，非 NTUB 正式校務選課紀錄。官方公告的已選人數只留在官方來源紀錄裡，不混進本站名額。
- 資料庫為 SQLite，定位是本地開發與 demo。
- 老師只看得到當前學期的課與名單，歷年紀錄目前沒有入口。
- 老師對自己的課仍看得到「加選」按鈕，系統沒擋老師加選自己的課。
- `0005_section` 這個 migration 不可逆（把 Course 的時段欄位移到 Section 並刪除，`reverse` 是 noop）； downgrade 前請備份。
- 「同課只選一班」是 `services.register` 的規則，DB 只約束 `(user, section)`；若 admin 手動造成同課兩班各一筆加選，系統不會擋，但課表與學分總和已按課號去重，不會雙算。
