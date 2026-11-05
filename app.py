import os, uuid
from io import BytesIO
from datetime import datetime, timedelta, date
from urllib.parse import urlparse
from functools import wraps
from flask import Flask, jsonify, request, redirect, render_template, session, abort, send_from_directory, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from sqlalchemy import inspect, text, func

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-before-deploying")
db_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "galaxy.db")).strip()
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

if db_url.startswith("postgresql+psycopg://"):
    db_url = db_url.replace(
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
        1
    )
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "static", "uploads")
db = SQLAlchemy(app)

def now():
    return datetime.utcnow()

class Player(db.Model):
    id = db.Column(db.String(80), primary_key=True)
    first_seen = db.Column(db.DateTime, default=now, nullable=False)
    last_seen = db.Column(db.DateTime, default=now, nullable=False)
    best_score = db.Column(db.Integer, default=0, nullable=False)

class PlaySession(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    player_id = db.Column(db.String(80), db.ForeignKey("player.id"), nullable=False, index=True)
    started_at = db.Column(db.DateTime, default=now, nullable=False, index=True)
    ended_at = db.Column(db.DateTime)

class Advertisement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    target_url = db.Column(db.String(1000), nullable=False)
    # image_path reste présent pour compatibilité avec les anciennes lignes SQLite.
    image_path = db.Column(db.String(500), nullable=False)
    image_data = db.Column(db.LargeBinary, nullable=True)
    image_mime = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=now, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)

class AdEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ad_id = db.Column(db.Integer, db.ForeignKey("advertisement.id"), nullable=False, index=True)
    player_id = db.Column(db.String(80), nullable=True, index=True)
    kind = db.Column(db.String(12), nullable=False) # impression / click
    created_at = db.Column(db.DateTime, default=now, nullable=False, index=True)

def valid_http_url(value):
    p = urlparse(value or "")
    return p.scheme in ("http", "https") and bool(p.netloc)

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify(error="Authentification requise"), 401
            return redirect("/admin/login")
        return fn(*args, **kwargs)
    return wrapped

@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")

@app.get("/<path:path>")
def static_files(path):
    if path.startswith("admin") or path.startswith("api/"):
        abort(404)
    return send_from_directory(app.static_folder, path)

@app.post("/api/session/start")
def start_session():
    data = request.get_json(silent=True) or {}
    player_id = str(data.get("user_id", ""))[:80]
    if not player_id:
        return jsonify(error="Identifiant manquant"), 400
    player = db.session.get(Player, player_id)
    if player is None:
        player = Player(id=player_id)
        db.session.add(player)
    player.last_seen = now()
    play = PlaySession(player_id=player_id)
    db.session.add(play)
    db.session.commit()
    return jsonify(session_id=play.id, best_score=player.best_score or 0)

@app.post("/api/session/end")
def end_session():
    data = request.get_json(silent=True) or {}
    sid = str(data.get("session_id", ""))
    play = db.session.get(PlaySession, sid)
    if play and not play.ended_at:
        play.ended_at = now()
        try:
            score = max(0, min(100000000, int(data.get("score", 0))))
        except (TypeError, ValueError):
            score = 0
        player = db.session.get(Player, play.player_id)
        if player and score > (player.best_score or 0):
            player.best_score = score
        db.session.commit()
    return jsonify(ok=True)

@app.get("/api/ads/active")
def active_ad():
    ad = Advertisement.query.filter_by(active=True).filter(Advertisement.ends_at > now()).order_by(Advertisement.created_at.desc()).first()
    if not ad:
        return jsonify({})
    return jsonify(id=ad.id, title=ad.title, image_url=f"/api/ads/{ad.id}/image")

@app.get("/api/ads/<int:ad_id>/image")
def ad_image(ad_id):
    ad = db.session.get(Advertisement, ad_id)
    if not ad:
        abort(404)
    if ad.image_data:
        return send_file(BytesIO(ad.image_data), mimetype=ad.image_mime or "application/octet-stream", max_age=3600)
    # Compatibilité avec les publicités historiques dont le fichier existe encore.
    path = os.path.abspath(os.path.join(app.static_folder, ad.image_path or ""))
    static_root = os.path.abspath(app.static_folder) + os.sep
    if path.startswith(static_root) and os.path.isfile(path):
        return send_file(path, conditional=True, max_age=3600)
    abort(404)

@app.post("/api/ads/<int:ad_id>/impression")
def ad_impression(ad_id):
    ad = db.session.get(Advertisement, ad_id)
    if not ad or not ad.active or ad.ends_at <= now():
        return jsonify(ok=False), 404
    data = request.get_json(silent=True) or {}
    player_id = str(data.get("user_id", ""))[:80] or None
    db.session.add(AdEvent(ad_id=ad.id, player_id=player_id, kind="impression"))
    db.session.commit()
    return jsonify(ok=True)

@app.get("/api/ads/<int:ad_id>/click")
def ad_click(ad_id):
    ad = db.session.get(Advertisement, ad_id)
    if not ad or not ad.active or ad.ends_at <= now():
        abort(404)
    player_id = str(request.args.get("uid", ""))[:80] or None
    db.session.add(AdEvent(ad_id=ad.id, player_id=player_id, kind="click"))
    db.session.commit()
    return redirect(ad.target_url, code=302)

@app.get("/admin/login")
def admin_login():
    return render_template("login.html", error=None)

