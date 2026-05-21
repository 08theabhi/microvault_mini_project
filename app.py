from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import sqlite3, hashlib, os, uuid, json, logging, random, string, struct
from datetime import datetime, date, timedelta
from functools import wraps
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── OCR Integration ──
try:
    from ocr_routes import register_ocr_routes
    from ocr_certificate_verification import CertificateOCRService
    OCR_AVAILABLE = True
except ImportError as e:
    OCR_AVAILABLE = False
    print(f'⚠️ OCR module not available: {e}. Install with: pip install pytesseract opencv-python pillow')

# ── SocketIO for real-time messaging ──
try:
    from flask_socketio import SocketIO, emit, join_room, leave_room
    SOCKETIO_AVAILABLE = True
except ImportError:
    SOCKETIO_AVAILABLE = False
    logging.warning('flask-socketio not installed. Run: pip install flask-socketio eventlet')

# ── APScheduler for automation ──
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    logging.warning('apscheduler not installed. Run: pip install apscheduler')

# ── Load .env if present ──
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; use system env vars directly

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── SocketIO initialization ──
if SOCKETIO_AVAILABLE:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet',
                        logger=False, engineio_logger=False)
else:
    socketio = None

# ── Secret Key ──
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32)
if not os.environ.get('SECRET_KEY'):
    logger.warning('SECRET_KEY not set — sessions will reset on restart. Set SECRET_KEY in .env')

# ── Security Headers & HTTPS Enforcement (fix #8) ──
@app.after_request
def apply_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options']        = 'SAMEORIGIN'
    response.headers['X-XSS-Protection']       = '1; mode=block'
    response.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    # Only enforce HTTPS in production (when explicitly enabled)
    if os.environ.get('FORCE_HTTPS', 'false').lower() == 'true':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

@app.before_request
def enforce_https():
    """Redirect HTTP to HTTPS in production if FORCE_HTTPS=true."""
    if os.environ.get('FORCE_HTTPS', 'false').lower() == 'true':
        if not request.is_secure and request.headers.get('X-Forwarded-Proto', 'http') != 'https':
            url = request.url.replace('http://', 'https://', 1)
            return redirect(url, code=301)

UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', 10))
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

DB = os.environ.get('DB_PATH', 'microcred.db')

# ── Email / OTP Config ──
MAIL_HOST   = os.environ.get('MAIL_HOST', 'smtp.gmail.com')
MAIL_PORT   = int(os.environ.get('MAIL_PORT', 587))
MAIL_USER   = os.environ.get('MAIL_USER', '')
MAIL_PASS   = os.environ.get('MAIL_PASS', '')
MAIL_FROM   = os.environ.get('MAIL_FROM', 'MicroCred <noreply@microcred.io>')
OTP_EXPIRY_MINUTES = 10

# ── Rate Limiting (fix #7) ──
# In-memory store: {ip: [timestamp, ...]}
_rate_store = {}

def rate_limit(key, max_attempts=5, window_seconds=300):
    """Return True if request is allowed, False if rate limited."""
    now = datetime.now().timestamp()
    bucket = _rate_store.get(key, [])
    # Remove old attempts outside window
    bucket = [t for t in bucket if now - t < window_seconds]
    if len(bucket) >= max_attempts:
        return False
    bucket.append(now)
    _rate_store[key] = bucket
    return True

def get_rate_key(prefix):
    """Build rate limit key from IP + prefix."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    return f"{prefix}:{ip}"

# ── File Magic Bytes Validation (fix #9) ──
FILE_SIGNATURES = {
    b'%PDF':           'pdf',
    b'\x89PNG\r\n':   'png',
    b'\xff\xd8\xff':  'jpg',
    b'GIF8':          'gif',
}

def validate_file_content(file_storage):
    """Check file magic bytes match the claimed extension. Returns True if safe."""
    header = file_storage.read(8)
    file_storage.seek(0)  # Reset for actual saving
    ext = file_storage.filename.rsplit('.', 1)[-1].lower() if '.' in file_storage.filename else ''
    for sig, sig_ext in FILE_SIGNATURES.items():
        if header.startswith(sig):
            # PDF magic bytes match pdf extension, PNG matches png/jpg is jpeg
            if sig_ext == 'jpg' and ext in ('jpg', 'jpeg'):
                return True
            if sig_ext == ext:
                return True
            if sig_ext == 'pdf' and ext == 'pdf':
                return True
    # If no signature matched but it's a JPEG variant
    if header[:2] == b'\xff\xd8' and ext in ('jpg', 'jpeg'):
        return True
    return False

# ── Google OAuth Config (fix #18) ──
GOOGLE_CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')

# ── Twilio SMS Config ──
TWILIO_SID   = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_FROM  = os.environ.get('TWILIO_PHONE_NUMBER', '')  # e.g. +1234567890

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

app.jinja_env.filters['from_json'] = lambda s: json.loads(s) if s else []

def migrate_db():
    cols = [
        ("users","headline","TEXT DEFAULT ''"),("users","location","TEXT DEFAULT ''"),
        ("users","linkedin","TEXT DEFAULT ''"),("users","github","TEXT DEFAULT ''"),
        ("users","website","TEXT DEFAULT ''"),("users","avatar_color","TEXT DEFAULT '#f0a430'"),
        ("users","lang","TEXT DEFAULT 'en'"),("users","dark_mode","INTEGER DEFAULT 0"),
        ("users","suspended","INTEGER DEFAULT 0"),
        ("users","suspend_reason","TEXT DEFAULT NULL"),
        ("users","google_id","TEXT DEFAULT NULL"),
        ("users","phone","TEXT DEFAULT NULL"),
        ("users","phone_verified","INTEGER DEFAULT 0"),
        ("credentials","expiry_date","TEXT DEFAULT NULL"),
        ("credentials","credential_url","TEXT DEFAULT NULL"),
        ("credentials","zkp_commitment","TEXT DEFAULT NULL"),
        ("credentials","zkp_salt","TEXT DEFAULT NULL"),
    ]
    with get_db() as conn:
        for table,col,coldef in cols:
            try: conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}")
            except: pass
        # Reports table
        conn.execute('''CREATE TABLE IF NOT EXISTS reports (
            report_id TEXT PRIMARY KEY, admin_id TEXT, target_type TEXT,
            target_id TEXT, reason TEXT, notes TEXT,
            status TEXT DEFAULT "open", created_at TEXT
        )''')
        # Messages/inbox table
        conn.execute('''CREATE TABLE IF NOT EXISTS messages (
            msg_id TEXT PRIMARY KEY, sender_id TEXT, receiver_id TEXT,
            subject TEXT, body TEXT, is_read INTEGER DEFAULT 0,
            parent_id TEXT DEFAULT NULL, created_at TEXT
        )''')
        # Profile views
        conn.execute('''CREATE TABLE IF NOT EXISTS profile_views (
            view_id TEXT PRIMARY KEY, viewer_id TEXT,
            profile_id TEXT, viewed_at TEXT
        )''')
        # Connections table
        conn.execute('''CREATE TABLE IF NOT EXISTS connections (
            connection_id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (requester_id) REFERENCES users(user_id),
            FOREIGN KEY (receiver_id) REFERENCES users(user_id),
            UNIQUE(requester_id, receiver_id)
        )''')
        # Helpline table
        conn.execute('''CREATE TABLE IF NOT EXISTS helpline (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            status TEXT DEFAULT 'open',
            priority TEXT DEFAULT 'normal',
            admin_response TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )''')
        # Phone OTP verifications
        conn.execute('''CREATE TABLE IF NOT EXISTS phone_verifications (
            ver_id TEXT PRIMARY KEY,
            phone TEXT NOT NULL,
            code TEXT NOT NULL,
            purpose TEXT DEFAULT "register",
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT
        )''')
        # ZKP proofs table
        conn.execute('''CREATE TABLE IF NOT EXISTS zkp_proofs (
            proof_id   TEXT PRIMARY KEY,
            credential_id TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            claim_type TEXT NOT NULL,
            claim_value TEXT NOT NULL,
            proof_hash TEXT NOT NULL,
            proof_token TEXT NOT NULL,
            expires_at TEXT,
            created_at TEXT
        )''')
        # Contact requests (employer → learner)
        conn.execute('''CREATE TABLE IF NOT EXISTS contact_requests (
            req_id TEXT PRIMARY KEY, employer_id TEXT, learner_id TEXT,
            message TEXT, status TEXT DEFAULT 'pending', created_at TEXT
        )''')
        # Activity log for admin audit trail
        conn.execute('''CREATE TABLE IF NOT EXISTS activity_log (
            log_id TEXT PRIMARY KEY, admin_id TEXT, action TEXT,
            target_id TEXT, details TEXT, created_at TEXT
        )''')
        # Learning pathways
        conn.execute('''CREATE TABLE IF NOT EXISTS pathways (
            pathway_id TEXT PRIMARY KEY, user_id TEXT,
            title TEXT, description TEXT, steps TEXT, created_at TEXT
        )''')
        # Notifications
        conn.execute('''CREATE TABLE IF NOT EXISTS notifications (
            notif_id TEXT PRIMARY KEY, user_id TEXT, message TEXT,
            link TEXT, is_read INTEGER DEFAULT 0, created_at TEXT
        )''')

def init_db():
    with get_db() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL, role TEXT DEFAULT 'learner', bio TEXT,
            headline TEXT DEFAULT '', location TEXT DEFAULT '', linkedin TEXT DEFAULT '',
            github TEXT DEFAULT '', website TEXT DEFAULT '', avatar_color TEXT DEFAULT '#6c63ff',
            lang TEXT DEFAULT 'en', dark_mode INTEGER DEFAULT 0, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS credentials (
            credential_id TEXT PRIMARY KEY, user_id TEXT, title TEXT NOT NULL,
            provider TEXT NOT NULL, issue_date TEXT, expiry_date TEXT, description TEXT,
            skill_category TEXT, level TEXT, file_path TEXT, credential_url TEXT,
            status TEXT DEFAULT 'unverified', hash_value TEXT, created_at TEXT,
            zkp_commitment TEXT DEFAULT NULL, zkp_salt TEXT DEFAULT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS verification (
            verification_id TEXT PRIMARY KEY, credential_id TEXT, hash_value TEXT,
            verified_status TEXT, verified_at TEXT,
            FOREIGN KEY(credential_id) REFERENCES credentials(credential_id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            notif_id TEXT PRIMARY KEY, user_id TEXT, message TEXT, link TEXT,
            is_read INTEGER DEFAULT 0, created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS pathways (
            pathway_id TEXT PRIMARY KEY, user_id TEXT, title TEXT, description TEXT,
            steps TEXT, created_at TEXT, FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            log_id TEXT PRIMARY KEY, admin_id TEXT, action TEXT,
            target_id TEXT, details TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS contact_requests (
            req_id TEXT PRIMARY KEY, employer_id TEXT, learner_id TEXT,
            message TEXT, status TEXT DEFAULT 'pending', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS otp_codes (
            otp_id TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            code TEXT NOT NULL,
            purpose TEXT DEFAULT 'register',
            expires_at TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT
        );
        ''')
        admin_id = str(uuid.uuid4())
        emp_id = str(uuid.uuid4())
        try:
            conn.execute("INSERT INTO users(user_id,name,email,password,role,bio,headline,location,linkedin,github,website,avatar_color,lang,dark_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (admin_id,'Admin','admin@microcred.io',generate_password_hash('admin123'),'admin','Platform administrator','Platform Admin','','','','','#0f172a','en',0,datetime.now().isoformat()))
            conn.execute("INSERT INTO users(user_id,name,email,password,role,bio,headline,location,linkedin,github,website,avatar_color,lang,dark_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (emp_id,'TechCorp HR','employer@techcorp.com',generate_password_hash('emp123'),'employer','Leading tech company recruiter','Senior Technical Recruiter','Bengaluru','','','','#0891b2','en',0,datetime.now().isoformat()))
        except: pass

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.',1)[1].lower() in ALLOWED_EXTENSIONS

def generate_hash(cid, uid, title, provider):
    return hashlib.sha256(f"{cid}{uid}{title}{provider}".encode()).hexdigest()

# ════════════════════════════════════════
#  AUTOMATED FRAUD DETECTION ENGINE
# ════════════════════════════════════════

# Known legitimate providers and what they typically issue
KNOWN_PROVIDERS = {
    'coursera':       ['specialization','certificate','course','professional'],
    'udemy':          ['course','complete','bootcamp','masterclass'],
    'aws':            ['certified','associate','professional','practitioner','specialty'],
    'amazon web services': ['certified','solutions architect','developer','sysops','devops'],
    'google':         ['professional','associate','cloud','data analytics','ux design','it support'],
    'microsoft':      ['azure','certified','associate','expert','fundamentals','mcp'],
    'cisco':          ['ccna','ccnp','ccie','cyberops','devnet'],
    'ibm':            ['certified','data science','ai','cloud','watson'],
    'meta':           ['certified','front-end','back-end','marketing','social media'],
    'deeplearning.ai':['specialization','deep learning','nlp','mlops','tensorflow'],
    'edx':            ['micromasters','professional certificate','xseries','course'],
    'linkedin learning':['certificate','learning path','course'],
    'pmi':            ['pmp','capm','agile','scrum','pgmp'],
    'comptia':        ['a+','network+','security+','cloud+','cysa+','pentest+'],
    'oracle':         ['oca','ocp','certified','java','database','cloud'],
    'salesforce':     ['certified','administrator','developer','consultant','architect'],
    'harvard':        ['certificate','cs50','extension','online','professional'],
    'mit':            ['certificate','micromasters','professional','opencourseware'],
    'stanford':       ['certificate','online','professional','machine learning'],
    'nptel':          ['certification','course','elite','topper'],
    'infosys springboard':['foundation','intermediate','advanced','professional'],
}

# Red flag keywords in certificate titles
SUSPICIOUS_KEYWORDS = [
    'master of', 'bachelor of', 'doctor of', 'phd', 'mba', 'btech', 'mtech',
    'university degree', 'graduation', 'diploma in', 'postgraduate',
    'gold medal', 'rank 1', 'topper', '100%', 'perfect score',
    'lifetime', 'permanent', 'guaranteed', 'instant', 'express',
    'fake', 'sample', 'test', 'demo', 'dummy', 'example',
]

# Minimum realistic days between issue and upload
MIN_ISSUE_DAYS_AGO   = 0     # can upload same day
MAX_ISSUE_YEARS_AGO  = 20    # older than 20 years is suspicious
MAX_VALIDITY_YEARS   = 10    # validity period > 10 years is suspicious

def calculate_text_similarity(str1, str2):
    """Jaccard similarity between two strings (word-level)."""
    if not str1 or not str2:
        return 0.0
    words1 = set(str1.lower().split())
    words2 = set(str2.lower().split())
    if not words1 or not words2:
        return 0.0
    intersection = words1 & words2
    union = words1 | words2
    return len(intersection) / len(union)

def get_file_hash(file_path):
    """SHA-256 hash of the actual file content."""
    full_path = os.path.join(UPLOAD_FOLDER, file_path)
    if not file_path or not os.path.exists(full_path):
        return None
    try:
        h = hashlib.sha256()
        with open(full_path, 'rb') as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None

# ════════════════════════════════════════
#  AI DOCUMENT ANALYSIS
#  Uses FREE tools:
#  1. Tesseract OCR  — reads text from image/PDF (100% free)
#  2. PyMuPDF        — extracts text from PDFs (100% free)
#  3. Groq API       — free LLaMA3 AI for analysis
#     (Free tier: 14,400 requests/day — sign up at groq.com)
# ════════════════════════════════════════

def extract_text_from_file(file_path):
    """
    Extract all text from an uploaded certificate file.
    Supports PDF (via PyMuPDF) and images (via Tesseract OCR).
    Both are 100% free and open source.
    """
    full_path = os.path.join(UPLOAD_FOLDER, file_path)
    if not full_path or not os.path.exists(full_path):
        return None, "File not found"

    ext = file_path.rsplit('.', 1)[-1].lower() if '.' in file_path else ''
    extracted_text = ''

    try:
        if ext == 'pdf':
            # Use PyMuPDF (fitz) to extract text from PDF
            try:
                import fitz  # PyMuPDF
                doc  = fitz.open(full_path)
                text = ''
                for page in doc:
                    text += page.get_text()
                doc.close()

                if text.strip():
                    extracted_text = text.strip()
                else:
                    # PDF has no embedded text (scanned) — convert to image and OCR
                    doc = fitz.open(full_path)
                    page = doc[0]
                    mat  = fitz.Matrix(2, 2)  # 2x zoom for better OCR
                    pix  = page.get_pixmap(matrix=mat)
                    doc.close()
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                        pix.save(tmp.name)
                        tmp_path = tmp.name
                    from PIL import Image
                    import pytesseract
                    img  = Image.open(tmp_path)
                    extracted_text = pytesseract.image_to_string(img, config='--oem 3 --psm 6')
                    os.unlink(tmp_path)
            except ImportError:
                return None, "PyMuPDF not installed — run: pip install pymupdf"

        elif ext in ['png', 'jpg', 'jpeg']:
            # Use Tesseract OCR for images
            try:
                import pytesseract
                from PIL import Image

                img  = Image.open(full_path)
                # Enhance image for better OCR accuracy
                img  = img.convert('RGB')
                extracted_text = pytesseract.image_to_string(img, config='--oem 3 --psm 6')
            except ImportError:
                return None, "pytesseract not installed — run: pip install pytesseract"
            except Exception as e:
                return None, f"OCR failed: {str(e)}"
        else:
            return None, f"Unsupported file type: {ext}"

        extracted_text = extracted_text.strip()
        if len(extracted_text) < 10:
            return '', "Very little text found — document may be a blank image or non-certificate"
        return extracted_text, None

    except Exception as e:
        logger.error(f"Text extraction error: {e}")
        return None, str(e)


def ai_analyze_certificate(file_path, claimed_title, claimed_provider,
                           claimed_name, claimed_date):
    """
    Analyze a certificate using:
    1. Tesseract OCR / PyMuPDF — extract text (FREE)
    2. Rule-based text matching — check if claimed details exist in text (FREE)
    3. Groq API (LLaMA3) — deep AI analysis (FREE tier: groq.com)

    Returns a comprehensive fraud analysis result.
    """
    full_path = os.path.join(UPLOAD_FOLDER, file_path) if file_path else None

    default = {
        'is_certificate': None, 'name_found': None, 'provider_found': None,
        'date_found': None, 'title_match': None, 'ai_risk_score': 0,
        'ai_summary': 'AI analysis not available.',
        'ai_flags': [], 'confidence': 'low', 'ai_available': False,
        'extracted_text': None, 'method': 'none'
    }

    if not full_path or not os.path.exists(full_path):
        default['ai_summary'] = 'No file uploaded — cannot verify document content.'
        default['ai_risk_score'] = 25
        default['ai_flags'] = ['No certificate document attached']
        return default

    # ── STEP 1: Extract text using OCR (FREE) ──
    extracted_text, ocr_error = extract_text_from_file(file_path)

    if ocr_error and not extracted_text:
        default['ai_summary'] = f'Text extraction failed: {ocr_error}'
        default['ai_risk_score'] = 15
        default['method'] = 'ocr_failed'
        return default

    text_lower = (extracted_text or '').lower()
    default['extracted_text'] = extracted_text[:500] if extracted_text else None

    # ── STEP 2: Rule-based text matching (FREE, no API needed) ──
    result = {
        'ai_available':   True,
        'method':         'ocr_rules',
        'extracted_text': extracted_text[:500] if extracted_text else None,
        'is_certificate': None,
        'name_found':     None,
        'provider_found': None,
        'date_found':     None,
        'title_match':    None,
        'ai_risk_score':  0,
        'ai_flags':       [],
        'confidence':     'medium',
        'ai_summary':     '',
    }

    ai_score = 0
    ai_flags = []

    # ── Negative keywords — documents that are NOT completion certificates ──
    # These words indicate admission, exam permission, or other non-completion docs
    NOT_CERTIFICATE_KEYWORDS = [
        # Hall ticket / admit card indicators
        'hall ticket', 'admit card', 'admission ticket', 'hall-ticket',
        'roll number', 'roll no', 'seat number', 'seat no',
        'examination hall', 'exam hall', 'reporting time', 'report time',
        'examination centre', 'exam centre', 'exam center', 'test center',
        'invigilator', 'candidate must', 'candidate should', 'instructions to candidate',
        'bring this', 'carry this', 'show this', 'present this',
        'entry ticket', 'permission to appear', 'allowed to appear',

        # Application / registration forms
        'application form', 'registration form', 'application number',
        'form number', 'filled by', 'applicant',

        # Invoice / receipt
        'invoice', 'receipt', 'payment receipt', 'tax invoice',
        'amount paid', 'total amount', 'gst', 'bill to',

        # ID cards
        'identity card', 'id card', 'employee id', 'student id',
        'library card', 'membership card',

        # Mark sheets / transcripts (different from certificates)
        'mark sheet', 'marksheet', 'grade sheet', 'transcript',
        'result sheet', 'marks obtained', 'subject marks',
        'pass/fail', 'pass / fail',
    ]

    # Check for negative keywords — strong indicators this is NOT a completion cert
    found_negative = [kw for kw in NOT_CERTIFICATE_KEYWORDS if kw in text_lower]

    # Document type classification
    # Completion certificate POSITIVE keywords
    COMPLETION_KEYWORDS = [
        'certificate of completion', 'this is to certify', 'hereby certify',
        'successfully completed', 'has completed', 'has successfully',
        'awarded this certificate', 'in recognition of', 'upon completion',
        'certifies that', 'this certifies', 'certificate of achievement',
        'certificate of participation', 'has passed', 'has qualified',
        'conferred upon', 'granted to', 'presented to',
    ]

    completion_phrase_count = sum(1 for kw in COMPLETION_KEYWORDS if kw in text_lower)

    # Generic certificate keywords (weaker signal)
    certificate_keywords = [
        'certificate', 'certify', 'certified', 'completion', 'achievement',
        'awarded', 'successfully', 'hereby', 'credential', 'diploma',
        'specialization', 'verified', 'training', 'program',
    ]
    cert_keyword_count  = sum(1 for kw in certificate_keywords if kw in text_lower)
    cert_keyword_substr = sum(1 for kw in certificate_keywords if kw in text_lower.replace(' ', ''))

    # ── Document type determination ──
    if len(text_lower) < 20:
        result['is_certificate'] = False
        ai_score += 50
        ai_flags.append('Document contains almost no readable text — likely a blank or unreadable image')

    elif found_negative:
        # Strong negative signal — this is clearly NOT a completion certificate
        result['is_certificate'] = False
        if completion_phrase_count >= 1:
            # Has both positive and negative — very suspicious
            penalty = 45
            ai_flags.append(
                f'Document appears to be a HALL TICKET / ADMIT CARD / NON-COMPLETION document. '
                f'Found: "{found_negative[0]}" — This is not a certificate of completion.'
            )
        else:
            penalty = 55
            ai_flags.append(
                f'Document is NOT a completion certificate. '
                f'Detected as: {found_negative[0].upper()}. '
                f'Hall tickets, admit cards, mark sheets, invoices and ID cards '
                f'are not valid credentials.'
            )
        ai_score += penalty
        result['confidence'] = 'high'

    elif completion_phrase_count >= 1:
        # Strong positive signal — has completion language
        result['is_certificate'] = True
        result['confidence']     = 'high'

    elif cert_keyword_count >= 2 or cert_keyword_substr >= 2:
        # Moderate positive signal
        result['is_certificate'] = True
        result['confidence']     = 'medium'

    elif cert_keyword_count == 1 or cert_keyword_substr == 1:
        # Weak positive signal
        result['is_certificate'] = True
        result['confidence']     = 'low'
        ai_flags.append('Few certificate keywords found — document type uncertain')

    else:
        result['is_certificate'] = False
        ai_score += 35
        ai_flags.append(
            f'Document does not contain certificate-related keywords. '
            f'Found {cert_keyword_count} of {len(certificate_keywords)} expected keywords.'
        )

    # Check if learner name is in the document
    name_parts = claimed_name.lower().split() if claimed_name else []
    name_matches = sum(1 for part in name_parts if len(part) > 2 and part in text_lower)
    if name_parts:
        if name_matches >= len(name_parts):
            result['name_found'] = True
        elif name_matches >= 1:
            result['name_found'] = True
            result['confidence'] = 'medium'
        else:
            result['name_found'] = False
            ai_score += 25
            ai_flags.append(f'Learner name "{claimed_name}" not found in document text')

    # Check if provider is in the document
    provider_parts = claimed_provider.lower().split() if claimed_provider else []
    # Filter common words
    stop_words = {'of', 'the', 'and', 'for', 'in', 'by', 'at', 'to', 'a', 'an'}
    provider_keywords = [p for p in provider_parts if p not in stop_words and len(p) > 2]
    provider_matches  = sum(1 for pk in provider_keywords if pk in text_lower)

    if provider_keywords:
        if provider_matches >= max(1, len(provider_keywords) // 2):
            result['provider_found'] = True
        else:
            result['provider_found'] = False
            ai_score += 20
            ai_flags.append(f'Provider "{claimed_provider}" not clearly found in document')

    # Check if a date is present
    import re
    date_patterns = [
        r'\b\d{4}\b',                                # year like 2024
        r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b',      # dd/mm/yyyy
        r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4}\b',  # Month Year
        r'\b\d{4}[/-]\d{2}[/-]\d{2}\b',             # yyyy-mm-dd
    ]
    date_found = any(re.search(pat, text_lower) for pat in date_patterns)
    result['date_found'] = date_found
    if not date_found:
        ai_score += 10
        ai_flags.append('No date found in the document')

    # Check if title keywords appear in document
    title_words = [w for w in claimed_title.lower().split()
                   if w not in stop_words and len(w) > 3]
    title_matches = sum(1 for tw in title_words if tw in text_lower)
    if title_words:
        match_ratio = title_matches / len(title_words)
        if match_ratio >= 0.5:
            result['title_match'] = True
        elif match_ratio >= 0.25:
            result['title_match'] = True
            result['confidence'] = 'low'
        else:
            result['title_match'] = False
            ai_score += 20
            ai_flags.append(f'Claimed title "{claimed_title}" keywords not found in document')

    result['ai_risk_score'] = min(ai_score, 100)
    result['ai_flags']      = ai_flags

    # Build summary
    if ai_score == 0:
        result['ai_summary'] = f'OCR verified: Document appears to be a genuine certificate. Name, provider and title found in document text.'
    elif ai_score <= 20:
        result['ai_summary'] = f'OCR analysis: Document looks like a certificate but some details could not be confirmed.'
    elif ai_score <= 45:
        result['ai_summary'] = f'OCR analysis: Several claimed details not found in document. Manual review strongly recommended.'
    else:
        result['ai_summary'] = f'OCR analysis: Document does not appear to be a legitimate certificate matching the claimed details.'

    # ── STEP 3: Groq AI deep analysis (FREE tier) ──
    groq_key = os.environ.get('GROQ_API_KEY', '')
    if groq_key and extracted_text and len(extracted_text) > 20:
        try:
            import urllib.request, json as json_mod

            # Truncate text to save tokens
            text_sample = extracted_text[:1500]

            prompt = f"""You are a certificate fraud detection AI. Analyze this certificate text and the claims made.

