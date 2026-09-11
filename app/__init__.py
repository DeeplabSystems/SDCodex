from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from app.config import Config

db = SQLAlchemy()

def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    
    db.init_app(app)
    
    # Register Core Blueprint
    from app.routes import main
    app.register_blueprint(main)
    
    with app.app_context():
        # Import models to ensure they are registered with SQLAlchemy
        from app import models
        db.create_all()
        # Pull-request-safe migration: add any missing columns to existing tables
        # (SQLite's create_all() ignores new columns on already-created tables).
        _ensure_column(app, "users", "avatar")

    # Initialize Plugin System
    from app.plugin_manager import plugin_manager
    plugin_manager.init_app(app, db)
    
    return app

import sqlalchemy as sa

def _ensure_column(app, table, column):
    """Add a column to an existing table if it is missing (idempotent)."""
    try:
        db.session.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} TEXT"))
        db.session.commit()
    except Exception:
        # Column already exists (or DB locked) — race with create_all is benign.
        db.session.rollback()
    finally:
        pass