@app.post("/admin/login")
def admin_login_post():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if os.environ.get("ADMIN_USERNAME") and os.environ.get("ADMIN_PASSWORD") and username == os.environ["ADMIN_USERNAME"] and password == os.environ["ADMIN_PASSWORD"]:
        session.clear()
        session["is_admin"] = True
        return redirect("/admin")
    return render_template("login.html", error="Identifiants incorrects"), 401

@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin/login")

@app.get("/admin")
@admin_required
def dashboard():
    current = now()
    today_start = datetime.combine(date.today(), datetime.min.time())
    week_start = current - timedelta(days=7)
    total_users = Player.query.count()
    users_today = Player.query.filter(Player.last_seen >= today_start).count()
    users_week = Player.query.filter(Player.last_seen >= week_start).count()
    plays_today = PlaySession.query.filter(PlaySession.started_at >= today_start).count()
    plays_week = PlaySession.query.filter(PlaySession.started_at >= week_start).count()
    plays_total = PlaySession.query.count()
    active_now = PlaySession.query.filter(PlaySession.started_at >= current-timedelta(minutes=10), PlaySession.ended_at.is_(None)).count()
    impressions = AdEvent.query.filter_by(kind="impression").count()
    clicks = AdEvent.query.filter_by(kind="click").count()
    ads = Advertisement.query.order_by(Advertisement.created_at.desc()).all()
    ad_rows = []
    for ad in ads:
        views = AdEvent.query.filter_by(ad_id=ad.id, kind="impression").count()
        taps = AdEvent.query.filter_by(ad_id=ad.id, kind="click").count()
        ad_rows.append({"ad":ad, "views":views, "clicks":taps, "expired":ad.ends_at <= current})
    return render_template("admin.html", stats={
        "total_users":total_users, "users_today":users_today, "users_week":users_week,
        "plays_today":plays_today, "plays_week":plays_week, "plays_total":plays_total,
        "active_now":active_now, "impressions":impressions, "clicks":clicks,
        "ctr":round(clicks/impressions*100, 1) if impressions else 0
    }, ads=ad_rows)

@app.post("/admin/ads")
@admin_required
def create_ad():
    title = request.form.get("title", "").strip()[:120]
    target_url = request.form.get("target_url", "").strip()
    try:
        days = max(1, min(365, int(request.form.get("duration_days", "7"))))
    except ValueError:
        days = 7
    image = request.files.get("image")
    if not title or not valid_http_url(target_url) or not image or not image.filename:
        return redirect("/admin?error=Veuillez+remplir+tous+les+champs")
    ext = os.path.splitext(secure_filename(image.filename))[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return redirect("/admin?error=Format+image+non+autorise")
    image_bytes = image.read()
    if not image_bytes:
        return redirect("/admin?error=Image+vide")
    # Le fichier est conservé dans PostgreSQL (Neon), pas sur le disque éphémère de Render.
    mime = image.mimetype or {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif"
    }.get(ext, "application/octet-stream")
    ad = Advertisement(
        title=title,
        target_url=target_url,
        image_path="database/" + uuid.uuid4().hex + ext,
        image_data=image_bytes,
        image_mime=mime,
        ends_at=now()+timedelta(days=days)
    )
    try:
        db.session.add(ad)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Échec de l'enregistrement de la publicité")
        return redirect("/admin?error=Erreur+de+stockage.+Veuillez+reessayer")
    return redirect("/admin")

@app.post("/admin/ads/<int:ad_id>/toggle")
@admin_required
def toggle_ad(ad_id):
    ad = db.session.get(Advertisement, ad_id)
    if ad:
        ad.active = not ad.active
        db.session.commit()
    return redirect("/admin")

@app.post("/admin/ads/<int:ad_id>/delete")
@admin_required
def delete_ad(ad_id):
    ad = db.session.get(Advertisement, ad_id)
    if ad:
        AdEvent.query.filter_by(ad_id=ad.id).delete()
        if ad.image_path and not ad.image_path.startswith("database/"):
            image_path = os.path.abspath(os.path.join(app.static_folder, ad.image_path))
            static_root = os.path.abspath(app.static_folder) + os.sep
            if image_path.startswith(static_root) and os.path.isfile(image_path):
                os.remove(image_path)
        db.session.delete(ad)
        db.session.commit()
    return redirect("/admin")

@app.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify(status="ok", service="Galaxy Defender", database="connected")
    except Exception:
        db.session.rollback()
        app.logger.exception("La base de données est inaccessible")
        return jsonify(status="error", service="Galaxy Defender", database="unavailable"), 503

def initialize_database():
    """Crée les tables manquantes et ajoute les colonnes sans effacer les données."""
    db.create_all()
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    dialect = db.engine.dialect.name
    additions = {
        "player": {
            "best_score": "INTEGER NOT NULL DEFAULT 0",
        },
        "advertisement": {
            "image_data": "BYTEA" if dialect == "postgresql" else "BLOB",
            "image_mime": "VARCHAR(100)",
        },
    }
    for table, columns in additions.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspect(db.engine).get_columns(table)}
        for column, sql_type in columns.items():
            if column not in existing:
                try:
                    with db.engine.begin() as connection:
                        connection.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}'))
                except Exception:
                    # Tolère le cas où un autre worker Gunicorn a ajouté la colonne en même temps.
                    refreshed = {item["name"] for item in inspect(db.engine).get_columns(table)}
                    if column not in refreshed:
                        raise

with app.app_context():
    initialize_database()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
