# Tournament Manager Auth PostgreSQL v2.1

這是你的淘汰賽程管理系統 v2.1：

- 前端：單一 `frontend/index.html`，保留原本 tournament bracket HTML / JavaScript 賽程邏輯
- 後端：FastAPI
- 資料庫：PostgreSQL
- ORM：SQLAlchemy
- 新增：使用者註冊、登入、登出、忘記密碼、本機 reset token、更換密碼、刪除帳號
- 新增：每個使用者可以儲存多份進行中或已完成的賽程


---

## v2.1 改善內容

這版針對實際操作時發現的 UX 問題做了修正：

1. 按「儲存目前賽程」後，右上角會跳出「已儲存賽程」提示。
2. 賽程不需要產生冠軍也可以儲存。尚未產生冠軍時，Profile 會顯示為「進行中」。
3. 登出後會清空登入、註冊、忘記密碼、重設密碼、更換密碼與刪除帳號欄位。
4. 密碼欄位改用 `autocomplete="off"` 並加入常見密碼管理器忽略標記，降低瀏覽器或密碼管理器跳出提示的機率。
   - 注意：如果警告是瀏覽器內建的外洩密碼偵測，網站端無法 100% 禁止，只能降低自動填入與誤判機率。
5. 在敗部區點選勝負後，會保留目前水平捲動位置，不會一直跳回中間。

---

## 專案結構

```text
tournament-manager-auth-postgres/
├── backend/
│   ├── main.py
│   ├── auth.py
│   ├── security.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   ├── requirements.txt
│   ├── .env.example
│   └── run_backend_mac.sh
│
├── frontend/
│   ├── index.html
│   ├── package.json
│   └── run_frontend_mac.sh
│
├── docker-compose.yml
└── README.md
```

---

## 新版資料庫設計

```text
users
- id
- email
- username
- password_hash
- is_active
- created_at
- updated_at

tournaments
- id
- user_id
- title
- champion
- team_count
- max_losses
- has_grand_final_reset
- state JSONB
- completed_at
- created_at
- updated_at

password_reset_tokens
- id
- user_id
- token_hash
- expires_at
- used_at
- created_at
```

目前賽程仍使用 `state JSONB` 存整份前端賽程狀態。這樣可以保留你原本 HTML 裡完整的單敗/雙敗/輪空/晉級/排行榜邏輯。

---

## Step 1：啟動 PostgreSQL

在專案根目錄執行：

```bash
docker compose up -d
```

驗證你連到的是 Docker PostgreSQL：

```bash
export PGPASSWORD=postgres
psql -h localhost -p 5432 -U postgres -d postgres -tAc "SELECT version();"
```

預期看到類似：

```text
PostgreSQL 16.x (Debian ...)
```

如果你之前用過舊版資料庫，想完全重來：

```bash
docker compose down -v
docker compose up -d
```

如果遇到錯誤：`Conflict. The container name "/tournament_postgres" is already in use`，代表同名容器還存在。可用以下其中一種方式處理：

方式 A（建議，指定同一個 compose project 名稱）：

```bash
docker compose -p tournament-manager-auth-postgres down -v
docker rm -f tournament_postgres 2>/dev/null || true
docker compose -p tournament-manager-auth-postgres up -d
```

方式 B（快速）：

```bash
docker rm -f tournament_postgres
docker compose up -d
```

可再用以下指令確認容器已正常啟動：

```bash
docker ps --filter name=^/tournament_postgres$
```

---

## Step 2：啟動後端 FastAPI

開第一個 VS Code Terminal：

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
unset PIP_TARGET
unset PYTHONPATH
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

成功後打開：

```text
http://localhost:8000/docs
```

健康檢查：

```text
http://localhost:8000/health
```

---

## Step 3：啟動前端

開第二個 VS Code Terminal：

```bash
cd frontend
npm install
npm run dev
```

成功後打開：

```text
http://localhost:5173
```

---

## 使用流程

1. 打開前端後會先看到登入頁
2. 建立帳號或登入
3. 進入 Profile Dashboard
4. 點「建立新賽程」
5. 操作賽程，可在任何階段按「儲存目前賽程」
6. 若冠軍尚未產生，該賽程會顯示為「進行中」
7. 若冠軍已產生，該賽程會顯示為「已完成」並記錄冠軍
8. 回 Profile 後可以看到該賽程
9. 可以載入、繼續編輯或刪除自己的已儲存賽程

---

## API 說明

Auth：

```text
POST /auth/register
POST /auth/login
GET  /auth/me
POST /auth/forgot-password
POST /auth/reset-password
```

Account：

```text
PUT    /account/password
DELETE /account
```

Tournaments：

```text
GET    /tournaments
POST   /tournaments
GET    /tournaments/{tournament_id}
PUT    /tournaments/{tournament_id}
DELETE /tournaments/{tournament_id}
```

`tournaments` API 都需要登入 token。前端會自動在 request header 帶：

```text
Authorization: Bearer <token>
```

---

## 忘記密碼說明

這是入門開發版，所以不會真的寄 email。

流程是：

1. 使用者輸入 email
2. 後端產生 reset token
3. 前端直接顯示 token
4. 使用者把 token 貼到 reset 欄位並輸入新密碼

正式部署時，可以把這段改成寄 email。

---

## 重要觀念

這版的分工是：

```text
frontend/index.html
  負責登入畫面、Profile 畫面、賽程互動、冠軍產生、呼叫 API

backend/main.py
  負責 Auth API、Account API、Tournament API

backend/security.py
  負責 password hash、JWT token、reset token

backend/models.py
  負責 users / tournaments / password_reset_tokens 資料表

PostgreSQL
  負責永久保存使用者與使用者自己的賽程
```

目前賽程演算法仍在前端 JavaScript。後端負責儲存結果與控管使用者資料。
# tournament_manager
