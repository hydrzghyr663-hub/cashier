import html
import hashlib
import json
import os
import sqlite3
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse


DB_PATH = os.path.join(os.path.dirname(__file__), 'pos.db')
CSS_PATH = os.path.join(os.path.dirname(__file__), 'static', 'style.css')
DEFAULT_ADMIN_PIN = os.environ.get('POS_ADMIN_PIN', '1234')


def init_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30)
    cur = conn.cursor()
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            buy_price REAL NOT NULL DEFAULT 0,
            sell_price REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            stock INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS sale_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            customer_name TEXT,
            company_name TEXT,
            payment_method TEXT NOT NULL DEFAULT 'نقدي',
            total_amount REAL NOT NULL,
            paid_amount REAL NOT NULL DEFAULT 0,
            debt_amount REAL NOT NULL DEFAULT 0,
            total_profit REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            receipt_id INTEGER,
            customer_id INTEGER,
            customer_name TEXT,
            quantity INTEGER NOT NULL,
            total REAL,
            buy_total REAL NOT NULL DEFAULT 0,
            sell_total REAL NOT NULL DEFAULT 0,
            profit REAL NOT NULL DEFAULT 0,
            payment_method TEXT DEFAULT 'نقدي',
            sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items(id),
            FOREIGN KEY (customer_id) REFERENCES customers(id),
            FOREIGN KEY (receipt_id) REFERENCES sale_receipts(id)
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'item',
            entity_id INTEGER,
            actor_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            details TEXT NOT NULL,
            status TEXT NOT NULL,
            client_ip TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS customer_debt_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            receipt_id INTEGER,
            transaction_type TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id),
            FOREIGN KEY (receipt_id) REFERENCES sale_receipts(id)
        )
        '''
    )

    item_columns = get_table_columns(conn, 'items')
    if 'buy_price' not in item_columns:
        cur.execute('ALTER TABLE items ADD COLUMN buy_price REAL NOT NULL DEFAULT 0')
    if 'sell_price' not in item_columns:
        cur.execute('ALTER TABLE items ADD COLUMN sell_price REAL NOT NULL DEFAULT 0')
    if 'price' not in item_columns:
        cur.execute('ALTER TABLE items ADD COLUMN price REAL NOT NULL DEFAULT 0')
    if 'stock' not in item_columns:
        cur.execute('ALTER TABLE items ADD COLUMN stock INTEGER NOT NULL DEFAULT 0')

    sales_columns = get_table_columns(conn, 'sales')
    for column in ['buy_total', 'sell_total', 'profit']:
        if column not in sales_columns:
            cur.execute(f'ALTER TABLE sales ADD COLUMN {column} REAL NOT NULL DEFAULT 0')
    if 'total' not in sales_columns:
        cur.execute('ALTER TABLE sales ADD COLUMN total REAL DEFAULT 0')
    if 'customer_id' not in sales_columns:
        cur.execute('ALTER TABLE sales ADD COLUMN customer_id INTEGER')
    if 'customer_name' not in sales_columns:
        cur.execute('ALTER TABLE sales ADD COLUMN customer_name TEXT')
    if 'receipt_id' not in sales_columns:
        cur.execute('ALTER TABLE sales ADD COLUMN receipt_id INTEGER')
    if 'payment_method' not in sales_columns:
        cur.execute("ALTER TABLE sales ADD COLUMN payment_method TEXT DEFAULT 'نقدي'")

    receipt_columns = get_table_columns(conn, 'sale_receipts')
    if 'paid_amount' not in receipt_columns:
        cur.execute('ALTER TABLE sale_receipts ADD COLUMN paid_amount REAL NOT NULL DEFAULT 0')
    if 'debt_amount' not in receipt_columns:
        cur.execute('ALTER TABLE sale_receipts ADD COLUMN debt_amount REAL NOT NULL DEFAULT 0')

    audit_columns = get_table_columns(conn, 'audit_logs')
    if 'entity_type' not in audit_columns:
        cur.execute("ALTER TABLE audit_logs ADD COLUMN entity_type TEXT NOT NULL DEFAULT 'item'")
    if 'entity_id' not in audit_columns:
        cur.execute('ALTER TABLE audit_logs ADD COLUMN entity_id INTEGER')
    if 'item_id' in audit_columns:
        cur.execute('UPDATE audit_logs SET entity_id = item_id WHERE entity_id IS NULL')

    default_admin_pin = _normalize_pin(DEFAULT_ADMIN_PIN)
    existing_pin = cur.execute('SELECT value FROM settings WHERE key = ?', ('admin_pin_hash',)).fetchone()
    if not existing_pin:
        cur.execute(
            '''
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ''',
            ('admin_pin_hash', _hash_pin(default_admin_pin)),
        )

    conn.commit()
    conn.close()


def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def format_iqd(amount):
    value = int(round(float(amount or 0)))
    return f'{value:,} دينار'


def _arabic_number_words_under_1000(num):
    ones = {
        0: 'صفر',
        1: 'واحد',
        2: 'اثنان',
        3: 'ثلاثة',
        4: 'أربعة',
        5: 'خمسة',
        6: 'ستة',
        7: 'سبعة',
        8: 'ثمانية',
        9: 'تسعة',
        10: 'عشرة',
        11: 'أحد عشر',
        12: 'اثنا عشر',
        13: 'ثلاثة عشر',
        14: 'أربعة عشر',
        15: 'خمسة عشر',
        16: 'ستة عشر',
        17: 'سبعة عشر',
        18: 'ثمانية عشر',
        19: 'تسعة عشر',
    }
    tens = {
        20: 'عشرون',
        30: 'ثلاثون',
        40: 'أربعون',
        50: 'خمسون',
        60: 'ستون',
        70: 'سبعون',
        80: 'ثمانون',
        90: 'تسعون',
    }
    hundreds = {
        1: 'مئة',
        2: 'مئتان',
        3: 'ثلاثمئة',
        4: 'أربعمئة',
        5: 'خمسمئة',
        6: 'ستمئة',
        7: 'سبعمئة',
        8: 'ثمانمئة',
        9: 'تسعمئة',
    }

    if num < 20:
        return ones[num]
    if num < 100:
        t = (num // 10) * 10
        u = num % 10
        if u == 0:
            return tens[t]
        return f"{ones[u]} و{tens[t]}"

    h = num // 100
    rest = num % 100
    if rest == 0:
        return hundreds[h]
    return f"{hundreds[h]} و{_arabic_number_words_under_1000(rest)}"


def amount_to_arabic_words(amount):
    value = int(round(float(amount or 0)))
    if value == 0:
        return 'صفر دينار'

    millions = value // 1_000_000
    thousands = (value % 1_000_000) // 1_000
    remainder = value % 1_000

    parts = []
    if millions:
        if millions == 1:
            parts.append('مليون')
        elif millions == 2:
            parts.append('مليونان')
        elif 3 <= millions <= 10:
            parts.append(f"{_arabic_number_words_under_1000(millions)} ملايين")
        else:
            parts.append(f"{_arabic_number_words_under_1000(millions)} مليون")

    if thousands:
        if thousands == 1:
            parts.append('ألف')
        elif thousands == 2:
            parts.append('ألفان')
        elif 3 <= thousands <= 10:
            parts.append(f"{_arabic_number_words_under_1000(thousands)} آلاف")
        else:
            parts.append(f"{_arabic_number_words_under_1000(thousands)} ألف")

    if remainder:
        parts.append(_arabic_number_words_under_1000(remainder))

    return ' و'.join(parts) + ' دينار'


def format_iqd_with_words(amount):
    return f"{format_iqd(amount)} ({amount_to_arabic_words(amount)})"


def get_table_columns(conn, table_name):
    return [row[1] for row in conn.execute(f'PRAGMA table_info({table_name})').fetchall()]


def list_items(db_path=DB_PATH):
    conn = connect(db_path)
    items = conn.execute(
        '''
        SELECT id, name,
               COALESCE(buy_price, 0) AS buy_price,
               COALESCE(sell_price, 0) AS sell_price,
               COALESCE(price, sell_price, 0) AS price,
               COALESCE(stock, 0) AS stock
        FROM items
        ORDER BY id DESC
        '''
    ).fetchall()
    conn.close()
    return items


def list_sales(db_path=DB_PATH, limit=10):
    conn = connect(db_path)
    sales = conn.execute(
        '''
        SELECT s.id, s.receipt_id, s.item_id, s.customer_id, s.customer_name, s.quantity, s.buy_total, s.sell_total,
               s.profit, s.sold_at, COALESCE(s.payment_method, 'نقدي') AS payment_method,
         i.name AS item_name, c.name AS saved_customer_name
        FROM sales s
        LEFT JOIN items i ON i.id = s.item_id
     LEFT JOIN customers c ON c.id = s.customer_id
        ORDER BY s.id DESC
        LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    conn.close()
    return sales


