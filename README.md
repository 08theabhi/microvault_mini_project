# 🎓 MicroCred — Micro-Credential Aggregator Platform

A full-stack Flask web application for aggregating, verifying, and showcasing micro-credentials in a unified learner profile.

---

## 🚀 Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the app
```bash
python app.py
```

### 3. Seed demo data (optional but recommended)
```bash
python seed.py
```

Then open: **http://localhost:5000**

---

## 🔑 Demo Login Accounts

| Role | Email | Password |
|------|-------|----------|
| Learner | `learner@demo.com` | `demo123` |
| Employer | `employer@techcorp.com` | `emp123` |
| Admin | `admin@microcred.io` | `admin123` |

---

## 📦 Project Structure

```
microcred/
├── app.py              # Flask backend (routes, DB, logic)
├── seed.py             # Demo data seeder
├── requirements.txt    # Python dependencies
├── microcred.db        # SQLite database (auto-created)
├── templates/
│   ├── base.html           # Shared layout & nav
│   ├── index.html          # Landing page + hash verifier
│   ├── login.html          # Auth: login
│   ├── register.html       # Auth: register
│   ├── dashboard.html      # Learner dashboard
│   ├── upload.html         # Add credential
│   ├── credential_detail.html  # Single credential view
│   ├── employer.html       # Employer search portal
│   ├── learner_profile.html    # Public learner profile
│   └── admin.html          # Admin panel
└── static/
    └── uploads/        # Uploaded certificate files
```

---

## ✨ Features

### 👤 User Management
- Register as Learner or Employer
- Role-based access control (Learner / Employer / Admin)
- Secure password hashing (SHA-256)

### 📜 Credential Management
- Upload certificates (PDF/PNG/JPG)
- Store title, provider, date, description, skill category, level
- Unique Credential ID per entry

### ✅ Verification System
- Cryptographic hash generated per credential
- Admins & employers can verify credentials with one click
- Public hash verifier on landing page — paste a hash to check instantly

### 📊 Learner Dashboard
- View all credentials in one place
- Filter by status (verified/pending) or skill category
- Visual skill-category breakdown bar chart
- Profile score based on credential count

### 🏢 Employer Portal
- Search learners by name or skill
- Filter by skill category
- View full learner profiles with verified credential list
- One-click verification from profile

### 🛡️ Admin Panel
- View all users and credentials
- Verify any credential with one click
- Platform-wide stats

---

## 🛠️ Tech Stack
- **Backend**: Python + Flask
- **Database**: SQLite (via sqlite3)
- **Frontend**: HTML5 + CSS3 (custom design system) + Vanilla JS
- **Fonts**: Syne + DM Sans (Google Fonts)
- **Icons**: Font Awesome 6

---

## 🔒 Security Notes
- Passwords are hashed before storage
- Credentials get a unique SHA-256 hash for tamper detection
- Session-based authentication with role guards
- File uploads use secure_filename sanitization

---

## 🔮 Future Enhancements
- Blockchain-based verification
- OAuth login (Google, LinkedIn)
- AI skill recommendations
- REST API for third-party integrations
- Email notifications on verification
- Mobile app (React Native)
