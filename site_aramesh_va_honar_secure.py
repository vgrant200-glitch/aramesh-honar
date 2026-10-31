# -*- coding: utf-8 -*-
"""
آرامش و هنر
اجرا:  python app.py
اطلاعات ورود مدیر از متغیرهای محیطی ADMIN_USERNAME و ADMIN_PASSWORD خوانده می‌شود.
"""
import csv
import io
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, Response, abort, redirect, render_template_string,
    request, session, url_for
)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

# ----------------------------------------------------------------------
# تنظیمات
# ----------------------------------------------------------------------
LEGACY_DB = "/storage/emulated/0/appointments.db"
DB_NAME = os.environ.get("DB_PATH") or LEGACY_DB
# اطلاعات ورود را در Environment Variables هاست تنظیم کنید؛ رمز را داخل کد ننویسید.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_USERNAME or not ADMIN_PASSWORD:
    raise RuntimeError(
        "برای اجرای برنامه، متغیرهای محیطی ADMIN_USERNAME و ADMIN_PASSWORD را تنظیم کنید."
    )
PER_PAGE = 15

SERVICES = ["مشاوره روان‌شناسی", "ذهن و هنر", "کارگاه‌های آموزشی"]
SESSION_TYPES = ["حضوری (تهران)", "آنلاین"]

STATUSES = {
    "new": "جدید",
    "contacted": "تماس گرفته شد",
    "done": "انجام شد",
    "cancelled": "لغو شد",
}

SERVICE_CARDS = [
    {
        "title": "مشاوره روان‌شناسی",
        "text": "فضایی امن برای گفت‌وگو، شناخت بهتر خود و بررسی چالش‌های زندگی و روابط",
        "icon": "chat",
    },
    {
        "title": "ذهن و هنر",
        "text": "ورکشاپ‌هایی برای لمس شیرینی ساختن و عمیق‌تر فهمیدن خود؛ ترکیبی از هنر، خلاقیت و خودشناسی",
        "icon": "palette",
    },
    {
        "title": "کارگاه‌های آموزشی",
        "text": "دوره‌ها و کارگاه‌های گروهی با موضوعات روان‌شناسی و هنر",
        "icon": "group",
    },
]

STEPS = [
    ("۱", "ثبت درخواست", "فرم رزرو را در چند ثانیه تکمیل می‌کنید."),
    ("۲", "هماهنگی", "برای تعیین زمان مناسب با شما تماس گرفته می‌شود."),
    ("۳", "برگزاری جلسه", "حضوری یا آنلاین، هر کدام که برای شما راحت‌تر است."),
]

FAQ = [
    ("جلسات به چه صورت برگزار می‌شود؟",
     "جلسات هم به‌صورت حضوری در تهران و هم به‌صورت آنلاین قابل برگزاری است. نوع جلسه را هنگام ثبت درخواست انتخاب می‌کنید."),
    ("اطلاعات من نزد چه کسی می‌ماند؟",
     "اطلاعاتی که در فرم وارد می‌کنید فقط برای هماهنگی جلسه استفاده می‌شود و در اختیار شخص ثالث قرار نمی‌گیرد."),
    ("برای شرکت در کارگاه ها چه باید کرد؟",
     "کافی است در فرم، «کارگاه‌های آموزشی» یا «ذهن و هنر» را انتخاب کنید تا از برنامه‌ی کارگاه‌ها مطلع شوید."),
    ("بعد از ثبت درخواست چه اتفاقی می‌افتد؟",
     "درخواست شما برای مدیر ارسال می‌شود و برای هماهنگی زمان، با شماره‌ای که وارد کرده‌اید تماس گرفته خواهد شد."),
]

CONTACT_KINDS = {
    "instagram": "اینستاگرام",
    "whatsapp": "واتساپ",
    "telegram": "تلگرام",
    "phone": "تلفن",
    "email": "ایمیل",
    "other": "سایر",
}

# ----------------------------------------------------------------------
# دیتابیس
# ----------------------------------------------------------------------
def _resolve_db_path():
    global DB_NAME
    folder = os.path.dirname(DB_NAME) or "."
    if not os.path.isdir(folder):
        DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appointments.db")


@contextmanager
def db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def create_database():
    _resolve_db_path()
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                subject TEXT NOT NULL
            )""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(appointments)")}
        for col, ddl in (
            ("session_type", "TEXT"),
            ("notes", "TEXT"),
            ("created_at", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'new'"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE appointments ADD COLUMN {col} {ddl}")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS contact_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                value TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                is_visible INTEGER NOT NULL DEFAULT 1
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS about_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                is_visible INTEGER NOT NULL DEFAULT 1
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")

        if not get_setting(conn, "secret_key"):
            set_setting(conn, "secret_key", secrets.token_hex(32))

        # فقط در اولین راه‌اندازی رمز اولیه ثبت می‌شود؛ تغییر رمز مدیر در دیتابیس حفظ می‌شود.
        if not get_setting(conn, "admin_password_hash"):
            set_setting(conn, "admin_password_hash", generate_password_hash(ADMIN_PASSWORD))


create_database()
with db() as _c:
    _SECRET = get_setting(_c, "secret_key")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or _SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=64 * 1024,
)

# ----------------------------------------------------------------------
# ابزارهای کمکی
# ----------------------------------------------------------------------
_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def fa_digits(value):
    return str(value).translate(_FA)


def to_ascii_digits(value):
    return str(value).translate(_EN)


def gregorian_to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
            + (gy2 + 399) // 400 + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                 "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]


def jalali(value, with_time=True):
    if not value:
        return "—"
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    out = f"{jd} {JALALI_MONTHS[jm - 1]} {jy}"
    if with_time:
        out += f" — {dt.strftime('%H:%M')}"
    return fa_digits(out)


app.jinja_env.filters["fa"] = fa_digits
app.jinja_env.filters["jalali"] = jalali


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token, "statuses": STATUSES, "contact_kinds": CONTACT_KINDS}


@app.before_request
def protect_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "درخواست نامعتبر است. صفحه را دوباره باز کنید.")


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*a, **kw)
    return wrapped


_attempts = {}


def too_many_attempts(ip):
    now = time.time()
    _attempts[ip] = [t for t in _attempts.get(ip, []) if now - t < 600]
    return len(_attempts[ip]) >= 5


PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-]{6,19}$")


def build_contact_url(kind, value):
    value = value.strip()
    if kind == "instagram":
        user = value.rstrip("/").split("/")[-1].lstrip("@")
        user = re.sub(r"[^A-Za-z0-9._]", "", user)
        return f"https://www.instagram.com/{user}/" if user else None
    if kind == "telegram":
        user = value.rstrip("/").split("/")[-1].lstrip("@")
        user = re.sub(r"[^A-Za-z0-9_]", "", user)
        return f"https://t.me/{user}" if user else None
    if kind == "whatsapp":
        num = "".join(ch for ch in to_ascii_digits(value) if ch.isdigit())
        if num.startswith("00"):
            num = num[2:]
        elif num.startswith("0"):
            num = "98" + num[1:]
        return f"https://wa.me/{num}" if num else None
    if kind == "phone":
        num = re.sub(r"[^0-9+]", "", to_ascii_digits(value))
        return f"tel:{num}" if num else None
    if kind == "email":
        return f"mailto:{value}" if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else None
    if kind == "other" and value.startswith("https://"):
        return value
    return None


