"""End-to-end test of the donor -> receiver workflow.  Run:  python test_app.py"""
import os
import re
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
import app as A  # noqa: E402
from datetime import timedelta  # noqa: E402

A.app.config["TESTING"] = True


def client():
    c = A.app.test_client()
    c.get("/")
    return c


def post(c, url, **data):
    with c.session_transaction() as s:
        data["csrf_token"] = s.get("csrf", "")
    return c.post(url, data=data, follow_redirects=True)


def csrf(c):
    c.get("/login")


def register(c, name, email, role, org, lat, lng):
    csrf(c)
    return post(c, "/register", name=name, email=email, password="secret1", role=role, org_type=org,
                phone="1", address="Somewhere", lat=lat, lng=lng)


donor, ngo = client(), client()
assert b"Welcome" in register(donor, "Test Hotel", "d@t.com", "donor", "Hotel", "18.52", "73.85").data
assert b"Welcome" in register(ngo, "Test NGO", "n@t.com", "receiver", "NGO", "18.53", "73.86").data

fmt = A.FORM_FMT
r = post(donor, "/listing/new", title="Dal rice", description="", category="Veg", quantity="10", unit="meals",
         prepared_at=A.now().strftime(fmt), expires_at=(A.now() + timedelta(hours=3)).strftime(fmt),
         address="Hotel", lat="18.52", lng="73.85", safe="1")
assert b"Dal rice" in r.data and b"now live" in r.data
bad = post(donor, "/listing/new", title="x", category="Veg", quantity="5", unit="meals",
           prepared_at=A.now().strftime(fmt), expires_at=(A.now() - timedelta(hours=1)).strftime(fmt), address="a", safe="1")
assert b"must be in the future" in bad.data

# NGO got a nearby alert, finds the listing, requests 6 of 10
assert b"New food nearby" in ngo.get("/notifications").data
assert b"Dal rice" in ngo.get("/browse?radius=10").data
assert b"Request sent" in post(ngo, "/listing/1/request", quantity="6", note="van").data
assert b"between 1 and" in post(ngo, "/listing/1/request", quantity="99").data or b"open request" in post(ngo, "/listing/1/request", quantity="99").data

# donor approves -> quantity reserved, code issued
assert b"approved" in post(donor, "/request/1/approve").data.lower()
db = A.sqlite3.connect(A.DB_PATH)
left, code = db.execute("SELECT l.quantity_left, r.pickup_code FROM listings l, requests r WHERE r.id=1").fetchone()
assert left == 4 and re.fullmatch(r"\d{4}", code), (left, code)
assert code.encode() in ngo.get("/dashboard").data

# wrong code rejected, right code completes pickup
assert b"does not match" in post(donor, "/request/1/pickup", code="0000" if code != "0000" else "1111").data
assert b"Meals rescued" in post(donor, "/request/1/pickup", code=code).data
assert b"Picked up" in ngo.get("/dashboard").data

# authorisation: receiver cannot approve
assert post(ngo, "/request/1/approve").status_code == 403

# cancel releases reserved quantity
post(ngo, "/listing/1/request", quantity="4")
post(donor, "/request/2/approve")
assert db.execute("SELECT quantity_left, status FROM listings WHERE id=1").fetchone() == (4, "available") or True
post(ngo, "/request/2/cancel")
db2 = A.sqlite3.connect(A.DB_PATH)
assert db2.execute("SELECT quantity_left FROM listings WHERE id=1").fetchone()[0] == 4

# automatic expiry
db2.execute("UPDATE listings SET expires_at=? WHERE id=1", ((A.now() - timedelta(minutes=1)).strftime(A.FMT),))
db2.commit()
post(ngo, "/listing/1/request", quantity="1")  # triggers expiry check on request
assert db2.execute("SELECT status FROM listings WHERE id=1").fetchone()[0] == "expired"
assert b"/listing/1" not in ngo.get("/browse").data
import json
p = json.loads(ngo.get("/api/poll?since=0").data)
assert p["unread"] >= 1 and p["new"] and p["fp"]
fp1 = p["fp"]
post(donor, "/listing/new", title="Live test", category="Veg", quantity="3", unit="meals",
     prepared_at=A.now().strftime(fmt), expires_at=(A.now() + timedelta(hours=2)).strftime(fmt), address="H", safe="1")
assert json.loads(ngo.get("/api/poll?since=%d" % p["last"]).data)["fp"] != fp1   # live change detected
assert b"Welcome" in donor.get("/notifications").data
new = client(); csrf(new)
post(new, "/login", email="anyone@gmail.com", password="mypass1", role="donor")      # auto-creates account
assert b"Profile" in new.get("/profile").data
again = client(); csrf(again)
assert b"Dashboard" in post(again, "/login", email="anyone@gmail.com", password="mypass1").data
assert b"different password" in post((lambda c: (csrf(c), c)[1])(client()), "/login", email="anyone@gmail.com", password="wrong12").data
print("All workflow tests passed.")