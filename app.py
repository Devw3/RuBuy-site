import os
from functools import wraps
import sqlite3
import time
import json
from datetime import timedelta
import requests
import random
from threading import Thread
from dotenv import load_dotenv
from base import Database
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from parser.taobao import parse_taobao_product
from parser.weidian import parse_weidian_product

load_dotenv()
UPLOAD_FOLDER = os.path.join('static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

API_TOKEN = os.getenv("API_TOKEN")
LOGISTIC_MARKUP_PCT = float(os.getenv("LOGISTIC_MARKUP_PCT", 0.03))
SECRET_KEY = os.getenv("SECRET_KEY")
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
DATABASE = os.getenv("DATABASE", "instance/users.db")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin1")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "123456")


app = Flask(__name__)
app.config.update({
    'DATABASE': DATABASE,
    'SECRET_KEY': SECRET_KEY,
    'UPLOAD_FOLDER': UPLOAD_FOLDER,
    'PERMANENT_SESSION_LIFETIME': timedelta(days=7)
})

DELIVERY_RATES = {
    'air_fast': 40.0,
    'air_slow': 9.2,
    'auto_fast': 8.2
}

SHIPMENT_STATUSES = {
    "pending":    "Ожидает обработки",
    "processing": "В обработке",
    "shipped":    "В пути",
    "delivered":  "Доставлено",
}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs('instance', exist_ok=True)  # Убедимся, что папка instance существует
db = Database(app)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session or not session['user'].get('id'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
@login_required
def index():
    user = session.get('user')
    if not user or not user.get('id'):
        return redirect(url_for('login'))
    
    # Иначе — показываем index
    return render_template('index.html', user=user)

def start_cleanup_loop(db):
    def run():
        with app.app_context():
            while True:
                db.clean_old_temporary_models()
                time.sleep(600)  # каждые 10 минут

    thread = Thread(target=run, daemon=True)
    thread.start()

@app.route('/profile')
@login_required
def profile():
    user_id = session['user']['id']
    
    # Получаем данные пользователя
    user = db.get_user(user_id)
    if not user:
        flash('Пользователь не найден', 'error')
        return redirect(url_for('login'))
    
    # Получаем балансы (рубли и юани)
    balance_rub = db.get_balance(user_id)['rub']
    balance_cny = db.get_balance(user_id)['cny']

    
    return render_template('profile.html', 
                         user=user,
                         balance_rub=balance_rub,
                         balance_cny=balance_cny)

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
    pending_withdrawals = db.get_pending_withdrawals()
    orders = db.get_pending_orders()  
    pending_shipments = db.get_pending_shipments()
    print("DEBUG pending_shipments:", pending_shipments)
    for s in pending_shipments:
        print("DEBUG shipment:", s['id'], type(s['model_ids']), s['model_ids'])

    return render_template(
        'admin/admin_panel.html',
        user=user,
        replenishments=pending_replenishments,
        withdrawals=pending_withdrawals,
        orders=orders, 
        shipments=pending_shipments
    )

@app.route('/admin/shipments/<int:shipment_id>/packaging', methods=['POST'])
def admin_set_packaging(shipment_id):
    # TODO: тут проверь, что user — админ
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get('amount_cny', 0))
    except Exception:
        return jsonify({'success': False, 'error': 'amount_cny должен быть числом'}), 400
    if amount < 0:
        return jsonify({'success': False, 'error': 'Сумма не может быть отрицательной'}), 400

    try:
        with db.get_cursor() as cursor:
            # проверим, что посылка есть
            row = cursor.execute(
                "SELECT id FROM order_shipments WHERE id = ?", (shipment_id,)
            ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Посылка не найдена'}), 404

            cursor.execute("""
                UPDATE order_shipments
                SET packaging_cost = ?, packaging_paid = 0
                WHERE id = ?
            """, (amount, shipment_id))
        return jsonify({'success': True})
    except Exception as e:
        app.logger.exception("admin_set_packaging failed: %s", e)
        return jsonify({'success': False, 'error': 'DB error'}), 500

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
    
def fetch_cbr_rates():
    try:
        url = "https://www.cbr-xml-daily.ru/daily_json.js"
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        data = r.json()
        usd = data["Valute"]["USD"]["Value"]
        cny = data["Valute"]["CNY"]["Value"]
        return {"USD": float(usd), "CNY": float(cny)}
    except Exception as e:
        app.logger.error(f"Ошибка при получении курса ЦБ: {e}")
        return None
    
def convert_rub_to_cny(rub_amount):
    cny_to_rub_rate = get_cny_to_rub_rate()
    if cny_to_rub_rate is None:
        return None
    rub_to_cny_rate = 1 / cny_to_rub_rate
    cny_amount = rub_amount * rub_to_cny_rate
    
    return round(cny_amount, 2)

def convert_cny_to_rub(cny_amount):
    cny_to_rub_rate = get_cny_to_rub_rate()
    if cny_to_rub_rate is None:
        return None
    
    rub_amount = cny_amount * cny_to_rub_rate
    return round(rub_amount, 2)

@app.route('/logout')
@login_required
def logout():
    session.pop('user', None)
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('login'))

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

@app.route('/terms')
@login_required
def terms():
    return render_template('terms.html')


# Профиль

@app.route('/profile/balance')
@login_required
def balance():
    user_id = session['user']['id']
    
    # Получаем текущие балансы
    balance_rub = db.get_balance(user_id)['rub']
    balance_cny = db.get_balance(user_id)['cny']
    
    # Получаем историю транзакций
    transactions = db.get_balance_history(user_id)
    print(transactions)
    
    return render_template('profile/balance.html',
                         balance_rub=balance_rub,
                         balance_cny=balance_cny,
                         transactions=transactions)

@app.route('/profile/replenishment', methods=['GET', 'POST'])
@login_required
def replenishment():
    if request.method == 'POST':
        # Проверяем, это AJAX-запрос или обычная форма
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        
        try:
            # Валидация данных
            amount = request.form.get('amount')
            payment_date = request.form.get('payment_date')
            receipt = request.files.get('receipt')
            
            if not all([amount, payment_date, receipt]):
                error_msg = 'Заполните все обязательные поля'
                if is_ajax:
                    return jsonify({'success': False, 'message': error_msg}), 400
                flash(error_msg, 'error')
                return redirect(url_for('replenishment'))
            
            amount_rub = float(amount)
            amount_cny = convert_rub_to_cny(amount_rub)
            if amount_rub <= 0:
                raise ValueError("Сумма должна быть положительной")
            
            # Сохранение файла
            file_ext = receipt.filename.rsplit('.', 1)[1].lower()
            filename = secure_filename(f"receipt_{session['user']['id']}_{int(time.time())}.{file_ext}")
            receipt_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            receipt.save(receipt_path)
            
            # Создание заявки
            replenishment_id = db.create_replenishment(
                user_id=session['user']['id'],
                amount_rub=amount_rub,
                amount_cny=amount_cny,
                payment_date=payment_date,
                receipt_path=filename
            )
            
            if is_ajax:
                return jsonify({
                    'success': True,
                    'replenishment_id': replenishment_id,
                    'message': 'Заявка успешно создана'
                })
            
            flash('Заявка на пополнение отправлена на рассмотрение', 'success')
            return redirect(url_for('replenishment'))
            
        except Exception as e:
            # Логируем полную информацию об ошибке
            app.logger.error("Ошибка при обработке пополнения:", exc_info=True)
            
            # Для AJAX возвращаем больше информации
            if is_ajax:
                return jsonify({
                    'success': False,
                    'message': str(e),
                    'type': type(e).__name__,
                    'traceback': traceback.format_exc()
                }), 500
            
            # Для обычных запросов
            flash(f'Ошибка: {str(e)}', 'error')
            return redirect(url_for('replenishment'))  
          
    # GET запрос
    user_id = session['user']['id']
    balance_rub = db.get_balance(user_id)['rub']
    balance_cny = db.get_balance(user_id)['cny']

    return render_template('profile/replenishment.html',
                         balance_rub=balance_rub,
                         balance_cny=balance_cny)


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
    
@app.route('/profile/withdraw', methods=['GET', 'POST'])
@login_required
def withdraw():
    user_id = session['user']['id']
    
    if request.method == 'GET':
        try:
            balance_rub = db.get_balance(user_id)['rub']
            balance_cny = db.get_balance(user_id)['cny']
            withdrawals = db.get_user_withdrawals(user_id)
            
            return render_template('profile/withdraw.html',
                                current_balance=balance_cny,
                                balance_rub=balance_rub,
                                withdrawals=withdrawals)
        
        except Exception as e:
            print(f"Error in GET /withdraw: {str(e)}")
            flash('Ошибка при получении данных', 'error')
            return redirect(url_for('profile'))

    elif request.method == 'POST':
        try:
            # Получение и очистка данных
            card_number = request.form.get('card_number', '').replace(' ', '')
            card_holder = request.form.get('card_holder', '').strip()
            amount = float(request.form.get('amount', 0))
            name = request.form.get('name', '').strip()
            
            # Валидация данных
            errors = []
            if amount < 100:
                errors.append('Минимальная сумма вывода - 100 ₽')
            
            current_balance = db.get_balance(user_id)['rub']
            if amount > current_balance:
                errors.append('Недостаточно средств на балансе')
            
            if len(card_number) != 16 or not card_number.isdigit():
                errors.append('Некорректный номер карты (требуется 16 цифр)')
            
            if not card_holder or not all(c.isalpha() or c.isspace() for c in card_holder):
                errors.append('Некорректное имя держателя карты (только буквы и пробелы)')
            
            if errors:
                for error in errors:
                    flash(error, 'error')
                return redirect(url_for('withdraw'))
            
            # Создание заявки
            success = db.create_withdrawal(
                user_id=user_id,
                amount=amount,
                card_number=card_number,
                card_holder=card_holder.upper(),
                name=name
            )
            
            if not success:
                flash('Ошибка при создании заявки', 'error')
                return redirect(url_for('withdraw'))
            
            flash('Заявка на вывод создана и ожидает обработки', 'success')
            return redirect(url_for('withdraw'))
            
        except ValueError:
            flash('Некорректная сумма', 'error')
            return redirect(url_for('withdraw'))
        except Exception as e:
            print(f"Error in POST /withdraw: {str(e)}")
            flash('Произошла ошибка при обработке запроса', 'error')
            return redirect(url_for('withdraw'))

@app.route('/admin/withdrawals/<int:withdrawal_id>/<action>', methods=['POST'])
@admin_required
def handle_withdrawal_action(withdrawal_id, action):
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Неверный формат данных'}), 400
            
        comment = data.get('comment', '')
        
        if action not in ['approve', 'reject']:
            return jsonify({'error': 'Некорректное действие'}), 400
        
        # Получаем информацию о выводе
        withdrawal = db.get_withdrawal_by_id(withdrawal_id)
        if not withdrawal:
            return jsonify({'error': 'Заявка не найдена'}), 404
        
        if withdrawal['status'] != 'pending':
            return jsonify({'error': 'Заявка уже обработана'}), 400
        
        new_status = 'approved' if action == 'approve' else 'rejected'
        user_id = withdrawal['user_id']
        amount = withdrawal['amount']
        
        # Для подтверждения - проверяем баланс и списываем средства
        if action == 'approve':
            user_balance = db.get_user_balance(user_id)
            db.update_balance_rub(user_id, -amount)
            amount_cny = convert_rub_to_cny(amount)
            db.update_balance_cny(user_id, -amount_cny)

            if user_balance < amount:
                return jsonify({'error': 'Недостаточно средств на балансе'}), 400
        
        # Обновляем статус заявки
        success = db.update_withdrawal_status(
            withdrawal_id=withdrawal_id,
            status=new_status,
            comment=comment
        )
        
        if not success:
            # Если не удалось обновить статус - отменяем изменения баланса
            if action == 'approve':
                db.update_balance(user_id, amount)
            return jsonify({'error': 'Ошибка обновления статуса'}), 500
            
        return jsonify({
            'success': True,
            'message': f'Заявка #{withdrawal_id} успешно {new_status}',
            'new_status': new_status,
            'new_balance': db.get_user_balance(user_id)
        })
        
    except Exception as e:
        print(f"Error in withdrawal processing: {str(e)}")
        return jsonify({'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/admin/withdrawals/data')
@admin_required
def get_withdrawals_data():
    try:
        withdrawals = db.get_pending_withdrawals()
        return jsonify(withdrawals)  # Возвращаем список напрямую
    except Exception as e:
        print(f"Error getting withdrawals data: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/add_product', methods=['POST'])
def add_product():
    url = request.form['product_url'].strip()
    
    try:
        if 'taobao.com' in url or 'tmall.com' in url:
            product = parse_taobao_product(API_TOKEN, url)
        elif 'weidian.com' in url:
            product = parse_weidian_product(url)
        else:
            return render_template('error.html', message="Неподдерживаемый сайт")
        
        product_id = db.add_product(product, url)
        
        return redirect(url_for('product_page', product_id=product_id))
    
    except Exception as e:
        return render_template('error.html', message=f"Ошибка: {str(e)}")


@app.route('/product/<int:product_id>')
def product_page(product_id):
    try:
        product_data = db.get_product_with_models(product_id)
        
        return render_template('product.html', 
                             product=product_data['product'],
                             variants=product_data['variants'],
                             models=product_data['models'])
    
    except ValueError as e:
        return render_template('error.html', message=str(e))
    except Exception as e:
        return render_template('error.html', message=f"Ошибка: {str(e)}")
    

# товары в корзину

@app.route('/add-to-cart', methods=['POST'])
@login_required
def add_to_cart():
    try:
        data = request.get_json()
        model_id = data['model_id']
        quantity = int(data.get('quantity', 1))
        user_id = session['user']['id']

        # Проверяем наличие и сток
        model = db.get_model_info(model_id)
        if not model:
            return jsonify(success=False, error='Модель не найдена'), 404
        if model['stock'] < quantity:
            return jsonify(success=False, error='Недостаточно товара в наличии'), 400

        # Добавляем в корзину
        db.add_cart_item(user_id, model_id, quantity)

        # Обновляем сток и статус
        with db.get_cursor() as cursor:
            cursor.execute(
                'UPDATE models SET stock = stock - ? WHERE id = ? AND stock >= ?',
                (quantity, model_id, quantity)
            )
            cursor.execute(
                """
                UPDATE models
                SET status = 'in_cart'
                WHERE id = ?
                """, (model_id,)
            )

        # Возвращаем обновлённую корзину
        items = db.get_cart_items(user_id)
        return jsonify(
            success=True,
            message='Товар добавлен в корзину',
            cart_count=len(items),
            cart_items=items
        )
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500
    
@app.route('/basket')
@login_required
def basket():
    try:
        # Берём из БД
        user_id = session['user']['id']
        items = db.get_cart_items(user_id)
        for it in items:
            if 'color_name' in it:
                it['color'] = it.pop('color_name')
            if 'size_name' in it:
                it['size'] = it.pop('size_name')

        # Считаем общую сумму
        total = sum(it['price'] * it['quantity'] for it in items)

        return render_template('basket.html', cart=items, total=total)
    except Exception as e:
        app.logger.error(f"Ошибка при загрузке корзины для user_id={user_id}: {e}")
        return render_template('basket.html', cart=[], total=0)

@app.route('/remove-cart-item', methods=['POST'])
@login_required
def remove_cart_item():
    data = request.get_json() or {}
    model_id = data.get('model_id')
    
    if not model_id:
        return jsonify(success=False, error="Нет model_id"), 400

    try:
        model_id = int(model_id)
    except:
        return jsonify(success=False, error="Неверный ID"), 400

    deleted = db.remove_from_cart(model_id, session['user']['id'])
    
    if deleted:
        return jsonify(success=True)
    else:
        return jsonify(success=False, error="Товар не найден в корзине"), 404
        
@app.route('/checkout/init', methods=['POST'])
@login_required
def checkout_init():
    data = request.get_json()
    print("Полученные товары:", data)
    items = data.get('items', [])
    if not items:
        return jsonify(success=False, error="Нет товаров"), 400

    # сохраняем в сессии
    session['checkout_items'] = items
    return jsonify(success=True)
    
@app.route('/checkout', methods=['GET'])
@login_required
def checkout():
    items = session.get('checkout_items')
    if not items:
        return redirect(url_for('basket'))

    user_id = session['user']['id']
    balance_cny = db.get_balance(user_id)['cny']

    cart_items = []
    total = 0.0

    for entry in items:
        model_id = entry['model_id']
        qty = int(entry['quantity'])
        
        row = db.get_model_info(model_id)

        if not row:
            continue

        item_total = row['price'] * qty
        total += item_total

        cart_items.append({
            'model_id': row['id'],
            'color': row['color_name'],
            'size': row['size_name'],
            'price': row['price'],
            'quantity': qty,
            'image_url': row['image_url'],
        })

    return render_template('checkout.html',
        cart_items=cart_items,
        total=total,
        balance=balance_cny
    )

@app.route('/process-payment', methods=['POST'])
@login_required
def process_payment():
    try:
        data = request.get_json() or {}
        items = data.get('items') or []
        services = data.get('services', [])

        if not isinstance(items, list) or not items:
            return jsonify(success=False, error='Неверные данные товаров'), 400

        # 1) Собираем детальную информацию по товарам и считаем total_products
        detailed = []
        total_products = 0.0
        for idx, it in enumerate(items):
            raw_mid = it.get('model_id')
            if raw_mid is None:
                return jsonify(success=False, error=f'model_id не указан в позиции {idx+1}'), 400
            try:
                model_id = int(raw_mid)
            except ValueError:
                return jsonify(success=False, error=f'Некорректный model_id в позиции {idx+1}'), 400

            raw_qty = it.get('quantity', 1)
            try:
                qty = int(raw_qty)
            except (ValueError, TypeError):
                qty = 1

            row = db.get_model_info(model_id)
            if not row:
                return jsonify(success=False, error=f'Модель {model_id} не найдена'), 404

            price = float(row['price'])
            product_cost = price * qty
            total_products += product_cost
            detailed.append({
                'model_id': model_id,
                'quantity': qty,
                'price': price,
                'product_cost': product_cost
            })

        # 2) Считаем услуги
        service_prices = {'photos': 10, 'video': 30, 'inspection': 20}
        total_quantity = sum(it['quantity'] for it in detailed)
        total_services = sum(service_prices.get(s, 0) for s in services) * total_quantity

        # 3) Рассчитываем стоимость услуг для каждого товара пропорционально
        for item in detailed:
            if total_products > 0:
                # Распределяем услуги пропорционально стоимости товара
                item['service_cost'] = total_services * (item['product_cost'] / total_products)
            else:
                item['service_cost'] = total_services / len(detailed) if detailed else 0

            # Общая стоимость товара с учетом услуг
            item['total_price'] = item['product_cost'] + item['service_cost']

        # 4) Общая стоимость в юанях
        total_cny = total_products + total_services

        # 5) Проверяем баланс CNY и списываем
        user_id = session['user']['id']
        balance_cny = db.get_balance(user_id)['cny']
        if total_cny > balance_cny:
            return jsonify(success=False, error='Недостаточно средств на балансе CNY'), 400
        db.update_balance_cny(user_id, -total_cny)

        # 6) Конвертируем и списываем рубли
        total_rub = convert_cny_to_rub(total_cny)
        db.update_balance_rub(user_id, -total_rub)

        # 7) Генерируем уникальный трек-номер для заказа
        with db.get_cursor() as cursor:
            while True:
                rand_num = random.randint(100000000, 9999999999)
                our_track = f'RUB{rand_num}'
                cursor.execute(
                    "SELECT COUNT(*) AS cnt FROM orders WHERE our_tracking_number = ?",
                    (our_track,)
                )
                if cursor.fetchone()['cnt'] == 0:
                    break

        # --- Вспомогательная функция: удаляет/уменьшает товар в корзине (проверяет несколько возможных таблиц) ---
        def remove_from_cart(cursor, user_id, model_id, qty_to_remove):
            candidate_tables = ['cart_items', 'cart', 'basket', 'user_cart']
            for table in candidate_tables:
                try:
                    # 1) Попробуем прочитать запись — проверим, есть ли колонка quantity
                    cursor.execute(f"SELECT * FROM {table} WHERE user_id = ? AND model_id = ?", (user_id, model_id))
                    row = cursor.fetchone()
                except Exception:
                    # Таблица не существует или ошибка — пробуем следующую
                    continue

                if not row:
                    # Если таблица есть, но записи нет — ничего делать не нужно, считаем успешно
                    return True

                # Если у записи есть поле 'quantity' — уменьшаем
                if 'quantity' in row.keys():
                    try:
                        current_qty = int(row['quantity'] or 0)
                    except Exception:
                        current_qty = 0
                    new_qty = current_qty - int(qty_to_remove)
                    if new_qty > 0:
                        try:
                            cursor.execute(f"UPDATE {table} SET quantity = ? WHERE user_id = ? AND model_id = ?",
                                           (new_qty, user_id, model_id))
                        except Exception:
                            # если UPDATE неожиданно упал — пробуем удалить
                            try:
                                cursor.execute(f"DELETE FROM {table} WHERE user_id = ? AND model_id = ?",
                                               (user_id, model_id))
                            except Exception:
                                pass
                    else:
                        # удаляем запись целиком
                        try:
                            cursor.execute(f"DELETE FROM {table} WHERE user_id = ? AND model_id = ?",
                                           (user_id, model_id))
                        except Exception:
                            pass
                    # После успешной операции — возвращаем True
                    return True
                else:
                    # Если quantity нет — просто удаляем строку
                    try:
                        cursor.execute(f"DELETE FROM {table} WHERE user_id = ? AND model_id = ?",
                                       (user_id, model_id))
                        return True
                    except Exception:
                        # не получилось удалить из этой таблицы — пробуем следующую
                        continue

            # Если не нашли подходящей таблицы — считаем это нефатальной ситуацией и возвращаем True
            # (можно логировать это место)
            return True

        # 8) Сохраняем заказы в базу данных и удаляем товары из корзины
        order_ids = []
        with db.get_cursor() as cursor:
            for item in detailed:
                cursor.execute('''
                    INSERT INTO orders (
                        user_id, model_id, quantity,
                        status, additional_services,
                        total_price, our_tracking_number,
                        created_at, updated_at,
                        cn_delivery_paid
                    ) VALUES (?, ?, ?, 'ordered', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                ''', (
                    user_id,
                    item['model_id'],
                    item['quantity'],
                    json.dumps(services),
                    item['total_price'],  # Индивидуальная стоимость для каждого товара
                    our_track  # Одинаковый трек-номер для всех товаров заказа
                ))

                order_id = cursor.lastrowid
                order_ids.append(order_id)

                # Обновляем статус модели (как было в твоём коде)
                cursor.execute(
                    "UPDATE models SET status = 'Принято' WHERE id = ?",
                    (item['model_id'],)
                )

                # Удаляем / уменьшаем товар в корзине
                try:
                    remove_from_cart(cursor, user_id, item['model_id'], item['quantity'])
                except Exception:
                    # не критично — продолжаем, можно логировать
                    pass

        # Возвращаем первый order_id для совместимости
        first_order_id = order_ids[0] if order_ids else None
        return jsonify(success=True, order_id=first_order_id, tracking_number=our_track)

    except Exception as e:
        return jsonify(success=False, error=str(e)), 500
    
@app.route('/profile/orders')
@login_required
def profile_orders():
    user_id = session['user']['id']
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT 
                o.id            AS order_id,
                o.created_at    AS order_date,
                o.our_tracking_number,
                o.total_price,
                o.model_id,
                o.quantity,
                o.status,
                o.cn_delivery_price,
                o.cn_delivery_paid,
                o.photos,
                m.image_url,
                m.color_name    AS color,
                m.size_name     AS size,
                m.price         AS unit_price,
                p.title         AS product_title
            FROM orders o
            JOIN models m ON o.model_id = m.id
            JOIN products p ON m.product_id = p.id
            WHERE o.user_id = ?
            ORDER BY o.created_at DESC, o.id, o.model_id
        ''', (user_id,))
        rows = [dict(r) for r in cursor.fetchall()]

    orders = []
    current = None
    for r in rows:
        if current is None or current['id'] != r['order_id']:
            current = {
                'id': r['order_id'],
                'created_at': r['order_date'],
                'our_tracking_number': r['our_tracking_number'],
                'total_price': r['total_price'],
                'cn_delivery_price': r['cn_delivery_price'],
                'cn_delivery_paid': r['cn_delivery_paid'],
                'items': []
            }
            orders.append(current)
        
        # Парсим photos из JSON-строки
        photos = []
        if r['photos']:
            try:
                # Удаляем лишние кавычки и пробелы, пробуем распарсить JSON
                photos_str = r['photos'].strip('"\'')
                photos = json.loads(photos_str) if photos_str else []
            except (json.JSONDecodeError, TypeError, AttributeError):
                # Если не получается распарсить, создаём массив из строки
                photos = [r['photos']] if isinstance(r['photos'], str) else []
        
        current['items'].append({
            'product_title': r['product_title'],
            'image_url': r['image_url'],
            'color': r['color'],
            'size': r['size'],
            'price': r['unit_price'],
            'quantity': r['quantity'],
            'status': r['status'],
            'photos': [url.split(' ')[0] for url in photos]
        })

    return render_template('profile/orders.html', orders=orders)

@app.route('/profile/warehouse')
@login_required
def warehouse():
    user_id = session['user']['id']
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT 
                o.id            AS order_id,
                o.created_at    AS order_date,
                o.our_tracking_number,
                o.total_price,
                o.model_id,
                o.quantity,
                o.status,
                o.cn_delivery_price,
                o.cn_delivery_paid,
                o.photos,
                m.image_url,
                m.color_name    AS color,
                m.size_name     AS size,
                m.price         AS unit_price,
                p.title         AS product_title
            FROM orders o
            JOIN models m ON o.model_id = m.id
            JOIN products p ON m.product_id = p.id
            WHERE o.user_id = ?
            ORDER BY o.created_at DESC, o.id, o.model_id
        ''', (user_id,))
        rows = [dict(r) for r in cursor.fetchall()]

    orders = []
    current = None
    for r in rows:
        if current is None or current['id'] != r['order_id']:
            current = {
                'id': r['order_id'],
                'created_at': r['order_date'],
                'our_tracking_number': r['our_tracking_number'],
                'total_price': r['total_price'],
                'cn_delivery_price': r['cn_delivery_price'],
                'cn_delivery_paid': r['cn_delivery_paid'],
                'items': []
            }
            orders.append(current)
        
        # Парсим photos из JSON-строки
        photos = []
        if r['photos']:
            try:
                # Удаляем лишние кавычки и пробелы, пробуем распарсить JSON
                photos_str = r['photos'].strip('"\'')
                photos = json.loads(photos_str) if photos_str else []
            except (json.JSONDecodeError, TypeError, AttributeError):
                # Если не получается распарсить, создаём массив из строки
                photos = [r['photos']] if isinstance(r['photos'], str) else []
        
        current['items'].append({
            'product_title': r['product_title'],
            'image_url': r['image_url'],
            'color': r['color'],
            'size': r['size'],
            'price': r['unit_price'],
            'quantity': r['quantity'],
            'status': r['status'],
            'photos': [url.split(' ')[0] for url in photos]
        })

    return render_template('profile/my_warehouse.html', orders=orders)
    
@app.route('/api/orders/<int:order_id>/status', methods=['POST'])
@admin_required 
def update_status(order_id):
    data = request.get_json() or {}
    new_status = data.get('status')
    if not new_status:
        return jsonify({'success': False, 'error': 'Missing "status" field'}), 400

    result = db.update_order_status(order_id, new_status)
    if result is True:
        return jsonify({'success': True}), 200
    else:
        # если метод вернул dict с ошибкой
        if isinstance(result, dict):
            return jsonify(result), 500
        # на всякий случай — общая ошибка
        return jsonify({'success': False, 'error': 'Unknown error'}), 500
    
@app.route('/api/orders/<int:order_id>/cn_delivery_price', methods=['POST'])
@admin_required
def update_china_price(order_id):
    data = request.get_json() or {}
    price = data.get('cn_delivery_price')
    result = db.update_cn_delivery_price(order_id, price)
    if result is True:
        return jsonify({'success': True}), 200
    else:
        if isinstance(result, dict):
            return jsonify(result), 500
        return jsonify({'success': False, 'error': 'Unknown error'}), 500
    
@app.route('/api/orders/<int:order_id>/weight', methods=['POST'])
@admin_required
def update_order_weight(order_id):
    data = request.get_json()
    weight = data.get('weight')
    
    result = db.update_order_weight(order_id, weight)
    if result is True:
        return jsonify({'success': True}), 200
    else:
        if isinstance(result, dict):
            return jsonify(result), 500
        return jsonify({'success': False, 'error': 'Unknown error'}), 500

@app.route('/pay_delivery/<int:order_id>', methods=['POST'])
@login_required
def pay_delivery(order_id):
    user_id = session['user']['id']
    
    with db.get_cursor() as cursor:
        try:
            # 1. Получаем данные о заказе и балансе
            cursor.execute('''
                SELECT 
                    o.cn_delivery_price,
                    o.cn_delivery_paid,
                    u.balance_cny,
                    u.balance_rub
                FROM orders o
                JOIN users u ON o.user_id = u.id
                WHERE o.id = ? AND o.user_id = ?
            ''', (order_id, user_id))
            data = cursor.fetchone()
            
            if not data:
                return jsonify({'error': 'Заказ не найден'}), 404
            
            # 2. Извлекаем значения
            price = data['cn_delivery_price']
            is_paid = data['cn_delivery_paid']
            balance_cny = data['balance_cny']
            
            # 3. Проверки перед оплатой
            if is_paid:
                return jsonify({'error': 'Доставка уже оплачена'}), 400
                
            if not price or price <= 0:
                return jsonify({'error': 'Сумма доставки не указана'}), 400
                
            if balance_cny < price:
                return jsonify({
                    'error': f'Недостаточно средств. Нужно: {price} ¥, доступно: {balance_cny} ¥'
                }), 400
            
            # 4. Конвертация и списание
            price_rub = convert_cny_to_rub(price)
            
            # Обновляем балансы
            db.update_balance_cny(user_id, -price)
            db.update_balance_rub(user_id, -price_rub)
            
            # 5. Обновляем статус оплаты
            cursor.execute(
                "UPDATE orders SET cn_delivery_paid = 1 WHERE id = ?",
                (order_id,)
            )

            
            return jsonify({
                'success': 'Оплачено',
                'message': f'Доставка оплачена: {price} ¥',
                'new_balance_cny': balance_cny - price,
            })
            
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        
@app.route('/admin/orders/<int:order_id>/add_photo', methods=['POST'])
@admin_required
def add_photo(order_id):
    try:
        data = request.json
        photo_url = data.get('photo_url')
        
        if not photo_url:
            return jsonify(success=False, error='Не указана ссылка на фото'), 400
        
        with db.get_cursor() as cursor:
            # Получаем текущие фото
            cursor.execute("SELECT photos FROM orders WHERE id = ?", (order_id,))
            row = cursor.fetchone()
            current_photos = json.loads(row['photos']) if row and row['photos'] else []
            
            # Проверяем, нет ли уже такой ссылки
            if photo_url in current_photos:
                return jsonify(success=False, error='Фото уже добавлено'), 400
                
            # Добавляем новую ссылку
            current_photos.append(photo_url)
            
            # Обновляем запись
            cursor.execute(
                "UPDATE orders SET photos = ? WHERE id = ?",
                (json.dumps(current_photos), order_id)
            )
        
        return jsonify(success=True)
    
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500

@app.route('/admin/orders/<int:order_id>/remove_photo', methods=['POST'])
@admin_required
def remove_photo(order_id):
    try:
        data = request.json
        photo_url = data.get('photo_url')
        
        if not photo_url:
            return jsonify(success=False, error='Не указана ссылка на фото'), 400
        
        with db.get_cursor() as cursor:
            # Получаем текущие фото
            cursor.execute("SELECT photos FROM orders WHERE id = ?", (order_id,))
            row = cursor.fetchone()
            
            if not row or not row['photos']:
                return jsonify(success=False, error='Фото не найдены'), 404
                
            current_photos = json.loads(row['photos'])
            
            # Проверяем, есть ли такая ссылка
            if photo_url not in current_photos:
                return jsonify(success=False, error='Фото не найдено'), 404
                
            # Удаляем ссылку
            current_photos.remove(photo_url)
            
            # Обновляем запись
            cursor.execute(
                "UPDATE orders SET photos = ? WHERE id = ?",
                (json.dumps(current_photos), order_id)
            )
        
        return jsonify(success=True)
    
    except Exception as e:
        return jsonify(success=False, error=str(e)), 500
    
@app.route('/profile/warehouse_order', methods=['POST'])
@login_required
def warehouse_order():
    selected = request.form.getlist('selected_items')
    if not selected:
        flash('Выберите хотя бы один товар', 'warning')
        return redirect(url_for('warehouse'))  # или куда нужно

    user_id = session['user']['id']

    order_ids = list({int(value.split('_')[0]) for value in selected})
    print(order_ids)

    warehouse_items = db.get_orders_by_ids(user_id, order_ids)
    if not warehouse_items:
        flash('Выбранные товары не найдены', 'danger')
        return redirect(url_for('warehouse'))

    # # Общая сумма
    unit_prices = sum(item['unit_price'] * item['quantity'] for item in warehouse_items)    

    return render_template(
        'profile/my_warehouse_order.html',
        warehouse_items=warehouse_items,
)

def calc_delivery_cost_with_pct(
    P_usd_per_kg: float,
    weight_kg: float,
    K_usd_rub: float,
    K_rub_cny: float,
    m_pct: float = 0.03,
    fee_cny: float = 0.0,
    min_pct: float = 0.0,
    max_pct: float = 0.15,
    round_digits: int = 2
) -> dict:
    # Валидация
    if P_usd_per_kg < 0 or weight_kg < 0:
        raise ValueError("Цена или вес не могут быть отрицательными")
    if K_usd_rub <= 0 or K_rub_cny <= 0:
        raise ValueError("Курсы должны быть положительными")

    # 1) USD сумма
    usd_total = float(P_usd_per_kg) * float(weight_kg)

    # 2) кросс-курс: CNY за 1 USD
    K_cny_per_usd = float(K_usd_rub) / float(K_rub_cny)

    # 3) базовая сумма в CNY (по курсу)
    cny_base = usd_total * K_cny_per_usd

    # 4) применяем процентную наценку (ограничиваем min/max)
    used_pct = float(m_pct)
    if used_pct < min_pct:
        used_pct = float(min_pct)
    if used_pct > max_pct:
        used_pct = float(max_pct)

    # 5) итоговая сумма в CNY
    cny_charge = cny_base * (1.0 + used_pct) + float(fee_cny)

    # 6) эквивалент в рублях (по курсу RUB per CNY)
    rub_equivalent = cny_charge * float(K_rub_cny)

    # Округление значений для вывода/сохранения
    result = {
        "usd_total": round(usd_total, round_digits),
        "K_cny_per_usd": round(K_cny_per_usd, 8),
        "cny_base": round(cny_base, round_digits),
        "used_pct": used_pct,
        "fee_cny": round(float(fee_cny), round_digits),
        "cny_charge": round(cny_charge, round_digits),
        "rub_equivalent": round(rub_equivalent, round_digits),
        "notes": f"Applied m_pct={used_pct}, fee_cny={fee_cny}"
    }
    return result

@app.route('/process-shipment', methods=['POST'])
def process_shipment():
    import json
    try:
        data = request.get_json(silent=True)
        app.logger.debug("process-shipment: incoming data: %s", data)

        # Авторизация
        if 'user' not in session:
            return jsonify({'success': False, 'error': 'Требуется авторизация'}), 401
        user_id = session['user']['id']

        # Проверка полей
        required = ['items', 'delivery', 'packaging', 'fullname', 'phone', 'city', 'address']
        for f in required:
            if f not in data or data[f] in (None, '', [], {}):
                app.logger.debug("Missing field %s => %r", f, data.get(f))
                return jsonify({'success': False, 'error': f'Поле {f} обязательно для заполнения'}), 400

        # Проверка метода доставки
        delivery_key = str(data['delivery'])
        if delivery_key not in DELIVERY_RATES:
            return jsonify({'success': False, 'error': 'Указан несуществующий метод доставки'}), 400

        # --- Нормализация и валидация items как orders.id текущего пользователя ---
        raw_items = data['items']
        candidate_ids = []
        if isinstance(raw_items, (list, tuple)):
            for v in raw_items:
                try:
                    candidate_ids.append(int(v))
                except Exception:
                    app.logger.warning("Bad item id skipped: %r", v)
        else:
            try:
                candidate_ids = [int(raw_items)]
            except Exception:
                return jsonify({'success': False, 'error': 'Поле items должно быть списком идентификаторов'}), 400

        if not candidate_ids:
            return jsonify({'success': False, 'error': 'Нужно указать хотя бы один товар'}), 400

        # Оставляем только те id, которые реально принадлежат этому пользователю
        # и существуют в таблице orders
        with db.get_cursor() as cursor:
            placeholders = ','.join(['?'] * len(candidate_ids))
            rows = cursor.execute(
                f"SELECT id FROM orders WHERE user_id = ? AND id IN ({placeholders})",
                (user_id, *candidate_ids)
            ).fetchall()
        valid_ids_set = { (r['id'] if isinstance(r, dict) else r[0]) for r in rows }

        # Сохраняем исходный порядок и убираем дубли
        seen = set()
        order_ids = [oid for oid in candidate_ids if (oid in valid_ids_set and (oid not in seen and not seen.add(oid)))]

        if not order_ids:
            return jsonify({'success': False, 'error': 'Не найдено валидных заказов пользователя'}), 400

        # Вес по валидным orders.id
        total_weight = db.calculate_total_weight(order_ids)
        app.logger.debug("calculate_total_weight -> %r", total_weight)
        if total_weight is None:
            return jsonify({'success': False, 'error': 'Не удалось вычислить вес'}), 400

        # Курсы ЦБ
        rates = fetch_cbr_rates()
        if not rates:
            app.logger.error("fetch_cbr_rates failed -> %r", rates)
            return jsonify({'success': False, 'error': 'Не удалось получить курсы валют (ЦБ)'}), 500

        # Извлекаем курс
        try:
            usd_to_rub = float(rates.get('USD'))
            cny_to_rub = float(rates.get('CNY'))
        except Exception:
            try:
                usd_to_rub = float(rates['Valute']['USD']['Value'])
                cny_to_rub = float(rates['Valute']['CNY']['Value'])
            except Exception as e:
                app.logger.exception("Cannot parse rates structure: %s", e)
                return jsonify({'success': False, 'error': 'Неподдерживаемый формат курсов ЦБ'}), 500

        if usd_to_rub <= 0 or cny_to_rub <= 0:
            app.logger.error("Invalid rates: usd_to_rub=%r, cny_to_rub=%r", usd_to_rub, cny_to_rub)
            return jsonify({'success': False, 'error': 'Некорректные курсы валют'}), 500

        # Тариф USD/кг
        P_usd_per_kg = float(DELIVERY_RATES[delivery_key])

        # Опциональные параметры
        m_pct        = float(data.get('m_pct', 0.03))
        fee_cny      = float(data.get('fee_cny', 0.0))
        min_pct      = float(data.get('min_pct', 0.0))
        max_pct      = float(data.get('max_pct', 0.15))
        round_digits = int(data.get('round_digits', 2))

        # Расчёт
        calc = calc_delivery_cost_with_pct(
            P_usd_per_kg=P_usd_per_kg,
            weight_kg=float(total_weight),
            K_usd_rub=float(usd_to_rub),
            K_rub_cny=float(cny_to_rub),
            m_pct=m_pct,
            fee_cny=fee_cny,
            min_pct=min_pct,
            max_pct=max_pct,
            round_digits=round_digits
        )

        usd_total        = calc["usd_total"]
        K_cny_per_usd    = calc["K_cny_per_usd"]
        total_cost_cny   = calc["cny_charge"]
        total_cost_rub   = calc["rub_equivalent"]
        used_pct         = calc["used_pct"]
        fee_cny_rounded  = calc["fee_cny"]

        app.logger.debug(
            "Cost via formula: P=%s usd/kg, weight=%s kg, usd_total=%s, K_cny_per_usd=%s, "
            "m_pct=%s (used=%s), fee_cny=%s, total_cost_cny=%s, total_cost_rub=%s",
            P_usd_per_kg, total_weight, usd_total, K_cny_per_usd,
            m_pct, used_pct, fee_cny_rounded, total_cost_cny, total_cost_rub
        )

        # Проверка баланса CNY
        balance = db.get_balance(user_id)
        balance_cny = float(balance.get('cny', 0)) if balance else 0.0
        if float(total_cost_cny) > balance_cny:
            return jsonify({'success': False, 'error': 'Недостаточно средств на балансе CNY'}), 400

        # Списание CNY (и опционально зеркальный RUB)
        db.update_balance_cny(user_id, -float(total_cost_cny))
        try:
            db.update_balance_rub(user_id, -float(total_cost_rub))
        except Exception:
            app.logger.debug("update_balance_rub failed or not needed; continuing")

        # Генерация уникального трека
        with db.get_cursor() as cursor:
            while True:
                rand_num = random.randint(100000000, 9999999999)
                our_track = f'RUBOX{rand_num}'
                cursor.execute("SELECT COUNT(*) AS cnt FROM order_shipments WHERE our_tracking_number = ?", (our_track,))
                row = cursor.fetchone()
                cnt = row['cnt'] if isinstance(row, dict) and 'cnt' in row else (row[0] if row else 0)
                if cnt == 0:
                    break

        # Нормализуем packaging_options: всегда список строк
        packaging_raw = data['packaging']
        if isinstance(packaging_raw, (list, tuple)):
            packaging_options = [str(x) for x in packaging_raw]
        else:
            packaging_options = [str(packaging_raw)]

        # Сохранение отправки (стоимость доставки — в CNY).
        # model_ids сохраняем как JSON-строку строго по orders.id текущего пользователя.
        db.add_shipment(
            user_id=user_id,
            model_ids=json.dumps(order_ids, ensure_ascii=False),
            delivery_method=delivery_key,
            packaging_options=packaging_options,   # <-- ВАЖНО: правильное имя аргумента
            recipient_name=str(data['fullname']),
            recipient_phone=str(data['phone']),
            recipient_city=str(data['city']),
            recipient_address=str(data['address']),
            total_weight=float(total_weight),
            delivery_cost=float(total_cost_cny),
            packaging_cost=0.0,
            total_cost=float(total_cost_cny),
            our_tracking_number=our_track
            # статус не передаем — пусть отработает DEFAULT 'pending' вашей таблицы
        )

        # Обновление статусов заказов
        try:
            with db.get_cursor() as cursor:
                for oid in order_ids:
                    try:
                        cursor.execute(
                            "UPDATE orders SET status = ? WHERE id = ? AND user_id = ?",
                            ('in_shipment', oid, user_id)
                        )
                    except Exception:
                        app.logger.exception("Failed updating order %s", oid)
        except Exception:
            app.logger.exception("Failed updating orders (non-fatal)")

        # Ответ клиенту
        return jsonify({
            'success': True,
            'tracking': our_track,
            'delivery_usd': usd_total,
            'delivery_cny_charged': total_cost_cny,
            'delivery_rub_equivalent': total_cost_rub,
            'K_usd_rub': usd_to_rub,
            'K_rub_cny': cny_to_rub,
            'K_cny_per_usd': K_cny_per_usd,
            'used_pct': used_pct,
            'fee_cny': fee_cny_rounded,
        }), 200

    except sqlite3.Error as e:
        app.logger.exception("SQLite error in /process-shipment: %s", e)
        return jsonify({'success': False, 'error': 'Ошибка базы данных'}), 500
    except Exception as e:
        app.logger.exception("Unexpected error in /process-shipment: %s", e)
        return jsonify({'success': False, 'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/pay-packaging', methods=['POST'])
def pay_packaging():
    if 'user' not in session:
        return jsonify({'success': False, 'error': 'Требуется авторизация'}), 401

    user_id = int(session['user']['id'])
    data = request.get_json(silent=True) or {}
    shipment_id = data.get('shipment_id')

    try:
        shipment_id = int(shipment_id)
    except Exception:
        return jsonify({'success': False, 'error': 'Некорректный shipment_id'}), 400

    try:
        with db.get_cursor() as cursor:
            # Начинаем транзакцию вручную, чтобы списание и UPDATE были атомарными
            cursor.execute("BEGIN")

            row = cursor.execute("""
                SELECT id, user_id, packaging_cost, packaging_paid
                FROM order_shipments
                WHERE id = ?
            """, (shipment_id,)).fetchone()

            if not row:
                cursor.execute("ROLLBACK")
                return jsonify({'success': False, 'error': 'Посылка не найдена'}), 404

            # dict/tuple safe getters
            def g(r, key, idx):
                if isinstance(r, dict):
                    return r.get(key)
                return r[idx]

            owner_id       = int(g(row, 'user_id', 1))
            packaging_cost = g(row, 'packaging_cost', 2)
            packaging_paid = g(row, 'packaging_paid', 3)

            # приводим типы
            try:
                packaging_cost = float(packaging_cost or 0.0)
            except Exception:
                packaging_cost = 0.0
            try:
                packaging_paid = int(packaging_paid or 0)
            except Exception:
                packaging_paid = 0

            if owner_id != user_id:
                cursor.execute("ROLLBACK")
                return jsonify({'success': False, 'error': 'Нет доступа'}), 403
            if packaging_cost <= 0:
                cursor.execute("ROLLBACK")
                return jsonify({'success': False, 'error': 'Упаковка не выставлена'}), 400
            if packaging_paid == 1:
                cursor.execute("ROLLBACK")
                return jsonify({'success': False, 'error': 'Упаковка уже оплачена'}), 400

            # проверяем баланс CNY
            balance = db.get_balance(user_id) or {}
            try:
                balance_cny = float(balance.get('cny', 0))
            except Exception:
                balance_cny = 0.0

            if packaging_cost > balance_cny:
                cursor.execute("ROLLBACK")
                return jsonify({'success': False, 'error': 'Недостаточно средств на балансе CNY'}), 400

            # списываем и помечаем оплачено
            db.update_balance_cny(user_id, -packaging_cost)
            cursor.execute("""
                UPDATE order_shipments
                SET packaging_paid = 1
                WHERE id = ?
            """, (shipment_id,))

            cursor.execute("COMMIT")

        return jsonify({'success': True})
    except Exception as e:
        app.logger.exception("pay_packaging failed: %s", e)
        # в случае сбоя откатываем, чтобы не осталось «полусостояния»
        try:
            with db.get_cursor() as cursor:
                cursor.execute("ROLLBACK")
        except Exception:
            pass
        return jsonify({'success': False, 'error': 'DB error'}), 500
    
@app.route('/admin/shipments/<int:shipment_id>/status', methods=['POST'])
@admin_required
def admin_set_shipment_status(shipment_id):
    try:
        data = request.get_json(silent=True) or {}
        new_status = str(data.get('status', '')).strip()

        if new_status not in SHIPMENT_STATUSES:
            return jsonify({'success': False, 'error': 'Недопустимый статус'}), 400

        with db.get_cursor() as cursor:
            row = cursor.execute(
                "SELECT id, user_id, status FROM order_shipments WHERE id = ?",
                (shipment_id,)
            ).fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Посылка не найдена'}), 404

            # Проверим, есть ли колонка updated_at
            cols = cursor.execute("PRAGMA table_info(order_shipments)").fetchall()

            def _colname(c):
                # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
                return c['name'] if isinstance(c, dict) else c[1]

            colnames = { _colname(c) for c in cols }

            if 'updated_at' in colnames:
                cursor.execute(
                    "UPDATE order_shipments SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_status, shipment_id)
                )
            else:
                # Без updated_at — просто меняем статус
                cursor.execute(
                    "UPDATE order_shipments SET status = ? WHERE id = ?",
                    (new_status, shipment_id)
                )

        return jsonify({
            'success': True,
            'shipment_id': shipment_id,
            'status': new_status,
            'status_label': SHIPMENT_STATUSES[new_status]
        })
    except Exception as e:
        app.logger.exception("admin_set_shipment_status failed: %s", e)
        # вернём расшифровку для дебага (можно оставить общую ошибку в проде)
        return jsonify({'success': False, 'error': f'DB error: {type(e).__name__}: {e}'}), 500

@app.route('/profile/shipments')
def shipments():
    if 'user' not in session:
        return redirect(url_for('login'))  # опционально
    user_id = session['user']['id']

    shipments_list = db.get_shipments_with_photos(user_id) or []

    # Нормализуем отсутствующие поля и приводим типы
    for s in shipments_list:
        # если это объект, превращать не нужно; если dict — безопасно
        if isinstance(s, dict):
            s.setdefault('packaging_cost', 0.0)
            s.setdefault('packaging_paid', 0)
            try:
                s['packaging_cost'] = float(s.get('packaging_cost') or 0.0)
            except Exception:
                s['packaging_cost'] = 0.0
            try:
                s['packaging_paid'] = int(s.get('packaging_paid') or 0)
            except Exception:
                s['packaging_paid'] = 0

    return render_template('profile/shipments.html', shipments=shipments_list)
    
if __name__ == '__main__':
    with app.app_context():
        db.init_db()
        if not db.get_user(name=ADMIN_USERNAME):
            db.create_admin(ADMIN_USERNAME, ADMIN_PASSWORD)
            
    start_cleanup_loop(db)

    app.run(host='0.0.0.0', debug=True)






