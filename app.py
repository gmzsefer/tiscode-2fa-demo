from flask import Flask, request, jsonify, Response, session
import random, os, time, smtplib, ssl, io, wave, csv, datetime, secrets
import hmac, hashlib, base64, struct
from urllib.parse import quote
from email.message import EmailMessage
import numpy as np

app = Flask(__name__)

# Optional redirect mode: a second deployment with REDIRECT_TO set forwards every
# request (path and query included) to the main site. Used for an old address that
# some phones still have cached (tiscode-demo.onrender.com -> tiscode-2fa-demo).
REDIRECT_TO = os.environ.get("REDIRECT_TO", "").rstrip("/")
if REDIRECT_TO:
    from flask import redirect

    @app.before_request
    def forward_everything():
        qs = request.query_string.decode()
        return redirect(REDIRECT_TO + request.path + (("?" + qs) if qs else ""), code=302)

# Login state lives in a signed browser cookie (session), so it survives server restarts.
# The signing key is stored in a local file (git-ignored).
# On the server (Render) the FLASK_SECRET environment variable is used.
if os.environ.get("FLASK_SECRET"):
    app.secret_key = os.environ["FLASK_SECRET"]
else:
    if not os.path.exists(".flask_secret"):
        open(".flask_secret", "w").write(secrets.token_hex(32))
    app.secret_key = open(".flask_secret").read().strip()

# MODE: True = experiment mode (phone selector, "Next trial", "didn't react" button)
#      False = demo mode (real flow: password + 2FA every time, experiment tools hidden)
# Local default: experiment mode. The server sets EXPERIMENT_MODE=false (demo).
EXPERIMENT_MODE = os.environ.get("EXPERIMENT_MODE", "true").lower() == "true"

# Demo account - never hard-coded in the source.
# Server: DEMO_EMAIL / DEMO_PASSWORD environment variables; locally a `.demo_login`
# file (line 1 e-mail, line 2 password, git-ignored).
def load_login():
    if os.environ.get("DEMO_EMAIL") and os.environ.get("DEMO_PASSWORD"):
        return os.environ["DEMO_EMAIL"], os.environ["DEMO_PASSWORD"]
    if os.path.exists(".demo_login"):
        lines = open(".demo_login", encoding="utf-8").read().splitlines()
        return lines[0].strip(), lines[1].strip()
    return "demo@tiscode.test", "tiscode-demo"

EMAIL, PASSWORD = load_login()

# --- E-mail OTP settings ---
# Gmail App Password - never hard-coded in the source.
# Put it on one line in `.smtp_password` in the project folder
# (or the TISCODE_SMTP_PASSWORD environment variable). Without it no e-mail is sent
# and the code is delivered to the phone inbox page instead.
def load_app_password():
    if os.environ.get("TISCODE_SMTP_PASSWORD"):
        return os.environ["TISCODE_SMTP_PASSWORD"].replace(" ", "")
    if os.path.exists(".smtp_password"):
        return open(".smtp_password").read().strip().replace(" ", "")
    return ""

EMAIL_APP_PASSWORD = load_app_password()
OTP_TTL = 300             # code valid for 5 minutes
OTP_MAX_TRIES = 3         # code cancelled after 3 wrong attempts
otp = {"code": None, "start": None, "tries": 0}

# State of the TISCODE login currently waiting for a phone confirmation
waiting = {"confirmed": False, "token": None}

# --- TISCODE audio layout (measured on all files with inspect_sounds.py) ---
# 0-1 s silence | 1-3 s opening marker (OM) | 3-4 s silence | 4-9 s infocore (5 s)
INFOCORE_START = 4.0      # saniye
INFOCORE_MAX = 5          # hoca: maksimum 5 sn
FADE = 0.03               # 30 ms fade-out so the cut does not click

# --- Balanced design: a fixed list chosen at random once ---
# SEQ_LEN sounds are drawn once from all files (fixed seed -> always the same list).
# Every phone x length cell plays the same sounds, in a cell-specific shuffled order,
# so phones and lengths are compared on identical sounds.
SEQ_SEED = 42
SEQ_LEN = 20             # block 1: first 10 sounds, block 2: 10 more sounds
BLOCK = 10
EXP_START = "2026-10-07T04:00"     # start of the real trials (earlier rows = setup tests)

DUPLICATES = {"20_2"}    # 20_2.wav is byte-identical to 20.wav (same md5) -> excluded

