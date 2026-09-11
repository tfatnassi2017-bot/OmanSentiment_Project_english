# -*- coding: utf-8 -*-
"""
Omani Dialect Sentiment Data Collector
---------------------------------------
Two-role web app:
  - Participants: open "/", write a dialect sentence, self-tag its sentiment.
  - Admin: open "/admin", log in with a password, see live stats and export CSV.

Storage: PostgreSQL (NOT local SQLite). This is required if you deploy to
Render's free tier (or most PaaS free tiers) — their filesystem is ephemeral,
so a local SQLite file gets wiped on every restart/redeploy/idle-spin-down.
A free hosted Postgres (e.g. https://neon.tech, https://supabase.com) fixes
that: your data lives independently of the app's compute instance.

Run locally:
    pip install -r requirements.txt
    export DATABASE_URL="postgresql://user:pass@host/dbname"   # from Neon/Supabase
    export ADMIN_PASSWORD="choose-a-strong-password"
    export FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    python app.py

Then open http://localhost:5000  (participant form)
and    http://localhost:5000/admin  (admin login)

See README.md for deployment instructions.
"""

import csv
import io
import os
import re
import time
from datetime import datetime
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, send_file, g
)

# ---------------------------------------------------------------
# Config
# ---------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-only-insecure-key-change-me")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Get a free Postgres connection string from "
        "https://neon.tech or https://supabase.com and set it as an "
        "environment variable before running this app."
    )

if ADMIN_PASSWORD == "changeme123":
    print("!" * 70)
    print("WARNING: Using the default admin password. Set ADMIN_PASSWORD")
    print("as an environment variable before deploying this publicly.")
    print("!" * 70)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Very simple in-memory rate limiter: max N submissions per IP per window.
# Resets on server restart. Good enough to blunt casual spam/bot abuse;
# swap for Flask-Limiter + Redis if you need something more robust.
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_SUBMISSIONS = 5
_submission_log = {}  # ip -> list of timestamps