CLAIMED DETAILS:
- Title: {claimed_title}
- Provider: {claimed_provider}
- Issued to: {claimed_name}
- Issue date: {claimed_date}

ACTUAL TEXT EXTRACTED FROM DOCUMENT (via OCR):
{text_sample}

Answer ONLY with a JSON object — no other text:
{{
  "is_genuine_certificate": true or false,
  "details_match": true or false,
  "fraud_risk": "low" or "medium" or "high" or "critical",
  "risk_score": 0 to 100,
  "issues": ["list of specific issues found"],
  "verdict": "one sentence verdict"
}}"""

            payload = json_mod.dumps({
                "model": "llama3-8b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "temperature": 0.1
            }).encode('utf-8')

            req = urllib.request.Request(
                'https://api.groq.com/openai/v1/chat/completions',
                data=payload,
                headers={
                    'Content-Type':  'application/json',
                    'Authorization': f'Bearer {groq_key}'
                }
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                groq_result = json_mod.loads(resp.read())

            raw = groq_result['choices'][0]['message']['content'].strip()
            if '```' in raw:
                raw = raw.split('```')[1]
                if raw.startswith('json'): raw = raw[4:]

            ai_data = json_mod.loads(raw)

            # Merge Groq result with OCR result
            groq_score = ai_data.get('risk_score', 0)
            # Average OCR score and Groq score
            combined   = int((result['ai_risk_score'] + groq_score) / 2)
            result['ai_risk_score'] = min(combined, 100)
            result['ai_summary']    = ai_data.get('verdict', result['ai_summary'])
            result['method']        = 'ocr_groq_llama3'
            result['confidence']    = 'high'

            for issue in ai_data.get('issues', []):
                if issue not in result['ai_flags']:
                    result['ai_flags'].append(issue)

            logger.info(f'Groq AI analysis complete: risk={groq_score} verdict={ai_data.get("verdict","")}')

        except Exception as e:
            logger.warning(f'Groq AI analysis failed (using OCR result only): {e}')
            result['method'] = 'ocr_only'
    else:
        if not groq_key:
            result['method'] = 'ocr_only'
            logger.info('Groq AI skipped — GROQ_API_KEY not set (using OCR rules only)')

    return result

def run_fraud_detection(credential_id):
    """
    Full automated fraud analysis on a credential.
    Returns a dict with:
      - risk_score: 0-100 (0=clean, 100=definitely fraud)
      - risk_level: 'low' | 'medium' | 'high' | 'critical'
      - flags: list of detected issues
      - checks: list of all checks with pass/fail status
      - recommendation: 'approve' | 'review' | 'reject'
    """
    flags   = []
    checks  = []
    score   = 0

    def add_check(name, passed, detail='', weight=0):
        checks.append({'name': name, 'passed': passed, 'detail': detail})
        if not passed:
            flags.append({'check': name, 'detail': detail, 'weight': weight})
            return weight
        return 0

    with get_db() as conn:
        # Load the credential being checked
        cred = conn.execute("""
            SELECT c.*, u.name as owner_name, u.email as owner_email,
                   u.created_at as user_created
            FROM credentials c JOIN users u ON c.user_id=u.user_id
            WHERE c.credential_id=?
        """, (credential_id,)).fetchone()

        if not cred:
            return {'error': 'Credential not found'}

        # Load all other credentials for cross-checking
        all_creds = conn.execute("""
            SELECT c.*, u.name as owner_name FROM credentials c
            JOIN users u ON c.user_id=u.user_id
            WHERE c.credential_id != ?
        """, (credential_id,)).fetchall()

        # Load this user's credentials
        user_creds = conn.execute("""
            SELECT * FROM credentials WHERE user_id=? AND credential_id!=?
            ORDER BY created_at DESC
        """, (cred['user_id'], credential_id)).fetchall()

    title    = (cred['title'] or '').strip()
    provider = (cred['provider'] or '').strip()
    issue_dt = cred['issue_date'] or ''
    expiry_dt= cred['expiry_date'] or ''
    file_path= cred['file_path'] or ''

    # ── CHECK 1: Future issue date ──
    if issue_dt:
        try:
            issue_date_obj = date.fromisoformat(issue_dt)
            today          = date.today()
            days_ago       = (today - issue_date_obj).days
            if issue_date_obj > today:
                score += add_check('Issue date in future', False,
                    f'Certificate claims to be issued on {issue_dt} which is in the future', 40)
            elif days_ago > MAX_ISSUE_YEARS_AGO * 365:
                score += add_check('Issue date too old', False,
                    f'Certificate issued {int(days_ago/365)} years ago — older than {MAX_ISSUE_YEARS_AGO} years', 15)
            else:
                add_check('Issue date realistic', True, f'Issued {days_ago} days ago')
        except ValueError:
            score += add_check('Issue date invalid format', False, f'Date "{issue_dt}" is not valid', 20)
    else:
        score += add_check('Issue date missing', False, 'No issue date provided', 10)

    # ── CHECK 2: Expiry date consistency ──
    if expiry_dt and issue_dt:
        try:
            exp_obj   = date.fromisoformat(expiry_dt)
            issue_obj = date.fromisoformat(issue_dt)
            validity_years = (exp_obj - issue_obj).days / 365
            if exp_obj <= issue_obj:
                score += add_check('Expiry before issue date', False,
                    f'Expiry {expiry_dt} is before issue date {issue_dt}', 35)
            elif validity_years > MAX_VALIDITY_YEARS:
                score += add_check('Validity period too long', False,
                    f'Validity of {validity_years:.0f} years exceeds normal range', 15)
            else:
                add_check('Expiry date consistent', True,
                    f'Valid for {validity_years:.1f} years')
        except ValueError:
            score += add_check('Expiry date invalid', False, f'Invalid expiry date format', 10)
    else:
        add_check('Expiry date (optional)', True, 'No expiry date — acceptable')

    # ── CHECK 3: Suspicious keywords in title ──
    title_lower = title.lower()
    found_suspicious = [kw for kw in SUSPICIOUS_KEYWORDS if kw in title_lower]
    if found_suspicious:
        score += add_check('Suspicious title keywords', False,
            f'Title contains suspicious words: {", ".join(found_suspicious)}', 30)
    else:
        add_check('Title keywords clean', True, 'No suspicious keywords detected')

    # ── CHECK 4: Provider-title consistency ──
    provider_lower = provider.lower()
    matched_provider = None
    for known, keywords in KNOWN_PROVIDERS.items():
        if known in provider_lower or provider_lower in known:
            matched_provider = (known, keywords)
            break

    if matched_provider:
        known_name, expected_keywords = matched_provider
        title_words = title_lower.split()
        keyword_match = any(kw in title_lower for kw in expected_keywords)
        if keyword_match:
            add_check('Provider-title consistency', True,
                f'{provider} typically issues credentials matching this title')
        else:
            score += add_check('Provider-title mismatch', False,
                f'{provider} does not typically issue "{title}". Expected keywords: {", ".join(expected_keywords)}', 20)
    else:
        add_check('Provider not in known list', True,
            f'"{provider}" not in known providers list — manual check recommended')

    # ── CHECK 5: Cross-user duplicate detection ──
    exact_duplicates = []
    similar_duplicates = []
    for other in all_creds:
        other_title    = (other['title'] or '').strip()
        other_provider = (other['provider'] or '').strip()
        other_date     = other['issue_date'] or ''

        # Exact match: same title + provider + date → same person uploading twice?
        if (title.lower() == other_title.lower() and
            provider.lower() == other_provider.lower() and
            issue_dt == other_date):
            exact_duplicates.append(other['owner_name'])

        # High similarity: >85% title similarity + same provider
        elif (calculate_text_similarity(title, other_title) >= 0.85 and
              provider.lower() == other_provider.lower()):
            similar_duplicates.append({
                'name':  other['owner_name'],
                'title': other_title,
                'sim':   round(calculate_text_similarity(title, other_title) * 100)
            })

    if exact_duplicates:
        score += add_check('Exact duplicate across users', False,
            f'Identical credential found under: {", ".join(exact_duplicates[:3])}', 45)
    else:
        add_check('No exact cross-user duplicates', True)

    if similar_duplicates:
        names = [f'{d["name"]} ({d["sim"]}% match)' for d in similar_duplicates[:3]]
        score += add_check('Similar credential in other accounts', False,
            f'Very similar credential found in: {", ".join(names)}', 20)
    else:
        add_check('No similar cross-user credentials', True)

    # ── CHECK 6: File content duplicate detection ──
    if file_path:
        file_hash = get_file_hash(file_path)
        if file_hash:
            for other in all_creds:
                other_hash = get_file_hash(other['file_path'] or '')
                if other_hash and other_hash == file_hash:
                    score += add_check('Identical file in another account', False,
                        f'Same file already uploaded by {other["owner_name"]}', 50)
                    break
            else:
                add_check('File is unique', True, 'No identical file found in other accounts')
        add_check('File uploaded', True, f'Certificate document provided')
    else:
        score += add_check('No file uploaded', False,
            'No certificate document attached — cannot verify', 15)

    # ── CHECK 7: User upload velocity ──
    if user_creds:
        # Count credentials uploaded in last 24 hours
        yesterday = (datetime.now() - timedelta(hours=24)).isoformat()
        recent    = [c for c in user_creds if c['created_at'] > yesterday]
        if len(recent) >= 10:
            score += add_check('High upload velocity', False,
                f'User uploaded {len(recent)} credentials in last 24 hours — unusual pattern', 25)
        elif len(recent) >= 5:
            score += add_check('Moderate upload velocity', False,
                f'User uploaded {len(recent)} credentials in last 24 hours — worth checking', 10)
        else:
            add_check('Upload velocity normal', True,
                f'{len(recent)} credential(s) uploaded in last 24 hours')

        # Check if this user already has the same credential (self-duplicate)
        self_dups = [c for c in user_creds
                     if c['title'].lower() == title.lower()
                     and c['provider'].lower() == provider.lower()]
        if self_dups:
            score += add_check('Self-duplicate credential', False,
                f'User already has credential: "{self_dups[0]["title"]}"', 30)
        else:
            add_check('No self-duplicate', True)
    else:
        add_check('Upload velocity', True, 'First credential from this user')

    # ── CHECK 8: Title and provider not empty/generic ──
    generic_providers = ['company', 'organization', 'institute', 'university',
                         'college', 'academy', 'school', 'abc', 'xyz', 'test', 'unknown']
    if any(gp == provider.lower().strip() for gp in generic_providers):
        score += add_check('Generic provider name', False,
            f'Provider "{provider}" appears to be a placeholder name', 25)
    elif len(provider.strip()) < 3:
        score += add_check('Provider name too short', False,
            f'Provider name "{provider}" is too short to be legitimate', 20)
    else:
        add_check('Provider name looks legitimate', True)

    if len(title.strip()) < 5:
        score += add_check('Title too short', False,
            f'Credential title "{title}" is too short', 15)
    else:
        add_check('Title length acceptable', True)

    # ── CHECK 9: AI Document Analysis (FREE — Tesseract OCR + optional Groq LLaMA3) ──
    ai_result = {'ai_available': False, 'ai_risk_score': 0, 'ai_flags': [],
                 'ai_summary': 'AI analysis not run', 'is_certificate': None, 'method': 'none'}

    if file_path:
        ai_result = ai_analyze_certificate(
            file_path,
            claimed_title    = title,
            claimed_provider = provider,
            claimed_name     = cred['owner_name'],
            claimed_date     = issue_dt
        )
        if ai_result.get('ai_available'):
            ai_score   = ai_result.get('ai_risk_score', 0)
            method     = ai_result.get('method', 'ocr_only')
            lbl        = 'OCR+Groq AI' if 'groq' in method else 'OCR'

            if ai_score >= 76:   ai_weight = 60
            elif ai_score >= 56: ai_weight = 40
            elif ai_score >= 36: ai_weight = 20
            else:                ai_weight = 0

            if ai_result.get('is_certificate') == False:
                score += add_check(f'{lbl}: Not a certificate document', False,
                    ai_result.get('ai_summary','Document does not appear to be a certificate'), ai_weight)
            elif ai_weight > 0:
                score += add_check(f'{lbl}: Document flagged as suspicious', False,
                    ai_result.get('ai_summary','Issues detected in document'), ai_weight)
            else:
                add_check(f'{lbl}: Document looks like a certificate ✓', True,
                    ai_result.get('ai_summary','Verified as certificate'))

            if ai_result.get('name_found') == False:
                score += add_check(f'{lbl}: Learner name not found in document', False,
                    f'Name "{cred["owner_name"]}" not found in extracted text', 25)
            elif ai_result.get('name_found') == True:
                add_check(f'{lbl}: Learner name found ✓', True)

            if ai_result.get('provider_found') == False:
                score += add_check(f'{lbl}: Provider not found in document', False,
                    f'"{provider}" not found in extracted text', 20)
            elif ai_result.get('provider_found') == True:
                add_check(f'{lbl}: Provider name found ✓', True)

            if ai_result.get('title_match') == False:
                score += add_check(f'{lbl}: Title not matching document', False,
                    f'Claimed title "{title}" keywords not found in document', 20)
            elif ai_result.get('title_match') == True:
                add_check(f'{lbl}: Title matches document ✓', True)

            if ai_result.get('date_found') == False:
                score += add_check(f'{lbl}: No date in document', False,
                    'No date found in extracted text', 10)
        else:
            add_check('AI Document Analysis', True,
                ai_result.get('ai_summary', 'Install pytesseract & pymupdf to enable OCR'))
    else:
        score += add_check('No file uploaded', False,
            'No certificate document attached — cannot verify content', 20)

    # ── Compute final risk level ──
    score = min(score, 100)  # cap at 100

    if score == 0:
        risk_level     = 'low'
        recommendation = 'approve'
        summary        = 'No issues detected. Credential appears legitimate.'
    elif score <= 20:
        risk_level     = 'low'
        recommendation = 'approve'
        summary        = 'Minor concerns. Credential likely legitimate.'
    elif score <= 45:
        risk_level     = 'medium'
        recommendation = 'review'
        summary        = 'Some concerns found. Manual review recommended before approving.'
    elif score <= 70:
        risk_level     = 'high'
        recommendation = 'review'
        summary        = 'Multiple red flags detected. Careful manual review required.'
    else:
        risk_level     = 'critical'
        recommendation = 'reject'
        summary        = 'Critical fraud indicators detected. Rejection recommended.'

    return {
        'risk_score':     score,
        'risk_level':     risk_level,
        'recommendation': recommendation,
        'summary':        summary,
        'flags':          flags,
        'checks':         checks,
        'total_checks':   len(checks),
        'passed_checks':  sum(1 for c in checks if c['passed']),
        'failed_checks':  sum(1 for c in checks if not c['passed']),
        'ai_analysis':    ai_result,
        'credential':     {
            'title':    title,
            'provider': provider,
            'owner':    cred['owner_name'],
            'issued':   issue_dt,
        }
    }

# ════════════════════════════════════════
#  ZERO-KNOWLEDGE PROOF (ZKP) SYSTEM
#  Hash-based Commitment Scheme
# ════════════════════════════════════════

def zkp_generate_commitment(credential_data: dict, salt: str = None) -> tuple:
    """
    Generate a ZKP commitment for a credential.

    The commitment = SHA-256(all credential data + salt)
    The salt is known only to the system (stored encrypted).
    The commitment is stored publicly.

    This allows proving claims about a credential WITHOUT
    revealing the full credential details.

    Returns: (commitment_hex, salt_hex)
    """
    import secrets
    if not salt:
        salt = secrets.token_hex(32)

    # Combine all credential fields into a canonical string
    canonical = '|'.join([
        str(credential_data.get('credential_id','')),
        str(credential_data.get('user_id','')),
        str(credential_data.get('title','')).lower().strip(),
        str(credential_data.get('provider','')).lower().strip(),
        str(credential_data.get('issue_date','')),
        str(credential_data.get('skill_category','')).lower().strip(),
        str(credential_data.get('level','')).lower().strip(),
        str(credential_data.get('status','')),
    ])

    commitment = hashlib.sha256((canonical + salt).encode()).hexdigest()
    return commitment, salt


def zkp_generate_proof(claim_type: str, claim_value: str,
                       credential_data: dict, salt: str) -> tuple:
    """
    Generate a ZKP proof for a specific claim about a credential.

    Claim types:
      - 'has_credential'    : prove learner has a specific credential title
      - 'has_category'      : prove learner has a credential in a skill category
      - 'has_level'         : prove learner has an Advanced/Intermediate/Beginner credential
      - 'is_verified'       : prove a credential is verified without revealing details
      - 'issued_after'      : prove credential was issued after a certain date
      - 'provider_is'       : prove credential was issued by a specific provider

    The proof proves the claim is TRUE without revealing other credential details.

    Returns: (proof_hash, proof_token)
      - proof_hash  : The cryptographic proof (public)
      - proof_token : Short shareable token for easy verification
    """
    import secrets

    # Validate the claim against actual credential data
    claim_valid = False
    if claim_type == 'has_credential':
        claim_valid = claim_value.lower().strip() in credential_data.get('title','').lower()
    elif claim_type == 'has_category':
        claim_valid = claim_value.lower().strip() == credential_data.get('skill_category','').lower().strip()
    elif claim_type == 'has_level':
        claim_valid = claim_value.lower().strip() == credential_data.get('level','').lower().strip()
    elif claim_type == 'is_verified':
        claim_valid = credential_data.get('status','') == 'verified'
    elif claim_type == 'issued_after':
        claim_valid = credential_data.get('issue_date','') >= claim_value
    elif claim_type == 'provider_is':
        claim_valid = claim_value.lower().strip() in credential_data.get('provider','').lower()

    if not claim_valid:
        return None, None  # Cannot generate proof for false claim

    # Proof = SHA-256(claim_type + claim_value + salt + commitment_nonce)
    nonce = secrets.token_hex(16)
    proof_input = f"{claim_type}|{claim_value}|{salt}|{nonce}"
    proof_hash  = hashlib.sha256(proof_input.encode()).hexdigest()

    # Short human-readable token: first 16 chars of proof
    proof_token = proof_hash[:16].upper()

    return proof_hash, proof_token


def zkp_verify_proof(proof_token: str) -> dict:
    """
    Verify a ZKP proof token.
    Returns claim details if valid, or error if not found/expired.
    Does NOT reveal credential details — only confirms the claim.
    """
    with get_db() as conn:
        proof = conn.execute(
            """SELECT zp.*, c.status, u.name as learner_name
               FROM zkp_proofs zp
               JOIN credentials c ON zp.credential_id = c.credential_id
               JOIN users u ON zp.user_id = u.user_id
               WHERE zp.proof_token = ?""",
            (proof_token.upper().strip(),)
        ).fetchone()

    if not proof:
        return {'valid': False, 'error': 'Proof token not found'}

    # Check expiry
    if proof['expires_at'] and proof['expires_at'] < datetime.now().isoformat():
        return {'valid': False, 'error': 'Proof has expired'}

    # Return only the claim — NOT the full credential details
    claim_labels = {
        'has_credential': 'Has credential',
        'has_category':   'Has skill in category',
        'has_level':      'Has credential at level',
        'is_verified':    'Credential is verified',
        'issued_after':   'Credential issued after',
        'provider_is':    'Credential issued by',
    }

    return {
        'valid':       True,
        'claim_type':  proof['claim_type'],
        'claim_label': claim_labels.get(proof['claim_type'], proof['claim_type']),
        'claim_value': proof['claim_value'],
        'learner':     proof['learner_name'],
        'issued_at':   proof['created_at'][:10],
        'expires_at':  proof['expires_at'][:10] if proof['expires_at'] else 'Never',
        'proof_token': proof['proof_token'],
        # Privacy: we do NOT reveal credential_id, title, provider, date etc.
    }

# ── SMS OTP via Twilio ──
def send_sms_otp(phone, code, purpose='phone_verify'):
    """Send OTP via Twilio SMS. Returns (ok, error)."""
    if not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_FROM:
        print(f"\n{'='*50}")
        print(f"  📱 DEV MODE — SMS OTP")
        print(f"  Phone   : {phone}")
        print(f"  OTP Code: {code}")
        print(f"{'='*50}\n")
        logger.info(f'[DEV MODE] SMS OTP for {phone}: {code}')
        return True, None
    try:
        import urllib.request, urllib.parse, base64
        msg  = f'Your MicroCred verification code is: {code}. Valid for 10 minutes. Do not share this.'
        data = urllib.parse.urlencode({'From': TWILIO_FROM, 'To': phone, 'Body': msg}).encode()
        url  = f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json'
        creds = base64.b64encode(f'{TWILIO_SID}:{TWILIO_TOKEN}'.encode()).decode()
        req  = urllib.request.Request(url, data=data,
               headers={'Authorization': f'Basic {creds}', 'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            if resp.get('status') in ('queued','sent','delivered'):
                logger.info(f'SMS OTP sent to {phone}')
                return True, None
            return False, resp.get('message','Unknown error')
    except Exception as e:
        logger.error(f'Twilio SMS error: {e}')
        return False, str(e)

def store_phone_otp(phone, code, purpose='phone_verify'):
    """Store phone OTP, invalidate old ones."""
    expires = (datetime.now() + timedelta(minutes=10)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE phone_verifications SET used=1 WHERE phone=? AND purpose=? AND used=0",
                     (phone, purpose))
        conn.execute("INSERT INTO phone_verifications VALUES(?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), phone, code, purpose, expires, 0, datetime.now().isoformat()))

def verify_phone_otp(phone, code, purpose='phone_verify'):
    """Verify phone OTP. Returns True/False."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM phone_verifications WHERE phone=? AND code=? AND purpose=?
               AND used=0 AND expires_at > ? ORDER BY created_at DESC LIMIT 1""",
            (phone, code, purpose, datetime.now().isoformat())
        ).fetchone()
        if row:
            conn.execute("UPDATE phone_verifications SET used=1 WHERE ver_id=?", (row['ver_id'],))
            return True
    return False

def normalize_phone(phone):
    """Strip spaces/dashes, ensure starts with +."""
    phone = ''.join(c for c in phone if c.isdigit() or c == '+')
    if phone and not phone.startswith('+'):
        phone = '+91' + phone  # Default India country code
    return phone

def normalize_name(name):
    """Lowercase, strip extra spaces for comparison."""
    return ' '.join(name.lower().split())

def name_similarity(a, b):
    """Simple similarity score 0-1 between two names."""
    a_parts = set(normalize_name(a).split())
    b_parts = set(normalize_name(b).split())
    if not a_parts or not b_parts: return 0
    # Check if all parts of shorter name are in longer name
    shorter = a_parts if len(a_parts) <= len(b_parts) else b_parts
    longer  = a_parts if len(a_parts) >  len(b_parts) else b_parts
    contained = len(shorter & longer) / len(shorter)
    # Also compute Jaccard for symmetric similarity
    jaccard = len(a_parts & b_parts) / len(a_parts | b_parts)
    return max(jaccard, contained * 0.8)

def check_duplicate_account(name, email, phone):
    """
    Check for duplicate/fraudulent account attempts.
    Returns (is_duplicate: bool, reason: str)
    """
    phone_norm = normalize_phone(phone) if phone else ''
    with get_db() as conn:
        # 1. Same phone number
        if phone_norm:
            existing = conn.execute(
                "SELECT name, email FROM users WHERE phone=?", (phone_norm,)
            ).fetchone()
            if existing:
                return True, f'A MicroCred account already exists with this mobile number. Please log in or delete your existing account before creating a new one.'

        # 2. Same email (already handled by UNIQUE constraint, but give better message)
        existing = conn.execute(
            "SELECT user_id FROM users WHERE LOWER(email)=?", (email.lower(),)
        ).fetchone()
        if existing:
            return True, 'This email address is already registered. Please log in instead.'

        # 3. Highly similar name + same email domain
        email_domain = email.split('@')[-1].lower() if '@' in email else ''
        all_users = conn.execute(
            "SELECT name, email FROM users WHERE role='learner'"
        ).fetchall()
        for u in all_users:
            sim = name_similarity(name, u['name'])
            u_domain = u['email'].split('@')[-1].lower() if '@' in u['email'] else ''
            # Same name (>85% similar) AND same email domain = very likely same person
            if sim >= 0.85 and email_domain == u_domain and email_domain not in ('gmail.com','yahoo.com','hotmail.com','outlook.com'):
                return True, f'An account with very similar personal details already exists. If this is your account, please log in. To create a new account, delete your existing one first.'

        # 4. Exact same name + same phone prefix (first 8 digits)
        if phone_norm and len(phone_norm) >= 8:
            prefix = phone_norm[:9]  # +91XXXXX
            similar_phones = conn.execute(
                "SELECT name FROM users WHERE phone LIKE ?", (prefix + '%',)
            ).fetchall()
            for u in similar_phones:
                if name_similarity(name, u['name']) >= 0.9:
                    return True, 'An account with the same name and a very similar phone number already exists. You cannot create duplicate accounts.'

    return False, ''

def send_notification_email(to_email, subject, message, link=''):
    """Send a simple notification email. Silent fail if email not configured."""
    if not MAIL_USER or not MAIL_PASS:
        logger.info(f'[DEV] Notification email to {to_email}: {subject}')
        return
    try:
        base_url = os.environ.get('BASE_URL', 'http://localhost:5000')
        html = f"""<!DOCTYPE html><html><body style="background:#05050a;font-family:'Segoe UI',sans-serif;margin:0;padding:0;">
        <div style="max-width:480px;margin:40px auto;background:#11111e;border:1px solid rgba(255,255,255,0.07);border-radius:16px;overflow:hidden;">
          <div style="background:linear-gradient(135deg,#1a1a2e,#11111e);padding:28px 36px 20px;border-bottom:1px solid rgba(255,255,255,0.07);">
            <div style="font-size:1.3rem;font-weight:900;color:#f8f6f2;">MicroCred</div>
          </div>
          <div style="padding:28px 36px;">
            <p style="color:rgba(248,246,242,0.7);font-size:0.9rem;line-height:1.7;">{message}</p>
            {'<a href="' + base_url + link + '" style="display:inline-block;margin-top:1rem;background:#f0a430;color:#1a0800;padding:0.6rem 1.5rem;border-radius:8px;text-decoration:none;font-weight:700;">View Now</a>' if link else ''}
          </div>
          <div style="padding:16px 36px;border-top:1px solid rgba(255,255,255,0.05);"><p style="color:rgba(248,246,242,0.2);font-size:0.7rem;margin:0;">MicroCred Platform Notification</p></div>
        </div></body></html>"""
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = MAIL_FROM
        msg['To']      = to_email
        msg.attach(MIMEText(html, 'html'))
        with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=8) as server:
            server.ehlo(); server.starttls()
            server.login(MAIL_USER, MAIL_PASS)
            server.sendmail(MAIL_USER, to_email, msg.as_string())
    except Exception as e:
        logger.error(f'Notification email failed to {to_email}: {e}')

def check_expiring():
    """Send expiry warning emails for credentials expiring in 30 days (fix #13)."""
    try:
        soon = (date.today() + timedelta(days=30)).isoformat()
        today_str = date.today().isoformat()
        with get_db() as conn:
            expiring = conn.execute("""
                SELECT c.*, u.email, u.name FROM credentials c
                JOIN users u ON c.user_id=u.user_id
                WHERE c.expiry_date IS NOT NULL
                AND c.expiry_date BETWEEN ? AND ?
                AND c.status='verified'
            """, (today_str, soon)).fetchall()
            for cred in expiring:
                # Check if we already notified (avoid duplicate emails)
                already = conn.execute(
                    "SELECT notif_id FROM notifications WHERE user_id=? AND message LIKE ? AND created_at > ?",
                    (cred['user_id'], f'%{cred["title"]}%expir%',
                     (datetime.now()-timedelta(days=7)).isoformat())
                ).fetchone()
                if not already:
                    days_left = (date.fromisoformat(cred['expiry_date']) - date.today()).days
                    conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                        (str(uuid.uuid4()), cred['user_id'],
                         f"⚠️ Your credential \"{cred['title']}\" expires in {days_left} days!",
                         f"/credential/{cred['credential_id']}", 0, datetime.now().isoformat()))
                    send_notification_email(
                        cred['email'],
                        f'Credential Expiring Soon — {cred["title"]}',
                        f'Your credential "{cred["title"]}" issued by {cred["provider"]} will expire in {days_left} days on {cred["expiry_date"]}. Consider renewing it.',
                        f'/credential/{cred["credential_id"]}'
                    )
    except Exception as e:
        logger.error(f'check_expiring error: {e}')

