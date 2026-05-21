"""
seed.py — FOR LOCAL TESTING ONLY
---------------------------------
Run this ONLY on your local machine to create sample data.
DO NOT run this on a production server.

Usage:  python seed.py
"""
import sqlite3, hashlib, uuid, secrets, sys, os
from datetime import datetime

DB = 'microcred.db'

# ── Install werkzeug if needed ──
try:
    from werkzeug.security import generate_password_hash
except ImportError:
    print("Installing werkzeug...")
    os.system(f"{sys.executable} -m pip install werkzeug")
    from werkzeug.security import generate_password_hash

def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_tables():
    """Create tables if they don't exist yet."""
    conn = get_conn()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'learner',
            bio TEXT, headline TEXT, location TEXT,
            linkedin TEXT, github TEXT, website TEXT,
            avatar_color TEXT DEFAULT '#f0a430',
            lang TEXT DEFAULT 'en',
            dark_mode INTEGER DEFAULT 0,
            created_at TEXT,
            phone TEXT DEFAULT NULL,
            phone_verified INTEGER DEFAULT 0,
            suspended INTEGER DEFAULT 0,
            suspend_reason TEXT DEFAULT NULL,
            google_id TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS credentials (
            credential_id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT NOT NULL,
            provider TEXT NOT NULL,
            issue_date TEXT,
            expiry_date TEXT,
            description TEXT,
            skill_category TEXT,
            level TEXT,
            file_path TEXT,
            credential_url TEXT,
            status TEXT DEFAULT 'unverified',
            hash_value TEXT,
            created_at TEXT,
            zkp_commitment TEXT DEFAULT NULL,
            zkp_salt TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS verification (
            verification_id TEXT PRIMARY KEY,
            credential_id TEXT,
            hash_value TEXT,
            verified_status TEXT,
            verified_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notifications (
            notif_id TEXT PRIMARY KEY,
            user_id TEXT,
            message TEXT,
            link TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS otp_codes (
            otp_id TEXT PRIMARY KEY,
            email TEXT,
            code TEXT,
            purpose TEXT DEFAULT 'register',
            expires_at TEXT,
            used INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            msg_id TEXT PRIMARY KEY,
            sender_id TEXT,
            receiver_id TEXT,
            subject TEXT,
            body TEXT,
            is_read INTEGER DEFAULT 0,
            parent_id TEXT DEFAULT NULL,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY,
            admin_id TEXT,
            target_type TEXT,
            target_id TEXT,
            reason TEXT,
            notes TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            log_id TEXT PRIMARY KEY,
            admin_id TEXT,
            action TEXT,
            target_id TEXT,
            details TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pathways (
            pathway_id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT,
            description TEXT,
            steps TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS zkp_proofs (
            proof_id TEXT PRIMARY KEY,
            credential_id TEXT,
            user_id TEXT,
            claim_type TEXT,
            claim_value TEXT,
            proof_hash TEXT,
            proof_token TEXT,
            expires_at TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS phone_verifications (
            ver_id TEXT PRIMARY KEY,
            phone TEXT,
            code TEXT,
            purpose TEXT DEFAULT 'register',
            expires_at TEXT,
            used INTEGER DEFAULT 0,
            created_at TEXT
        );
    ''')
    conn.commit()
    conn.close()
    print("  ✅ Tables ready")

def gen_hash(cid, uid, title, provider):
    return hashlib.sha256(f"{cid}{uid}{title}{provider}".encode()).hexdigest()

def gen_zkp(cid, uid, title, provider, issue_date, category, level, status):
    salt = secrets.token_hex(32)
    canonical = '|'.join([cid, uid, title.lower().strip(),
                           provider.lower().strip(), issue_date,
                           category.lower().strip(), level.lower().strip(), status])
    commitment = hashlib.sha256((canonical + salt).encode()).hexdigest()
    return commitment, salt

def insert_user(conn, name, email, password, role, bio='', headline='',
                location='', linkedin='', github='', avatar_color='#f0a430'):
    try:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE email=?", (email,)
        ).fetchone()
        if existing:
            print(f"  ℹ️  {email} already exists — skipping")
            return existing['user_id']
    except Exception as e:
        print(f"  ⚠️  Check error: {e}")
        return None

    uid = str(uuid.uuid4())
    try:
        conn.execute("""
            INSERT INTO users (
                user_id, name, email, password, role, bio, headline,
                location, linkedin, github, website, avatar_color,
                lang, dark_mode, created_at, phone, phone_verified
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (uid, name, generate_password_hash(password), password,
              role, bio, headline, location, linkedin, github, '',
              avatar_color, 'en', 0, datetime.now().isoformat(), '', 1))

        # Fix: correct column order
        conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
        conn.execute("""
            INSERT INTO users (
                user_id, name, email, password, role, bio, headline,
                location, linkedin, github, website, avatar_color,
                lang, dark_mode, created_at, phone, phone_verified
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (uid, name, email, generate_password_hash(password),
              role, bio, headline, location, linkedin, github, '',
              avatar_color, 'en', 0, datetime.now().isoformat(), '', 1))
        conn.commit()
        print(f"  ✅ Created {role}: {email} / {password}")
        return uid
    except Exception as e:
        print(f"  ❌ Failed to create {email}: {e}")
        return None

def insert_credentials(conn, uid, creds):
    if not uid:
        return 0
    added = 0
    for title, provider, date, expiry, desc, cat, level, url, status in creds:
        cid  = str(uuid.uuid4())
        hval = gen_hash(cid, uid, title, provider)
        zkp_commitment, zkp_salt = gen_zkp(
            cid, uid, title, provider, date, cat, level, status)
        try:
            conn.execute("""
                INSERT INTO credentials (
                    credential_id, user_id, title, provider, issue_date,
                    expiry_date, description, skill_category, level,
                    file_path, credential_url, status, hash_value,
                    created_at, zkp_commitment, zkp_salt
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (cid, uid, title, provider, date, expiry, desc, cat,
                  level, '', url or '', status, hval,
                  datetime.now().isoformat(), zkp_commitment, zkp_salt))
            conn.execute("""
                INSERT INTO verification (
                    verification_id, credential_id, hash_value,
                    verified_status, verified_at
                ) VALUES (?,?,?,?,?)
            """, (str(uuid.uuid4()), cid, hval,
                  status, datetime.now().isoformat()))
            added += 1
        except Exception as e:
            print(f"    ⚠️  Credential error: {e}")
    conn.commit()
    return added

def seed():
    print()
    print("⚠️  WARNING: This creates demo accounts for LOCAL TESTING only.")
    print("   Do NOT run this on a production server.\n")

    # Step 1: Ensure all tables exist
    ensure_tables()

    conn = get_conn()

    # ── ADMIN ──
    admin_uid = insert_user(conn,
        name='Admin User',
        email='admin@microcred.io',
        password='admin123',
        role='admin',
        bio='Platform administrator.',
        headline='MicroVault Admin',
        location='Bengaluru, India',
        avatar_color='#7c3aed')

    # ── EMPLOYER ──
    emp_uid = insert_user(conn,
        name='TechCorp HR',
        email='employer@techcorp.com',
        password='emp123',
        role='employer',
        bio='Hiring top tech talent for our engineering teams.',
        headline='Senior HR Manager at TechCorp',
        location='Hyderabad, India',
        linkedin='https://linkedin.com/company/techcorp',
        avatar_color='#0891b2')

    # ── LEARNER 1 — Alex Johnson ──
    alex_uid = insert_user(conn,
        name='Alex Johnson',
        email='learner@demo.com',
        password='demo123',
        role='learner',
        bio='Passionate full-stack developer and ML enthusiast from Bengaluru.',
        headline='Full-Stack Developer | ML Enthusiast',
        location='Bengaluru, India',
        linkedin='https://linkedin.com/in/alexj',
        github='https://github.com/alexj',
        avatar_color='#f0a430')

    if alex_uid:
        n = insert_credentials(conn, alex_uid, [
            ('AWS Certified Solutions Architect',
             'Amazon Web Services', '2023-08-15', '2026-08-15',
             'Cloud infrastructure design and deployment on AWS.',
             'Technology', 'Advanced',
             'https://aws.amazon.com/verify', 'verified'),

            ('Python for Data Science',
             'Coursera', '2023-03-22', None,
             'NumPy, Pandas, Matplotlib, Scikit-learn and ML basics.',
             'Data Science', 'Intermediate',
             'https://coursera.org/verify', 'verified'),

            ('UI/UX Design Fundamentals',
             'Google', '2023-06-01', None,
             'User research, wireframing, prototyping and Figma.',
             'Design', 'Beginner', None, 'verified'),

            ('Machine Learning Specialization',
             'DeepLearning.AI', '2024-02-20', None,
             'Supervised learning, neural networks and model deployment.',
             'Data Science', 'Advanced', None, 'unverified'),

            ('Agile Project Management',
             'PMI', '2024-01-10', None,
             'Scrum, sprints and stakeholder communication.',
             'Business', 'Intermediate', None, 'unverified'),
        ])
        print(f"     → {n} credentials for Alex Johnson")

    # ── LEARNER 2 — Priya Sharma ──
    priya_uid = insert_user(conn,
        name='Priya Sharma',
        email='priya@demo.com',
        password='priya123',
        role='learner',
        bio='Data scientist with experience in ML and analytics.',
        headline='Data Scientist | Python | TensorFlow',
        location='Mumbai, India',
        linkedin='https://linkedin.com/in/priyasharma',
        github='https://github.com/priyasharma',
        avatar_color='#7c3aed')

    if priya_uid:
        n = insert_credentials(conn, priya_uid, [
            ('Python for Everybody',
             'Coursera', '2022-11-10', None,
             'Python programming fundamentals and web scraping.',
             'Technology', 'Beginner', None, 'verified'),

            ('TensorFlow Developer Certificate',
             'Google', '2023-05-18', '2026-05-18',
             'Deep learning with TensorFlow, Keras, CNN and RNN models.',
             'Data Science', 'Advanced',
             'https://google.com/verify', 'verified'),

            ('SQL for Data Analysis',
             'Udacity', '2023-09-01', None,
             'Advanced SQL queries, joins, window functions.',
             'Data Science', 'Intermediate', None, 'verified'),

            ('Power BI Certification',
             'Microsoft', '2024-01-25', None,
             'Data visualization and business intelligence dashboards.',
             'Data Science', 'Intermediate', None, 'unverified'),
        ])
        print(f"     → {n} credentials for Priya Sharma")

    # ── LEARNER 3 — Rahul Kumar ──
    rahul_uid = insert_user(conn,
        name='Rahul Kumar',
        email='rahul@demo.com',
        password='rahul123',
        role='learner',
        bio='Cybersecurity enthusiast and ethical hacker.',
        headline='Cybersecurity Analyst | Ethical Hacker',
        location='Delhi, India',
        github='https://github.com/rahulk',
        avatar_color='#059669')

    if rahul_uid:
        n = insert_credentials(conn, rahul_uid, [
            ('CompTIA Security+',
             'CompTIA', '2023-07-14', '2026-07-14',
             'Network security, cryptography and risk management.',
             'Technology', 'Intermediate', None, 'verified'),

            ('Certified Ethical Hacker (CEH)',
             'EC-Council', '2023-12-01', '2026-12-01',
             'Penetration testing and vulnerability assessment.',
             'Technology', 'Advanced', None, 'verified'),

            ('Linux Essentials',
             'LPI', '2022-08-20', None,
             'Linux command line, file systems and shell scripting.',
             'Technology', 'Beginner', None, 'verified'),

            ('Network+ Certification',
             'CompTIA', '2024-03-10', None,
             'Networking concepts, protocols and troubleshooting.',
             'Technology', 'Intermediate', None, 'unverified'),
        ])
        print(f"     → {n} credentials for Rahul Kumar")

    # ── Sample notifications for Alex ──
    if alex_uid:
        try:
            for msg, link in [
                ('✅ Your AWS credential has been verified!', '/dashboard'),
                ('💼 TechCorp HR viewed your profile.', '/dashboard'),
                ('🔐 Your ZKP proof token is ready to share.', '/dashboard'),
            ]:
                conn.execute("""
                    INSERT INTO notifications (
                        notif_id, user_id, message, link, is_read, created_at
                    ) VALUES (?,?,?,?,?,?)
                """, (str(uuid.uuid4()), alex_uid, msg, link,
                      0, datetime.now().isoformat()))
            conn.commit()
            print("  ✅ Sample notifications added")
        except Exception as e:
            print(f"  ⚠️  Notifications: {e}")

    conn.close()

    print()
    print("=" * 52)
    print("  Demo Login Credentials:")
    print()
    print("  Admin    : admin@microcred.io   / admin123")
    print("  Employer : employer@techcorp.com / emp123")
    print("  Learner 1: learner@demo.com     / demo123")
    print("  Learner 2: priya@demo.com       / priya123")
    print("  Learner 3: rahul@demo.com       / rahul123")
    print("=" * 52)
    print()
    print("✅ Done! Open http://localhost:5000")

if __name__ == '__main__':
    seed()