# Demo mode (server) plays only the sounds that were most reliable in the trials,
# so only these infotis links need to point to the public site.
DEMO_SOUNDS = os.environ.get("DEMO_SOUNDS", "18,36,7,11,32").split(",")

# ACK RELAY: infotis links point to the public site (/ack); the local experiment
# server polls confirmations from there. Phones can confirm from any network
# (Wi-Fi, 5G) and links survive IP changes. Disabled on the server (demo mode).
ACK_RELAY = os.environ.get("ACK_RELAY", "https://tiscode-2fa-demo.onrender.com" if EXPERIMENT_MODE else "")

def all_sounds():
    return sorted(f[:-4] for f in os.listdir("static") if f.endswith(".wav"))

def sequence():
    first = random.Random(SEQ_SEED).sample(all_sounds(), BLOCK)          # first 10 (unchanged)
    rest = [x for x in all_sounds() if x not in first and x not in DUPLICATES]
    second = random.Random(SEQ_SEED + 1).sample(rest, BLOCK)              # 10 more sounds
    return first + second

def next_in_sequence(phone, dur):
    """Next sound for this phone x length cell, counted from results.csv (survives restarts)."""
    done = 0
    if os.path.exists(RESULTS):
        for r in csv.DictReader(open(RESULTS)):
            if r["time"] < EXP_START or r["method"] != "TISCODE" or r["duration_s"] != str(dur):
                continue
            if phone == "all" and r["phone"].startswith("all#") and r["result"] == "session_end":
                done += 1
            elif (r["phone"] == phone and r["result"] in ("success", "fail")
                  and r.get("attempt", "") in ("", "1")):
                done += 1
    seq = sequence()
    first, second = seq[:BLOCK], seq[BLOCK:]
    random.Random(f"{phone}-{dur}").shuffle(first)       # block 1: same order as before
    random.Random(f"{phone}-{dur}-b").shuffle(second)    # block 2: its own order
    order = first + second                                # each cell: block 1, then block 2
    return order[done % SEQ_LEN], done

# --- Experiment log: one row per trial in results.csv ---
RESULTS = "results.csv"
FIELDS = ["time", "method", "sound", "duration_s", "phone", "result", "elapsed_s", "ack_device", "attempt"]

def log_result(method, sound, duration, phone, result, elapsed, ack_device="", attempt=""):
    # Upgrade the header if an older file lacks new columns (old rows stay empty)
    if os.path.exists(RESULTS):
        rows = list(csv.reader(open(RESULTS)))
        if rows and rows[0] != FIELDS:
            with open(RESULTS, "w", newline="") as f:
                csv.writer(f).writerows([FIELDS] + [r + [""] * (len(FIELDS) - len(r)) for r in rows[1:]])
    new = not os.path.exists(RESULTS)
    with open(RESULTS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FIELDS)
        w.writerow([datetime.datetime.now().isoformat(timespec="seconds"),
                    method, sound, duration, phone, result, elapsed, ack_device, attempt])

# Identify the confirming phone: IP + device info from the browser user agent
def device_id():
    ua = request.headers.get("User-Agent", "")
    model = ua.split("(", 1)[1].split(")", 1)[0] if "(" in ua else ua[:60]
    return f"{request.remote_addr} | {model}"

# Home page: login form
@app.route("/logout")
def logout():
    session.clear()
    return home()

@app.route("/")
def home():
    return open("login.html", encoding="utf-8").read()

# Login form posts here: check the password
@app.route("/login", methods=["POST"])
def login():
    email = request.form["email"]
    password = request.form["password"]
    if email == EMAIL and password == PASSWORD:
        session["logged_in"] = True
        return choose_html()
    else:
        return "<h1>❌ Wrong email or password</h1>"

def choose_html():
    h = open("choose.html", encoding="utf-8").read()
    if not EXPERIMENT_MODE:   # demo: hide the phone selector (experiment tool)
        h = h.replace('<label class="opt">Phone', '<label class="opt" style="display:none">Phone')
        h = h.replace('<label class="opt">Sound order', '<label class="opt" style="display:none">Sound order')
    return h

# Experiment shortcut: after one password login, go straight back to the 2FA choice
@app.route("/choose")
def choose():
    if not session.get("logged_in"):
        return home()
    return choose_html()