def generate_otp():
    """Generate a 6-digit OTP code."""
    return ''.join(random.choices(string.digits, k=6))

def store_otp(email, code, purpose='register'):
    """Save OTP to DB, replacing any existing unused one for this email."""
    expires = (datetime.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()
    with get_db() as conn:
        # Invalidate old OTPs for this email+purpose
        conn.execute("UPDATE otp_codes SET used=1 WHERE email=? AND purpose=? AND used=0",
                     (email, purpose))
        conn.execute("INSERT INTO otp_codes VALUES(?,?,?,?,?,?,?)",
                     (str(uuid.uuid4()), email, code, purpose, expires, 0, datetime.now().isoformat()))

def verify_otp(email, code, purpose='register'):
    """Check OTP is valid, unexpired, and unused. Returns True/False."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM otp_codes WHERE email=? AND code=? AND purpose=?
               AND used=0 AND expires_at > ? ORDER BY created_at DESC LIMIT 1""",
            (email, code, purpose, datetime.now().isoformat())
        ).fetchone()
        if row:
            conn.execute("UPDATE otp_codes SET used=1 WHERE otp_id=?", (row['otp_id'],))
            return True
    return False

def send_otp_email(to_email, otp_code, purpose='register'):
    """Send OTP via email. Returns (success: bool, error: str)."""
    if not MAIL_USER or not MAIL_PASS:
        # Dev mode: print OTP clearly to terminal — impossible to miss
        print(f"\n{'='*50}")
        print(f"  📧 DEV MODE — EMAIL OTP")
        print(f"  Email   : {to_email}")
        print(f"  OTP Code: {otp_code}")
        print(f"  Purpose : {purpose}")
        print(f"{'='*50}\n")
        logger.info(f'[DEV MODE] OTP for {to_email}: {otp_code} (purpose: {purpose})')
        return True, None

    subject_map = {
        'register': 'Your MicroCred Verification Code',
        'reset':    'MicroCred Password Reset Code',
    }
    subject = subject_map.get(purpose, 'Your MicroCred OTP')

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <body style="margin:0;padding:0;background:#05050a;font-family:'Segoe UI',sans-serif;">
      <div style="max-width:480px;margin:40px auto;background:#11111e;border:1px solid rgba(255,255,255,0.07);border-radius:16px;overflow:hidden;">
        <div style="background:linear-gradient(135deg,#1a1a2e,#11111e);padding:32px 40px 24px;border-bottom:1px solid rgba(255,255,255,0.07);">
          <div style="font-size:1.5rem;font-weight:900;color:#f8f6f2;letter-spacing:-0.03em;">MicroCred</div>
          <div style="font-size:0.85rem;color:rgba(248,246,242,0.4);margin-top:4px;">Unified Skill Profiling</div>
        </div>
        <div style="padding:32px 40px;">
          <h2 style="color:#f8f6f2;font-size:1.2rem;margin:0 0 12px;font-weight:600;">
            {'Verify your email address' if purpose=='register' else 'Reset your password'}
          </h2>
          <p style="color:rgba(248,246,242,0.55);font-size:0.9rem;line-height:1.7;margin:0 0 28px;">
            {'Use the code below to complete your registration. It expires in ' + str(OTP_EXPIRY_MINUTES) + ' minutes.' if purpose=='register'
             else 'Use the code below to reset your password. It expires in ' + str(OTP_EXPIRY_MINUTES) + ' minutes.'}
          </p>
          <div style="background:#1e1e32;border:1px solid rgba(240,164,48,0.2);border-radius:12px;padding:24px;text-align:center;margin-bottom:28px;">
            <div style="font-size:2.5rem;font-weight:900;letter-spacing:0.2em;color:#f0a430;">{otp_code}</div>
            <div style="font-size:0.75rem;color:rgba(248,246,242,0.3);margin-top:8px;">Expires in {OTP_EXPIRY_MINUTES} minutes</div>
          </div>
          <p style="color:rgba(248,246,242,0.3);font-size:0.78rem;line-height:1.6;margin:0;">
            If you didn't request this, you can safely ignore this email.
            Never share this code with anyone.
          </p>
        </div>
        <div style="padding:20px 40px;border-top:1px solid rgba(255,255,255,0.05);">
          <p style="color:rgba(248,246,242,0.2);font-size:0.72rem;margin:0;">© 2024 MicroCred Platform</p>
        </div>
      </div>
    </body>
    </html>
    """

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = MAIL_FROM
        msg['To']      = to_email
        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(MAIL_USER, MAIL_PASS)
            server.sendmail(MAIL_USER, to_email, msg.as_string())

        logger.info(f'OTP email sent to {to_email} (purpose: {purpose})')
        return True, None
    except smtplib.SMTPAuthenticationError:
        logger.error('Email auth failed — check MAIL_USER and MAIL_PASS in .env')
        return False, 'Email authentication failed. Check your MAIL_USER and MAIL_PASS.'
    except smtplib.SMTPException as e:
        logger.error(f'SMTP error: {e}')
        return False, f'Could not send email: {str(e)}'
    except Exception as e:
        logger.error(f'Email send error: {e}')
        return False, 'Email service unavailable. Please try again.'

def add_notif(user_id, message, link=''):
    with get_db() as conn:
        conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()),user_id,message,link,0,datetime.now().isoformat()))

def log_action(admin_id, action, target_id='', details=''):
    with get_db() as conn:
        conn.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()),admin_id,action,target_id,details,datetime.now().isoformat()))

def get_unread(uid):
    with get_db() as conn:
        r = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",(uid,)).fetchone()
    return r[0] if r else 0

TRANSLATIONS = {
    'en':{'dashboard':'Dashboard','credentials':'Credentials','add_credential':'Add Credential',
          'verified':'Verified','unverified':'Pending','profile':'Profile','logout':'Logout',
          'search':'Search','find_talent':'Find Talent','pathways':'Pathways',
          'notifications':'Notifications','settings':'Settings'},
    'hi':{'dashboard':'डैशबोर्ड','credentials':'प्रमाणपत्र','add_credential':'जोड़ें',
          'verified':'सत्यापित','unverified':'लंबित','profile':'प्रोफ़ाइल','logout':'लॉग आउट',
          'search':'खोजें','find_talent':'प्रतिभा खोजें','pathways':'पाथवे',
          'notifications':'सूचनाएं','settings':'सेटिंग्स'},
    'kn':{'dashboard':'ಡ್ಯಾಶ್‌ಬೋರ್ಡ್','credentials':'ಪ್ರಮಾಣಪತ್ರಗಳು','add_credential':'ಸೇರಿಸಿ',
          'verified':'ಪರಿಶೀಲಿಸಲಾಗಿದೆ','unverified':'ಬಾಕಿ','profile':'ಪ್ರೊಫೈಲ್','logout':'ಲಾಗ್ ಔಟ್',
          'search':'ಹುಡುಕಿ','find_talent':'ಪ್ರತಿಭೆ ಹುಡುಕಿ','pathways':'ಮಾರ್ಗಗಳು',
          'notifications':'ಅಧಿಸೂಚನೆಗಳು','settings':'ಸೆಟ್ಟಿಂಗ್‌ಗಳು'},
    'ta':{'dashboard':'டாஷ்போர்டு','credentials':'சான்றிதழ்கள்','add_credential':'சேர்க்க',
          'verified':'சரிபார்க்கப்பட்டது','unverified':'நிலுவையில்','profile':'சுயவிவரம்','logout':'வெளியேறு',
          'search':'தேடு','find_talent':'திறமை தேடு','pathways':'பாதைகள்',
          'notifications':'அறிவிப்புகள்','settings':'அமைப்புகள்'},
    'de':{'dashboard':'Dashboard','credentials':'Zertifikate','add_credential':'Hinzufügen',
          'verified':'Verifiziert','unverified':'Ausstehend','profile':'Profil','logout':'Abmelden',
          'search':'Suchen','find_talent':'Talente finden','pathways':'Lernpfade',
          'notifications':'Benachrichtigungen','settings':'Einstellungen'},
}

@app.context_processor
def inject_globals():
    unread=0; lang='en'; t=TRANSLATIONS['en']; inbox_unread=0
    if 'user_id' in session:
        unread=get_unread(session['user_id'])
        try:
            with get_db() as conn:
                u=conn.execute("SELECT lang FROM users WHERE user_id=?",(session['user_id'],)).fetchone()
                if u:
                    lang=u['lang'] if u['lang'] in TRANSLATIONS else 'en'
                    t=TRANSLATIONS[lang]
                inbox_unread=conn.execute("SELECT COUNT(*) FROM messages WHERE receiver_id=? AND is_read=0",(session['user_id'],)).fetchone()[0]
        except: pass
    return dict(unread_count=unread,lang=lang,t=t,translations=TRANSLATIONS,dark_mode=1,inbox_unread=inbox_unread)

@app.route('/')
def index():
    if 'user_id' in session: return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method=='POST':
        name     = request.form['name'].strip()
        email    = request.form['email'].strip().lower()
        password = request.form['password']
        role     = request.form.get('role','learner')
        phone    = normalize_phone(request.form.get('phone','').strip())

        # Validate phone provided
        if not phone or len(phone) < 10:
            flash('A valid mobile number is required to create an account.','error')
            return render_template('register.html')

        # ── Duplicate account detection ──
        is_dup, dup_reason = check_duplicate_account(name, email, phone)
        if is_dup:
            flash(dup_reason, 'error')
            return render_template('register.html', show_duplicate_warning=True,
                                   dup_name=name, dup_email=email)

        # Send phone OTP (step 1 of 2-step verification)
        phone_otp = generate_otp()
        store_phone_otp(phone, phone_otp, purpose='register')
        sms_ok, sms_err = send_sms_otp(phone, phone_otp, purpose='register')

        # Store pending registration in session
        session['pending_reg'] = {
            'name': name, 'email': email,
            'password': generate_password_hash(password),
            'role': role, 'phone': phone,
            'phone_verified': False,
            'step': 'phone'   # Track which OTP step we're on
        }

        if sms_ok:
            flash(f'A 6-digit verification code was sent to {phone}. Enter it below.','info')
        else:
            logger.warning(f'SMS not sent ({sms_err}). OTP for {phone}: {phone_otp}')
            flash('SMS service not configured. Check terminal for your code (dev mode).','info')

        return redirect(url_for('verify_phone_page'))

    return render_template('register.html')


@app.route('/verify-phone', methods=['GET','POST'])
def verify_phone_page():
    """Step 1: Verify mobile number with SMS OTP."""
    pending = session.get('pending_reg')
    if not pending or pending.get('step') != 'phone':
        return redirect(url_for('register'))

    if request.method=='POST':
        entered  = request.form.get('otp','').strip()
        rate_key = get_rate_key('phone_otp')

        if not rate_limit(rate_key, max_attempts=5, window_seconds=600):
            flash('Too many attempts. Please wait 10 minutes or request a new code.','error')
            return render_template('verify_phone.html', phone=pending['phone'])

        if verify_phone_otp(pending['phone'], entered, purpose='register'):
            # Phone verified — now send email OTP (step 2)
            session['pending_reg']['phone_verified'] = True
            session['pending_reg']['step'] = 'email'
            session.modified = True

            email_otp = generate_otp()
            store_otp(pending['email'], email_otp, purpose='register')
            ok, err = send_otp_email(pending['email'], email_otp, purpose='register')

            if ok:
                flash(f'Phone verified! Now enter the email code sent to {pending["email"]}.','success')
            else:
                logger.warning(f'Email not sent. OTP for {pending["email"]}: {email_otp}')
                flash('Phone verified! Check terminal for email OTP (dev mode).','info')

            return redirect(url_for('verify_otp_page'))
        else:
            flash('Invalid or expired code. Please try again.','error')

    return render_template('verify_phone.html', phone=pending['phone'])


@app.route('/resend-phone-otp', methods=['POST'])
def resend_phone_otp():
    pending = session.get('pending_reg')
    if not pending:
        return jsonify({'error': 'No pending registration'}), 400
    code = generate_otp()
    store_phone_otp(pending['phone'], code, purpose='register')
    ok, err = send_sms_otp(pending['phone'], code, purpose='register')
    if ok:
        return jsonify({'ok': True, 'message': f'New code sent to {pending["phone"]}'})
    return jsonify({'error': err or 'Failed to send SMS'}), 500


@app.route('/verify-otp', methods=['GET','POST'])
def verify_otp_page():
    """Step 2: Verify email with email OTP."""
    pending = session.get('pending_reg')
    if not pending:
        return redirect(url_for('register'))
    # Must have completed phone step first
    if not pending.get('phone_verified'):
        return redirect(url_for('verify_phone_page'))

    if request.method=='POST':
        entered  = request.form.get('otp','').strip()
        rate_key = get_rate_key('otp')

        if not rate_limit(rate_key, max_attempts=5, window_seconds=600):
            flash('Too many incorrect attempts. Please wait 10 minutes or request a new code.','error')
            return render_template('verify_otp.html', email=pending['email'])

        if verify_otp(pending['email'], entered, purpose='register'):
            uid = str(uuid.uuid4())
            try:
                with get_db() as conn:
                    conn.execute(
                        """INSERT INTO users(user_id,name,email,password,role,bio,headline,
                           location,linkedin,github,website,avatar_color,lang,dark_mode,
                           created_at,phone,phone_verified)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (uid, pending['name'], pending['email'], pending['password'],
                         pending['role'],'','','','','','','#f0a430','en',0,
                         datetime.now().isoformat(), pending['phone'], 1))
                session.pop('pending_reg', None)
                logger.info(f'New user fully verified & registered: {pending["email"]} ({pending["role"]})')
                flash('✅ Phone and email verified! Your account is ready. Please login.','success')
                return redirect(url_for('login'))
            except sqlite3.IntegrityError:
                session.pop('pending_reg', None)
                flash('Email already registered.','error')
                return redirect(url_for('register'))
        else:
            flash('Invalid or expired code. Please try again or request a new one.','error')

    return render_template('verify_otp.html', email=pending['email'])