# ----------------------------------------------------------------------
# استایل و اسکریپت
# ----------------------------------------------------------------------
CSS = r"""
:root{
  --olive:#3F4932; --olive-deep:#2B3322; --olive-mid:#566347;
  --olive-pale:#EEEBD9;
  --cream:#F5F0E4; --cream-white:#FFFCF5; --sand:#EFE6D0; --sand-deep:#E5D8B8;
  --gold:#B49A62; --gold-light:#D8C69A; --gold-deep:#8C7640;
  --text:#2C3426; --muted:#6B7060; --border:#E3DCC6;
  --danger:#A33D32; --ok:#3E6B3A;
  --radius:20px;
  --shadow:0 6px 22px rgba(60,55,30,.08);
  --shadow-lg:0 12px 30px rgba(60,55,30,.13);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:76px}
html,body{overflow-x:clip}
body{margin:0;direction:rtl;background:var(--cream);color:var(--text);
  font-family:'Vazirmatn',Tahoma,sans-serif;font-size:15px;line-height:1.95}
a{color:var(--olive);text-decoration:none}
a:hover{color:var(--gold-deep)}
button,input,select,textarea{font-family:inherit}
:focus-visible{outline:3px solid var(--gold);outline-offset:2px;border-radius:8px}
::selection{background:var(--gold-light);color:var(--olive-deep)}
svg{flex-shrink:0}

/* ---------- ناوبری ---------- */
.site-nav{position:sticky;top:0;z-index:50;background:var(--cream-white);
  border-bottom:1px solid var(--border)}
.site-nav.scrolled{box-shadow:0 4px 16px rgba(60,55,30,.10)}
.site-nav .inner{max-width:1120px;margin:0 auto;padding:12px 22px;display:flex;
  align-items:center;justify-content:center;gap:18px}
.brand{font-family:'Lalezar','Vazirmatn',sans-serif;font-size:26px;color:var(--olive);
  display:flex;align-items:center;gap:8px}
.brand svg{width:28px;height:28px;color:var(--gold)}
.navlinks{display:flex;flex-wrap:wrap;gap:4px 20px;align-items:center}
.navlinks a{color:var(--olive);font-size:14px;padding:2px 0;border-bottom:2px solid transparent}
.navlinks a:hover,.navlinks a.active{color:var(--gold-deep);border-bottom-color:var(--gold)}
.navlinks .cta{padding:7px 18px;border-radius:999px;background:var(--olive);color:var(--cream);
  font-weight:700;border:0}
.navlinks .cta:hover{background:var(--olive-mid);color:#fff}

/* ---------- هیرو ---------- */
.hero{position:relative;overflow:hidden;text-align:center;padding:42px 6% 44px;
  background:
    radial-gradient(circle at 12% 18%,rgba(216,198,154,.45),transparent 38%),
    radial-gradient(circle at 88% 85%,rgba(180,154,98,.22),transparent 40%),
    linear-gradient(170deg,#FBF6EA,#F1E7D0)}
.hero::after{content:"";position:absolute;left:0;right:0;bottom:0;height:4px;
  background:linear-gradient(90deg,var(--olive),var(--gold),var(--olive))}
.blob{display:none}
.leaf{position:absolute;color:var(--olive);opacity:.10;pointer-events:none}
.leaf.l1{width:170px;right:-20px;top:30px;transform:rotate(18deg)}
.leaf.l2{width:130px;left:3%;bottom:20px;transform:rotate(-24deg) scaleX(-1)}
.hero > *:not(.leaf){position:relative}
.eyebrow{display:inline-flex;gap:8px;align-items:center;padding:5px 16px;border-radius:999px;
  border:1px solid var(--gold-light);background:var(--cream-white);
  color:var(--gold-deep);font-size:13px;margin-bottom:14px}
.eyebrow svg{width:16px;height:16px}
.tagline{font-family:'Lalezar','Vazirmatn',sans-serif;font-size:clamp(30px,5.4vw,56px);
  line-height:1.55;margin:0 0 4px;color:var(--olive-deep)}
.tagline em{font-style:normal;color:var(--gold-deep)}
.gold-line{width:80px;height:3px;margin:12px auto 16px;border-radius:10px;
  background:linear-gradient(90deg,var(--gold),var(--gold-light))}
.intro{max-width:560px;margin:0 auto 20px;color:var(--muted);font-size:13.5px;line-height:2.05}
.hero-actions{display:flex;flex-direction:column;gap:10px;align-items:center}
.btn{display:inline-flex;align-items:center;gap:8px;padding:12px 28px;border-radius:14px;border:0;
  font-weight:700;font-size:15px;cursor:pointer}
.btn-gold{background:var(--olive);color:var(--cream);box-shadow:0 6px 16px rgba(63,73,50,.25)}
.btn-gold:hover{background:var(--olive-mid);color:#fff}
.btn-ghost{background:var(--cream-white);color:var(--olive);border:1.5px solid var(--gold)}
.btn-ghost:hover{background:var(--sand);color:var(--olive-deep)}
.btn-ghost.btn-sm{padding:8px 20px;font-size:13px;border-radius:12px;font-weight:600}
.hero-bottom{display:none}

/* ---------- بخش‌ها ---------- */
.container{width:min(100% - 32px,1060px);margin-inline:auto}
section.block{padding:28px 0}
section.alt{background:linear-gradient(180deg,var(--sand),#F2EAD6);
  border-block:1px solid var(--sand-deep)}
.section-head{text-align:center;margin-bottom:18px}
.section-title{margin:0;color:var(--olive);font-family:'Lalezar','Vazirmatn',sans-serif;
  font-weight:400;font-size:clamp(25px,3.4vw,36px);line-height:1.7}
.section-sub{color:var(--muted);max-width:560px;margin:0 auto}
.orn{display:flex;align-items:center;justify-content:center;gap:12px;margin:4px 0 8px;color:var(--gold)}
.orn::before,.orn::after{content:"";width:50px;height:1px;background:var(--gold-light)}
.orn svg{width:18px;height:18px}

.about-card{background:var(--cream-white);border:1px solid var(--border);border-radius:var(--radius);
  padding:clamp(18px,3.5vw,32px);box-shadow:var(--shadow);margin-bottom:14px;
  border-top:4px solid var(--gold)}
.about-text{max-width:780px;margin:0 auto;font-size:15.5px;line-height:2.4;white-space:pre-line}

.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
.card{padding:28px 24px;border-radius:var(--radius);background:var(--cream-white);
  border:1px solid var(--border);box-shadow:var(--shadow);transition:transform .25s,box-shadow .25s}
.card:hover{transform:translateY(-5px);box-shadow:var(--shadow-lg);border-color:var(--gold-light)}
.ico{width:56px;height:56px;border-radius:16px;display:grid;place-items:center;margin-bottom:16px;
  background:linear-gradient(135deg,var(--gold),var(--gold-light));color:var(--olive-deep)}
.ico svg{width:28px;height:28px}
.card h3{margin:0 0 8px;color:var(--olive);font-size:18px}
.card p{margin:0;color:var(--muted);line-height:2.1}

.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:20px}
.step{text-align:center;padding:10px 16px}
.step .num{width:60px;height:60px;margin:0 auto 12px;border-radius:50%;display:grid;place-items:center;
  font-family:'Lalezar',sans-serif;font-size:26px;color:var(--cream);background:var(--olive);
  border:3px solid var(--gold-light)}
.step h3{margin:0 0 4px;color:var(--olive);font-size:17px}
.step p{margin:0;color:var(--muted);font-size:14px}

.faq{max-width:780px;margin-inline:auto}
.faq details{background:var(--cream-white);border:1px solid var(--border);border-radius:14px;margin-bottom:10px}
.faq details[open]{border-color:var(--gold);box-shadow:var(--shadow)}
.faq summary{list-style:none;cursor:pointer;padding:15px 20px;font-weight:600;color:var(--olive);
  display:flex;justify-content:space-between;align-items:center;gap:12px}
.faq summary::-webkit-details-marker{display:none}
.faq summary::after{content:"+";font-size:24px;color:var(--gold-deep);line-height:1}
.faq details[open] summary::after{content:"–"}
.faq details p{margin:0;padding:0 20px 16px;color:var(--muted)}

/* ---------- فرم ---------- */
.booking{display:block;border-radius:24px;overflow:hidden;
  box-shadow:var(--shadow-lg);border:1px solid var(--border);background:var(--cream-white);
  max-width:640px;margin-inline:auto}
.booking-form{padding:36px 34px}
.field{margin-bottom:18px}
.field label.lbl,.field span.lbl{display:block;margin-bottom:6px;color:var(--olive);font-weight:600;font-size:14px}
.field input:not([type=radio]),.field select,.field textarea{width:100%;padding:12px 15px;
  border:1.5px solid var(--sand-deep);border-radius:12px;background:#FFFDF7;color:var(--text);font-size:15px}
.field input:focus,.field select:focus,.field textarea:focus{outline:none;border-color:var(--olive);
  box-shadow:0 0 0 3px rgba(63,73,50,.12);background:#fff}
.field textarea{min-height:110px;resize:vertical}
.field.invalid input,.field.invalid select{border-color:var(--danger);background:#FFF6F4}
.err{display:none;color:var(--danger);font-size:12.5px;margin-top:4px}
.field.invalid .err{display:block}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.seg label{position:relative;cursor:pointer}
.seg input{position:absolute;opacity:0;top:0;left:0;width:100%;height:100%;margin:0}
.seg span{display:flex;align-items:center;justify-content:center;padding:12px;border-radius:12px;
  border:1.5px solid var(--sand-deep);background:#FFFDF7;color:var(--olive);font-weight:500}
.seg input:checked + span{background:var(--olive);color:var(--cream);border-color:var(--olive)}
.seg input:focus-visible + span{outline:3px solid var(--gold);outline-offset:2px}
.hp{position:absolute;width:0;height:0;overflow:hidden;opacity:0;pointer-events:none}
.submit{width:100%;margin-top:8px;padding:14px;border:0;border-radius:14px;cursor:pointer;
  background:var(--olive);color:var(--cream);font-weight:700;font-size:16px;
  box-shadow:0 6px 16px rgba(63,73,50,.22)}
.submit:hover{background:var(--olive-mid)}
.submit[disabled]{opacity:.7;cursor:wait}
.counter{text-align:left;font-size:12px;color:var(--muted)}

.toast{position:fixed;z-index:80;bottom:22px;left:50%;transform:translate(-50%,30px);opacity:0;
  padding:13px 24px;border-radius:14px;font-weight:600;box-shadow:var(--shadow-lg);
  transition:opacity .35s,transform .35s;max-width:90vw;text-align:center}
.toast.show{transform:translate(-50%,0);opacity:1}
.toast.ok{background:#E9F0DF;border:1px solid #9DB68A;color:#2F4A28}
.toast.bad{background:#FBEAE7;border:1px solid #E0A9A2;color:#7B2A21}

/* ---------- تماس و فوتر ---------- */
.contact-row{display:flex;flex-wrap:wrap;justify-content:center;gap:18px}
.contact-pill{display:grid;place-items:center;width:56px;height:56px;border-radius:50%;
  background:var(--cream-white);border:1px solid var(--border);box-shadow:var(--shadow);color:var(--olive);
  transition:transform .2s,border-color .2s,background .2s}
.contact-pill:hover{border-color:var(--gold);background:var(--sand);transform:translateY(-3px)}
.contact-pill .ci{width:100%;height:100%;border-radius:50%;display:grid;place-items:center;
  background:var(--olive);color:var(--gold-light)}
.contact-pill svg{width:26px;height:26px}
footer.site-footer{margin-top:20px;padding:28px 20px 20px;text-align:center;background:var(--olive-deep);
  color:var(--cream);border-top:3px solid var(--gold)}
footer .fbrand{font-family:'Lalezar',sans-serif;font-size:24px;margin:0}
footer .fsub{color:var(--gold-light);margin:0 0 12px;font-size:14px}
footer a{color:var(--gold-light);font-size:13px}
footer a:hover{color:#fff}
footer small{display:block;margin-top:10px;color:#aeb39f}
.to-top{position:fixed;z-index:60;bottom:20px;right:20px;width:44px;height:44px;border-radius:50%;border:0;
  cursor:pointer;background:var(--olive);color:var(--gold-light);box-shadow:var(--shadow-lg);
  opacity:0;pointer-events:none;font-size:20px;transition:opacity .25s}
.to-top.show{opacity:1;pointer-events:auto}

/* ---------- ظاهر شدن هنگام اسکرول (فقط وقتی JS فعال باشد) ---------- */

/* ---------- پنل مدیریت ---------- */
body.admin-body{background:var(--cream)}
.admin-wrap{width:min(100% - 24px,1000px);margin:24px auto 60px}
.admin-bar{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:12px;
  padding:14px 22px;border-radius:16px;background:var(--cream-white);color:var(--olive);
  border:1px solid var(--border);border-bottom:3px solid var(--gold);box-shadow:var(--shadow);margin-bottom:20px}
.admin-bar .back-arrow{font-size:22px;line-height:1;color:var(--olive);padding:4px 10px;border-radius:10px;
  border:1px solid var(--border);background:var(--sand);text-decoration:none}
.admin-bar .back-arrow:hover{background:var(--olive);color:var(--cream);border-color:var(--olive)}
.admin-bar nav{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center}
.admin-bar a,.admin-bar button.link{color:var(--olive);font-size:13.5px;background:none;border:0;
  cursor:pointer;padding:4px 0;border-bottom:2px solid transparent}
.admin-bar a:hover,.admin-bar button.link:hover,.admin-bar a.cur{color:var(--gold-deep);border-bottom-color:var(--gold)}
.admin-bar a.back-arrow{border-bottom:0}
.panel{background:var(--cream-white);border:1px solid var(--border);border-radius:18px;
  padding:clamp(16px,3vw,28px);box-shadow:var(--shadow);margin-bottom:18px}
.panel h1,.panel h2{margin:0 0 6px;color:var(--olive);font-family:'Lalezar',sans-serif;font-weight:400}
.panel h1{font-size:28px}.panel h2{font-size:22px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.stat{padding:16px;border-radius:16px;background:var(--cream-white);border:1px solid var(--border);
  box-shadow:var(--shadow);border-right:5px solid var(--gold)}
.stat small{color:var(--muted)}
.stat b{display:block;font-size:28px;color:var(--olive);font-family:'Lalezar',sans-serif;font-weight:400;line-height:1.4}
.bars{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;height:150px;padding-top:10px}
.bar{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;gap:6px}
.bar i{display:block;width:100%;max-width:44px;border-radius:9px 9px 3px 3px;min-height:4px;
  background:linear-gradient(180deg,var(--gold),var(--olive-mid))}
.bar span{font-size:11.5px;color:var(--muted)}
.bar em{font-style:normal;font-size:12px;color:var(--olive);font-weight:700}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px}
.tile{display:block;padding:18px 20px;border-radius:14px;background:var(--sand);
  border:1px solid var(--sand-deep);color:var(--olive);font-weight:600}
.tile:hover{border-color:var(--gold);background:#F3EBD7}
.tile small{display:block;font-weight:400;color:var(--muted)}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 8px}
.toolbar input,.toolbar select,.panel select,.panel textarea,.panel input:not([type=checkbox]){
  padding:10px 14px;border:1.5px solid var(--sand-deep);border-radius:12px;background:#FFFDF7;font-size:14px;color:var(--text)}
.toolbar input{flex:1;min-width:160px}
.panel form label{display:block;margin:12px 0 5px;color:var(--olive);font-weight:600;font-size:14px}
.panel form input:not([type=checkbox]),.panel form select,.panel form textarea{width:100%}
.btn-sm{padding:9px 18px;border-radius:11px;border:0;cursor:pointer;font-weight:600;font-size:13.5px;
  background:var(--olive);color:var(--cream)}
.btn-sm:hover{background:var(--olive-mid)}
.btn-sm.danger{background:var(--danger)}.btn-sm.danger:hover{background:#8a2f26}
.btn-sm.light{background:var(--sand);color:var(--olive);border:1px solid var(--sand-deep)}
.item{margin:12px 0;padding:16px 18px;border:1px solid var(--border);border-right:5px solid var(--gold);
  border-radius:14px;background:#FAF6EA;overflow-wrap:anywhere}
.item.s-new{border-right-color:#C9892E}.item.s-contacted{border-right-color:#4C7DA8}
.item.s-done{border-right-color:var(--ok)}.item.s-cancelled{border-right-color:#999}
.item .head{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:center;margin-bottom:6px}
.item .head label{display:flex!important;gap:8px;align-items:center;margin:0!important}
.item p{margin:3px 0}.item b{color:var(--olive)}
.badge{display:inline-block;padding:2px 12px;border-radius:999px;font-size:12px;font-weight:700}
.badge.new{background:#FBEFD8;color:#8A5A10}.badge.contacted{background:#E1ECF6;color:#2B5278}
.badge.done{background:#E1F0DD;color:#2F5A2B}.badge.cancelled{background:#EEE;color:#666}
.quick{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.quick a{padding:5px 14px;border-radius:999px;background:var(--sand);border:1px solid var(--sand-deep);font-size:13px}
.quick a:hover{background:var(--olive);color:var(--cream)}
.msg{padding:12px 16px;border-radius:12px;background:#E9F0DF;border:1px solid #B8C5A5;color:#34472B;
  font-weight:600;margin:12px 0}
.msg.bad{background:#FBEAE7;border-color:#E0A9A2;color:#7B2A21}
.pager{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:16px}
.pager a,.pager span{padding:6px 14px;border-radius:10px;border:1px solid var(--border);background:var(--cream-white)}
.pager span{background:var(--olive);color:var(--cream)}
.row-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.row-actions form{margin:0}
.login-card{max-width:420px;margin:10vh auto;text-align:center}
.login-card .ico{margin:0 auto 14px}
.muted{color:var(--muted)}

@media (max-width:860px){
  .grid-3,.steps{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,1fr)}
  .tiles{grid-template-columns:1fr}
}
@media (max-width:900px) and (min-width:621px){
  .tiles{grid-template-columns:repeat(2,1fr)}
}
@media (max-width:620px){
  .site-nav{position:static}
  .site-nav .inner{flex-direction:column;gap:6px;padding:10px 14px}
  .navlinks{justify-content:center;gap:4px 14px}
  .navlinks a{font-size:13px}
  .hero{padding:36px 6% 40px}
  .booking-form{padding:26px 18px}
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important}
}
@media print{.site-nav,.to-top,.toast{display:none}}
"""