def list_receipts(db_path=DB_PATH, limit=10):
    conn = connect(db_path)
    receipts = conn.execute(
        '''
     SELECT r.id, r.customer_id, r.customer_name, r.company_name, r.payment_method,
         r.total_amount, r.paid_amount, r.debt_amount, r.total_profit, r.created_at,
               COUNT(s.id) AS line_count
        FROM sale_receipts r
        LEFT JOIN sales s ON s.receipt_id = r.id
        GROUP BY r.id
        ORDER BY r.id DESC
        LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    conn.close()
    return receipts


def get_receipt(receipt_id, db_path=DB_PATH):
    conn = connect(db_path)
    receipt = conn.execute(
        '''
        SELECT id, customer_id, customer_name, company_name, payment_method,
               total_amount, paid_amount, debt_amount, total_profit, created_at
        FROM sale_receipts
        WHERE id = ?
        ''',
        (receipt_id,),
    ).fetchone()
    conn.close()
    return receipt


def get_receipt_lines(receipt_id, db_path=DB_PATH):
    conn = connect(db_path)
    lines = conn.execute(
        '''
        SELECT s.id, s.item_id, s.quantity, s.buy_total, s.sell_total, s.profit, s.sold_at,
               s.customer_name, COALESCE(s.payment_method, 'نقدي') AS payment_method,
               i.name AS item_name, i.sell_price AS current_sell_price
        FROM sales s
        LEFT JOIN items i ON i.id = s.item_id
        WHERE s.receipt_id = ?
        ORDER BY s.id ASC
        ''',
        (receipt_id,),
    ).fetchall()
    conn.close()
    return lines


def get_daily_receipts(report_date=None, db_path=DB_PATH):
    report_date = report_date or date.today().isoformat()
    conn = connect(db_path)
    receipts = conn.execute(
        '''
        SELECT id, customer_name, company_name, payment_method, total_amount, paid_amount, debt_amount, total_profit, created_at
        FROM sale_receipts
        WHERE DATE(created_at) = ?
        ORDER BY id DESC
        ''',
        (report_date,),
    ).fetchall()
    summary = conn.execute(
        '''
        SELECT COUNT(*) AS receipt_count,
               COALESCE(SUM(total_amount), 0) AS total_amount,
             COALESCE(SUM(paid_amount), 0) AS paid_amount,
             COALESCE(SUM(debt_amount), 0) AS debt_amount,
               COALESCE(SUM(total_profit), 0) AS total_profit
        FROM sale_receipts
        WHERE DATE(created_at) = ?
        ''',
        (report_date,),
    ).fetchone()
    conn.close()
    return receipts, dict(summary), report_date


def list_audit_logs(db_path=DB_PATH, limit=25):
    conn = connect(db_path)
    logs = conn.execute(
        '''
        SELECT id, entity_type, entity_id, action, actor_name, reason, status, details, client_ip, created_at
        FROM audit_logs
        ORDER BY id DESC
        LIMIT ?
        ''',
        (limit,),
    ).fetchall()
    conn.close()
    return logs


def get_summary(db_path=DB_PATH):
    conn = connect(db_path)
    row = conn.execute(
        '''
        SELECT
            (SELECT COUNT(*) FROM items) AS total_items,
            (SELECT COALESCE(SUM(stock), 0) FROM items) AS total_stock,
            (SELECT COALESCE(SUM(sell_total), 0) FROM sales) AS total_sales,
            (SELECT COALESCE(SUM(profit), 0) FROM sales) AS total_profit
        '''
    ).fetchone()
    raw_total_sales = float(row['total_sales'] or 0)
    raw_total_profit = float(row['total_profit'] or 0)

    sales_baseline_setting = get_setting('sales_total_baseline', db_path=db_path)
    profit_baseline_setting = get_setting('profit_total_baseline', db_path=db_path)

    try:
        sales_baseline = float(sales_baseline_setting or 0)
    except (TypeError, ValueError):
        sales_baseline = 0.0

    try:
        profit_baseline = float(profit_baseline_setting or 0)
    except (TypeError, ValueError):
        profit_baseline = 0.0

    adjusted_total_sales = max(raw_total_sales - sales_baseline, 0.0)
    adjusted_total_profit = max(raw_total_profit - profit_baseline, 0.0)

    conn.close()
    return {
        'total_items': row['total_items'],
        'total_stock': row['total_stock'],
        'total_sales': adjusted_total_sales,
        'total_profit': adjusted_total_profit,
    }


def reset_sales_profit_totals(actor_name=None, reason=None, admin_pin=None, client_ip=None, db_path=DB_PATH):
    conn = connect(db_path)
    try:
        totals = conn.execute(
            '''
            SELECT
                COALESCE(SUM(sell_total), 0) AS total_sales,
                COALESCE(SUM(profit), 0) AS total_profit
            FROM sales
            '''
        ).fetchone()

        current_sales = float(totals['total_sales'] or 0)
        current_profit = float(totals['total_profit'] or 0)

        try:
            clean_actor_name, clean_reason = _validate_sensitive_action(
                actor_name,
                reason,
                admin_pin,
                db_path=db_path,
            )
        except Exception as exc:
            _write_audit_log(
                conn,
                'summary',
                None,
                'reset_totals',
                (actor_name or 'غير معروف').strip() or 'غير معروف',
                (reason or 'بدون سبب').strip() or 'بدون سبب',
                'denied',
                {
                    'error': str(exc),
                    'current_sales': current_sales,
                    'current_profit': current_profit,
                },
                client_ip=client_ip,
            )
            conn.commit()
            raise

        set_setting('sales_total_baseline', str(current_sales), conn)
        set_setting('profit_total_baseline', str(current_profit), conn)

        _write_audit_log(
            conn,
            'summary',
            None,
            'reset_totals',
            clean_actor_name,
            clean_reason,
            'success',
            {
                'sales_baseline': current_sales,
                'profit_baseline': current_profit,
            },
            client_ip=client_ip,
        )
        conn.commit()
    finally:
        conn.close()


def list_customers(db_path=DB_PATH):
    conn = connect(db_path)
    customers = conn.execute(
        '''
        SELECT
            c.id,
            c.name,
            c.phone,
            c.email,
            c.address,
            c.created_at,
            COALESCE((SELECT COUNT(*) FROM sales s WHERE s.customer_id = c.id), 0) AS sales_count,
            COALESCE((SELECT SUM(r.total_amount) FROM sale_receipts r WHERE r.customer_id = c.id), 0) AS total_sales_amount,
            COALESCE((SELECT SUM(r.debt_amount) FROM sale_receipts r WHERE r.customer_id = c.id), 0) AS total_debt_amount,
            COALESCE((
                SELECT SUM(
                    CASE
                        WHEN r.payment_method = 'دين' THEN r.debt_amount
                        ELSE 0
                    END
                )
                FROM sale_receipts r
                WHERE r.customer_id = c.id
            ), 0) AS open_debt_amount
        FROM customers c
        ORDER BY id DESC
        '''
    ).fetchall()
    conn.close()
    return customers


def get_customer(customer_id, db_path=DB_PATH):
    conn = connect(db_path)
    customer = conn.execute(
        '''
        SELECT id, name, phone, email, address, created_at
        FROM customers
        WHERE id = ?
        ''',
        (customer_id,),
    ).fetchone()
    conn.close()
    return customer


def get_customer_sales(customer_id, db_path=DB_PATH):
    conn = connect(db_path)
    sales = conn.execute(
        '''
        SELECT s.id, s.item_id, s.customer_name, s.quantity, s.buy_total, s.sell_total, s.profit, s.sold_at,
               COALESCE(s.payment_method, 'نقدي') AS payment_method, i.name AS item_name
        FROM sales s
        LEFT JOIN items i ON i.id = s.item_id
        WHERE s.customer_id = ?
        ORDER BY s.sold_at DESC
        ''',
        (customer_id,),
    ).fetchall()
    conn.close()
    return sales


def get_customer_receipts(customer_id, debt_only=False, db_path=DB_PATH):
    conn = connect(db_path)
    query = '''
        SELECT id, customer_name, company_name, payment_method,
               total_amount, paid_amount, debt_amount, total_profit, created_at
        FROM sale_receipts
        WHERE customer_id = ?
    '''
    params = [customer_id]

    if debt_only:
        query += ' AND debt_amount > 0'

    query += ' ORDER BY id DESC'

    receipts = conn.execute(query, params).fetchall()
    conn.close()
    return receipts


def find_customer_by_name(name, db_path=DB_PATH):
    clean_name = (name or '').strip()
    if not clean_name:
        return None

    conn = connect(db_path)
    try:
        return conn.execute(
            '''
            SELECT id, name, phone, email, address, created_at
            FROM customers
            WHERE LOWER(name) = LOWER(?)
            ORDER BY id DESC
            LIMIT 1
            ''',
            (clean_name,),
        ).fetchone()
    finally:
        conn.close()


def add_customer(name, phone, email=None, address=None, db_path=DB_PATH):
    name = (name or '').strip()
    phone = (phone or '').strip()

    if not name:
        raise ValueError('يرجى إدخال اسم الزبون')
    if not phone:
        raise ValueError('يرجى إدخال رقم الهاتف')

    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO customers (name, phone, email, address) VALUES (?, ?, ?, ?)',
            (name, phone, email or None, address or None),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError('رقم الهاتف مسجل بالفعل') from exc
    finally:
        conn.close()


def update_customer(customer_id, name, phone, email=None, address=None, db_path=DB_PATH):
    name = (name or '').strip()
    phone = (phone or '').strip()

    if not name:
        raise ValueError('يرجى إدخال اسم الزبون')
    if not phone:
        raise ValueError('يرجى إدخال رقم الهاتف')

    conn = connect(db_path)
    try:
        conn.execute(
            'UPDATE customers SET name=?, phone=?, email=?, address=? WHERE id=?',
            (name, phone, email or None, address or None, customer_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError('رقم الهاتف مسجل بالفعل') from exc
    finally:
        conn.close()


def delete_customer(customer_id, db_path=DB_PATH):
    conn = connect(db_path)
    try:
        conn.execute('DELETE FROM customers WHERE id=?', (customer_id,))
        conn.commit()
    finally:
        conn.close()


def _normalize_item_values(buy_price, sell_price=None, stock=None):
    if stock is None and isinstance(sell_price, int):
        stock = sell_price
        sell_price = None

    buy_value = float(buy_price or 0)
    sell_value = buy_value if sell_price is None else float(sell_price or 0)
    stock_value = 0 if stock is None else int(stock or 0)
    return buy_value, sell_value, stock_value


def add_item(name, buy_price, sell_price=None, stock=None, db_path=DB_PATH):
    name = (name or '').strip()
    if not name:
        raise ValueError('يرجى إدخال اسم المنتج')

    buy_value, sell_value, stock_value = _normalize_item_values(buy_price, sell_price, stock)
    conn = connect(db_path)
    try:
        conn.execute(
            'INSERT INTO items (name, buy_price, sell_price, price, stock) VALUES (?, ?, ?, ?, ?)',
            (name, buy_value, sell_value, sell_value, stock_value),
        )
        conn.commit()
    finally:
        conn.close()


def _serialize_row(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _write_audit_log(conn, entity_type, entity_id, action, actor_name, reason, status, details, client_ip=None):
    conn.execute(
        '''
        INSERT INTO audit_logs (entity_type, entity_id, action, actor_name, reason, status, details, client_ip)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            entity_type,
            entity_id,
            action,
            actor_name,
            reason,
            status,
            json.dumps(details, ensure_ascii=False),
            client_ip,
        ),
    )


def _normalize_pin(pin_value):
    clean_pin = (pin_value or '').strip()
    if not clean_pin:
        raise ValueError('يرجى إدخال رمز الأمان')
    if not clean_pin.isdigit():
        raise ValueError('رمز الأمان يجب أن يحتوي على أرقام فقط')
    if len(clean_pin) < 4:
        raise ValueError('رمز الأمان يجب أن يتكون من 4 أرقام على الأقل')
    return clean_pin


def _hash_pin(pin_value):
    return hashlib.sha256(pin_value.encode('utf-8')).hexdigest()


def get_setting(key, db_path=DB_PATH):
    conn = connect(db_path)
    try:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else None
    finally:
        conn.close()


def set_setting(key, value, conn):
    conn.execute(
        '''
        INSERT INTO settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        ''',
        (key, value),
    )


def verify_admin_pin(admin_pin, db_path=DB_PATH):
    normalized_pin = _normalize_pin(admin_pin)
    saved_hash = get_setting('admin_pin_hash', db_path=db_path)
    return saved_hash == _hash_pin(normalized_pin)


def get_security_status(db_path=DB_PATH):
    return {
        'configured': bool(get_setting('admin_pin_hash', db_path=db_path)),
    }


def _validate_sensitive_action(actor_name, reason, admin_pin, db_path=DB_PATH):
    clean_actor_name = (actor_name or '').strip()
    clean_reason = (reason or '').strip()

    if not clean_actor_name:
        raise ValueError('يرجى إدخال اسم المسؤول عن العملية')
    if not clean_reason:
        raise ValueError('يرجى كتابة سبب العملية الرقابية')
    if not verify_admin_pin(admin_pin, db_path=db_path):
        raise PermissionError('رمز الأمان غير صحيح')

    return clean_actor_name, clean_reason


def update_admin_pin(actor_name, current_pin, new_pin, confirm_pin, client_ip=None, db_path=DB_PATH):
    clean_actor_name = (actor_name or '').strip()
    if not clean_actor_name:
        raise ValueError('يرجى إدخال اسم الأدمن')

    normalized_current_pin = _normalize_pin(current_pin)
    normalized_new_pin = _normalize_pin(new_pin)
    normalized_confirm_pin = _normalize_pin(confirm_pin)

    if normalized_new_pin != normalized_confirm_pin:
        raise ValueError('تأكيد رمز الأمان غير مطابق')
    if normalized_current_pin == normalized_new_pin:
        raise ValueError('رمز الأمان الجديد يجب أن يكون مختلفًا عن الحالي')

    conn = connect(db_path)
    try:
        if not verify_admin_pin(normalized_current_pin, db_path=db_path):
            _write_audit_log(
                conn,
                'security',
                None,
                'update_pin',
                clean_actor_name,
                'محاولة تغيير رمز الأمان',
                'denied',
                {'error': 'رمز الأمان الحالي غير صحيح'},
                client_ip=client_ip,
            )
            conn.commit()
            raise PermissionError('رمز الأمان الحالي غير صحيح')

        set_setting('admin_pin_hash', _hash_pin(normalized_new_pin), conn)
        _write_audit_log(
            conn,
            'security',
            None,
            'update_pin',
            clean_actor_name,
            'تغيير رمز الأمان',
            'success',
            {'changed': True},
            client_ip=client_ip,
        )
        conn.commit()
    finally:
        conn.close()


