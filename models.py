from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # الدور: admin (أدمن) أو employee (موظف)
    role = db.Column(db.String(20), nullable=False, default='employee')
    is_active_user = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == 'admin'

    # مطلوبة من Flask-Login حتى لا يُسمح بتسجيل الدخول للحسابات المعطّلة
    @property
    def is_active(self):
        return self.is_active_user


# فئات الأوراق
DOCUMENT_CATEGORIES = ['اختبار', 'رسالة', 'أخرى']
SIDES = ['يمين', 'يسار']
WORK_STATUSES = ['قيد التنفيذ', 'منتهي', 'متوقف']


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(50), nullable=False)  # اختبار / رسالة / أخرى
    description = db.Column(db.Text)

    # بيانات الملف المرفوع (اختيارية - يمكن تسجيل ورقة بدون رفع ملف)
    stored_file_name = db.Column(db.String(300))
    original_file_name = db.Column(db.String(300))

    # ربط اختياري بموقع على الطريق
    side = db.Column(db.String(10))       # يمين / يسار / فارغ = عام
    km_point = db.Column(db.Float)

    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    uploaded_by = db.relationship('User')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def has_file(self):
        return bool(self.stored_file_name)


class WorkRecord(db.Model):
    """حصر الأعمال على الطريق الساحلي"""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)

    side = db.Column(db.String(10), nullable=False)     # يمين / يسار
    km_start = db.Column(db.Float, nullable=False)
    km_end = db.Column(db.Float)

    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)

    status = db.Column(db.String(30), default='قيد التنفيذ')
    work_date = db.Column(db.Date)

    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_by = db.relationship('User')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