JS = r"""
(function(){
  document.documentElement.classList.add('js');
  var nav=document.querySelector('.site-nav'), top=document.querySelector('.to-top');
  function onScroll(){
    var y=window.scrollY;
    if(nav) nav.classList.toggle('scrolled', y>40);
    if(top) top.classList.toggle('show', y>600);
  }
  window.addEventListener('scroll', onScroll, {passive:true}); onScroll();
  if(top) top.addEventListener('click', function(){window.scrollTo({top:0,behavior:'smooth'})});

  var els=document.querySelectorAll('.reveal');
  if('IntersectionObserver' in window){
    var io=new IntersectionObserver(function(es){
      es.forEach(function(e){ if(e.isIntersecting){ e.target.classList.add('in'); io.unobserve(e.target);} });
    },{threshold:.04,rootMargin:'0px 0px -4% 0px'});
    els.forEach(function(el){io.observe(el)});
  } else { els.forEach(function(el){el.classList.add('in')}); }

  // لینک فعال در ناوبری
  var links=[].slice.call(document.querySelectorAll('.navlinks a[href^="#"]:not(.cta)'));
  if(links.length && 'IntersectionObserver' in window){
    var map={}; links.forEach(function(a){map[a.getAttribute('href').slice(1)]=a});
    var so=new IntersectionObserver(function(es){
      es.forEach(function(e){ if(e.isIntersecting){
        links.forEach(function(a){a.classList.remove('active')});
        var a=map[e.target.id]; if(a) a.classList.add('active');
      }});
    },{rootMargin:'-45% 0px -50% 0px'});
    Object.keys(map).forEach(function(id){var s=document.getElementById(id); if(s) so.observe(s)});
  }

  // پیام شناور
  var t=document.querySelector('.toast');
  if(t){ setTimeout(function(){t.classList.add('show')},300);
         setTimeout(function(){t.classList.remove('show')},6500); }

  // فرم رزرو
  var form=document.getElementById('bookingForm');
  if(form){
    var fa='۰۱۲۳۴۵۶۷۸۹', en='0123456789';
    function norm(s){return s.replace(/[۰-۹]/g,function(d){return en[fa.indexOf(d)]})}
    var phone=form.phone, notes=form.notes, cnt=document.getElementById('cnt');
    function setErr(name, bad){
      var f=form.querySelector('[data-f="'+name+'"]'); if(f) f.classList.toggle('invalid', bad); return !bad;
    }
    function validate(){
      var ok=true;
      ok=setErr('name', form.name.value.trim().length<3) && ok;
      ok=setErr('phone', !/^\+?[0-9][0-9\s\-]{6,19}$/.test(norm(phone.value.trim()))) && ok;
      ok=setErr('subject', !form.subject.value) && ok;
      ok=setErr('session_type', !form.querySelector('input[name=session_type]:checked')) && ok;
      return ok;
    }
    ['name','phone','subject'].forEach(function(n){
      form[n].addEventListener('blur', validate);
    });
    if(notes&&cnt){ notes.addEventListener('input', function(){cnt.textContent=notes.value.length+' / 2000'}); }
    form.addEventListener('submit', function(e){
      if(!validate()){ e.preventDefault();
        var bad=form.querySelector('.invalid'); if(bad) bad.scrollIntoView({behavior:'smooth',block:'center'});
        return; }
      var b=form.querySelector('.submit'); b.disabled=true; b.textContent='در حال ارسال…';
    });
  }

  // نمودار مدیریت
  document.querySelectorAll('.bar i[data-h]').forEach(function(i){
    var h=i.getAttribute('data-h'); i.style.height='4px';
    setTimeout(function(){i.style.height=h+'%'},120);
  });
  // انتخاب همه
  var all=document.getElementById('selectAll');
  if(all){ all.addEventListener('change', function(){
    document.querySelectorAll('input[name=selected_ids]').forEach(function(c){c.checked=all.checked});
  }); }
  document.querySelectorAll('form[data-confirm]').forEach(function(f){
    f.addEventListener('submit', function(e){
      var sub=e.submitter, need=(sub&&sub.dataset.confirm)||null;
      if(need && !confirm(need)) e.preventDefault();
    });
  });
})();
"""

