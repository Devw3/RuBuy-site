import os
from functools import wraps
import sqlite3
import time
import requests
from base import Database
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from parser.taobao import parse_taobao_product
from parser.weidian import parse_weidian_product

UPLOAD_FOLDER = os.path.join('static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
API_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJVc2VybmFtZSI6Im1jcWVlbjk1OTUiLCJDb21pZCI6bnVsbCwiUm9sZWlkIjpudWxsLCJpc3MiOiJ0bWFwaSIsInN1YiI6Im1jcWVlbjk1OTUiLCJhdWQiOlsiIl0sImlhdCI6MTc1MTY0MzIxNn0.EAeSwRbi7N4pvC71EnJCfEroQicKwz4J4ZvjWFTSUXs"

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
    if not session.get('user') or 'id' not in session['user']:
        return redirect(url_for('login'))  # Перенаправляем на страницу входа
    
    return render_template('index.html', user=session['user'])

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
    balance_cny = convert_rub_to_cny(balance_rub)

    
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

    
    return render_template(
        'admin/admin_panel.html',
        user=user,
        replenishments=pending_replenishments,
        withdrawals=pending_withdrawals
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
    
    # Получаем текущие балансы
    balance_rub = db.get_balance(user_id)['rub']
    balance_cny = convert_rub_to_cny(balance_rub)
    
    # Получаем историю транзакций
    transactions = db.get_balance_history(user_id)
    
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
    balance_cny = convert_rub_to_cny(balance_rub)

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
            current_balance_cny = convert_rub_to_cny(balance_rub)
            withdrawals = db.get_user_withdrawals(user_id)
            
            return render_template('profile/withdraw.html',
                                current_balance=current_balance_cny,
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
            
            # Обновляем баланс пользователя
            db.update_balance(user_id, -amount)
            
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
            if user_balance < amount:
                return jsonify({'error': 'Недостаточно средств на балансе'}), 400
            
            # Списание средств
            db.update_balance(user_id, -amount)
        
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


# @app.route('/add_product', methods=['POST'])
# def add_product():
#     url = request.form['product_url'].strip()
    
#     try:
#         if 'taobao.com' in url or 'tmall.com' in url:
#             product = parse_taobao_product(url)
#         elif 'weidian.com' in url:
#             product = parse_weidian_product(url)
#         else:
#             return render_template('error.html', message="Неподдерживаемый сайт")
        
#         # Добавляем товар через метод класса (он вернет product_id)
#         product_id = db.add_product(product)
        
#         return redirect(url_for('product_page', product_id=product_id))
    
#     except Exception as e:
#         return render_template('error.html', message=f"Ошибка: {str(e)}")


# @app.route('/product/<int:product_id>')
# def product_page(product_id):
#     try:
#         # Получаем данные через метод класса
#         product, models = db.get_product_with_models(product_id)
        
#         # Подготовка данных для шаблона (та же логика)
#         colors = {}
#         for model in models:
#             color = model['color_name']
#             if color not in colors:
#                 colors[color] = {
#                     'image_url': model['image_url'],
#                     'sizes': []
#                 }
#             colors[color]['sizes'].append({
#                 'size': model['size_name'],
#                 'price': model['price'],
#                 'stock': model['stock'],
#                 'model_id': model['id']
#             })
        
#         return render_template('product.html', 
#                              product=product,
#                              colors=colors)
    
#     except ValueError as e:
#         return render_template('error.html', message=str(e))
#     except Exception as e:
#         return render_template('error.html', message=f"Ошибка: {str(e)}")

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
        
        # Добавляем товар через метод класса (он вернет product_id)
        product_id = db.add_product(product)
        
        return redirect(url_for('product_page', product_id=product_id))
    
    except Exception as e:
        return render_template('error.html', message=f"Ошибка: {str(e)}")


@app.route('/product/<int:product_id>')
def product_page(product_id):
    try:
        # Используем новую версию функции
        product_data = db.get_product_with_models(product_id)
        
        return render_template('product.html', 
                             product=product_data['product'],
                             variants=product_data['variants'],
                             models=product_data['models'])
    
    except ValueError as e:
        return render_template('error.html', message=str(e))
    except Exception as e:
        return render_template('error.html', message=f"Ошибка: {str(e)}")


@app.route('/add-to-cart', methods=['POST'])
def add_to_cart():
    try:
        data = request.get_json()
        model_id = data['model_id']
        
        # Проверка наличия модели
        with db.get_cursor() as cursor:
            cursor.execute('SELECT * FROM models WHERE id = ?', (model_id,))
            model = cursor.fetchone()
            
            if not model:
                return jsonify(success=False, error='Модель не найдена'), 404
            
            if model['stock'] <= 0:
                return jsonify(success=False, error='Нет в наличии'), 400
            
            # Здесь должна быть логика добавления в корзину
            # Например, обновление сессии или таблицы корзины
            
            # Уменьшаем количество на складе
            cursor.execute('UPDATE models SET stock = stock - 1 WHERE id = ?', (model_id,))
            
            return jsonify(success=True, message='Товар добавлен в корзину')
    
    except Exception as e:
        return jsonify(success=False, error=f'Ошибка: {str(e)}'), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=True)