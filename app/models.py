from app import db
from datetime import datetime
import json

class Setting(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(256))

    def __repr__(self):
        return f'<Setting {self.key}: {self.value}>'

class Download(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    model_id = db.Column(db.Integer, nullable=False)
    version_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(256), nullable=False)
    type = db.Column(db.String(64))
    files = db.Column(db.Text) # JSON string of file paths
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_files(self, files_dict):
        self.files = json.dumps(files_dict)

    def get_files(self):
        return json.loads(self.files) if self.files else {}

    @property
    def image_path(self):
        files = self.get_files()
        return files.get('image')
        
    @property
    def model_path(self):
        files = self.get_files()
        return files.get('model')

    def __repr__(self):
        return f'<Download {self.name}>'

class PluginRepo(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    repo_url = db.Column(db.String(256), unique=True, nullable=False)
    name = db.Column(db.String(128))
    description = db.Column(db.String(512))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<PluginRepo {self.repo_url}>'

class User(db.Model):
    """A local account. The same row doubles as the target for OIDC/SSO logins
    (auth_provider records where the account came from; OIDC users have an empty
    password_hash and can only sign in through their IdP)."""
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(128), unique=True, nullable=False, index=True)
    email = db.Column(db.String(256))
    password_hash = db.Column(db.String(256), default='')
    display_name = db.Column(db.String(128))
    auth_provider = db.Column(db.String(64), default='local')  # 'local' or 'oidc:<name>'
    is_active = db.Column(db.Boolean, default=True)
    is_admin = db.Column(db.Boolean, default=False)
    api_key = db.Column(db.String(256))  # optional Civitai API key (per-user, not auth)
    last_login = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<User {self.username}>'

class Session(db.Model):
    """Server-side session backing the httpOnly session cookie."""
    __tablename__ = 'sessions'
    id = db.Column(db.String(64), primary_key=True)  # 32 random bytes, base64url
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='cascade'), nullable=False, index=True)
    provider = db.Column(db.String(64), nullable=False, default='local')
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='sessions')

    def __repr__(self):
        return f'<Session for user {self.user_id}>'

class OidcConfig(db.Model):
    """An OIDC identity-provider configuration (one row per SSO provider)."""
    __tablename__ = 'oidc_configs'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    enabled = db.Column(db.Boolean, default=False)
    issuer_url = db.Column(db.String(512), nullable=False)
    client_id = db.Column(db.String(256), nullable=False)
    client_secret = db.Column(db.String(512), nullable=False)
    redirect_uri = db.Column(db.String(512), nullable=False)
    scopes = db.Column(db.String(256), default='openid profile email')
    username_claim = db.Column(db.String(64), default='preferred_username')
    email_claim = db.Column(db.String(64), default='email')
    display_name_claim = db.Column(db.String(64), default='name')
    admin_claim = db.Column(db.String(64))       # e.g. 'groups'
    admin_value = db.Column(db.String(256))      # comma-separated values that grant admin
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<OidcConfig {self.name}>'