# ----------------------------------------------------------------------
# آیکون‌ها
# ----------------------------------------------------------------------
ICONS = {
    "chat": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4A2.5 2.5 0 0 1 4 13.5z"/><path d="M12 12.6s-3-1.8-3-3.9a1.7 1.7 0 0 1 3-1.1 1.7 1.7 0 0 1 3 1.1c0 2.1-3 3.9-3 3.9z"/></svg>',
    "palette": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a9 9 0 1 0 0 18c1.7 0 2.3-1.2 1.7-2.3s-.2-2.2 1.3-2.2h2A3.5 3.5 0 0 0 20.5 13C20.5 7.5 16.7 3 12 3z"/><circle cx="7.8" cy="11" r="1" fill="currentColor"/><circle cx="10.5" cy="7.2" r="1" fill="currentColor"/><circle cx="15" cy="7.8" r="1" fill="currentColor"/></svg>',
    "group": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17" cy="9" r="2.5"/><path d="M16 14.2c3 .1 5 2.2 5 5.3"/></svg>',
    "instagram": '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="5" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="17.5" cy="6.5" r="1.2" fill="currentColor"/></svg>',
    "whatsapp": '<svg viewBox="0 0 24 24"><path d="M20 11.5a8 8 0 0 1-11.8 7L4 20l1.5-4A8 8 0 1 1 20 11.5Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M9 8.5c.3-.5.6-.5.9-.1l1 1.4c.2.3.1.6-.2.9l-.5.5c.6 1.2 1.4 2 2.6 2.6l.5-.6c.2-.3.5-.3.8-.2l1.4.7c.4.2.4.5.3.8-.3 1-1.2 1.5-2.2 1.3-2.7-.6-5.5-3.4-6.1-6.1-.2-.5.2-1 .5-1.2Z" fill="currentColor"/></svg>',
    "telegram": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"><path d="M21 4 3 11l5.5 2L10 19l3-3.5L17.5 19z"/><path d="m8.5 13 8-5.5"/></svg>',
    "phone": '<svg viewBox="0 0 24 24"><path d="M7 3H5a2 2 0 0 0-2 2c0 8.8 7.2 16 16 16a2 2 0 0 0 2-2v-2l-5-2-2 2a14 14 0 0 1-7-7l2-2-2-5Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
    "email": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 8 8 6 8-6"/></svg>',
    "other": '<svg viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.1 0l3-3A5 5 0 0 0 13 2.9l-1.7 1.7M14 11a5 5 0 0 0-7.1 0l-3 3A5 5 0 0 0 11 21.1l1.7-1.7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    "leaf": '<svg viewBox="0 0 120 240" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M60 235V20"/><path d="M60 60c-30-5-45-25-45-45 30 3 45 20 45 45zM60 60c30-5 45-25 45-45-30 3-45 20-45 45zM60 110c-34-6-50-28-50-50 34 4 50 24 50 50zM60 110c34-6 50-28 50-50-34 4-50 24-50 50zM60 165c-36-6-54-30-54-54 36 4 54 26 54 54zM60 165c36-6 54-30 54-54-36 4-54 26-54 54z"/></svg>',
    "lotus": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M12 4c2.5 2.3 3.5 5 0 10-3.5-5-2.5-7.700 0-10z"/><path d="M4 9c3.300-.3 6 1.700 8 5-3.500.5-7.500-.8-8-5zM20 9c-3.300-.3-6 1.700-8 5 3.500.5 7.500-.8 8-5z"/><path d="M6 17c2 1.500 4 2 6 2s4-.5 6-2"/></svg>',
}
app.jinja_env.globals["ICONS"] = ICONS
import hashlib
app.jinja_env.globals["ASSET_V"] = hashlib.md5((CSS + JS).encode("utf-8")).hexdigest()[:8]

