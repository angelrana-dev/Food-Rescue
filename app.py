"""
FoodRescue - Food Surplus Rescue Platform
Flask + SQLite. Run:  python app.py   (then open http://127.0.0.1:5000)
"""
import hashlib
import math
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.datastructures import MultiDict
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "food_rescue.db"))
DEFAULT_CENTER = (18.5204, 73.8567)          # map centre when no location is known
NOTIFY_RADIUS_KM = 10                        # receivers within this range get alerted
FMT = "%Y-%m-%d %H:%M:%S"
FORM_FMT = "%Y-%m-%dT%H:%M"

DONOR_TYPES = ["Restaurant", "Hotel", "Event organizer", "Caterer", "Canteen / mess"]
RECEIVER_TYPES = ["NGO", "Shelter", "Community kitchen", "Orphanage / old-age home"]
CATEGORIES = ["Veg", "Non-veg", "Vegan", "Bakery", "Packaged"]
UNITS = ["meals", "plates", "kg", "packets"]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-before-deploying")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('donor','receiver')),
  org_type TEXT NOT NULL,
  phone TEXT, address TEXT, lat REAL, lng REAL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  donor_id INTEGER NOT NULL REFERENCES users(id),
  title TEXT NOT NULL, description TEXT,
  category TEXT NOT NULL,
  quantity_total INTEGER NOT NULL,
  quantity_left INTEGER NOT NULL,
  unit TEXT NOT NULL,
  address TEXT NOT NULL, lat REAL, lng REAL,
  prepared_at TEXT, expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'available',   -- available|reserved|completed|expired|cancelled
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  listing_id INTEGER NOT NULL REFERENCES listings(id),
  receiver_id INTEGER NOT NULL REFERENCES users(id),
  quantity INTEGER NOT NULL, note TEXT,
  status TEXT NOT NULL DEFAULT 'pending',     -- pending|approved|picked_up|rejected|cancelled|expired
  pickup_code TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  message TEXT NOT NULL, link TEXT,
  is_read INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_requests_listing ON requests(listing_id);
