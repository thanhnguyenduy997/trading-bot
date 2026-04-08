# Trading Web App Foundation

Initial FastAPI project scaffold for a trading web app with PostgreSQL, SQLAlchemy ORM, Alembic migrations, JWT authentication, encrypted trading-account password storage, server-rendered pages, and starter tests.

## Stack

- Python 3.12
- FastAPI
- SQLAlchemy 2.x ORM
- Alembic
- PostgreSQL
- Jinja2 templates
- JWT authentication

## Project Structure

```text
app/
  api/routes/
  core/
  models/
  schemas/
  services/
  templates/
tests/
alembic/
```

## Local Setup

1. Create and activate a Python 3.12 virtual environment.
2. Install dependencies.
3. Create a PostgreSQL database.
4. Copy `.env.example` to `.env` and update values.
5. Run Alembic migrations.
6. Start the development server.

### Commands

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .[dev]
Copy-Item .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
alembic upgrade head
uvicorn app.main:app --reload
```

Open:

- `http://127.0.0.1:8000/login`
- `http://127.0.0.1:8000/docs`

## Environment Variables

- `SECRET_KEY`: JWT signing secret
- `DATABASE_URL`: SQLAlchemy PostgreSQL URL
- `ENCRYPTION_KEY`: Fernet key for encrypting trading account passwords
- `ACCESS_TOKEN_EXPIRE_MINUTES`: JWT lifetime in minutes
- `JWT_ALGORITHM`: JWT algorithm, default `HS256`

## Auth Flow

- `POST /api/auth/register`
- `POST /api/auth/token`
- `POST /api/auth/login`
- `GET /api/auth/me`

## Trading Account APIs

- `GET /api/trading-accounts/`
- `POST /api/trading-accounts/`
- `GET /api/trading-accounts/{account_id}`
- `PUT /api/trading-accounts/{account_id}`
- `DELETE /api/trading-accounts/{account_id}`

Trading account passwords are stored encrypted in the database via Fernet. Plain text passwords are only accepted at the API or form boundary and are never persisted directly.

## Tests

Run:

```powershell
pytest
```

The test suite uses SQLite in-memory for speed while preserving the application service and API flow.