# ----------------------------------------------------------------------
# قالب‌ها
# ----------------------------------------------------------------------
BASE = """<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#3F4932">
<title>{% block title %}آرامش و هنر{% endblock %}</title>
{% block meta %}{% endblock %}
<link rel="stylesheet" href="{{ url_for('asset_css', v=ASSET_V) }}">
<link rel="stylesheet" media="print" onload="this.media='all'" href="https://fonts.googleapis.com/css2?family=Lalezar&family=Vazirmatn:wght@300;400;500;600;700&display=swap">
</head>
<body class="{% block bodyclass %}{% endblock %}">
{% block body %}{% endblock %}
<script src="{{ url_for('asset_js', v=ASSET_V) }}" defer></script>
</body>
</html>"""

HOME = """{% extends "base.html" %}
{% block title %}آرامش و هنر | مشاوره روان‌شناسی، ذهن و هنر، کارگاه‌های آموزشی{% endblock %}
{% block meta %}
<meta name="description" content="آرامش و هنر؛ مشاوره روان‌شناسی، ورکشاپ‌های ذهن و هنر و کارگاه‌های آموزشی. رزرو جلسه حضوری در تهران یا آنلاین.">
<meta property="og:title" content="آرامش و هنر">
<meta property="og:description" content="شناخت فردی و روابط، رشد و خلاقیت">
<meta property="og:locale" content="fa_IR">
{% endblock %}
{% block body %}
<nav class="site-nav"><div class="inner">
  <a class="brand" href="#top">{{ ICONS.lotus|safe }}آرامش و هنر</a>
</div></nav>

<header class="hero" id="top">
  <span class="eyebrow">{{ ICONS.lotus|safe }} مشاوره · هنر · رشد</span>
  <h1 class="tagline">شناخت فردی و روابط،<br><em>رشد و خلاقیت</em></h1>
  <div class="gold-line"></div>
  <p class="intro">همراه شما در مسیر سلامت روان، رشد فردی، تجربه هنری، فعالیت گروهی و برگزاری کارگاه‌های آموزشی جذاب</p>
  <div class="hero-actions">
    <a class="btn btn-ghost" href="#services">آشنایی با خدمات</a>
    <a class="btn btn-gold" href="#booking">رزرو مشاوره</a>
    <a class="btn btn-ghost btn-sm" href="#contact">راه‌های ارتباطی</a>
  </div>
  <div class="hero-bottom"><svg viewBox="0 0 1440 70" preserveAspectRatio="none"><path fill="#F5F0E4" d="M0 40c240 40 480 40 720 12S1200 0 1440 30v40H0z"/></svg></div>
</header>

<main>
{% if about_sections %}
<section class="block" id="about"><div class="container">
  {% for s in about_sections %}
  <article class="about-card reveal">
    <div class="section-head" style="margin-bottom:18px">
      <div class="orn">{{ ICONS.lotus|safe }}</div>
      <h2 class="section-title">{{ s.title }}</h2>
    </div>
    <div class="about-text">{{ s.content }}</div>
  </article>
  {% endfor %}
</div></section>
{% endif %}

<section class="block alt" id="services"><div class="container">
  <div class="section-head reveal">
    <div class="orn">{{ ICONS.lotus|safe }}</div>
    <h2 class="section-title">خدمات ما</h2>
    <p class="section-sub">سه مسیر برای شناخت بیشتر، رشد و تجربه‌ی خلاقانه</p>
  </div>
  <div class="grid-3">
    {% for c in service_cards %}
    <article class="card reveal d{{ loop.index }}">
      <div class="ico">{{ ICONS[c.icon]|safe }}</div>
      <h3>{{ c.title }}</h3>
      <p>{{ c.text }}</p>
    </article>
    {% endfor %}
  </div>
</div></section>

<section class="block" id="process"><div class="container">
  <div class="section-head reveal">
    <div class="orn">{{ ICONS.lotus|safe }}</div>
    <h2 class="section-title">روند کار</h2>
    <p class="section-sub">رزرو ساده است؛ فقط سه قدم</p>
  </div>
  <div class="steps">
    {% for n,t,d in steps %}
    <div class="step reveal d{{ loop.index }}"><div class="num">{{ n }}</div><h3>{{ t }}</h3><p>{{ d }}</p></div>
    {% endfor %}
  </div>
</div></section>

<section class="block" id="booking"><div class="container">
  <div class="booking reveal">
    <form class="booking-form" id="bookingForm" method="POST" action="{{ url_for('home') }}#booking" novalidate>
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">

      <div class="field" data-f="name">
        <label class="lbl" for="name">نام و نام خانوادگی *</label>
        <input id="name" name="name" maxlength="100" placeholder="نام خود را وارد کنید" autocomplete="name" required>
        <div class="err">لطفاً نام خود را وارد کنید.</div>
      </div>
      <div class="field" data-f="phone">
        <label class="lbl" for="phone">شماره تماس *</label>
        <input id="phone" name="phone" type="tel" inputmode="tel" maxlength="30" dir="ltr"
               style="text-align:right" placeholder="۰۹۱۲۳۴۵۶۷۸۹" autocomplete="tel" required>
        <div class="err">شماره تماس معتبر وارد کنید.</div>
      </div>
      <div class="field" data-f="subject">
        <label class="lbl" for="subject">خدمت موردنظر *</label>
        <select id="subject" name="subject" required>
          <option value="">انتخاب کنید</option>
          {% for s in services %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
        </select>
        <div class="err">یکی از خدمات را انتخاب کنید.</div>
      </div>
      <div class="field" data-f="session_type">
        <span class="lbl">نوع جلسه *</span>
        <div class="seg">
          {% for t in session_types %}
          <label><input type="radio" name="session_type" value="{{ t }}"><span>{{ t }}</span></label>
          {% endfor %}
        </div>
        <div class="err">نوع جلسه را مشخص کنید.</div>
      </div>
      <div class="field">
        <label class="lbl" for="notes">توضیحات بیشتر (اختیاری)</label>
        <textarea id="notes" name="notes" maxlength="2000" placeholder="اگر توضیح یا درخواست خاصی دارید، اینجا بنویسید…"></textarea>
        <div class="counter" id="cnt">۰ / 2000</div>
      </div>
      <button class="submit" type="submit">ثبت درخواست</button>
    </form>
  </div>
</div></section>

<section class="block alt" id="faq"><div class="container">
  <div class="section-head reveal">
    <div class="orn">{{ ICONS.lotus|safe }}</div>
    <h2 class="section-title">پرسش‌های متداول</h2>
  </div>
  <div class="faq reveal">
    {% for q,a in faq %}
    <details><summary>{{ q }}</summary><p>{{ a }}</p></details>
    {% endfor %}
  </div>
</div></section>

{% if contact_methods %}
<section class="block" id="contact"><div class="container">
  <div class="section-head reveal">
    <div class="orn">{{ ICONS.lotus|safe }}</div>
    <h2 class="section-title">ارتباط با ما</h2>
  </div>
  <div class="contact-row reveal">
    {% for c in contact_methods %}
    <a class="contact-pill" href="{{ c.url }}" aria-label="{{ c.label }}"
       {% if c.kind not in ['phone','email'] %}target="_blank" rel="noopener noreferrer"{% endif %}>
      <span class="ci">{{ ICONS[c.kind]|safe }}</span>
    </a>
    {% endfor %}
  </div>
</div></section>
{% endif %}
</main>

<footer class="site-footer">
  <p class="fbrand">آرامش و هنر</p>
  <p class="fsub">شناخت، رشد و خلاقیت</p>
  <a href="{{ url_for('admin_login') }}">ورود مدیر</a>
  <small>© {{ year|fa }}</small>
</footer>
<button class="to-top" aria-label="بازگشت به بالا">↑</button>
{% if flash %}<div class="toast {{ flash.kind }}" role="status">{{ flash.text }}</div>{% endif %}
{% endblock %}"""

