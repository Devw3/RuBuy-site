import os
from functools import wraps
import sqlite3
import time
import requests
from base import Database
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

UPLOAD_FOLDER = os.path.join('static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

app = Flask(__name__)
app.config.update({
    'DATABASE': 'instance/users.db',
    'SECRET_KEY': 'your-secure-secret-key',  # Замените на надежный секретный ключ
    'UPLOAD_FOLDER': UPLOAD_FOLDER
})

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs('instance', exist_ok=True)  # Убедимся, что папка instance существует

db = Database(app)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            flash('Пожалуйста, войдите для доступа к этой странице', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def index():
    user = session.get('user')
    if not user:
        return render_template('welcome.html')
    return render_template('index.html', user=user)

@app.route('/profile')
@login_required
def profile():
    user_id = session['user']['id']

    
    # Получаем текущий баланс
    current_balance = db.get_balance(user_id)
    current_balance = convert_rub_to_cny(current_balance)


    user = db.get_user(user_id)
    if not user:
        flash('Пользователь не найден', 'error')
        return redirect(url_for('login'))
    return render_template('profile.html', user=user, current_balance=current_balance)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user' in session:
        flash('Вы уже вошли в систему', 'info')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '').strip()
        region = request.form.get('region', '').strip()

        # Валидация
        if not all([name, password, region]):
            flash('Заполните все поля', 'error')
            return redirect(url_for('register'))
            
        if len(password) < 6:
            flash('Пароль должен содержать не менее 6 символов', 'error')
            return redirect(url_for('register'))

        # Создаем пользователя
        user_id = db.create_user(
            name=name,
            password=password,
            region=region,
            photo_path='static/default.png',
            is_admin=False
        )
        
        if not user_id:
            flash('Пользователь с таким именем уже существует', 'error')
            return redirect(url_for('register'))
        
        # Получаем только что созданного пользователя из БД
        new_user = db.get_user(user_id=user_id)
        if not new_user:
            flash('Ошибка при создании пользователя', 'error')
            return redirect(url_for('register'))
        
        # Сохраняем в сессию те же поля, что и при входе
        session['user'] = {
            'id': new_user['id'],
            'name': new_user['name'],
            'is_admin': bool(new_user.get('is_admin', False))
        }
        
        flash('Регистрация прошла успешно!', 'success')
        return redirect(url_for('index'))
        
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user' in session:
        flash('Вы уже вошли в систему', 'info')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        password = request.form.get('password', '').strip()

        if not name or not password:
            flash('Заполните все поля', 'error')
            return redirect(url_for('login'))
            
        # Получаем пользователя из БД
        user = db.get_user(name=name)
        
        if not user:
            flash('Пользователь не найден', 'error')
            return redirect(url_for('login'))
            
        # Проверяем пароль
        if not check_password_hash(user['password'], password):
            flash('Неверный пароль', 'error')
            return redirect(url_for('login'))
        
        # Проверяем is_admin (может быть 0/1 или True/False)
        is_admin = bool(user.get('is_admin', False))
        
        # Сохраняем в сессию
        session['user'] = {
            'id': user['id'],
            'name': user['name'],
            'is_admin': is_admin
        }
        
        flash('Вы успешно вошли в систему', 'success')
        return redirect(url_for('admin_panel' if is_admin else 'index'))
        
    return render_template('login.html')
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = session.get('user')
        if not user or not user.get('is_admin'):
            flash('Требуются права администратора', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/admin')
@admin_required
def admin_panel():
    user_id = session['user']['id']
    user = db.get_user(user_id)
    
    if not user:
        flash('Пользователь не найден', 'error')
        return redirect(url_for('login'))
    
    # Получаем все ожидающие заявки
    pending_replenishments = db.get_pending_replenishments()
    
    return render_template(
        'admin/admin_panel.html',
        user=user,
        replenishments=pending_replenishments  # Передаем заявки в шаблон
    )

def get_cny_to_rub_rate():
    try:
        url = "https://www.cbr-xml-daily.ru/daily_json.js"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        cny_rate = data["Valute"]["CNY"]["Value"]
        return cny_rate
    except Exception as e:
        app.logger.error(f"Ошибка при получении курса: {str(e)}")
        return None
    
def convert_rub_to_cny(rub_amount):
    cny_to_rub_rate = get_cny_to_rub_rate()
    if cny_to_rub_rate is None:
        return None
    
    rub_to_cny_rate = 1 / cny_to_rub_rate
    
    cny_amount = rub_amount * rub_to_cny_rate
    return round(cny_amount, 2)

@app.route('/logout')
@login_required
def logout():
    session.pop('user', None)
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('login'))

@app.route('/basket')
@login_required
def basket():
    return render_template('basket.html')

# Основное меню

@app.route('/main_menu/calculator')
@login_required
def calculator():
    return render_template('main_menu/calculator.html')

@app.route('/main_menu/net')
@login_required
def net():
    return render_template('main_menu/net.html')

@app.route('/main_menu/course')
@login_required
def course():
    rate = get_cny_to_rub_rate()
    return render_template('main_menu/curs.html', rate=rate)

# Профиль

@app.route('/profile/balance')
@login_required
def balance():
    user_id = session['user']['id']
    
    # Получаем текущий баланс
    current_balance = db.get_balance(user_id)
    current_balance = convert_rub_to_cny(current_balance)
    
    # Получаем историю транзакций
    transactions = db.get_balance_history(user_id)
    
    return render_template('profile/balance.html',
                         current_balance=current_balance,
                         transactions=transactions)

@app.route('/profile/replenishment', methods=['GET', 'POST'])
@login_required
def replenishment():
    if request.method == 'POST':
        # Проверка аутентификации
        if 'user' not in session:
            flash('Требуется авторизация', 'error')
            return redirect(url_for('login'))

        user_id = session['user']['id']
        
        # Валидация данных
        amount = request.form.get('amount')
        payment_date = request.form.get('payment_date')
        
        if not all([amount, payment_date]):
            flash('Заполните все обязательные поля', 'error')
            return redirect(url_for('replenishment'))
        
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError("Сумма должна быть положительной")
        except ValueError:
            flash('Некорректная сумма', 'error')
            return redirect(url_for('replenishment'))
        
        # Обработка файла
        if 'receipt' not in request.files:
            flash('Не прикреплен файл чека', 'error')
            return redirect(url_for('replenishment'))
        
        receipt = request.files['receipt']
        if receipt.filename == '':
            flash('Не выбран файл чека', 'error')
            return redirect(url_for('replenishment'))
            
        if not (receipt and allowed_file(receipt.filename)):
            flash('Недопустимый формат файла. Разрешены: jpg, png, pdf', 'error')
            return redirect(url_for('replenishment'))
        
        # Сохранение файла
        try:
            file_ext = receipt.filename.rsplit('.', 1)[1].lower()
            filename = secure_filename(f"receipt_{user_id}_{int(time.time())}.{file_ext}")
            receipt_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            
            # Создаем папку, если не существует
            # os.makedirs(os.path.dirname(receipt_path), exist_ok=True)
            receipt.save(receipt_path)
            
            # Проверяем, что файл сохранился
            if not os.path.exists(receipt_path):
                raise Exception("Файл не сохранился")
        except Exception as e:
            app.logger.error(f"Ошибка сохранения файла: {str(e)}")
            flash('Ошибка при сохранении чека', 'error')
            return redirect(url_for('replenishment'))
        
        # Создание заявки через метод класса DB (без комментария)
        try:
            replenishment_id = db.create_replenishment(
                user_id=user_id,
                amount=amount,
                payment_date=payment_date,
                receipt_path=filename
            )
            app.logger.info(f"Создана заявка на пополнение. ID: {replenishment_id}, User ID: {user_id}, Amount: {amount}")
            flash('Заявка на пополнение отправлена на рассмотрение', 'success')
        except Exception as e:
            app.logger.error(f"Ошибка при создании заявки: {str(e)}")
            flash('Ошибка при создании заявки', 'error')
            # Удаляем сохраненный файл, если заявка не создалась
            if os.path.exists(receipt_path):
                os.remove(receipt_path)
        
        return redirect(url_for('replenishment'))
    
    # GET запрос
    user_id = session['user']['id']
    user = db.get_user(user_id=user_id)
    
    if not user:
        flash('Пользователь не найден', 'error')
        return redirect(url_for('login'))
    
    return render_template('profile/replenishment.html',
                         current_balance=user.get('balance', 0),
                         user_id=user_id)

@app.route('/api/replenishments/<int:replenishment_id>/approve', methods=['POST'])
@admin_required
def approve_replenishment(replenishment_id):
    try:
        admin_id = session['user']['id']
        comment = request.form.get('comment', '')
        
        success = db.process_replenishment(
            replenishment_id=replenishment_id,
            action='approve',
            admin_id=admin_id,
            comment=comment
        )
        
        if success:
            return jsonify({'status': 'success', 'message': 'Заявка одобрена'})
        return jsonify({'status': 'error', 'message': 'Заявка не найдена или уже обработана'}), 400
        
    except Exception as e:
        app.logger.error(f"Error in approve_replenishment: {str(e)}")
        return jsonify({'status': 'error', 'message': 'Ошибка сервера'}), 500

@app.route('/api/replenishments/<int:replenishment_id>/reject', methods=['POST'])
@admin_required
def reject_replenishment(replenishment_id):
    try:
        if 'user' not in session or not session['user'].get('is_admin'):
            return jsonify({'status': 'error', 'message': 'Доступ запрещен'}), 403
            
        admin_id = session['user']['id']
        comment = request.form.get('comment', '') or 'Причина не указана'
        
        success = db.process_replenishment(
            replenishment_id=replenishment_id,
            action='reject',
            admin_id=admin_id,
            comment=comment
        )
        
        if success:
            return jsonify({'status': 'success', 'message': 'Заявка отклонена'})
        return jsonify({'status': 'error', 'message': 'Заявка не найдена или уже обработана'}), 400
            
    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({'status': 'error', 'message': 'Внутренняя ошибка сервера'}), 500
    

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)