@app.route('/resend-otp', methods=['POST'])
def resend_otp():
    pending = session.get('pending_reg')
    if not pending:
        return jsonify({'error': 'No pending registration'}), 400
    otp = generate_otp()
    store_otp(pending['email'], otp, purpose='register')
    ok, err = send_otp_email(pending['email'], otp, purpose='register')
    if ok:
        return jsonify({'ok': True, 'message': f'New code sent to {pending["email"]}'})
    return jsonify({'error': err}), 500

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        email    = request.form['email'].strip().lower()
        raw_pw   = request.form['password']
        rate_key = get_rate_key('login')

        # Rate limit: 10 attempts per 5 minutes per IP (fix #7)
        if not rate_limit(rate_key, max_attempts=10, window_seconds=300):
            flash('Too many login attempts. Please wait 5 minutes and try again.','error')
            return render_template('login.html')

        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
        if user and check_password_hash(user['password'], raw_pw):
            if user['suspended']:
                reason = user['suspend_reason'] or 'Contact support for more information.'
                flash(f'Your account has been suspended. Reason: {reason}','error')
                return render_template('login.html')
            session.update({'user_id':user['user_id'],'name':user['name'],'role':user['role'],'email':user['email']})
            logger.info(f'User logged in: {email} ({user["role"]})')
            check_expiring()
            return redirect(url_for('dashboard'))
        logger.warning(f'Failed login attempt for: {email}')
        flash('Invalid email or password.','error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    role=session.get('role')
    if role=='employer': return redirect(url_for('employer_portal'))
    if role=='admin': return redirect(url_for('admin_panel'))
    search=request.args.get('q','')
    with get_db() as conn:
        if search:
            creds=conn.execute("SELECT * FROM credentials WHERE user_id=? AND (title LIKE ? OR provider LIKE ? OR skill_category LIKE ?) ORDER BY created_at DESC",
                (session['user_id'],f'%{search}%',f'%{search}%',f'%{search}%')).fetchall()
        else:
            creds=conn.execute("SELECT * FROM credentials WHERE user_id=? ORDER BY created_at DESC",(session['user_id'],)).fetchall()
    today=date.today().isoformat()
    verified=sum(1 for c in creds if c['status']=='verified')
    categories=list(set(c['skill_category'] for c in creds if c['skill_category']))
    expiring=[c for c in creds if c['expiry_date'] and c['expiry_date']<=date.today().replace(day=date.today().day).isoformat()]
    return render_template('dashboard.html',credentials=creds,verified=verified,categories=categories,search=search,today=today)

@app.route('/upload', methods=['GET','POST'])
@login_required
def upload_credential():
    if request.method=='POST':
        title          = request.form['title'].strip()
        provider       = request.form['provider'].strip()
        issue_date     = request.form['issue_date']
        expiry_date    = request.form.get('expiry_date') or None
        description    = request.form.get('description','')
        skill_category = request.form.get('skill_category','General')
        level          = request.form.get('level','Beginner')
        credential_url = request.form.get('credential_url','')

        # Duplicate detection (fix #14)
        with get_db() as conn:
            dup = conn.execute(
                "SELECT credential_id FROM credentials WHERE user_id=? AND LOWER(title)=? AND LOWER(provider)=?",
                (session['user_id'], title.lower(), provider.lower())
            ).fetchone()
        if dup:
            flash('You already have a credential with this title and provider. Please check your existing credentials.','error')
            return render_template('upload.html')

        cid = str(uuid.uuid4()); file_path = ''
        if 'file' in request.files:
            f = request.files['file']
            if f and f.filename:
                if not allowed_file(f.filename):
                    flash('Invalid file type. Only PDF, PNG, JPG are allowed.','error')
                    return render_template('upload.html')
                # File content scanning — magic bytes check (fix #9)
                if not validate_file_content(f):
                    flash('File content does not match its extension. Please upload a genuine PDF or image file.','error')
                    return render_template('upload.html')
                fname = secure_filename(f"{cid}_{f.filename}")
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
                file_path = fname

        hash_val = generate_hash(cid, session['user_id'], title, provider)

        # Generate ZKP commitment for this credential
        cred_data = {
            'credential_id': cid, 'user_id': session['user_id'],
            'title': title, 'provider': provider, 'issue_date': issue_date,
            'skill_category': skill_category, 'level': level, 'status': 'unverified'
        }
        zkp_commitment, zkp_salt = zkp_generate_commitment(cred_data)

        with get_db() as conn:
            conn.execute("INSERT INTO credentials VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, session['user_id'], title, provider, issue_date, expiry_date, description,
                 skill_category, level, file_path, credential_url, 'unverified', hash_val,
                 datetime.now().isoformat(), zkp_commitment, zkp_salt))
            conn.execute("INSERT INTO verification VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()),cid,hash_val,'unverified',datetime.now().isoformat()))
            # Notify all admins about new credential pending review (fix #19)
            admins = conn.execute("SELECT user_id,email FROM users WHERE role='admin'").fetchall()
            for admin in admins:
                conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), admin['user_id'],
                     f"📋 New credential pending review: \"{title}\" by {session['name']}",
                     f"/admin/review/{cid}", 0, datetime.now().isoformat()))
                # Email admin (fix #19)
                send_notification_email(
                    admin['email'],
                    'New Credential Pending Review — MicroCred',
                    f"A new credential \"{title}\" by {session['name']} has been submitted for review.",
                    f"/admin/review/{cid}"
                )
        flash('Credential uploaded successfully! It will be reviewed by an admin shortly.','success')
        return redirect(url_for('dashboard'))
    return render_template('upload.html')

@app.route('/credential/<cid>')
@login_required
def view_credential(cid):
    with get_db() as conn:
        cred=conn.execute("SELECT c.*,u.name as owner_name FROM credentials c JOIN users u ON c.user_id=u.user_id WHERE c.credential_id=?",(cid,)).fetchone()
        verif=conn.execute("SELECT * FROM verification WHERE credential_id=?",(cid,)).fetchone()
    if not cred: flash('Not found.','error'); return redirect(url_for('dashboard'))
    today=date.today().isoformat()
    return render_template('credential_detail.html',credential=cred,verification=verif,today=today)