ADMIN_BASE = """{% extends "base.html" %}
{% block bodyclass %}admin-body{% endblock %}
{% block body %}
<div class="admin-wrap">
  <div class="admin-bar">
    <a class="back-arrow" href="{{ url_for('home') }}" title="بازگشت به سایت">←</a>
    <nav>
      <a href="{{ url_for('admin_requests') }}" class="{{ 'cur' if cur=='req' }}">درخواست‌ها</a>
      <a href="{{ url_for('manage_about') }}" class="{{ 'cur' if cur=='about' }}">درباره</a>
      <a href="{{ url_for('manage_contact') }}" class="{{ 'cur' if cur=='contact' }}">ارتباط</a>
      <a href="{{ url_for('admin_panel') }}" class="{{ 'cur' if cur=='home' }}">داشبورد</a>
      <form method="post" action="{{ url_for('admin_logout') }}" style="display:inline">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button class="link" type="submit">خروج</button>
      </form>
    </nav>
  </div>
  {% if message %}<div class="msg {{ 'bad' if bad }}">{{ message }}</div>{% endif %}
  {% block content %}{% endblock %}
</div>
{% endblock %}"""

LOGIN = """{% extends "base.html" %}
{% block title %}ورود مدیر | آرامش و هنر{% endblock %}
{% block bodyclass %}admin-body{% endblock %}
{% block body %}
<main class="panel login-card">
  <div class="ico" style="margin:0 auto 14px">{{ ICONS.lotus|safe }}</div>
  <h1>ورود مدیر</h1>
  <div class="gold-line"></div>
  {% if error %}<div class="msg bad">{{ error }}</div>{% endif %}
  <form method="POST" style="text-align:right">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label>نام کاربری</label>
    <input name="username" required autocomplete="username" autofocus>
    <label>رمز عبور</label>
    <input name="password" type="password" required autocomplete="current-password">
    <button class="submit" type="submit">ورود</button>
  </form>
  <p><a href="{{ url_for('home') }}">← بازگشت به سایت</a></p>
</main>
{% endblock %}"""

DASH = """{% extends "admin_base.html" %}
{% block title %}داشبورد | آرامش و هنر{% endblock %}
{% block content %}
<div class="tiles">
  <a class="tile" href="{{ url_for('admin_requests') }}">مدیریت درخواست‌ها<small>جست‌وجو، وضعیت، خروجی اکسل</small></a>
  <a class="tile" href="{{ url_for('manage_about') }}">منوی درباره من<small>افزودن، ترتیب و نمایش بخش‌ها</small></a>
  <a class="tile" href="{{ url_for('manage_contact') }}">ارتباط با ما<small>اینستاگرام، واتساپ، تلگرام و…</small></a>
</div>
<div class="stats">
  <div class="stat"><small>کل درخواست‌ها</small><b>{{ stats.total|fa }}</b></div>
  <div class="stat"><small>درخواست‌های جدید</small><b>{{ stats.new|fa }}</b></div>
  <div class="stat"><small>امروز</small><b>{{ stats.today|fa }}</b></div>
  <div class="stat"><small>آنلاین / حضوری</small><b>{{ stats.online|fa }} / {{ stats.inperson|fa }}</b></div>
</div>
<div class="panel">
  <div class="bars">
    {% for d in chart %}
    <div class="bar"><em>{{ d.count|fa }}</em><i data-h="{{ d.pct }}"></i><span>{{ d.label }}</span></div>
    {% endfor %}
  </div>
</div>
{% endblock %}"""

REQUESTS = """{% extends "admin_base.html" %}
{% block title %}درخواست‌ها | آرامش و هنر{% endblock %}
{% block content %}
<div class="panel">
  <h1>درخواست‌های ثبت‌شده</h1>
  <p class="muted">{{ total|fa }} نتیجه</p>
  <form class="toolbar" method="get">
    <input name="q" value="{{ q }}" placeholder="جست‌وجوی نام، شماره، توضیحات…">
    <select name="status" style="width:auto">
      <option value="">همه‌ی وضعیت‌ها</option>
      {% for k,v in statuses.items() %}<option value="{{ k }}" {{ 'selected' if k==status }}>{{ v }}</option>{% endfor %}
    </select>
    <button class="btn-sm" type="submit">اعمال فیلتر</button>
    <a class="btn-sm light" href="{{ url_for('export_csv', q=q, status=status) }}" style="display:inline-block">دریافت CSV</a>
  </form>

  {% if rows %}
  <form method="POST" data-confirm>
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <div class="toolbar">
      <label style="display:flex;gap:6px;align-items:center;margin:0"><input type="checkbox" id="selectAll"> انتخاب همه</label>
      <select name="status_value" style="width:auto">
        {% for k,v in statuses.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}
      </select>
      <button class="btn-sm light" name="action" value="bulk_status" type="submit">تغییر وضعیت انتخاب‌شده‌ها</button>
      <button class="btn-sm danger" name="action" value="bulk_delete" type="submit"
              data-confirm="انتخاب‌شده‌ها حذف شوند؟ این کار قابل بازگشت نیست.">حذف انتخاب‌شده‌ها</button>
    </div>
    {% for r in rows %}
    <div class="item s-{{ r.status }}">
      <div class="head">
        <label><input type="checkbox" name="selected_ids" value="{{ r.id }}"> <b>#{{ r.id|fa }} — {{ r.name }}</b></label>
        <span class="badge {{ r.status }}">{{ statuses.get(r.status, r.status) }}</span>
      </div>
      <p><b>تلفن:</b> <span dir="ltr">{{ r.phone }}</span></p>
      <p><b>خدمت:</b> {{ r.subject }} &nbsp;·&nbsp; <b>نوع جلسه:</b> {{ r.session_type or 'در نسخه‌ی قبلی ثبت نشده' }}</p>
      <p><b>زمان ثبت:</b> {{ r.created_at|jalali }}</p>
      <p><b>توضیحات:</b> {{ r.notes or '—' }}</p>
      <div class="quick">
        <a href="{{ r.tel }}">تماس</a>
        {% if r.wa %}<a href="{{ r.wa }}" target="_blank" rel="noopener noreferrer">واتساپ</a>{% endif %}
      </div>
    </div>
    {% endfor %}
  </form>
  <div class="pager">
    {% for p in range(1, pages+1) %}
      {% if p==page %}<span>{{ p|fa }}</span>
      {% else %}<a href="{{ url_for('admin_requests', q=q, status=status, page=p) }}">{{ p|fa }}</a>{% endif %}
    {% endfor %}
  </div>
  {% else %}
  <p>درخواستی پیدا نشد.</p>
  {% endif %}
</div>
{% endblock %}"""

ABOUT = """{% extends "admin_base.html" %}
{% block title %}مدیریت درباره من | آرامش و هنر{% endblock %}
{% block content %}
<div class="panel">
  <h2>افزودن بخش جدید</h2>
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <input type="hidden" name="action" value="add">
    <label>عنوان</label><input name="title" maxlength="200" required>
    <label>متن</label><textarea name="content" rows="6" maxlength="8000"></textarea>
    <button class="btn-sm" style="margin-top:12px" type="submit">افزودن</button>
  </form>
</div>
{% for s in sections %}
<div class="panel">
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <input type="hidden" name="action" value="update">
    <input type="hidden" name="section_id" value="{{ s.id }}">
    <label>عنوان</label><input name="title" value="{{ s.title }}" required maxlength="200">
    <label>متن</label><textarea name="content" rows="6" maxlength="8000">{{ s.content }}</textarea>
    <button class="btn-sm" style="margin-top:12px" type="submit">ذخیره تغییرات</button>
  </form>
  <p>وضعیت: <span class="badge {{ 'done' if s.is_visible else 'cancelled' }}">{{ 'نمایش داده می‌شود' if s.is_visible else 'مخفی است' }}</span></p>
  <div class="row-actions" data-x>
    {% for act,label,cls in [('up','↑ بالا','light'),('down','↓ پایین','light'),('toggle','مخفی/نمایش','light')] %}
    <form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="hidden" name="action" value="{{ act }}"><input type="hidden" name="section_id" value="{{ s.id }}">
      <button class="btn-sm {{ cls }}" type="submit">{{ label }}</button></form>
    {% endfor %}
    <form method="post" data-confirm><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="hidden" name="action" value="delete"><input type="hidden" name="section_id" value="{{ s.id }}">
      <button class="btn-sm danger" type="submit" data-confirm="این بخش حذف شود؟">حذف</button></form>
  </div>
</div>
{% else %}
<div class="panel"><p>هنوز بخشی اضافه نشده است.</p></div>
{% endfor %}
{% endblock %}"""

