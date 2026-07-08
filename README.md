# Tournament Manager

可登入、可保存的淘汰賽管理系統（單敗/雙敗）。

- 前端：`frontend/index.html`（單頁應用，含賽程互動與統計邏輯）
- 後端：`FastAPI` + `SQLAlchemy`
- 資料庫：`PostgreSQL`
- 驗證：`JWT`（Bearer token）

---

## 功能總覽

- 使用者註冊、登入、登出、取得當前帳號資訊
- 忘記密碼（開發模式回傳 reset token）、重設密碼、登入後改密碼
- 刪除帳號（刪除後連帶刪除該使用者所有賽程）
- 每位使用者可建立多份賽程，支援：
  - 建立、載入、更新、刪除賽程
  - 賽程可在「未產生冠軍」前先儲存（進行中狀態）
  - 產生冠軍後標記為已完成
- 前端內建賽程演算法與排行榜/匯出邏輯，後端主要負責持久化與權限控管

---

## 專案結構

```text
tournament_manager/
├── backend/
│   ├── main.py                # API 入口與所有路由
│   ├── auth.py                # Bearer token 解析與 current_user
│   ├── security.py            # 密碼雜湊、JWT、reset token
│   ├── database.py            # DB engine / session / Base
│   ├── models.py              # SQLAlchemy models
│   ├── schemas.py             # Pydantic request/response schemas
│   ├── requirements.txt
│   ├── .env.example
│   └── run_backend_mac.sh
├── frontend/
│   ├── index.html             # UI + bracket 邏輯 + API 呼叫
│   ├── package.json
│   └── run_frontend_mac.sh
├── docker-compose.yml         # 本機 PostgreSQL
└── README.md
```

---

## 系統需求

- Python 3.10+
- Node.js 18+
- Docker（或自備 PostgreSQL）

---

## 快速啟動（本機全端）

### 1) 啟動 PostgreSQL

在專案根目錄：

```bash
docker compose up -d
```

重置資料庫（可選）：

```bash
docker compose down -v
docker compose up -d
```

### 2) 啟動後端 API

開新 terminal：

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
python -m uvicorn main:app --reload
```

可用網址：

- Swagger: <http://localhost:8000/docs>
- Health check: <http://localhost:8000/health>

### 3) 啟動前端

再開一個 terminal：

```bash
cd frontend
npm install
npm run dev
```

前端網址：<http://localhost:5173>

---

## 環境變數（backend/.env）

可參考 `backend/.env.example`：

```env
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/tournament_db
SECRET_KEY=replace-this-with-a-long-random-string
ACCESS_TOKEN_EXPIRE_MINUTES=1440
```

說明：

- `DATABASE_URL`：PostgreSQL 連線字串
- `SECRET_KEY`：JWT 簽章金鑰，正式環境必須更換為長隨機字串
- `ACCESS_TOKEN_EXPIRE_MINUTES`：登入 token 有效分鐘數

---

## 主要 API

### Auth

- `POST /auth/register`
- `POST /auth/login`
- `GET /auth/me`
- `POST /auth/forgot-password`
- `POST /auth/reset-password`

### Account

- `PUT /account/password`
- `DELETE /account`

### Tournaments

- `GET /tournaments`
- `POST /tournaments`
- `GET /tournaments/{tournament_id}`
- `PUT /tournaments/{tournament_id}`
- `DELETE /tournaments/{tournament_id}`

註：除註冊/登入/忘記密碼外，多數操作需帶 Bearer token：

```text
Authorization: Bearer <access_token>
```

---

## 資料表設計（目前）

### users

- `id`
- `email`（unique）
- `username`（unique）
- `password_hash`
- `role`（預設 `member`）
- `is_active`
- `created_at`
- `updated_at`

### tournaments

- `id`
- `user_id`（FK -> users.id）
- `title`
- `champion`（可空）
- `team_count`
- `max_losses`
- `has_grand_final_reset`
- `state`（`JSONB`，保存完整賽程狀態）
- `completed_at`
- `created_at`
- `updated_at`

### password_reset_tokens

- `id`
- `user_id`（FK -> users.id）
- `token_hash`（unique）
- `expires_at`
- `used_at`
- `created_at`

### admin_logs

- `id`
- `actor_user_id`（FK -> users.id，可空）
- `action`
- `target_type`
- `target_id`
- `details`（`JSONB`）
- `created_at`

---

## 開發備註

- 此專案目前無 migration 工具；資料表由 `Base.metadata.create_all()` 自動建立。
- 忘記密碼流程是開發模式：後端直接回傳 reset token，不會寄 email。
- 前端核心邏輯集中在 `frontend/index.html`，修改前建議先備份或切分模組。

---

## 安全建議（上線前）

- 必換 `SECRET_KEY`
- 收斂 CORS allow origins
- 忘記密碼改為 email 寄送流程
- 規劃 migration（例如 Alembic）與自動化測試