@app.route('/verify/<cid>', methods=['POST'])
@login_required
def verify_credential(cid):
    if session.get('role') not in ['admin','employer']:
        return jsonify({'error':'Unauthorized'}),403
    with get_db() as conn:
        cred=conn.execute("SELECT * FROM credentials WHERE credential_id=?",(cid,)).fetchone()
        if cred:
            conn.execute("UPDATE credentials SET status='verified' WHERE credential_id=?",(cid,))
            conn.execute("UPDATE verification SET verified_status='verified',verified_at=? WHERE credential_id=?",(datetime.now().isoformat(),cid))
            conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),cred['user_id'],f"🎉 Your credential \"{cred['title']}\" has been verified!",f"/credential/{cid}",0,datetime.now().isoformat()))
            conn.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),session['user_id'],'verify',cid,f"Verified: {cred['title']}",datetime.now().isoformat()))
    return jsonify({'status':'verified'})

@app.route('/credential/edit/<cid>', methods=['GET','POST'])
@login_required
def edit_credential(cid):
    with get_db() as conn:
        cred=conn.execute("SELECT * FROM credentials WHERE credential_id=? AND user_id=?",(cid,session['user_id'])).fetchone()
    if not cred: flash('Not found.','error'); return redirect(url_for('dashboard'))
    if request.method=='POST':
        with get_db() as conn:
            conn.execute("UPDATE credentials SET title=?,provider=?,issue_date=?,expiry_date=?,description=?,skill_category=?,level=?,credential_url=? WHERE credential_id=? AND user_id=?",
                (request.form['title'],request.form['provider'],request.form['issue_date'],
                 request.form.get('expiry_date') or None,request.form.get('description',''),
                 request.form.get('skill_category','General'),request.form.get('level','Beginner'),
                 request.form.get('credential_url',''),cid,session['user_id']))
        flash('Credential updated!','success'); return redirect(url_for('view_credential',cid=cid))
    return render_template('edit_credential.html',credential=cred)

@app.route('/credential/delete/<cid>', methods=['POST'])
@login_required
def delete_credential(cid):
    with get_db() as conn:
        cred=conn.execute("SELECT * FROM credentials WHERE credential_id=? AND user_id=?",(cid,session['user_id'])).fetchone()
        if cred:
            if cred['file_path']:
                fp=os.path.join(app.config['UPLOAD_FOLDER'],cred['file_path'])
                if os.path.exists(fp): os.remove(fp)
            conn.execute("DELETE FROM credentials WHERE credential_id=?",(cid,))
            conn.execute("DELETE FROM verification WHERE credential_id=?",(cid,))
            flash('Credential deleted.','success')
    return redirect(url_for('dashboard'))

@app.route('/contact/<learner_id>', methods=['POST'])
@login_required
def contact_learner(learner_id):
    if session.get('role') not in ['employer','admin']:
        return jsonify({'error':'Unauthorized'}),403
    data    = request.json or {}
    message = data.get('message','')
    subject = data.get('subject', f'Message from {session["name"]}')
    if not message: return jsonify({'error':'Message required'}),400
    with get_db() as conn:
        learner = conn.execute("SELECT * FROM users WHERE user_id=?",(learner_id,)).fetchone()
        if not learner: return jsonify({'error':'Learner not found'}),404
        mid = str(uuid.uuid4())
        conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
            (mid, session['user_id'], learner_id, subject, message, 0, None, datetime.now().isoformat()))
        # Record first contact only
        existing = conn.execute("SELECT * FROM contact_requests WHERE employer_id=? AND learner_id=?",
            (session['user_id'],learner_id)).fetchone()
        if not existing:
            conn.execute("INSERT INTO contact_requests VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),session['user_id'],learner_id,message,'pending',datetime.now().isoformat()))
        conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), learner_id,
             f"💬 {session['name']} sent you a message — check your inbox!",
             f"/inbox/{mid}", 0, datetime.now().isoformat()))
    return jsonify({'status':'sent', 'msg_id': mid})

@app.route('/compose', methods=['GET','POST'])
@login_required
def compose():
    """Any user can compose a new message."""
    if request.method == 'POST':
        receiver_id = request.form.get('receiver_id','')
        subject     = request.form.get('subject','').strip() or 'No subject'
        body        = request.form.get('body','').strip()
        if not receiver_id or not body:
            flash('Please fill in all fields.','error')
        else:
            with get_db() as conn:
                receiver = conn.execute("SELECT * FROM users WHERE user_id=?",(receiver_id,)).fetchone()
                if not receiver:
                    flash('Recipient not found.','error')
                else:
                    mid = str(uuid.uuid4())
                    conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                        (mid, session['user_id'], receiver_id, subject, body, 0, None, datetime.now().isoformat()))
                    conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                        (str(uuid.uuid4()), receiver_id,
                         f"💬 New message from {session['name']}",
                         f"/inbox/{mid}", 0, datetime.now().isoformat()))
                    flash('Message sent!','success')
                    return redirect(url_for('inbox'))
    with get_db() as conn:
        if session.get('role') == 'learner':
            # Learners can message anyone who has messaged them before
            contacts = conn.execute("""
                SELECT DISTINCT u.user_id, u.name, u.role, u.avatar_color, u.headline
                FROM users u
                JOIN messages m ON (m.sender_id=u.user_id AND m.receiver_id=?)
                               OR (m.receiver_id=u.user_id AND m.sender_id=?)
                WHERE u.user_id != ?
                ORDER BY u.name
            """,(session['user_id'],session['user_id'],session['user_id'])).fetchall()
        else:
            contacts = conn.execute(
                "SELECT user_id, name, role, avatar_color, headline FROM users WHERE role='learner' AND suspended=0 ORDER BY name"
            ).fetchall()
    return render_template('compose.html', contacts=contacts)

@app.route('/public/<uid>')
def public_profile(uid):
    with get_db() as conn:
        learner=conn.execute("SELECT * FROM users WHERE user_id=? AND role='learner'",(uid,)).fetchone()
        creds=conn.execute("SELECT * FROM credentials WHERE user_id=? ORDER BY created_at DESC",(uid,)).fetchall()
    if not learner: flash('Not found.','error'); return redirect(url_for('index'))
    verified=sum(1 for c in creds if c['status']=='verified')
    categories=list(set(c['skill_category'] for c in creds if c['skill_category']))
    return render_template('public_profile.html',learner=learner,credentials=creds,verified=verified,categories=categories)

@app.route('/admin')
@login_required
def admin_panel():
    if session.get('role')!='admin': flash('Access denied.','error'); return redirect(url_for('dashboard'))
    with get_db() as conn:
        users=conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        creds=conn.execute("SELECT c.*,u.name as owner FROM credentials c JOIN users u ON c.user_id=u.user_id ORDER BY c.created_at DESC").fetchall()
        logs=conn.execute("SELECT l.*,u.name as admin_name FROM activity_log l LEFT JOIN users u ON l.admin_id=u.user_id ORDER BY l.created_at DESC LIMIT 50").fetchall()
        reports=conn.execute("SELECT r.*,u.name as admin_name FROM reports r LEFT JOIN users u ON r.admin_id=u.user_id ORDER BY r.created_at DESC").fetchall()
        total_users=len(users); total_creds=len(creds)
        verified_creds=sum(1 for c in creds if c['status']=='verified')
        pending_creds=sum(1 for c in creds if c['status']=='unverified')
        suspended_users=sum(1 for u in users if u['suspended'])
        open_reports=sum(1 for r in reports if r['status']=='open')
    return render_template('admin.html',users=users,credentials=creds,logs=logs,reports=reports,
        total_users=total_users,total_creds=total_creds,verified_creds=verified_creds,
        pending_creds=pending_creds,suspended_users=suspended_users,open_reports=open_reports)

@app.route('/admin/users')
@login_required
def admin_users():
    if session.get('role')!='admin': flash('Access denied.','error'); return redirect(url_for('dashboard'))
    with get_db() as conn:
        users=conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return render_template('admin_users.html',users=users)

@app.route('/admin/review/<cid>')
@login_required
def admin_review(cid):
    if session.get('role')!='admin': flash('Access denied.','error'); return redirect(url_for('dashboard'))
    with get_db() as conn:
        cred  = conn.execute("SELECT c.*,u.name as owner_name,u.email as owner_email,u.headline,u.location FROM credentials c JOIN users u ON c.user_id=u.user_id WHERE c.credential_id=?",(cid,)).fetchone()
        verif = conn.execute("SELECT * FROM verification WHERE credential_id=?",(cid,)).fetchone()
    if not cred: flash('Not found.','error'); return redirect(url_for('admin_panel'))

    # Run automated fraud detection
    fraud_report = run_fraud_detection(cid)

    return render_template('admin_review.html', credential=cred, verification=verif,
                           fraud_report=fraud_report)

@app.route('/api/fraud_check/<cid>')
@login_required
def api_fraud_check(cid):
    """Re-run fraud detection on demand via API."""
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    report = run_fraud_detection(cid)
    return jsonify(report)

@app.route('/admin/reject/<cid>', methods=['POST'])
@login_required
def admin_reject(cid):
    if session.get('role')!='admin': return jsonify({'error':'Unauthorized'}),403
    reason=request.json.get('reason','No reason given')
    with get_db() as conn:
        cred=conn.execute("SELECT * FROM credentials WHERE credential_id=?",(cid,)).fetchone()
        if cred:
            conn.execute("UPDATE credentials SET status='rejected' WHERE credential_id=?",(cid,))
            conn.execute("UPDATE verification SET verified_status='rejected',verified_at=? WHERE credential_id=?",(datetime.now().isoformat(),cid))
            conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),cred['user_id'],f"❌ Your credential \"{cred['title']}\" was rejected. Reason: {reason}",f"/credential/{cid}",0,datetime.now().isoformat()))
            conn.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),session['user_id'],'reject',cid,f"Rejected: {cred['title']} — {reason}",datetime.now().isoformat()))
    return jsonify({'status':'rejected'})

@app.route('/admin/delete_user/<uid>', methods=['POST'])
@login_required
def admin_delete_user(uid):
    if session.get('role')!='admin': return jsonify({'error':'Unauthorized'}),403
    if uid==session['user_id']: return jsonify({'error':'Cannot delete yourself'}),400
    with get_db() as conn:
        user=conn.execute("SELECT name FROM users WHERE user_id=?",(uid,)).fetchone()
        if user:
            conn.execute("DELETE FROM credentials WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM notifications WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM pathways WHERE user_id=?",(uid,))
            conn.execute("DELETE FROM users WHERE user_id=?",(uid,))
            conn.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),session['user_id'],'delete_user',uid,f"Deleted user: {user['name']}",datetime.now().isoformat()))
    return jsonify({'status':'deleted'})

# ── NEW: Admin full user profile view ──
@app.route('/admin/user/<uid>')
@login_required
def admin_view_user(uid):
    if session.get('role')!='admin': flash('Access denied.','error'); return redirect(url_for('dashboard'))
    with get_db() as conn:
        user  = conn.execute("SELECT * FROM users WHERE user_id=?",(uid,)).fetchone()
        creds = conn.execute("SELECT * FROM credentials WHERE user_id=? ORDER BY created_at DESC",(uid,)).fetchall()
        logs  = conn.execute("""SELECT * FROM activity_log WHERE target_id=? OR target_id IN
                             (SELECT credential_id FROM credentials WHERE user_id=?)
                             ORDER BY created_at DESC""",(uid,uid)).fetchall()
        reports = conn.execute("SELECT * FROM reports WHERE target_id=? ORDER BY created_at DESC",(uid,)).fetchall()
    if not user: flash('User not found.','error'); return redirect(url_for('admin_panel'))
    verified = sum(1 for c in creds if c['status']=='verified')
    return render_template('admin_user_profile.html',
        user=user, credentials=creds, logs=logs,
        reports=reports, verified=verified)

# ── NEW: Suspend / Unsuspend user ──
@app.route('/admin/suspend/<uid>', methods=['POST'])
@login_required
def admin_suspend_user(uid):
    if session.get('role')!='admin': return jsonify({'error':'Unauthorized'}),403
    if uid==session['user_id']: return jsonify({'error':'Cannot suspend yourself'}),400
    data   = request.json or {}
    action = data.get('action','suspend')
    reason = data.get('reason','Suspicious activity')
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?",(uid,)).fetchone()
        if not user: return jsonify({'error':'User not found'}),404
        if action=='suspend':
            conn.execute("UPDATE users SET suspended=1, suspend_reason=? WHERE user_id=?",(reason,uid))
            conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),uid,f"⚠️ Your account has been suspended. Reason: {reason}","",0,datetime.now().isoformat()))
            conn.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),session['user_id'],'suspend',uid,f"Suspended: {user['name']} — {reason}",datetime.now().isoformat()))
            logger.info(f"Admin suspended user: {user['email']}")
        else:
            conn.execute("UPDATE users SET suspended=0, suspend_reason=NULL WHERE user_id=?",(uid,))
            conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),uid,"✅ Your account suspension has been lifted. You can now access all features.","",0,datetime.now().isoformat()))
            conn.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),session['user_id'],'unsuspend',uid,f"Unsuspended: {user['name']}",datetime.now().isoformat()))
            logger.info(f"Admin unsuspended user: {user['email']}")
    return jsonify({'status': action+'ed'})

# ── NEW: Admin delete a specific credential ──
@app.route('/admin/delete_credential/<cid>', methods=['POST'])
@login_required
def admin_delete_credential(cid):
    if session.get('role')!='admin': return jsonify({'error':'Unauthorized'}),403
    reason = (request.json or {}).get('reason','Removed by admin')
    with get_db() as conn:
        cred = conn.execute("SELECT * FROM credentials WHERE credential_id=?",(cid,)).fetchone()
        if cred:
            if cred['file_path']:
                fp = os.path.join(app.config['UPLOAD_FOLDER'], cred['file_path'])
                if os.path.exists(fp): os.remove(fp)
            conn.execute("DELETE FROM credentials WHERE credential_id=?",(cid,))
            conn.execute("DELETE FROM verification WHERE credential_id=?",(cid,))
            conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),cred['user_id'],
                 f"🗑️ Your credential \"{cred['title']}\" was removed by admin. Reason: {reason}",
                 "",0,datetime.now().isoformat()))
            conn.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()),session['user_id'],'delete_credential',cid,
                 f"Deleted credential: {cred['title']} — {reason}",datetime.now().isoformat()))
    return jsonify({'status':'deleted'})

# ── NEW: File a report on a user or credential ──
@app.route('/admin/report', methods=['POST'])
@login_required
def admin_report():
    if session.get('role')!='admin': return jsonify({'error':'Unauthorized'}),403
    data        = request.json or {}
    target_type = data.get('target_type','user')
    target_id   = data.get('target_id','')
    reason      = data.get('reason','')
    notes       = data.get('notes','')
    if not target_id or not reason:
        return jsonify({'error':'Target and reason required'}),400
    with get_db() as conn:
        conn.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()),session['user_id'],target_type,target_id,
             reason,notes,'open',datetime.now().isoformat()))
        conn.execute("INSERT INTO activity_log VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()),session['user_id'],'report',target_id,
             f"Reported {target_type}: {reason}",datetime.now().isoformat()))
    return jsonify({'status':'reported'})

# ── NEW: Resolve / close a report ──
@app.route('/admin/report/resolve/<rid>', methods=['POST'])
@login_required
def admin_resolve_report(rid):
    if session.get('role')!='admin': return jsonify({'error':'Unauthorized'}),403
    with get_db() as conn:
        conn.execute("UPDATE reports SET status='resolved' WHERE report_id=?",(rid,))
    return jsonify({'status':'resolved'})

# ════════════════════════════════════════
#  GOOGLE OAUTH LOGIN (fix #18)
# ════════════════════════════════════════
@app.route('/auth/google')
def google_login():
    if not GOOGLE_CLIENT_ID:
        flash('Google login is not configured on this server.','error')
        return redirect(url_for('login'))
    import urllib.parse
    params = {
        'client_id':     GOOGLE_CLIENT_ID,
        'redirect_uri':  url_for('google_callback', _external=True),
        'response_type': 'code',
        'scope':         'openid email profile',
        'access_type':   'offline',
        'prompt':        'select_account',
    }
    return redirect('https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode(params))

@app.route('/auth/google/callback')
def google_callback():
    import urllib.parse, urllib.request
    code = request.args.get('code')
    if not code:
        flash('Google login cancelled.','error')
        return redirect(url_for('login'))
    try:
        # Exchange code for token
        token_data = urllib.parse.urlencode({
            'code':          code,
            'client_id':     GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'redirect_uri':  url_for('google_callback', _external=True),
            'grant_type':    'authorization_code',
        }).encode()
        req = urllib.request.Request('https://oauth2.googleapis.com/token', data=token_data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_json = json.loads(resp.read())
        access_token = token_json.get('access_token')

        # Get user info
        req2 = urllib.request.Request(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {access_token}'}
        )
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            info = json.loads(resp2.read())

        google_id = info.get('id')
        email     = info.get('email','').lower()
        name      = info.get('name', email.split('@')[0])

        with get_db() as conn:
            # Check if user exists by google_id or email
            user = conn.execute(
                "SELECT * FROM users WHERE google_id=? OR email=?", (google_id, email)
            ).fetchone()

            if user:
                # Update google_id if not set
                if not user['google_id']:
                    conn.execute("UPDATE users SET google_id=? WHERE user_id=?", (google_id, user['user_id']))
                if user['suspended']:
                    flash(f'Your account has been suspended. Reason: {user["suspend_reason"] or "Contact support."}','error')
                    return redirect(url_for('login'))
                session.update({'user_id':user['user_id'],'name':user['name'],'role':user['role'],'email':user['email']})
                logger.info(f'Google login: {email}')
                check_expiring()
                return redirect(url_for('dashboard'))
            else:
                # Create new account via Google
                uid = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO users(user_id,name,email,password,role,bio,headline,location,
                       linkedin,github,website,avatar_color,lang,dark_mode,created_at,google_id)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uid, name, email, generate_password_hash(os.urandom(32).hex()),
                     'learner','','','','','','','#f0a430','en',0,
                     datetime.now().isoformat(), google_id)
                )
                session.update({'user_id':uid,'name':name,'role':'learner','email':email})
                logger.info(f'New user via Google: {email}')
                flash(f'Welcome to MicroCred, {name}! Your account has been created.','success')
                return redirect(url_for('dashboard'))
    except Exception as e:
        logger.error(f'Google OAuth error: {e}')
        flash('Google login failed. Please try again or use email/password.','error')
        return redirect(url_for('login'))