CONTACT = """{% extends "admin_base.html" %}
{% block title %}مدیریت ارتباط | آرامش و هنر{% endblock %}
{% block content %}
<div class="panel">
  <h2>افزودن راه ارتباطی</h2>
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <input type="hidden" name="action" value="add">
    <label>نوع ارتباط</label>
    <select name="kind" required>{% for k,v in contact_kinds.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select>
    <label>عنوان نمایشی</label><input name="label" maxlength="60" placeholder="مثلاً صفحه اینستاگرام" required>
    <label>آیدی، شماره یا لینک</label>
    <input name="value" maxlength="200" placeholder="آیدی یا شماره؛ برای «سایر» لینک کامل https://" required>
    <button class="btn-sm" style="margin-top:12px" type="submit">افزودن</button>
  </form>
</div>
{% for c in contacts %}
<div class="panel">
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <input type="hidden" name="action" value="update">
    <input type="hidden" name="id" value="{{ c.id }}">
    <label>نوع ارتباط</label>
    <select name="kind">{% for k,v in contact_kinds.items() %}<option value="{{ k }}" {{ 'selected' if k==c.kind }}>{{ v }}</option>{% endfor %}</select>
    <label>عنوان نمایشی</label><input name="label" value="{{ c.label }}" required maxlength="60">
    <label>آیدی، شماره یا لینک</label><input name="value" value="{{ c.value }}" required maxlength="200">
    <button class="btn-sm" style="margin-top:12px" type="submit">ذخیره تغییرات</button>
  </form>
  <p>وضعیت: <span class="badge {{ 'done' if c.is_visible else 'cancelled' }}">{{ 'نمایش داده می‌شود' if c.is_visible else 'مخفی است' }}</span>
     {% if not c.ok %}<span class="badge cancelled">مقدار نامعتبر؛ در سایت نمایش داده نمی‌شود</span>{% endif %}</p>
  <div class="row-actions">
    {% for act,label in [('up','↑ بالا'),('down','↓ پایین'),('toggle','مخفی/نمایش')] %}
    <form method="post"><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="hidden" name="action" value="{{ act }}"><input type="hidden" name="id" value="{{ c.id }}">
      <button class="btn-sm light" type="submit">{{ label }}</button></form>
    {% endfor %}
    <form method="post" data-confirm><input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="hidden" name="action" value="delete"><input type="hidden" name="id" value="{{ c.id }}">
      <button class="btn-sm danger" type="submit" data-confirm="این راه ارتباطی حذف شود؟">حذف</button></form>
  </div>
</div>
{% else %}
<div class="panel"><p>هنوز راه ارتباطی ثبت نشده است.</p></div>
{% endfor %}
{% endblock %}"""

PASSWORD = """{% extends "admin_base.html" %}
{% block title %}تغییر رمز | آرامش و هنر{% endblock %}
{% block content %}
<div class="panel" style="max-width:520px">
  <h2>تغییر رمز عبور</h2>
  <form method="post">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <label>رمز فعلی</label><input type="password" name="current" required autocomplete="current-password">
    <label>رمز جدید (حداقل ۸ کاراکتر)</label><input type="password" name="new" required minlength="8" autocomplete="new-password">
    <label>تکرار رمز جدید</label><input type="password" name="again" required minlength="8" autocomplete="new-password">
    <button class="btn-sm" style="margin-top:14px" type="submit">ذخیره رمز جدید</button>
  </form>
</div>
{% endblock %}"""

app.jinja_loader = DictLoader({
    "base.html": BASE, "home.html": HOME, "admin_base.html": ADMIN_BASE,
    "login.html": LOGIN, "dash.html": DASH, "requests.html": REQUESTS,
    "about.html": ABOUT, "contact.html": CONTACT, "password.html": PASSWORD,
})


# ----------------------------------------------------------------------
# فایل‌های استاتیک
# ----------------------------------------------------------------------
def _asset(body, mime):
    resp = Response(body, mimetype=mime)
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


@app.route("/assets/style.css")
def asset_css():
    return _asset(CSS, "text/css")


@app.route("/assets/app.js")
def asset_js():
    return _asset(JS, "application/javascript")


# ----------------------------------------------------------------------
# صفحه‌ی اصلی
# ----------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        if request.form.get("website"):  # honeypot
            return redirect(url_for("home"))

        name = request.form.get("name", "").strip()[:100]
        phone = to_ascii_digits(request.form.get("phone", "").strip())[:30]
        subject = request.form.get("subject", "").strip()
        session_type = request.form.get("session_type", "").strip()
        notes = request.form.get("notes", "").strip()[:2000]

        last = session.get("last_submit", 0)
        if time.time() - last < 20:
            session["flash"] = {"kind": "bad", "text": "لطفاً چند لحظه صبر کنید و دوباره تلاش کنید."}
        elif (len(name) >= 3 and PHONE_RE.match(phone)
              and subject in SERVICES and session_type in SESSION_TYPES):
            with db() as conn:
                conn.execute(
                    "INSERT INTO appointments (name, phone, subject, session_type, notes, created_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'new')",
                    (name, phone, subject, session_type, notes,
                     datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")),
                )
            session["last_submit"] = time.time()
            session["flash"] = {"kind": "ok", "text": "درخواست شما با موفقیت ثبت شد. به‌زودی با شما تماس می‌گیریم."}
        else:
            session["flash"] = {"kind": "bad", "text": "لطفاً نام، شماره تماس معتبر، خدمت و نوع جلسه را تکمیل کنید."}
        return redirect(url_for("home") + "#booking")

    with db() as conn:
        about_sections = conn.execute(
            "SELECT title, content FROM about_sections WHERE is_visible = 1 ORDER BY position, id"
        ).fetchall()
        rows = conn.execute(
            "SELECT kind, label, value FROM contact_methods WHERE is_visible = 1 ORDER BY position, id"
        ).fetchall()

    contact_methods = []
    for r in rows:
        url = build_contact_url(r["kind"], r["value"])
        if url:
            contact_methods.append({"label": r["label"], "url": url, "kind": r["kind"]})

    return render_template_string(
        open_template("home.html"),
        services=SERVICES, session_types=SESSION_TYPES, service_cards=SERVICE_CARDS,
        steps=STEPS, faq=FAQ, about_sections=about_sections, contact_methods=contact_methods,
        flash=session.pop("flash", None),
        year=jalali_year(),
    )


def open_template(name):
    # از DictLoader استفاده می‌کنیم؛ این تابع فقط متن قالب را برمی‌گرداند
    return app.jinja_loader.mapping[name]


def jalali_year():
    n = datetime.now()
    return gregorian_to_jalali(n.year, n.month, n.day)[0]


def render(name, **ctx):
    return render_template_string(open_template(name), **ctx)


# ----------------------------------------------------------------------
# مدیریت: ورود و خروج
# ----------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if too_many_attempts(ip):
            error = "تلاش‌های ناموفق زیاد بود. ده دقیقه بعد دوباره امتحان کنید."
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            with db() as conn:
                hashed = get_setting(conn, "admin_password_hash", "")
            ok_user = secrets.compare_digest(username.encode(), ADMIN_USERNAME.encode())
            ok_pass = check_password_hash(hashed, password)
            if ok_user and ok_pass:
                session.clear()
                session["is_admin"] = True
                session.permanent = True
                _attempts.pop(ip, None)
                return redirect(url_for("admin_panel"))
            _attempts.setdefault(ip, []).append(time.time())
            error = "نام کاربری یا رمز عبور اشتباه است."
    return render("login.html", error=error)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("home"))


# ----------------------------------------------------------------------
# مدیریت: داشبورد
# ----------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_panel():
    today = datetime.now().astimezone().date()
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        new = conn.execute("SELECT COUNT(*) FROM appointments WHERE status = 'new'").fetchone()[0]
        online = conn.execute("SELECT COUNT(*) FROM appointments WHERE session_type = 'آنلاین'").fetchone()[0]
        inperson = conn.execute("SELECT COUNT(*) FROM appointments WHERE session_type LIKE 'حضوری%'").fetchone()[0]
        by_day = {r[0]: r[1] for r in conn.execute(
            "SELECT substr(created_at,1,10), COUNT(*) FROM appointments "
            "WHERE created_at IS NOT NULL GROUP BY 1")}

    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    counts = [by_day.get(d.isoformat(), 0) for d in days]
    peak = max(counts + [1])
    chart = []
    for d, c in zip(days, counts):
        _, jm, jd = gregorian_to_jalali(d.year, d.month, d.day)
        chart.append({"count": c, "pct": round(c / peak * 100), "label": fa_digits(f"{jd} {JALALI_MONTHS[jm-1]}")})

    stats = {"total": total, "new": new, "today": by_day.get(today.isoformat(), 0),
             "online": online, "inperson": inperson}
    return render("dash.html", stats=stats, chart=chart, cur="home", message="")


