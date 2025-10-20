import sqlite3
import requests
import os
import re
import json
from werkzeug.security import generate_password_hash, check_password_hash
from typing import List, Dict, Any
from flask import current_app
import json, re
from collections import defaultdict
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

def convert_cny_to_rub(cny_amount):
    cny_to_rub_rate = get_cny_to_rub_rate()
    if cny_to_rub_rate is None:
        return None
    
    rub_amount = cny_amount * cny_to_rub_rate
    return round(rub_amount, 2)

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
                    balance_cny DECIMAL(10, 2) DEFAULT 0.0,
                    balance_rub DECIMAL(10, 2) DEFAULT 0.0,
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
                    product_url TEXT,
                    color_name TEXT,
                    size_name TEXT,
                    price REAL,
                    stock INTEGER,  
                    image_url TEXT,
                    status TEXT DEFAULT 'temporary',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (product_id) REFERENCES products(id)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS cart_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    model_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (model_id) REFERENCES models(id)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    model_id INTEGER NOT NULL,
                    quantity INTEGER NOT NULL,
                    status TEXT DEFAULT 'ordered',
                    additional_services TEXT,
                    total_price REAL,
                    our_tracking_number TEXT,
                    china_tracking_number TEXT,
                    cn_delivery_price REAL,
                    cn_delivery_paid BOOLEAN DEFAULT FALSE,
                    photos TEXT DEFAULT '[]',
                    weight REAL,
                    warehouse_location TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (model_id) REFERENCES models(id)
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS order_shipments  (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    model_ids TEXT NOT NULL,
                    delivery_method TEXT NOT NULL,
                    packaging_options TEXT NOT NULL,
                    recipient_name TEXT NOT NULL,
                    recipient_phone TEXT NOT NULL,
                    recipient_city TEXT NOT NULL,
                    recipient_address TEXT NOT NULL,
                    total_weight REAL NOT NULL,
                    delivery_cost REAL NOT NULL,
                    our_tracking_number TEXT,
                    packaging_cost REAL NOT NULL,
                    total_cost REAL NOT NULL,
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'processing', 'shipped', 'delivered')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    def update_balance_rub(self, user_id, amount):
        with self.get_cursor() as cursor:
            try:
                cursor.execute(
                    'UPDATE users SET balance_rub = balance_rub + ? WHERE id = ?',
                    (float(amount), user_id))
                return True
            except sqlite3.Error as e:
                current_app.logger.error(f"Balance update failed: {str(e)}")
                return False
            
    def update_balance_cny(self, user_id, amount):
        with self.get_cursor() as cursor:
            try:
                cursor.execute(
                    'UPDATE users SET balance_cny = balance_cny + ? WHERE id = ?',
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
    
    def create_replenishment(self, user_id, amount_rub,amount_cny, payment_date, receipt_path):
        with self.get_cursor() as cursor:
            try:
                cursor.execute('''
                    INSERT INTO replenishments (
                        user_id, 
                        amount_rub, 
                        amount_cny, 
                        payment_date, 
                        receipt_path
                    ) VALUES (?, ?, ?, ?, ?)
                ''', (user_id, amount_rub, amount_cny, payment_date, receipt_path))
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
        """
        История баланса: replenishments, withdrawals, оплаты заказов (orders.total_price),
        оплата CN-доставки (orders.cn_delivery_price если cn_delivery_paid = 1),
        и order_shipments.total_cost (если есть).
        """
        with self.get_cursor() as cursor:
            cursor.execute('''
                SELECT amount_rub, amount_cny, date, status, operation_type FROM (
                    -- пополнения (RUB)
                    SELECT 
                        amount_rub,
                        amount_cny,
                        created_at AS date,
                        status,
                        'replenishment' AS operation_type
                    FROM replenishments
                    WHERE user_id = ?

                    UNION ALL

                    -- выводы (RUB)
                    SELECT
                        amount AS amount_rub,
                        NULL AS amount_cny,
                        created_at AS date,
                        status,
                        'withdrawal' AS operation_type
                    FROM withdrawals
                    WHERE user_id = ?

                    UNION ALL

                    -- оплата заказа (orders.total_price в CNY)
                    SELECT
                        NULL AS amount_rub,
                        COALESCE(total_price, 0.0) AS amount_cny,
                        created_at AS date,
                        status,
                        'purchase' AS operation_type
                    FROM orders
                    WHERE user_id = ? AND COALESCE(total_price, 0) != 0

                    UNION ALL

                    -- ОТДЕЛЬНО: оплата китайской доставки (orders.cn_delivery_price),
                    -- только если пользователь уже оплатил cn_delivery_paid = 1
                    SELECT
                        NULL AS amount_rub,
                        COALESCE(cn_delivery_price, 0.0) AS amount_cny,
                        created_at AS date,
                        CASE WHEN cn_delivery_paid = 1 THEN 'approved' ELSE 'pending' END AS status,
                        'delivery_cn' AS operation_type
                    FROM orders
                    WHERE user_id = ? AND COALESCE(cn_delivery_price, 0) != 0 AND cn_delivery_paid = 1

                    UNION ALL

                    -- оплата отправки/доставки из order_shipments (если применимо)
                    SELECT
                        NULL AS amount_rub,
                        COALESCE(total_cost, 0.0) AS amount_cny,
                        created_at AS date,
                        status,
                        'shipment' AS operation_type
                    FROM order_shipments
                    WHERE user_id = ? AND COALESCE(total_cost, 0) != 0
                )
                ORDER BY date DESC
            ''', (user_id, user_id, user_id, user_id, user_id))

            rows = cursor.fetchall()

            # текущие балансы
            bal = self.get_balance(user_id)
            current_balance_rub = float(bal.get('rub', 0.0))
            current_balance_cny = float(bal.get('cny', 0.0))

            history = []

            def get_field(row, name, idx):
                try:
                    return row[name]
                except Exception:
                    try:
                        return row[idx]
                    except Exception:
                        return None

            for r in rows:
                amount_rub_raw = get_field(r, 'amount_rub', 0)
                amount_cny_raw = get_field(r, 'amount_cny', 1)
                date = get_field(r, 'date', 2)
                status = get_field(r, 'status', 3)
                op_type = get_field(r, 'operation_type', 4)

                try:
                    amount_rub_val = float(amount_rub_raw) if amount_rub_raw is not None else 0.0
                except Exception:
                    amount_rub_val = 0.0
                try:
                    amount_cny_val = float(amount_cny_raw) if amount_cny_raw is not None else 0.0
                except Exception:
                    amount_cny_val = 0.0

                change_rub = 0.0
                change_cny = 0.0
                label = None

                if op_type == 'replenishment':
                    change_rub = float(amount_rub_val)
                    label = 'Пополнение'
                elif op_type == 'withdrawal':
                    change_rub = -float(amount_rub_val)
                    label = 'Вывод'
                elif op_type == 'purchase':
                    change_cny = -float(amount_cny_val)
                    # конвертируем в рубли для отображения
                    try:
                        change_rub = -float(convert_cny_to_rub(abs(change_cny)))
                    except Exception:
                        change_rub = 0.0
                    label = 'Оплата заказа'
                elif op_type == 'delivery_cn':
                    # специально помеченная CN-доставка (cn_delivery_price)
                    change_cny = -float(amount_cny_val)
                    try:
                        change_rub = -float(convert_cny_to_rub(abs(change_cny)))
                    except Exception:
                        change_rub = 0.0
                    label = 'Оплата доставки (Китай)'
                elif op_type == 'shipment':
                    change_cny = -float(amount_cny_val)
                    try:
                        change_rub = -float(convert_cny_to_rub(abs(change_cny)))
                    except Exception:
                        change_rub = 0.0
                    label = 'Оплата отправки'
                else:
                    label = op_type or 'Операция'
                    if amount_rub_val:
                        change_rub = -float(amount_rub_val)
                    if amount_cny_val:
                        change_cny = -float(amount_cny_val)
                        try:
                            change_rub = -float(convert_cny_to_rub(abs(change_cny)))
                        except Exception:
                            pass

                # считаем balance_after только для подтверждённых/оплаченных статусов
                if status is not None and str(status).lower() in ('approved', 'paid', 'completed', 'done', 'ok', 'in_warehouse', 'purchased', 'seller_sent', 'in_transit', 'shipped', 'pending'):
                    balance_after_rub = round(current_balance_rub, 2)
                    balance_after_cny = round(current_balance_cny, 2)
                    current_balance_rub -= change_rub
                    current_balance_cny -= change_cny
                else:
                    balance_after_rub = None
                    balance_after_cny = None

                display_amount_rub = None
                display_amount_cny = None
                if amount_rub_val != 0:
                    display_amount_rub = f"{'+' if change_rub > 0 else '-'}{abs(amount_rub_val):.2f}"
                else:
                    if change_rub != 0:
                        display_amount_rub = f"{'+' if change_rub > 0 else '-'}{abs(change_rub):.2f}"

                if amount_cny_val != 0:
                    display_amount_cny = f"{'+' if change_cny > 0 else '-'}{abs(amount_cny_val):.2f}"
                else:
                    if change_cny != 0:
                        display_amount_cny = f"{'+' if change_cny > 0 else '-'}{abs(change_cny):.2f}"

                history.append({
                    'type': label,
                    'operation_type': op_type,
                    'amount_rub': display_amount_rub,
                    'amount_cny': display_amount_cny,
                    'date': date,
                    'status': status,
                    'balance_after': balance_after_cny,
                    'change_rub': change_rub,
                    'change_cny': change_cny,
                })

            return history

    
        # Вывод
    def create_withdrawal(self, user_id: int, amount: float, card_number: str,
                        card_holder: str, name: str) -> bool:
        with self.get_cursor() as cursor:
            try:
                # Вставляем заявку на вывод
                cursor.execute('''
                    INSERT INTO withdrawals (
                        user_id, amount, card_number,
                        card_holder, name, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', datetime('now'))
                ''', (
                    user_id,
                    amount,
                    card_number,
                    card_holder,
                    name
                ))
                return cursor.lastrowid is not None

            except Exception as e:
                print(f"Ошибка при создании вывода: {e}")
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
    def add_product(self, product_data, url):
        with self.get_cursor() as cursor:
            try:
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
                            product_url,
                            color_name, 
                            size_name, 
                            price, 
                            stock, 
                            image_url
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        product_id,
                        url,
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
            # print(models_list)
            
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
        
    # корзина
        
    def add_cart_item(self, user_id: int, model_id: int, quantity: int):
        with self.get_cursor() as cursor:
            # Проверим, есть ли уже запись
            cursor.execute(
                'SELECT id, quantity FROM cart_items WHERE user_id = ? AND model_id = ?',
                (user_id, model_id)
            )
            existing = cursor.fetchone()
            if existing:
                new_qty = existing['quantity'] + quantity
                cursor.execute(
                    'UPDATE cart_items SET quantity = ? WHERE id = ?',
                    (new_qty, existing['id'])
                )
            else:
                cursor.execute(
                    'INSERT INTO cart_items (user_id, model_id, quantity) VALUES (?, ?, ?)',
                    (user_id, model_id, quantity)
                )

    def get_cart_items(self, user_id: int) -> list:
        with self.get_cursor() as cursor:
            cursor.execute(
                '''
                SELECT c.model_id, c.quantity, m.price, m.color_name, m.size_name,
                       m.image_url, p.title AS product_title
                FROM cart_items c
                JOIN models m ON c.model_id = m.id
                JOIN products p ON m.product_id = p.id
                WHERE c.user_id = ?
                ''', (user_id,)
            )
            return [dict(row) for row in cursor.fetchall()]
        
    def clean_old_temporary_models(self):
        with self.get_cursor() as cursor:
            cursor.execute('''
                DELETE FROM models
                WHERE status = 'temporary'
                AND created_at < DATETIME('now', '-10 minutes')
            ''')

    def get_model_info(self, model_id):
        with self.get_cursor() as cursor:
            cursor.execute('SELECT * FROM models WHERE id = ?', (model_id,))
            product = cursor.fetchone()
            return dict(product) if product else None
        
    def get_pending_orders(self):
        with self.get_cursor() as cursor:
            try:
                cursor.execute('''
                    SELECT 
                        orders.id,
                        users.name AS user_name,
                        products.title AS product_title,
                        models.product_url AS url,
                        models.color_name AS color,
                        models.size_name AS size,
                        orders.quantity,
                        orders.status,
                        orders.total_price,
                        orders.our_tracking_number,
                        orders.china_tracking_number,
                        orders.cn_delivery_price,
                        orders.cn_delivery_paid,
                        orders.photos,
                        orders.warehouse_location,
                        orders.created_at,
                        orders.additional_services
                    FROM orders
                    JOIN users ON orders.user_id = users.id
                    JOIN models ON orders.model_id = models.id
                    JOIN products ON models.product_id = products.id
                    ORDER BY orders.created_at DESC
                ''')
                
                orders = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                orders_list = []
                for row in orders:
                    order_dict = dict(zip(columns, row))
                    
                    additional_services = order_dict['additional_services']
                    if additional_services:
                        try:
                            services_str = additional_services.strip('"\'')
                            services_list = json.loads(services_str)
                            order_dict['additional_services_list'] = services_list
                        except (json.JSONDecodeError, TypeError, AttributeError) as e:
                            print(f"Ошибка при парсинге additional_services: {e}")
                            order_dict['additional_services_list'] = []
                    else:
                        order_dict['additional_services_list'] = []
                    
                    photos = order_dict['photos']
                    if photos:
                        try:
                            photos_str = photos.strip('"\'')
                            photos_list = json.loads(photos_str)
                            order_dict['photos'] = photos_list
                        except (json.JSONDecodeError, TypeError, AttributeError) as e:
                            print(f"Ошибка при парсинге photos: {e}")
                            order_dict['photos'] = []
                    else:
                        order_dict['photos'] = []
                    
                    orders_list.append(order_dict)


                return orders_list
                
            except sqlite3.Error as e:
                print(f"Ошибка при получении заказов: {e}")
                return []
                
    def update_order_status(self, order_id, new_status):
        try:
            with self.get_cursor() as cursor:
                cursor.execute(
                    '''
                    UPDATE orders
                    SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''',
                    (new_status, order_id)
                )
            return True

        except Exception as e:
            return {'success': False, 'error': str(e)}
        
    def update_cn_delivery_price(self, order_id, price):
            try:
                with self.get_cursor() as cursor:
                    cursor.execute(
                        '''
                        UPDATE orders
                        SET cn_delivery_price = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        ''',
                        (price  , order_id)
                    )
                return True

            except Exception as e:
                return {'success': False, 'error': str(e)}
            
    def update_order_weight(self, order_id, weight):
        try:
            with self.get_cursor() as cursor:
                cursor.execute(
                    '''
                    UPDATE orders
                    SET weight = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''',
                    (weight  , order_id)
                )
            return True

        except Exception as e:
            return {'success': False, 'error': str(e)}

    # добавить вес товара в заказ
    def get_orders_by_ids(self, user_id, order_ids):
        if not order_ids:
            return []

        placeholders = ','.join('?' for _ in order_ids)
        query = f'''
            SELECT
                o.id                AS order_id,
                o.quantity          AS quantity,
                o.total_price       AS total_price,
                o.weight            AS weight,
                m.product_url       AS product_url,
                m.color_name        AS color,
                m.size_name         AS size,
                m.price             AS unit_price,
                m.image_url         AS image_url
            FROM orders o
            JOIN models m ON o.model_id = m.id
            WHERE o.user_id = ?
            AND o.id IN ({placeholders})
        '''
        params = [user_id] + order_ids

        with self.get_cursor() as cursor:
            rows = cursor.execute(query, params).fetchall()
            cols = [col[0] for col in cursor.description]
            return [dict(zip(cols, row)) for row in rows]
        
    def add_shipment(self,
                    user_id: int,
                    model_ids: list,
                    delivery_method: str,
                    packaging: list,
                    recipient_name: str,
                    recipient_phone: str,
                    recipient_city: str,
                    recipient_address: str,
                    total_weight: float,
                    delivery_cost: float,
                    packaging_cost: float = 0.0,
                    total_cost: float | None = None,
                    our_tracking_number: str | None = None
                    ) -> int:
        model_ids_str = ','.join(map(str, model_ids)) if model_ids is not None else ''
        packaging_str = ','.join(map(str, packaging)) if packaging else ''

        with self.get_cursor() as cursor:
            # Убедимся, что колонка our_tracking_number существует (безопасно)
            cursor.execute("PRAGMA table_info(order_shipments)")
            cols = [row['name'] if isinstance(row, dict) and 'name' in row else row[1] for row in cursor.fetchall()]
            if 'our_tracking_number' not in cols:
                try:
                    cursor.execute("ALTER TABLE order_shipments ADD COLUMN our_tracking_number TEXT")
                except sqlite3.OperationalError:
                    # игнорируем — миграцию нужно сделать отдельно
                    pass

            # Вставка: перечисляем колонки без лишней запятой
            cursor.execute('''
                INSERT INTO order_shipments (
                    user_id, model_ids, delivery_method, packaging_options,
                    recipient_name, recipient_phone, recipient_city, recipient_address,
                    total_weight, delivery_cost, packaging_cost, total_cost, our_tracking_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                model_ids_str,
                delivery_method,
                packaging_str,
                recipient_name,
                recipient_phone,
                recipient_city,
                recipient_address,
                total_weight,
                delivery_cost,
                packaging_cost,
                total_cost,
                our_tracking_number
            ))

    
    def get_shipments_with_photos(self, user_id):
        with self.get_cursor() as cursor:
            try:
                # Получаем все посылки пользователя
                cursor.execute('''
                    SELECT 
                        id, 
                        model_ids, 
                        recipient_city, 
                        status, 
                        created_at, 
                        recipient_address, 
                        total_weight
                    FROM order_shipments
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                ''', (user_id,))
                
                shipments = cursor.fetchall()
                if not shipments:
                    return []
                
                # Собираем все order_ids из всех посылок
                all_order_ids = set()
                shipments_list = []
                
                for shipment in shipments:
                    # Безопасное преобразование строки в список order_ids
                    order_ids_str = shipment[1] or ""
                    order_ids = []
                    
                    try:
                        if order_ids_str:
                            order_ids = [int(id_str) for id_str in order_ids_str.split(',')]
                    except ValueError:
                        print(f"Invalid order_ids format: {order_ids_str}")
                        order_ids = []
                    
                    shipment_dict = {
                        'id': shipment[0],
                        'order_ids': order_ids,  # Сохраняем как список
                        'recipient_city': shipment[2],
                        'status': shipment[3],
                        'created_at': shipment[4],
                        'recipient_address': shipment[5],
                        'total_weight': shipment[6],
                        'photo_urls': []
                    }
                    shipments_list.append(shipment_dict)
                    all_order_ids.update(order_ids)
                
                # Если нет заказов, возвращаем посылки без фото
                if not all_order_ids:
                    return shipments_list
                
                # Получаем фотографии для всех заказов одним запросом
                placeholders = ','.join(['?'] * len(all_order_ids))
                cursor.execute(f'''
                    SELECT 
                        o.id AS order_id,
                        m.image_url
                    FROM orders o
                    JOIN models m ON o.model_id = m.id
                    WHERE o.id IN ({placeholders})
                ''', list(all_order_ids))
                
                order_photos = {}
                for row in cursor.fetchall():
                    order_id = row[0]
                    image_url = row[1]
                    
                    # Если для одного заказа несколько фото (не должно быть, но для безопасности)
                    if order_id not in order_photos:
                        order_photos[order_id] = image_url
                    else:
                        # Если уже есть фото, добавляем как дополнительное
                        if isinstance(order_photos[order_id], list):
                            order_photos[order_id].append(image_url)
                        else:
                            order_photos[order_id] = [order_photos[order_id], image_url]
                
                # Добавляем фото к посылкам
                for shipment in shipments_list:
                    for order_id in shipment['order_ids']:
                        if order_id in order_photos:
                            photo = order_photos[order_id]
                            
                            # Обработка разных форматов хранения фото
                            if isinstance(photo, list):
                                shipment['photo_urls'].extend(photo)
                            else:
                                shipment['photo_urls'].append(photo)
                    
                    # Ограничиваем количество фото (например, первые 3)
                    shipment['photo_urls'] = shipment['photo_urls']
                
                return shipments_list
                
            except sqlite3.Error as e:
                print(f"Database error: {e}")
                return []        
                    
    def calculate_total_weight(self, order_ids):
        with self.get_cursor() as cursor:
            if not order_ids:
                return 0.0
            total = 0.0            
            try:
                for order_id in order_ids:
                    cursor.execute('SELECT weight FROM orders WHERE id = ?', (order_id,))
                    result = cursor.fetchone()
                    if result and result[0]:
                        total += float(result[0])
                        
                return total
                
            except sqlite3.Error as e:
                print(f"Database error: {e}")
                return 0.0
                        
    def get_pending_shipments(self) -> List[Dict[str, Any]]:
        import json, re
        from flask import current_app

        # --- читаем посылки
        try:
            with self.get_cursor() as cursor:
                cursor.execute('''
                    SELECT
                        id, user_id, model_ids, delivery_method, packaging_options,
                        recipient_name, recipient_phone, recipient_city, recipient_address,
                        total_weight, delivery_cost, packaging_cost, total_cost,
                        our_tracking_number, created_at, status
                    FROM order_shipments
                    WHERE status = 'pending'
                    ORDER BY created_at DESC
                ''')
                rows = cursor.fetchall()
        except Exception as e:
            current_app.logger.exception("Failed to fetch pending shipments: %s", e)
            return []

        def parse_ids_field(val):
            if val is None:
                return []
            if callable(val):
                current_app.logger.warning("parse_ids_field: callable found, returning []")
                return []
            ids = []
            if isinstance(val, (list, tuple)):
                for v in val:
                    try: ids.append(int(v))
                    except Exception: pass
                return ids
            if isinstance(val, str):
                s = val.strip()
                if s.startswith('[') and s.endswith(']'):
                    try:
                        arr = json.loads(s)
                        if isinstance(arr, (list, tuple)):
                            for a in arr:
                                try: ids.append(int(a))
                                except Exception: pass
                            return ids
                    except Exception:
                        pass
                for p in re.split(r'\s*,\s*', s):
                    if not p: continue
                    try: ids.append(int(p))
                    except Exception:
                        m = re.search(r'(\d+)', p)
                        if m:
                            try: ids.append(int(m.group(1)))
                            except Exception: pass
                return ids
            try: ids.append(int(val))
            except Exception: pass
            return ids

        # --- helper
        def safe_get(row, key, idx):
            try:
                if isinstance(row, dict):
                    return row.get(key)
                return row[idx]
            except Exception:
                return None

        shipments: List[Dict[str, Any]] = []

        for r in rows:
            sid                 = safe_get(r, 'id', 0)
            creator_user_id     = safe_get(r, 'user_id', 1)
            model_ids_raw       = safe_get(r, 'model_ids', 2)   # тут хранятся orders.id
            delivery_method     = safe_get(r, 'delivery_method', 3)
            packaging_raw       = safe_get(r, 'packaging_options', 4)
            recipient_name      = safe_get(r, 'recipient_name', 5)
            recipient_phone     = safe_get(r, 'recipient_phone', 6)
            recipient_city      = safe_get(r, 'recipient_city', 7)
            recipient_address   = safe_get(r, 'recipient_address', 8)
            total_weight        = safe_get(r, 'total_weight', 9)
            delivery_cost       = safe_get(r, 'delivery_cost', 10)
            packaging_cost      = safe_get(r, 'packaging_cost', 11)
            total_cost          = safe_get(r, 'total_cost', 12)
            our_tracking_number = safe_get(r, 'our_tracking_number', 13)
            created_at          = safe_get(r, 'created_at', 14)
            status              = safe_get(r, 'status', 15)

            order_ids = parse_ids_field(model_ids_raw)
            current_app.logger.debug("shipment %s parsed order_ids=%r", sid, order_ids)

            items: List[Dict[str, Any]] = []

            if order_ids:
                # --- определяем, какие поля есть в users
                try:
                    with self.get_cursor() as cursor:
                        cols = cursor.execute("PRAGMA table_info(users)").fetchall()
                    def _colname(c):
                        # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
                        if isinstance(c, dict):
                            return str(c.get('name'))
                        return str(c[1])
                    user_cols = { _colname(c) for c in cols }
                except Exception:
                    user_cols = set()

                # подберём выражения с алиасами (если нет — вернём NULL)
                def pick_user_expr(preferred, fallbacks, alias):
                    for col in ([preferred] + fallbacks):
                        if col in user_cols:
                            return f"u.{col} AS {alias}"
                    return f"NULL AS {alias}"

                u_name_expr    = pick_user_expr('name', ['full_name', 'fullname', 'username', 'login'], 'u_name')
                u_phone_expr   = pick_user_expr('phone', ['phone_number', 'tel', 'telephone', 'mobile'], 'u_phone')
                u_city_expr    = pick_user_expr('city', ['town', 'locality'], 'u_city')
                u_address_expr = pick_user_expr('address', ['addr', 'street', 'address_line'], 'u_address')

                placeholders = ','.join(['?'] * len(order_ids))
                sql = f"""
                    SELECT 
                        o.id                    AS order_id,
                        o.user_id               AS buyer_id,
                        o.model_id              AS model_id,
                        o.quantity              AS quantity,
                        o.weight                AS weight,
                        o.total_price           AS price,
                        o.our_tracking_number   AS our_tracking_number,
                        o.china_tracking_number AS china_tracking_number,
                        o.created_at            AS order_created_at,

                        m.id                    AS m_id,
                        m.product_id            AS product_id,
                        m.product_url           AS product_url,
                        m.color_name            AS color_name,
                        m.size_name             AS size_name,
                        m.image_url             AS model_image_url,

                        p.title                 AS product_title,

                        u.id                    AS u_id,
                        {u_name_expr},
                        {u_phone_expr},
                        {u_city_expr},
                        {u_address_expr}
                    FROM orders o
                    LEFT JOIN models   m ON o.model_id = m.id
                    LEFT JOIN products p ON m.product_id = p.id
                    LEFT JOIN users    u ON o.user_id = u.id
                    WHERE o.id IN ({placeholders})
                """

                try:
                    with self.get_cursor() as cursor:
                        order_rows = cursor.execute(sql, order_ids).fetchall()
                except Exception as ex:
                    current_app.logger.exception("Error fetching orders for shipment %s: %s", sid, ex)
                    order_rows = []

                def _asdict(row):
                    return dict(row) if isinstance(row, dict) else {
                        # Порядок алиасов совпадает с SELECT
                        'order_id'            : row[0],
                        'buyer_id'            : row[1],
                        'model_id'            : row[2],
                        'quantity'            : row[3],
                        'weight'              : row[4],
                        'price'               : row[5],
                        'our_tracking_number' : row[6],
                        'china_tracking_number': row[7],
                        'order_created_at'    : row[8],
                        'm_id'                : row[9],
                        'product_id'          : row[10],
                        'product_url'         : row[11],
                        'color_name'          : row[12],
                        'size_name'           : row[13],
                        'model_image_url'     : row[14],
                        'product_title'       : row[15],
                        'u_id'                : row[16],
                        'u_name'              : row[17],
                        'u_phone'             : row[18],
                        'u_city'              : row[19],
                        'u_address'           : row[20],
                    }

                order_rows = [_asdict(x) for x in order_rows]
                by_oid = { int(x['order_id']): x for x in order_rows if x.get('order_id') is not None }

                for oid in order_ids:
                    r0 = by_oid.get(int(oid))
                    if r0:
                        model_id_val = int(r0['model_id']) if r0.get('model_id') is not None else None
                        items.append({
                            'order_id': int(r0['order_id']),
                            'model_id': model_id_val,
                            'product_title': r0.get('product_title'),
                            'quantity': (int(r0['quantity']) if r0.get('quantity') is not None else None),
                            'weight': (float(r0['weight']) if r0.get('weight') is not None else None),
                            'price': (float(r0['price']) if r0.get('price') is not None else None),
                            'our_tracking_number': r0.get('our_tracking_number'),
                            'china_tracking_number': r0.get('china_tracking_number'),
                            'buyer': {
                                'id': r0.get('buyer_id') or r0.get('u_id'),
                                'name': r0.get('u_name'),
                                'phone': r0.get('u_phone'),
                                'city': r0.get('u_city'),
                                'address': r0.get('u_address'),
                            },
                            'product': {
                                'id': r0.get('product_id'),
                                'url': r0.get('product_url'),
                                'color_name': r0.get('color_name'),
                                'size_name': r0.get('size_name'),
                                'image_url': r0.get('model_image_url'),
                            },
                            'found': True
                        })
                    else:
                        items.append({
                            'order_id': int(oid),
                            'model_id': None,
                            'product_title': None,
                            'quantity': None,
                            'weight': None,
                            'price': None,
                            'our_tracking_number': None,
                            'china_tracking_number': None,
                            'buyer': None,
                            'product': None,
                            'found': False
                        })

            # packaging parse
            if packaging_raw is None or callable(packaging_raw):
                packaging_options = None
            elif isinstance(packaging_raw, (list, tuple)):
                packaging_options = [str(x) for x in packaging_raw]
            elif isinstance(packaging_raw, str):
                s = packaging_raw.strip()
                if s.startswith('[') and s.endswith(']'):
                    try:
                        parsed = json.loads(s)
                        packaging_options = [str(x) for x in parsed] if isinstance(parsed, (list, tuple)) else [s]
                    except Exception:
                        packaging_options = [p for p in re.split(r'\s*,\s*', s) if p]
                else:
                    packaging_options = [p for p in re.split(r'\s*,\s*', s) if p]
            else:
                packaging_options = None

            shipments.append({
                'id': (int(sid) if sid is not None else None),
                'creator_user_id': (int(creator_user_id) if creator_user_id is not None else None),
                'model_ids': order_ids,  # оставляем имя поля для совместимости (это именно order_ids)
                'items': items,
                'delivery_method': (str(delivery_method) if delivery_method is not None else None),
                'packaging_options': packaging_options,
                'recipient_name': recipient_name,
                'recipient_phone': recipient_phone,
                'recipient_city': recipient_city,
                'recipient_address': recipient_address,
                'total_weight': (float(total_weight) if total_weight is not None else None),
                'delivery_cost': (float(delivery_cost) if delivery_cost is not None else None),
                'packaging_cost': (float(packaging_cost) if packaging_cost is not None else None),
                'total_cost': (float(total_cost) if total_cost is not None else None),
                'our_tracking_number': (str(our_tracking_number) if our_tracking_number is not None else None),
                'created_at': created_at,
                'status': (str(status) if status is not None else None)
            })

        return shipments