# ════════════════════════════════════════
#  EMPLOYER VERIFIED FILTER (fix #15)
# ════════════════════════════════════════
@app.route('/employer')
@login_required
def employer_portal():
    if session.get('role') not in ['employer','admin']:
        flash('Access denied.','error'); return redirect(url_for('dashboard'))
    q            = request.args.get('q','')
    cat          = request.args.get('category','')
    verified_only = request.args.get('verified_only','') == '1'
    min_creds    = request.args.get('min_creds','0')
    try: min_creds = max(0, int(min_creds))
    except: min_creds = 0

    with get_db() as conn:
        if verified_only:
            # Only learners with at least 1 verified credential
            if q:
                learners = conn.execute("""
                    SELECT DISTINCT u.* FROM users u
                    JOIN credentials c ON c.user_id=u.user_id
                    WHERE u.role='learner' AND c.status='verified'
                    AND (u.name LIKE ? OR u.headline LIKE ? OR c.title LIKE ? OR c.skill_category LIKE ?)
                    ORDER BY u.created_at DESC
                """, (f'%{q}%',f'%{q}%',f'%{q}%',f'%{q}%')).fetchall()
            elif cat:
                learners = conn.execute("""
                    SELECT DISTINCT u.* FROM users u
                    JOIN credentials c ON c.user_id=u.user_id
                    WHERE u.role='learner' AND c.status='verified' AND c.skill_category=?
                    ORDER BY u.created_at DESC
                """, (cat,)).fetchall()
            else:
                learners = conn.execute("""
                    SELECT DISTINCT u.* FROM users u
                    JOIN credentials c ON c.user_id=u.user_id
                    WHERE u.role='learner' AND c.status='verified'
                    ORDER BY u.created_at DESC
                """).fetchall()
        else:
            if q:
                learners = conn.execute("""
                    SELECT DISTINCT u.* FROM users u LEFT JOIN credentials c ON c.user_id=u.user_id
                    WHERE u.role='learner' AND (u.name LIKE ? OR u.headline LIKE ? OR c.title LIKE ? OR c.skill_category LIKE ?)
                    ORDER BY u.created_at DESC
                """, (f'%{q}%',f'%{q}%',f'%{q}%',f'%{q}%')).fetchall()
            elif cat:
                learners = conn.execute("""
                    SELECT DISTINCT u.* FROM users u JOIN credentials c ON c.user_id=u.user_id
                    WHERE u.role='learner' AND c.skill_category=? ORDER BY u.created_at DESC
                """, (cat,)).fetchall()
            else:
                learners = conn.execute("SELECT * FROM users WHERE role='learner' ORDER BY created_at DESC").fetchall()

        # Apply min_creds filter
        if min_creds > 0:
            filtered = []
            for l in learners:
                vc = conn.execute(
                    "SELECT COUNT(*) FROM credentials WHERE user_id=? AND status='verified'",
                    (l['user_id'],)
                ).fetchone()[0]
                if vc >= min_creds:
                    filtered.append(l)
            learners = filtered

        categories = conn.execute(
            "SELECT DISTINCT skill_category FROM credentials WHERE skill_category IS NOT NULL"
        ).fetchall()
        sent = conn.execute(
            "SELECT learner_id FROM contact_requests WHERE employer_id=?", (session['user_id'],)
        ).fetchall()
        sent_ids = [r['learner_id'] for r in sent]

    return render_template('employer.html', learners=learners, categories=categories,
                           query=q, sent_ids=sent_ids, verified_only=verified_only,
                           cat=cat, min_creds=min_creds)

# ════════════════════════════════════════
#  INBOX / MESSAGES
# ════════════════════════════════════════
@app.route('/inbox')
@login_required
def inbox():
    with get_db() as conn:
        # Show all conversations this user is part of (sent OR received)
        # Group by thread root (parent_id IS NULL)
        msgs = conn.execute("""
            SELECT m.*,
                   u.name as sender_name,
                   u.avatar_color as sender_color,
                   u.role as sender_role,
                   r.name as receiver_name,
                   r.avatar_color as receiver_color,
                   (SELECT COUNT(*) FROM messages rep WHERE rep.parent_id=m.msg_id) as reply_count,
                   (SELECT COUNT(*) FROM messages rep WHERE rep.parent_id=m.msg_id AND rep.receiver_id=? AND rep.is_read=0) as unread_replies
            FROM messages m
            JOIN users u ON m.sender_id=u.user_id
            JOIN users r ON m.receiver_id=r.user_id
            WHERE m.parent_id IS NULL
              AND (m.receiver_id=? OR m.sender_id=?)
            ORDER BY m.created_at DESC
        """,(session['user_id'], session['user_id'], session['user_id'])).fetchall()
        unread = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE receiver_id=? AND is_read=0",
            (session['user_id'],)
        ).fetchone()[0]
        # Mark received messages as read
        conn.execute("UPDATE messages SET is_read=1 WHERE receiver_id=? AND is_read=1",
                     (session['user_id'],))
    return render_template('inbox.html', messages=msgs, unread=unread)

@app.route('/inbox/<msg_id>')
@login_required
def view_message(msg_id):
    with get_db() as conn:
        msg  = conn.execute("""SELECT m.*,u.name as sender_name,u.avatar_color as sender_color,u.role as sender_role,u.headline as sender_headline
            FROM messages m JOIN users u ON m.sender_id=u.user_id WHERE m.msg_id=?""",(msg_id,)).fetchone()
        thread = conn.execute("""SELECT m.*,u.name as sender_name,u.avatar_color as sender_color
            FROM messages m JOIN users u ON m.sender_id=u.user_id
            WHERE m.parent_id=? ORDER BY m.created_at ASC""",(msg_id,)).fetchall()
    if not msg or (msg['receiver_id']!=session['user_id'] and msg['sender_id']!=session['user_id']):
        flash('Message not found.','error'); return redirect(url_for('inbox'))
    with get_db() as conn:
        conn.execute("UPDATE messages SET is_read=1 WHERE msg_id=?",(msg_id,))
    return render_template('view_message.html', msg=msg, thread=thread)

@app.route('/inbox/reply/<msg_id>', methods=['POST'])
@login_required
def reply_message(msg_id):
    body = request.form.get('body','').strip()
    if not body: flash('Reply cannot be empty.','error'); return redirect(url_for('view_message', msg_id=msg_id))
    with get_db() as conn:
        orig = conn.execute("SELECT * FROM messages WHERE msg_id=?",(msg_id,)).fetchone()
        if not orig: flash('Message not found.','error'); return redirect(url_for('inbox'))
        receiver_id = orig['sender_id'] if orig['receiver_id']==session['user_id'] else orig['receiver_id']
        rid = str(uuid.uuid4())
        conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
            (rid, session['user_id'], receiver_id,
             'Re: '+orig['subject'], body, 0, msg_id, datetime.now().isoformat()))
        conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), receiver_id,
             f"💬 {session['name']} replied to your message",
             f"/inbox/{msg_id}", 0, datetime.now().isoformat()))
    flash('Reply sent!','success')
    return redirect(url_for('view_message', msg_id=msg_id))

@app.route('/inbox/send', methods=['POST'])
@login_required
def send_message():
    receiver_id = request.form.get('receiver_id','')
    subject     = request.form.get('subject','').strip() or 'No subject'
    body        = request.form.get('body','').strip()
    if not receiver_id or not body:
        return jsonify({'error':'Missing fields'}), 400
    with get_db() as conn:
        mid = str(uuid.uuid4())
        conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
            (mid, session['user_id'], receiver_id, subject, body, 0, None, datetime.now().isoformat()))
        conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), receiver_id,
             f"💬 New message from {session['name']}",
             f"/inbox/{mid}", 0, datetime.now().isoformat()))
    return jsonify({'status':'sent', 'msg_id': mid})

# ════════════════════════════════════════
#  PROFILE VIEWS TRACKING
# ════════════════════════════════════════
@app.route('/learner/<uid>')
@login_required
def learner_profile(uid):
    with get_db() as conn:
        learner = conn.execute("SELECT * FROM users WHERE user_id=?",(uid,)).fetchone()
        creds   = conn.execute("SELECT * FROM credentials WHERE user_id=? ORDER BY created_at DESC",(uid,)).fetchall()
        # Track profile view (don't count self-views)
        if session['user_id'] != uid:
            conn.execute("INSERT INTO profile_views VALUES(?,?,?,?)",
                (str(uuid.uuid4()), session['user_id'], uid, datetime.now().isoformat()))
        # Weekly view count for learner
        week_ago = (datetime.now() - timedelta(days=7)).isoformat()
        view_count = conn.execute(
            "SELECT COUNT(DISTINCT viewer_id) FROM profile_views WHERE profile_id=? AND viewed_at>?",
            (uid, week_ago)).fetchone()[0]
    if not learner: flash('Not found.','error'); return redirect(url_for('employer_portal'))
    verified   = sum(1 for c in creds if c['status']=='verified')
    categories = list(set(c['skill_category'] for c in creds if c['skill_category']))
    return render_template('learner_profile.html', learner=learner, credentials=creds,
                           verified=verified, categories=categories, view_count=view_count)

@app.route('/api/profile_views')
@login_required
def api_profile_views():
    week_ago  = (datetime.now() - timedelta(days=7)).isoformat()
    month_ago = (datetime.now() - timedelta(days=30)).isoformat()
    with get_db() as conn:
        week  = conn.execute("SELECT COUNT(DISTINCT viewer_id) FROM profile_views WHERE profile_id=? AND viewed_at>?",(session['user_id'],week_ago)).fetchone()[0]
        month = conn.execute("SELECT COUNT(DISTINCT viewer_id) FROM profile_views WHERE profile_id=? AND viewed_at>?",(session['user_id'],month_ago)).fetchone()[0]
        total = conn.execute("SELECT COUNT(DISTINCT viewer_id) FROM profile_views WHERE profile_id=?",(session['user_id'],)).fetchone()[0]
        recent = conn.execute("""SELECT u.name,u.avatar_color,u.role,u.headline,pv.viewed_at
            FROM profile_views pv JOIN users u ON pv.viewer_id=u.user_id
            WHERE pv.profile_id=? ORDER BY pv.viewed_at DESC LIMIT 5""",(session['user_id'],)).fetchall()
    return jsonify({'week':week,'month':month,'total':total,
        'recent':[{'name':r['name'],'color':r['avatar_color'],'role':r['role'],'headline':r['headline'],'when':r['viewed_at'][:10]} for r in recent]})

# ════════════════════════════════════════
#  ADMIN CHARTS DATA
# ════════════════════════════════════════
@app.route('/api/admin_stats')
@login_required
def api_admin_stats():
    if session.get('role')!='admin': return jsonify({'error':'Unauthorized'}),403
    with get_db() as conn:
        # Registrations per month
        regs = conn.execute("SELECT substr(created_at,1,7) as m, COUNT(*) as c FROM users GROUP BY m ORDER BY m").fetchall()
        # Credentials per month
        creds_by_month = conn.execute("SELECT substr(created_at,1,7) as m, COUNT(*) as c FROM credentials GROUP BY m ORDER BY m").fetchall()
        # Credentials by category
        by_cat = conn.execute("SELECT skill_category, COUNT(*) as c FROM credentials GROUP BY skill_category").fetchall()
        # Verification rate
        total_creds = conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]
        verified    = conn.execute("SELECT COUNT(*) FROM credentials WHERE status='verified'").fetchone()[0]
    return jsonify({
        'registrations': [{'month':r['m'],'count':r['c']} for r in regs],
        'credentials_by_month': [{'month':r['m'],'count':r['c']} for r in creds_by_month],
        'by_category': [{'label':r['skill_category'] or 'General','value':r['c']} for r in by_cat],
        'verification_rate': round((verified/total_creds*100) if total_creds else 0, 1),
        'total_creds': total_creds, 'verified': verified,
    })

# ════════════════════════════════════════
#  BULK CSV UPLOAD
# ════════════════════════════════════════
@app.route('/upload/bulk', methods=['GET','POST'])
@login_required
def bulk_upload():
    if request.method=='POST':
        if 'csv_file' not in request.files:
            flash('No file selected.','error'); return redirect(url_for('bulk_upload'))
        f = request.files['csv_file']
        if not f.filename.endswith('.csv'):
            flash('Please upload a .csv file.','error'); return redirect(url_for('bulk_upload'))
        import csv, io
        content = f.read().decode('utf-8-sig')
        reader  = csv.DictReader(io.StringIO(content))
        added = 0; errors = []
        required = {'title','provider','issue_date'}
        for i, row in enumerate(reader, 1):
            row = {k.strip().lower(): v.strip() for k,v in row.items()}
            if not required.issubset(row.keys()):
                errors.append(f'Row {i}: missing required columns (title, provider, issue_date)'); continue
            if not row.get('title') or not row.get('provider') or not row.get('issue_date'):
                errors.append(f'Row {i}: empty required field'); continue
            cid = str(uuid.uuid4())
            hval = generate_hash(cid, session['user_id'], row['title'], row['provider'])
            cred_data = {
                'credential_id': cid, 'user_id': session['user_id'],
                'title': row['title'], 'provider': row['provider'],
                'issue_date': row['issue_date'],
                'skill_category': row.get('skill_category','General'),
                'level': row.get('level','Beginner'), 'status': 'unverified'
            }
            zkp_commitment, zkp_salt = zkp_generate_commitment(cred_data)
            try:
                with get_db() as conn:
                    conn.execute("INSERT INTO credentials VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, session['user_id'], row['title'], row['provider'],
                         row['issue_date'], row.get('expiry_date') or None,
                         row.get('description',''), row.get('skill_category','General'),
                         row.get('level','Beginner'), '', row.get('credential_url',''),
                         'unverified', hval, datetime.now().isoformat(),
                         zkp_commitment, zkp_salt))
                    conn.execute("INSERT INTO verification VALUES(?,?,?,?,?)",
                        (str(uuid.uuid4()),cid,hval,'unverified',datetime.now().isoformat()))
                added += 1
            except Exception as e:
                errors.append(f'Row {i}: {str(e)}')
        msg = f'{added} credential(s) uploaded successfully.'
        if errors: msg += f' {len(errors)} row(s) skipped.'
        flash(msg, 'success' if added else 'error')
        if errors:
            for e in errors[:3]: flash(e,'error')
        return redirect(url_for('dashboard'))
    return render_template('bulk_upload.html')

# ════════════════════════════════════════
#  CREDENTIAL CARD / SHARE IMAGE
# ════════════════════════════════════════
@app.route('/credential/card/<cid>')
@login_required
def credential_card(cid):
    with get_db() as conn:
        cred = conn.execute("SELECT c.*,u.name as owner_name FROM credentials c JOIN users u ON c.user_id=u.user_id WHERE c.credential_id=?",(cid,)).fetchone()
    if not cred: flash('Not found.','error'); return redirect(url_for('dashboard'))
    return render_template('credential_card.html', credential=cred)

# ════════════════════════════════════════
#  ZERO-KNOWLEDGE PROOF ROUTES
# ════════════════════════════════════════