# TISCODE chosen: play the sound and wait for the phone confirmation
@app.route("/tiscode")
def tiscode():
    n = request.args.get("n")            # testing: /tiscode?n=5 plays infotis 5
    dur = int(request.args.get("dur", INFOCORE_MAX))   # infocore length in seconds
    dur = max(1, min(INFOCORE_MAX, dur))
    phone = request.args.get("phone", "unknown")
    order = request.args.get("order", "fixed")
    if n:
        chosen = n
    elif order == "fixed" and EXPERIMENT_MODE:      # balanced: next sound of this cell
        chosen, _ = next_in_sequence(phone, dur)
    elif not EXPERIMENT_MODE:                        # demo: random reliable sound
        chosen = random.choice([x for x in DEMO_SOUNDS if x in all_sounds()])
    else:                                            # fully random (like real 2FA)
        chosen = random.choice([x for x in all_sounds() if x not in DUPLICATES])
    if phone == "all":                   # simultaneous test: each play = new session id
        waiting["session"] = waiting.get("session", 0) + 1
        phone = f"all#{waiting['session']}"
    waiting.update(confirmed=False, token=chosen, dur=dur, phone=phone,
                   start=time.time(), acks={}, attempt=1, relay_seen=set())    # timer starts
    return playing_page()

# Not recognised -> play the same sound again. The first try is logged as "fail (attempt 1)";
# the timer keeps running from the first play (the user's total wait).
@app.route("/retry")
def retry():
    if waiting.get("token") and not waiting["confirmed"] and not waiting["phone"].startswith("all#"):
        log_result("TISCODE", waiting["token"], waiting["dur"], waiting["phone"], "fail",
                   round(time.time() - waiting["start"], 1), "", waiting["attempt"])
        waiting["attempt"] += 1
        return playing_page()
    return choose_html()

def playing_page():
    chosen, dur, phone = waiting["token"], waiting["dur"], waiting["phone"]
    html = open("playing.html", encoding="utf-8").read()
    attempt = waiting.get("attempt", 1)
    return (html.replace("SOUND_URL", f"/cut/{chosen}/{dur}?a={attempt}")
                .replace("ATTEMPT", str(attempt))
                .replace("SOUND_NAME", chosen)
                .replace("DUR", str(dur))
                .replace("MODE", "all" if phone.startswith("all#") else "single")
                .replace("❌ Phone didn't react (log as failed)",
                         "❌ Phone didn't react (log as failed)" if EXPERIMENT_MODE
                         else "❌ Not recognised — choose another method"))

# Shorten a sound: keep the OM, cut the infocore after `dur` seconds
@app.route("/cut/<name>/<int:dur>")
def cut(name, dur):
    path = os.path.join("static", os.path.basename(name) + ".wav")
    with wave.open(path) as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    sr, ch = params.framerate, params.nchannels
    dtype = {2: "<i2", 4: "<i4"}[params.sampwidth]
    a = np.frombuffer(frames, dtype=dtype).reshape(-1, ch).astype(np.float64)
    end = int((INFOCORE_START + max(1, min(INFOCORE_MAX, dur))) * sr)
    a = a[:end]
    f = int(FADE * sr)
    a[-f:] *= np.linspace(1, 0, f)[:, None]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams(params)
        out.writeframes(a.astype(dtype).tobytes())
    return Response(buf.getvalue(), mimetype="audio/wav")

# Phone did not react -> log a failed trial
@app.route("/fail")
def fail():
    if waiting.get("token") and waiting["phone"].startswith("all#"):
        log_result("TISCODE", waiting["token"], waiting["dur"], waiting["phone"], "session_end",
                   round(time.time() - waiting["start"], 1), f"{len(waiting['acks'])} phone(s)")
        waiting["token"] = None
    elif waiting.get("token") and not waiting["confirmed"]:
        log_result("TISCODE", waiting["token"], waiting["dur"], waiting["phone"], "fail",
                   round(time.time() - waiting["start"], 1), "", waiting.get("attempt", 1))
        waiting["token"] = None
    from flask import redirect
    return redirect("/choose")

# Register a confirmation at time t (server clock)
def register_ack(dev, t):
    if waiting["phone"] == "test":        # /test page: not logged
        waiting["confirmed"] = True
        return f"<h1>✅ Test OK: infotis {waiting['token']} reached the computer.</h1>"
    if waiting["phone"].startswith("all#"):
        if dev not in waiting["acks"]:     # each phone counted once
            waiting["acks"][dev] = round(t - waiting["start"], 1)
            log_result("TISCODE", waiting["token"], waiting["dur"], waiting["phone"],
                       "success", waiting["acks"][dev], dev)
        return "<h1>✅ Confirmed! Go back to your computer.</h1>"
    if not waiting["confirmed"]:          # never count the same confirmation twice
        waiting["confirmed"] = True
        waiting["elapsed"] = round(t - waiting["start"], 1)
        log_result("TISCODE", waiting["token"], waiting["dur"], waiting["phone"],
                   "success", waiting["elapsed"], dev, waiting.get("attempt", 1))
    return "<h1>✅ Confirmed! Go back to your computer.</h1>"