# ---------------------------------------------------------------
# Topics: (key, label, prompt)
# NOTE: keys must be unique — the database groups/validates by this key,
# so two different questions must never share one. ("Generalities" below
# is split into three distinct keys for exactly this reason.)
# ---------------------------------------------------------------
# Each topic: (key, label_en, prompt_en, label_ar, prompt_ar)
TOPICS = [
    ("traffic", "Traffic & Driving", "How was traffic and driving for you today or yesterday?", "الطريق والسواقة", "كيف كانت السواقة والطريق عندك اليوم أو أمس؟"),
    ("work", "Work", "How's work or your studies going lately?", "الشغل", "كيف ماشي وضع الشغل أو الدراسة عندك هالفترة؟"),
    ("health", "Health", "Tell us about a recent experience at a hospital, clinic, or doctor's visit.", "الصحة", "شاركنا تجربة أخيرة مع مستشفى أو عيادة أو دكتور."),
    ("Traditions", "Customs & Traditions", "Do you think customs and traditions are still important today?", "العادات و التقاليد", "تشوف العادات والتقاليد لسا مهمة إلى الحين؟"),
    ("government", "Government Services", "How was your most recent government transaction?", "الخدمات الحكومية", "آخر معاملة سويتها بجهة حكومية، كيف كانت؟"),
    ("tourism", "Tourism & Travel", "Tell us about a trip or place you visited recently.", "السياحة والسفر", "خبرني عن رحلة أو مكان زرته أخيراً."),
    ("sports", "Sports", "Share your opinion on a recent match or sports activity.", "الرياضة", "شاركنا رأيك بمباراة أو نشاط رياضي أخير."),
    ("Generalities_generations", "General: Generations", "What do you think about today's generation compared to your uncle Hamid's generation?", "عموميات", "شنو رايك بجيل اليوم مقارنة بجيل عمك حميد؟"),
    ("wedding_social", "Social Occasions", "Tell us about a wedding, gathering, or occasion you attended recently.", "المناسبات الاجتماعية", "خبرني عن عرس أو عزيمة أو مناسبة حضرتها أخيراً."),
    ("Prices", "Prices", "Do you feel prices have risen a lot in Oman lately?", "الأسعار", "تحس الأسعار زادت وايد في عمان هالفترة؟"),
    ("sea_fishing", "Sea & Fishing", "If you fish or go to the sea, tell us about your last experience.", "البحر والصيد", "إذا تصيد أو تروح البحر، خبرني عن آخر تجربة."),
    ("agriculture", "Agriculture", "If you have a farm or palm trees, tell us about this year's season.", "الزراعة", "إذا عندك مزرعة أو نخل، خبرني عن الموسم هالسنة."),
    ("finance", "Money & Banking", "Tell us about a recent experience with a bank or a specific expense.", "الفلوس والبنك", "خبرني عن تجربة أخيرة مع البنك أو مصرف معين."),
    ("entertainment", "Entertainment", "Tell us about the last movie, show, or event you attended — how was it?", "الترفيه", "آخر فيلم أو برنامج أو فعالية حضرتها، كيف كانت؟"),
    ("khareef_dhofar", "Salalah Khareef (Monsoon)", "If you visited Salalah during khareef season, tell us about the experience.", "خريف صلالة", "إذا زرت صلالة وقت الخريف، خبرني عن التجربة."),
    ("Work Mode", "Work Mode", "Do you feel more productive working online or in person?", "طريقة العمل", "تحس نفسك أنتج وانت تداوم أونلاين ولا حضوري؟"),
    ("environment_dust", "Environment & Dust", "How's the air quality or cleanliness in your area lately?", "البيئة والغبار", "كيف جودة الهوا أو النظافة بمنطقتك هالأيام؟"),
    ("parking_fines", "Traffic Violations", "If you got a parking ticket or fine, tell us about it.", "المخالفات المرورية", "إذا صارت لك مخالفة أو غرامة، خبرني عن الموقف."),
    ("banking_apps", "Banking Apps", "How's your experience with your bank's app?", "تطبيقات البنوك", "كيف تجربتك مع تطبيق بنكك؟"),
    ("real_estate", "Housing & Real Estate", "If you're looking for a house or apartment, tell us about your latest search.", "السكن والعقار", "إذا تدور بيت أو شقة، خبرني عن آخر بحث سويته."),
    ("Generalities_opportunities", "General: Opportunities", "Do you think Omani youth have enough opportunities to succeed, or do they face obstacles?", "عموميات", "تشوف الشاب العماني عنده فرص كافية للنجاح ولا يواجه عوايق؟"),
    ("Generalities_quality_of_life", "General: Quality of Life", "Do you think life in Oman is comfortable and easy?", "عموميات", "تشوف الحياة في عمان مريحة وسهلة؟"),
    ("fitness_gym", "Fitness & Gym", "How's your experience with the gym or your workout routine lately?", "الرياضة واللياقة", "كيف تجربتك مع النادي أو التمرين هالفترة؟"),
    ("beach_outings", "Beach Outings", "Tell us about your last outing or trip to the beach.", "الفسحة والشاطئ", "خبرني عن آخر فسحة أو نزهة عالبحر."),
    ("delivery_apps", "Delivery Apps", "How was your last delivery order experience?", "تطبيقات التوصيل", "كيف كانت آخر تجربة توصيل طلب لك؟"),
    ("customer_service", "Customer Service", "Tell us about a recent experience with a company's customer service.", "خدمة العملاء", "خبرني عن تجربة أخيرة مع خدمة عملاء شركة."),
    ("utilities", "Water & Electricity Bills", "How are your utility bills or services this month?", "فواتير الماء والكهرباء", "كيف وضع الفواتير أو الخدمات عندك هالشهر؟"),
    ("floods_rain", "Rain & Flooding", "Have you had any flooding lately? How has the rain been for you?", "الأمطار والسيول", "عساكم سيلتوا هالأيام؟ شلون كانت حالة الأمطار عندكم؟"),
    ("stray_livestock", "Stray Livestock", "What do you think about people who let their livestock roam freely on others' land without supervision?", "الحلال السايبة", "ويش رايك في اللي يربي حلال ويسرحه في أراضي الناس بلا رقيب؟"),
    ("youth_behavior_public", "Behavior in Public Places", "Have you visited a tourist spot and seen youth behavior there you didn't like?", "تصرفات بالأماكن العامة", "جالك رحت مكان سياحي وشفت فيه شي ما عجبك من تصرفات بعض الشباب؟"),
    ("kids_school_break", "Kids' School Break", "Did you enjoy the kids' school break, or was it exhausting for you?", "إجازة الصغار", "هين سُرتوا في إجازة الصغار، ولا طلعت متعبة عليكم؟"),
    ("public_transport", "Public Transportation", "What do you think about the state of public transportation in Oman?", "المواصلات العامة", "ويش رايك في وضع المواصلات العامة في عمان؟"),
    ("rent_costs", "Rent Costs", "How do you see housing rents these days — reasonable or expensive?", "إيجارات السكن", "شلون شايف إيجارات البيوت والشقق هالأيام، معقولة ولا غالية؟"),
    ("hospital_wait", "Hospital Wait Times", "Have you had long waits for appointments at a government hospital or health center?", "الانتظار في المستشفيات", "طولت عليك المواعيد أو الانتظار في مستشفى أو مركز صحي حكومي؟"),
    ("neighbor_noise", "Neighbor Disputes", "Have you had a noise disturbance or dispute with your neighbors?", "مشاكل مع الجيران", "صار عندك موقف إزعاج أو خلاف مع جيرانك بسبب الضوضاء أو شي ثاني؟"),
    ("crowd_mall_eid", "Mall Crowds Before Eid", "How crowded was the market or mall before Eid this year?", "الزحمة في السوق قبل العيد", "شلون كانت الزحمة في السوق أو المول قبل العيد هالسنة؟"),
    ("crowd_airport", "Airport Crowds", "Have you seen crowding at Muscat airport during peak travel season? How was your experience there?", "الزحمة في المطار", "شفت زحمة في مطار مسقط وقت موسم السفر؟ كيف كانت تجربتك هناك؟"),
    ("crowd_stadium", "Stadium Crowds", "How was the crowd atmosphere when you attended a match?", "زحمة المدرجات بالمباريات", "كيف كان جو الزحمة وأنت بالمدرجات وقت حضورك مباراة؟"),
    ("crowd_beach_weekend", "Weekend Beach Crowds", "How crowded was the beach or park on your last weekend outing?", "زحمة الشاطئ نهاية الأسبوع", "كيف كانت زحمة الشاطئ أو المنتزه آخر نهاية أسبوع رحت فيها؟"),
    ("crowd_bank_queue", "Bank Queues", "Did you wait a long time in a bank queue or at an ATM recently?", "طابور البنك", "طولت عليك المدة في طابور البنك أو عند الصراف الآلي آخر مرة؟"),
    ("crowd_mosque_prayer", "Friday/Taraweeh Prayer Crowds", "How crowded was Friday prayer or Taraweeh at your mosque lately?", "زحمة صلاة الجمعة أو التراويح", "كيف زحمة صلاة الجمعة أو التراويح في مسجدكم هالفترة؟"),
    ("fish_market_cleanliness", "Fish Market Organization", "What do you think about your local fish market's organization and cleanliness?", "سوق السمك: التنظيم والنظافة", "يش رأيك بسوق السمك بمنطقتك من ناحية التنظيم والنظافة؟"),
    ("livestock_prices", "Livestock Prices", "What do you think about sheep/livestock prices?", "أسعار الأغنام", "إيش رأيك اسعار الأغنام؟"),
    ("public_park", "Public Park", "When was the last time you visited a public park, and what did you think of it?", "الحديقة العامة", "متى آخر مرة رحت الحديقة العامه وهيش رأيك فيها؟"),
    ("qarangashoh_tradition", "Qarangashoh Tradition", "Give us your impression of the Qarangashoh tradition in your area.", "عادة القرنقشوه", "اعطيني إنطباعك عن عادة القرنقشوه في منطقتك؟"),
    ("school_canteen_food", "School Canteen Food", "What's your honest opinion of the food sold to students at school canteens?", "أكل المدارس (الجمعية)", "أكل الطلاب في المدارس بالجمعية شو رأيك فيه بصراحة؟"),
    ("wedding_costs", "Wedding Costs", "What do you think about wedding costs in your city?", "تكاليف الأعراس", "شو رأيك بتكاليف العرس في مدينتك؟"),
    ("social_relations_change", "Social Relations Over Time", "How do you feel social relationships are now compared to 15 years ago?", "العلاقات الاجتماعية عبر الزمن", "كيف تحس بالعلاقات الإجتماعية بهذا الوقت مقارنة بالفترة قبل ١٥ سنة؟"),
    ("bad_habits_recent", "Recently Noticed Bad Habits", "Tell us about any bad habits you've noticed recently, and give us your honest opinion.", "عادات سيئة ملاحظة مؤخراً", "كلمني عن العادات السيئة اللي لاحظتها في الفترة الأخيرة وعطيني رأيك فيها بصراحة؟"),
    ("tiktok_opinion", "TikTok", "What's your opinion of TikTok — do you feel it's a waste of time and promotes bad habits?", "برنامج التيك توك", "شو رأيك ببرنامج التك تك هل تحس إنه مضيعه للوقت ويعلم عادت ليست طيبة؟"),
    ("salary_sufficiency", "Salary Sufficiency", "How do you feel about your salary — is it enough to last until the end of the month?", "كفاية الراتب", "كيف تحس الراتب وهل يكفيك لآخر الشهر؟"),
    ("friends_habit_dislike", "A Habit You Dislike in Friends", "What's a habit in your friends that you've never liked and couldn't change?", "عادة ما تحبها بأصدقائك", "شنهي العادة اللي ما حبيتها بأصدقاءك للحين وما قدرت تغيرها؟"),
    ("khanbasha_salty", "Khanbasha (Salted)", "Give us your honest opinion of khanbasha (salted).", "خنباشة المالح", "عطيني رأيك في خنباشة المالح بصراحة؟"),
    ("youth_heritage_interest", "Youth Interest in Heritage", "How do you feel about young people's interest in heritage, customs, and traditions these days?", "اهتمام الشباب بالموروث", "كيف تحس وضع الشباب من حيث الإهتمام بالموروث من العادات والتقاليد بهذي الفترة؟"),
    ("haircut_trends", "Haircut Trends", "What do you think about the haircut trends popular these days?", "تقليعات قصات الشعر", "شو رأيك بتقليعات حلاقة الشعر اللي منتشرة هالأيام؟"),
    ("wilayat_market_turnout", "Wilayat Market Turnout", "Last time you went to the wilayat market, how was people's turnout for shopping, and why do you think that is?", "إقبال الناس على سوق الولاية", "آخر مرة رحت سوق الولاية كيف حسيت اقبال الناس على التسوق وهيش السبب باعتقادك؟"),
    ("chinese_cars_opinion", "Chinese Cars", "What's your honest opinion of Chinese cars?", "السيارات الصينية", "السيارات الصينية شو رايك فيها بصراحة؟"),
    ("marriage_rate_decline", "Declining Marriage Rate", "Do you feel marriage rates have declined recently? What do you think the reason is?", "انخفاض نسبة الزواج", "ما تحس أن الزواج قل في الفترة الأخيرة ممكن تعطيني رأيك في السبب."),
    ("fish_prices_rising", "Rising Fish Prices", "Why do you think fish prices have gotten so high lately?", "ارتفاع أسعار السمك", "كيف اسعار السمك هالفترة ليش صارت مرتفعة كثير؟"),
    ("pigeon_keeping_neighborhood", "Pigeon Keeping in Neighborhoods", "What's your opinion of people who keep pigeons among houses in the neighborhood and let them roam freely?", "تربية الحمام وسط الأحياء", "ويش رأيك باللي يربي الحمام بالحارة وسط بيوت الخلق ومخلنهن يسرحن وحدهن؟"),
]