def update_item(
    item_id,
    name,
    buy_price,
    sell_price=None,
    stock=None,
    actor_name=None,
    reason=None,
    admin_pin=None,
    client_ip=None,
    db_path=DB_PATH,
):
    buy_value, sell_value, stock_value = _normalize_item_values(buy_price, sell_price, stock)
    conn = connect(db_path)
    try:
        current_item = conn.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if not current_item:
            raise ValueError('المنتج غير موجود')

        try:
            clean_actor_name, clean_reason = _validate_sensitive_action(
                actor_name,
                reason,
                admin_pin,
                db_path=db_path,
            )
        except Exception as exc:
            _write_audit_log(
                conn,
                'item',
                item_id,
                'update',
                (actor_name or 'غير معروف').strip() or 'غير معروف',
                (reason or 'بدون سبب').strip() or 'بدون سبب',
                'denied',
                {'before': _serialize_row(current_item), 'error': str(exc)},
                client_ip=client_ip,
            )
            conn.commit()
            raise

        clean_name = (name or '').strip()
        if not clean_name:
            raise ValueError('يرجى إدخال اسم المنتج')

        conn.execute(
            'UPDATE items SET name=?, buy_price=?, sell_price=?, price=?, stock=? WHERE id=?',
            (clean_name, buy_value, sell_value, sell_value, stock_value, item_id),
        )
        updated_item = conn.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        _write_audit_log(
            conn,
            'item',
            item_id,
            'update',
            clean_actor_name,
            clean_reason,
            'success',
            {'before': _serialize_row(current_item), 'after': _serialize_row(updated_item)},
            client_ip=client_ip,
        )
        conn.commit()
    finally:
        conn.close()


def delete_item(item_id, actor_name=None, reason=None, admin_pin=None, client_ip=None, db_path=DB_PATH):
    conn = connect(db_path)
    try:
        current_item = conn.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if not current_item:
            raise ValueError('المنتج غير موجود')

        try:
            clean_actor_name, clean_reason = _validate_sensitive_action(
                actor_name,
                reason,
                admin_pin,
                db_path=db_path,
            )
        except Exception as exc:
            _write_audit_log(
                conn,
                'item',
                item_id,
                'delete',
                (actor_name or 'غير معروف').strip() or 'غير معروف',
                (reason or 'بدون سبب').strip() or 'بدون سبب',
                'denied',
                {'before': _serialize_row(current_item), 'error': str(exc)},
                client_ip=client_ip,
            )
            conn.commit()
            raise

        conn.execute('DELETE FROM items WHERE id=?', (item_id,))
        _write_audit_log(
            conn,
            'item',
            item_id,
            'delete',
            clean_actor_name,
            clean_reason,
            'success',
            {'before': _serialize_row(current_item)},
            client_ip=client_ip,
        )
        conn.commit()
    finally:
        conn.close()


def delete_receipt(receipt_id, actor_name=None, reason=None, admin_pin=None, client_ip=None, db_path=DB_PATH):
    conn = connect(db_path)
    try:
        receipt = conn.execute('SELECT * FROM sale_receipts WHERE id=?', (receipt_id,)).fetchone()
        if not receipt:
            raise ValueError('الوصل غير موجود')

        lines = conn.execute('SELECT item_id, quantity FROM sales WHERE receipt_id = ?', (receipt_id,)).fetchall()

        try:
            clean_actor_name, clean_reason = _validate_sensitive_action(
                actor_name,
                reason,
                admin_pin,
                db_path=db_path,
            )
        except Exception as exc:
            _write_audit_log(
                conn,
                'receipt',
                receipt_id,
                'delete_receipt',
                (actor_name or 'غير معروف').strip() or 'غير معروف',
                (reason or 'بدون سبب').strip() or 'بدون سبب',
                'denied',
                {'before': _serialize_row(receipt), 'error': str(exc)},
                client_ip=client_ip,
            )
            conn.commit()
            raise

        for line in lines:
            conn.execute('UPDATE items SET stock = stock + ? WHERE id = ?', (line['quantity'], line['item_id']))

        conn.execute('DELETE FROM sales WHERE receipt_id = ?', (receipt_id,))
        conn.execute('DELETE FROM customer_debt_transactions WHERE receipt_id = ?', (receipt_id,))
        conn.execute('DELETE FROM sale_receipts WHERE id = ?', (receipt_id,))

        _write_audit_log(
            conn,
            'receipt',
            receipt_id,
            'delete_receipt',
            clean_actor_name,
            clean_reason,
            'success',
            {'before': _serialize_row(receipt), 'line_count': len(lines)},
            client_ip=client_ip,
        )
        conn.commit()
    finally:
        conn.close()


def delete_all_receipts(actor_name=None, reason=None, admin_pin=None, client_ip=None, db_path=DB_PATH):
    conn = connect(db_path)
    try:
        receipts = conn.execute('SELECT * FROM sale_receipts ORDER BY id ASC').fetchall()
        if not receipts:
            raise ValueError('لا توجد وصولات لحذفها')

        try:
            clean_actor_name, clean_reason = _validate_sensitive_action(
                actor_name,
                reason,
                admin_pin,
                db_path=db_path,
            )
        except Exception as exc:
            _write_audit_log(
                conn,
                'receipt',
                None,
                'delete_all_receipts',
                (actor_name or 'غير معروف').strip() or 'غير معروف',
                (reason or 'بدون سبب').strip() or 'بدون سبب',
                'denied',
                {'receipt_count': len(receipts), 'error': str(exc)},
                client_ip=client_ip,
            )
            conn.commit()
            raise

        lines = conn.execute('SELECT item_id, quantity FROM sales').fetchall()
        for line in lines:
            conn.execute('UPDATE items SET stock = stock + ? WHERE id = ?', (line['quantity'], line['item_id']))

        conn.execute('DELETE FROM sales')
        conn.execute('DELETE FROM customer_debt_transactions')
        conn.execute('DELETE FROM sale_receipts')

        _write_audit_log(
            conn,
            'receipt',
            None,
            'delete_all_receipts',
            clean_actor_name,
            clean_reason,
            'success',
            {'receipt_count': len(receipts), 'line_count': len(lines)},
            client_ip=client_ip,
        )
        conn.commit()
    finally:
        conn.close()


def _resolve_customer_for_sale(conn, customer_id=None, customer_name=None):
    clean_customer_name = (customer_name or '').strip()
    resolved_customer_id = None

    if customer_id:
        resolved_customer_id = int(customer_id)
        customer = conn.execute('SELECT id, name FROM customers WHERE id = ?', (resolved_customer_id,)).fetchone()
        if customer:
            return customer['id'], customer['name']

    if clean_customer_name:
        matched_customer = conn.execute(
            'SELECT id, name FROM customers WHERE LOWER(name) = LOWER(?) ORDER BY id DESC LIMIT 1',
            (clean_customer_name,),
        ).fetchone()
        if matched_customer:
            return matched_customer['id'], matched_customer['name']

    return None, clean_customer_name


def _create_auto_debt_customer(conn, customer_name):
    clean_name = (customer_name or '').strip()
    if not clean_name:
        raise ValueError('يرجى إدخال اسم الزبون في البيع الآجل')

    existing = conn.execute(
        'SELECT id, name FROM customers WHERE LOWER(name) = LOWER(?) ORDER BY id DESC LIMIT 1',
        (clean_name,),
    ).fetchone()
    if existing:
        return existing['id'], existing['name']

    auto_phone = f"دين-{int(datetime.now().timestamp() * 1000)}"
    cursor = conn.execute(
        '''
        INSERT INTO customers (name, phone, email, address)
        VALUES (?, ?, ?, ?)
        ''',
        (clean_name, auto_phone, None, 'أضيف تلقائيًا من مبيعات الدين'),
    )
    return cursor.lastrowid, clean_name


def _record_debt_transaction(conn, customer_id, amount, transaction_type, receipt_id=None, note=None):
    conn.execute(
        '''
        INSERT INTO customer_debt_transactions (customer_id, receipt_id, transaction_type, amount, note)
        VALUES (?, ?, ?, ?, ?)
        ''',
        (customer_id, receipt_id, transaction_type, float(amount), note or None),
    )


