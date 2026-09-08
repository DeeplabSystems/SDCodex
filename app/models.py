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