REGIONS = [
    ("muscat", "Muscat", "مسقط"),
    ("dhofar", "Dhofar", "ظفار"),
    ("musandam", "Musandam", "مسندم"),
    ("al_buraimi", "Al Buraimi", "البريمي"),
    ("ad_dakhiliyah", "Ad Dakhiliyah", "الداخلية"),
    ("north_al_batinah", "North Al Batinah", "شمال الباطنة"),
    ("south_al_batinah", "South Al Batinah", "جنوب الباطنة"),
    ("north_ash_sharqiyah", "North Ash Sharqiyah", "شمال الشرقية"),
    ("south_ash_sharqiyah", "South Ash Sharqiyah", "جنوب الشرقية"),
    ("ad_dhahirah", "Ad Dhahirah", "الظاهرة"),
    ("al_wusta", "Al Wusta", "الوسطى"),
    ("prefer_not_to_say", "Prefer not to say", "أُفضّل عدم التحديد"),
]
REGION_KEYS = {r[0] for r in REGIONS}

# Build lookup structures from TOPICS itself, so editing the list above is
# the only thing you ever need to do — nothing else needs to change in sync.
TOPIC_BY_KEY = {t[0]: t for t in TOPICS}
TOPIC_KEYS = set(TOPIC_BY_KEY.keys())