def create_sale_receipt(
    lines,
    customer_id=None,
    customer_name=None,
    company_name=None,
    payment_method='نقدي',
    received_amount=None,
    db_path=DB_PATH,
):
    if not lines:
        raise ValueError('يرجى إدخال منتج واحد على الأقل')

    normalized_lines = []
    conn = connect(db_path)
    for line in lines:
        try:
            quantity = int(line.get('quantity') or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError('بيانات البيع غير صحيحة') from exc

        raw_item_id = str(line.get('item_id') or '').strip()
        item_name = (line.get('item_name') or '').strip()
        raw_unit_price = str(line.get('unit_price') or '').strip()

        try:
            item_id = int(raw_item_id) if raw_item_id else 0
        except (TypeError, ValueError):
            item_id = 0

        if item_id <= 0 and item_name:
            matched_item = conn.execute('SELECT id FROM items WHERE LOWER(name) = LOWER(?)', (item_name,)).fetchone()
            if matched_item:
                item_id = int(matched_item['id'])

        if item_id <= 0 or quantity <= 0:
            raise ValueError('كل سطر بيع يجب أن يحتوي على منتج وكمية صحيحة')

        unit_price = None
        if raw_unit_price:
            try:
                unit_price = float(raw_unit_price)
            except (TypeError, ValueError) as exc:
                raise ValueError('سعر الوحدة غير صحيح') from exc
            if unit_price <= 0:
                raise ValueError('سعر الوحدة يجب أن يكون أكبر من صفر')

        normalized_lines.append({'item_id': item_id, 'quantity': quantity, 'unit_price': unit_price})

    clean_company_name = (company_name or '').strip()
    clean_payment_method = (payment_method or 'نقدي').strip() or 'نقدي'
    try:
        resolved_customer_id, resolved_customer_name = _resolve_customer_for_sale(
            conn,
            customer_id=customer_id,
            customer_name=customer_name,
        )

        total_amount = 0.0
        total_profit = 0.0
        prepared_lines = []
        for line in normalized_lines:
            item = conn.execute('SELECT * FROM items WHERE id = ?', (line['item_id'],)).fetchone()
            if not item:
                raise ValueError('أحد المنتجات غير موجود')
            if line['quantity'] > item['stock']:
                raise ValueError(f'الكمية المطلوبة أكبر من المخزون للمنتج: {item["name"]}')

            buy_total = float(item['buy_price'] or item['price'] or 0) * line['quantity']
            base_sell_price = float(item['sell_price'] or item['price'] or 0)
            sell_unit_price = line['unit_price'] if line['unit_price'] is not None else base_sell_price
            sell_total = sell_unit_price * line['quantity']
            profit = sell_total - buy_total
            total_amount += sell_total
            total_profit += profit
            prepared_lines.append(
                {
                    'item_id': item['id'],
                    'quantity': line['quantity'],
                    'buy_total': buy_total,
                    'sell_total': sell_total,
                    'profit': profit,
                }
            )

        if clean_payment_method == 'دين':
            if not (resolved_customer_name or '').strip():
                raise ValueError('يرجى إدخال اسم الزبون في البيع الآجل')
            if (received_amount or '').strip() == '':
                paid_amount = 0.0
            else:
                try:
                    paid_amount = float(received_amount)
                except (TypeError, ValueError) as exc:
                    raise ValueError('مبلغ المستلم من الذمم غير صحيح') from exc
                if paid_amount < 0:
                    raise ValueError('مبلغ المستلم لا يمكن أن يكون سالبًا')
                if paid_amount > total_amount:
                    raise ValueError('مبلغ المستلم أكبر من إجمالي الوصل')
        else:
            paid_amount = total_amount

        debt_amount = total_amount - paid_amount

        if debt_amount > 0:
            resolved_customer_id, resolved_customer_name = _create_auto_debt_customer(conn, resolved_customer_name)

        cursor = conn.execute(
            '''
            INSERT INTO sale_receipts (
                customer_id, customer_name, company_name, payment_method,
                total_amount, paid_amount, debt_amount, total_profit
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                resolved_customer_id,
                resolved_customer_name or None,
                clean_company_name or None,
                clean_payment_method,
                total_amount,
                paid_amount,
                debt_amount,
                total_profit,
            ),
        )
        receipt_id = cursor.lastrowid

        if debt_amount > 0 and resolved_customer_id:
            _record_debt_transaction(
                conn,
                resolved_customer_id,
                debt_amount,
                'charge',
                receipt_id=receipt_id,
                note='إضافة دين من فاتورة بيع',
            )

        sales_columns = get_table_columns(conn, 'sales')
        for line in prepared_lines:
            conn.execute('UPDATE items SET stock = stock - ? WHERE id = ?', (line['quantity'], line['item_id']))
            insert_columns = ['item_id', 'quantity', 'buy_total', 'sell_total', 'profit']
            insert_values = [line['item_id'], line['quantity'], line['buy_total'], line['sell_total'], line['profit']]

            if 'receipt_id' in sales_columns:
                insert_columns.insert(0, 'receipt_id')
                insert_values.insert(0, receipt_id)

            if 'total' in sales_columns:
                insert_columns.append('total')
                insert_values.append(line['sell_total'])
            if resolved_customer_id and 'customer_id' in sales_columns:
                insert_columns.append('customer_id')
                insert_values.append(resolved_customer_id)
            if resolved_customer_name and 'customer_name' in sales_columns:
                insert_columns.append('customer_name')
                insert_values.append(resolved_customer_name)
            if 'payment_method' in sales_columns:
                insert_columns.append('payment_method')
                insert_values.append(clean_payment_method)

            placeholders = ', '.join(['?'] * len(insert_columns))
            conn.execute(
                f'INSERT INTO sales ({", ".join(insert_columns)}) VALUES ({placeholders})',
                insert_values,
            )

        conn.commit()
        return receipt_id, total_amount
    finally:
        conn.close()


def get_customer_debt_transactions(customer_id, db_path=DB_PATH):
    conn = connect(db_path)
    transactions = conn.execute(
        '''
        SELECT t.id, t.receipt_id, t.transaction_type, t.amount, t.note, t.created_at,
               r.total_amount, r.paid_amount, r.debt_amount
        FROM customer_debt_transactions t
        LEFT JOIN sale_receipts r ON r.id = t.receipt_id
        WHERE t.customer_id = ?
        ORDER BY t.id ASC
        ''',
        (customer_id,),
    ).fetchall()
    conn.close()
    return transactions


def pay_customer_debt(customer_id, amount, note=None, db_path=DB_PATH):
    try:
        payment_amount = float(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError('مبلغ التسديد غير صحيح') from exc

    if payment_amount <= 0:
        raise ValueError('مبلغ التسديد يجب أن يكون أكبر من صفر')

    conn = connect(db_path)
    try:
        customer = conn.execute('SELECT id, name FROM customers WHERE id = ?', (customer_id,)).fetchone()
        if not customer:
            raise ValueError('الزبون غير موجود')

        open_receipts = conn.execute(
            '''
            SELECT id, debt_amount
            FROM sale_receipts
            WHERE customer_id = ? AND debt_amount > 0
            ORDER BY id ASC
            ''',
            (customer_id,),
        ).fetchall()

        if not open_receipts:
            raise ValueError('لا يوجد دين مفتوح لهذا الزبون')

        total_open = sum(float(row['debt_amount']) for row in open_receipts)
        if payment_amount > total_open:
            raise ValueError('مبلغ التسديد أكبر من المتبقي على الزبون')

        remaining = payment_amount
        for receipt in open_receipts:
            if remaining <= 0:
                break

            current_debt = float(receipt['debt_amount'])
            applied = current_debt if current_debt <= remaining else remaining

            conn.execute(
                '''
                UPDATE sale_receipts
                SET paid_amount = paid_amount + ?,
                    debt_amount = debt_amount - ?
                WHERE id = ?
                ''',
                (applied, applied, receipt['id']),
            )
            remaining -= applied

        _record_debt_transaction(
            conn,
            customer_id,
            payment_amount,
            'payment',
            receipt_id=None,
            note=(note or '').strip() or 'تسديد من الذمة',
        )

        updated_open = conn.execute(
            'SELECT COALESCE(SUM(debt_amount), 0) AS open_debt FROM sale_receipts WHERE customer_id = ?',
            (customer_id,),
        ).fetchone()

        conn.commit()
        return float(updated_open['open_debt'] or 0)
    finally:
        conn.close()


def sell_item(item_id, quantity, customer_id=None, customer_name=None, payment_method='نقدي', db_path=DB_PATH):
    receipt_id, total_amount = create_sale_receipt(
        [{'item_id': item_id, 'quantity': quantity}],
        customer_id=customer_id,
        customer_name=customer_name,
        payment_method=payment_method,
        db_path=db_path,
    )
    return total_amount


def _build_customer_options(customers):
    options = ['<option value="">-- بدون زبون --</option>']
    for customer in customers:
        options.append(f'<option value="{customer["id"]}">{html.escape(customer["name"])}' + '</option>')
    return ''.join(options)


def _build_item_options(items, placeholder):
    options = [f'<option value="">{html.escape(placeholder)}</option>']
    for item in items:
        label = f'{item["name"]} (المخزون: {item["stock"]})'
        options.append(f'<option value="{item["id"]}">{html.escape(label)}' + '</option>')
    return ''.join(options)


def _build_receipt_options(receipts, placeholder):
    options = [f'<option value="">{html.escape(placeholder)}</option>']
    for receipt in receipts:
        receipt_no = f"{int(receipt['id']):04d}"
        customer_label = receipt['customer_name'] or 'بدون اسم زبون'
        label = f"وصل {receipt_no} - {customer_label} - {format_iqd(receipt['total_amount'])}"
        options.append(f'<option value="{receipt["id"]}">{html.escape(label)}' + '</option>')
    return ''.join(options)


def _render_item_cards(items):
    cards = []
    for item in items:
        stock_class = 'low' if item['stock'] <= 5 else ''
        cards.append(
            f'''
            <article class="item-card">
                <div class="item-head">
                    <h3>{html.escape(item['name'])}</h3>
                    <span class="stock-pill {stock_class}">{item['stock']} متوفر</span>
                </div>
                <p class="price">بيع: {format_iqd(item['sell_price'])}</p>
                <p class="buy-price">شراء: {format_iqd(item['buy_price'])}</p>
                <form action="/items/{item['id']}/update" method="post" class="edit-form">
                    <input type="text" name="name" value="{html.escape(item['name'])}" required>
                    <input type="number" step="0.01" name="buy_price" value="{item['buy_price']}" required>
                    <input type="number" step="0.01" name="sell_price" value="{item['sell_price']}" required>
                    <input type="number" name="stock" value="{item['stock']}" required>
                    <input type="text" name="actor_name" placeholder="اسم المسؤول" required>
                    <input type="text" name="reason" placeholder="سبب التعديل" required>
                    <input type="password" name="admin_pin" placeholder="رمز الأمان" required>
                    <button type="submit">حفظ التعديل</button>
                </form>
                <form action="/items/delete" method="post" class="edit-form">
                    <input type="hidden" name="item_id" value="{item['id']}">
                    <input type="text" name="actor_name" placeholder="اسم المسؤول" required>
                    <input type="text" name="reason" placeholder="سبب الحذف" required>
                    <input type="password" name="admin_pin" placeholder="الرمز الرقابي" required>
                    <button type="submit" class="danger">حذف المنتج</button>
                </form>
            </article>
            '''
        )
    return ''.join(cards)


def _render_sales_rows(sales):
    rows = []
    for sale in sales:
        item_name = sale['item_name'] or f'صنف #{sale["item_id"]}'
        customer_name = sale['saved_customer_name'] or sale['customer_name'] or 'بدون اسم زبون'
        rows.append(
            f'''
            <li class="sale-row">
                <div>
                    <strong>{html.escape(item_name)}</strong>
                    <p>{sale['quantity']} قطعة | البيع: {format_iqd(sale['sell_total'])} | الربح: {format_iqd(sale['profit'])}</p>
                    <p>الزبون: {html.escape(customer_name)} | الدفع: {html.escape(sale['payment_method'])}</p>
                </div>
                <span>{sale['sold_at']}</span>
            </li>
            '''
        )
    return ''.join(rows)


def _render_receipt_rows(receipts):
    if not receipts:
        return '<li class="sale-row">لا توجد وصولات بيع بعد</li>'

    rows = []
    for receipt in receipts:
        receipt_no = f"{int(receipt['id']):04d}"
        customer_label = receipt['customer_name'] or 'بدون اسم زبون'
        company_label = receipt['company_name'] or '-'
        rows.append(
            f'''
            <li class="sale-row receipt-row">
                <div>
                    <strong>وصل رقم {receipt_no}</strong>
                    <p>الزبون: {html.escape(customer_label)} | الجهة: {html.escape(company_label)}</p>
                    <p>{receipt['line_count']} منتج | الإجمالي: {format_iqd(receipt['total_amount'])} | الدفع: {html.escape(receipt['payment_method'])}</p>
                    <p>المستلم: {format_iqd(receipt['paid_amount'])} | المتبقي: {format_iqd(receipt['debt_amount'])}</p>
                </div>
                <div class="receipt-actions">
                    <span>{receipt['created_at']}</span>
                    <a class="print-link" href="/receipts/{receipt['id']}/print" target="_blank">طباعة الوصل</a>
                </div>
            </li>
            '''
        )
    return ''.join(rows)


def _render_customer_cards(customers):
    cards = []
    for customer in customers:
        total_sales_amount = float(customer['total_sales_amount'] or 0)
        total_debt_amount = float(customer['total_debt_amount'] or 0)
        open_debt_amount = float(customer['open_debt_amount'] or 0)
        sales_count = int(customer['sales_count'] or 0)
        email_html = f'<p class="email">{html.escape(customer["email"])}' + '</p>' if customer['email'] else ''
        address_html = f'<p class="address">{html.escape(customer["address"])}' + '</p>' if customer['address'] else ''
        debt_badge = (
            f'<span class="stock-pill low">دين قائم: {format_iqd(open_debt_amount)}</span>'
            if open_debt_amount > 0
            else '<span class="stock-pill">بدون دين قائم</span>'
        )
        cards.append(
            f'''
            <article class="customer-card">
                <div class="customer-head">
                    <h3>{html.escape(customer['name'])}</h3>
                    <p class="phone">{html.escape(customer['phone'])}</p>
                    {debt_badge}
                </div>
                {email_html}
                {address_html}
                <p class="address">عدد الحركات: {sales_count} | إجمالي البيع: {format_iqd(total_sales_amount)}</p>
                <p class="address">إجمالي الديون: {format_iqd(total_debt_amount)}</p>
                <div class="card-actions">
                    <form action="/customers/{customer['id']}/details" method="get">
                        <input type="hidden" name="view" value="sales">
                        <button type="submit" class="secondary">حركات البيع</button>
                    </form>
                    <form action="/customers/{customer['id']}/details" method="get">
                        <input type="hidden" name="view" value="debts">
                        <button type="submit" class="secondary">سجل الديون الكامل</button>
                    </form>
                    <form action="/customers/{customer['id']}/delete" method="post">
                        <button type="submit" class="danger">حذف</button>
                    </form>
                </div>
                <form action="/customers/{customer['id']}/update" method="post" class="edit-form">
                    <input type="text" name="name" value="{html.escape(customer['name'])}" required>
                    <input type="tel" name="phone" value="{html.escape(customer['phone'])}" required>
                    <input type="email" name="email" value="{html.escape(customer['email'] or '')}">
                    <input type="text" name="address" value="{html.escape(customer['address'] or '')}">
                    <button type="submit">تحديث</button>
                </form>
            </article>
            '''
        )
    return ''.join(cards)


def _render_debt_customers(customers):
    debt_customers = [row for row in customers if float(row['open_debt_amount'] or 0) > 0]
    if not debt_customers:
        return '<li class="sale-row">لا توجد ذمم مفتوحة حاليًا</li>'

    rows = []
    for customer in debt_customers:
        open_debt = float(customer['open_debt_amount'] or 0)
        rows.append(
            f'''
            <li class="sale-row receipt-row debt-row">
                <div>
                    <strong>{html.escape(customer['name'])}</strong>
                    <p>الهاتف: {html.escape(customer['phone'])}</p>
                    <p>المتبقي عليه: {format_iqd_with_words(open_debt)}</p>
                </div>
                <div class="receipt-actions">
                    <a class="print-link" href="/customers/{customer['id']}/details?view=debts">فتح حساب الذمم</a>
                </div>
            </li>
            '''
        )
    return ''.join(rows)


def _render_audit_rows(audit_logs):
    if not audit_logs:
        return '<tr><td colspan="8">لا توجد عمليات رقابية مسجلة بعد.</td></tr>'

    rows = []
    for log in audit_logs:
        action_labels = {
            'update': 'تعديل منتج',
            'delete': 'حذف منتج',
            'update_pin': 'تغيير رمز الأمان',
            'delete_receipt': 'حذف وصل',
            'delete_all_receipts': 'حذف كل الوصولات',
            'reset_totals': 'تصفير إجمالي المبيعات والربح',
        }
        action_label = action_labels.get(log['action'], log['action'])
        status_label = 'ناجحة' if log['status'] == 'success' else 'مرفوضة'
        status_class = 'ok' if log['status'] == 'success' else 'warn'

        product_name = '-'
        try:
            details = json.loads(log['details'] or '{}')
            if isinstance(details, dict):
                before_name = ((details.get('before') or {}).get('name') if isinstance(details.get('before'), dict) else None)
                after_name = ((details.get('after') or {}).get('name') if isinstance(details.get('after'), dict) else None)
                product_name = before_name or after_name or '-'
        except Exception:
            product_name = '-'

        rows.append(
            f'''
            <tr>
                <td>{log['created_at']}</td>
                <td>{html.escape(log['actor_name'])}</td>
                <td>{action_label}</td>
                <td>{log['entity_id'] or '-'}</td>
                <td>{html.escape(product_name)}</td>
                <td>{html.escape(log['reason'])}</td>
                <td><span class="audit-status {status_class}">{status_label}</span></td>
                <td>{html.escape(log['client_ip'] or '-')}</td>
            </tr>
            '''
        )
    return ''.join(rows)


def render_receipt_page(receipt, lines):
    receipt_no = f"{int(receipt['id']):04d}"
    customer_label = receipt['customer_name'] or 'بدون اسم زبون'
    company_label = receipt['company_name'] or '-'
    line_rows = []
    for line in lines:
        unit_price = (line['sell_total'] / line['quantity']) if line['quantity'] else 0
        line_rows.append(
            f'''
            <tr>
                <td>{html.escape(line['item_name'] or f'صنف #{line["item_id"]}')}</td>
                <td>{line['quantity']}</td>
                <td>{format_iqd(unit_price)}</td>
                <td>{format_iqd(line['sell_total'])}</td>
            </tr>
            '''
        )

    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>وصل بيع {receipt_no}</title>
    <style>
        :root {{
            --ink: #0f172a;
            --muted: #64748b;
            --line: #e2e8f0;
            --brand: #b91c1c;
            --paper: #ffffff;
            --bg: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%);
        }}
        body {{
            font-family: Tahoma, sans-serif;
            margin: 0;
            color: var(--ink);
            background: var(--bg);
            padding: 24px;
        }}
        .toolbar {{
            margin-bottom: 12px;
            display: flex;
            gap: 0.75rem;
            justify-content: flex-end;
        }}
        .toolbar a,
        .toolbar button {{
            padding: 0.7rem 1rem;
            border: none;
            border-radius: 10px;
            background: #0f172a;
            color: #fff;
            text-decoration: none;
            cursor: pointer;
            font-size: 0.95rem;
        }}
        .receipt {{
            width: 210mm;
            min-height: 297mm;
            max-width: 100%;
            margin: 0 auto;
            background: var(--paper);
            border: 1px solid var(--line);
            border-radius: 14px;
            padding: 14mm 12mm;
            box-sizing: border-box;
            box-shadow: 0 20px 45px rgba(15, 23, 42, 0.12);
            position: relative;
            overflow: hidden;
        }}
        .receipt-watermark {{
            position: absolute;
            inset: 0;
            pointer-events: none;
            user-select: none;
            z-index: 0;
        }}
        .receipt-watermark .wm-name {{
            position: absolute;
            top: 51%;
            left: 50%;
            transform: translate(-50%, -50%) rotate(-17deg);
            width: 94%;
            text-align: center;
            font-family: Tahoma, Arial, sans-serif;
            font-size: clamp(46px, 8.2vw, 92px);
            font-weight: 800;
            letter-spacing: 0;
            color: rgba(153, 27, 27, 0.13);
            line-height: 1;
            text-rendering: geometricPrecision;
            -webkit-font-smoothing: antialiased;
            text-shadow: 0 1px 2px rgba(127, 29, 29, 0.08);
        }}
        .receipt-watermark .wm-seal-ring {{
            position: absolute;
            top: 50%;
            left: 50%;
            width: min(64vw, 400px);
            aspect-ratio: 1 / 1;
            border-radius: 50%;
            border: 2px dashed rgba(127, 29, 29, 0.12);
            transform: translate(-50%, -50%) rotate(-17deg);
            background: radial-gradient(circle, rgba(127, 29, 29, 0.04) 0%, rgba(127, 29, 29, 0.01) 58%, rgba(255, 255, 255, 0) 75%);
        }}
        .receipt-watermark .wm-badge {{
            position: absolute;
            top: 63%;
            left: 50%;
            transform: translateX(-50%) rotate(-17deg);
            padding: 0.18rem 0.8rem;
            border-radius: 999px;
            border: 1px solid rgba(127, 29, 29, 0.16);
            color: rgba(127, 29, 29, 0.22);
            background: rgba(255, 255, 255, 0.18);
            font-size: 0.8rem;
            font-weight: 700;
            white-space: nowrap;
        }}
        .receipt > *:not(.receipt-watermark) {{
            position: relative;
            z-index: 1;
        }}
        .brand-head {{
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            border-bottom: 2px solid #fecaca;
            padding-bottom: 10px;
            margin-bottom: 12px;
        }}
        .receipt-no {{
            align-self: center;
            background: #fef2f2;
            color: #991b1b;
            border: 1px solid #fecaca;
            border-radius: 999px;
            padding: 0.45rem 0.9rem;
            font-size: 0.95rem;
            font-weight: 700;
            white-space: nowrap;
        }}
        .brand-title {{
            margin: 0;
            font-size: 1.4rem;
            color: var(--brand);
            font-weight: 700;
        }}
        .brand-subtitle {{
            margin: 4px 0 0;
            color: var(--muted);
            font-size: 0.95rem;
        }}
        .meta {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 8px 14px;
            margin-bottom: 12px;
        }}
        .meta div {{
            background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
            border: 1px solid var(--line);
            border-radius: 8px;
            padding: 8px 10px;
            font-size: 0.95rem;
        }}
        .meta strong {{ color: #334155; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 8px;
        }}
        thead th {{
            background: #fef2f2;
            color: #991b1b;
            border: 1px solid #fecaca;
            padding: 10px;
            font-size: 0.95rem;
        }}
        tbody td {{
            border: 1px solid var(--line);
            padding: 10px;
            font-size: 0.95rem;
        }}
        tbody tr:nth-child(even) {{ background: #fcfdff; }}
        .summary {{
            margin-top: 14px;
            margin-right: auto;
            width: min(320px, 100%);
            border: 1px solid var(--line);
            border-radius: 10px;
            overflow: hidden;
        }}
        .summary-row {{
            display: flex;
            justify-content: space-between;
            padding: 9px 12px;
            border-bottom: 1px solid var(--line);
            font-size: 0.95rem;
        }}
        .summary-row:last-child {{ border-bottom: none; }}
        .summary-row.total {{
            background: #fef2f2;
            font-weight: 700;
            color: #991b1b;
        }}
        .footer {{
            margin-top: 24px;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            color: var(--muted);
            font-size: 0.9rem;
        }}
        .owner-box {{
            margin-top: 16px;
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 10px 12px;
            background: #fff;
            color: #334155;
            font-size: 0.95rem;
        }}
        .owner-box strong {{
            color: #991b1b;
        }}
        .sign-box {{
            border-top: 1px dashed #94a3b8;
            padding-top: 8px;
            min-height: 28px;
        }}
        @page {{
            size: A4;
            margin: 10mm;
        }}
        @media print {{
            body {{ background: #fff; padding: 0; }}
            .toolbar {{ display: none; }}
            .receipt {{ border: none; border-radius: 0; width: 100%; min-height: auto; padding: 0; box-shadow: none; }}
        }}
    </style>
</head>
<body>
    <div class="toolbar">
        <button onclick="window.print()">طباعة</button>
        <a href="/">العودة للنظام</a>
    </div>
    <section class="receipt">
        <div class="receipt-watermark" aria-hidden="true">
            <span class="wm-seal-ring"></span>
            <span class="wm-name">حسين زغير</span>
            <span class="wm-badge">ختم معتمد - حسين زغير</span>
        </div>
        <div class="brand-head">
            <div>
                <h1 class="brand-title">مكتب لارا لتجارة العامة</h1>
                <p class="brand-subtitle">وصل بيع رسمي</p>
            </div>
            <div class="receipt-no">رقم الوصل: {receipt_no}</div>
        </div>

        <div class="meta">
            <div><strong>التاريخ:</strong> {receipt['created_at']}</div>
            <div><strong>طريقة الدفع:</strong> {html.escape(receipt['payment_method'])}</div>
            <div><strong>اسم الزبون:</strong> {html.escape(customer_label)}</div>
            <div><strong>الجهة:</strong> {html.escape(company_label)}</div>
        </div>

        <table>
            <thead>
                <tr>
                    <th>المنتج</th>
                    <th>الكمية</th>
                    <th>سعر الوحدة</th>
                    <th>الإجمالي</th>
                </tr>
            </thead>
            <tbody>{''.join(line_rows)}</tbody>
        </table>

        <div class="summary">
            <div class="summary-row total"><span>الإجمالي</span><span>{format_iqd_with_words(receipt['total_amount'])}</span></div>
            <div class="summary-row"><span>المستلم</span><span>{format_iqd_with_words(receipt['paid_amount'])}</span></div>
            <div class="summary-row"><span>المتبقي</span><span>{format_iqd_with_words(receipt['debt_amount'])}</span></div>
        </div>

        <div class="owner-box">
            <div><strong>صاحب المكتب:</strong> حسين زغير</div>
            <div><strong>رقم التواصل:</strong> 07828289615</div>
            <div><strong>حيدر حسين:</strong> 07828289614</div>
        </div>

        <div class="footer">
            <div class="sign-box">توقيع المستلم: ........................................</div>
            <div class="sign-box">ختم وتوقيع المكتب: ........................................</div>
        </div>
    </section>
</body>
</html>'''


def render_daily_report_page(receipts, summary, report_date):
    receipt_rows = []
    for receipt in receipts:
        receipt_no = f"{int(receipt['id']):04d}"
        receipt_rows.append(
            f'''
            <tr>
                <td>{receipt_no}</td>
                <td>{html.escape(receipt['customer_name'] or 'بدون اسم زبون')}</td>
                <td>{html.escape(receipt['company_name'] or '-')}</td>
                <td>{html.escape(receipt['payment_method'])}</td>
                <td>{format_iqd(receipt['total_amount'])}</td>
                <td>{format_iqd(receipt['paid_amount'])}</td>
                <td>{format_iqd(receipt['debt_amount'])}</td>
                <td>{format_iqd(receipt['total_profit'])}</td>
                <td>{receipt['created_at']}</td>
            </tr>
            '''
        )

    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>التقرير اليومي {report_date}</title>
    <style>
        body {{ font-family: Tahoma, sans-serif; margin: 24px; color: #111827; }}
        .toolbar {{ margin-bottom: 1rem; display: flex; gap: 0.75rem; }}
        .toolbar a, .toolbar button {{ padding: 0.7rem 1rem; border: none; border-radius: 10px; background: #0f172a; color: white; text-decoration: none; cursor: pointer; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
        th, td {{ border-bottom: 1px solid #e5e7eb; padding: 0.75rem; text-align: right; }}
        .cards {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1rem; margin: 1rem 0; }}
        .card {{ border: 1px solid #d1d5db; border-radius: 14px; padding: 1rem; }}
        @media print {{ .toolbar {{ display: none; }} body {{ margin: 0; }} }}
    </style>
</head>
<body>
    <div class="toolbar">
        <button id="print-report-btn" type="button">طباعة التقرير</button>
        <a href="/">العودة للنظام</a>
    </div>
    <h1>التقرير اليومي</h1>
    <p>التاريخ: {report_date}</p>
    <div class="cards">
        <div class="card"><strong>عدد الوصول:</strong><div>{summary['receipt_count']}</div></div>
        <div class="card"><strong>إجمالي المبيعات:</strong><div>{format_iqd(summary['total_amount'])}</div></div>
        <div class="card"><strong>إجمالي المستلم:</strong><div>{format_iqd(summary['paid_amount'])}</div></div>
        <div class="card"><strong>إجمالي المتبقي:</strong><div>{format_iqd(summary['debt_amount'])}</div></div>
        <div class="card"><strong>إجمالي الربح:</strong><div>{format_iqd(summary['total_profit'])}</div></div>
    </div>
    <table>
        <thead>
            <tr>
                <th>الوصل</th>
                <th>الزبون</th>
                <th>الجهة</th>
                <th>الدفع</th>
                <th>الإجمالي</th>
                <th>المستلم</th>
                <th>المتبقي</th>
                <th>الربح</th>
                <th>الوقت</th>
            </tr>
        </thead>
        <tbody>{''.join(receipt_rows) or '<tr><td colspan="9">لا توجد مبيعات في هذا اليوم</td></tr>'}</tbody>
    </table>
    <script>
        (function () {{
            const printBtn = document.getElementById('print-report-btn');
            if (printBtn) {{
                printBtn.addEventListener('click', function () {{
                    window.print();
                }});
            }}

            const params = new URLSearchParams(window.location.search);
            if (params.get('print') === '1') {{
                setTimeout(function () {{
                    window.print();
                }}, 120);
            }}
        }})();
    </script>
</body>
</html>'''


def render_page(items, sales, receipts, customers, summary, audit_logs, security_status, receipt_options='', message=None, message_type='success'):
    message_html = ''
    if message:
        message_html = f'<div class="alert {message_type}">{html.escape(message)}</div>'

    total_items = summary.get('total_items', 0)
    total_stock = summary.get('total_stock', 0)
    total_sales_value = summary.get('total_sales', 0)
    total_profit = summary.get('total_profit', 0)

    item_cards = _render_item_cards(items)
    sales_rows = _render_sales_rows(sales)
    receipt_rows = _render_receipt_rows(receipts)
    customer_cards = _render_customer_cards(customers)
    debt_customer_rows = _render_debt_customers(customers)
    audit_rows = _render_audit_rows(audit_logs)
    delete_item_options = _build_item_options(items, 'اختر المنتج المراد حذفه')
    delete_receipt_options = receipt_options or _build_receipt_options(receipts, 'اختر الوصل المراد حذفه')
    customer_name_options = ''.join([f'<option value="{html.escape(customer["name"])}"></option>' for customer in customers])
    item_name_options = ''.join([f'<option value="{html.escape(item["name"])}"></option>' for item in items])
    item_json = json.dumps([
        {
            'id': item['id'],
            'name': item['name'],
            'stock': item['stock'],
            'sell_price': float(item['sell_price'] or item['price'] or 0),
            'label': f"{item['name']} (المخزون: {item['stock']})",
        }
        for item in items
    ], ensure_ascii=False)
    pin_status = 'تم حفظ رمز الأمان' if security_status.get('configured') else 'لم يتم حفظ رمز الأمان بعد'

    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>نظام الكاشير</title>
    <link rel="stylesheet" href="/static/style.css">
    <style>
        .tabs {{
            display: flex;
            gap: 1rem;
            margin-bottom: 1.5rem;
            border-bottom: 2px solid #e0e0e0;
            flex-wrap: wrap;
        }}
        .tabs button {{
            padding: 0.75rem 1.5rem;
            background: none;
            border: none;
            cursor: pointer;
            font-size: 1rem;
            color: #666;
            border-bottom: 3px solid transparent;
            transition: all 0.3s;
        }}
        .tabs button.active {{
            color: #0066cc;
            border-bottom-color: #0066cc;
        }}
        .tab-content {{
            display: none;
        }}
        .tab-content.active {{
            display: block;
        }}
        .customer-card {{
            background: #f9f9f9;
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 1.5rem;
            margin-bottom: 1rem;
        }}
        .customer-head {{
            margin-bottom: 1rem;
        }}
        .customer-head h3 {{
            margin: 0 0 0.5rem 0;
            color: #333;
        }}
        .phone {{
            color: #0066cc;
            font-weight: bold;
            margin: 0.25rem 0;
        }}
        .email {{
            color: #666;
            margin: 0.25rem 0;
        }}
        .address {{
            color: #999;
            font-size: 0.9rem;
            margin: 0.25rem 0;
        }}
        .audit-table-wrapper {{
            overflow-x: auto;
        }}
        .audit-table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .audit-table th,
        .audit-table td {{
            padding: 0.85rem;
            border-bottom: 1px solid rgba(255,255,255,0.08);
            text-align: right;
        }}
        .audit-status {{
            display: inline-block;
            padding: 0.25rem 0.65rem;
            border-radius: 999px;
            font-size: 0.85rem;
        }}
        .audit-status.ok {{
            background: rgba(34, 197, 94, 0.18);
            color: #86efac;
        }}
        .audit-status.warn {{
            background: rgba(239, 68, 68, 0.18);
            color: #fca5a5;
        }}
        .security-box {{
            margin-top: 1rem;
            margin-bottom: 1rem;
            padding: 1rem;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            background: rgba(15, 23, 42, 0.55);
        }}
        .security-status {{
            margin: 0 0 1rem;
            padding: 0.85rem 1rem;
            border-radius: 12px;
            background: rgba(56, 189, 248, 0.12);
            color: #bae6fd;
        }}
        .sales-form-wrap {{
            display: grid;
            gap: 0.75rem;
            margin-bottom: 1rem;
            padding: 1rem;
            border: 1px solid rgba(148, 163, 184, 0.3);
            border-radius: 14px;
            background: rgba(15, 23, 42, 0.35);
        }}
        #sales .sales-entry-form {{
            display: grid;
            gap: 0.9rem;
        }}
        .sale-lines {{
            display: grid;
            gap: 0.75rem;
        }}
        .sale-line-head {{
            display: grid;
            grid-template-columns: minmax(320px, 2.2fr) minmax(180px, 1fr) minmax(120px, 0.7fr) 120px;
            gap: 0.75rem;
            font-size: 0.86rem;
            color: #cbd5e1;
            opacity: 0.95;
            padding: 0 0.25rem;
        }}
        .sale-line-head span:last-child {{
            text-align: center;
        }}
        .sale-line {{
            display: grid;
            grid-template-columns: minmax(320px, 2.2fr) minmax(180px, 1fr) minmax(120px, 0.7fr) 120px;
            gap: 0.75rem;
            align-items: center;
        }}
        .sale-line input,
        .sale-line button,
        .sales-meta input,
        .sales-meta select {{
            width: 100%;
            box-sizing: border-box;
            min-height: 44px;
        }}
        .sale-line button {{
            padding: 0.72rem 0.6rem;
            white-space: nowrap;
        }}
        .sale-tools {{
            display: flex;
            gap: 0.75rem;
            flex-wrap: wrap;
            justify-content: flex-start;
        }}
        .receipt-row {{
            align-items: flex-start;
        }}
        .receipt-actions {{
            display: grid;
            gap: 0.5rem;
            justify-items: end;
        }}
        .print-link {{
            display: inline-block;
            text-decoration: none;
            color: white;
            background: linear-gradient(135deg, #0284c7, #38bdf8);
            padding: 0.6rem 0.9rem;
            border-radius: 10px;
        }}
        .sales-meta {{
            display: grid;
            grid-template-columns: minmax(280px, 2fr) minmax(150px, 0.8fr) minmax(240px, 1.2fr);
            gap: 0.75rem;
        }}
        .mini-note {{
            color: #94a3b8;
            margin: 0;
            font-size: 0.9rem;
        }}
        .sales-footer-link {{
            margin-top: 1.2rem;
            margin-bottom: 1.2rem;
            padding: 0.9rem;
            border: 1px dashed rgba(148, 163, 184, 0.45);
            border-radius: 12px;
            background: rgba(2, 132, 199, 0.08);
        }}
        .sales-footer-link p {{
            margin: 0 0 0.6rem;
            color: #cbd5e1;
            font-size: 0.9rem;
        }}
        .debt-board {{
            margin-top: 1rem;
            margin-bottom: 1rem;
            padding: 0.9rem;
            border: 1px dashed rgba(239, 68, 68, 0.45);
            border-radius: 12px;
            background: rgba(127, 29, 29, 0.14);
        }}
        .debt-board h3 {{
            margin-top: 0;
            margin-bottom: 0.5rem;
            color: #fecaca;
        }}
        .debt-row strong {{
            color: #fecaca;
        }}
        #sales .panel {{
            max-width: 1360px;
            margin: 0 auto;
        }}
        @media (min-width: 1200px) {{
            .sales-meta {{
                grid-template-columns: minmax(340px, 2fr) minmax(170px, 1fr) minmax(240px, 1fr);
                align-items: end;
            }}
            .sale-line-head,
            .sale-line {{
                grid-template-columns: minmax(420px, 2.2fr) minmax(180px, 1fr) minmax(120px, 0.7fr) auto;
            }}
            .sale-line button {{ width: 132px; }}
        }}
        @media (max-width: 900px) {{
            .sales-meta {{
                grid-template-columns: 1fr;
            }}
            .sale-line-head {{
                display: none;
            }}
            .sale-line {{
                grid-template-columns: 1fr;
            }}
            .sale-tools {{
                width: 100%;
            }}
            .sale-tools button {{
                width: 100%;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header class="hero">
            <div>
                <h1>نظام نقطة البيع</h1>
                <p>إدارة المنتجات والزبائن والبيع من صفحة واحدة</p>
            </div>
            <div class="hero-badge">CA$HIER</div>
        </header>

        {message_html}

        <section class="stats-grid">
            <div class="stat-card">
                <h3>إجمالي الأصناف</h3>
                <p>{total_items}</p>
            </div>
            <div class="stat-card">
                <h3>إجمالي المخزون</h3>
                <p>{total_stock}</p>
            </div>
            <div class="stat-card">
                <h3>إجمالي المبيعات</h3>
                <p>{format_iqd(total_sales_value)}</p>
            </div>
            <div class="stat-card">
                <h3>الأرباح</h3>
                <p>{format_iqd(total_profit)}</p>
            </div>
        </section>

        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab(event, 'products')">المنتجات</button>
            <button class="tab-btn" onclick="switchTab(event, 'customers')">الزبائن</button>
            <button class="tab-btn" onclick="switchTab(event, 'sales')">المبيعات</button>
            <button class="tab-btn" onclick="switchTab(event, 'audit')">الرقابة</button>
        </div>

        <div id="products" class="tab-content active">
            <section class="panel">
                <h2>إدارة المنتجات</h2>
                <form action="/items" method="post" class="form-grid add-form">
                    <input type="text" name="name" placeholder="اسم المنتج" required>
                    <input type="number" step="0.01" name="buy_price" placeholder="سعر الشراء" required>
                    <input type="number" step="0.01" name="sell_price" placeholder="سعر البيع" required>
                    <input type="number" name="stock" placeholder="المخزون" required>
                    <button type="submit">إضافة منتج</button>
                </form>

                <p class="hint">هذا القسم مخصص للإضافة، مراجعة المخزون، وتعديل بيانات المنتجات فقط.</p>
                <div class="items-grid">{item_cards}</div>
            </section>
        </div>

        <div id="customers" class="tab-content">
            <section class="panel">
                <h2>إدارة الزبائن</h2>
                <form action="/customers" method="post" class="form-grid add-form">
                    <input type="text" name="name" placeholder="اسم الزبون" required>
                    <input type="tel" name="phone" placeholder="رقم الهاتف" required>
                    <input type="email" name="email" placeholder="البريد الإلكتروني">
                    <input type="text" name="address" placeholder="العنوان">
                    <button type="submit">إضافة زبون</button>
                </form>

                <div class="debt-board">
                    <h3>القوائم المدينة</h3>
                    <p class="mini-note">تظهر تلقائيًا كل الحسابات التي لديها متبقي دين.</p>
                    <ul class="sales-list">{debt_customer_rows}</ul>
                </div>

                <div class="items-grid">{customer_cards}</div>
            </section>
        </div>

        <div id="sales" class="tab-content">
            <section class="panel">
                <h2>المبيعات</h2>
                <div class="sales-form-wrap">
                    <form action="/sell" method="post" class="sales-entry-form">
                        <div class="sales-meta">
                            <input type="text" name="customer_name" list="customer-name-options" placeholder="اسم الزبون" required>
                            <datalist id="customer-name-options">{customer_name_options}</datalist>
                            <select name="payment_method">
                                <option value="نقدي">نقدي</option>
                                <option value="دين">دين</option>
                            </select>
                            <input type="number" step="0.01" min="0" name="received_amount" id="received-amount" placeholder="المبلغ المستلم من القائمة المدينة">
                        </div>
                        <p class="mini-note">في الدفع النقدي سيتم اعتبار كامل المبلغ مستلمًا تلقائيًا، وفي الدين يمكنك إدخال المبلغ الواصل من القائمة المدينة.</p>

                        <div class="sale-tools">
                            <button type="submit">تنفيذ البيع وطباعة الوصل</button>
                        </div>

                        <div id="sale-lines" class="sale-lines">
                            <div class="sale-line-head">
                                <span>اسم المنتج</span>
                                <span>سعر الوحدة</span>
                                <span>الكمية</span>
                                <span>إجراء</span>
                            </div>
                            <div class="sale-line">
                                <input type="search" name="item_name" list="item-name-options" placeholder="اكتب اسم المنتج واضغط Enter لإدراجه" onkeydown="handleProductEnter(event, this)" required>
                                <input type="number" name="unit_price" step="0.01" min="0.01" placeholder="سعر الوحدة (اختياري)">
                                <input type="number" name="quantity" value="1" min="1" required>
                                <button type="button" class="secondary" onclick="removeSaleLine(this)">حذف السطر</button>
                            </div>
                        </div>
                        <datalist id="item-name-options">{item_name_options}</datalist>
                    </form>
                    <div class="sales-footer-link">
                        <p>خيارات الطباعة</p>
                        <a class="print-link" href="/reports/daily?print=1" target="_blank">طباعة التقرير اليومي</a>
                    </div>
                </div>

                <h3>الوصول الحديثة</h3>
                <ul class="sales-list">{receipt_rows}</ul>

                <h3>سجل سطور المبيعات</h3>
                <ul class="sales-list">{sales_rows}</ul>
                <div class="profit-box">الأرباح الكلية: {format_iqd(total_profit)}</div>
            </section>
        </div>

        <div id="audit" class="tab-content">
            <section class="panel">
                <h2>الرقابة على التعديل والحذف</h2>
                <p class="hint">كل تعديل أو حذف للمنتجات يتطلب اسم المنفذ، سبب العملية، ورمز أمان إداري. يتم تسجيل المحاولات المرفوضة أيضًا.</p>
                <p class="security-status">{pin_status}</p>

                <div class="security-box">
                    <h3>إدارة رمز الأمان</h3>
                    <form action="/security/pin" method="post" class="form-grid add-form">
                        <input type="text" name="actor_name" placeholder="اسم الأدمن" required>
                        <input type="password" name="current_pin" placeholder="رمز الأمان الحالي" required>
                        <input type="password" name="new_pin" placeholder="رمز الأمان الجديد" required>
                        <input type="password" name="confirm_pin" placeholder="تأكيد الرمز الجديد" required>
                        <button type="submit">حفظ رمز الأمان الجديد</button>
                    </form>
                </div>

                <div class="security-box">
                    <h3>حذف منتج بشكل مراقب</h3>
                    <form action="/items/delete" method="post" class="form-grid add-form">
                        <select name="item_id" required>{delete_item_options}</select>
                        <input type="text" name="actor_name" placeholder="اسم المسؤول" required>
                        <input type="text" name="reason" placeholder="سبب الحذف" required>
                        <input type="password" name="admin_pin" placeholder="رمز الأمان" required>
                        <button type="submit" class="danger">حذف المنتج</button>
                    </form>
                </div>

                <div class="security-box">
                    <h3>إدارة حذف الوصولات</h3>
                    <form action="/receipts/delete" method="post" class="form-grid add-form">
                        <select name="receipt_id" required>{delete_receipt_options}</select>
                        <input type="text" name="actor_name" placeholder="اسم المسؤول" required>
                        <input type="text" name="reason" placeholder="سبب حذف الوصل" required>
                        <input type="password" name="admin_pin" placeholder="رمز الأمان" required>
                        <button type="submit" class="danger">حذف وصل محدد</button>
                    </form>
                    <form action="/receipts/delete-all" method="post" class="form-grid add-form" onsubmit="return confirm('سيتم حذف كل الوصولات وإرجاع المخزون. هل أنت متأكد؟');">
                        <input type="text" name="actor_name" placeholder="اسم المسؤول" required>
                        <input type="text" name="reason" placeholder="سبب حذف كل الوصولات" required>
                        <input type="password" name="admin_pin" placeholder="رمز الأمان" required>
                        <button type="submit" class="danger">حذف كل الوصولات</button>
                    </form>
                </div>

                <div class="security-box">
                    <h3>تصفير إجمالي المبيعات والربح</h3>
                    <form action="/summary/reset" method="post" class="form-grid add-form" onsubmit="return confirm('سيتم تصفير إجمالي المبيعات وإجمالي الربح الظاهرين في الواجهة. هل أنت متأكد؟');">
                        <input type="text" name="actor_name" placeholder="اسم المسؤول" required>
                        <input type="text" name="reason" placeholder="سبب التصفير" required>
                        <input type="password" name="admin_pin" placeholder="رمز الأمان" required>
                        <button type="submit" class="danger">تصفير العدادات</button>
                    </form>
                </div>

                <div class="audit-table-wrapper">
                    <table class="audit-table">
                        <thead>
                            <tr>
                                <th>الوقت</th>
                                <th>المنفذ</th>
                                <th>العملية</th>
                                <th>معرف المنتج</th>
                                <th>اسم المنتج</th>
                                <th>السبب</th>
                                <th>الحالة</th>
                                <th>عنوان المصدر</th>
                            </tr>
                        </thead>
                        <tbody>{audit_rows}</tbody>
                    </table>
                </div>
            </section>
        </div>
    </div>

    <script>
        const saleItems = {item_json};

        function handleProductEnter(event, searchInput) {{
            if (event.key !== 'Enter') {{
                return;
            }}
            event.preventDefault();

            const value = searchInput.value.trim().toLowerCase();
            if (!value) {{
                return;
            }}

            const row = searchInput.closest('.sale-line');
            const unitPriceInput = row.querySelector('input[name="unit_price"]');
            const qtyInput = row.querySelector('input[name="quantity"]');

            const matchedItem = saleItems.find(item => item.name.toLowerCase() === value) || saleItems.find(item => item.name.toLowerCase().includes(value));
            if (matchedItem) {{
                searchInput.value = matchedItem.name;
                if (!unitPriceInput.value) {{
                    unitPriceInput.value = matchedItem.sell_price.toFixed(2);
                }}
            }}

            const nextRow = addSaleLine();
            nextRow.querySelector('input[name="item_name"]').focus();
            qtyInput.value = qtyInput.value || '1';
        }}

        function addSaleLine() {{
            const container = document.getElementById('sale-lines');
            const line = document.createElement('div');
            line.className = 'sale-line';
            line.innerHTML = `
                <input type="search" name="item_name" list="item-name-options" placeholder="اكتب اسم المنتج واضغط Enter لإدراجه" onkeydown="handleProductEnter(event, this)" required>
                <input type="number" name="unit_price" step="0.01" min="0.01" placeholder="سعر الوحدة (اختياري)">
                <input type="number" name="quantity" value="1" min="1" required>
                <button type="button" class="secondary" onclick="removeSaleLine(this)">حذف السطر</button>
            `;
            container.appendChild(line);
            return line;
        }}

        function removeSaleLine(button) {{
            const container = document.getElementById('sale-lines');
            if (container.children.length === 1) {{
                const row = button.closest('.sale-line');
                row.querySelector('input[name="item_name"]').value = '';
                row.querySelector('input[name="unit_price"]').value = '';
                row.querySelector('input[name="quantity"]').value = 1;
                return;
            }}
            button.closest('.sale-line').remove();
        }}

        function switchTab(event, tabName) {{
            const contents = document.querySelectorAll('.tab-content');
            contents.forEach(tab => tab.classList.remove('active'));

            const buttons = document.querySelectorAll('.tab-btn');
            buttons.forEach(btn => btn.classList.remove('active'));

            document.getElementById(tabName).classList.add('active');
            event.currentTarget.classList.add('active');
        }}
    </script>
</body>
</html>'''


class PosHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/static/style.css':
            self.serve_static_css()
            return

        query = parse_qs(parsed.query)
        message = query.get('msg', [None])[0]
        message_type = query.get('type', ['success'])[0]

        if parsed.path.startswith('/customers/') and parsed.path.endswith('/details'):
            customer_id = int(parsed.path.split('/')[2])
            customer = get_customer(customer_id, db_path=DB_PATH)
            if customer:
                sales = get_customer_sales(customer_id, db_path=DB_PATH)
                view_mode = (query.get('view', ['sales'])[0] or 'sales').strip().lower()
                if view_mode not in {'sales', 'debts', 'all'}:
                    view_mode = 'sales'
                receipts = get_customer_receipts(customer_id, debt_only=(view_mode == 'debts'), db_path=DB_PATH)
                debt_transactions = get_customer_debt_transactions(customer_id, db_path=DB_PATH)
                body = self.render_customer_details(
                    customer,
                    sales,
                    receipts,
                    debt_transactions,
                    message,
                    message_type,
                    view_mode=view_mode,
                )
                self.send_html(body)
                return

        if parsed.path.startswith('/receipts/') and parsed.path.endswith('/print'):
            receipt_id = int(parsed.path.split('/')[2])
            receipt = get_receipt(receipt_id, db_path=DB_PATH)
            if receipt:
                body = render_receipt_page(receipt, get_receipt_lines(receipt_id, db_path=DB_PATH))
                self.send_html(body)
                return

        if parsed.path == '/reports/daily':
            receipts, report_summary, report_date = get_daily_receipts(db_path=DB_PATH)
            body = render_daily_report_page(receipts, report_summary, report_date)
            self.send_html(body)
            return

        body = render_page(
            list_items(db_path=DB_PATH),
            list_sales(db_path=DB_PATH),
            list_receipts(db_path=DB_PATH),
            list_customers(db_path=DB_PATH),
            get_summary(db_path=DB_PATH),
            list_audit_logs(db_path=DB_PATH),
            get_security_status(db_path=DB_PATH),
            receipt_options=_build_receipt_options(list_receipts(db_path=DB_PATH, limit=500), 'اختر الوصل المراد حذفه'),
            message=message,
            message_type=message_type,
        )
        self.send_html(body)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8')
        data = parse_qs(body, keep_blank_values=True)

        def first(key):
            return data.get(key, [''])[0]

        parsed = urlparse(self.path)
        message = ''
        message_type = 'success'

        try:
            if parsed.path == '/items':
                add_item(first('name'), first('buy_price'), first('sell_price'), first('stock'), db_path=DB_PATH)
                message = 'تمت إضافة المنتج بنجاح'
            elif parsed.path.endswith('/update'):
                parts = parsed.path.split('/')
                if parts[1] == 'items':
                    update_item(
                        int(parts[2]),
                        first('name'),
                        first('buy_price'),
                        first('sell_price'),
                        first('stock'),
                        actor_name=first('actor_name'),
                        reason=first('reason'),
                        admin_pin=first('admin_pin'),
                        client_ip=self.client_address[0],
                        db_path=DB_PATH,
                    )
                    message = 'تم تعديل المنتج بنجاح'
                elif parts[1] == 'customers':
                    update_customer(
                        int(parts[2]),
                        first('name'),
                        first('phone'),
                        first('email'),
                        first('address'),
                        db_path=DB_PATH,
                    )
                    message = 'تم تحديث بيانات الزبون بنجاح'
            elif parsed.path == '/items/delete':
                delete_item(
                    int(first('item_id') or '0'),
                    actor_name=first('actor_name'),
                    reason=first('reason'),
                    admin_pin=first('admin_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم حذف المنتج بنجاح'
            elif parsed.path == '/receipts/delete':
                delete_receipt(
                    int(first('receipt_id') or '0'),
                    actor_name=first('actor_name'),
                    reason=first('reason'),
                    admin_pin=first('admin_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم حذف الوصل بنجاح'
            elif parsed.path == '/receipts/delete-all':
                delete_all_receipts(
                    actor_name=first('actor_name'),
                    reason=first('reason'),
                    admin_pin=first('admin_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم حذف كل الوصولات بنجاح'
            elif parsed.path == '/summary/reset':
                reset_sales_profit_totals(
                    actor_name=first('actor_name'),
                    reason=first('reason'),
                    admin_pin=first('admin_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم تصفير إجمالي المبيعات والربح بنجاح'
            elif parsed.path == '/security/pin':
                update_admin_pin(
                    first('actor_name'),
                    first('current_pin'),
                    first('new_pin'),
                    first('confirm_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم تحديث رمز الأمان بنجاح'
            elif parsed.path.endswith('/delete'):
                parts = parsed.path.split('/')
                if parts[1] == 'customers':
                    delete_customer(int(parts[2]), db_path=DB_PATH)
                    message = 'تم حذف الزبون بنجاح'
            elif parsed.path == '/customers':
                add_customer(first('name'), first('phone'), first('email'), first('address'), db_path=DB_PATH)
                message = 'تم إضافة الزبون بنجاح'
            elif parsed.path.startswith('/customers/') and parsed.path.endswith('/debt/pay'):
                parts = parsed.path.split('/')
                if parts[1] == 'customers':
                    open_debt = pay_customer_debt(
                        int(parts[2]),
                        first('payment_amount'),
                        note=first('payment_note'),
                        db_path=DB_PATH,
                    )
                    message = f'تم تسجيل التسديد بنجاح. المتبقي: {format_iqd_with_words(open_debt)}'
                    self.send_response(303)
                    self.send_header('Location', f'/customers/{int(parts[2])}/details?view=debts&msg={quote(message)}&type=success')
                    self.end_headers()
                    return
            elif parsed.path == '/sell':
                item_ids = data.get('item_id', [])
                item_names = data.get('item_name', [])
                unit_prices = data.get('unit_price', [])
                quantities = data.get('quantity', [])
                sale_lines = []
                max_lines = max(len(item_ids), len(item_names), len(unit_prices), len(quantities))
                for idx in range(max_lines):
                    item_id_value = item_ids[idx] if idx < len(item_ids) else ''
                    item_name_value = item_names[idx] if idx < len(item_names) else ''
                    unit_price_value = unit_prices[idx] if idx < len(unit_prices) else ''
                    quantity_value = quantities[idx] if idx < len(quantities) else ''

                    if ((item_id_value or '').strip() or (item_name_value or '').strip()) and (quantity_value or '').strip():
                        sale_lines.append(
                            {
                                'item_id': item_id_value,
                                'item_name': item_name_value,
                                'unit_price': unit_price_value,
                                'quantity': quantity_value,
                            }
                        )

                receipt_id, _ = create_sale_receipt(
                    sale_lines,
                    customer_name=first('customer_name'),
                    payment_method=first('payment_method') or 'نقدي',
                    received_amount=first('received_amount'),
                    db_path=DB_PATH,
                )
                self.send_response(303)
                self.send_header('Location', f'/receipts/{receipt_id}/print')
                self.end_headers()
                return
            else:
                message = 'الطلب غير معروف'
                message_type = 'error'
        except Exception as exc:
            message = str(exc)
            message_type = 'error'

        self.send_response(303)
        self.send_header('Location', f'/?msg={quote(message)}&type={message_type}')
        self.end_headers()

    def serve_static_css(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/css; charset=utf-8')
        self.end_headers()
        with open(CSS_PATH, 'rb') as handle:
            self.wfile.write(handle.read())

    def render_customer_details(self, customer, sales, receipts, debt_transactions, message=None, message_type='success', view_mode='sales'):
        message_html = ''
        if message:
            message_html = f'<div class="alert {message_type}">{html.escape(message)}</div>'

        total_spent = sum(sale['sell_total'] for sale in sales)
        total_cash = sum(sale['sell_total'] for sale in sales if sale['payment_method'] != 'دين')
        total_debt = sum(sale['sell_total'] for sale in sales if sale['payment_method'] == 'دين')
        total_open_debt = sum(receipt['debt_amount'] for receipt in receipts)

        sales_rows = []
        for sale in sales:
            badge = 'دين' if sale['payment_method'] == 'دين' else 'نقدي'
            item_name = sale['item_name'] or f'صنف #{sale["item_id"]}'
            sales_rows.append(
                f'''
                <li class="sale-row">
                    <div>
                        <strong>{html.escape(item_name)}</strong>
                        <p>{sale['quantity']} قطعة | الإجمالي: {format_iqd(sale['sell_total'])} | {badge}</p>
                    </div>
                    <span>{sale['sold_at']}</span>
                </li>
                '''
            )

        debt_rows = []
        for receipt in receipts:
            receipt_no = f"{int(receipt['id']):04d}"
            debt_rows.append(
                f'''
                <li class="sale-row receipt-row">
                    <div>
                        <strong>وصل رقم {receipt_no}</strong>
                        <p>طريقة الدفع: {html.escape(receipt['payment_method'])} | الإجمالي: {format_iqd(receipt['total_amount'])}</p>
                        <p>المستلم: {format_iqd(receipt['paid_amount'])} | المتبقي: {format_iqd_with_words(receipt['debt_amount'])}</p>
                    </div>
                    <div class="receipt-actions">
                        <span>{receipt['created_at']}</span>
                        <a class="print-link" href="/receipts/{receipt['id']}/print" target="_blank">طباعة الوصل</a>
                    </div>
                </li>
                '''
            )

        debt_ledger_rows = []
        running_balance = 0.0
        for tx in debt_transactions:
            tx_amount = float(tx['amount'] or 0)
            tx_type = tx['transaction_type']
            if tx_type == 'charge':
                running_balance += tx_amount
            else:
                running_balance -= tx_amount

            tx_label = 'قيد دين' if tx_type == 'charge' else 'تسديد'
            receipt_label = f"{int(tx['receipt_id']):04d}" if tx['receipt_id'] else '-'
            debt_ledger_rows.append(
                f'''
                <tr>
                    <td>{tx['created_at']}</td>
                    <td>{tx_label}</td>
                    <td>{receipt_label}</td>
                    <td>{format_iqd(tx_amount)}</td>
                    <td>{format_iqd(running_balance)}</td>
                    <td>{html.escape(tx['note'] or '-')}</td>
                </tr>
                '''
            )

        sales_html = ''.join(sales_rows) or '<li class="sale-row">لا توجد عمليات شراء لهذا الزبون</li>'
        debt_html = ''.join(debt_rows) or '<li class="sale-row">لا توجد ديون مسجلة لهذا الزبون</li>'
        debt_ledger_html = ''.join(reversed(debt_ledger_rows)) or '<tr><td colspan="6">لا توجد حركات ذمم بعد</td></tr>'
        show_sales = view_mode in {'sales', 'all'}
        show_debts = view_mode in {'debts', 'all'}
        return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تفاصيل الزبون - نظام الكاشير</title>
    <link rel="stylesheet" href="/static/style.css">
    <style>
        .debt-payment-wrap {{
            margin-top: 0.75rem;
            padding: 1rem;
            border: 1px solid rgba(59, 130, 246, 0.2);
            border-radius: 12px;
            background: rgba(30, 64, 175, 0.06);
        }}
        .debt-payment-wrap .form-grid {{
            margin-top: 0.75rem;
        }}
        .debt-table-wrapper {{
            overflow-x: auto;
            margin-top: 1rem;
        }}
        .debt-table {{
            width: 100%;
            border-collapse: collapse;
        }}
        .debt-table th,
        .debt-table td {{
            padding: 0.75rem;
            border-bottom: 1px solid rgba(148, 163, 184, 0.35);
            text-align: right;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header class="hero">
            <div>
                <h1>تفاصيل الزبون</h1>
                <p>{html.escape(customer['name'])}</p>
            </div>
            <a href="/" style="padding: 0.5rem 1rem; background: #0066cc; color: white; text-decoration: none; border-radius: 4px;">العودة للرئيسية</a>
        </header>

        {message_html}

        <section class="stats-grid">
            <div class="stat-card">
                <h3>إجمالي المشتريات</h3>
                <p>{format_iqd(total_spent)}</p>
            </div>
            <div class="stat-card">
                <h3>نقدي</h3>
                <p>{format_iqd(total_cash)}</p>
            </div>
            <div class="stat-card">
                <h3>دين</h3>
                <p>{format_iqd(total_debt)}</p>
            </div>
            <div class="stat-card">
                <h3>عدد العمليات</h3>
                <p>{len(sales)}</p>
            </div>
            <div class="stat-card">
                <h3>الديون المتبقية</h3>
                <p>{format_iqd_with_words(total_open_debt)}</p>
            </div>
        </section>

        <section class="panel">
            <h2>عرض السجل</h2>
            <div class="card-actions">
                <form action="/customers/{customer['id']}/details" method="get">
                    <input type="hidden" name="view" value="sales">
                    <button type="submit" class="secondary">حركات البيع</button>
                </form>
                <form action="/customers/{customer['id']}/details" method="get">
                    <input type="hidden" name="view" value="debts">
                    <button type="submit" class="secondary">سجل الديون الكامل</button>
                </form>
                <form action="/customers/{customer['id']}/details" method="get">
                    <input type="hidden" name="view" value="all">
                    <button type="submit" class="secondary">عرض الكل</button>
                </form>
            </div>
            <div class="debt-payment-wrap">
                <strong>تسجيل تسديد دين</strong>
                <p class="mini-note">المتبقي الحالي: {format_iqd_with_words(total_open_debt)}</p>
                <form action="/customers/{customer['id']}/debt/pay" method="post" class="form-grid add-form">
                    <input type="number" step="0.01" min="0.01" name="payment_amount" placeholder="مبلغ التسديد" required>
                    <input type="text" name="payment_note" placeholder="ملاحظة (اختياري)">
                    <button type="submit">تسجيل التسديد</button>
                </form>
            </div>
        </section>

        <section class="panel">
            <h2>معلومات الزبون</h2>
            <p>الاسم: {html.escape(customer['name'])}</p>
            <p>الهاتف: {html.escape(customer['phone'])}</p>
            <p>البريد: {html.escape(customer['email'] or '-')}</p>
            <p>العنوان: {html.escape(customer['address'] or '-')}</p>
            <p>تاريخ التسجيل: {customer['created_at']}</p>
        </section>

        {f'''<section class="panel"><h2>حركات البيع</h2><ul class="sales-list">{sales_html}</ul></section>''' if show_sales else ''}
        {f'''<section class="panel"><h2>سجل الديون الكامل</h2><ul class="sales-list">{debt_html}</ul></section>''' if show_debts else ''}
        <section class="panel">
            <h2>دفتر الذمم (دين/سداد)</h2>
            <div class="debt-table-wrapper">
                <table class="debt-table">
                    <thead>
                        <tr>
                            <th>الوقت</th>
                            <th>نوع الحركة</th>
                            <th>الوصل</th>
                            <th>المبلغ</th>
                            <th>الرصيد بعد الحركة</th>
                            <th>ملاحظة</th>
                        </tr>
                    </thead>
                    <tbody>{debt_ledger_html}</tbody>
                </table>
            </div>
        </section>
    </div>
</body>
</html>'''

    def send_html(self, body):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def log_message(self, format, *args):
        return


def run_server(host='127.0.0.1', port=5000):
    init_db()
    server = ThreadingHTTPServer((host, port), PosHandler)
    print(f'الخادم يعمل على http://{host}:{port}')
    server.serve_forever()


if __name__ == '__main__':
    env_host = os.environ.get('HOST', '0.0.0.0')
    env_port = int(os.environ.get('PORT', '5000'))
    run_server(host=env_host, port=env_port)
