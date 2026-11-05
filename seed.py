"""Load demo data:  python seed.py   (resets the database). All demo passwords: demo123"""
import os
import sqlite3
from datetime import timedelta

from werkzeug.security import generate_password_hash

import app as A

if os.path.exists(A.DB_PATH):
    os.remove(A.DB_PATH)
A.init_db()
db = sqlite3.connect(A.DB_PATH)
pw = generate_password_hash("demo123")
t = A.now()
f = lambda d: d.strftime(A.FMT)
cx, cy = A.DEFAULT_CENTER

users = [  # name, email, role, org_type, phone, address, dlat, dlng
    ("Spice Garden Restaurant", "spice@demo.com", "donor", "Restaurant", "9800000001", "FC Road", .010, .008),
    ("Grand Residency Hotel", "hotel@demo.com", "donor", "Hotel", "9800000002", "Station Road", -.012, .015),
    ("Joyful Events & Catering", "events@demo.com", "donor", "Event organizer", "9800000003", "Banquet Hall, Camp", .004, -.020),
    ("Annapurna Foundation", "ngo@demo.com", "receiver", "NGO", "9800000011", "Kothrud", .020, -.010),
    ("Hope Shelter", "shelter@demo.com", "receiver", "Shelter", "9800000012", "Hadapsar", -.030, .040),
]
for n, e, r, o, ph, ad, dl, dg in users:
    db.execute("INSERT INTO users(name,email,password_hash,role,org_type,phone,address,lat,lng,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (n, e, pw, r, o, ph, ad, cx + dl, cy + dg, f(t)))

def listing(donor, title, desc, cat, total, left, unit, addr, dl, dg, prep_h, exp_h, status="available"):
    db.execute("INSERT INTO listings(donor_id,title,description,category,quantity_total,quantity_left,unit,address,lat,lng,"
               "prepared_at,expires_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (donor, title, desc, cat, total, left, unit, addr, cx + dl, cy + dg,
                f(t - timedelta(hours=prep_h)), f(t + timedelta(hours=exp_h)), status, f(t - timedelta(hours=prep_h))))
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]

listing(1, "Veg biryani with raita", "Cooked at 6 pm, stored hot. Contains dairy.", "Veg", 60, 60, "meals", "Spice Garden, FC Road", .010, .008, 1, 3)
listing(2, "Buffet dinner leftovers", "Dal, rice, 3 sabzis and rotis from a conference dinner.", "Veg", 120, 120, "plates", "Grand Residency, Station Road", -.012, .015, 2, 5)
listing(3, "Wedding reception surplus", "Paneer, pulao, sweets. Sealed in containers.", "Veg", 200, 200, "meals", "Banquet Hall, Camp", .004, -.020, 1, 1.5)
listing(1, "Fresh bread and buns", "Baked this morning, unsold.", "Bakery", 40, 40, "packets", "Spice Garden, FC Road", .010, .008, 6, 10)
listing(2, "Chicken curry and rice", "Kept refrigerated, reheat before serving.", "Non-veg", 50, 50, "meals", "Grand Residency, Station Road", -.012, .015, 1, 4)
# history so the impact numbers are not zero
for donor, title, q in [(1, "Lunch buffet", 80), (2, "Breakfast spread", 45), (3, "Corporate lunch", 110)]:
    lid = listing(donor, title, "Collected earlier.", "Veg", q, 0, "meals", "Demo address", .01, .01, 30, -24, "completed")
    db.execute("INSERT INTO requests(listing_id,receiver_id,quantity,status,pickup_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
               (lid, 4, q, "picked_up", "1234", f(t - timedelta(hours=28)), f(t - timedelta(hours=25))))
db.execute("INSERT INTO requests(listing_id,receiver_id,quantity,note,created_at,updated_at) VALUES(1,4,20,'We can collect by van in 30 minutes',?,?)", (f(t), f(t)))
msgs = [(1, "Annapurna Foundation requested 20 meals of “Veg biryani with raita”."),
        (4, "New food nearby: Veg biryani with raita (60 meals) from Spice Garden Restaurant."),
        (4, "New food nearby: Wedding reception surplus (200 meals) from Joyful Events & Catering."),
        (5, "New food nearby: Buffet dinner leftovers (120 plates) from Grand Residency Hotel."),
        (2, "Welcome to FoodRescue! Post your first surplus listing to alert nearby NGOs.")]
for uid, m in msgs:
    db.execute("INSERT INTO notifications(user_id,message,link,created_at) VALUES(?,?,?,?)", (uid, m, "/dashboard", f(t)))
db.commit()
db.close()
print("Demo data loaded.\n  Donors:    spice@demo.com, hotel@demo.com, events@demo.com\n  Receivers: ngo@demo.com, shelter@demo.com\n  Password:  demo123")