@app.route('/credential/zkp/<cid>', methods=['GET','POST'])
@login_required
def zkp_generate(cid):
    """Learner generates a ZKP proof for a specific claim about their credential."""
    with get_db() as conn:
        cred = conn.execute(
            "SELECT * FROM credentials WHERE credential_id=? AND user_id=?",
            (cid, session['user_id'])
        ).fetchone()
        existing_proofs = conn.execute(
            "SELECT * FROM zkp_proofs WHERE credential_id=? ORDER BY created_at DESC LIMIT 10",
            (cid,)
        ).fetchall()

    if not cred:
        flash('Credential not found.','error')
        return redirect(url_for('dashboard'))

    if cred['status'] != 'verified':
        flash('You can only generate ZKP proofs for verified credentials.','error')
        return redirect(url_for('view_credential', cid=cid))

    if request.method == 'POST':
        claim_type  = request.form.get('claim_type','').strip()
        claim_value = request.form.get('claim_value','').strip()
        expiry_days = int(request.form.get('expiry_days', 30))

        # Support both form POST and JSON (AJAX)
        if request.is_json:
            data        = request.get_json()
            claim_type  = data.get('claim_type','').strip()
            claim_value = data.get('claim_value','').strip()
            expiry_days = int(data.get('expiry_days', 30))

        if not claim_type or not claim_value:
            if request.is_json:
                return jsonify({'error': 'Please select a claim type.'}), 400
            flash('Please select a claim type and value.','error')
            return render_template('zkp_generate.html', credential=cred, proofs=existing_proofs)

        cred_data = {
            'credential_id': cred['credential_id'],
            'user_id':       cred['user_id'],
            'title':         cred['title'],
            'provider':      cred['provider'],
            'issue_date':    cred['issue_date'],
            'skill_category':cred['skill_category'],
            'level':         cred['level'],
            'status':        cred['status'],
        }

        proof_hash, proof_token = zkp_generate_proof(
            claim_type, claim_value, cred_data, cred['zkp_salt']
        )

        if not proof_hash:
            msg = 'Cannot generate proof — the claim does not match this credential.'
            if request.is_json:
                return jsonify({'error': msg}), 400
            flash(msg,'error')
            return render_template('zkp_generate.html', credential=cred, proofs=existing_proofs)

        expires_at = (datetime.now() + timedelta(days=expiry_days)).isoformat()

        with get_db() as conn:
            conn.execute(
                """INSERT INTO zkp_proofs VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), cid, session['user_id'],
                 claim_type, claim_value, proof_hash, proof_token,
                 expires_at, datetime.now().isoformat())
            )

        logger.info(f'ZKP proof generated: {proof_token} for {session["email"]} claim={claim_type}:{claim_value}')

        # Return JSON for AJAX calls (no page reload)
        if request.is_json:
            return jsonify({
                'ok':          True,
                'proof_token': proof_token,
                'claim_type':  claim_type,
                'claim_value': claim_value,
                'expires_at':  expires_at[:10],
                'verify_url':  f'/zkp/verify?token={proof_token}'
            })

        # For regular form POST — pass new_token to template to show popup
        with get_db() as conn:
            existing_proofs = conn.execute(
                "SELECT * FROM zkp_proofs WHERE credential_id=? ORDER BY created_at DESC LIMIT 10",
                (cid,)
            ).fetchall()
        return render_template('zkp_generate.html', credential=cred,
                               proofs=existing_proofs, new_token=proof_token,
                               new_claim_type=claim_type, new_claim_value=claim_value)

    return render_template('zkp_generate.html', credential=cred, proofs=existing_proofs)


@app.route('/zkp/verify', methods=['GET','POST'])
def zkp_verify_page():
    """Public page — anyone can verify a ZKP proof token without seeing credential details."""
    result = None
    token  = request.args.get('token','') or request.form.get('token','')

    if token:
        result = zkp_verify_proof(token.strip().upper())

    return render_template('zkp_verify.html', result=result, token=token)


@app.route('/api/zkp/verify', methods=['POST'])
def api_zkp_verify():
    """API endpoint for ZKP proof verification."""
    token = (request.json or {}).get('token','').strip().upper()
    if not token:
        return jsonify({'valid': False, 'error': 'Token required'}), 400
    result = zkp_verify_proof(token)
    return jsonify(result)

# ════════════════════════════════════════
#  PROFILE COMPLETENESS (API)
# ════════════════════════════════════════
@app.route('/api/profile_completeness')
@login_required
def profile_completeness():
    with get_db() as conn:
        u = conn.execute("SELECT * FROM users WHERE user_id=?",(session['user_id'],)).fetchone()
        cred_count = conn.execute("SELECT COUNT(*) FROM credentials WHERE user_id=?",(session['user_id'],)).fetchone()[0]
    checks = [
        ('Name added',       bool(u['name'])),
        ('Bio written',      bool(u['bio'])),
        ('Headline added',   bool(u['headline'])),
        ('Location added',   bool(u['location'])),
        ('LinkedIn added',   bool(u['linkedin'])),
        ('1+ credentials',   cred_count >= 1),
        ('3+ credentials',   cred_count >= 3),
        ('GitHub/Website',   bool(u['github']) or bool(u['website'])),
    ]
    done  = sum(1 for _,v in checks if v)
    score = round(done / len(checks) * 100)
    return jsonify({'score': score, 'checks': [{'label':l,'done':v} for l,v in checks]})

@app.route('/profile', methods=['GET','POST'])
@login_required
def edit_profile():
    with get_db() as conn:
        user=conn.execute("SELECT * FROM users WHERE user_id=?",(session['user_id'],)).fetchone()
    if request.method=='POST':
        action=request.form.get('action','profile')
        if action=='password':
            if not check_password_hash(user['password'], request.form['current_password']):
                flash('Current password is incorrect.','error')
            else:
                new_pw=generate_password_hash(request.form['new_password'])
                with get_db() as conn:
                    conn.execute("UPDATE users SET password=? WHERE user_id=?",(new_pw,session['user_id']))
                flash('Password changed successfully!','success')
        else:
            name=request.form.get('name',user['name'])
            with get_db() as conn:
                conn.execute("UPDATE users SET name=?,bio=?,headline=?,location=?,linkedin=?,github=?,website=?,avatar_color=?,lang=?,dark_mode=? WHERE user_id=?",
                    (name,request.form.get('bio',''),request.form.get('headline',''),
                     request.form.get('location',''),request.form.get('linkedin',''),
                     request.form.get('github',''),request.form.get('website',''),
                     request.form.get('avatar_color','#6c63ff'),request.form.get('lang','en'),
                     1 if request.form.get('dark_mode') else 0,session['user_id']))
            session['name']=name
            flash('Profile updated!','success')
        return redirect(url_for('edit_profile'))
    return render_template('profile.html',user=user)

@app.route('/view-profile/<int:user_id>')
@login_required
def view_profile(user_id):
    current_user_id = session['user_id']
    
    with get_db() as conn:
        # Get user details
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            flash('User not found', 'error')
            return redirect(url_for('dashboard'))
        
        # Get connection status
        connection = conn.execute("""
            SELECT * FROM connections 
            WHERE (requester_id=? AND receiver_id=?) OR (requester_id=? AND receiver_id=?)
        """, (current_user_id, user_id, user_id, current_user_id)).fetchone()
        
        # Get user credentials
        credentials = conn.execute("""
            SELECT * FROM credentials WHERE user_id=? AND status='verified'
        """, (user_id,)).fetchall()
        
        # Get mutual connections (people both are connected with)
        mutual = conn.execute("""
            SELECT DISTINCT u.user_id, u.name, u.email, u.role, u.location
            FROM users u
            JOIN connections c1 ON u.user_id=c1.receiver_id OR u.user_id=c1.requester_id
            WHERE c1.status='accepted' AND (c1.requester_id=? OR c1.receiver_id=?)
            AND u.user_id IN (
                SELECT CASE WHEN c2.requester_id=? THEN c2.receiver_id ELSE c2.requester_id END
                FROM connections c2
                WHERE c2.status='accepted' AND (c2.requester_id=? OR c2.receiver_id=?)
            )
            AND u.user_id != ?
            LIMIT 5
        """, (user_id, user_id, current_user_id, current_user_id, current_user_id, current_user_id)).fetchall()
        
        # Get users from same region (nearest users)
        same_region = conn.execute("""
            SELECT * FROM users 
            WHERE location=? AND user_id != ? AND user_id != ?
            LIMIT 5
        """, (user['location'] if user['location'] else '', user_id, current_user_id)).fetchall()
    
    return render_template('view_profile.html', 
                         user=user, 
                         credentials=credentials,
                         connection=connection,
                         mutual=mutual,
                         same_region=same_region,
                         current_user_id=current_user_id)

@app.route('/search-users')
@login_required
def search_users():
    query = request.args.get('q', '').strip()
    location = request.args.get('location', '').strip()
    role = request.args.get('role', '').strip()
    current_user_id = session['user_id']
    
    with get_db() as conn:
        sql = "SELECT * FROM users WHERE user_id != ?"
        params = [current_user_id]
        
        if query:
            sql += " AND (name LIKE ? OR email LIKE ?)"
            params.extend([f'%{query}%', f'%{query}%'])
        
        if location:
            sql += " AND location LIKE ?"
            params.append(f'%{location}%')
        
        if role:
            sql += " AND role = ?"
            params.append(role)
        
        sql += " ORDER BY created_at DESC LIMIT 20"
        users = conn.execute(sql, params).fetchall()
        
        # Get connection status for each user
        users_list = []
        for u in users:
            user_dict = dict(u)
            conn_status = conn.execute("""
                SELECT status FROM connections 
                WHERE (requester_id=? AND receiver_id=?) OR (requester_id=? AND receiver_id=?)
            """, (current_user_id, u['user_id'], u['user_id'], current_user_id)).fetchone()
            user_dict['connection_status'] = conn_status['status'] if conn_status else None
            users_list.append(user_dict)
    
    return render_template('search_users.html', users=users_list, query=query, location=location, role=role)


@app.route('/forgot-password', methods=['GET','POST'])
def forgot_password():
    if request.method=='POST':
        email = request.form['email'].strip().lower()
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone()
        if user:
            otp = generate_otp()
            store_otp(email, otp, purpose='reset')
            ok, err = send_otp_email(email, otp, purpose='reset')
            session['reset_email'] = email
            if ok:
                flash(f'A 6-digit reset code was sent to {email}.','info')
            else:
                logger.warning(f'Reset email not sent ({err}). OTP for {email}: {otp}')
                flash('Email service not configured. Check the terminal for your reset code.','info')
            return redirect(url_for('reset_password'))
        else:
            flash('If that email is registered, a reset code has been sent.','info')
    return render_template('forgot_password.html')

@app.route('/reset-password', methods=['GET','POST'])
def reset_password():
    reset_email = session.get('reset_email')
    if not reset_email: return redirect(url_for('forgot_password'))
    if request.method=='POST':
        otp_code = request.form.get('otp','').strip()
        new_pw   = request.form.get('new_password','')
        if not otp_code or not new_pw:
            flash('Please fill in all fields.','error')
        elif verify_otp(reset_email, otp_code, purpose='reset'):
            hashed = generate_password_hash(new_pw)
            with get_db() as conn:
                conn.execute("UPDATE users SET password=? WHERE email=?",(hashed, reset_email))
            session.pop('reset_email', None)
            logger.info(f'Password reset for {reset_email}')
            flash('Password reset successfully! Please login.','success')
            return redirect(url_for('login'))
        else:
            flash('Invalid or expired code. Please try again.','error')
    return render_template('reset_password.html', email=reset_email)

@app.route('/notifications')
@login_required
def notifications():
    with get_db() as conn:
        notifs=conn.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50",(session['user_id'],)).fetchall()
        conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?",(session['user_id'],))
    return render_template('notifications.html',notifications=notifs)

@app.route('/pathways')
@login_required
def pathways():
    with get_db() as conn:
        my_pathways=conn.execute("SELECT * FROM pathways WHERE user_id=? ORDER BY created_at DESC",(session['user_id'],)).fetchall()
        creds=conn.execute("SELECT * FROM credentials WHERE user_id=?",(session['user_id'],)).fetchall()
    return render_template('pathways.html',pathways=my_pathways,credentials=creds)

@app.route('/pathways/create', methods=['POST'])
@login_required
def create_pathway():
    title=request.form.get('title',''); steps=request.form.getlist('steps[]')
    if not title: flash('Title required.','error'); return redirect(url_for('pathways'))
    with get_db() as conn:
        conn.execute("INSERT INTO pathways VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()),session['user_id'],title,request.form.get('description',''),json.dumps(steps),datetime.now().isoformat()))
    flash('Pathway created!','success'); return redirect(url_for('pathways'))

@app.route('/pathways/delete/<pid>', methods=['POST'])
@login_required
def delete_pathway(pid):
    with get_db() as conn:
        conn.execute("DELETE FROM pathways WHERE pathway_id=? AND user_id=?",(pid,session['user_id']))
    flash('Pathway deleted.','success'); return redirect(url_for('pathways'))

@app.route('/set_lang/<lang>')
@login_required
def set_lang(lang):
    if lang in TRANSLATIONS:
        with get_db() as conn:
            conn.execute("UPDATE users SET lang=? WHERE user_id=?",(lang,session['user_id']))
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/export/profile')
@login_required
def export_profile():
    with get_db() as conn:
        user=conn.execute("SELECT * FROM users WHERE user_id=?",(session['user_id'],)).fetchone()
        creds=conn.execute("SELECT * FROM credentials WHERE user_id=? ORDER BY created_at DESC",(session['user_id'],)).fetchall()
    verified=sum(1 for c in creds if c['status']=='verified')
    categories=list(set(c['skill_category'] for c in creds if c['skill_category']))
    return render_template('export_profile.html',user=user,credentials=creds,verified=verified,categories=categories,now=datetime.now().isoformat())

@app.route('/api/dashboard_stats')
@login_required
def dashboard_stats():
    with get_db() as conn:
        by_cat=conn.execute("SELECT skill_category,COUNT(*) as c FROM credentials WHERE user_id=? GROUP BY skill_category",(session['user_id'],)).fetchall()
        by_level=conn.execute("SELECT level,COUNT(*) as c FROM credentials WHERE user_id=? GROUP BY level",(session['user_id'],)).fetchall()
        by_month=conn.execute("SELECT substr(created_at,1,7) as month,COUNT(*) as c FROM credentials WHERE user_id=? GROUP BY month ORDER BY month",(session['user_id'],)).fetchall()
    return jsonify({
        'by_category':[{'label':r['skill_category'] or 'General','value':r['c']} for r in by_cat],
        'by_level':[{'label':r['level'] or 'Unknown','value':r['c']} for r in by_level],
        'by_month':[{'label':r['month'],'value':r['c']} for r in by_month],
    })

@app.route('/verify_hash', methods=['GET','POST'])
def verify_hash():
    if request.method == 'GET':
        return redirect(url_for('index'))
    h=request.json.get('hash','')
    with get_db() as conn:
        r=conn.execute("SELECT c.*,u.name FROM credentials c JOIN users u ON c.user_id=u.user_id WHERE c.hash_value=?",(h,)).fetchone()
    if r: return jsonify({'found':True,'title':r['title'],'provider':r['provider'],'owner':r['name'],'status':r['status']})
    return jsonify({'found':False})

@app.route('/setup', methods=['GET','POST'])
def setup():
    with get_db() as conn:
        existing=conn.execute("SELECT * FROM users WHERE role='admin'").fetchone()
    if existing: return redirect(url_for('login'))
    if request.method=='POST':
        name=request.form['name']; email=request.form['email']
        password=generate_password_hash(request.form['password'])
        uid=str(uuid.uuid4())
        with get_db() as conn:
            conn.execute("INSERT INTO users(user_id,name,email,password,role,bio,headline,location,linkedin,github,website,avatar_color,lang,dark_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid,name,email,password,'admin','','Admin','','','','','#0f172a','en',0,datetime.now().isoformat()))
        flash('Admin account created! Please login.','success')
        return redirect(url_for('login'))
    return render_template('setup.html')

@app.errorhandler(404)
def not_found(e):
    logger.warning(f'404: {request.path}')
    return render_template('error.html', code=404, message='Page not found'), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f'500: {e}')
    return render_template('error.html', code=500, message='Something went wrong on our end'), 500

@app.errorhandler(413)
def too_large(e):
    flash(f'File too large. Maximum size is {MAX_UPLOAD_MB}MB.', 'error')
    return redirect(url_for('upload_credential'))

# ════════════════════════════════════════
#  REAL-TIME MESSAGING — SOCKETIO EVENTS
# ════════════════════════════════════════
if SOCKETIO_AVAILABLE:

    @socketio.on('connect')
    def on_connect():
        user_id = session.get('user_id')
        if user_id:
            join_room(f'user_{user_id}')
            logger.info(f'SocketIO: {session.get("name")} connected')

    @socketio.on('disconnect')
    def on_disconnect():
        user_id = session.get('user_id')
        if user_id:
            leave_room(f'user_{user_id}')

    @socketio.on('join_conversation')
    def on_join_conversation(data):
        """User joins a conversation room to receive real-time messages."""
        conv_id = data.get('conv_id','')
        user_id = session.get('user_id')
        if conv_id and user_id:
            join_room(f'conv_{conv_id}')

    @socketio.on('send_message')
    def on_send_message(data):
        """
        Real-time message send event.
        data: {receiver_id, body, subject, conv_id (optional)}
        """
        user_id     = session.get('user_id')
        receiver_id = data.get('receiver_id','')
        body        = (data.get('body','') or '').strip()
        subject     = (data.get('subject','') or f'Message from {session.get("name","Someone")}').strip()
        parent_id   = data.get('parent_id', None)

        if not user_id or not receiver_id or not body:
            emit('message_error', {'error': 'Missing required fields'})
            return

        # Save to database
        mid       = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()

        try:
            with get_db() as conn:
                conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)",
                    (mid, user_id, receiver_id, subject, body, 0, parent_id, timestamp))
                conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                    (str(uuid.uuid4()), receiver_id,
                     f"💬 New message from {session.get('name','Someone')}",
                     f"/inbox/{parent_id or mid}", 0, timestamp))
                # Get sender info for UI
                sender = conn.execute("SELECT name, avatar_color, role FROM users WHERE user_id=?",
                    (user_id,)).fetchone()
        except Exception as e:
            logger.error(f'SocketIO message save error: {e}')
            emit('message_error', {'error': 'Failed to save message'})
            return

        # Build message payload
        msg_data = {
            'msg_id':      mid,
            'sender_id':   user_id,
            'receiver_id': receiver_id,
            'sender_name': sender['name'] if sender else 'Unknown',
            'sender_color':sender['avatar_color'] if sender else '#f0a430',
            'sender_role': sender['role'] if sender else 'learner',
            'subject':     subject,
            'body':        body,
            'parent_id':   parent_id,
            'created_at':  timestamp,
            'is_read':     0,
        }

        # Emit to sender (confirmation)
        emit('message_sent', msg_data)

        # Emit to receiver's personal room (real-time delivery)
        emit('new_message', msg_data, room=f'user_{receiver_id}')

        # Also emit to conversation room if they are both viewing it
        if parent_id:
            emit('new_reply', msg_data, room=f'conv_{parent_id}')
        else:
            emit('new_reply', msg_data, room=f'conv_{mid}')

        logger.info(f'SocketIO: message {mid} from {user_id} to {receiver_id}')

    @socketio.on('mark_read')
    def on_mark_read(data):
        """Mark a message as read in real-time."""
        msg_id  = data.get('msg_id','')
        user_id = session.get('user_id')
        if msg_id and user_id:
            with get_db() as conn:
                conn.execute("UPDATE messages SET is_read=1 WHERE msg_id=? AND receiver_id=?",
                    (msg_id, user_id))
            emit('message_read', {'msg_id': msg_id})

    @socketio.on('typing')
    def on_typing(data):
        """Broadcast typing indicator to the other person."""
        receiver_id = data.get('receiver_id','')
        user_id     = session.get('user_id')
        if receiver_id and user_id:
            emit('user_typing', {
                'sender_id':   user_id,
                'sender_name': session.get('name','Someone'),
            }, room=f'user_{receiver_id}')

    @socketio.on('stop_typing')
    def on_stop_typing(data):
        """Broadcast stop typing to the other person."""
        receiver_id = data.get('receiver_id','')
        user_id     = session.get('user_id')
        if receiver_id and user_id:
            emit('user_stop_typing', {'sender_id': user_id},
                 room=f'user_{receiver_id}')

# ════════════════════════════════════════
#  AUTOMATION JOBS (APScheduler)
# ════════════════════════════════════════

def job_check_expiring_credentials():
    """
    Job 1 — Daily at 8:00 AM
    Sends email + in-app notification to learners whose
    verified credentials expire within 30 days.
    """
    with app.app_context():
        try:
            soon      = (date.today() + timedelta(days=30)).isoformat()
            today_str = date.today().isoformat()
            with get_db() as conn:
                expiring = conn.execute("""
                    SELECT c.*, u.email, u.name, u.user_id as uid
                    FROM credentials c JOIN users u ON c.user_id=u.user_id
                    WHERE c.expiry_date IS NOT NULL
                    AND c.expiry_date BETWEEN ? AND ?
                    AND c.status='verified'
                """, (today_str, soon)).fetchall()

                notified = 0
                for cred in expiring:
                    # Check not already notified in last 7 days
                    already = conn.execute("""
                        SELECT notif_id FROM notifications
                        WHERE user_id=? AND message LIKE ? AND created_at > ?
                    """, (cred['uid'], f'%{cred["title"]}%expir%',
                          (datetime.now()-timedelta(days=7)).isoformat())).fetchone()

                    if not already:
                        days_left = (date.fromisoformat(cred['expiry_date']) - date.today()).days
                        conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                            (str(uuid.uuid4()), cred['uid'],
                             f"⚠️ Your credential \"{cred['title']}\" expires in {days_left} days!",
                             f"/credential/{cred['credential_id']}", 0,
                             datetime.now().isoformat()))
                        send_notification_email(
                            cred['email'],
                            f'Credential Expiring Soon — {cred["title"]}',
                            f'Your credential "{cred["title"]}" issued by {cred["provider"]} '
                            f'expires in {days_left} days on {cred["expiry_date"]}. '
                            f'Consider renewing it before it expires.',
                            f'/credential/{cred["credential_id"]}'
                        )
                        notified += 1

            logger.info(f'[SCHEDULER] Expiry check: {notified} reminder(s) sent')
        except Exception as e:
            logger.error(f'[SCHEDULER] job_check_expiring_credentials error: {e}')


def job_fraud_scan_all():
    """
    Job 2 — Daily at 2:00 AM
    Runs fraud detection on ALL unverified credentials
    and auto-flags critical ones for admin attention.
    """
    with app.app_context():
        try:
            with get_db() as conn:
                unverified = conn.execute(
                    "SELECT credential_id FROM credentials WHERE status='unverified'"
                ).fetchall()
                admins = conn.execute(
                    "SELECT user_id FROM users WHERE role='admin'"
                ).fetchall()

            flagged = 0
            for cred in unverified:
                report = run_fraud_detection(cred['credential_id'])
                if report.get('risk_level') in ['high', 'critical']:
                    with get_db() as conn:
                        # Notify all admins
                        for admin in admins:
                            # Avoid duplicate notifications
                            existing = conn.execute("""
                                SELECT notif_id FROM notifications
                                WHERE user_id=? AND message LIKE ? AND created_at > ?
                            """, (admin['user_id'],
                                  f'%fraud%{cred["credential_id"]}%',
                                  (datetime.now()-timedelta(days=1)).isoformat())).fetchone()
                            if not existing:
                                conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                                    (str(uuid.uuid4()), admin['user_id'],
                                     f"🚨 High fraud risk detected on credential (score: {report['risk_score']}/100)",
                                     f"/admin/review/{cred['credential_id']}",
                                     0, datetime.now().isoformat()))
                    flagged += 1

            logger.info(f'[SCHEDULER] Fraud scan: {len(unverified)} checked, {flagged} flagged')
        except Exception as e:
            logger.error(f'[SCHEDULER] job_fraud_scan_all error: {e}')


def job_cleanup_otps():
    """
    Job 3 — Every hour
    Deletes expired and used OTP codes from the database
    to keep it clean and fast.
    """
    with app.app_context():
        try:
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            with get_db() as conn:
                r1 = conn.execute(
                    "DELETE FROM otp_codes WHERE used=1 OR expires_at < ?", (cutoff,)
                )
                r2 = conn.execute(
                    "DELETE FROM phone_verifications WHERE used=1 OR expires_at < ?", (cutoff,)
                )
            logger.info(f'[SCHEDULER] OTP cleanup: {r1.rowcount + r2.rowcount} records removed')
        except Exception as e:
            logger.error(f'[SCHEDULER] job_cleanup_otps error: {e}')


def job_cleanup_zkp_proofs():
    """
    Job 4 — Daily at 3:00 AM
    Removes expired ZKP proof tokens from the database.
    """
    with app.app_context():
        try:
            now = datetime.now().isoformat()
            with get_db() as conn:
                r = conn.execute(
                    "DELETE FROM zkp_proofs WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
                )
            logger.info(f'[SCHEDULER] ZKP cleanup: {r.rowcount} expired proof(s) removed')
        except Exception as e:
            logger.error(f'[SCHEDULER] job_cleanup_zkp_proofs error: {e}')


def job_weekly_admin_report():
    """
    Job 5 — Every Monday at 9:00 AM
    Sends a weekly summary email to all admins:
    new users, new credentials, fraud flags, verification rate.
    """
    with app.app_context():
        try:
            week_ago = (datetime.now() - timedelta(days=7)).isoformat()
            with get_db() as conn:
                new_users   = conn.execute("SELECT COUNT(*) FROM users WHERE created_at > ?", (week_ago,)).fetchone()[0]
                new_creds   = conn.execute("SELECT COUNT(*) FROM credentials WHERE created_at > ?", (week_ago,)).fetchone()[0]
                verified    = conn.execute("SELECT COUNT(*) FROM credentials WHERE status='verified' AND created_at > ?", (week_ago,)).fetchone()[0]
                pending     = conn.execute("SELECT COUNT(*) FROM credentials WHERE status='unverified'").fetchone()[0]
                total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                total_creds = conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]
                open_reports= conn.execute("SELECT COUNT(*) FROM reports WHERE status='open'").fetchone()[0]
                admins      = conn.execute("SELECT email, name FROM users WHERE role='admin'").fetchall()

            report_msg = (
                f"Weekly MicroVault Report\n\n"
                f"This week:\n"
                f"• New users registered: {new_users}\n"
                f"• New credentials uploaded: {new_creds}\n"
                f"• Credentials verified this week: {verified}\n\n"
                f"Platform totals:\n"
                f"• Total users: {total_users}\n"
                f"• Total credentials: {total_creds}\n"
                f"• Pending review: {pending}\n"
                f"• Open reports: {open_reports}"
            )

            for admin in admins:
                send_notification_email(
                    admin['email'],
                    f'MicroVault Weekly Report — {date.today().strftime("%d %b %Y")}',
                    report_msg.replace('\n', '<br>'),
                    '/admin'
                )
            logger.info(f'[SCHEDULER] Weekly report sent to {len(admins)} admin(s)')
        except Exception as e:
            logger.error(f'[SCHEDULER] job_weekly_admin_report error: {e}')


def job_inactive_account_alerts():
    """
    Job 6 — Every Sunday at 10:00 AM
    Sends a gentle reminder to learners who have not
    logged in for 90 days and have unverified credentials.
    """
    with app.app_context():
        try:
            ninety_days_ago = (datetime.now() - timedelta(days=90)).isoformat()
            with get_db() as conn:
                # Get users created more than 90 days ago with no recent activity
                # We use created_at as proxy (no last_login column yet)
                inactive = conn.execute("""
                    SELECT u.user_id, u.name, u.email,
                           COUNT(c.credential_id) as unverified_count
                    FROM users u
                    LEFT JOIN credentials c ON c.user_id=u.user_id AND c.status='unverified'
                    WHERE u.role='learner'
                    AND u.created_at < ?
                    AND u.suspended = 0
                    GROUP BY u.user_id
                    HAVING unverified_count > 0
                """, (ninety_days_ago,)).fetchall()

            reminded = 0
            for user in inactive:
                send_notification_email(
                    user['email'],
                    'You have pending credentials on MicroVault',
                    f'Hi {user["name"]}, you have {user["unverified_count"]} credential(s) '
                    f'waiting to be reviewed on MicroVault. '
                    f'Log in to check their status and keep your profile up to date.',
                    '/dashboard'
                )
                reminded += 1

            logger.info(f'[SCHEDULER] Inactive alerts: {reminded} reminder(s) sent')
        except Exception as e:
            logger.error(f'[SCHEDULER] job_inactive_account_alerts error: {e}')


def job_duplicate_scan():
    """
    Job 7 — Daily at 1:00 AM
    Scans all users for suspicious duplicate accounts
    (same name + similar phone) and notifies admins.
    """
    with app.app_context():
        try:
            with get_db() as conn:
                users  = conn.execute("SELECT * FROM users WHERE role='learner' AND suspended=0").fetchall()
                admins = conn.execute("SELECT user_id FROM users WHERE role='admin'").fetchall()

            suspicious_pairs = []
            users_list = list(users)
            for i, u1 in enumerate(users_list):
                for u2 in users_list[i+1:]:
                    # Same phone
                    if u1['phone'] and u2['phone'] and u1['phone'] == u2['phone']:
                        suspicious_pairs.append((u1, u2, 'Same phone number'))
                    # Very similar name
                    elif name_similarity(u1['name'], u2['name']) >= 0.9:
                        suspicious_pairs.append((u1, u2, 'Very similar name'))

            if suspicious_pairs:
                with get_db() as conn:
                    for admin in admins:
                        conn.execute("INSERT INTO notifications VALUES(?,?,?,?,?,?)",
                            (str(uuid.uuid4()), admin['user_id'],
                             f"⚠️ Nightly scan: {len(suspicious_pairs)} suspicious duplicate account pair(s) detected",
                             '/admin', 0, datetime.now().isoformat()))

            logger.info(f'[SCHEDULER] Duplicate scan: {len(suspicious_pairs)} suspicious pair(s) found')
        except Exception as e:
            logger.error(f'[SCHEDULER] job_duplicate_scan error: {e}')


def job_auto_fraud_upload():
    """
    Job 8 — Runs immediately when called after upload.
    Auto-flags critical credentials without waiting for admin to open them.
    Called directly from upload route (not scheduled).
    """
    pass  # Called inline from upload route — see upload_credential()


def start_scheduler():
    """Initialize and start the APScheduler with all automation jobs."""
    if not SCHEDULER_AVAILABLE:
        logger.warning('[SCHEDULER] APScheduler not installed — automation disabled')
        return None

    scheduler = BackgroundScheduler(timezone='Asia/Kolkata')

    # Job 1: Expiry reminders — daily at 8:00 AM
    scheduler.add_job(job_check_expiring_credentials, CronTrigger(hour=8, minute=0),
                      id='expiry_check', name='Credential Expiry Reminders', replace_existing=True)

    # Job 2: Fraud scan all unverified — daily at 2:00 AM
    scheduler.add_job(job_fraud_scan_all, CronTrigger(hour=2, minute=0),
                      id='fraud_scan', name='Automated Fraud Scan', replace_existing=True)

    # Job 3: OTP cleanup — every hour
    scheduler.add_job(job_cleanup_otps, CronTrigger(minute=0),
                      id='otp_cleanup', name='OTP Database Cleanup', replace_existing=True)

    # Job 4: ZKP proof cleanup — daily at 3:00 AM
    scheduler.add_job(job_cleanup_zkp_proofs, CronTrigger(hour=3, minute=0),
                      id='zkp_cleanup', name='ZKP Proof Cleanup', replace_existing=True)

    # Job 5: Weekly admin report — Monday at 9:00 AM
    scheduler.add_job(job_weekly_admin_report, CronTrigger(day_of_week='mon', hour=9, minute=0),
                      id='weekly_report', name='Weekly Admin Report', replace_existing=True)

    # Job 6: Inactive account alerts — Sunday at 10:00 AM
    scheduler.add_job(job_inactive_account_alerts, CronTrigger(day_of_week='sun', hour=10, minute=0),
                      id='inactive_alerts', name='Inactive Account Alerts', replace_existing=True)

    # Job 7: Duplicate scan — daily at 1:00 AM
    scheduler.add_job(job_duplicate_scan, CronTrigger(hour=1, minute=0),
                      id='duplicate_scan', name='Duplicate Account Scan', replace_existing=True)

    scheduler.start()
    logger.info(f'[SCHEDULER] Started with {len(scheduler.get_jobs())} automation jobs')
    for job in scheduler.get_jobs():
        logger.info(f'[SCHEDULER]   ✓ {job.name}')
    return scheduler


# ── Admin route to view scheduler status ──
@app.route('/admin/scheduler')
@login_required
def admin_scheduler():
    """Admin page to view and manually trigger automation jobs."""
    if session.get('role') != 'admin':
        flash('Access denied.', 'error')
        return redirect(url_for('dashboard'))
    return render_template('admin_scheduler.html')


@app.route('/api/scheduler/status')
@login_required
def api_scheduler_status():
    """API to get scheduler status and job list."""
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    jobs = [
        {'id': 'expiry_check',    'name': 'Credential Expiry Reminders', 'schedule': 'Daily at 8:00 AM'},
        {'id': 'fraud_scan',      'name': 'Automated Fraud Scan',        'schedule': 'Daily at 2:00 AM'},
        {'id': 'otp_cleanup',     'name': 'OTP Database Cleanup',        'schedule': 'Every hour'},
        {'id': 'zkp_cleanup',     'name': 'ZKP Proof Cleanup',           'schedule': 'Daily at 3:00 AM'},
        {'id': 'weekly_report',   'name': 'Weekly Admin Report',         'schedule': 'Monday at 9:00 AM'},
        {'id': 'inactive_alerts', 'name': 'Inactive Account Alerts',     'schedule': 'Sunday at 10:00 AM'},
        {'id': 'duplicate_scan',  'name': 'Duplicate Account Scan',      'schedule': 'Daily at 1:00 AM'},
    ]

    if not SCHEDULER_AVAILABLE:
        return jsonify({
            'available': False,
            'message': 'APScheduler not installed. Run: pip install apscheduler',
            'jobs': jobs   # always return jobs list so UI shows them
        })

    return jsonify({'available': True, 'jobs': jobs})


@app.route('/api/scheduler/run/<job_id>', methods=['POST'])
@login_required
def api_run_job(job_id):
    """Manually trigger a specific automation job from admin panel."""
    if session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403

    job_map = {
        'expiry_check':    job_check_expiring_credentials,
        'fraud_scan':      job_fraud_scan_all,
        'otp_cleanup':     job_cleanup_otps,
        'zkp_cleanup':     job_cleanup_zkp_proofs,
        'weekly_report':   job_weekly_admin_report,
        'inactive_alerts': job_inactive_account_alerts,
        'duplicate_scan':  job_duplicate_scan,
    }

    job_fn = job_map.get(job_id)
    if not job_fn:
        return jsonify({'error': 'Unknown job'}), 404

    try:
        job_fn()
        logger.info(f'[SCHEDULER] Manual trigger: {job_id} by {session["email"]}')
        return jsonify({'ok': True, 'message': f'Job "{job_id}" completed successfully'})
    except Exception as e:
        logger.error(f'[SCHEDULER] Manual trigger error: {e}')
        return jsonify({'error': str(e)}), 500

# ── Connections Routes ──
@app.route('/connect/<int:user_id>', methods=['POST'])
@login_required
def send_connection_request(user_id):
    try:
        requester_id = session['user_id']
        if requester_id == user_id:
            return jsonify({'error': 'Cannot connect with yourself'}), 400
        
        with get_db() as conn:
            existing = conn.execute("SELECT * FROM connections WHERE (requester_id=? AND receiver_id=?) OR (requester_id=? AND receiver_id=?)", 
                                   (requester_id, user_id, user_id, requester_id)).fetchone()
            if existing:
                return jsonify({'error': 'Connection already exists'}), 400
            
            conn.execute("INSERT INTO connections (requester_id, receiver_id, status) VALUES (?, ?, 'pending')",
                        (requester_id, user_id))
            conn.commit()
        
        logger.info(f'[CONNECTION] {requester_id} requested connection with {user_id}')
        return jsonify({'ok': True, 'message': 'Connection request sent'}), 201
    except Exception as e:
        logger.error(f'[CONNECTION] Error: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/accept-connection/<int:connection_id>', methods=['POST'])
@login_required
def accept_connection(connection_id):
    try:
        user_id = session['user_id']
        with get_db() as conn:
            conn.execute("UPDATE connections SET status='accepted', updated_at=CURRENT_TIMESTAMP WHERE connection_id=? AND receiver_id=?",
                        (connection_id, user_id))
            conn.commit()
        logger.info(f'[CONNECTION] Connection {connection_id} accepted')
        return jsonify({'ok': True, 'message': 'Connection accepted'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/reject-connection/<int:connection_id>', methods=['POST'])
@login_required
def reject_connection(connection_id):
    try:
        user_id = session['user_id']
        with get_db() as conn:
            conn.execute("UPDATE connections SET status='rejected', updated_at=CURRENT_TIMESTAMP WHERE connection_id=? AND receiver_id=?",
                        (connection_id, user_id))
            conn.commit()
        logger.info(f'[CONNECTION] Connection {connection_id} rejected')
        return jsonify({'ok': True, 'message': 'Connection rejected'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/my-connections')
@login_required
def my_connections():
    user_id = session['user_id']
    with get_db() as conn:
        # Get accepted connections
        accepted = conn.execute("""
            SELECT c.connection_id, 
                   CASE WHEN c.requester_id=? THEN c.receiver_id ELSE c.requester_id END as connected_user_id,
                   u.name, u.email, u.role, c.created_at
            FROM connections c
            JOIN users u ON (c.requester_id=? AND u.user_id=c.receiver_id) OR (c.receiver_id=? AND u.user_id=c.requester_id)
            WHERE (c.requester_id=? OR c.receiver_id=?) AND c.status='accepted'
        """, (user_id, user_id, user_id, user_id, user_id)).fetchall()
        
        # Get pending requests
        pending = conn.execute("""
            SELECT c.connection_id, u.name, u.email, u.role, c.requester_id, c.created_at
            FROM connections c
            JOIN users u ON u.user_id=c.requester_id
            WHERE c.receiver_id=? AND c.status='pending'
        """, (user_id,)).fetchall()
    
    return render_template('connections.html', accepted=accepted, pending=pending)

# ── Helpline Routes ──
@app.route('/helpline')
@login_required
def helpline_page():
    user_id = session['user_id']
    with get_db() as conn:
        tickets = conn.execute("SELECT * FROM helpline WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    return render_template('helpline.html', tickets=tickets)

@app.route('/helpline/submit', methods=['POST'])
@login_required
def submit_helpline():
    try:
        user_id = session['user_id']
        subject = request.form.get('subject', '').strip()
        message = request.form.get('message', '').strip()
        category = request.form.get('category', 'general')
        priority = request.form.get('priority', 'normal')
        
        if not subject or not message:
            return jsonify({'error': 'Subject and message required'}), 400
        
        with get_db() as conn:
            conn.execute("INSERT INTO helpline (user_id, subject, message, category, priority) VALUES (?, ?, ?, ?, ?)",
                        (user_id, subject, message, category, priority))
            conn.commit()
        
        logger.info(f'[HELPLINE] Ticket submitted by {user_id}')
        flash('Your helpline ticket has been submitted. Our team will respond soon.', 'success')
        return redirect(url_for('helpline_page'))
    except Exception as e:
        logger.error(f'[HELPLINE] Error: {e}')
        flash('Error submitting ticket', 'error')
        return redirect(url_for('helpline_page'))

@app.route('/admin/helpline')
@login_required
def admin_helpline():
    if session.get('role') != 'admin':
        return redirect(url_for('dashboard'))
    
    with get_db() as conn:
        tickets = conn.execute("""
            SELECT h.*, u.name, u.email FROM helpline h
            JOIN users u ON h.user_id=u.user_id
            ORDER BY h.created_at DESC
        """).fetchall()
    
    return render_template('admin_helpline.html', tickets=tickets)

@app.route('/admin/helpline/<int:ticket_id>/respond', methods=['POST'])
@login_required
def respond_helpline(ticket_id):
    if session.get('role') != 'admin':
        return jsonify({'error': 'Access denied'}), 403
    
    try:
        response = request.form.get('response', '').strip()
        if not response:
            return jsonify({'error': 'Response required'}), 400
        
        with get_db() as conn:
            conn.execute("UPDATE helpline SET admin_response=?, status='resolved', updated_at=CURRENT_TIMESTAMP WHERE ticket_id=?",
                        (response, ticket_id))
            conn.commit()
        
        logger.info(f'[HELPLINE] Admin responded to ticket {ticket_id}')
        return jsonify({'ok': True, 'message': 'Response sent'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── Register OCR Routes ──
if OCR_AVAILABLE:
    logger.info('🔍 Registering OCR certificate verification routes...')
    register_ocr_routes(app)
    logger.info('✅ OCR routes registered successfully')
else:
    logger.warning('⚠️ OCR routes not registered (dependencies missing)')

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    init_db()
    migrate_db()
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    port  = int(os.environ.get('PORT', 5000))
    scheduler = start_scheduler()
    logger.info(f'Starting MicroVault on port {port} (debug={debug})')
    if SOCKETIO_AVAILABLE and socketio:
        logger.info('Real-time messaging enabled via SocketIO')
        socketio.run(app, debug=debug, port=port, host='0.0.0.0',
                     allow_unsafe_werkzeug=True)
    else:
        logger.warning('Running WITHOUT real-time messaging (install flask-socketio)')
        app.run(debug=debug, port=port)
