# My Other App - Backend API

A robust FastAPI backend powering the My Other App community and event management platform. Built with async Python, PostgreSQL, and modern best practices.

![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15-4169E1?logo=postgresql)
![License](https://img.shields.io/badge/License-Proprietary-red)

## 🚀 Features

### Authentication
- **Google OAuth** - Sign in with Google account verification
- **JWT Tokens** - Secure access and refresh token management
- **Role-based Access** - User, Admin, and Club Admin roles

### Events Management
- **CRUD Operations** - Create, read, update, delete events
- **Event Registration** - User registration with capacity management
- **Event Categories** - Categorized events for discovery
- **Event Ratings** - Post-event rating system
- **Search & Filter** - Full-text search with category, date, and status filters
- **Certificates** - Auto-generated attendance certificates

### Clubs Management
- **Club Profiles** - Rich profiles with about, socials, and ratings
- **Follow System** - Users can follow/unfollow clubs
- **Pin & Hide** - Pin favorite clubs, hide unwanted events
- **Club Events** - Past and upcoming events per club
- **Club Ratings** - Aggregated ratings from event reviews

### Organizations
- **Multi-org Support** - Multiple organizations/colleges
- **Org-based Clubs** - Clubs belong to organizations
- **Location Management** - Geographic organization support

### Payments
- **Razorpay Integration** - Secure payment processing
- **Event Tickets** - Paid event registration
- **Payment Verification** - Webhook-based verification

### Notifications
- **Push Notifications** - Event reminders and updates
- **Email Notifications** - Registration confirmations

## 🏗️ Architecture

```
backend/
├── app/
│   ├── api/                    # API modules
│   │   ├── auth/               # Authentication endpoints
│   │   │   ├── router.py       # Routes
│   │   │   ├── service.py      # Business logic
│   │   │   ├── schemas.py      # Pydantic models
│   │   │   └── models.py       # SQLAlchemy models
│   │   ├── clubs/              # Club management
│   │   ├── events/             # Event management
│   │   ├── home/               # Home page data
│   │   ├── interests/          # User interests
│   │   ├── notifications/      # Push notifications
│   │   ├── orgs/               # Organizations
│   │   ├── payments/           # Payment processing
│   │   └── users/              # User management
│   ├── core/                   # Core utilities
│   │   ├── auth/               # JWT utilities
│   │   ├── deps/               # Dependency injection
│   │   ├── email/              # Email templates
│   │   ├── exceptions/         # Custom exceptions
│   │   ├── geo/                # Geographic utilities
│   │   ├── pdf/                # PDF generation
│   │   └── storage/            # S3 file storage
│   ├── db/                     # Database
│   │   ├── database.py         # Async session management
│   │   └── models.py           # Base models
│   ├── config.py               # App configuration
│   └── response.py             # Standard responses
├── migrations/                 # Alembic migrations
│   └── versions/               # Migration files
├── templates/                  # Email & PDF templates
├── main.py                     # App entry point
├── requirements.txt            # Dependencies
├── Dockerfile                  # Container config
└── alembic.ini                 # Migration config
```

## 🛠️ Tech Stack

| Category | Technology |
|----------|------------|
| **Framework** | FastAPI 0.115 |
| **ORM** | SQLAlchemy 2.0 (async) |
| **Database** | PostgreSQL 15 with asyncpg |
| **Validation** | Pydantic 2.10 |
| **Auth** | JWT (PyJWT), Google Auth |
| **Payments** | Razorpay |
| **Storage** | AWS S3 (boto3) |
| **PDF** | pdfkit, Jinja2 |
| **Server** | Uvicorn with uvloop |
| **Migrations** | Alembic |

## 🚀 Getting Started

### Prerequisites

- Python 3.11 or higher
- PostgreSQL 15+
- AWS S3 bucket (for file storage)
- Razorpay account (for payments)

### Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/my-other-app/moa_backend.git
   cd moa_backend/backend
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   # or
   .\venv\Scripts\activate  # Windows
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

5. **Run migrations**
   ```bash
   alembic upgrade head
   ```

6. **Start the server**
   ```bash
   uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

### Environment Variables

Create a `.env` file:

```env
# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/myotherapp

# JWT
JWT_SECRET_KEY=your-secret-key
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7

# Google OAuth
GOOGLE_CLIENT_ID=your-client-id

# AWS S3
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_REGION=ap-south-1
S3_BUCKET_NAME=your-bucket

# Razorpay
RAZORPAY_KEY_ID=your-key-id
RAZORPAY_KEY_SECRET=your-key-secret

# Email (SMTP)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email
SMTP_PASSWORD=your-password
```

## 📚 API Documentation

Once running, access the interactive API docs:

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## 🔑 API Endpoints

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/google` | Google OAuth sign-in |
| POST | `/auth/refresh` | Refresh access token |
| POST | `/auth/logout` | Logout user |

### Events
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/events/list` | List events with filters |
| GET | `/events/{id}` | Get event details |
| POST | `/events/{id}/register` | Register for event |
| POST | `/events/{id}/rate` | Rate attended event |
| GET | `/events/{id}/ticket` | Get event ticket |

### Clubs
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/clubs/list` | List clubs with search |
| GET | `/clubs/{id}` | Get club details |
| POST | `/clubs/follow/{id}` | Follow club |
| POST | `/clubs/unfollow/{id}` | Unfollow club |
| POST | `/clubs/pin/{id}` | Toggle pin club |
| POST | `/clubs/hide/{id}` | Toggle hide events |
| GET | `/clubs/{id}/events` | Get club events |

### Users
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/users/me` | Get current user |
| PUT | `/users/me` | Update profile |
| GET | `/users/interests` | Get user interests |
| PUT | `/users/interests` | Update interests |

### Payments
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/payments/create-order` | Create Razorpay order |
| POST | `/payments/verify` | Verify payment |
| POST | `/payments/webhook` | Razorpay webhook |

## 🗃️ Database Models

### Core Models
- **Users** - User accounts with OAuth
- **Clubs** - Community clubs/organizations
- **Events** - Events with details and capacity
- **Orgs** - Parent organizations
- **Interests** - Event/user interest tags

### Link Models
- **ClubUsersLink** - Follow/pin/hide relationships
- **EventUsersLink** - Event registrations
- **EventCategories** - Event categorization
- **UserInterests** - User interest selections

## 🔐 Security

- **JWT Authentication** - Secure token-based auth
- **Password Hashing** - bcrypt for any local auth
- **CORS** - Configurable cross-origin settings
- **Rate Limiting** - Protect against abuse
- **Input Validation** - Pydantic schema validation

## 🐳 Docker

```bash
# Build image
docker build -t moa-backend .

# Run container
docker run -p 8000:8000 --env-file .env moa-backend
```

## 🧪 Testing

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=app
```

## 📊 Database Migrations

```bash
# Create new migration
alembic revision --autogenerate -m "Description"

# Apply migrations
alembic upgrade head

# Rollback one revision
alembic downgrade -1
```

## 🚢 Deployment

The API is deployed using:
- **GitHub Actions** - CI/CD pipeline
- **Docker** - Containerized deployment
- **AWS/GCP** - Cloud hosting

### Deploy Command
```bash
# Trigger deployment via GitHub Actions
# Use "live-deploy" workflow with sync_code=true
```

## 📁 Key Files

| File | Description |
|------|-------------|
| `main.py` | FastAPI app initialization |
| `app/config.py` | Settings and configuration |
| `app/db/database.py` | Async database session |
| `app/core/auth/jwt.py` | JWT token utilities |
| `app/core/deps/auth.py` | Auth dependencies |
| `app/api/router.py` | Root API router |

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Write tests for new features
4. Submit a pull request

## 📄 License

This project is proprietary software. All rights reserved.

---

**My Other App Backend** - Powering community connections 🔌