assert len(TOPIC_KEYS) == len(TOPICS), (
    "Duplicate topic keys found in TOPICS — every key must be unique, "
    "even if two questions share the same display label."
)


def region_for(topic_key: str) -> str:
    return "Dhofar (Salalah)" if topic_key == "khareef_dhofar" else "Oman - General/Northern"


# ---------------------------------------------------------------
# Database helpers (PostgreSQL)
# ---------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS responses (
                    id SERIAL PRIMARY KEY,
                    sentence TEXT NOT NULL,
                    question TEXT NOT NULL,
                    sentiment TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    region TEXT NOT NULL,
                    style TEXT NOT NULL DEFAULT 'community_submitted',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            # Safe migration for an already-deployed table: adds the new
            # column without touching any existing rows (they'll simply have
            # NULL here, since region wasn't collected before this update —
            # worth noting in your methodology that pre-migration rows lack
            # self-reported region).
            cur.execute("""
                ALTER TABLE responses
                ADD COLUMN IF NOT EXISTS participant_region TEXT
            """)
    conn.close()


# ---------------------------------------------------------------
# Validation
# ---------------------------------------------------------------
ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


def is_mostly_arabic(text: str) -> bool:
    # Disabled for this English review copy: the supervisor may type test
    # answers in English while verifying the flow. The production Arabic
    # app keeps the real check (see that repo's app.py) — this override
    # exists only so an English test sentence doesn't get wrongly rejected
    # here as "not Arabic."
    return True


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def rate_limited(ip: str) -> bool:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    timestamps = [t for t in _submission_log.get(ip, []) if t > window_start]
    _submission_log[ip] = timestamps
    if len(timestamps) >= RATE_LIMIT_MAX_SUBMISSIONS:
        return True
    timestamps.append(now)
    _submission_log[ip] = timestamps
    return False