CREATE INDEX IF NOT EXISTS idx_requests_receiver ON requests(receiver_id);
"""


# ----------------------------------------------------------------- helpers
def now():
    return datetime.now()


def now_s():
    return now().strftime(FMT)


def parse_dt(s):
    return datetime.strptime(s, FMT)


def to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def haversine(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()
    db.close()


def notify(db, user_id, message, link=None):
    db.execute("INSERT INTO notifications(user_id,message,link,created_at) VALUES(?,?,?,?)",
               (user_id, message, link, now_s()))


# ------------------------------------------------- expiry + status engine
def expire_listings(db):
    """Automatic expiry: any listing past its expiry time is closed and its open requests expire."""
    ts = now_s()
    rows = db.execute("SELECT id, title FROM listings WHERE status IN ('available','reserved') "
                      "AND expires_at <= ?", (ts,)).fetchall()
    for l in rows:
        db.execute("UPDATE listings SET status='expired' WHERE id=?", (l["id"],))
        open_reqs = db.execute("SELECT id, receiver_id FROM requests WHERE listing_id=? "
                               "AND status IN ('pending','approved')", (l["id"],)).fetchall()
        for r in open_reqs:
            db.execute("UPDATE requests SET status='expired', updated_at=? WHERE id=?", (ts, r["id"]))
            notify(db, r["receiver_id"], f"“{l['title']}” expired before pickup.", url_for_safe("dashboard"))
    if rows:
        db.commit()
    return len(rows)


def url_for_safe(endpoint, **kw):
    try:
        return url_for(endpoint, **kw)
    except RuntimeError:                 # called from background thread (no app context)
        return {"dashboard": "/dashboard"}.get(endpoint, "/")


def refresh_listing(db, lid):
    """Recompute listing status from its remaining quantity and open requests."""
    l = db.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
    if not l or l["status"] in ("cancelled", "expired"):
        return
    if l["quantity_left"] > 0:
        status = "available"
    else:
        approved = db.execute("SELECT COUNT(*) FROM requests WHERE listing_id=? AND status='approved'",
                              (lid,)).fetchone()[0]
        if approved:
            status = "reserved"
        else:
            status = "completed"
            for r in db.execute("SELECT id, receiver_id FROM requests WHERE listing_id=? AND status='pending'",
                                (lid,)).fetchall():
                db.execute("UPDATE requests SET status='rejected', updated_at=? WHERE id=?", (now_s(), r["id"]))
                notify(db, r["receiver_id"], f"“{l['title']}” was fully allocated to others.", "/dashboard")
    db.execute("UPDATE listings SET status=? WHERE id=?", (status, lid))


def _expiry_loop():
    while True:
        try:
            db = sqlite3.connect(DB_PATH, timeout=10)
            db.row_factory = sqlite3.Row
            expire_listings(db)
            db.close()
        except Exception as exc:          # keep the worker alive
            print("expiry worker:", exc)
        time.sleep(60)


# ------------------------------------------------------------------ auth
def current_user():
    if "user" not in g:
        uid = session.get("uid")
        g.user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None
    return g.user


def login_required(role=None):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                flash("Please log in to continue.", "info")
                return redirect(url_for("login", next=request.path))
            if role and u["role"] != role:
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["CENTER"] = DEFAULT_CENTER


@app.before_request
def before():
    if request.endpoint == "static":
        return
    expire_listings(get_db())
    if request.method == "POST" and request.form.get("csrf_token") != session.get("csrf"):
        abort(400, "Your session expired. Go back, refresh the page and try again.")


@app.context_processor
def inject():
    u = current_user()
    unread = last_nid = 0
    if u:
        unread, last_nid = get_db().execute(
            "SELECT COALESCE(SUM(is_read=0),0), COALESCE(MAX(id),0) FROM notifications WHERE user_id=?",
            (u["id"],)).fetchone()
    return dict(me=u, unread=unread, last_nid=last_nid, DONOR_TYPES=DONOR_TYPES, RECEIVER_TYPES=RECEIVER_TYPES,
                CATEGORIES=CATEGORIES, UNITS=UNITS)


@app.template_filter("secs_left")
def secs_left(s):
    return max(0, int((parse_dt(s) - now()).total_seconds()))


@app.template_filter("timeleft")
def timeleft(s):
    sec = secs_left(s)
    if sec <= 0:
        return "Expired"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


@app.template_filter("nice")
def nice(s):
    return parse_dt(s).strftime("%d %b, %I:%M %p") if s else ""


# ----------------------------------------------------------- query helpers
def search_listings(args, user=None):
    """Location-based discovery: filter, compute distance (haversine) and sort."""
    db = get_db()
    q = (args.get("q") or "").strip()
    cat = args.get("category") or ""
    sort = args.get("sort") or "nearest"
    radius = args.get("radius", type=float)
    lat, lng = args.get("lat", type=float), args.get("lng", type=float)
    if lat is None or lng is None:
        lat = lng = None
        if user and user["lat"] is not None and user["lng"] is not None:
            lat, lng = user["lat"], user["lng"]
    sql = ("SELECT l.*, u.name AS donor_name, u.org_type FROM listings l "
           "JOIN users u ON u.id=l.donor_id WHERE l.status='available' AND l.expires_at > ?")
    params = [now_s()]
    if q:
        sql += " AND (l.title LIKE ? OR l.description LIKE ? OR u.name LIKE ?)"
        params += [f"%{q}%"] * 3
    if cat:
        sql += " AND l.category=?"
        params.append(cat)
    rows = [dict(r) for r in db.execute(sql, params)]
    for r in rows:
        r["distance"] = (haversine(lat, lng, r["lat"], r["lng"])
                         if lat is not None and r["lat"] is not None else None)
    if radius and lat is not None:
        rows = [r for r in rows if r["distance"] is not None and r["distance"] <= radius]
    far = 1e9
    if sort == "expiry":
        rows.sort(key=lambda r: r["expires_at"])
    elif sort == "newest":
        rows.sort(key=lambda r: -r["id"])
    elif sort == "quantity":
        rows.sort(key=lambda r: -r["quantity_left"])
    else:
        rows.sort(key=lambda r: r["distance"] if r["distance"] is not None else far)
    return rows, lat, lng


def impact_stats(db):
    return dict(
        meals=db.execute("SELECT COALESCE(SUM(quantity),0) FROM requests WHERE status='picked_up'").fetchone()[0],
        active=db.execute("SELECT COUNT(*) FROM listings WHERE status='available'").fetchone()[0],
        donors=db.execute("SELECT COUNT(*) FROM users WHERE role='donor'").fetchone()[0],
        receivers=db.execute("SELECT COUNT(*) FROM users WHERE role='receiver'").fetchone()[0],
    )


# ------------------------------------------------------------------ pages
@app.route("/")
def index():
    db = get_db()
    rows, _, _ = search_listings(MultiDict({"sort": "expiry"}), current_user())
    return render_template("index.html", stats=impact_stats(db), listings=rows[:6])


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f, db, errors = request.form, get_db(), []
        name, email = f.get("name", "").strip(), f.get("email", "").strip().lower()
        role, org = f.get("role"), f.get("org_type")
        if not name:
            errors.append("Enter your organisation name.")
        if "@" not in email:
            errors.append("Enter a valid email address.")
        if len(f.get("password", "")) < 6:
            errors.append("Password must be at least 6 characters.")
        if role not in ("donor", "receiver") or org not in (DONOR_TYPES if role == "donor" else RECEIVER_TYPES):
            errors.append("Choose your account type and organisation type.")
        if not f.get("address", "").strip():
            errors.append("Enter your address.")
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            errors.append("An account with this email already exists. Log in instead.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", form=f)
        cur = db.execute(
            "INSERT INTO users(name,email,password_hash,role,org_type,phone,address,lat,lng,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, email, generate_password_hash(f["password"]), role, org, f.get("phone", "").strip(),
             f.get("address", "").strip(), to_float(f.get("lat")), to_float(f.get("lng")), now_s()))
        notify(db, cur.lastrowid, "Welcome to FoodRescue! Pin your location in your profile to get alerts about nearby food."
               if role == "receiver" else "Welcome to FoodRescue! Post your first surplus listing to alert nearby NGOs.",
               "/dashboard")
        db.commit()
        session.clear()
        session["uid"] = cur.lastrowid
        flash(f"Welcome to FoodRescue, {name}!", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    """Log in with any email + password. A new email creates the account automatically."""
    if request.method == "POST":
        db = get_db()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u and not check_password_hash(u["password_hash"], pw):
            flash("This email is already registered with a different password. Try again.", "error")
        elif not u and ("@" not in email or len(pw) < 6):
            flash("Enter a valid email and a password of at least 6 characters.", "error")
        else:
            created = False
            if not u:
                role = "donor" if request.form.get("role") == "donor" else "receiver"
                name = email.split("@")[0].replace(".", " ").replace("_", " ").title()
                cur = db.execute(
                    "INSERT INTO users(name,email,password_hash,role,org_type,phone,address,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (name, email, generate_password_hash(pw), role, "Restaurant" if role == "donor" else "NGO",
                     "", "Not set yet. Update in your profile", now_s()))
                notify(db, cur.lastrowid, "Welcome to FoodRescue! Add your organisation name and pin your location in Profile.",
                       "/profile")
                db.commit()
                u = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
                created = True
            session.clear()
            session["uid"] = u["id"]
            if created:
                flash("New account created. Add your organisation name and location to get started.", "success")
                return redirect(url_for("profile"))
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
@login_required()
def profile():
    u = current_user()
    if request.method == "POST":
        f = request.form
        if not f.get("name", "").strip() or not f.get("address", "").strip():
            flash("Name and address are required.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET name=?, phone=?, address=?, lat=?, lng=? WHERE id=?",
                       (f["name"].strip(), f.get("phone", "").strip(), f["address"].strip(),
                        to_float(f.get("lat")), to_float(f.get("lng")), u["id"]))
            db.commit()
            flash("Profile saved.", "success")
            return redirect(url_for("profile"))
    return render_template("profile.html")


@app.route("/browse")
def browse():
    rows, lat, lng = search_listings(request.args, current_user())
    markers = [dict(id=r["id"], title=r["title"], lat=r["lat"], lng=r["lng"], donor=r["donor_name"],
                    left=r["quantity_left"], unit=r["unit"]) for r in rows if r["lat"] is not None]
    return render_template("browse.html", listings=rows, markers=markers, lat=lat, lng=lng,
                           center=(lat, lng) if lat is not None else DEFAULT_CENTER, args=request.args)


@app.route("/api/listings")
def api_listings():
    rows, lat, lng = search_listings(request.args, current_user())
    return jsonify(count=len(rows), origin=dict(lat=lat, lng=lng), listings=[
        {k: r[k] for k in ("id", "title", "category", "quantity_left", "unit", "address", "lat", "lng",
                           "expires_at", "donor_name", "distance")} for r in rows])


@app.route("/listing/new", methods=["GET", "POST"])
@login_required("donor")
def listing_new():
    u, f = current_user(), request.form
    if request.method == "POST":
        errors = []
        title = f.get("title", "").strip()
        try:
            qty = int(f.get("quantity", "0"))
        except ValueError:
            qty = 0
        try:
            prepared = datetime.strptime(f.get("prepared_at", ""), FORM_FMT)
            expires = datetime.strptime(f.get("expires_at", ""), FORM_FMT)
        except ValueError:
            prepared = expires = None
        if not title:
            errors.append("Give the food a title.")
        if qty < 1:
            errors.append("Quantity must be at least 1.")
        if f.get("category") not in CATEGORIES or f.get("unit") not in UNITS:
            errors.append("Choose a category and unit.")
        if not expires or not prepared:
            errors.append("Enter when the food was prepared and when it expires.")
        elif expires <= now():
            errors.append("Expiry time must be in the future.")
        elif prepared >= expires:
            errors.append("Preparation time must be before the expiry time.")
        if not f.get("address", "").strip():
            errors.append("Enter the pickup address.")
        if not f.get("safe"):
            errors.append("Confirm that the food is fresh and safe to eat.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            db = get_db()
            lat, lng = to_float(f.get("lat")), to_float(f.get("lng"))
            cur = db.execute(
                "INSERT INTO listings(donor_id,title,description,category,quantity_total,quantity_left,unit,"
                "address,lat,lng,prepared_at,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (u["id"], title, f.get("description", "").strip(), f["category"], qty, qty, f["unit"],
                 f["address"].strip(), lat, lng, prepared.strftime(FMT), expires.strftime(FMT), now_s()))
            lid = cur.lastrowid
            if lat is not None:                       # alert nearby receiving organisations
                for r in db.execute("SELECT id,lat,lng FROM users WHERE role='receiver' AND lat IS NOT NULL"):
                    if haversine(lat, lng, r["lat"], r["lng"]) <= NOTIFY_RADIUS_KM:
                        notify(db, r["id"], f"New food nearby: {title} ({qty} {f['unit']}) from {u['name']}.",
                               url_for("listing_detail", lid=lid))
            db.commit()
            flash("Your surplus food is now live.", "success")
            return redirect(url_for("listing_detail", lid=lid))
    return render_template("listing_form.html", form=f if request.method == "POST" else {},
                           now_local=now().strftime(FORM_FMT),
                           exp_local=(now() + timedelta(hours=4)).strftime(FORM_FMT))


@app.route("/listing/<int:lid>")
def listing_detail(lid):
    db, u = get_db(), current_user()
    l = db.execute("SELECT l.*, u.name AS donor_name, u.org_type, u.phone AS donor_phone FROM listings l "
                   "JOIN users u ON u.id=l.donor_id WHERE l.id=?", (lid,)).fetchone()
    if not l:
        abort(404)
    reqs, my_req, distance = None, None, None
    if u and u["id"] == l["donor_id"]:
        reqs = db.execute("SELECT r.*, x.name AS receiver_name, x.org_type AS receiver_type, x.phone AS receiver_phone "
                          "FROM requests r JOIN users x ON x.id=r.receiver_id WHERE r.listing_id=? "
                          "ORDER BY r.id DESC", (lid,)).fetchall()
    elif u and u["role"] == "receiver":
        my_req = db.execute("SELECT * FROM requests WHERE listing_id=? AND receiver_id=? ORDER BY id DESC LIMIT 1",
                            (lid, u["id"])).fetchone()
    if u and u["lat"] is not None and l["lat"] is not None:
        distance = haversine(u["lat"], u["lng"], l["lat"], l["lng"])
    return render_template("listing_detail.html", l=l, reqs=reqs, my_req=my_req, distance=distance)


@app.post("/listing/<int:lid>/request")
@login_required("receiver")
def request_food(lid):
    db, u = get_db(), current_user()
    l = db.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
    if not l:
        abort(404)
    try:
        qty = int(request.form.get("quantity", "0"))
    except ValueError:
        qty = 0
    dup = db.execute("SELECT 1 FROM requests WHERE listing_id=? AND receiver_id=? AND status IN ('pending','approved')",
                     (lid, u["id"])).fetchone()
    if l["status"] != "available" or l["expires_at"] <= now_s():
        flash("This listing is no longer available.", "error")
    elif dup:
        flash("You already have an open request for this listing.", "error")
    elif qty < 1 or qty > l["quantity_left"]:
        flash(f"Request between 1 and {l['quantity_left']} {l['unit']}.", "error")
    else:
        db.execute("INSERT INTO requests(listing_id,receiver_id,quantity,note,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?)",
                   (lid, u["id"], qty, request.form.get("note", "").strip(), now_s(), now_s()))
        notify(db, l["donor_id"], f"{u['name']} requested {qty} {l['unit']} of “{l['title']}”.",
               url_for("dashboard"))
        db.commit()
        flash("Request sent. The donor will approve it shortly.", "success")
    return redirect(url_for("listing_detail", lid=lid))


@app.post("/request/<int:rid>/<action>")
@login_required()
def request_action(rid, action):
    db, u = get_db(), current_user()
    r = db.execute("SELECT r.*, l.donor_id, l.title, l.status AS lstatus, l.quantity_left, l.unit "
                   "FROM requests r JOIN listings l ON l.id=r.listing_id WHERE r.id=?", (rid,)).fetchone()
    if not r:
        abort(404)
    if action in ("approve", "reject", "pickup"):
        if u["id"] != r["donor_id"]:
            abort(403)
    elif action == "cancel":
        if u["id"] != r["receiver_id"]:
            abort(403)
    else:
        abort(404)
    ts = now_s()
    if action == "approve":
        if r["status"] != "pending":
            flash("This request has already been handled.", "error")
        elif r["lstatus"] != "available" or r["quantity"] > r["quantity_left"]:
            flash("Not enough quantity is left to approve this request.", "error")
        else:
            code = f"{secrets.randbelow(10000):04d}"
            db.execute("UPDATE requests SET status='approved', pickup_code=?, updated_at=? WHERE id=?", (code, ts, rid))
            db.execute("UPDATE listings SET quantity_left=quantity_left-? WHERE id=?", (r["quantity"], r["listing_id"]))
            notify(db, r["receiver_id"], f"Approved: {r['quantity']} {r['unit']} of “{r['title']}”. "
                                         f"Your pickup code is {code}.", url_for("dashboard"))
            flash("Request approved. Quantity reserved for the receiver.", "success")
    elif action == "reject":
        if r["status"] == "pending":
            db.execute("UPDATE requests SET status='rejected', updated_at=? WHERE id=?", (ts, rid))
            notify(db, r["receiver_id"], f"Your request for “{r['title']}” was declined.", url_for("dashboard"))
            flash("Request declined.", "info")
    elif action == "cancel":
        if r["status"] in ("pending", "approved"):
            if r["status"] == "approved":                       # release reserved quantity
                db.execute("UPDATE listings SET quantity_left=quantity_left+? WHERE id=?",
                           (r["quantity"], r["listing_id"]))
            db.execute("UPDATE requests SET status='cancelled', updated_at=? WHERE id=?", (ts, rid))
            notify(db, r["donor_id"], f"{u['name']} cancelled their request for “{r['title']}”.", url_for("dashboard"))
            flash("Request cancelled.", "info")
    elif action == "pickup":
        if r["status"] != "approved":
            flash("Only approved requests can be marked as picked up.", "error")
        elif request.form.get("code", "").strip() != r["pickup_code"]:
            flash("That pickup code does not match. Ask the receiver to show their code.", "error")
        else:
            db.execute("UPDATE requests SET status='picked_up', updated_at=? WHERE id=?", (ts, rid))
            notify(db, r["receiver_id"], f"Pickup confirmed for “{r['title']}”. Thank you for rescuing food!",
                   url_for("dashboard"))
            flash("Pickup confirmed. Meals rescued!", "success")
    refresh_listing(db, r["listing_id"])
    db.commit()
    return redirect(request.referrer or url_for("dashboard"))


@app.post("/listing/<int:lid>/cancel")
@login_required("donor")
def listing_cancel(lid):
    db, u = get_db(), current_user()
    l = db.execute("SELECT * FROM listings WHERE id=? AND donor_id=?", (lid, u["id"])).fetchone()
    if not l:
        abort(404)
    if l["status"] in ("available", "reserved"):
        db.execute("UPDATE listings SET status='cancelled' WHERE id=?", (lid,))
        for r in db.execute("SELECT id, receiver_id FROM requests WHERE listing_id=? AND status IN ('pending','approved')",
                            (lid,)).fetchall():
            db.execute("UPDATE requests SET status='cancelled', updated_at=? WHERE id=?", (now_s(), r["id"]))
            notify(db, r["receiver_id"], f"“{l['title']}” was withdrawn by the donor.", url_for("dashboard"))
        db.commit()
        flash("Listing withdrawn.", "info")
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required()
def dashboard():
    db, u = get_db(), current_user()
    if u["role"] == "donor":
        listings = db.execute("SELECT l.*, (SELECT COUNT(*) FROM requests r WHERE r.listing_id=l.id AND r.status='pending') "
                              "AS pending FROM listings l WHERE donor_id=? ORDER BY l.id DESC", (u["id"],)).fetchall()
        reqs = db.execute("SELECT r.*, l.title, l.unit, x.name AS receiver_name, x.org_type AS receiver_type, "
                          "x.phone AS receiver_phone FROM requests r JOIN listings l ON l.id=r.listing_id "
                          "JOIN users x ON x.id=r.receiver_id WHERE l.donor_id=? AND r.status IN ('pending','approved') "
                          "ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.id DESC", (u["id"],)).fetchall()
        stats = dict(
            posted=len(listings),
            active=sum(1 for l in listings if l["status"] in ("available", "reserved")),
            pending=sum(1 for r in reqs if r["status"] == "pending"),
            rescued=db.execute("SELECT COALESCE(SUM(r.quantity),0) FROM requests r JOIN listings l ON l.id=r.listing_id "
                               "WHERE l.donor_id=? AND r.status='picked_up'", (u["id"],)).fetchone()[0])
        return render_template("donor_dashboard.html", listings=listings, reqs=reqs, stats=stats)
    reqs = db.execute("SELECT r.*, l.title, l.unit, l.address, l.expires_at, l.id AS lid, d.name AS donor_name, "
                      "d.phone AS donor_phone FROM requests r JOIN listings l ON l.id=r.listing_id "
                      "JOIN users d ON d.id=l.donor_id WHERE r.receiver_id=? ORDER BY r.id DESC", (u["id"],)).fetchall()
    stats = dict(
        total=len(reqs),
        pending=sum(1 for r in reqs if r["status"] == "pending"),
        approved=sum(1 for r in reqs if r["status"] == "approved"),
        rescued=sum(r["quantity"] for r in reqs if r["status"] == "picked_up"))
    return render_template("receiver_dashboard.html", reqs=reqs, stats=stats)


@app.route("/notifications")
@login_required()
def notifications():
    db, u = get_db(), current_user()
    if not db.execute("SELECT 1 FROM notifications WHERE user_id=?", (u["id"],)).fetchone():
        notify(db, u["id"], "Alerts are live. New food nearby, approvals and pickups will appear here instantly.", "/dashboard")
        db.commit()
    items = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],)).fetchall()
    html = render_template("notifications.html", items=items)
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (u["id"],))
    db.commit()
    return html



@app.route("/api/poll")
def api_poll():
    """Real-time updates: the browser polls this every few seconds."""
    db, u = get_db(), current_user()
    parts = [str(r[:]) for r in db.execute("SELECT id,status,quantity_left FROM listings ORDER BY id")]
    out = dict(new=[], unread=0, last=request.args.get("since", 0, type=int))
    if u:
        if u["role"] == "donor":
            q = ("SELECT r.id,r.status FROM requests r JOIN listings l ON l.id=r.listing_id "
                 "WHERE l.donor_id=? ORDER BY r.id")
        else:
            q = "SELECT id,status FROM requests WHERE receiver_id=? ORDER BY id"
        parts += [str(r[:]) for r in db.execute(q, (u["id"],))]
        rows = db.execute("SELECT id,message,link,created_at FROM notifications WHERE user_id=? AND id>? ORDER BY id",
                          (u["id"], out["last"])).fetchall()
        out["new"] = [dict(id=r["id"], message=r["message"], link=r["link"] or "/dashboard",
                           time=nice(r["created_at"])) for r in rows]
        if rows:
            out["last"] = rows[-1]["id"]
        if request.args.get("mark"):
            db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (u["id"],))
            db.commit()
        out["unread"] = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
                                   (u["id"],)).fetchone()[0]
    out["fp"] = hashlib.md5("|".join(parts).encode()).hexdigest()
    return jsonify(out)

@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def error(e):
    return render_template("error.html", e=e), e.code


init_db()

if __name__ == "__main__":
    threading.Thread(target=_expiry_loop, daemon=True).start()
    app.run(debug=True, use_reloader=False, port = 5001)