# The phone opens this link when the user taps the notification.
# Every hit is also kept in relay_log for the local experiment server.
# Known limitation: the link carries no per-login secret, so anyone who calls it while
# a login is pending confirms that login. A real system needs a one-time token.
relay_log = []

@app.route("/ack")
def ack():
    token = request.args.get("token")
    t, dev = time.time(), device_id()
    relay_log.append({"t": t, "dev": dev})
    del relay_log[:-50]
    # a given token must match; without one the pending login is confirmed (demo)
    if waiting.get("token") and (token is None or token == waiting["token"]):
        return register_ack(dev, t)
    if token is not None:
        return "<h1>❌ Wrong or expired token</h1>"
    return "<h1>✅ Confirmation sent. Go back to your computer.</h1>"

# Polled by the local experiment server: confirmations after time t
@app.route("/acks-since")
def acks_since():
    t = float(request.args.get("t", 0))
    r = jsonify(acks=[a for a in relay_log if a["t"] > t])
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r

# Test page: arm a sound as pending (not logged)
@app.route("/arm/<name>")
def arm(name):
    waiting.update(confirmed=False, token=name, phone="test", start=time.time(), acks={}, relay_seen=set())
    return jsonify(ok=True)

# Test page: play every infotis to check which ones the phone recognises
@app.route("/test")
def test():
    def key(x):
        head = x[:-4].split("-")[0].split("_")[0]
        return (int(head) if head.isdigit() else 999, x)
    sounds = sorted([f for f in os.listdir("static") if f.endswith(".wav")], key=key)
    cards = ""
    for s in sounds:
        name = s[:-4]
        cards += (f'<div id="c-{name}" style="margin:8px;padding:12px;border:1px solid #ddd;'
                  f'border-radius:10px;width:230px;text-align:center;background:#fff;">'
                  f'<b style="color:#764ba2;font-size:16px;">infotis {name}</b><br>'
                  f'<audio src="/static/{s}" data-name="{name}" controls style="width:100%;margin-top:6px"></audio>'
                  f'<div class="st" style="font-size:13px;margin-top:4px;color:#888"></div>'
                  f'</div>')
    return (f'<html><head><meta charset="utf-8"><title>Infotis Test</title></head>'
            f'<body style="font-family:sans-serif;padding:20px;background:#f5f5f7">'
            f'<h1>🔊 Infotis Test ({len(sounds)} sounds)</h1>'
            f'<p>Play each sound with the phone nearby; a card turns green when the phone confirms it.</p>'
            f'<div style="display:flex;flex-wrap:wrap">{cards}</div>'
            f'''<script>
let current = null;
document.querySelectorAll("audio").forEach(a => a.addEventListener("play", async () => {{
  document.querySelectorAll("audio").forEach(o => {{ if (o !== a) o.pause(); }});
  current = a.dataset.name;
  await fetch("/arm/" + encodeURIComponent(current));
  document.querySelector("#c-" + CSS.escape(current) + " .st").textContent = "📡 waiting for phone...";
}}));
setInterval(async () => {{
  if (!current) return;
  const d = await (await fetch("/status")).json();
  if (d.confirmed) {{
    const c = document.getElementById("c-" + current);
    c.style.background = "#dcfce7"; c.style.borderColor = "#16a34a";
    c.querySelector(".st").textContent = "✅ notification + link OK";
    current = null;
  }}
}}, 1000);
</script></body></html>''')


# Polled by the playing page: has the phone confirmed?
@app.route("/status")
def status():
    if ACK_RELAY and waiting.get("token") and (not waiting["confirmed"] or waiting["phone"].startswith("all#")):
        try:
            import urllib.request, json as _json
            url = f"{ACK_RELAY}/acks-since?t={waiting['start']}"
            with urllib.request.urlopen(url, timeout=3) as resp:
                for a in _json.load(resp)["acks"]:
                    if a["t"] not in waiting.setdefault("relay_seen", set()):
                        waiting["relay_seen"].add(a["t"])
                        register_ack(a["dev"] + " (relay)", a["t"])
        except Exception as e:
            print("relay error:", e)
    return jsonify(confirmed=waiting["confirmed"], acks=waiting.get("acks", {}))