# ---------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------
def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------
# Participant routes
# ---------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", topics=TOPICS, regions=REGIONS)


@app.route("/submit", methods=["POST"])
def submit():
    ip = client_ip()
    if rate_limited(ip):
        return jsonify({"ok": False, "error": "rate_limited",
                         "message": "Too many submissions in a short time — please wait a moment."}), 429

    data = request.get_json(silent=True) or {}
    sentence = (data.get("sentence") or "").strip()
    sentiment = (data.get("sentiment") or "").strip()
    topic = (data.get("topic") or "").strip()
    participant_region = (data.get("participant_region") or "").strip()

    if len(sentence) < 6 or len(sentence) > 500:
        return jsonify({"ok": False, "error": "bad_length",
                         "message": "Please write a sentence of reasonable length (6-500 characters)."}), 400
    if not is_mostly_arabic(sentence):
        return jsonify({"ok": False, "error": "not_arabic",
                         "message": "Please write in Arabic (Omani dialect)."}), 400
    if sentiment not in {"positive", "negative", "neutral"}:
        return jsonify({"ok": False, "error": "bad_sentiment",
                         "message": "Please select the sentiment of your answer."}), 400
    if topic not in TOPIC_KEYS:
        return jsonify({"ok": False, "error": "bad_topic",
                         "message": "Invalid topic."}), 400
    if participant_region not in REGION_KEYS:
        return jsonify({"ok": False, "error": "bad_region",
                         "message": "Please agree to the consent statement and select a region first."}), 400

    # Trust the server's own copy of the question text for this topic key,
    # rather than whatever the client sent — the client can't be trusted
    # to send the exact current wording, and this keeps it tamper-proof.
    question_text = TOPIC_BY_KEY[topic][2]

    db = get_db()
    with db:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO responses (sentence, question, sentiment, topic, region, style, participant_region) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (sentence, question_text, sentiment, topic, region_for(topic),
                 "community_submitted", participant_region),
            )
            cur.execute("SELECT COUNT(*) FROM responses")
            total = cur.fetchone()[0]

    return jsonify({"ok": True, "total": total})


