import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash
from flask import current_app, g
from contextlib import contextmanager

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
                    balance DECIMAL(10, 2) DEFAULT 0.00,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS replenishments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount DECIMAL(10, 2) NOT NULL,
                    payment_date TEXT NOT NULL,
                    receipt_path TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    admin_comment TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')
            
    
    # ============== User Methods ==============
    
    def create_user(self, name, password, region, photo_path='static/default.png', is_admin=False, initial_balance=0.00):
        hashed_pw = generate_password_hash(password)
        with self.get_cursor() as cursor:
            try:
                cursor.execute(
                    '''INSERT INTO users 
                    (name, password, region, photo, is_admin, balance) 
                    VALUES (?, ?, ?, ?, ?, ?)''',
                    (name, hashed_pw, region, photo_path, int(is_admin), float(initial_balance))
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
        with self.get_cursor() as cursor:
            cursor.execute('SELECT balance FROM users WHERE id = ?', (user_id,))
            result = cursor.fetchone()
            return float(result['balance']) if result else 0.00

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

    def create_replenishment(self, user_id, amount, payment_date, receipt_path):
        """Создает заявку на пополнение баланса"""
        with self.get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO replenishments 
                (user_id, amount, payment_date, receipt_path, status)
                VALUES (?, ?, ?, ?, 'pending')
            ''', (user_id, float(amount), payment_date, receipt_path))
            return cursor.lastrowid
        
    def get_pending_replenishments(self):
        """Получает все ожидающие заявки на пополнение"""
        with self.get_cursor() as cursor:
            cursor.execute('''
                SELECT r.*, u.name as user_name 
                FROM replenishments r
                JOIN users u ON r.user_id = u.id
                WHERE r.status = 'pending'
                ORDER BY r.created_at DESC
            ''')
            return [dict(row) for row in cursor.fetchall()]

    def process_replenishment(self, replenishment_id, action, admin_id, comment=None):
        """Обрабатывает заявку на пополнение баланса с учетом структуры таблицы"""
        if action not in ('approve', 'reject'):
            raise ValueError("Недопустимое действие. Используйте 'approve' или 'reject'")
        
        with self.get_cursor() as cursor:
            try:
                # Начинаем транзакцию
                cursor.execute("BEGIN TRANSACTION")
                
                # 1. Проверяем существование и статус заявки
                cursor.execute('''
                    SELECT user_id, amount 
                    FROM replenishments 
                    WHERE id = ? AND status = 'pending'
                ''', (replenishment_id,))
                
                replenishment = cursor.fetchone()
                if not replenishment:
                    return False
                
                user_id, amount = replenishment['user_id'], replenishment['amount']
                
                # 2. Для одобрения - обновляем баланс пользователя
                if action == 'approve':
                    cursor.execute('''
                        UPDATE users 
                        SET balance = balance + ? 
                        WHERE id = ?
                    ''', (amount, user_id))
                    
                    if cursor.rowcount == 0:
                        raise Exception("Не удалось обновить баланс пользователя")
                
                # 3. Обновляем статус заявки (без processed_by, так как его нет в таблице)
                cursor.execute('''
                    UPDATE replenishments 
                    SET status = ?,
                        admin_comment = ?,
                        created_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (f"{action}ed", comment, replenishment_id))
                
                cursor.connection.commit()
                return True
                
            except Exception as e:
                if 'cursor' in locals():
                    cursor.connection.rollback()
                raise
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
        """Получает только подтверждённые (approved) пополнения"""
        with self.get_cursor() as cursor:
            # Исправляем опечатку (approveed -> approved) и убираем UNION
            cursor.execute('''
                SELECT 
                    amount,
                    created_at as date,
                    status
                FROM replenishments
                WHERE 
                    user_id = ? 
                    AND (status = 'approved' OR status = 'approveed')
                ORDER BY date DESC
            ''', (user_id,))
            
            operations = cursor.fetchall()
            current_balance = self.get_balance(user_id)
            history = []
            
            # Правильный расчёт баланса (пополнения увеличивают баланс)
            for op in operations:
                history.append({
                    'amount': op['amount'],
                    'date': op['date'],
                    'status': 'approved',  # Нормализуем статус
                    'balance_after': current_balance
                })
                current_balance -= op['amount']  # Идём от текущего баланса в прошлое
            
            return history