# Shared success page (time + log-out button)
def success_page(method, seconds):
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Success</title>
<style>body{{font-family:-apple-system,sans-serif;background:linear-gradient(135deg,#667eea,#764ba2);
display:flex;justify-content:center;align-items:center;height:100vh;margin:0}}
.kart{{background:white;padding:40px;border-radius:20px;box-shadow:0 20px 50px rgba(0,0,0,.3);
width:340px;text-align:center}}h1{{color:#16a34a;margin-bottom:6px}}
.info{{color:#555;font-size:14px}}.time{{font-size:24px;color:#764ba2;font-weight:bold;margin:14px 0}}
a{{display:inline-block;margin-top:16px;padding:12px 28px;background:#764ba2;color:white;
text-decoration:none;border-radius:10px;font-weight:bold}}a:hover{{opacity:.9}}</style></head>
<body><div class="kart"><h1>✅ Welcome!</h1>
<p class="info">Login complete via <b>{method}</b></p>
<p class="time">⏱️ {seconds} s</p>
{'<a href="/choose">🔁 Next trial</a>' if EXPERIMENT_MODE else ''}
{'<p style="margin-top:10px"><a href="/logout" style="background:none;color:#888;padding:0;font-weight:normal">🚪 Log out</a></p>' if EXPERIMENT_MODE else '<a href="/logout">🚪 Log out</a>'}
</div></body></html>'''


# TISCODE success page
@app.route("/success")
def success():
    return success_page("TISCODE", waiting.get("elapsed", "?"))


# Experiment summary: success rate and mean time per method / phone / length
@app.route("/results")
def results():
    rows = list(csv.DictReader(open(RESULTS))) if os.path.exists(RESULTS) else []
    groups = {}
    for r in rows:
        if r["phone"].startswith("all#"):
            continue
        groups.setdefault((r["method"], r["phone"], r["duration_s"]), []).append(r)
    table = ""
    for (method, phone, dur), rs in sorted(groups.items()):
        ok = [float(r["elapsed_s"]) for r in rs if r["result"] == "success"]
        avg = f"{sum(ok)/len(ok):.1f}" if ok else "-"
        table += (f"<tr><td>{method}</td><td>{phone}</td><td>{dur}</td><td>{len(rs)}</td>"
                  f"<td>{len(ok)} ({100*len(ok)//len(rs)}%)</td><td>{avg}</td></tr>")
    # Simultaneous test: which devices reacted in each session?
    sessions = {}                                   # length -> number of sessions
    for r in rows:
        if r["result"] == "session_end":
            sessions[r["duration_s"]] = sessions.get(r["duration_s"], 0) + 1
    dev = {}
    for r in rows:
        if r["phone"].startswith("all#") and r["result"] == "success":
            dev.setdefault((r["ack_device"], r["duration_s"]), []).append(float(r["elapsed_s"]))
    table2 = ""
    for (d, dur), ts in sorted(dev.items()):
        n = sessions.get(dur, 0)
        rate = f"{len(ts)}/{n} ({100*len(ts)//n}%)" if n else f"{len(ts)}/?"
        table2 += f"<tr><td>{d}</td><td>{dur}</td><td>{rate}</td></tr>"
    if sessions:
        table2 = (f'<h2>📱📱📱 Simultaneous test (recognition per phone)</h2><table>'
                  f'<tr><th>Phone (IP | model)</th><th>Infocore (s)</th><th>Recognized</th></tr>'
                  f'{table2}</table><p>Sessions per length: {sessions}</p>')
    return (f'<html><head><meta charset="utf-8"><title>Results</title><style>'
            f'body{{font-family:sans-serif;padding:20px}}td,th{{border:1px solid #ccc;padding:6px 12px}}'
            f'table{{border-collapse:collapse}}</style></head><body>'
            f'<h1>📊 Results ({len(rows)} attempts)</h1><table>'
            f'<tr><th>Method</th><th>Phone</th><th>Infocore (s)</th><th>Attempts</th>'
            f'<th>Success</th><th>Avg time (s)</th></tr>{table}</table>'
            f'{table2}<p>Raw data: <code>results.csv</code></p></body></html>')


# ---------- AUTHENTICATOR OTP (TOTP, RFC 6238 — Google Authenticator) ----------
# The phone app and the server share a secret key.
# Every 30 s: code = HMAC-SHA1(secret, time // 30), truncated to 6 digits.
# No network needed: both sides use the same clock, so they compute the same code.
TOTP_STEP = 30
TOTP_FILE = ".totp_secret"        # secret key (git-ignored)

def totp_secret():
    if os.environ.get("TOTP_SECRET"):  # fixed on the server (no new QR scan after each deploy)
        return os.environ["TOTP_SECRET"]
    if not os.path.exists(TOTP_FILE):
        open(TOTP_FILE, "w").write(base64.b32encode(secrets.token_bytes(20)).decode())
    return open(TOTP_FILE).read().strip()

def totp_code(secret, counter):
    key = base64.b32decode(secret)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F                                   # "dynamic truncation"
    num = struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF
    return f"{num % 10**6:06d}"

totp = {"start": None, "last_counter": None}

def card(title, body):
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:-apple-system,sans-serif;background:linear-gradient(135deg,#667eea,#764ba2);
display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}}
.kart{{background:white;padding:40px;border-radius:20px;box-shadow:0 20px 50px rgba(0,0,0,.3);
width:320px;text-align:center}}h1{{color:#764ba2}}p{{color:#666;font-size:14px}}
input{{width:100%;padding:12px;margin:10px 0;border:2px solid #e0e0e0;border-radius:10px;
box-sizing:border-box;font-size:18px;text-align:center;letter-spacing:4px}}
button{{width:100%;padding:13px;background:#10b981;color:white;border:none;border-radius:10px;
font-size:16px;font-weight:bold;cursor:pointer}}#qr{{display:inline-block;margin:10px 0}}
code{{word-break:break-all;background:#f3f4f6;padding:4px 6px;border-radius:6px}}
a{{color:#764ba2;font-size:13px}}</style></head>
<body><div class="kart">{body}</div></body></html>'''

# One-time setup: scan the QR code with an Authenticator app
@app.route("/totp-setup")
def totp_setup():
    secret = totp_secret()
    uri = (f"otpauth://totp/{quote('TISCODE Demo:' + EMAIL)}"
           f"?secret={secret}&issuer=TISCODE%20Demo&digits=6&period={TOTP_STEP}")
    return card("Authenticator setup", f'''<h1>📲 Setup</h1>
<p>Open <b>Google Authenticator</b> (or Microsoft Authenticator) → <b>+</b> → <b>Scan QR code</b></p>
<div id="qr"></div>
<p>Can't scan? Enter this key manually:<br><code>{secret}</code></p>
<a href="/totp">✅ Done → enter a code</a>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>new QRCode(document.getElementById("qr"), {{text: "{uri}", width: 200, height: 200}});</script>''')

# Login: the user types the 6-digit code from the app
@app.route("/totp")
def totp_page(error=""):
    if not error:
        totp["start"] = time.time()                    # timer starts
        totp["phone"] = request.args.get("phone", "")
    err = f"<p style='color:#c62828;font-weight:bold'>{error}</p>" if error else ""
    return card("Authenticator", f'''<h1>🔢 Authenticator</h1>
<p>Enter the 6-digit code from your authenticator app</p>{err}
<form method="post" action="/verify-totp">
<input name="code" placeholder="6-digit code" maxlength="7" inputmode="numeric" autocomplete="one-time-code" autofocus>
<button>Verify</button></form>
<p><a href="/totp-setup">First time? Set up the app</a></p>''')

@app.route("/verify-totp", methods=["POST"])
def verify_totp():
    # Apps show the code as "123 456" -> keep digits only
    entered = "".join(ch for ch in request.form.get("code", "") if ch.isdigit())
    if not totp["start"]:
        return totp_page()
    now = int(time.time()) // TOTP_STEP
    elapsed = round(time.time() - totp["start"], 1)
    # Accept the previous / next 30 s code to tolerate clock drift
    for c in (now - 1, now, now + 1):
        if hmac.compare_digest(entered, totp_code(totp_secret(), c)):
            if c == totp["last_counter"]:              # a code can be used only once
                return totp_page("❌ Code already used. Wait for the next one.")
            totp["last_counter"] = c
            totp["start"] = None
            log_result("Authenticator OTP", "", "", totp.get("phone", ""), "success", elapsed)
            return success_page("Authenticator OTP", elapsed)
    log_result("Authenticator OTP", "", "", totp.get("phone", ""), "wrong_code", elapsed)
    return totp_page("❌ Wrong code, try again.")


# ---------- OTP CODE (e-mail, or phone inbox page as fallback) ----------
# Generate a 6-digit code, deliver it, ask the user to type it.
@app.route("/email-otp")
def email_otp():
    code = f"{secrets.randbelow(10**6):06d}"   # cryptographically random 6-digit code
    otp.update(code=code, start=time.time(), tries=0, sent=False,
               phone=request.args.get("phone", ""))  # timer starts

    if EMAIL_APP_PASSWORD:              # send a real e-mail if an app password is set
        try:
            msg = EmailMessage()
            msg["Subject"] = f"Your login code: {code}"
            msg["From"] = EMAIL
            msg["To"] = EMAIL
            msg.set_content(f"Your 2FA verification code is: {code}\n\n"
                            f"It expires in {OTP_TTL // 60} minutes. "
                            f"If you did not try to log in, ignore this email.")
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
                s.login(EMAIL, EMAIL_APP_PASSWORD)
                s.send_message(msg)
            otp["sent"] = True
        except Exception as e:
            print("email error:", e)

    if otp["sent"]:
        note = f"<p style='color:#16a34a'>📧 Code sent to <b>{EMAIL}</b>.</p>"
    else:
        note = (f"<p style='color:#16a34a'>📱 Code sent to your phone inbox.</p>"
                f"<p style='color:#888;font-size:12px'>Phone: open "
                f"<b>{inbox_url()}</b></p>")
    return otp_page(note)


# ---------- PHONE INBOX (OTP and push delivery without e-mail) ----------
# The phone keeps http://<laptop-IP>:5002/inbox open in its browser.
# New codes and push requests appear there and the phone vibrates.
def inbox_url():
    # Locally: the laptop's network IP; on the server: the site's own address
    host = request.host.split(":")[0]
    if host in ("localhost", "127.0.0.1"):
        return f"http://{local_ip()}:5002/inbox"
    return request.host_url + "inbox"

def local_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

@app.route("/inbox")
def inbox():
    return '''<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Inbox</title>
<style>body{font-family:-apple-system,sans-serif;background:#f5f5f7;margin:0;padding:20px;text-align:center}
.mail{background:white;border-radius:16px;padding:24px;margin-top:20px;box-shadow:0 4px 20px rgba(0,0,0,.1)}
.code{font-size:48px;letter-spacing:8px;font-weight:bold;color:#764ba2;margin:12px 0}
.muted{color:#888;font-size:14px}.new{animation:flash 1s 3}@keyframes flash{50%{background:#fde68a}}</style>
</head><body><h2>📩 Inbox</h2><p class="muted">Keep this page open. Login codes appear here.</p>
<div id="box" class="mail"><p class="muted">No new messages</p></div>
<script>
let last = null;
async function answer(a) { await fetch("/push-" + a, {method: "POST"}); }
setInterval(async () => {
  const d = await (await fetch("/inbox-status")).json();
  const box = document.getElementById("box");
  if (d.push) {
    if (last !== "push-" + d.push) {
      last = "push-" + d.push;
      box.innerHTML = '<p class="muted">TISCODE Demo · Login request</p><h2>Is this you trying to sign in?</h2>' +
        '<button onclick="answer(\'approve\')" style="font-size:20px;padding:14px 28px;margin:8px;background:#16a34a;color:white;border:none;border-radius:12px">Approve</button>' +
        '<button onclick="answer(\'deny\')" style="font-size:20px;padding:14px 28px;margin:8px;background:#e5e7eb;border:none;border-radius:12px">Deny</button>';
      box.classList.remove("new"); void box.offsetWidth; box.classList.add("new");
      if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
    }
    return;
  }
  if (!d.code) { box.innerHTML = '<p class="muted">No new messages</p>'; last = null; return; }
  if (d.code !== last) {
    last = d.code;
    box.innerHTML = '<p class="muted">From: TISCODE Demo · Your login code</p>' +
                    '<div class="code">' + d.code + '</div><p class="muted">Expires in 5 minutes</p>';
    box.classList.remove("new"); void box.offsetWidth; box.classList.add("new");
    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
  }
}, 1000);
</script></body></html>'''

@app.route("/inbox-status")
def inbox_status():
    alive = otp["code"] and not otp.get("sent") and time.time() - otp["start"] <= OTP_TTL
    p = push["id"] if push["pending"] and time.time() - push["start"] <= OTP_TTL else None
    return jsonify(code=otp["code"] if alive else None, push=p)


# ---------- PUSH APPROVAL ("Is this you?", as in Google / Microsoft / Duo) ----------
# Closest competitor to TISCODE: both need one tap. Push does not prove the phone
# is in the same room (open to MFA-fatigue attacks).
push = {"pending": False, "id": 0, "start": None, "phone": "", "approved": False}

@app.route("/push")
def push_start():
    push.update(pending=True, id=push["id"] + 1, start=time.time(), approved=False,
                phone=request.args.get("phone", ""))     # timer starts
    return card("Push approval", f'''<h1>📲 Check your phone</h1>
<p>A login request was sent to your phone.<br>Tap <b>Approve</b> to sign in.</p>
<p style="color:#888;font-size:12px">Phone: open <b>{inbox_url()}</b></p>
<script>setInterval(async () => {{
  const d = await (await fetch("/push-status")).json();
  if (d.approved) location.href = "/push-success";
  if (d.denied) location.href = "/choose";
}}, 1000);</script>''')

@app.route("/push-approve", methods=["POST"])
def push_approve():
    if push["pending"]:
        push.update(pending=False, approved=True, elapsed=round(time.time() - push["start"], 1))
        log_result("Push approval", "", "", push["phone"], "success", push["elapsed"])
    return jsonify(ok=True)

@app.route("/push-deny", methods=["POST"])
def push_deny():
    if push["pending"]:
        push.update(pending=False, denied=True)
        log_result("Push approval", "", "", push["phone"], "denied", round(time.time() - push["start"], 1))
    return jsonify(ok=True)

@app.route("/push-status")
def push_status():
    return jsonify(approved=push["approved"], denied=push.pop("denied", False))

@app.route("/push-success")
def push_success():
    return success_page("Push approval", push.get("elapsed", "?"))


# Code entry page (re-shown with an error message)
def otp_page(note, error=""):
    err = f"<p style='color:#c62828;font-weight:bold'>{error}</p>" if error else ""
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Email OTP</title>
<style>body{{font-family:-apple-system,sans-serif;background:linear-gradient(135deg,#667eea,#764ba2);
display:flex;justify-content:center;align-items:center;height:100vh;margin:0}}
.kart{{background:white;padding:40px;border-radius:20px;box-shadow:0 20px 50px rgba(0,0,0,.3);
width:320px;text-align:center}}h1{{color:#764ba2}}input{{width:100%;padding:12px;margin:10px 0;
border:2px solid #e0e0e0;border-radius:10px;box-sizing:border-box;font-size:18px;text-align:center;
letter-spacing:4px}}button{{width:100%;padding:13px;background:#f59e0b;color:white;border:none;
border-radius:10px;font-size:16px;font-weight:bold;cursor:pointer}}
.small{{display:block;margin-top:14px;font-size:13px;color:#764ba2}}</style></head>
<body><div class="kart"><h1>📩 {"Email OTP" if otp.get("sent") else "OTP code"}</h1>{note}{err}
<form method="post" action="/verify-otp">
<input name="code" placeholder="6-digit code" maxlength="6" inputmode="numeric"
       autocomplete="one-time-code" autofocus>
<button>Verify</button></form>
<p style="color:#888;font-size:12px">Code valid for {OTP_TTL // 60} minutes · {OTP_MAX_TRIES} attempts</p>
<a class="small" href="/email-otp">🔁 Send a new code</a></div></body></html>'''


# Check the code (expiry, attempt limit, one-time use)
@app.route("/verify-otp", methods=["POST"])
def verify_otp():
    entered = request.form.get("code", "").strip()
    method = "Email OTP" if otp.get("sent") else "OTP (phone inbox)"
    if not otp["code"]:
        return otp_page("", "No active code. Request a new one.")
    elapsed = round(time.time() - otp["start"], 1)
    if elapsed > OTP_TTL:
        otp["code"] = None
        log_result(method, "", "", otp.get("phone", ""), "expired", elapsed)
        return otp_page("", "⌛ Code expired. Request a new one.")
    if secrets.compare_digest(entered, otp["code"]):
        otp["code"] = None                  # one-time use
        log_result(method, "", "", otp.get("phone", ""), "success", elapsed)
        return success_page("Email OTP", elapsed)
    otp["tries"] += 1
    left = OTP_MAX_TRIES - otp["tries"]
    if left <= 0:
        otp["code"] = None
        log_result(method, "", "", otp.get("phone", ""), "fail", elapsed)
        return otp_page("", "❌ Too many wrong attempts. Request a new code.")
    return otp_page("", f"❌ Wrong code — {left} attempt(s) left.")

if __name__ == "__main__":
    # host=0.0.0.0 so phones on the same network can reach the server
    app.run(debug=True, host="0.0.0.0", port=5002)
