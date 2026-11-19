import os
import json
import logging
import queue
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta

from flask import Flask, Response, jsonify, render_template, request, send_from_directory, stream_with_context
from gtts import gTTS
from mutagen.mp3 import MP3
from werkzeug.security import check_password_hash

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", 16 * 1024 * 1024))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

SEED_USERS = [
    {
        "name": "Justin Baraka", "handle": "justinbaraka",
        "seed": "JustinB", "style": "adventurer",
        "tag": "Market", "title": "Konyo Konyo at dawn",
        "text": ("Konyo Konyo market never sleeps. From the first light of dawn, "
                 "you hear the calling of tea sellers, the chopping of onions, "
                 "the laughter of children running between stalls. This is where "
                 "Juba feeds itself, one tomato at a time."),
        "likes": 47, "minutes_ago": 12,
    },
    {
        "name": "Kuol Biong", "handle": "kuolbiong",
        "seed": "KuolB", "style": "lorelei",
        "tag": "Music", "title": "The music of the night",
        "text": ("The music in Juba tells our stories better than any book. "
                 "At night, when the generators hum and the streets grow quiet, "
                 "you hear it from the compounds: a drum, a guitar, voices singing "
                 "in Juba Arabic. We may not have everything, but we have our songs."),
        "likes": 63, "minutes_ago": 38,
    },
    {
        "name": "Joseph Khamis", "handle": "josephkhamis",
        "seed": "JosephK", "style": "micah",
        "tag": "Family", "title": "Charcoal smoke and homework",
        "text": ("Every evening my mother lights the charcoal stove and the smoke "
                 "rises between the buildings. My little sister does her homework "
                 "by the light of a phone. This is Juba life - simple, warm, and "
                 "full of hope for tomorrow."),
        "likes": 52, "minutes_ago": 92,
    },
    {
        "name": "Karim Joseph", "handle": "karimjoseph",
        "seed": "KarimJ", "style": "avataaars",
        "tag": "Traffic", "title": "Slow road to Juba Bridge",
        "text": ("The road to Juba Bridge is a river of cars and boda bodas. "
                 "Everyone is going somewhere, everyone is late, everyone is "
                 "laughing about it. The traffic here is not a problem - it is a "
                 "way of life. We move slowly, but we move together."),
        "likes": 29, "minutes_ago": 140,
    },
    {
        "name": "Stephen Weri", "handle": "stephenweri",
        "seed": "StephenW", "style": "adventurer",
        "tag": "Sports", "title": "Football until sunset",
        "text": ("On the football field in Jebel, the boys play until the sun "
                 "goes down. Barefoot, one ball, no referee. But the noise from "
                 "the crowd - you would think they were playing in a stadium. "
                 "Sports is how Juba lets off steam, how we celebrate being young "
                 "and alive."),
        "likes": 71, "minutes_ago": 210,
    },
    {
        "name": "Asunta Buo", "handle": "asuntabuo",
        "seed": "AsuntaB", "style": "lorelei",
        "tag": "Faith", "title": "Sunday bells",
        "text": ("On Sunday morning, the church bells ring across the city and "
                 "the streets fill with white dresses and bright smiles. In Juba, "
                 "faith is not just in the church - it is in how we greet strangers, "
                 "how we share what little we have. Faith is the heartbeat of this city."),
        "likes": 88, "minutes_ago": 320,
    },
]


def _safe_filename(handle):
    return "".join(c for c in handle if c.isalnum() or c in "-_") + ".mp3"


def _generate_audio(text, filename):
    path = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(path):
        try:
            gTTS(text=text, lang="en", slow=False).save(path)
            print("  generated", filename)
        except Exception as exc:
            print("  gTTS failed for %s: %s" % (filename, exc))
            return None, 0
    try:
        return filename, int(MP3(path).info.length)
    except Exception:
        return None, 0


