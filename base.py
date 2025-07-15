import sqlite3
import requests
import os
from werkzeug.security import generate_password_hash, check_password_hash
from flask import current_app, g
from contextlib import contextmanager


def get_cny_to_rub_rate():
    try:
        url = "https://www.cbr-xml-daily.ru/daily_json.js"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        cny_rate = data["Valute"]["CNY"]["Value"]
        return cny_rate
    except Exception as e:
        return None
    
def convert_rub_to_cny(rub_amount):
    cny_to_rub_rate = get_cny_to_rub_rate()
    if cny_to_rub_rate is None:
        return None
    
    rub_to_cny_rate = 1 / cny_to_rub_rate
    
    cny_amount = rub_amount * rub_to_cny_rate
    return round(cny_amount, 2)

class Database:
    def __init__(self, app=None):
        self.app = app
        self._is_initialized = False  # Явно инициализируем атрибут
        if app is not None:
            self.init_app(app)
    
    def init_app(self, app):
        """Инициализация базы данных в приложении Flask"""
        app.config.setdefault('DATABASE', os.path.join(app.instance_path, 'users.db'))
        os.makedirs(app.instance_path, exist_ok=True)
        app.teardown_appcontext(self.close_connection)
        
        # Устанавливаем флаг инициализации в extensions приложения
        if not hasattr(app, 'extensions'):
            app.extensions = {}
        app.extensions['db_initialized'] = False
        
        @app.before_request
        def _initialize():
            if not app.extensions['db_initialized']:
                with app.app_context():
                    self.init_db()
                    if not self.get_user(name='admin1'):
                        self.create_admin('admin1', '123456')
                app.extensions['db_initialized'] = True
                self._is_initialized = True

    
    def get_connection(self):
        """Получает или создает соединение с БД для текущего контекста"""
        if not hasattr(g, 'db_connection'):
            # Создаем новое соединение
            g.db_connection = sqlite3.connect(
                current_app.config['DATABASE'],
                detect_types=sqlite3.PARSE_DECLTYPES
            )
            g.db_connection.row_factory = sqlite3.Row
            g.db_connection.execute("PRAGMA foreign_keys = ON")
        return g.db_connection
    
    def close_connection(self, exception=None):
        """Закрывает соединение с БД"""
        connection = getattr(g, 'db_connection', None)
        if connection is not None:
            connection.close()
    
    @contextmanager
    def get_cursor(self):
        """Контекстный менеджер для безопасной работы с курсором"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            current_app.logger.error(f"Database error: {str(e)}")
            raise
        finally:
            cursor.close()
    
    def init_db(self):
        """Инициализирует структуру базы данных"""
        with self.get_cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    region TEXT NOT NULL,
                    photo TEXT DEFAULT 'static/default.png',
                    is_admin BOOLEAN DEFAULT FALSE,
                    balance_rub DECIMAL(10, 2) DEFAULT 0.0,
                    balance_cny DECIMAL(10, 2) DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS replenishments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_rub DECIMAL(10, 2) NOT NULL,
                    amount_cny DECIMAL(10, 2) NOT NULL,
                    payment_date TEXT NOT NULL,
                    receipt_path TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    admin_id INTEGER,
                    admin_comment TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id),
                    FOREIGN KEY (admin_id) REFERENCES users (id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount DECIMAL(10, 2) NOT NULL,
                    card_number TEXT NOT NULL,
                    card_holder TEXT NOT NULL,
                    name TEXT,
                    status TEXT DEFAULT 'pending',
                    admin_comment TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')

            # Таблица товаров
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    base_price REAL
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER,
                    color_name TEXT,
                    size_name TEXT,
                    price REAL,
                    stock INTEGER,
                    image_url TEXT,
                    FOREIGN KEY (product_id) REFERENCES products(id)
                )
            ''')
    
    # ============== User Methods ==============
    
    def create_user(self, name, password, region, photo_path='static/default.png', is_admin=False):
        hashed_pw = generate_password_hash(password)
        with self.get_cursor() as cursor:
            try:
                cursor.execute(
                    '''INSERT INTO users 
                    (name, password, region, photo, is_admin, balance_rub, balance_cny) 
                    VALUES (?, ?, ?, ?, ?, 0.0, 0.0)''',
                    (name, hashed_pw, region, photo_path, int(is_admin))
                )
                return cursor.lastrowid
            except sqlite3.IntegrityError as e:
                current_app.logger.error(f"Error creating user: {str(e)}")
                return None
    
    def get_user(self, user_id=None, name=None):
        """Получает пользователя по ID или имени"""
        if not user_id and not name:
            return None
            
        with self.get_cursor() as cursor:
            if user_id:
                cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
            else:
                cursor.execute('SELECT * FROM users WHERE name = ?', (name,))
                
            user = cursor.fetchone()
            return dict(user) if user else None
        
    def get_user_balance(self, user_id):
        with self.get_cursor() as cursor:
            cursor.execute("SELECT balance_rub FROM users WHERE id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else 0
    
    def authenticate_user(self, name, password):
        """Аутентифицирует пользователя"""
        user = self.get_user(name=name)
        if user and check_password_hash(user['password'], password):
            return user
        return None
    
    def update_user(self, user_id, **kwargs):
        """Обновляет данные пользователя"""
        allowed_fields = {'name', 'region', 'photo', 'is_admin'}
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        
        if not updates:
            return False
            
        set_clause = ', '.join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(user_id)
        
        with self.get_cursor() as cursor:
            cursor.execute(
                f'UPDATE users SET {set_clause} WHERE id = ?',
                values
            )
            return cursor.rowcount > 0
    
    def delete_user(self, user_id):
        """Удаляет пользователя"""
        with self.get_cursor() as cursor:
            cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
            return cursor.rowcount > 0
    
    def change_password(self, user_id, new_password):
        """Изменяет пароль пользователя"""
        hashed_pw = generate_password_hash(new_password)
        with self.get_cursor() as cursor:
            cursor.execute(
                'UPDATE users SET password = ? WHERE id = ?',
                (hashed_pw, user_id)
            )
            return cursor.rowcount > 0
    
    def list_users(self, is_admin=None):
        """Список пользователей с фильтрацией по роли"""
        query = 'SELECT * FROM users'
        params = []
        
        if is_admin is not None:
            query += ' WHERE is_admin = ?'
            params.append(int(is_admin))
            
        query += ' ORDER BY created_at DESC'
        
        with self.get_cursor() as cursor:
            cursor.execute(query, tuple(params))
            return [dict(row) for row in cursor.fetchall()]
        
    def get_balance(self, user_id):
        """Возвращает балансы пользователя в рублях и юанях"""
        with self.get_cursor() as cursor:
            cursor.execute(
                'SELECT balance_rub, balance_cny FROM users WHERE id = ?', 
                (user_id,)
            )
            result = cursor.fetchone()
            return {
                'rub': float(result['balance_rub']) if result else 0.0,
                'cny': float(result['balance_cny']) if result else 0.0
            }

    def update_balance(self, user_id, amount):
        with self.get_cursor() as cursor:
            try:
                cursor.execute(
                    'UPDATE users SET balance = balance + ? WHERE id = ?',
                    (float(amount), user_id))
                return True
            except sqlite3.Error as e:
                current_app.logger.error(f"Balance update failed: {str(e)}")
                return False
    
    # ============== Admin Methods ==============
    
    def create_admin(self, username, password):
        """Создает администратора с проверкой результата"""
        with self.get_cursor() as cursor:
            try:
                cursor.execute('DELETE FROM users WHERE name = ?', (username,))
                hashed_pw = generate_password_hash(password)
                cursor.execute(
                    '''INSERT INTO users 
                    (name, password, region, photo, is_admin) 
                    VALUES (?, ?, ?, ?, ?)''',
                    (username, hashed_pw, 'admin', 'static/default.png', 1)
                )
                cursor.execute('SELECT id FROM users WHERE name = ?', (username,))
                admin = cursor.fetchone()
                
                if admin:
                    return admin['id']
                return None
                    
            except sqlite3.Error as e:
                print(f"Ошибка при создании администратора: {e}")
                return None
            
    # заявки на пополнение
    

    def create_replenishment(self, user_id, amount_rub, payment_date, receipt_path):
        with self.get_cursor() as cursor:
            try:
                cursor.execute('''
                    INSERT INTO replenishments (
                        user_id, 
                        amount_rub, 
                        amount_cny, 
                        payment_date, 
                        receipt_path
                    ) VALUES (?, ?, 0, ?, ?)
                ''', (user_id, amount_rub, payment_date, receipt_path))
                return cursor.lastrowid
            except Exception as e:
                print(f"Error creating replenishment: {str(e)}")
                raise        
    def get_pending_replenishments(self):
        """Получает все ожидающие заявки на пополнение"""
        with self.get_cursor() as cursor:
            cursor.execute('''
                SELECT 
                    r.id,
                    r.user_id,
                    r.amount_rub,
                    r.amount_cny,
                    r.payment_date,
                    r.receipt_path,
                    r.status,
                    r.created_at,
                    u.name as user_name 
                FROM replenishments r
                JOIN users u ON r.user_id = u.id
                WHERE r.status = 'pending'
                ORDER BY r.created_at DESC
            ''')
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
            
    def process_replenishment(self, replenishment_id, action, admin_id, comment=None):
        if action not in ('approve', 'reject'):
            raise ValueError("Invalid action")
        
        with self.get_cursor() as cursor:
            cursor.execute("BEGIN TRANSACTION")
            
            # Получаем данные заявки
            cursor.execute('''
                SELECT user_id, amount_rub, amount_cny 
                FROM replenishments 
                WHERE id = ? AND status = 'pending'
            ''', (replenishment_id,))
            
            replenishment = cursor.fetchone()
            if not replenishment:
                return False
            
            user_id, amount_rub, amount_cny = replenishment
            
            # Одобрение: пополняем рубли и юани
            if action == 'approve':
                cursor.execute('''
                    UPDATE users 
                    SET balance_rub = balance_rub + ?,
                        balance_cny = balance_cny + ?
                    WHERE id = ?
                ''', (amount_rub, amount_cny, user_id))
            
            # Обновляем статус заявки
            new_status = 'approved' if action == 'approve' else 'rejected'
            cursor.execute('''
                UPDATE replenishments 
                SET status = ?,
                    admin_id = ?,
                    admin_comment = ?,
                    processed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (new_status, admin_id, comment, replenishment_id))
            
            cursor.connection.commit()
            return True
    # def debug_show_replenishments_table(self):
    #     """Выводит всю таблицу replenishments для отладки"""
    #     with self.get_cursor() as cursor:
    #         cursor.execute('''
    #             SELECT 
    #                 id,
    #                 user_id,
    #                 amount,
    #                 status,
    #                 payment_date,
    #                 receipt_path,
    #                 admin_comment,
    #                 created_at
    #             FROM replenishments
    #             ORDER BY created_at DESC
    #         ''')
            
    #         table_data = cursor.fetchall()
    #         print("\nDEBUG: Содержимое таблицы replenishments:")
    #         print("-" * 100)
    #         print(f"| {'ID':<4} | {'User ID':<7} | {'Amount':<8} | {'Status':<10} | {'Payment Date':<12} | {'Receipt Path':<20} | {'Comment':<20} |")
    #         print("-" * 100)
            
    #         for row in table_data:
    #             print(f"| {row['id']:<4} | {row['user_id']:<7} | {row['amount']:<8} | {row['status']:<10} | {row['payment_date']:<12} | {row['receipt_path'][:20]:<20} | {str(row['admin_comment'])[:20]:<20} |")
            
    #         print("-" * 100)
    #         return table_data



    def get_balance_history(self, user_id):
        """Получает историю операций с отображением изменения баланса"""
        with self.get_cursor() as cursor:
            # Получаем все операции
            cursor.execute('''
                SELECT 
                    amount_rub,
                    amount_cny,
                    created_at as date,
                    status,
                    'replenishment' as operation_type
                FROM replenishments
                WHERE user_id = ? 
                
                UNION ALL
                
                SELECT 
                    amount as amount_rub,
                    NULL as amount_cny,
                    created_at as date,
                    status,
                    'withdrawal' as operation_type
                FROM withdrawals
                WHERE user_id = ? 
                    
                ORDER BY date DESC
            ''', (user_id, user_id))
            
            operations = cursor.fetchall()
            current_balance_rub = self.get_balance(user_id)['rub']
            history = []
            
            for op in operations:
                # Для пополнения: RUB увеличивается
                if op['operation_type'] == 'replenishment':
                    change_rub = op['amount_rub']
                    transaction_type = 'Пополнение'
                    sign = '+'  # Положительное изменение
                else:  # withdrawal
                    change_rub = -op['amount_rub']  # Отрицательное изменение
                    transaction_type = 'Вывод'
                    sign = '-'  # Отрицательное изменение
                
                # Для утвержденных операций показываем баланс после операции
                if op['status'] == 'approved':
                    balance_after = current_balance_rub
                    # Обновляем текущий баланс для следующей операции
                    current_balance_rub -= change_rub
                else:
                    balance_after = None  # Для ожидающих операций баланс не меняется
                
                history.append({
                    'type': transaction_type,
                    'amount_rub': f"{sign}{op['amount_rub']}",  # С правильным знаком
                    'date': op['date'],
                    'balance_after': balance_after,
                    'status': op['status'],
                    'change_rub': change_rub  # Для сортировки
                })
            
            return history
    
        # Вывод
    def create_withdrawal(self, user_id: int, amount: float, card_number: str, 
                        card_holder: str, name: str) -> bool:
        with self.get_cursor() as cursor:
            try:
                cursor.execute("""
                    UPDATE users 
                    SET balance_rub = balance_rub - ? 
                    WHERE id = ?
                """, (amount, user_id))

                cursor.execute('''
                    INSERT INTO withdrawals (
                        user_id, amount, 
                        card_number, card_holder, name,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', datetime('now'))
                ''', (  # Явно указываем статус 'pending'
                    user_id, 
                    amount,
                    card_number, 
                    card_holder,
                    name
                ))
                return cursor.lastrowid is not None
            except Exception as e:
                print(f"Ошибка при создании вывода: {str(e)}")
                return False
            
    def get_user_withdrawals(self, user_id: int) -> list:
        with self.get_cursor() as cursor:
            try:
                query = '''
                    SELECT 
                        id, 
                        amount,
                        substr(card_number, -4) as card_last_four,
                        card_holder,
                        status,
                        created_at
                    FROM withdrawals
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                '''
                cursor.execute(query, (user_id,))
                return [dict(row) for row in cursor.fetchall()]
            except Exception as e:
                print(f"Ошибка при получении выводов пользователя: {str(e)}")
                return []
            
    def get_withdrawal_by_id(self, withdrawal_id):
        with self.get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM withdrawals WHERE id = ?
            ''', (withdrawal_id,))
            result = cursor.fetchone()
            return dict(result) if result else None

    def get_pending_withdrawals(self) -> list:
        with self.get_cursor() as cursor:
            try:
                # Получаем только ожидающие выводы
                cursor.execute('''
                    SELECT 
                        w.id,
                        w.amount,
                        u.name as user_name,
                        w.card_number,
                        COALESCE(w.card_holder, '') as card_holder,
                        w.created_at,
                        w.status
                    FROM withdrawals w
                    JOIN users u ON w.user_id = u.id
                    WHERE w.status = 'pending'
                    ORDER BY w.created_at DESC
                ''')
                
                # Форматируем результаты
                withdrawals = []
                for row in cursor.fetchall():
                    w = dict(row)
                    w['amount'] = float(w['amount'])
                    withdrawals.append(w)
                    
                return withdrawals
                
            except Exception as e:
                print(f"Ошибка при получении ожидающих выводов: {str(e)}")
                return []          
            
    def update_withdrawal_status(self, withdrawal_id, status, comment):
        with self.get_cursor() as cursor:
            try:
                # 1. Получаем информацию о выводе
                cursor.execute('''
                    SELECT user_id, amount FROM withdrawals 
                    WHERE id = ? AND status = 'pending'
                ''', (withdrawal_id,))
                withdrawal = cursor.fetchone()
                
                if not withdrawal:
                    raise ValueError("Заявка не найдена или уже обработана")
                    
                user_id, amount = withdrawal

                # 2. Обновляем статус вывода
                cursor.execute('''
                    UPDATE withdrawals 
                    SET 
                        status = ?,
                        admin_comment = ?,
                        processed_at = datetime('now')
                    WHERE id = ?
                ''', (status, comment, withdrawal_id))
                
                return True
                
            except Exception as e:
                print(f"Error updating withdrawal status: {str(e)}")
                raise

    # работа с товарами
    def add_product(self, product_data):
        with self.get_cursor() as cursor:
            try:
                cursor.execute('''
                    SELECT id FROM products 
                    WHERE title = ? AND base_price = ?
                    LIMIT 1
                ''', (product_data['title'], product_data['base_price']))
                existing_product = cursor.fetchone()
                
                if existing_product:
                    return existing_product['id']

                # 1. Добавляем основной товар
                cursor.execute('''
                    INSERT INTO products (title, base_price)
                    VALUES (?, ?)
                ''', (product_data['title'], product_data['base_price']))
                
                product_id = cursor.lastrowid
                base_price = product_data.get('base_price', 0.0)

                
                # 2. Добавляем все модели/вариации товара
                for model in product_data['models']:
                    cursor.execute('''
                        INSERT INTO models (
                            product_id, 
                            color_name, 
                            size_name, 
                            price, 
                            stock, 
                            image_url
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        product_id,
                        model.get('color_name', ''),
                        model.get('size_name', ''),
                        model.get('price', base_price),
                        model.get('stock', 0),
                        model.get('image_url', '')
                    ))
                
                return product_id
                
            except Exception as e:
                print(f"Ошибка при добавлении товара: {str(e)}")
                raise

    def get_product_with_models(self, product_id):
        with self.get_cursor() as cursor:
            # Получаем основной товар
            cursor.execute('SELECT * FROM products WHERE id = ?', (product_id,))
            product = cursor.fetchone()
            
            if not product:
                raise ValueError("Товар не найден")
            
            # Получаем все модели товара
            cursor.execute('''
                SELECT * FROM models 
                WHERE product_id = ?
                ORDER BY color_name, size_name
            ''', (product_id,))
            models = cursor.fetchall()
            
            # Конвертируем Row объекты в словари
            product_dict = dict(product)
            models_list = [dict(model) for model in models]
            
            # Дополнительная обработка данных для удобства использования в шаблоне
            variants = {
                'colors': {},
                'sizes': {},
                'images': [],  # Изменили set() на list()
                'min_price': float('inf'),
                'max_price': 0
            }
            
            for model in models_list:
                color = model['color_name']
                size = model['size_name']
                
                # Собираем цвета
                if color not in variants['colors']:
                    variants['colors'][color] = []
                variants['colors'][color].append(model)
                
                # Собираем размеры
                variants['sizes'][size] = {
                    'price': model['price'],
                    'stock': model['stock']
                }
                
                # Собираем изображения (уникальные)
                if model['image_url'] and model['image_url'] not in variants['images']:
                    variants['images'].append(model['image_url'])
                
                # Вычисляем ценовой диапазон
                variants['min_price'] = min(variants['min_price'], model['price'])
                variants['max_price'] = max(variants['max_price'], model['price'])
            
            # Если все цены одинаковые, оставляем только минимальную
            if variants['min_price'] == variants['max_price']:
                variants['max_price'] = None
            
            # Добавляем базовую цену из продукта, если не заданы модели
            if not models_list and product_dict['base_price']:
                variants['min_price'] = product_dict['base_price']
            
            return {
                'product': product_dict,
                'models': models_list,
                'variants': variants
            }