@app.route("/count")
def count():
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM responses")
        total = cur.fetchone()[0]
    return jsonify({"total": total})


# ---------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password and password == ADMIN_PASSWORD:
            session["is_admin"] = True
            next_url = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_url)
        error = "Incorrect password."
        time.sleep(1)  # slow down brute-force guessing
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT COUNT(*) AS c FROM responses")
        total = cur.fetchone()["c"]

        cur.execute("SELECT sentiment, COUNT(*) AS c FROM responses GROUP BY sentiment")
        sentiment_counts = {"positive": 0, "negative": 0, "neutral": 0}
        for row in cur.fetchall():
            sentiment_counts[row["sentiment"]] = row["c"]

        cur.execute("SELECT topic, COUNT(*) AS c FROM responses GROUP BY topic ORDER BY c DESC")
        by_topic = cur.fetchall()

        cur.execute("""
            SELECT COALESCE(participant_region, 'not_collected') AS participant_region, COUNT(*) AS c
            FROM responses GROUP BY participant_region ORDER BY c DESC
        """)
        by_region = cur.fetchall()

        cur.execute(
            "SELECT id, sentence, question, sentiment, topic, participant_region, created_at FROM responses "
            "ORDER BY id DESC LIMIT 30"
        )
        recent = cur.fetchall()

    # topic_labels maps key -> display label, built fresh from TOPICS every
    # time, so it always reflects your current list even for old rows saved
    # under a topic you've since renamed the label of.
    topic_labels = {t[0]: t[1] for t in TOPICS}
    region_labels = {r[0]: r[1] for r in REGIONS}
    region_labels["not_collected"] = "Not recorded (before region field was added)"

    return render_template(
        "admin_dashboard.html",
        total=total,
        sentiment_counts=sentiment_counts,
        by_topic=by_topic,
        by_region=by_region,
        topic_labels=topic_labels,
        region_labels=region_labels,
        recent=recent,
    )


@app.route("/admin/export.csv")
@admin_required
def admin_export_csv():
    db = get_db()
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, sentence, question, sentiment, topic, region, participant_region, style, created_at "
            "FROM responses ORDER BY id ASC"
        )
        rows = cur.fetchall()

    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM so Excel opens Arabic text correctly
    writer = csv.writer(buf)
    writer.writerow(["id", "sentence", "question", "sentiment", "topic", "region",
                      "participant_region", "style", "created_at"])
    for r in rows:
        writer.writerow([r["id"], r["sentence"], r["question"], r["sentiment"],
                          r["topic"], r["region"], r["participant_region"], r["style"], r["created_at"]])

    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    filename = f"omani_dialect_submissions_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)


@app.route("/admin/delete/<int:response_id>", methods=["POST"])
@admin_required
def admin_delete(response_id):
    db = get_db()
    with db:
        with db.cursor() as cur:
            cur.execute("DELETE FROM responses WHERE id = %s", (response_id,))
    return redirect(url_for("admin_dashboard"))


# ---------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
else:
    init_db()
