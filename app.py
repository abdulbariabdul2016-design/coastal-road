import os
import uuid
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, flash, request,
    send_from_directory, abort, jsonify
)
from werkzeug.exceptions import RequestEntityTooLarge
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from werkzeug.utils import secure_filename

from config import Config
import storage
from models import db, User, Document, WorkRecord, DOCUMENT_CATEGORIES, SIDES, WORK_STATUSES


# بيانات الدخول الثابتة للأدمن. يُفضَّل ضبطها من متغيرات البيئة في Render
# (ADMIN_USERNAME / ADMIN_PASSWORD) بدل تركها هنا، خاصة إذا كان مستودع GitHub عامًا.
DEFAULT_ADMIN_USERNAME = 'admin'
DEFAULT_ADMIN_PASSWORD = 'Admin@2026'
DEFAULT_ADMIN_FULL_NAME = 'المدير العام'


def fixed_admin_credentials():
    return (
        os.environ.get('ADMIN_USERNAME') or DEFAULT_ADMIN_USERNAME,
        os.environ.get('ADMIN_PASSWORD') or DEFAULT_ADMIN_PASSWORD,
        os.environ.get('ADMIN_FULL_NAME') or DEFAULT_ADMIN_FULL_NAME,
    )


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    db.init_app(app)
    storage.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = 'login'
    login_manager.login_message = 'يرجى تسجيل الدخول للمتابعة'
    login_manager.login_message_category = 'warning'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # ------------------------------------------------------------------
    # تهيئة قاعدة البيانات تلقائيًا عند الإقلاع
    # ------------------------------------------------------------------
    # هذا ضروري عند التشغيل عبر gunicorn (كما في Render)، لأن الكتلة
    # `if __name__ == '__main__':` في نهاية هذا الملف لا تُنفَّذ إطلاقًا
    # في هذه الحالة (gunicorn يستورد المتغير app مباشرة ولا يشغّل الملف).
    with app.app_context():
        db.create_all()

        # حساب الأدمن الثابت: يُنشأ إن لم يكن موجودًا، ويُعاد ضبط كلمة مروره
        # على القيمة الثابتة في كل تشغيل، فلا يمكن فقدان الدخول أو تغيير كلمة
        # المرور بالخطأ. القيم تُؤخذ من متغيرات البيئة، وإلا من القيم الافتراضية.
        fixed_username, fixed_password, fixed_full_name = fixed_admin_credentials()
        fixed_admin = User.query.filter_by(username=fixed_username).first()
        if fixed_admin is None:
            fixed_admin = User(full_name=fixed_full_name, username=fixed_username,
                               role='admin')
            db.session.add(fixed_admin)
        fixed_admin.role = 'admin'
        fixed_admin.is_active_user = True
        fixed_admin.set_password(fixed_password)
        db.session.commit()

    # ------------------------------------------------------------------
    # أدوات مساعدة
    # ------------------------------------------------------------------
    def allowed_file(filename):
        safe_filename = secure_filename(filename)
        return '.' in safe_filename and \
            safe_filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

    def read_kml_map_data(file_path):
        """يقرأ نقاط المحطات وخطوط الطريق من ملف KML أو KMZ."""
        if file_path.lower().endswith('.kmz'):
            with zipfile.ZipFile(file_path) as archive:
                kml_names = [name for name in archive.namelist()
                             if name.lower().endswith('.kml')]
                if not kml_names:
                    raise ValueError('ملف KMZ لا يحتوي على ملف KML')
                kml_content = archive.read(kml_names[0])
        else:
            with open(file_path, 'rb') as kml_file:
                kml_content = kml_file.read()

        root = ET.fromstring(kml_content)
        points = []
        lines = []
        for placemark in root.iter():
            if placemark.tag.rsplit('}', 1)[-1] != 'Placemark':
                continue

            name = ''
            description = ''
            point_coordinates = None
            line_coordinates = []
            for element in placemark.iter():
                tag = element.tag.rsplit('}', 1)[-1]
                text = (element.text or '').strip()
                if tag == 'name' and text and not name:
                    name = text
                elif tag == 'description' and text:
                    description = text
                elif tag == 'Point':
                    coordinates_element = next(
                        (child for child in element.iter()
                         if child.tag.rsplit('}', 1)[-1] == 'coordinates'),
                        None
                    )
                    if coordinates_element is not None and coordinates_element.text:
                        point_coordinates = coordinates_element.text.strip().split()[0]
                elif tag == 'LineString':
                    coordinates_element = next(
                        (child for child in element.iter()
                         if child.tag.rsplit('}', 1)[-1] == 'coordinates'),
                        None
                    )
                    if coordinates_element is not None and coordinates_element.text:
                        line_coordinates.extend(coordinates_element.text.split())

            if point_coordinates:
                values = point_coordinates.split(',')
                if len(values) >= 2:
                    try:
                        points.append({
                            'name': name or 'محطة بدون اسم',
                            'description': description,
                            'lat': float(values[1]),
                            'lng': float(values[0])
                        })
                    except ValueError:
                        continue
            if line_coordinates:
                path = []
                for coordinate in line_coordinates:
                    values = coordinate.split(',')
                    if len(values) >= 2:
                        try:
                            path.append([float(values[1]), float(values[0])])
                        except ValueError:
                            continue
                if len(path) >= 2:
                    lines.append(path)
        return {'stations': points, 'lines': lines}

    @app.errorhandler(RequestEntityTooLarge)
    def handle_file_too_large(error):
        flash('حجم الملف أكبر من الحد المسموح (25 ميجابايت)', 'danger')
        return redirect(url_for('document_new'))

    def admin_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated or not current_user.is_admin:
                flash('هذه الصفحة متاحة للأدمن فقط', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return wrapper

    # ------------------------------------------------------------------
    # المصادقة
    # ------------------------------------------------------------------
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for('dashboard'))

        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            user = User.query.filter_by(username=username).first()

            if user and user.check_password(password) and user.is_active_user:
                login_user(user)
                flash(f'مرحبًا بك {user.full_name}', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('dashboard'))
            else:
                flash('اسم المستخدم أو كلمة المرور غير صحيحة، أو الحساب معطّل', 'danger')

        return render_template('login.html')

    @app.route('/logout')
    @login_required
    def logout():
        logout_user()
        flash('تم تسجيل الخروج بنجاح', 'info')
        return redirect(url_for('login'))

    # ------------------------------------------------------------------
    # لوحة التحكم الرئيسية
    # ------------------------------------------------------------------
    @app.route('/')
    @login_required
    def dashboard():
        stats = {
            'documents_total': Document.query.count(),
            'documents_tests': Document.query.filter_by(category='اختبار').count(),
            'documents_letters': Document.query.filter_by(category='رسالة').count(),
            'documents_other': Document.query.filter_by(category='أخرى').count(),
            'works_total': WorkRecord.query.count(),
            'works_right': WorkRecord.query.filter_by(side='يمين').count(),
            'works_left': WorkRecord.query.filter_by(side='يسار').count(),
            'works_in_progress': WorkRecord.query.filter_by(status='قيد التنفيذ').count(),
            'works_done': WorkRecord.query.filter_by(status='منتهي').count(),
        }
        recent_documents = Document.query.order_by(Document.created_at.desc()).limit(5).all()
        recent_works = WorkRecord.query.order_by(WorkRecord.created_at.desc()).limit(5).all()
        return render_template('dashboard.html', stats=stats,
                                recent_documents=recent_documents,
                                recent_works=recent_works)

    # ------------------------------------------------------------------
    # الأوراق (المستندات)
    # ------------------------------------------------------------------
    @app.route('/documents')
    @login_required
    def documents_list():
        category = request.args.get('category', '')
        side = request.args.get('side', '')

        query = Document.query
        if category:
            query = query.filter_by(category=category)
        if side:
            query = query.filter_by(side=side)

        docs = query.order_by(Document.created_at.desc()).all()
        return render_template('documents.html', documents=docs,
                                categories=DOCUMENT_CATEGORIES, sides=SIDES,
                                selected_category=category, selected_side=side)

    @app.route('/documents/new', methods=['GET', 'POST'])
    @login_required
    def document_new():
        if request.method == 'POST':
            title = request.form.get('title', '').strip()
            category = request.form.get('category')
            description = request.form.get('description', '').strip()
            side = request.form.get('side') or None
            km_point = request.form.get('km_point') or None

            if not title or category not in DOCUMENT_CATEGORIES:
                flash('يرجى إدخال عنوان الورقة واختيار النوع بشكل صحيح', 'danger')
                return redirect(url_for('document_new'))

            doc = Document(
                title=title,
                category=category,
                description=description,
                side=side,
                km_point=float(km_point) if km_point else None,
                uploaded_by_id=current_user.id
            )

            file = request.files.get('file')
            if file and file.filename:
                if not allowed_file(file.filename):
                    flash('نوع الملف غير مسموح به. استخدم PDF أو Word أو Excel أو صورة.', 'danger')
                    return redirect(url_for('document_new'))
                original_name = secure_filename(file.filename)
                ext = original_name.rsplit('.', 1)[1].lower()
                stored_name = f"{uuid.uuid4().hex}.{ext}"
                try:
                    storage.save_file(file.stream, stored_name, file.mimetype)
                except Exception:
                    app.logger.exception('فشل رفع الملف إلى التخزين')
                    flash('تعذر رفع الملف إلى التخزين. حاول مرة أخرى.', 'danger')
                    return redirect(url_for('document_new'))
                doc.stored_file_name = stored_name
                doc.original_file_name = original_name

            db.session.add(doc)
            db.session.commit()
            flash('تم حفظ الورقة بنجاح', 'success')
            return redirect(url_for('documents_list'))

        return render_template('document_form.html', categories=DOCUMENT_CATEGORIES, sides=SIDES)

    @app.route('/documents/<int:doc_id>/download')
    @login_required
    def document_download(doc_id):
        doc = Document.query.get_or_404(doc_id)
        if not doc.has_file:
            abort(404)
        if storage.is_cloud():
            return redirect(storage.download_url(doc.stored_file_name, doc.original_file_name))
        return send_from_directory(storage.local_folder(), doc.stored_file_name,
                                    as_attachment=True, download_name=doc.original_file_name)

    @app.route('/documents/<int:doc_id>/delete', methods=['POST'])
    @login_required
    def document_delete(doc_id):
        doc = Document.query.get_or_404(doc_id)
        if not (current_user.is_admin or doc.uploaded_by_id == current_user.id):
            flash('لا تملك صلاحية حذف هذه الورقة', 'danger')
            return redirect(url_for('documents_list'))

        if doc.has_file:
            storage.delete_file(doc.stored_file_name)

        db.session.delete(doc)
        db.session.commit()
        flash('تم حذف الورقة', 'info')
        return redirect(url_for('documents_list'))

    # ------------------------------------------------------------------
    # حصر الأعمال
    # ------------------------------------------------------------------
    @app.route('/works')
    @login_required
    def works_list():
        side = request.args.get('side', '')
        status = request.args.get('status', '')

        query = WorkRecord.query
        if side:
            query = query.filter_by(side=side)
        if status:
            query = query.filter_by(status=status)

        works = query.order_by(WorkRecord.km_start.asc()).all()
        return render_template('works.html', works=works, sides=SIDES,
                                statuses=WORK_STATUSES,
                                selected_side=side, selected_status=status)

    @app.route('/works/new', methods=['GET', 'POST'])
    @login_required
    def work_new():
        if request.method == 'POST':
            title = request.form.get('title', '').strip()
            side = request.form.get('side')
            km_start = request.form.get('km_start')
            km_end = request.form.get('km_end') or None
            description = request.form.get('description', '').strip()
            status = request.form.get('status', 'قيد التنفيذ')
            work_date = request.form.get('work_date') or None
            latitude = request.form.get('latitude') or None
            longitude = request.form.get('longitude') or None

            if not title or side not in SIDES or not km_start:
                flash('يرجى تعبئة العنوان والجهة ونقطة بداية الكيلومتر', 'danger')
                return redirect(url_for('work_new'))

            work = WorkRecord(
                title=title,
                description=description,
                side=side,
                km_start=float(km_start),
                km_end=float(km_end) if km_end else None,
                status=status,
                work_date=datetime.strptime(work_date, '%Y-%m-%d').date() if work_date else None,
                latitude=float(latitude) if latitude else None,
                longitude=float(longitude) if longitude else None,
                created_by_id=current_user.id
            )
            db.session.add(work)
            db.session.commit()
            flash('تم إضافة سجل العمل بنجاح', 'success')
            return redirect(url_for('works_list'))

        return render_template('work_form.html', sides=SIDES, statuses=WORK_STATUSES,
                                road_length=app.config['ROAD_LENGTH_KM'], work=None)

    @app.route('/works/<int:work_id>/edit', methods=['GET', 'POST'])
    @login_required
    def work_edit(work_id):
        work = WorkRecord.query.get_or_404(work_id)
        if not (current_user.is_admin or work.created_by_id == current_user.id):
            flash('لا تملك صلاحية تعديل هذا السجل', 'danger')
            return redirect(url_for('works_list'))

        if request.method == 'POST':
            work.title = request.form.get('title', '').strip()
            work.side = request.form.get('side')
            work.km_start = float(request.form.get('km_start'))
            km_end = request.form.get('km_end') or None
            work.km_end = float(km_end) if km_end else None
            work.description = request.form.get('description', '').strip()
            work.status = request.form.get('status', 'قيد التنفيذ')
            work_date = request.form.get('work_date') or None
            work.work_date = datetime.strptime(work_date, '%Y-%m-%d').date() if work_date else None
            latitude = request.form.get('latitude') or None
            longitude = request.form.get('longitude') or None
            work.latitude = float(latitude) if latitude else None
            work.longitude = float(longitude) if longitude else None

            db.session.commit()
            flash('تم تحديث سجل العمل', 'success')
            return redirect(url_for('works_list'))

        return render_template('work_form.html', sides=SIDES, statuses=WORK_STATUSES,
                                road_length=app.config['ROAD_LENGTH_KM'], work=work)

    @app.route('/works/<int:work_id>/delete', methods=['POST'])
    @login_required
    def work_delete(work_id):
        work = WorkRecord.query.get_or_404(work_id)
        if not (current_user.is_admin or work.created_by_id == current_user.id):
            flash('لا تملك صلاحية حذف هذا السجل', 'danger')
            return redirect(url_for('works_list'))

        db.session.delete(work)
        db.session.commit()
        flash('تم حذف سجل العمل', 'info')
        return redirect(url_for('works_list'))

    # ------------------------------------------------------------------
    # الخريطة
    # ------------------------------------------------------------------
    @app.route('/map')
    @login_required
    def map_view():
        return render_template('map.html')

    @app.route('/map/stations/upload', methods=['POST'])
    @login_required
    def stations_upload():
        station_file = request.files.get('stations_file')
        if not station_file or not station_file.filename:
            flash('اختر ملف KMZ أو KML أولًا', 'danger')
            return redirect(url_for('map_view'))

        filename = secure_filename(station_file.filename)
        extension = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        if extension not in {'kmz', 'kml'}:
            flash('يسمح فقط بملفات KMZ أو KML', 'danger')
            return redirect(url_for('map_view'))

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = os.path.join(tmp_dir, 'stations.' + extension)
            station_file.save(tmp_path)
            try:
                points = read_kml_map_data(tmp_path)['stations']
            except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile):
                flash('تعذر قراءة ملف المحطات. تأكد أنه ملف KMZ/KML صالح.', 'danger')
                return redirect(url_for('map_view'))

            if not points:
                flash('لم يتم العثور على نقاط بإحداثيات داخل الملف', 'danger')
                return redirect(url_for('map_view'))

            try:
                with open(tmp_path, 'rb') as saved:
                    storage.save_file(saved, 'stations/stations.' + extension,
                                      'application/octet-stream')
            except Exception:
                app.logger.exception('فشل رفع ملف المحطات')
                flash('تعذر حفظ ملف المحطات في التخزين.', 'danger')
                return redirect(url_for('map_view'))

        other_extension = 'kml' if extension == 'kmz' else 'kmz'
        storage.delete_file('stations/stations.' + other_extension)
        flash(f'تم تحميل {len(points)} محطة بنجاح', 'success')
        return redirect(url_for('map_view'))

    @app.route('/api/stations')
    @login_required
    def stations_geo():
        # الأولوية للملف المرفوع من المستخدم (في التخزين)، ثم الملف الافتراضي
        # المرفق مع المشروع في static/data.
        for extension in ('kmz', 'kml'):
            data = storage.read_bytes('stations/stations.' + extension)
            if data is None:
                continue
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = os.path.join(tmp_dir, 'stations.' + extension)
                with open(tmp_path, 'wb') as out:
                    out.write(data)
                try:
                    return jsonify(read_kml_map_data(tmp_path))
                except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile):
                    return jsonify([])

        default_dir = os.path.join(app.root_path, 'static', 'data')
        for extension in ('kmz', 'kml'):
            station_path = os.path.join(default_dir, 'stations.' + extension)
            if os.path.exists(station_path):
                try:
                    return jsonify(read_kml_map_data(station_path))
                except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile):
                    return jsonify([])
        return jsonify([])

    @app.route('/api/works-geo')
    @login_required
    def works_geo():
        """يعيد سجلات الأعمال التي تحتوي على إحداثيات GPS بصيغة JSON لعرضها على الخريطة"""
        works = WorkRecord.query.filter(
            WorkRecord.latitude.isnot(None), WorkRecord.longitude.isnot(None)
        ).all()
        data = []
        for w in works:
            data.append({
                'id': w.id,
                'title': w.title,
                'side': w.side,
                'km_start': w.km_start,
                'km_end': w.km_end,
                'status': w.status,
                'lat': w.latitude,
                'lng': w.longitude,
                'work_date': w.work_date.strftime('%Y-%m-%d') if w.work_date else '',
                'description': w.description or ''
            })
        return jsonify(data)

    # ------------------------------------------------------------------
    # إدارة المستخدمين (أدمن فقط)
    # ------------------------------------------------------------------
    @app.route('/users')
    @login_required
    @admin_required
    def users_list():
        users = User.query.order_by(User.created_at.asc()).all()
        return render_template('users.html', users=users)

    @app.route('/users/new', methods=['GET', 'POST'])
    @login_required
    @admin_required
    def user_new():
        if request.method == 'POST':
            full_name = request.form.get('full_name', '').strip()
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            role = request.form.get('role', 'employee')

            if not full_name or not username or not password:
                flash('يرجى تعبئة جميع الحقول', 'danger')
                return redirect(url_for('user_new'))

            if User.query.filter_by(username=username).first():
                flash('اسم المستخدم موجود مسبقًا', 'danger')
                return redirect(url_for('user_new'))

            user = User(full_name=full_name, username=username, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash('تم إنشاء المستخدم بنجاح', 'success')
            return redirect(url_for('users_list'))

        return render_template('user_form.html')

    @app.route('/users/<int:user_id>/toggle-active', methods=['POST'])
    @login_required
    @admin_required
    def user_toggle_active(user_id):
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash('لا يمكنك تعطيل حسابك الخاص', 'danger')
            return redirect(url_for('users_list'))
        if user.username == fixed_admin_credentials()[0]:
            flash('لا يمكن تعطيل حساب الأدمن الثابت', 'danger')
            return redirect(url_for('users_list'))
        user.is_active_user = not user.is_active_user
        db.session.commit()
        flash('تم تحديث حالة المستخدم', 'info')
        return redirect(url_for('users_list'))

    @app.route('/users/<int:user_id>/delete', methods=['POST'])
    @login_required
    @admin_required
    def user_delete(user_id):
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash('لا يمكنك حذف حسابك الخاص', 'danger')
            return redirect(url_for('users_list'))
        if user.username == fixed_admin_credentials()[0]:
            flash('لا يمكن حذف حساب الأدمن الثابت', 'danger')
            return redirect(url_for('users_list'))
        db.session.delete(user)
        db.session.commit()
        flash('تم حذف المستخدم', 'info')
        return redirect(url_for('users_list'))

    # ------------------------------------------------------------------
    # أوامر CLI مساعدة: تهيئة قاعدة البيانات وإنشاء أول حساب أدمن
    # ------------------------------------------------------------------
    @app.cli.command('init-db')
    def init_db():
        """إنشاء جداول قاعدة البيانات"""
        db.create_all()
        print('تم إنشاء قاعدة البيانات بنجاح.')

    @app.cli.command('create-admin')
    def create_admin():
        """إنشاء أول حساب أدمن بشكل تفاعلي"""
        full_name = input('الاسم الكامل: ')
        username = input('اسم المستخدم: ')
        password = input('كلمة المرور: ')

        if User.query.filter_by(username=username).first():
            print('اسم المستخدم موجود مسبقًا!')
            return

        user = User(full_name=full_name, username=username, role='admin')
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f'تم إنشاء حساب الأدمن "{username}" بنجاح.')

    return app


app = create_app()

if __name__ == '__main__':
    # يُستخدم فقط عند التشغيل المحلي المباشر (python app.py).
    # على Render يقوم gunicorn (حسب Procfile) باستدعاء app مباشرة، ولا يمر بهذا الشرط إطلاقًا.
    local_port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=local_port)
