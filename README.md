# FoodRescue: Food Surplus Rescue Platform

A Flask + SQLite web app that connects restaurants, hotels and event organisers (donors) with NGOs and shelters (receivers) so safe surplus food is collected before it expires.

## Run it

```bash
pip install -r requirements.txt
python seed.py        # optional: loads demo data (password for all demo users: demo123)
python app.py         # open http://127.0.0.1:5000
python test_app.py    # end-to-end workflow test
```

Login: any email and password works. A new email creates the account automatically (an existing email needs its own password). The app also updates live every 5 seconds: alerts, dashboards and listings refresh by themselves.

Demo logins: donors `spice@demo.com`, `hotel@demo.com`, `events@demo.com`; receivers `ngo@demo.com`, `shelter@demo.com`.
Maps use Leaflet and OpenStreetMap, so they need an internet connection. Everything else works offline.

## Features

| Requirement | Where it lives |
|---|---|
| Food listing | `/listing/new`: title, category, quantity, unit, prepared/expiry time, pickup pin, food-safety confirmation |
| Quantity tracking | `listings.quantity_left` drops on approval, is restored on cancellation, and drives the progress bar |
| Pickup requests | Receivers request a quantity; donors approve or decline; a 4-digit pickup code is issued on approval |
| Location-based discovery | Haversine distance, radius filter, "near me" via browser GPS, live map, `/api/listings` JSON endpoint |
| Automatic expiry | `expire_listings()` runs on every request and in a 60-second background thread; open requests become `expired` and users are notified |
| Donor workflow | Post → review requests → approve → verify code at pickup → meals counted |
| Receiver workflow | Browse → request → approved (code shown) → collect; timeline shows Posted, Requested, Approved, Picked up |
| Extras | In-app alerts (nearby receivers are notified of new food), CSRF protection, hashed passwords, impact statistics |

## Status lifecycle

- Listing: `available` → `reserved` (all quantity approved) → `completed` (all picked up); or `expired` / `cancelled`
- Request: `pending` → `approved` → `picked_up`; or `rejected` / `cancelled` / `expired`

## Project structure

```
app.py            routes, database schema, expiry engine, search
seed.py           demo data
test_app.py       workflow tests
templates/        Jinja pages (base, dashboards, listing, browse, auth)
static/css, js    styling, countdown timers, maps, geolocation
```

## Before deploying

Set `SECRET_KEY` as an environment variable, run behind gunicorn instead of the Flask dev server, and consider moving the expiry thread to a scheduled job.