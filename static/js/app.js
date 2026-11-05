(function () {
  // Live countdowns: every .timer[data-secs] ticks down once per second.
  var timers = document.querySelectorAll('.timer[data-secs]');
  function fmt(s) {
    if (s <= 0) return 'Expired';
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
    if (d) return d + 'd ' + h + 'h';
    if (h) return h + 'h ' + m + 'm';
    return m + 'm ' + (sec < 10 ? '0' : '') + sec + 's';
  }
  setInterval(function () {
    timers.forEach(function (t) {
      var s = Math.max(0, parseInt(t.dataset.secs, 10) - 1);
      t.dataset.secs = s;
      t.textContent = (t.dataset.icon || '') + fmt(s);
      t.classList.toggle('urgent', s < 7200);
    });
  }, 1000);

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  // ---- Real-time updates: poll every 5 s for alerts and data changes
  var body = document.body, since = parseInt(body.dataset.since || '0', 10), fp = null;
  var path = location.pathname, onAlerts = path === '/notifications';
  var reloadable = path === '/' || path === '/browse' || path === '/dashboard' || (/^\/listing\/\d+$/.test(path));
  function toast(n) {
    var a = document.createElement('a'); a.className = 'toast'; a.href = n.link;
    a.innerHTML = esc(n.message) + '<small>Just now</small>';
    document.getElementById('toasts').appendChild(a);
    setTimeout(function () { a.remove(); }, 8000);
  }
  function busy() { var e = document.activeElement; return e && /INPUT|TEXTAREA|SELECT/.test(e.tagName) && e.closest('main'); }
  function poll() {
    if (document.hidden) return;
    fetch('/api/poll?since=' + since + (onAlerts ? '&mark=1' : ''), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        since = d.last;
        var dot = document.getElementById('dot');
        if (dot) { dot.textContent = d.unread; dot.hidden = !d.unread; }
        d.new.forEach(function (n) {
          if (onAlerts) {
            var box = document.getElementById('alerts'), e = document.getElementById('no-alerts'); if (e) e.remove();
            var a = document.createElement('a'); a.className = 'notif new'; a.href = n.link;
            a.innerHTML = esc(n.message) + '<small>' + esc(n.time) + '</small>'; box.insertBefore(a, box.firstChild);
          } else toast(n);
        });
        if (fp === null) fp = d.fp;
        else if (fp !== d.fp) { fp = d.fp; if (reloadable && !busy()) location.reload(); }
      }).catch(function () {});
  }
  setInterval(poll, 5000); poll();

  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!confirm(f.dataset.confirm)) e.preventDefault(); });
  });

  // "Use my location" buttons fill #lat / #lng (and optionally submit the form)
  document.querySelectorAll('[data-geo]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (!navigator.geolocation) { alert('Location is not supported by this browser.'); return; }
      btn.disabled = true; var old = btn.textContent; btn.textContent = 'Locating…';
      navigator.geolocation.getCurrentPosition(function (p) {
        document.getElementById('lat').value = p.coords.latitude.toFixed(6);
        document.getElementById('lng').value = p.coords.longitude.toFixed(6);
        if (window.setPick) window.setPick(p.coords.latitude, p.coords.longitude, true);
        if (btn.dataset.submit) { btn.closest('form').submit(); return; }
        btn.disabled = false; btn.textContent = old;
      }, function () {
        alert('Could not get your location. Allow location access or click on the map instead.');
        btn.disabled = false; btn.textContent = old;
      });
    });
  });

  // Register: organisation types depend on account type
  var role = document.querySelectorAll('input[name=role]'), org = document.getElementById('org_type');
  if (role.length && org) {
    var types = JSON.parse(org.dataset.types);
    var fill = function () {
      var r = document.querySelector('input[name=role]:checked'); if (!r) return;
      var keep = org.dataset.selected || org.value;
      org.innerHTML = types[r.value].map(function (t) { return '<option' + (t === keep ? ' selected' : '') + '>' + t + '</option>'; }).join('');
    };
    role.forEach(function (r) { r.addEventListener('change', function () { org.dataset.selected = ''; fill(); }); });
    fill();
  }

  if (typeof L === 'undefined') return;          // Leaflet not loaded (offline): maps are optional
  function tiles(map) {
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap' }).addTo(map);
  }

  var pm = document.getElementById('pickmap');
  if (pm) {
    var lat = parseFloat(document.getElementById('lat').value), lng = parseFloat(document.getElementById('lng').value);
    var has = !isNaN(lat) && !isNaN(lng);
    var map = L.map(pm).setView(has ? [lat, lng] : [+pm.dataset.lat, +pm.dataset.lng], has ? 14 : 11);
    tiles(map);
    var marker = null;
    window.setPick = function (a, b, pan) {
      if (marker) marker.setLatLng([a, b]); else {
        marker = L.marker([a, b], { draggable: true }).addTo(map);
        marker.on('dragend', function () { var c = marker.getLatLng(); window.setPick(c.lat, c.lng); });
      }
      document.getElementById('lat').value = a.toFixed(6); document.getElementById('lng').value = b.toFixed(6);
      if (pan) map.setView([a, b], 15);
    };
    if (has) window.setPick(lat, lng);
    map.on('click', function (e) { window.setPick(e.latlng.lat, e.latlng.lng); });
  }

  var bm = document.getElementById('browsemap');
  if (bm) {
    var markers = JSON.parse(document.getElementById('markers').textContent);
    var m = L.map(bm).setView([+bm.dataset.lat, +bm.dataset.lng], 12);
    tiles(m);
    var pts = [], me = bm.dataset.me === '1';
    markers.forEach(function (x) {
      L.marker([x.lat, x.lng]).addTo(m).bindPopup('<b>' + esc(x.title) + '</b><br>' + esc(x.donor) + '<br>' + x.left + ' ' + esc(x.unit) + ' left<br><a href="/listing/' + x.id + '">View details</a>');
      pts.push([x.lat, x.lng]);
    });
    if (me) { L.circleMarker([+bm.dataset.lat, +bm.dataset.lng], { radius: 8, color: '#12302b', fillColor: '#f2b134', fillOpacity: 1 }).addTo(m).bindPopup('You are here'); pts.push([+bm.dataset.lat, +bm.dataset.lng]); }
    if (pts.length > 1) m.fitBounds(pts, { padding: [30, 30], maxZoom: 15 });
  }

  var dm = document.getElementById('detailmap');
  if (dm) {
    var d = L.map(dm).setView([+dm.dataset.lat, +dm.dataset.lng], 15); tiles(d);
    L.marker([+dm.dataset.lat, +dm.dataset.lng]).addTo(d);
  }
})();