def build_seed_stories():
    stories, now = [], datetime.now()
    for u in SEED_USERS:
        filename = _safe_filename(u["handle"])
        fname, dur = _generate_audio(u["text"], filename)
        if fname is None:
            print("  skipping", u["name"], "(no audio)")
            continue
        when = now - timedelta(minutes=u["minutes_ago"])
        stories.append({
            "id": "seed_%s" % u["handle"],
            "author": {
                "name": u["name"],
                "handle": u["handle"],
                "avatar_url": (
                    "https://api.dicebear.com/7.x/%s/svg"
                    "?seed=%s&backgroundColor=b6e3f4,c0aede,d1d4f9,ffd5dc"
                    % (u["style"], u["seed"])
                ),
            },
            "title": u["title"],
            "tag": u["tag"],
            "duration": "%d:%02d" % (dur // 60, dur % 60),
            "time": when.strftime("%I:%M %p").lstrip("0"),
            "audio_url": "/static/audio/%s" % filename,
            "likes": u["likes"],
            "liked": False,
            "is_seed": True,
            "createdAt": int(when.timestamp() * 1000),
            "comments": [],
        })
    return stories


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/seed-stories")
def api_seed_stories():
    return jsonify(build_seed_stories())


# Free, self-hosted data layer: SQLite stores profiles, stories, likes and comments.
# Server-Sent Events notify every connected browser when shared data changes.
DB_PATH = os.environ.get("JUBA_STORY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "juba_story.db"))
SUBSCRIBERS = set()
SUBSCRIBERS_LOCK = threading.Lock()
FOUNDER_PASSWORD_HASH = os.environ.get("FOUNDER_PASSWORD_HASH", "")


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "microphone=(self)")
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Upload is too large (maximum 16 MB)"}), 413


@app.route("/healthz")
def healthz():
    try:
        with db_connect() as db:
            db.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok"})
    except sqlite3.Error:
        logger.exception("Health check database failure")
        return jsonify({"status": "error"}), 503


def db_connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db():
    with db_connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
          device_id TEXT PRIMARY KEY, name TEXT NOT NULL, handle TEXT NOT NULL,
          bio TEXT NOT NULL, topics TEXT NOT NULL, avatar TEXT, joined INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS accounts (
          handle TEXT PRIMARY KEY, password_hash TEXT NOT NULL, name TEXT NOT NULL,
          bio TEXT NOT NULL, topics TEXT NOT NULL, avatar TEXT, role TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stories (
          id TEXT PRIMARY KEY, device_id TEXT, title TEXT NOT NULL, tag TEXT NOT NULL,
          duration TEXT NOT NULL, audio_file TEXT NOT NULL, created_at INTEGER NOT NULL,
          author_name TEXT NOT NULL, author_handle TEXT NOT NULL, author_avatar TEXT,
          is_seed INTEGER NOT NULL DEFAULT 0, base_likes INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS likes (
          story_id TEXT NOT NULL, device_id TEXT NOT NULL,
          PRIMARY KEY (story_id, device_id)
        );
        CREATE TABLE IF NOT EXISTS comments (
          id INTEGER PRIMARY KEY AUTOINCREMENT, story_id TEXT NOT NULL, device_id TEXT NOT NULL,
          author_name TEXT NOT NULL, author_avatar TEXT, text TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
          device_id TEXT PRIMARY KEY, values_json TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, handle TEXT NOT NULL, device_id TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        """)
        if FOUNDER_PASSWORD_HASH:
            db.execute("""INSERT OR IGNORE INTO accounts(handle,password_hash,name,bio,topics,avatar,role)
              VALUES(?, ?, ?, ?, ?, ?, ?)""", (
              "justinbaraka", FOUNDER_PASSWORD_HASH, "Justin Baraka",
              "Founder of Juba Story. Sharing the voice of our city.",
              json.dumps(["Juba Life", "Market", "Music"]), None, "founder"))
        now = datetime.now()
        for user in SEED_USERS:
            filename = _safe_filename(user["handle"])
            fname, duration = _generate_audio(user["text"], filename)
            if not fname:
                continue
            created = int((now - timedelta(minutes=user["minutes_ago"])).timestamp() * 1000)
            avatar = "https://api.dicebear.com/7.x/%s/svg?seed=%s&backgroundColor=b6e3f4,c0aede,d1d4f9" % (user["style"], user["seed"])
            db.execute("""INSERT OR IGNORE INTO stories
              (id, title, tag, duration, audio_file, created_at, author_name, author_handle,
               author_avatar, is_seed, base_likes)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""", (
              "seed_" + user["handle"], user["title"], user["tag"],
              "%d:%02d" % (duration // 60, duration % 60), filename, created,
              user["name"], user["handle"], avatar, user["likes"]))


def notify(event, story_id=None):
    payload = json.dumps({"event": event, "story_id": story_id})
    with SUBSCRIBERS_LOCK:
        for subscriber in list(SUBSCRIBERS):
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                SUBSCRIBERS.discard(subscriber)


def serialize_story(row, device_id):
    with db_connect() as db:
        likes = row["base_likes"] + db.execute("SELECT COUNT(*) FROM likes WHERE story_id=?", (row["id"],)).fetchone()[0]
        liked = db.execute("SELECT 1 FROM likes WHERE story_id=? AND device_id=?", (row["id"], device_id)).fetchone() is not None
        comments = db.execute("SELECT * FROM comments WHERE story_id=? ORDER BY created_at", (row["id"],)).fetchall()
    return {
        "id": row["id"], "title": row["title"], "tag": row["tag"], "duration": row["duration"],
        "time": datetime.fromtimestamp(row["created_at"] / 1000).strftime("%I:%M %p").lstrip("0"),
        "createdAt": row["created_at"], "audio_url": "/media/" + row["audio_file"],
        "likes": likes, "liked": liked, "is_seed": bool(row["is_seed"]),
        "author": {"name": row["author_name"], "handle": row["author_handle"], "avatar_url": row["author_avatar"]},
        "comments": [{"text": c["text"], "time": datetime.fromtimestamp(c["created_at"] / 1000).strftime("%I:%M %p").lstrip("0"), "author": c["author_name"], "avatar": c["author_avatar"]} for c in comments]
    }


@app.route("/media/<path:filename>")
def media(filename):
    return send_from_directory(AUDIO_DIR, filename)


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "sw.js", mimetype="application/javascript")


@app.route("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\nSitemap: https://juba-story.onrender.com/sitemap.xml\n", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<url><loc>https://juba-story.onrender.com/</loc></url>'
        '</urlset>',
        mimetype="application/xml",
    )


@app.route("/api/stories")
def all_stories():
    device_id = request.args.get("device_id", "anonymous")
    with db_connect() as db:
        rows = db.execute("SELECT * FROM stories ORDER BY created_at DESC").fetchall()
    return jsonify([serialize_story(row, device_id) for row in rows])


@app.route("/api/profiles/<device_id>")
def profile_for_device(device_id):
    with db_connect() as db:
        row = db.execute("SELECT * FROM profiles WHERE device_id=?", (device_id,)).fetchone()
    if not row:
        return jsonify(None)
    return jsonify({"name": row["name"], "handle": row["handle"], "bio": row["bio"], "topics": json.loads(row["topics"]), "avatar": row["avatar"], "joined": row["joined"], "role": "founder" if row["handle"] == "justinbaraka" else "member"})


@app.route("/api/profiles", methods=["POST"])
def save_profile():
    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id", "")
    handle = data.get("handle", "").strip().lower()
    if not device_id or len(data.get("name", "").strip()) < 2 or len(handle) < 2:
        return jsonify({"error": "A valid name and handle are required"}), 400
    if handle == "justinbaraka":
        token = data.get("session_token", "")
        with db_connect() as db:
            founder_session = db.execute("SELECT 1 FROM sessions WHERE token=? AND handle=? AND device_id=?", (token, handle, device_id)).fetchone()
        if not founder_session:
            return jsonify({"error": "The founder handle is reserved"}), 403
    with db_connect() as db:
        account_owner = db.execute("SELECT 1 FROM accounts WHERE handle=?", (handle,)).fetchone()
        other_profile = db.execute("SELECT 1 FROM profiles WHERE handle=? AND device_id<>?", (handle, device_id)).fetchone()
        if account_owner and handle != "justinbaraka":
            return jsonify({"error": "That handle is already in use"}), 409
        if other_profile:
            return jsonify({"error": "That handle is already in use"}), 409
        db.execute("""INSERT INTO profiles(device_id,name,handle,bio,topics,avatar,joined)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET name=excluded.name,
          handle=excluded.handle,bio=excluded.bio,topics=excluded.topics,avatar=excluded.avatar""", (
          device_id, data["name"].strip(), handle, data.get("bio", "Voice of Juba."),
          json.dumps(data.get("topics", [])), data.get("avatar"), data.get("joined", int(datetime.now().timestamp() * 1000))))
    notify("profile_updated")
    return jsonify({"ok": True})


@app.route("/api/users")
def search_users():
    query = request.args.get("query", "").strip().lower()
    if len(query) < 1:
        return jsonify([])
    pattern = "%" + query + "%"
    with db_connect() as db:
        rows = db.execute("""SELECT name,handle,avatar,role FROM accounts
          WHERE lower(name) LIKE ? OR lower(handle) LIKE ? OR lower(bio) LIKE ? OR lower(topics) LIKE ?
          UNION
          SELECT name,handle,avatar,CASE WHEN handle='justinbaraka' THEN 'founder' ELSE 'member' END
          FROM profiles
          WHERE lower(name) LIKE ? OR lower(handle) LIKE ? OR lower(bio) LIKE ? OR lower(topics) LIKE ?
          UNION
          SELECT author_name,author_handle,author_avatar,
                 CASE WHEN author_handle='justinbaraka' THEN 'founder' ELSE 'member' END
          FROM stories
          WHERE lower(author_name) LIKE ? OR lower(author_handle) LIKE ?
          ORDER BY name LIMIT 30""",
          (pattern, pattern, pattern, pattern,
           pattern, pattern, pattern, pattern,
           pattern, pattern)).fetchall()
    results, seen_handles = [], set()
    for row in rows:
        if row["handle"] in seen_handles:
            continue
        seen_handles.add(row["handle"])
        results.append({"name": row["name"], "handle": row["handle"], "avatar": row["avatar"], "role": row["role"]})
    return jsonify(results)


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    handle = data.get("handle", "").strip().lower().lstrip("@")
    device_id = data.get("device_id", "")
    with db_connect() as db:
        account = db.execute("SELECT * FROM accounts WHERE handle=?", (handle,)).fetchone()
    if not account or not check_password_hash(account["password_hash"], data.get("password", "")):
        return jsonify({"error": "Invalid handle or password"}), 401
    session_token = secrets.token_urlsafe(32)
    profile = {"name": account["name"], "handle": account["handle"], "bio": account["bio"], "topics": json.loads(account["topics"]), "avatar": account["avatar"], "joined": int(datetime.now().timestamp() * 1000), "role": account["role"], "session_token": session_token}
    if device_id:
        with db_connect() as db:
            db.execute("INSERT INTO sessions(token,handle,device_id,created_at) VALUES(?,?,?,?)", (session_token, account["handle"], device_id, int(datetime.now().timestamp() * 1000)))
            db.execute("""INSERT INTO profiles(device_id,name,handle,bio,topics,avatar,joined)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET name=excluded.name,
              handle=excluded.handle,bio=excluded.bio,topics=excluded.topics,avatar=excluded.avatar""", (
              device_id, profile["name"], profile["handle"], profile["bio"], json.dumps(profile["topics"]), profile["avatar"], profile["joined"]))
    return jsonify(profile)


DEFAULT_SETTINGS = {"showSeedStories": True, "liveUpdates": True, "autoplayAudio": False, "compactFeed": False}


@app.route("/api/settings/<device_id>")
def get_settings(device_id):
    with db_connect() as db:
        row = db.execute("SELECT values_json FROM settings WHERE device_id=?", (device_id,)).fetchone()
    values = dict(DEFAULT_SETTINGS)
    if row:
        values.update(json.loads(row["values_json"]))
    return jsonify(values)


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json(force=True)
    device_id = data.get("device_id", "")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400
    values = dict(DEFAULT_SETTINGS)
    values.update({key: bool(data[key]) for key in DEFAULT_SETTINGS if key in data})
    with db_connect() as db:
        db.execute("""INSERT INTO settings(device_id,values_json,updated_at) VALUES(?,?,?)
          ON CONFLICT(device_id) DO UPDATE SET values_json=excluded.values_json,updated_at=excluded.updated_at""", (
          device_id, json.dumps(values), int(datetime.now().timestamp() * 1000)))
    notify("settings_updated")
    return jsonify(values)


@app.route("/api/stories", methods=["POST"])
def create_story():
    device_id = request.form.get("device_id", "")
    audio = request.files.get("audio")
    if not device_id or not audio:
        return jsonify({"error": "An audio recording and device ID are required"}), 400
    try:
        metadata = json.loads(request.form.get("metadata", "{}"))
    except (TypeError, json.JSONDecodeError):
        return jsonify({"error": "Invalid story metadata"}), 400
    if not isinstance(metadata, dict):
        return jsonify({"error": "Invalid story metadata"}), 400
    if audio.mimetype not in {"audio/webm", "video/webm", "audio/ogg", "audio/mp4", "audio/mpeg"}:
        return jsonify({"error": "Unsupported audio format"}), 415
    title = str(metadata.get("title", "Voice from Juba")).strip()[:120] or "Voice from Juba"
    tag = str(metadata.get("tag", "Juba Life")).strip()[:40] or "Juba Life"
    duration = str(metadata.get("duration", "0:00")).strip()[:12] or "0:00"
    with db_connect() as db:
        profile = db.execute("SELECT * FROM profiles WHERE device_id=?", (device_id,)).fetchone()
    if not profile:
        return jsonify({"error": "Create a profile first"}), 400
    story_id = "s_" + uuid.uuid4().hex
    filename = story_id + (".webm" if "webm" in (audio.mimetype or "") else ".ogg")
    audio.save(os.path.join(AUDIO_DIR, filename))
    created = int(datetime.now().timestamp() * 1000)
    with db_connect() as db:
        db.execute("""INSERT INTO stories(id,device_id,title,tag,duration,audio_file,created_at,
          author_name,author_handle,author_avatar) VALUES(?,?,?,?,?,?,?,?,?,?)""", (
          story_id, device_id, title, tag, duration, filename, created,
          profile["name"], profile["handle"], profile["avatar"]))
        row = db.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
    notify("story_created", story_id)
    return jsonify(serialize_story(row, device_id)), 201


@app.route("/api/stories/<story_id>/like", methods=["POST"])
def like_story(story_id):
    data = request.get_json(silent=True) or {}
    device_id = str(data.get("device_id", "")).strip()
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400
    with db_connect() as db:
        if not db.execute("SELECT 1 FROM stories WHERE id=?", (story_id,)).fetchone():
            return jsonify({"error": "Story not found"}), 404
        existing = db.execute("SELECT 1 FROM likes WHERE story_id=? AND device_id=?", (story_id, device_id)).fetchone()
        if existing:
            db.execute("DELETE FROM likes WHERE story_id=? AND device_id=?", (story_id, device_id))
        else:
            db.execute("INSERT OR IGNORE INTO likes(story_id,device_id) VALUES(?,?)", (story_id, device_id))
    notify("story_updated", story_id)
    return jsonify({"ok": True})


@app.route("/api/stories/<story_id>/comments", methods=["POST"])
def comment_story(story_id):
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()[:140]
    with db_connect() as db:
        profile = db.execute("SELECT * FROM profiles WHERE device_id=?", (data.get("device_id", ""),)).fetchone()
        story = db.execute("SELECT 1 FROM stories WHERE id=?", (story_id,)).fetchone()
        if not profile or not story or not text:
            return jsonify({"error": "A profile and comment are required"}), 400
        db.execute("INSERT INTO comments(story_id,device_id,author_name,author_avatar,text,created_at) VALUES(?,?,?,?,?,?)", (story_id, data["device_id"], profile["name"], profile["avatar"], text, int(datetime.now().timestamp() * 1000)))
    notify("story_updated", story_id)
    return jsonify({"ok": True})


@app.route("/api/stories/<story_id>", methods=["DELETE"])
def remove_story(story_id):
    device_id = request.args.get("device_id", "")
    session_token = request.args.get("session_token", "")
    with db_connect() as db:
        profile = db.execute("SELECT handle FROM profiles WHERE device_id=?", (device_id,)).fetchone()
        founder_session = db.execute("SELECT 1 FROM sessions WHERE token=? AND handle='justinbaraka' AND device_id=?", (session_token, device_id)).fetchone()
        row = db.execute("""SELECT audio_file FROM stories WHERE id=? AND
          (device_id=? OR (is_seed=1 AND author_handle='justinbaraka' AND ? IS NOT NULL))""", (story_id, device_id, founder_session[0] if founder_session else None)).fetchone()
        if not row:
            return jsonify({"error": "Story not found"}), 404
        db.execute("DELETE FROM stories WHERE id=?", (story_id,))
    try:
        os.remove(os.path.join(AUDIO_DIR, row["audio_file"]))
    except OSError:
        pass
    notify("story_deleted", story_id)
    return jsonify({"ok": True})


@app.route("/api/events")
def events():
    subscriber = queue.Queue(maxsize=20)
    with SUBSCRIBERS_LOCK:
        SUBSCRIBERS.add(subscriber)

    @stream_with_context
    def stream():
        try:
            yield "data: {\"event\": \"connected\"}\n\n"
            while True:
                try:
                    yield "data: " + subscriber.get(timeout=20) + "\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            with SUBSCRIBERS_LOCK:
                SUBSCRIBERS.discard(subscriber)
    return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


init_db()


if __name__ == "__main__":
    print("Generating seed voice notes (first run may take ~15s)...")
    build_seed_stories()
    print("Ready at http://127.0.0.1:5000")
    app.run(debug=False, use_reloader=False, threaded=True, port=5000)