# ----------------------------------------------------------------------
# مدیریت: درخواست‌ها
# ----------------------------------------------------------------------
def _filters():
    q = request.values.get("q", "").strip()
    status = request.values.get("status", "")
    where, params = [], []
    if q:
        like = f"%{q}%"
        where.append("(name LIKE ? OR phone LIKE ? OR notes LIKE ? OR subject LIKE ?)")
        params += [like] * 4
    if status in STATUSES:
        where.append("status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return q, status if status in STATUSES else "", clause, params


@app.route("/admin/requests", methods=["GET", "POST"])
@app.route("/appointments", methods=["GET", "POST"])
@admin_required
def admin_requests():
    message, bad = "", False
    if request.method == "POST":
        ids = [int(i) for i in request.form.getlist("selected_ids") if i.isdigit()]
        action = request.form.get("action", "")
        if not ids:
            message, bad = "لطفاً حداقل یک درخواست را انتخاب کنید.", True
        else:
            ph = ",".join("?" for _ in ids)
            with db() as conn:
                if action == "bulk_delete":
                    cur = conn.execute(f"DELETE FROM appointments WHERE id IN ({ph})", ids)
                    message = f"{fa_digits(cur.rowcount)} درخواست حذف شد."
                elif action == "bulk_status":
                    sv = request.form.get("status_value", "")
                    if sv in STATUSES:
                        cur = conn.execute(f"UPDATE appointments SET status = ? WHERE id IN ({ph})", [sv] + ids)
                        message = f"وضعیت {fa_digits(cur.rowcount)} درخواست تغییر کرد."

    q, status, clause, params = _filters()
    page = max(request.args.get("page", 1, type=int), 1)
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM appointments {clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM appointments {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [PER_PAGE, (page - 1) * PER_PAGE],
        ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        num = re.sub(r"[^0-9+]", "", to_ascii_digits(d["phone"]))
        d["tel"] = "tel:" + num
        d["wa"] = build_contact_url("whatsapp", d["phone"])
        d["status"] = d.get("status") or "new"
        items.append(d)

    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
    return render("requests.html", rows=items, total=total, q=q, status=status,
                  page=page, pages=pages, cur="req", message=message, bad=bad)


def _csv_safe(v):
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v


@app.route("/admin/export.csv")
@admin_required
def export_csv():
    q, status, clause, params = _filters()
    with db() as conn:
        rows = conn.execute(
            f"SELECT id, name, phone, subject, session_type, status, created_at, notes "
            f"FROM appointments {clause} ORDER BY id DESC", params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["شماره", "نام", "تلفن", "خدمت", "نوع جلسه", "وضعیت", "زمان ثبت", "توضیحات"])
    for r in rows:
        w.writerow([r["id"], _csv_safe(r["name"]), _csv_safe(r["phone"]), r["subject"],
                    r["session_type"] or "", STATUSES.get(r["status"], r["status"]),
                    r["created_at"] or "", _csv_safe(r["notes"])])
    data = "\ufeff" + buf.getvalue()
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=requests.csv"})


# ----------------------------------------------------------------------
# مدیریت: ترتیب‌دهی عمومی
# ----------------------------------------------------------------------
def move_item(conn, table, item_id, direction):
    ids = [r[0] for r in conn.execute(f"SELECT id FROM {table} ORDER BY position, id")]
    if item_id not in ids:
        return
    i = ids.index(item_id)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for pos, _id in enumerate(ids, start=1):
        conn.execute(f"UPDATE {table} SET position = ? WHERE id = ?", (pos, _id))


# ----------------------------------------------------------------------
# مدیریت: درباره من
# ----------------------------------------------------------------------
@app.route("/admin/about", methods=["GET", "POST"])
@admin_required
def manage_about():
    message = ""
    with db() as conn:
        if request.method == "POST":
            action = request.form.get("action", "")
            sid = request.form.get("section_id", type=int)
            title = request.form.get("title", "").strip()[:200]
            content = request.form.get("content", "").strip()[:8000]

            if action == "add":
                if title:
                    pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM about_sections").fetchone()[0]
                    conn.execute(
                        "INSERT INTO about_sections (title, content, position, is_visible) VALUES (?,?,?,1)",
                        (title, content, pos))
                    message = "بخش جدید اضافه شد."
                else:
                    message = "عنوان را وارد کنید."
            elif action == "update" and sid and title:
                conn.execute("UPDATE about_sections SET title=?, content=? WHERE id=?", (title, content, sid))
                message = "تغییرات ذخیره شد."
            elif action == "toggle" and sid:
                conn.execute("UPDATE about_sections SET is_visible = 1 - is_visible WHERE id=?", (sid,))
                message = "وضعیت نمایش تغییر کرد."
            elif action in ("up", "down") and sid:
                move_item(conn, "about_sections", sid, action)
                message = "ترتیب تغییر کرد."
            elif action == "delete" and sid:
                conn.execute("DELETE FROM about_sections WHERE id=?", (sid,))
                message = "بخش حذف شد."
        sections = conn.execute(
            "SELECT id, title, content, is_visible FROM about_sections ORDER BY position, id").fetchall()
    return render("about.html", sections=sections, message=message, cur="about")


# ----------------------------------------------------------------------
# مدیریت: ارتباط با ما
# ----------------------------------------------------------------------
@app.route("/admin/contact", methods=["GET", "POST"])
@admin_required
def manage_contact():
    message, bad = "", False
    with db() as conn:
        if request.method == "POST":
            action = request.form.get("action", "")
            cid = request.form.get("id", type=int)
            if action in ("add", "update"):
                kind = request.form.get("kind", "").strip()
                label = request.form.get("label", "").strip()[:60]
                value = request.form.get("value", "").strip()[:200]
                if kind not in CONTACT_KINDS or not label or not value:
                    message, bad = "نوع، عنوان و مقدار را کامل وارد کنید.", True
                elif not build_contact_url(kind, value):
                    message, bad = "مقدار واردشده برای این نوع ارتباط معتبر نیست.", True
                elif action == "add":
                    pos = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM contact_methods").fetchone()[0]
                    conn.execute(
                        "INSERT INTO contact_methods (kind,label,value,position,is_visible) VALUES (?,?,?,?,1)",
                        (kind, label, value, pos))
                    message = "راه ارتباطی اضافه شد."
                elif cid:
                    conn.execute("UPDATE contact_methods SET kind=?, label=?, value=? WHERE id=?",
                                 (kind, label, value, cid))
                    message = "تغییرات ذخیره شد."
            elif action == "toggle" and cid:
                conn.execute("UPDATE contact_methods SET is_visible = 1 - is_visible WHERE id=?", (cid,))
                message = "وضعیت نمایش تغییر کرد."
            elif action in ("up", "down") and cid:
                move_item(conn, "contact_methods", cid, action)
                message = "ترتیب تغییر کرد."
            elif action == "delete" and cid:
                conn.execute("DELETE FROM contact_methods WHERE id=?", (cid,))
                message = "راه ارتباطی حذف شد."
        rows = conn.execute(
            "SELECT id, kind, label, value, is_visible FROM contact_methods ORDER BY position, id").fetchall()
    contacts = []
    for r in rows:
        d = dict(r)
        d["ok"] = bool(build_contact_url(d["kind"], d["value"]))
        contacts.append(d)
    return render("contact.html", contacts=contacts, message=message, bad=bad, cur="contact")


@app.errorhandler(400)
def bad_request(e):
    return render_template_string(
        "<meta charset=utf-8><body dir=rtl style='font-family:Tahoma;padding:40px;text-align:center'>"
        "<h2>{{ m }}</h2><a href='/'>بازگشت به صفحه اصلی</a></body>",
        m=getattr(e, "description", "درخواست نامعتبر")), 400


@app.errorhandler(404)
def not_found(e):
    return render_template_string(
        "<meta charset=utf-8><body dir=rtl style='font-family:Tahoma;padding:40px;text-align:center'>"
        "<h2>صفحه‌ای که دنبالش بودید پیدا نشد</h2><a href='/'>بازگشت به صفحه اصلی</a></body>"), 404


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", 5000)), debug=False)
