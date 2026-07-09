import html
import base64
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

try:
    from rapidfuzz import fuzz
except ImportError:
    from difflib import SequenceMatcher

    class _FuzzFallback:
        @staticmethod
        def ratio(left, right):
            return SequenceMatcher(None, left, right).ratio() * 100

        @classmethod
        def partial_ratio(cls, left, right):
            if not left or not right:
                return 0.0
            shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
            if shorter in longer:
                return 100.0
            window = len(shorter)
            if window <= 0:
                return 0.0
            best = 0.0
            for idx in range(max(len(longer) - window + 1, 1)):
                best = max(best, cls.ratio(shorter, longer[idx:idx + window]))
            return best

        @classmethod
        def token_sort_ratio(cls, left, right):
            sort_left = ' '.join(sorted(left.split()))
            sort_right = ' '.join(sorted(right.split()))
            return cls.ratio(sort_left, sort_right)

        @classmethod
        def token_set_ratio(cls, left, right):
            left_tokens = set(left.split())
            right_tokens = set(right.split())
            common = sorted(left_tokens & right_tokens)
            left_only = sorted(left_tokens - right_tokens)
            right_only = sorted(right_tokens - left_tokens)
            base = ' '.join(common)
            left_text = ' '.join(common + left_only)
            right_text = ' '.join(common + right_only)
            return max(cls.ratio(base, left_text), cls.ratio(base, right_text), cls.ratio(left_text, right_text))

    fuzz = _FuzzFallback()


DB_PATH = os.environ.get('POS_DB_PATH', os.path.join(os.path.dirname(__file__), 'pos.db'))
CSS_PATH = os.path.join(os.path.dirname(__file__), 'static', 'style.css')
DEFAULT_ADMIN_PIN = os.environ.get('POS_ADMIN_PIN', '1234')
LOGIN_USERNAME = os.environ.get('POS_LOGIN_USERNAME', 'حيدر')
LOGIN_PASSWORD = os.environ.get('POS_LOGIN_PASSWORD', '1')
AUTH_COOKIE_NAME = 'pos_auth'
AUTH_COOKIE_SECRET = os.environ.get('POS_AUTH_SECRET', 'cashier-auth')
APP_TABS = [
    {'id': 'products', 'label': 'المنتجات'},
    {'id': 'customers', 'label': 'الزبائن'},
    {'id': 'suppliers', 'label': 'الشركات'},
    {'id': 'sales', 'label': 'المبيعات'},
    {'id': 'audit', 'label': 'الرقابة'},
    {'id': 'users', 'label': 'المستخدمون', 'admin_only': True},
]
ARABIC_SEARCH_TRANSLATION = str.maketrans(
    {
        'أ': 'ا',
        'إ': 'ا',
        'آ': 'ا',
        'ٱ': 'ا',
        'ة': 'ه',
        'ى': 'ي',
        'ؤ': 'و',
        'ئ': 'ي',
        'ء': '',
        'ـ': '',
    }
)


def init_db(db_path=DB_PATH):
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
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
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS supplier_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            phone TEXT,
            email TEXT,
            address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS supplier_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER,
            supplier_name TEXT NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'نقدي',
            total_amount REAL NOT NULL,
            paid_amount REAL NOT NULL DEFAULT 0,
            debt_amount REAL NOT NULL DEFAULT 0,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (supplier_id) REFERENCES supplier_companies(id)
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS supplier_purchase_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER NOT NULL,
            item_id INTEGER,
            item_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            unit_cost REAL NOT NULL,
            total_cost REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (purchase_id) REFERENCES supplier_purchases(id),
            FOREIGN KEY (item_id) REFERENCES items(id)
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS supplier_debt_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supplier_id INTEGER NOT NULL,
            purchase_id INTEGER,
            transaction_type TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (supplier_id) REFERENCES supplier_companies(id),
            FOREIGN KEY (purchase_id) REFERENCES supplier_purchases(id)
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS app_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            visible_tabs TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

    supplier_columns = get_table_columns(conn, 'supplier_companies')
    if 'phone' not in supplier_columns:
        cur.execute('ALTER TABLE supplier_companies ADD COLUMN phone TEXT')
    if 'email' not in supplier_columns:
        cur.execute('ALTER TABLE supplier_companies ADD COLUMN email TEXT')
    if 'address' not in supplier_columns:
        cur.execute('ALTER TABLE supplier_companies ADD COLUMN address TEXT')

    supplier_purchase_columns = get_table_columns(conn, 'supplier_purchases')
    if 'supplier_id' not in supplier_purchase_columns:
        cur.execute('ALTER TABLE supplier_purchases ADD COLUMN supplier_id INTEGER')
    if 'supplier_name' not in supplier_purchase_columns:
        cur.execute('ALTER TABLE supplier_purchases ADD COLUMN supplier_name TEXT')
    if 'payment_method' not in supplier_purchase_columns:
        cur.execute("ALTER TABLE supplier_purchases ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'نقدي'")
    if 'total_amount' not in supplier_purchase_columns:
        cur.execute('ALTER TABLE supplier_purchases ADD COLUMN total_amount REAL NOT NULL DEFAULT 0')
    if 'paid_amount' not in supplier_purchase_columns:
        cur.execute('ALTER TABLE supplier_purchases ADD COLUMN paid_amount REAL NOT NULL DEFAULT 0')
    if 'debt_amount' not in supplier_purchase_columns:
        cur.execute('ALTER TABLE supplier_purchases ADD COLUMN debt_amount REAL NOT NULL DEFAULT 0')
    if 'note' not in supplier_purchase_columns:
        cur.execute('ALTER TABLE supplier_purchases ADD COLUMN note TEXT')

    supplier_purchase_line_columns = get_table_columns(conn, 'supplier_purchase_lines')
    if 'item_id' not in supplier_purchase_line_columns:
        cur.execute('ALTER TABLE supplier_purchase_lines ADD COLUMN item_id INTEGER')
    if 'item_name' not in supplier_purchase_line_columns:
        cur.execute('ALTER TABLE supplier_purchase_lines ADD COLUMN item_name TEXT')
    if 'quantity' not in supplier_purchase_line_columns:
        cur.execute('ALTER TABLE supplier_purchase_lines ADD COLUMN quantity INTEGER NOT NULL DEFAULT 0')
    if 'unit_cost' not in supplier_purchase_line_columns:
        cur.execute('ALTER TABLE supplier_purchase_lines ADD COLUMN unit_cost REAL NOT NULL DEFAULT 0')
    if 'total_cost' not in supplier_purchase_line_columns:
        cur.execute('ALTER TABLE supplier_purchase_lines ADD COLUMN total_cost REAL NOT NULL DEFAULT 0')

    supplier_debt_columns = get_table_columns(conn, 'supplier_debt_transactions')
    if 'supplier_id' not in supplier_debt_columns:
        cur.execute('ALTER TABLE supplier_debt_transactions ADD COLUMN supplier_id INTEGER')
    if 'purchase_id' not in supplier_debt_columns:
        cur.execute('ALTER TABLE supplier_debt_transactions ADD COLUMN purchase_id INTEGER')
    if 'transaction_type' not in supplier_debt_columns:
        cur.execute("ALTER TABLE supplier_debt_transactions ADD COLUMN transaction_type TEXT NOT NULL DEFAULT 'charge'")
    if 'amount' not in supplier_debt_columns:
        cur.execute('ALTER TABLE supplier_debt_transactions ADD COLUMN amount REAL NOT NULL DEFAULT 0')
    if 'note' not in supplier_debt_columns:
        cur.execute('ALTER TABLE supplier_debt_transactions ADD COLUMN note TEXT')

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
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def get_tab_definitions():
    return [dict(tab) for tab in APP_TABS]


def get_assignable_tabs():
    return [tab for tab in get_tab_definitions() if not tab.get('admin_only')]


def normalize_visible_tabs(tab_ids, include_admin_tabs=False):
    allowed_map = {tab['id']: tab for tab in get_tab_definitions() if include_admin_tabs or not tab.get('admin_only')}
    normalized = []
    for tab_id in tab_ids or []:
        clean_tab_id = (tab_id or '').strip()
        if clean_tab_id in allowed_map and clean_tab_id not in normalized:
            normalized.append(clean_tab_id)
    return normalized


def _hash_user_password(password):
    return hashlib.sha256(f'user\0{password}\0{AUTH_COOKIE_SECRET}'.encode('utf-8')).hexdigest()


def _encode_auth_username(username):
    return base64.urlsafe_b64encode((username or '').encode('utf-8')).decode('ascii').rstrip('=')


def _decode_auth_username(value):
    if not value:
        return ''
    padding = '=' * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode('ascii')).decode('utf-8')


def build_auth_cookie_value(username, secret_value):
    encoded_username = _encode_auth_username(username)
    token = build_auth_token(username, secret_value)
    return f'{encoded_username}.{token}'


def _extract_auth_cookie_identity(cookie_value):
    if not cookie_value or '.' not in cookie_value:
        return None, None
    encoded_username, token = cookie_value.split('.', 1)
    try:
        username = _decode_auth_username(encoded_username)
    except Exception:
        return None, None
    return username, token


def _admin_user_record():
    return {
        'id': 0,
        'username': LOGIN_USERNAME,
        'display_name': LOGIN_USERNAME,
        'password_hash': _hash_user_password(LOGIN_PASSWORD),
        'visible_tabs': [tab['id'] for tab in get_assignable_tabs()],
        'is_admin': True,
        'is_active': 1,
    }


def _row_to_app_user(row):
    if not row:
        return None
    visible_tabs = []
    raw_tabs = row['visible_tabs']
    try:
        parsed_tabs = json.loads(raw_tabs or '[]')
        if isinstance(parsed_tabs, list):
            visible_tabs = normalize_visible_tabs(parsed_tabs)
    except Exception:
        visible_tabs = []

    return {
        'id': int(row['id']),
        'username': row['username'],
        'display_name': row['display_name'],
        'password_hash': row['password_hash'],
        'visible_tabs': visible_tabs,
        'is_admin': False,
        'is_active': int(row['is_active'] or 0),
    }


def list_app_users(db_path=DB_PATH):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            '''
            SELECT id, username, display_name, password_hash, visible_tabs, is_active, created_at
            FROM app_users
            ORDER BY id DESC
            '''
        ).fetchall()
        return [_row_to_app_user(row) for row in rows]
    finally:
        conn.close()


def get_app_user_by_username(username, db_path=DB_PATH):
    clean_username = (username or '').strip()
    if not clean_username:
        return None

    if clean_username == LOGIN_USERNAME:
        return _admin_user_record()

    conn = connect(db_path)
    try:
        row = conn.execute(
            '''
            SELECT id, username, display_name, password_hash, visible_tabs, is_active, created_at
            FROM app_users
            WHERE username = ?
            ''',
            (clean_username,),
        ).fetchone()
        return _row_to_app_user(row)
    finally:
        conn.close()


def create_app_user(username, password, display_name=None, visible_tabs=None, actor_name=None, db_path=DB_PATH):
    clean_username = (username or '').strip()
    clean_password = (password or '').strip()
    clean_display_name = (display_name or '').strip() or clean_username
    normalized_tabs = normalize_visible_tabs(visible_tabs)

    if not clean_username:
        raise ValueError('يرجى إدخال اسم المستخدم')
    if clean_username == LOGIN_USERNAME:
        raise ValueError('اسم المستخدم محجوز للأدمن')
    if not clean_password:
        raise ValueError('يرجى إدخال كلمة مرور للمستخدم')
    if not normalized_tabs:
        raise ValueError('يرجى اختيار واجهة واحدة على الأقل للمستخدم')

    conn = connect(db_path)
    try:
        cursor = conn.execute(
            '''
            INSERT INTO app_users (username, display_name, password_hash, visible_tabs, is_active)
            VALUES (?, ?, ?, ?, 1)
            ''',
            (clean_username, clean_display_name, _hash_user_password(clean_password), json.dumps(normalized_tabs, ensure_ascii=False)),
        )
        user_id = cursor.lastrowid
        created_user = conn.execute(
            '''
            SELECT id, username, display_name, visible_tabs, is_active
            FROM app_users
            WHERE id = ?
            ''',
            (user_id,),
        ).fetchone()
        _write_audit_log(
            conn,
            'user',
            user_id,
            'user_add',
            (actor_name or 'النظام').strip() or 'النظام',
            f'تم إنشاء المستخدم "{clean_display_name}"',
            'success',
            {'after': _serialize_row(created_user)},
            client_ip=None,
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError('اسم المستخدم مسجل بالفعل') from exc
    finally:
        conn.close()


def update_app_user(user_id, username, display_name=None, password=None, visible_tabs=None, actor_name=None, db_path=DB_PATH):
    clean_username = (username or '').strip()
    clean_display_name = (display_name or '').strip() or clean_username
    clean_password = (password or '').strip()
    normalized_tabs = normalize_visible_tabs(visible_tabs)

    if not clean_username:
        raise ValueError('يرجى إدخال اسم المستخدم')
    if clean_username == LOGIN_USERNAME:
        raise ValueError('اسم المستخدم محجوز للأدمن')
    if not normalized_tabs:
        raise ValueError('يرجى اختيار واجهة واحدة على الأقل للمستخدم')

    conn = connect(db_path)
    try:
        existing = conn.execute('SELECT * FROM app_users WHERE id = ?', (user_id,)).fetchone()
        if not existing:
            raise ValueError('المستخدم غير موجود')

        if clean_password:
            conn.execute(
                '''
                UPDATE app_users
                SET username = ?, display_name = ?, password_hash = ?, visible_tabs = ?
                WHERE id = ?
                ''',
                (clean_username, clean_display_name, _hash_user_password(clean_password), json.dumps(normalized_tabs, ensure_ascii=False), user_id),
            )
        else:
            conn.execute(
                '''
                UPDATE app_users
                SET username = ?, display_name = ?, visible_tabs = ?
                WHERE id = ?
                ''',
                (clean_username, clean_display_name, json.dumps(normalized_tabs, ensure_ascii=False), user_id),
            )
        updated_user = conn.execute('SELECT * FROM app_users WHERE id = ?', (user_id,)).fetchone()
        _write_audit_log(
            conn,
            'user',
            user_id,
            'user_update',
            (actor_name or 'النظام').strip() or 'النظام',
            f'تم تعديل المستخدم "{clean_display_name}"',
            'success',
            {'before': _serialize_row(existing), 'after': _serialize_row(updated_user)},
            client_ip=None,
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError('اسم المستخدم مسجل بالفعل') from exc
    finally:
        conn.close()


def delete_app_user(user_id, actor_name=None, db_path=DB_PATH):
    conn = connect(db_path)
    try:
        existing = conn.execute('SELECT * FROM app_users WHERE id = ?', (user_id,)).fetchone()
        if not existing:
            raise ValueError('المستخدم غير موجود')
        deleted = conn.execute('DELETE FROM app_users WHERE id = ?', (user_id,))
        if deleted.rowcount == 0:
            raise ValueError('المستخدم غير موجود')
        _write_audit_log(
            conn,
            'user',
            user_id,
            'user_delete',
            (actor_name or 'النظام').strip() or 'النظام',
            f'تم حذف المستخدم "{existing["display_name"]}"',
            'success',
            {'before': _serialize_row(existing)},
            client_ip=None,
        )
        conn.commit()
    finally:
        conn.close()


def authenticate_app_user(username, password, db_path=DB_PATH):
    clean_username = (username or '').strip()
    clean_password = (password or '').strip()
    if clean_username == LOGIN_USERNAME and clean_password == LOGIN_PASSWORD:
        return _admin_user_record()

    user = get_app_user_by_username(clean_username, db_path=db_path)
    if not user or user.get('is_admin'):
        return None
    if not int(user.get('is_active') or 0):
        return None
    if user['password_hash'] != _hash_user_password(clean_password):
        return None
    return user


def get_current_user(headers, db_path=DB_PATH):
    cookies = parse_cookie_header(headers.get('Cookie'))
    cookie_value = cookies.get(AUTH_COOKIE_NAME)
    username, token = _extract_auth_cookie_identity(cookie_value)
    if not username or not token:
        return None

    if username == LOGIN_USERNAME:
        expected = build_auth_token(LOGIN_USERNAME, LOGIN_PASSWORD)
        return _admin_user_record() if token == expected else None

    user = get_app_user_by_username(username, db_path=db_path)
    if not user or not int(user.get('is_active') or 0):
        return None
    expected = build_auth_token(user['username'], user['password_hash'])
    if token != expected:
        return None
    return user


def is_authenticated(headers, db_path=DB_PATH):
    return get_current_user(headers, db_path=db_path) is not None


def user_can_access_tab(user, tab_id):
    if not user:
        return False
    if user.get('is_admin'):
        return True
    return tab_id in set(normalize_visible_tabs(user.get('visible_tabs') or []))


def build_greeting_text(display_name, now=None):
    now = now or datetime.now()
    prefix = 'صباح الخير' if now.hour < 12 else 'مساء الخير'
    return f'{prefix} {display_name}'


def format_iqd(amount):
    value = int(round(float(amount or 0)))
    return f'{value:,} دينار'


def normalize_search_text(text):
    raw_value = str(text or '').strip().lower()
    if not raw_value:
        return ''

    decomposed = unicodedata.normalize('NFKD', raw_value)
    without_marks = ''.join(char for char in decomposed if unicodedata.category(char) != 'Mn')
    translated = without_marks.translate(ARABIC_SEARCH_TRANSLATION)
    translated = re.sub(r'[\W_]+', ' ', translated, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', translated).strip()


def _compact_search_text(text):
    return normalize_search_text(text).replace(' ', '')


def _score_item_match(query, item_name):
    normalized_query = normalize_search_text(query)
    normalized_name = normalize_search_text(item_name)
    if not normalized_query or not normalized_name:
        return 0.0

    compact_query = normalized_query.replace(' ', '')
    compact_name = normalized_name.replace(' ', '')
    query_tokens = normalized_query.split()
    name_tokens = normalized_name.split()

    if normalized_query == normalized_name or compact_query == compact_name:
        return 100.0

    scores = [
        fuzz.ratio(normalized_query, normalized_name),
        fuzz.ratio(compact_query, compact_name),
        fuzz.partial_ratio(normalized_query, normalized_name),
        fuzz.partial_ratio(compact_query, compact_name),
        fuzz.token_sort_ratio(normalized_query, normalized_name),
        fuzz.token_set_ratio(normalized_query, normalized_name),
    ]

    if normalized_query in normalized_name or compact_query in compact_name:
        scores.append(97.0)

    if query_tokens and all(token in name_tokens for token in query_tokens):
        scores.append(98.0)

    if len(query_tokens) > 1 and sorted(query_tokens) == sorted(name_tokens):
        scores.append(99.0)

    if len(query_tokens) > 1:
        matched_token_count = 0
        for query_token in query_tokens:
            token_matched = False
            for name_token in name_tokens:
                if fuzz.ratio(query_token, name_token) >= 70 or query_token in name_token or name_token in query_token:
                    token_matched = True
                    break
            if token_matched:
                matched_token_count += 1

        token_coverage = matched_token_count / len(query_tokens)
        if token_coverage < 1:
            scores = [score * (0.5 + (0.5 * token_coverage)) for score in scores]

    return max(scores)


def search_items(query, db_path=DB_PATH, limit=20, min_score=60):
    normalized_query = normalize_search_text(query)
    if not normalized_query:
        return []

    items = list_items(db_path=db_path)
    results = []
    for item in items:
        score = _score_item_match(normalized_query, item['name'])
        if score < min_score:
            continue
        results.append(
            {
                'id': item['id'],
                'name': item['name'],
                'stock': item['stock'],
                'sell_price': float(item['sell_price'] or item['price'] or 0),
                'buy_price': float(item['buy_price'] or 0),
                'score': round(float(score), 2),
                'normalized_name': normalize_search_text(item['name']),
            }
        )

    results.sort(key=lambda item: (-item['score'], item['name'], -int(item['id'])))
    return results[:limit]


def find_best_item_match(query, db_path=DB_PATH, min_score=60):
    matches = search_items(query, db_path=db_path, limit=1, min_score=min_score)
    return matches[0] if matches else None


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


def get_time_greeting(now=None):
    now = now or datetime.now()
    return 'صباح الخير حيدر' if now.hour < 12 else 'مساء الخير حيدر'


def build_auth_token(username, password):
    return hashlib.sha256(f'{username}\0{password}\0{AUTH_COOKIE_SECRET}'.encode('utf-8')).hexdigest()


def parse_cookie_header(cookie_header):
    cookies = {}
    for segment in (cookie_header or '').split(';'):
        if '=' not in segment:
            continue
        key, value = segment.split('=', 1)
        cookies[key.strip()] = value.strip()
    return cookies


def is_authenticated(headers, db_path=DB_PATH):
    return get_current_user(headers, db_path=db_path) is not None


def render_login_page(message=None, message_type='error'):
    message_html = ''
    if message:
        message_html = f'<div class="alert {message_type}">{html.escape(message)}</div>'

    greeting = get_time_greeting()
    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تسجيل الدخول - نظام الكاشير</title>
    <style>
        :root {{
            color-scheme: dark;
            --bg: radial-gradient(circle at top, #1e293b 0%, #0f172a 45%, #020617 100%);
            --card: rgba(15, 23, 42, 0.84);
            --line: rgba(148, 163, 184, 0.22);
            --accent: #38bdf8;
            --text: #e2e8f0;
            --muted: #94a3b8;
        }}
        body {{
            margin: 0;
            min-height: 100vh;
            font-family: Tahoma, sans-serif;
            background: var(--bg);
            color: var(--text);
            display: grid;
            place-items: center;
            padding: 24px;
            box-sizing: border-box;
        }}
        .login-card {{
            width: min(100%, 460px);
            background: var(--card);
            border: 1px solid var(--line);
            border-radius: 24px;
            padding: 28px;
            box-shadow: 0 30px 80px rgba(0, 0, 0, 0.45);
        }}
        .eyebrow {{
            margin: 0 0 10px;
            color: var(--accent);
            font-size: 0.92rem;
            letter-spacing: 0.02em;
        }}
        h1 {{
            margin: 0 0 8px;
            font-size: 1.9rem;
        }}
        .greeting {{
            margin: 0 0 20px;
            color: var(--muted);
        }}
        form {{
            display: grid;
            gap: 12px;
        }}
        input, button {{
            min-height: 48px;
            border-radius: 14px;
            border: 1px solid var(--line);
            box-sizing: border-box;
            font-size: 1rem;
        }}
        input {{
            background: rgba(2, 6, 23, 0.72);
            color: var(--text);
            padding: 0 14px;
        }}
        button {{
            background: linear-gradient(135deg, #0284c7, #38bdf8);
            color: white;
            border: none;
            cursor: pointer;
            font-weight: 700;
        }}
        .alert {{
            margin-bottom: 14px;
            padding: 12px 14px;
            border-radius: 14px;
            background: rgba(239, 68, 68, 0.14);
            color: #fecaca;
            border: 1px solid rgba(239, 68, 68, 0.24);
        }}
        .hint {{
            margin-top: 14px;
            color: var(--muted);
            font-size: 0.9rem;
            line-height: 1.7;
        }}
    </style>
</head>
<body>
    <section class="login-card">
        <p class="eyebrow">{greeting}</p>
        <h1>تسجيل الدخول</h1>
        <p class="greeting">أدخل اسم المستخدم وكلمة المرور للمتابعة إلى النظام.</p>
        {message_html}
        <form action="/login" method="post">
            <input type="text" name="username" placeholder="اسم المستخدم" required autocomplete="username">
            <input type="password" name="password" placeholder="كلمة المرور" required autocomplete="current-password">
            <button type="submit">دخول</button>
        </form>
        <p class="hint">حساب الأدمن الثابت: حيدر | كلمة المرور: 1</p>
    </section>
</body>
</html>'''


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


def _serialize_receipt_lines_snapshot(conn, receipt_id):
    rows = conn.execute(
        '''
        SELECT s.item_id,
               s.quantity,
               s.buy_total,
               s.sell_total,
               COALESCE(i.name, '') AS item_name
        FROM sales s
        LEFT JOIN items i ON i.id = s.item_id
        WHERE s.receipt_id = ?
        ORDER BY s.id ASC
        ''',
        (receipt_id,),
    ).fetchall()
    serialized = []
    for row in rows:
        qty = int(row['quantity'] or 0)
        sell_total = float(row['sell_total'] or 0)
        buy_total = float(row['buy_total'] or 0)
        unit_price = (sell_total / qty) if qty > 0 else 0.0
        serialized.append(
            {
                'item_id': row['item_id'],
                'item_name': row['item_name'] or f'صنف #{row["item_id"]}',
                'quantity': qty,
                'unit_price': unit_price,
                'sell_total': sell_total,
                'buy_total': buy_total,
            }
        )
    return serialized


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


def build_audit_payload(db_path=DB_PATH, limit=25):
    logs = list_audit_logs(db_path=db_path, limit=limit)
    latest_id = int(logs[0]['id']) if logs else 0
    return {
        'latest_id': latest_id,
        'rows_html': _render_audit_rows(logs),
    }


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


def list_suppliers(db_path=DB_PATH):
    conn = connect(db_path)
    suppliers = conn.execute(
        '''
        SELECT
            s.id,
            s.name,
            s.phone,
            s.email,
            s.address,
            s.created_at,
            COALESCE((SELECT COUNT(*) FROM supplier_purchases p WHERE p.supplier_id = s.id), 0) AS purchase_count,
            COALESCE((SELECT SUM(p.total_amount) FROM supplier_purchases p WHERE p.supplier_id = s.id), 0) AS total_purchases_amount,
            COALESCE((SELECT SUM(p.paid_amount) FROM supplier_purchases p WHERE p.supplier_id = s.id), 0) AS total_paid_amount,
            COALESCE((SELECT SUM(p.debt_amount) FROM supplier_purchases p WHERE p.supplier_id = s.id), 0) AS open_debt_amount
        FROM supplier_companies s
        ORDER BY s.id DESC
        '''
    ).fetchall()
    conn.close()
    return suppliers


def get_supplier(supplier_id, db_path=DB_PATH):
    conn = connect(db_path)
    supplier = conn.execute(
        '''
        SELECT id, name, phone, email, address, created_at
        FROM supplier_companies
        WHERE id = ?
        ''',
        (supplier_id,),
    ).fetchone()
    conn.close()
    return supplier


def add_supplier(name, phone=None, email=None, address=None, db_path=DB_PATH):
    name = (name or '').strip()
    phone = (phone or '').strip()
    if not name:
        raise ValueError('يرجى إدخال اسم الشركة')

    conn = connect(db_path)
    try:
        cursor = conn.execute(
            'INSERT INTO supplier_companies (name, phone, email, address) VALUES (?, ?, ?, ?)',
            (name, phone or None, email or None, address or None),
        )
        supplier_id = cursor.lastrowid
        _write_audit_log(
            conn,
            'supplier',
            supplier_id,
            'supplier_add',
            'النظام',
            'إضافة شركة جديدة',
            'success',
            {'after': {'id': supplier_id, 'name': name, 'phone': phone or None, 'email': email or None, 'address': address or None}},
            client_ip=None,
        )
        conn.commit()
        return supplier_id
    except sqlite3.IntegrityError as exc:
        raise ValueError('اسم الشركة مسجل بالفعل') from exc
    finally:
        conn.close()


def update_supplier(supplier_id, name, phone=None, email=None, address=None, actor_name=None, actor_role='مستخدم', db_path=DB_PATH):
    name = (name or '').strip()
    phone = (phone or '').strip()
    if not name:
        raise ValueError('يرجى إدخال اسم الشركة')

    conn = connect(db_path)
    try:
        before = conn.execute('SELECT * FROM supplier_companies WHERE id = ?', (supplier_id,)).fetchone()
        if not before:
            raise ValueError('الشركة غير موجودة')
        conn.execute(
            'UPDATE supplier_companies SET name=?, phone=?, email=?, address=? WHERE id=?',
            (name, phone or None, email or None, address or None, supplier_id),
        )
        after = conn.execute('SELECT * FROM supplier_companies WHERE id = ?', (supplier_id,)).fetchone()
        clean_actor_name = (actor_name or '').strip() or 'النظام'
        clean_actor_role = (actor_role or '').strip()
        if clean_actor_role not in {'أدمن', 'مستخدم'}:
            clean_actor_role = 'مستخدم'

        def _show_value(value):
            text = (value or '').strip()
            return text if text else 'فارغ'

        changed_parts = []
        field_labels = [
            ('name', 'اسم الشركة'),
            ('phone', 'الهاتف'),
            ('email', 'البريد الإلكتروني'),
            ('address', 'العنوان'),
        ]
        for key, label in field_labels:
            old_value = _show_value(before[key])
            new_value = _show_value(after[key] if after else None)
            if old_value != new_value:
                changed_parts.append(f'{label}: "{old_value}" إلى "{new_value}"')

        if changed_parts:
            reason_text = 'تم تحديث بيانات الشركة: ' + ' | '.join(changed_parts)
        else:
            reason_text = 'تم فتح تحديث الشركة بدون أي تغيير'
        _write_audit_log(
            conn,
            'supplier',
            supplier_id,
            'supplier_update',
            clean_actor_name,
            reason_text,
            'success',
            {'before': _serialize_row(before), 'after': _serialize_row(after)},
            client_ip=None,
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ValueError('اسم الشركة مسجل بالفعل') from exc
    finally:
        conn.close()


def delete_supplier(supplier_id, actor_name=None, actor_role='مستخدم', db_path=DB_PATH):
    conn = connect(db_path)
    try:
        before = conn.execute('SELECT * FROM supplier_companies WHERE id = ?', (supplier_id,)).fetchone()
        if not before:
            raise ValueError('الشركة غير موجودة')
        conn.execute('DELETE FROM supplier_companies WHERE id=?', (supplier_id,))
        clean_actor_name = (actor_name or '').strip() or 'النظام'
        reason_text = f'تم حذف الشركة "{before["name"]}"'
        _write_audit_log(
            conn,
            'supplier',
            supplier_id,
            'supplier_delete',
            clean_actor_name,
            reason_text,
            'success',
            {'before': _serialize_row(before)},
            client_ip=None,
        )
        conn.commit()
    finally:
        conn.close()


def get_supplier_purchases(supplier_id, db_path=DB_PATH):
    conn = connect(db_path)
    purchases = conn.execute(
        '''
        SELECT
            p.id,
            p.supplier_id,
            p.supplier_name,
            p.payment_method,
            p.total_amount,
            p.paid_amount,
            p.debt_amount,
            p.note,
            p.created_at,
            COALESCE((
                SELECT GROUP_CONCAT(
                    COALESCE(l.item_name, i.name) || ' × ' || l.quantity || ' = ' || l.total_cost,
                    ' | '
                )
                FROM supplier_purchase_lines l
                LEFT JOIN items i ON i.id = l.item_id
                WHERE l.purchase_id = p.id
            ), '') AS items_summary
        FROM supplier_purchases p
        WHERE p.supplier_id = ?
        ORDER BY p.id DESC
        ''',
        (supplier_id,),
    ).fetchall()
    conn.close()
    return purchases


def get_supplier_debt_transactions(supplier_id, db_path=DB_PATH):
    conn = connect(db_path)
    transactions = conn.execute(
        '''
        SELECT t.id, t.purchase_id, t.transaction_type, t.amount, t.note, t.created_at,
               p.total_amount, p.paid_amount, p.debt_amount
        FROM supplier_debt_transactions t
        LEFT JOIN supplier_purchases p ON p.id = t.purchase_id
        WHERE t.supplier_id = ?
        ORDER BY t.id ASC
        ''',
        (supplier_id,),
    ).fetchall()
    conn.close()
    return transactions


def _resolve_supplier_for_purchase(conn, supplier_id=None, supplier_name=None):
    clean_supplier_name = (supplier_name or '').strip()

    if supplier_id:
        resolved_supplier_id = int(supplier_id)
        supplier = conn.execute('SELECT id, name FROM supplier_companies WHERE id = ?', (resolved_supplier_id,)).fetchone()
        if supplier:
            return supplier['id'], supplier['name']

    if clean_supplier_name:
        matched_supplier = conn.execute(
            'SELECT id, name FROM supplier_companies WHERE LOWER(name) = LOWER(?) ORDER BY id DESC LIMIT 1',
            (clean_supplier_name,),
        ).fetchone()
        if matched_supplier:
            return matched_supplier['id'], matched_supplier['name']

    return None, clean_supplier_name


def _create_auto_supplier_company(conn, supplier_name):
    clean_name = (supplier_name or '').strip()
    if not clean_name:
        raise ValueError('يرجى إدخال اسم الشركة')

    existing = conn.execute(
        'SELECT id, name FROM supplier_companies WHERE LOWER(name) = LOWER(?) ORDER BY id DESC LIMIT 1',
        (clean_name,),
    ).fetchone()
    if existing:
        return existing['id'], existing['name']

    cursor = conn.execute(
        '''
        INSERT INTO supplier_companies (name, phone, email, address)
        VALUES (?, ?, ?, ?)
        ''',
        (clean_name, None, None, 'أضيفت تلقائيًا من سجل الشراء'),
    )
    return cursor.lastrowid, clean_name


def _record_supplier_debt_transaction(conn, supplier_id, amount, transaction_type, purchase_id=None, note=None):
    conn.execute(
        '''
        INSERT INTO supplier_debt_transactions (supplier_id, purchase_id, transaction_type, amount, note)
        VALUES (?, ?, ?, ?, ?)
        ''',
        (supplier_id, purchase_id, transaction_type, float(amount), note or None),
    )


def create_supplier_purchase(
    lines,
    supplier_id=None,
    supplier_name=None,
    payment_method='نقدي',
    paid_amount=None,
    note=None,
    apply_stock=True,
    actor_name='النظام',
    reason='تسجيل شراء من الشركة',
    client_ip=None,
    db_path=DB_PATH,
):
    if not lines:
        raise ValueError('يرجى إدخال صنف واحد على الأقل')

    normalized_lines = []
    conn = connect(db_path)
    try:
        for line in lines:
            try:
                quantity = int(line.get('quantity') or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError('بيانات الشراء غير صحيحة') from exc

            raw_item_id = str(line.get('item_id') or '').strip()
            item_name = (line.get('item_name') or '').strip()
            raw_unit_cost = str(line.get('unit_cost') or '').strip()

            try:
                item_id = int(raw_item_id) if raw_item_id else 0
            except (TypeError, ValueError):
                item_id = 0

            if item_id <= 0 and item_name:
                matched_item = find_best_item_match(item_name, db_path=db_path)
                if matched_item:
                    item_id = int(matched_item['id'])

            if item_id <= 0 or quantity <= 0:
                raise ValueError('كل سطر شراء يجب أن يحتوي على منتج وكمية صحيحة')

            if not raw_unit_cost:
                raise ValueError('يرجى إدخال سعر الشراء لكل صنف')

            try:
                unit_cost = float(raw_unit_cost)
            except (TypeError, ValueError) as exc:
                raise ValueError('سعر الشراء غير صحيح') from exc

            if unit_cost <= 0:
                raise ValueError('سعر الشراء يجب أن يكون أكبر من صفر')

            normalized_lines.append({'item_id': item_id, 'quantity': quantity, 'unit_cost': unit_cost})

        clean_supplier_name = (supplier_name or '').strip()
        clean_payment_method = (payment_method or 'نقدي').strip() or 'نقدي'
        clean_note = (note or '').strip()
        resolved_supplier_id, resolved_supplier_name = _resolve_supplier_for_purchase(
            conn,
            supplier_id=supplier_id,
            supplier_name=clean_supplier_name,
        )
        if not resolved_supplier_id and resolved_supplier_name:
            resolved_supplier_id, resolved_supplier_name = _create_auto_supplier_company(conn, resolved_supplier_name)

        total_amount = 0.0
        prepared_lines = []
        for line in normalized_lines:
            item = conn.execute('SELECT * FROM items WHERE id = ?', (line['item_id'],)).fetchone()
            if not item:
                raise ValueError('أحد الأصناف غير موجود')

            line_total = line['unit_cost'] * line['quantity']
            total_amount += line_total
            prepared_lines.append(
                {
                    'item_id': item['id'],
                    'item_name': item['name'],
                    'quantity': line['quantity'],
                    'unit_cost': line['unit_cost'],
                    'total_cost': line_total,
                }
            )

        if clean_payment_method == 'دين':
            if not (resolved_supplier_name or '').strip():
                raise ValueError('يرجى إدخال اسم الشركة')
            if (paid_amount or '').strip() == '':
                paid_value = 0.0
            else:
                try:
                    paid_value = float(paid_amount)
                except (TypeError, ValueError) as exc:
                    raise ValueError('مبلغ التسديد غير صحيح') from exc
                if paid_value < 0:
                    raise ValueError('مبلغ التسديد لا يمكن أن يكون سالبًا')
                if paid_value > total_amount:
                    raise ValueError('مبلغ التسديد أكبر من إجمالي الفاتورة')
        else:
            paid_value = total_amount

        debt_amount = total_amount - paid_value

        if debt_amount > 0:
            resolved_supplier_id, resolved_supplier_name = _create_auto_supplier_company(conn, resolved_supplier_name)

        cursor = conn.execute(
            '''
            INSERT INTO supplier_purchases (
                supplier_id, supplier_name, payment_method,
                total_amount, paid_amount, debt_amount, note
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                resolved_supplier_id,
                resolved_supplier_name or None,
                clean_payment_method,
                total_amount,
                paid_value,
                debt_amount,
                clean_note or None,
            ),
        )
        purchase_id = cursor.lastrowid

        if debt_amount > 0 and resolved_supplier_id:
            _record_supplier_debt_transaction(
                conn,
                resolved_supplier_id,
                debt_amount,
                'charge',
                purchase_id=purchase_id,
                note='إضافة دين من فاتورة شراء',
            )

        for line in prepared_lines:
            if apply_stock:
                conn.execute(
                    'UPDATE items SET stock = stock + ?, buy_price = ?, price = ? WHERE id = ?',
                    (line['quantity'], line['unit_cost'], line['unit_cost'], line['item_id']),
                )
            else:
                conn.execute(
                    'UPDATE items SET buy_price = ?, price = ? WHERE id = ?',
                    (line['unit_cost'], line['unit_cost'], line['item_id']),
                )
            conn.execute(
                '''
                INSERT INTO supplier_purchase_lines (purchase_id, item_id, item_name, quantity, unit_cost, total_cost)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    purchase_id,
                    line['item_id'],
                    line['item_name'],
                    line['quantity'],
                    line['unit_cost'],
                    line['total_cost'],
                ),
            )

        _write_audit_log(
            conn,
            'supplier',
            resolved_supplier_id,
            'supplier_purchase',
            actor_name,
            reason,
            'success',
            {
                'purchase_id': purchase_id,
                'supplier_name': resolved_supplier_name,
                'payment_method': clean_payment_method,
                'total_amount': total_amount,
                'paid_amount': paid_value,
                'debt_amount': debt_amount,
                'line_count': len(prepared_lines),
            },
            client_ip=client_ip,
        )

        conn.commit()
        return purchase_id, total_amount, resolved_supplier_id
    finally:
        conn.close()


def pay_supplier_debt(supplier_id, amount, note=None, actor_name='النظام', reason='تسجيل تسديد للشركة', client_ip=None, db_path=DB_PATH):
    try:
        payment_amount = float(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError('مبلغ التسديد غير صحيح') from exc

    if payment_amount <= 0:
        raise ValueError('مبلغ التسديد يجب أن يكون أكبر من صفر')

    conn = connect(db_path)
    try:
        supplier = conn.execute('SELECT id, name FROM supplier_companies WHERE id = ?', (supplier_id,)).fetchone()
        if not supplier:
            raise ValueError('الشركة غير موجودة')

        open_purchases = conn.execute(
            '''
            SELECT id, debt_amount
            FROM supplier_purchases
            WHERE supplier_id = ? AND debt_amount > 0
            ORDER BY id ASC
            ''',
            (supplier_id,),
        ).fetchall()

        if not open_purchases:
            raise ValueError('لا يوجد دين مفتوح لهذه الشركة')

        total_open = sum(float(row['debt_amount']) for row in open_purchases)
        if payment_amount > total_open:
            raise ValueError('مبلغ التسديد أكبر من المتبقي على الشركة')

        remaining = payment_amount
        for purchase in open_purchases:
            if remaining <= 0:
                break

            current_debt = float(purchase['debt_amount'])
            applied = current_debt if current_debt <= remaining else remaining

            conn.execute(
                '''
                UPDATE supplier_purchases
                SET paid_amount = paid_amount + ?,
                    debt_amount = debt_amount - ?
                WHERE id = ?
                ''',
                (applied, applied, purchase['id']),
            )
            remaining -= applied

        _record_supplier_debt_transaction(
            conn,
            supplier_id,
            payment_amount,
            'payment',
            purchase_id=None,
            note=(note or '').strip() or 'تسديد من الذمة',
        )

        _write_audit_log(
            conn,
            'supplier',
            supplier_id,
            'supplier_payment',
            actor_name,
            reason,
            'success',
            {
                'payment_amount': payment_amount,
                'remaining_open_debt': total_open - payment_amount,
                'note': (note or '').strip() or 'تسديد من الذمة',
            },
            client_ip=client_ip,
        )

        updated_open = conn.execute(
            'SELECT COALESCE(SUM(debt_amount), 0) AS open_debt FROM supplier_purchases WHERE supplier_id = ?',
            (supplier_id,),
        ).fetchone()

        conn.commit()
        return float(updated_open['open_debt'] or 0)
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


def add_item(name, buy_price, sell_price=None, stock=None, actor_name='النظام', reason='إضافة منتج جديد', client_ip=None, db_path=DB_PATH):
    name = (name or '').strip()
    if not name:
        raise ValueError('يرجى إدخال اسم المنتج')

    buy_value, sell_value, stock_value = _normalize_item_values(buy_price, sell_price, stock)
    conn = connect(db_path)
    try:
        cursor = conn.execute(
            'INSERT INTO items (name, buy_price, sell_price, price, stock) VALUES (?, ?, ?, ?, ?)',
            (name, buy_value, sell_value, sell_value, stock_value),
        )
        item_id = cursor.lastrowid
        created_item = conn.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        _write_audit_log(
            conn,
            'item',
            item_id,
            'item_add',
            (actor_name or 'النظام').strip() or 'النظام',
            reason,
            'success',
            {'after': _serialize_row(created_item)},
            client_ip=client_ip,
        )
        conn.commit()
        return item_id
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

        item_name = (current_item['name'] or '').strip() or f'#{item_id}'

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
                f'{(reason or "بدون سبب").strip() or "بدون سبب"} | المنتج: "{item_name}"',
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
            f'{clean_reason} | المنتج: "{item_name}"',
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
    actor_name='النظام',
    reason='إنشاء وصل بيع',
    client_ip=None,
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
            matched_item = find_best_item_match(item_name, db_path=db_path)
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
    clean_customer_name = (customer_name or '').strip()
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
        created_receipt = conn.execute('SELECT * FROM sale_receipts WHERE id = ?', (receipt_id,)).fetchone()

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

        receipt_number = f"{int(receipt_id):04d}"
        _write_audit_log(
            conn,
            'receipt',
            receipt_id,
            'receipt_add',
            (actor_name or 'النظام').strip() or 'النظام',
            f'{reason} | تم إنشاء الوصل رقم {receipt_number}',
            'success',
            {
                'after': _serialize_row(created_receipt),
                'receipt_number': receipt_number,
                'customer_name': resolved_customer_name or clean_customer_name or 'بدون اسم زبون',
                'line_count': len(prepared_lines),
                'total_amount': total_amount,
            },
            client_ip=client_ip,
        )

        conn.commit()
        return receipt_id, total_amount
    finally:
        conn.close()


def update_sale_receipt(
    receipt_id,
    lines,
    customer_name=None,
    company_name=None,
    payment_method='نقدي',
    received_amount=None,
    actor_name='النظام',
    reason='تعديل الوصل',
    client_ip=None,
    db_path=DB_PATH,
):
    if not lines:
        raise ValueError('يرجى إدخال منتج واحد على الأقل')

    normalized_lines = []
    conn = connect(db_path)
    try:
        receipt = conn.execute('SELECT * FROM sale_receipts WHERE id = ?', (receipt_id,)).fetchone()
        if not receipt:
            raise ValueError('الوصل غير موجود')
        before_receipt = _serialize_row(receipt)
        before_lines_snapshot = _serialize_receipt_lines_snapshot(conn, receipt_id)
        receipt_number = f"{int(receipt_id):04d}"

        for line in lines:
            try:
                item_id = int(line.get('item_id') or 0)
                quantity = int(line.get('quantity') or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError('بيانات التعديل غير صحيحة') from exc

            if item_id <= 0 or quantity <= 0:
                raise ValueError('كل سطر يجب أن يحتوي على منتج وكمية صحيحة')

            raw_unit_price = str(line.get('unit_price') or '').strip()
            unit_price = None
            if raw_unit_price:
                try:
                    unit_price = float(raw_unit_price)
                except (TypeError, ValueError) as exc:
                    raise ValueError('سعر الوحدة غير صحيح') from exc
                if unit_price <= 0:
                    raise ValueError('سعر الوحدة يجب أن يكون أكبر من صفر')

            normalized_lines.append({'item_id': item_id, 'quantity': quantity, 'unit_price': unit_price})

        old_lines = conn.execute('SELECT item_id, quantity FROM sales WHERE receipt_id = ?', (receipt_id,)).fetchall()
        for old_line in old_lines:
            conn.execute('UPDATE items SET stock = stock + ? WHERE id = ?', (old_line['quantity'], old_line['item_id']))

        conn.execute('DELETE FROM sales WHERE receipt_id = ?', (receipt_id,))
        conn.execute('DELETE FROM customer_debt_transactions WHERE receipt_id = ?', (receipt_id,))

        clean_company_name = (company_name or '').strip()
        clean_payment_method = (payment_method or 'نقدي').strip() or 'نقدي'
        clean_customer_name = (customer_name or receipt['customer_name'] or '').strip()

        # In edit mode, the typed customer name is the source of truth.
        # Do not force-link to the previous receipt customer_id.
        resolved_customer_id, resolved_customer_name = _resolve_customer_for_sale(
            conn,
            customer_id=None,
            customer_name=clean_customer_name,
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

        conn.execute(
            '''
            UPDATE sale_receipts
            SET customer_id = ?,
                customer_name = ?,
                company_name = ?,
                payment_method = ?,
                total_amount = ?,
                paid_amount = ?,
                debt_amount = ?,
                total_profit = ?
            WHERE id = ?
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
                receipt_id,
            ),
        )
        updated_receipt = conn.execute('SELECT * FROM sale_receipts WHERE id = ?', (receipt_id,)).fetchone()

        if debt_amount > 0 and resolved_customer_id:
            _record_debt_transaction(
                conn,
                resolved_customer_id,
                debt_amount,
                'charge',
                receipt_id=receipt_id,
                note='تحديث دين من فاتورة بيع',
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

        after_lines_snapshot = _serialize_receipt_lines_snapshot(conn, receipt_id)

        customer_label = (resolved_customer_name or clean_customer_name or receipt['customer_name'] or 'بدون اسم زبون').strip()
        _write_audit_log(
            conn,
            'receipt',
            receipt_id,
            'update_receipt',
            (actor_name or 'النظام').strip() or 'النظام',
            f'{reason} | تم تعديل الوصل رقم {receipt_number} للزبون "{customer_label}"',
            'success',
            {
                'before': before_receipt,
                'after': _serialize_row(updated_receipt),
                'before_lines': before_lines_snapshot,
                'after_lines': after_lines_snapshot,
                'receipt_number': receipt_number,
                'customer_name': customer_label,
                'line_count': len(prepared_lines),
            },
            client_ip=client_ip,
        )

        conn.commit()
        return total_amount
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


def pay_customer_debt(customer_id, amount, note=None, actor_name='النظام', reason='تسديد ذمة زبون', client_ip=None, db_path=DB_PATH):
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

        _write_audit_log(
            conn,
            'customer',
            customer_id,
            'customer_payment',
            (actor_name or 'النظام').strip() or 'النظام',
            reason,
            'success',
            {
                'customer_name': customer['name'],
                'payment_amount': payment_amount,
                'remaining_open_debt': float(updated_open['open_debt'] or 0),
                'note': (note or '').strip() or None,
            },
            client_ip=client_ip,
        )

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
            <article class="item-card" data-item-id="{item['id']}">
                <div class="item-head">
                    <h3>{html.escape(item['name'])}</h3>
                    <span class="stock-pill {stock_class}">{item['stock']} متوفر</span>
                </div>
                <p class="price">بيع: {format_iqd(item['sell_price'])}</p>
                <p class="buy-price">شراء: {format_iqd(item['buy_price'])}</p>
                <form action="/items/{item['id']}/update" method="post" class="edit-form product-action-form" onsubmit="return syncProductSecurityFields(this)">
                    <input type="text" name="name" value="{html.escape(item['name'])}" required>
                    <input type="number" step="0.01" name="buy_price" value="{item['buy_price']}" required>
                    <input type="number" step="0.01" name="sell_price" value="{item['sell_price']}" required>
                    <input type="number" name="stock" value="{item['stock']}" required>
                    <input type="hidden" name="actor_name">
                    <input type="hidden" name="reason">
                    <input type="hidden" name="admin_pin">
                    <button type="submit">حفظ التعديل</button>
                </form>
                <form action="/items/delete" method="post" class="edit-form product-action-form" onsubmit="return syncProductSecurityFields(this)">
                    <input type="hidden" name="item_id" value="{item['id']}">
                    <input type="hidden" name="actor_name">
                    <input type="hidden" name="reason">
                    <input type="hidden" name="admin_pin">
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
                    <a class="print-link edit-link" href="/receipts/{receipt['id']}/edit">تعديل الوصل</a>
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


def _render_supplier_cards(suppliers):
    cards = []
    for supplier in suppliers:
        total_purchases_amount = float(supplier['total_purchases_amount'] or 0)
        total_paid_amount = float(supplier['total_paid_amount'] or 0)
        open_debt_amount = float(supplier['open_debt_amount'] or 0)
        purchase_count = int(supplier['purchase_count'] or 0)
        email_html = f'<p class="email">{html.escape(supplier["email"])}' + '</p>' if supplier['email'] else ''
        address_html = f'<p class="address">{html.escape(supplier["address"])}' + '</p>' if supplier['address'] else ''
        debt_badge = (
            f'<span class="stock-pill low">المتبقي علينا: {format_iqd(open_debt_amount)}</span>'
            if open_debt_amount > 0
            else '<span class="stock-pill">لا يوجد متبقٍ</span>'
        )
        cards.append(
            f'''
            <article class="customer-card supplier-card">
                <div class="customer-head">
                    <h3>{html.escape(supplier['name'])}</h3>
                    {debt_badge}
                </div>
                {email_html}
                {address_html}
                <p class="address">عدد المشتريات: {purchase_count} | إجمالي المشتريات: {format_iqd(total_purchases_amount)}</p>
                <p class="address">إجمالي المسدد: {format_iqd(total_paid_amount)}</p>
                <div class="card-actions">
                    <form action="/suppliers/{supplier['id']}/details" method="get">
                        <input type="hidden" name="view" value="purchases">
                        <button type="submit" class="secondary">سجل الشركة</button>
                    </form>
                    <form action="/suppliers/{supplier['id']}/details" method="get">
                        <input type="hidden" name="view" value="debts">
                        <button type="submit" class="secondary">الذمم</button>
                    </form>
                    <form action="/suppliers/{supplier['id']}/delete" method="post">
                        <button type="submit" class="danger">حذف</button>
                    </form>
                </div>
            </article>
            '''
        )
    return ''.join(cards)


def _render_user_permission_inputs(selected_tabs=None, input_name='visible_tabs'):
    selected_ids = set(normalize_visible_tabs(selected_tabs))
    options = []
    for tab in get_assignable_tabs():
        checked = ' checked' if tab['id'] in selected_ids else ''
        options.append(
            f'''<label class="permission-option"><input type="checkbox" name="{html.escape(input_name)}" value="{html.escape(tab['id'])}"{checked}> <span>{html.escape(tab['label'])}</span></label>'''
        )
    return ''.join(options)


def _render_user_cards(users):
    cards = []
    for user in users:
        tabs_preview = '، '.join(
            tab['label'] for tab in get_assignable_tabs() if tab['id'] in set(user.get('visible_tabs') or [])
        ) or 'بدون صلاحيات'
        cards.append(
            f'''
            <article class="customer-card user-card">
                <div class="customer-head">
                    <h3>{html.escape(user['display_name'])}</h3>
                    <p class="mini-note">اسم الدخول: {html.escape(user['username'])}</p>
                </div>
                <p class="address">الواجهات الظاهرة: {html.escape(tabs_preview)}</p>
                <form action="/users/{user['id']}/update" method="post" class="edit-form users-form">
                    <input type="text" name="display_name" value="{html.escape(user['display_name'])}" placeholder="اسم العرض" required>
                    <input type="text" name="username" value="{html.escape(user['username'])}" placeholder="اسم المستخدم" required>
                    <input type="password" name="password" placeholder="كلمة مرور جديدة (اختياري)">
                    <div class="permissions-grid">{_render_user_permission_inputs(user.get('visible_tabs') or [])}</div>
                    <button type="submit">حفظ المستخدم</button>
                </form>
                <form action="/users/{user['id']}/delete" method="post" onsubmit="return confirm('سيتم حذف المستخدم نهائيًا. هل أنت متأكد؟');">
                    <button type="submit" class="danger">حذف المستخدم</button>
                </form>
            </article>
            '''
        )
    return ''.join(cards) or '<p class="mini-note">لا يوجد مستخدمون مضافون بعد.</p>'


def _render_debt_suppliers(suppliers):
    debt_suppliers = [row for row in suppliers if float(row['open_debt_amount'] or 0) > 0]
    if not debt_suppliers:
        return '<li class="sale-row">لا توجد ديون مفتوحة للشركات حاليًا</li>'

    rows = []
    for supplier in debt_suppliers:
        open_debt = float(supplier['open_debt_amount'] or 0)
        rows.append(
            f'''
            <li class="sale-row receipt-row debt-row">
                <div>
                    <strong>{html.escape(supplier['name'])}</strong>
                    <p>المتبقي علينا: {format_iqd_with_words(open_debt)}</p>
                </div>
                <div class="receipt-actions">
                    <a class="print-link" href="/suppliers/{supplier['id']}/details?view=debts">فتح سجل الشركة</a>
                </div>
            </li>
            '''
        )
    return ''.join(rows)


def _audit_field_label(field_name):
    labels = {
        'name': 'الاسم',
        'display_name': 'اسم العرض',
        'username': 'اسم المستخدم',
        'phone': 'الهاتف',
        'email': 'البريد الإلكتروني',
        'address': 'العنوان',
        'buy_price': 'سعر الشراء',
        'sell_price': 'سعر البيع',
        'price': 'السعر',
        'stock': 'الكمية',
        'visible_tabs': 'الواجهات',
        'customer_name': 'اسم الزبون',
        'company_name': 'الجهة',
        'payment_method': 'طريقة الدفع',
        'total_amount': 'إجمالي الوصل',
        'paid_amount': 'المستلم',
        'debt_amount': 'المتبقي',
        'total_profit': 'الربح',
    }
    return labels.get(field_name, field_name)


def _format_audit_field_value(field_name, value):
    if field_name == 'visible_tabs':
        parsed_tabs = value
        if isinstance(value, str):
            try:
                parsed_tabs = json.loads(value)
            except Exception:
                parsed_tabs = []
        tab_labels = []
        if isinstance(parsed_tabs, list):
            tab_map = {tab['id']: tab['label'] for tab in get_assignable_tabs()}
            for tab_id in parsed_tabs:
                label = tab_map.get((tab_id or '').strip())
                if label:
                    tab_labels.append(label)
        return '، '.join(tab_labels) if tab_labels else 'بدون واجهات'

    if field_name in {'buy_price', 'sell_price', 'price', 'total_amount', 'paid_amount', 'debt_amount', 'total_profit'}:
        try:
            return format_iqd(value)
        except Exception:
            return str(value)

    if value is None:
        return 'فارغ'
    if isinstance(value, str):
        clean_value = value.strip()
        return clean_value if clean_value else 'فارغ'
    return str(value)


def _build_audit_change_html(details):
    if not isinstance(details, dict):
        return ''
    before_payload = details.get('before') if isinstance(details.get('before'), dict) else None
    after_payload = details.get('after') if isinstance(details.get('after'), dict) else None
    if not before_payload or not after_payload:
        return ''

    ignored_fields = {'id', 'created_at', 'password_hash'}
    change_lines = []
    field_names = []
    for key in before_payload.keys():
        if key not in field_names:
            field_names.append(key)
    for key in after_payload.keys():
        if key not in field_names:
            field_names.append(key)

    for field_name in field_names:
        if field_name in ignored_fields:
            continue
        before_value = _format_audit_field_value(field_name, before_payload.get(field_name))
        after_value = _format_audit_field_value(field_name, after_payload.get(field_name))
        if before_value == after_value:
            continue
        change_lines.append(
            f'<div class="audit-diff-line"><strong>{html.escape(_audit_field_label(field_name))}:</strong> {html.escape(before_value)} &larr; {html.escape(after_value)}</div>'
        )

    return ''.join(change_lines)


def _render_audit_rows(audit_logs):
    if not audit_logs:
        return '<tr><td colspan="8">لا توجد عمليات رقابية مسجلة بعد.</td></tr>'

    rows = []
    for log in audit_logs:
        action_labels = {
            'item_add': 'إضافة منتج',
            'update': 'تعديل منتج',
            'delete': 'حذف منتج',
            'receipt_add': 'إضافة وصل',
            'update_receipt': 'تعديل وصل',
            'update_pin': 'تغيير رمز الأمان',
            'customer_payment': 'تسديد زبون',
            'delete_receipt': 'حذف وصل',
            'delete_all_receipts': 'حذف كل الوصولات',
            'reset_totals': 'تصفير إجمالي المبيعات والربح',
            'supplier_add': 'إضافة شركة',
            'supplier_update': 'تعديل شركة',
            'supplier_delete': 'حذف شركة',
            'supplier_purchase': 'تسجيل شراء شركة',
            'supplier_payment': 'تسديد شركة',
            'user_add': 'إضافة مستخدم',
            'user_update': 'تعديل مستخدم',
            'user_delete': 'حذف مستخدم',
        }
        action_label = action_labels.get(log['action'], log['action'])
        status_label = 'ناجحة' if log['status'] == 'success' else 'مرفوضة'
        status_class = 'ok' if log['status'] == 'success' else 'warn'

        product_name = '-'
        details = {}
        try:
            details = json.loads(log['details'] or '{}')
            if isinstance(details, dict):
                before_payload = details.get('before') if isinstance(details.get('before'), dict) else {}
                after_payload = details.get('after') if isinstance(details.get('after'), dict) else {}
                before_name = before_payload.get('name') or before_payload.get('display_name') or before_payload.get('username')
                after_name = after_payload.get('name') or after_payload.get('display_name') or after_payload.get('username')
                supplier_name = details.get('supplier_name')
                receipt_number = details.get('receipt_number')
                customer_name = details.get('customer_name')
                if receipt_number:
                    product_name = f'وصل {receipt_number} - {customer_name or "-"}'
                else:
                    product_name = before_name or after_name or supplier_name or '-'
        except Exception:
            product_name = '-'
            details = {}

        details_button_html = '<span class="audit-details-placeholder">-</span>'
        if isinstance(details, dict) and details:
            details_json = html.escape(json.dumps(details, ensure_ascii=False), quote=True)
            details_button_html = (
                f'<button type="button" class="audit-details-btn" data-audit-details="{details_json}" '
                'onclick="openAuditDetailsFromButton(this)" title="عرض تفاصيل الحركة" aria-label="تفاصيل الحركة">i</button>'
            )

        change_html = _build_audit_change_html(details)
        reason_html = f'<div class="audit-reason"><div>{html.escape(log["reason"])}</div>'
        if change_html:
            reason_html += f'<div class="audit-diff">{change_html}</div>'
        reason_html += '</div>'

        rows.append(
            f'''
            <tr>
                <td>{log['created_at']}</td>
                <td>{html.escape(log['actor_name'])}</td>
                <td>{action_label}</td>
                <td>{log['entity_id'] or '-'}</td>
                <td>{html.escape(product_name)}</td>
                <td>{reason_html}</td>
                <td><div class="audit-status-tools"><span class="audit-status {status_class}">{status_label}</span>{details_button_html}</div></td>
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


def render_receipt_edit_page(receipt, lines, items, message=None, message_type='success'):
    message_html = ''
    if message:
        message_html = f'<div class="alert {message_type}">{html.escape(message)}</div>'

    def _build_item_options(selected_item_id):
        options = []
        for item in items:
            selected = ' selected' if int(item['id']) == int(selected_item_id) else ''
            label = f"{item['name']} (المخزون: {item['stock']})"
            options.append(f'<option value="{item["id"]}"{selected}>{html.escape(label)}</option>')
        return ''.join(options)

    line_rows = []
    for line in lines:
        unit_price = (line['sell_total'] / line['quantity']) if line['quantity'] else 0
        line_rows.append(
            f'''
            <div class="sale-line">
                <select name="item_id" required>{_build_item_options(line['item_id'])}</select>
                <input type="number" name="unit_price" step="0.01" min="0.01" value="{unit_price:.2f}" required>
                <input type="number" name="quantity" min="1" value="{line['quantity']}" required>
                <button type="button" class="secondary" onclick="removeSaleLine(this)">حذف السطر</button>
            </div>
            '''
        )

    if not line_rows:
        default_options = _build_item_options(items[0]['id']) if items else ''
        line_rows.append(
            f'''
            <div class="sale-line">
                <select name="item_id" required>{default_options}</select>
                <input type="number" name="unit_price" step="0.01" min="0.01" required>
                <input type="number" name="quantity" min="1" value="1" required>
                <button type="button" class="secondary" onclick="removeSaleLine(this)">حذف السطر</button>
            </div>
            '''
        )

    options_html = ''.join(
        f'<option value="{item["id"]}">{html.escape(item["name"])} (المخزون: {item["stock"]})</option>' for item in items
    )
    received_value = '' if receipt['payment_method'] != 'دين' else float(receipt['paid_amount'] or 0)
    return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تعديل الوصل</title>
    <link rel="stylesheet" href="/static/style.css">
    <style>
        .panel {{
            max-width: 1200px;
            margin: 24px auto;
        }}
        .sale-lines {{
            display: grid;
            gap: 0.75rem;
            margin-top: 1rem;
        }}
        .sale-line {{
            display: grid;
            grid-template-columns: minmax(320px, 2fr) minmax(160px, 1fr) minmax(120px, 0.7fr) 120px;
            gap: 0.75rem;
            align-items: center;
        }}
        .sale-head {{
            display: grid;
            grid-template-columns: minmax(320px, 2fr) minmax(160px, 1fr) minmax(120px, 0.7fr) 120px;
            gap: 0.75rem;
            color: #cbd5e1;
            font-size: 0.9rem;
            margin-top: 1rem;
        }}
        .tools {{
            display: flex;
            gap: 0.75rem;
            flex-wrap: wrap;
            margin-top: 1rem;
        }}
        .tools a {{
            text-decoration: none;
            color: white;
            background: linear-gradient(135deg, #475569, #64748b);
            padding: 0.7rem 1rem;
            border-radius: 10px;
        }}
        @media (max-width: 900px) {{
            .sale-head {{ display: none; }}
            .sale-line {{ grid-template-columns: 1fr; }}
            .sale-line button {{ width: 100%; }}
        }}
    </style>
</head>
<body>
    <section class="panel">
        <h2>تعديل الوصل رقم {int(receipt['id']):04d}</h2>
        {message_html}
        <form action="/receipts/{receipt['id']}/update" method="post">
            <div class="form-grid add-form">
                <input type="text" name="customer_name" value="{html.escape(receipt['customer_name'] or '')}" placeholder="اسم الزبون" required>
                <input type="text" name="company_name" value="{html.escape(receipt['company_name'] or '')}" placeholder="اسم الجهة (اختياري)">
                <select name="payment_method" id="payment-method" onchange="toggleReceivedAmount()">
                    <option value="نقدي"{' selected' if receipt['payment_method'] == 'نقدي' else ''}>نقدي</option>
                    <option value="دين"{' selected' if receipt['payment_method'] == 'دين' else ''}>دين</option>
                </select>
                <input type="number" step="0.01" min="0" name="received_amount" id="received-amount" value="{received_value}" placeholder="المبلغ المستلم للذمم">
            </div>

            <div class="sale-head">
                <span>المنتج</span>
                <span>سعر الوحدة</span>
                <span>الكمية</span>
                <span>إجراء</span>
            </div>
            <div id="sale-lines" class="sale-lines">
                {''.join(line_rows)}
            </div>

            <div class="tools">
                <button type="button" class="secondary" onclick="addSaleLine()">إضافة سطر</button>
                <button type="submit">حفظ التعديلات</button>
                <a href="/receipts/{receipt['id']}/print" target="_blank">طباعة الوصل</a>
                <a href="/">العودة</a>
            </div>
        </form>
    </section>

    <template id="line-template">
        <div class="sale-line">
            <select name="item_id" required>{options_html}</select>
            <input type="number" name="unit_price" step="0.01" min="0.01" required>
            <input type="number" name="quantity" min="1" value="1" required>
            <button type="button" class="secondary" onclick="removeSaleLine(this)">حذف السطر</button>
        </div>
    </template>

    <script>
        function addSaleLine() {{
            const template = document.getElementById('line-template');
            const clone = template.content.firstElementChild.cloneNode(true);
            document.getElementById('sale-lines').appendChild(clone);
        }}

        function removeSaleLine(button) {{
            const container = document.getElementById('sale-lines');
            if (container.children.length === 1) {{
                return;
            }}
            button.closest('.sale-line').remove();
        }}

        function toggleReceivedAmount() {{
            const payment = document.getElementById('payment-method').value;
            const input = document.getElementById('received-amount');
            if (payment === 'دين') {{
                input.removeAttribute('disabled');
            }} else {{
                input.setAttribute('disabled', 'disabled');
            }}
        }}

        toggleReceivedAmount();
    </script>
</body>
</html>'''


def render_page(items, sales, receipts, customers, suppliers, summary, audit_logs, security_status, receipt_options='', message=None, message_type='success', current_user=None, app_users=None, initial_tab=None):
    message_html = ''
    if message:
        message_html = f'<div class="alert {message_type}">{html.escape(message)}</div>'

    current_user = current_user or _admin_user_record()
    app_users = app_users or []

    total_items = summary.get('total_items', 0)
    total_stock = summary.get('total_stock', 0)
    total_sales_value = summary.get('total_sales', 0)
    total_profit = summary.get('total_profit', 0)

    item_cards = _render_item_cards(items)
    sales_rows = _render_sales_rows(sales)
    receipt_rows = _render_receipt_rows(receipts)
    customer_cards = _render_customer_cards(customers)
    supplier_cards = _render_supplier_cards(suppliers)
    user_cards = _render_user_cards(app_users)
    debt_customer_rows = _render_debt_customers(customers)
    debt_supplier_rows = _render_debt_suppliers(suppliers)
    audit_rows = _render_audit_rows(audit_logs)
    delete_item_options = _build_item_options(items, 'اختر المنتج المراد حذفه')
    delete_receipt_options = receipt_options or _build_receipt_options(receipts, 'اختر الوصل المراد حذفه')
    customer_name_options = ''.join([f'<option value="{html.escape(customer["name"])}"></option>' for customer in customers])
    supplier_name_options = ''.join([f'<option value="{html.escape(supplier["name"])}"></option>' for supplier in suppliers])
    item_name_options = ''.join([f'<option value="{html.escape(item["name"])}"></option>' for item in items])
    item_json = json.dumps([
        {
            'id': item['id'],
            'name': item['name'],
            'stock': item['stock'],
            'buy_price': float(item['buy_price'] or item['price'] or 0),
            'sell_price': float(item['sell_price'] or item['price'] or 0),
            'normalized_name': normalize_search_text(item['name']),
            'label': f"{item['name']} (المخزون: {item['stock']})",
        }
        for item in items
    ], ensure_ascii=False)
    visible_tab_ids = normalize_visible_tabs(current_user.get('visible_tabs') or [], include_admin_tabs=bool(current_user.get('is_admin')))
    if current_user.get('is_admin'):
        visible_tab_ids = [tab['id'] for tab in get_tab_definitions()]
    all_tab_ids = [tab['id'] for tab in get_tab_definitions()]
    clean_initial_tab = (initial_tab or '').strip()
    user_permissions_html = _render_user_permission_inputs()
    pin_status = 'تم حفظ رمز الأمان' if security_status.get('configured') else 'لم يتم حفظ رمز الأمان بعد'
    greeting_text = build_greeting_text(current_user.get('display_name') or LOGIN_USERNAME)
    current_user_name = html.escape(current_user.get('display_name') or current_user.get('username') or LOGIN_USERNAME)
    visible_tabs_json = json.dumps(visible_tab_ids, ensure_ascii=False)
    all_tabs_json = json.dumps(all_tab_ids, ensure_ascii=False)
    initial_tab_json = json.dumps(clean_initial_tab, ensure_ascii=False)
    users_tab_button = ''
    users_section = ''
    if current_user.get('is_admin'):
        users_tab_button = '<button class="tab-btn" data-tab-target="users" onclick="switchTab(event, \'users\')">المستخدمون</button>'
        users_section = f'''
        <div id="users" class="tab-content">
            <section class="panel">
                <h2>إدارة المستخدمين</h2>
                <p class="mini-note">الأدمن الثابت هو حيدر ورمز دخوله 1. من هنا يمكنك إنشاء بقية المستخدمين وتحديد الواجهات الظاهرة لكل مستخدم.</p>
                <div class="security-box">
                    <h3>إضافة مستخدم</h3>
                    <form action="/users" method="post" class="users-form">
                        <div class="form-grid add-form">
                            <input type="text" name="display_name" placeholder="اسم العرض" required>
                            <input type="text" name="username" placeholder="اسم المستخدم" required>
                            <input type="password" name="password" placeholder="كلمة المرور" required>
                        </div>
                        <div class="permissions-box">
                            <h4>الواجهات الظاهرة للمستخدم</h4>
                            <div class="permissions-grid">{user_permissions_html}</div>
                        </div>
                        <button type="submit">حفظ المستخدم</button>
                    </form>
                </div>
                <div class="items-grid">{user_cards}</div>
            </section>
        </div>
        '''

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
        .supplier-card {{
            border-color: rgba(251, 146, 60, 0.24);
            background: linear-gradient(180deg, rgba(15, 23, 42, 0.42), rgba(30, 41, 59, 0.82));
        }}
        .user-card {{
            border-color: rgba(56, 189, 248, 0.22);
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
        .audit-status-tools {{
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
        }}
        .audit-details-btn {{
            min-height: 30px;
            width: 30px;
            border-radius: 999px;
            border: 1px solid rgba(148, 163, 184, 0.42);
            background: rgba(15, 23, 42, 0.78);
            color: #bae6fd;
            cursor: pointer;
            font-weight: 700;
            line-height: 1;
            padding: 0;
        }}
        .audit-details-btn:hover {{
            border-color: rgba(56, 189, 248, 0.7);
            color: #e0f2fe;
        }}
        .audit-details-placeholder {{
            display: inline-block;
            color: #94a3b8;
            min-width: 14px;
            text-align: center;
        }}
        .audit-reason {{
            display: grid;
            gap: 0.35rem;
        }}
        .audit-diff {{
            display: grid;
            gap: 0.22rem;
            padding: 0.55rem 0.7rem;
            border-radius: 10px;
            background: rgba(2, 6, 23, 0.28);
            border: 1px solid rgba(148, 163, 184, 0.18);
            color: #cbd5e1;
            font-size: 0.9rem;
        }}
        .audit-diff-line strong {{
            color: #f8fafc;
        }}
        .audit-modal {{
            position: fixed;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            background: rgba(2, 6, 23, 0.72);
            z-index: 999;
            padding: 1rem;
        }}
        .audit-modal[hidden] {{
            display: none;
        }}
        .audit-modal-card {{
            width: min(900px, 100%);
            max-height: 86vh;
            overflow: auto;
            border-radius: 16px;
            border: 1px solid rgba(148, 163, 184, 0.28);
            background: linear-gradient(180deg, rgba(15, 23, 42, 0.95), rgba(30, 41, 59, 0.95));
            padding: 1rem;
        }}
        .audit-modal-head {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin-bottom: 0.8rem;
        }}
        .audit-modal-close {{
            min-height: 38px;
            padding: 0.45rem 0.8rem;
        }}
        .audit-detail-grid {{
            display: grid;
            gap: 0.65rem;
        }}
        .audit-detail-line {{
            display: grid;
            grid-template-columns: minmax(140px, auto) 1fr;
            gap: 0.55rem;
            align-items: baseline;
            padding: 0.55rem 0.7rem;
            border-radius: 10px;
            background: rgba(2, 6, 23, 0.28);
            border: 1px solid rgba(148, 163, 184, 0.16);
        }}
        .audit-detail-label {{
            color: #e2e8f0;
            font-weight: 700;
        }}
        .audit-detail-value {{
            color: #cbd5e1;
        }}
        .audit-detail-empty {{
            color: #94a3b8;
            padding: 0.45rem 0;
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
        .users-form {{
            display: grid;
            gap: 1rem;
        }}
        .permissions-box {{
            padding: 1rem;
            border: 1px solid rgba(148, 163, 184, 0.26);
            border-radius: 14px;
            background: rgba(15, 23, 42, 0.3);
        }}
        .permissions-box h4 {{
            margin-top: 0;
            margin-bottom: 0.75rem;
        }}
        .permissions-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 0.75rem;
        }}
        .permission-option {{
            display: flex;
            align-items: center;
            gap: 0.55rem;
            padding: 0.75rem 0.9rem;
            border-radius: 12px;
            border: 1px solid rgba(148, 163, 184, 0.24);
            background: rgba(2, 6, 23, 0.34);
        }}
        .permission-option input {{
            min-height: auto;
            width: auto;
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
        .print-link.edit-link {{
            background: linear-gradient(135deg, #475569, #64748b);
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
        .product-security-panel {{
            display: grid;
            gap: 0.75rem;
            margin: 1rem 0 1.25rem;
            padding: 1rem;
            border: 1px solid rgba(148, 163, 184, 0.3);
            border-radius: 14px;
            background: rgba(15, 23, 42, 0.28);
        }}
        .product-security-panel .form-grid {{
            margin-top: 0;
        }}
        .hero-actions {{
            display: flex;
            align-items: center;
            gap: 0.75rem;
            flex-wrap: wrap;
        }}
        .logout-link {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 44px;
            padding: 0.6rem 1rem;
            border-radius: 14px;
            text-decoration: none;
            color: #fff;
            background: linear-gradient(135deg, #ef4444, #f97316);
            box-shadow: 0 10px 24px rgba(239, 68, 68, 0.22);
        }}
        .greeting-banner {{
            margin: 1rem 0 1.25rem;
            padding: 0.95rem 1rem;
            border-radius: 16px;
            background: linear-gradient(135deg, rgba(14, 165, 233, 0.16), rgba(37, 99, 235, 0.14));
            border: 1px solid rgba(96, 165, 250, 0.18);
            color: #dbeafe;
            font-weight: 700;
        }}
        .inventory-search-box {{
            display: grid;
            gap: 0.55rem;
            margin-bottom: 1rem;
            padding: 0.9rem;
            border: 1px solid rgba(148, 163, 184, 0.3);
            border-radius: 14px;
            background: rgba(15, 23, 42, 0.28);
        }}
        .inventory-search-box input {{
            width: 100%;
            min-height: 44px;
            box-sizing: border-box;
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
        .purchase-form-wrap {{
            display: grid;
            gap: 0.75rem;
            margin-bottom: 1rem;
            padding: 1rem;
            border: 1px solid rgba(148, 163, 184, 0.3);
            border-radius: 14px;
            background: rgba(15, 23, 42, 0.35);
        }}
        .purchase-entry-form {{
            display: grid;
            gap: 0.9rem;
        }}
        .purchase-lines {{
            display: grid;
            gap: 0.75rem;
        }}
        .purchase-line-head {{
            display: grid;
            grid-template-columns: minmax(320px, 2.2fr) minmax(160px, 1fr) minmax(120px, 0.7fr) 120px;
            gap: 0.75rem;
            font-size: 0.86rem;
            color: #cbd5e1;
            opacity: 0.95;
            padding: 0 0.25rem;
        }}
        .purchase-line-head span:last-child {{
            text-align: center;
        }}
        .purchase-line {{
            display: grid;
            grid-template-columns: minmax(320px, 2.2fr) minmax(160px, 1fr) minmax(120px, 0.7fr) 120px;
            gap: 0.75rem;
            align-items: center;
        }}
        .purchase-line input,
        .purchase-line button,
        .purchase-meta input,
        .purchase-meta select {{
            width: 100%;
            box-sizing: border-box;
            min-height: 44px;
        }}
        .purchase-line button {{
            padding: 0.72rem 0.6rem;
            white-space: nowrap;
        }}
        .purchase-tools {{
            display: flex;
            gap: 0.75rem;
            flex-wrap: wrap;
            justify-content: flex-start;
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
            .purchase-line-head,
            .purchase-line {{
                grid-template-columns: minmax(420px, 2.2fr) minmax(160px, 1fr) minmax(120px, 0.7fr) auto;
            }}
            .purchase-line button {{ width: 132px; }}
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
            .purchase-line-head {{
                display: none;
            }}
            .purchase-line {{
                grid-template-columns: 1fr;
            }}
            .purchase-tools {{
                width: 100%;
            }}
            .purchase-tools button {{
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
                <p>إدارة المنتجات والزبائن والشركات والبيع من صفحة واحدة</p>
            </div>
            <div class="hero-actions">
                <div class="hero-badge">{current_user_name}</div>
                <a class="logout-link" href="/logout">تسجيل الخروج</a>
            </div>
        </header>

        <div class="greeting-banner">{greeting_text}</div>

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
            <button class="tab-btn active" data-tab-target="products" onclick="switchTab(event, 'products')">المنتجات</button>
            <button class="tab-btn" data-tab-target="customers" onclick="switchTab(event, 'customers')">الزبائن</button>
            <button class="tab-btn" data-tab-target="suppliers" onclick="switchTab(event, 'suppliers')">الشركات</button>
            <button class="tab-btn" data-tab-target="sales" onclick="switchTab(event, 'sales')">المبيعات</button>
            <button class="tab-btn" data-tab-target="audit" onclick="switchTab(event, 'audit')">الرقابة</button>
            {users_tab_button}
        </div>

        <div id="products" class="tab-content active">
            <section class="panel">
                <h2>إدارة المنتجات</h2>
                <form id="product-add-form" action="/items" method="post" class="form-grid add-form">
                    <input type="text" name="name" placeholder="اسم المنتج" required>
                    <input type="number" step="0.01" name="buy_price" placeholder="سعر الشراء" required>
                    <input type="number" step="0.01" name="sell_price" placeholder="سعر البيع" required>
                    <input type="number" name="stock" placeholder="المخزون" required>
                    <input type="text" name="supplier_name" list="supplier-name-options" placeholder="اسم الشركة (اختياري)">
                    <input type="number" step="0.01" min="0" name="paid_amount" placeholder="المبلغ المسدد للشركة (اختياري)">
                    <button type="submit">إضافة منتج</button>
                </form>

                <p class="hint">هذا القسم مخصص للإضافة، مراجعة المخزون، وتعديل بيانات المنتجات فقط. وإذا اخترت اسم شركة ومبلغًا مسددًا فسيُسجل مباشرة في إدارة الشركات.</p>
                <div class="product-security-panel">
                    <h3>بيانات التعديل والحذف</h3>
                    <p class="mini-note">هذه البيانات تُستخدم تلقائيًا لكل بطاقة منتج عند الحفظ أو الحذف.</p>
                    <div class="form-grid add-form">
                        <input type="text" id="product-actor-name" placeholder="اسم المسؤول" required>
                        <input type="text" id="product-reason" placeholder="سبب العملية" required>
                        <input type="password" id="product-admin-pin" placeholder="رمز الأمان" required>
                    </div>
                </div>
                <div class="inventory-search-box">
                    <input type="search" id="inventory-search" placeholder="ابحث بذكاء في المخزون: اسم كامل، كلمة ناقصة، خطأ إملائي، أو ترتيب مختلف" oninput="scheduleInventorySearch(this.value)">
                    <p class="mini-note" id="inventory-search-status">اكتب اسم المنتج وسيتم ترتيب النتائج تلقائيًا حسب أعلى نسبة تشابه.</p>
                </div>
                <div class="items-grid" id="inventory-items-grid">{item_cards}</div>
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

        <div id="suppliers" class="tab-content">
            <section class="panel">
                <h2>إدارة الشركات والموردين</h2>
                <p class="mini-note">لا توجد إضافة مباشرة للشركات من هذا القسم. يتم إنشاء سجل الشركة تلقائيًا عند إدخال المنتج من تبويب المنتجات مع اسم الشركة، وهنا فقط تتابع الحركات والذمم وسجل الشركة.</p>

                <div class="debt-board">
                    <h3>الشركات المدينة علينا</h3>
                    <p class="mini-note">تظهر الشركات التي لها متبقٍ غير مسدد.</p>
                    <ul class="sales-list">{debt_supplier_rows}</ul>
                </div>

                <div class="items-grid">{supplier_cards}</div>
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
                                <input type="number" name="unit_price" step="0.01" min="0.01" placeholder="سعر الوحدة (اختياري)" onkeydown="handleUnitPriceEnter(event, this)">
                                <input type="number" name="quantity" value="1" min="1" required onkeydown="handleQuantityEnter(event, this)">
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
                    <p class="mini-note" id="audit-live-status">الرقابة تعمل كتحديث مباشر أثناء فتح هذا التبويب.</p>
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
                        <tbody id="audit-table-body">{audit_rows}</tbody>
                    </table>
                </div>

                <div id="audit-details-modal" class="audit-modal" hidden>
                    <div class="audit-modal-card">
                        <div class="audit-modal-head">
                            <h3>تفاصيل الحركة الرقابية</h3>
                            <button type="button" class="secondary audit-modal-close" onclick="closeAuditDetailsModal()">إغلاق</button>
                        </div>
                        <div id="audit-details-content" class="audit-detail-grid"></div>
                    </div>
                </div>
            </section>
        </div>
        {users_section}
    </div>

    <script>
        const saleItems = {item_json};
        const allowedTabIds = {visible_tabs_json};
        const knownTabIds = {all_tabs_json};
        const requestedTabId = {initial_tab_json};
        let latestAuditId = 0;
        let auditPollTimer = null;

        let inventorySearchTimer = null;
        let inventorySearchRequestId = 0;

        function normalizeSearchText(value) {{
            return (value || '')
                .toLowerCase()
                .normalize('NFKD')
                .replace(/[\u064B-\u065F\u0670\u06D6-\u06ED]/g, '')
                .replace(/[أإآٱ]/g, 'ا')
                .replace(/ة/g, 'ه')
                .replace(/ى/g, 'ي')
                .replace(/ؤ/g, 'و')
                .replace(/ئ/g, 'ي')
                .replace(/ء/g, '')
                .replace(/ـ/g, '')
                .replace(/[\\W_]+/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
        }}

        function compactSearchText(value) {{
            return normalizeSearchText(value).replace(/\\s+/g, '');
        }}

        function getMatchedItemByName(rawName) {{
            const normalizedValue = normalizeSearchText(rawName);
            const compactValue = compactSearchText(rawName);
            if (!normalizedValue) {{
                return null;
            }}

            const exactMatch = saleItems.find(item => item.normalized_name === normalizedValue || compactSearchText(item.name) === compactValue);
            if (exactMatch) {{
                return exactMatch;
            }}

            let bestMatch = null;
            let bestScore = 0;
            for (const item of saleItems) {{
                const normalizedName = item.normalized_name || normalizeSearchText(item.name);
                const compactName = compactSearchText(item.name);
                let score = 0;

                if (normalizedName.includes(normalizedValue) || compactName.includes(compactValue)) {{
                    score = 97;
                }} else {{
                    const left = compactValue;
                    const right = compactName;
                    const maxLength = Math.max(left.length, right.length, 1);
                    let distance = 0;
                    const matrix = Array.from({{ length: left.length + 1 }}, (_, row) => Array.from({{ length: right.length + 1 }}, (_, col) => row === 0 ? col : col === 0 ? row : 0));
                    for (let row = 1; row <= left.length; row += 1) {{
                        for (let col = 1; col <= right.length; col += 1) {{
                            const cost = left[row - 1] === right[col - 1] ? 0 : 1;
                            matrix[row][col] = Math.min(
                                matrix[row - 1][col] + 1,
                                matrix[row][col - 1] + 1,
                                matrix[row - 1][col - 1] + cost
                            );
                        }}
                    }}
                    distance = matrix[left.length][right.length];
                    score = Math.max(0, 100 - ((distance / maxLength) * 100));
                    const sortedQuery = normalizedValue.split(' ').sort().join(' ');
                    const sortedName = normalizedName.split(' ').sort().join(' ');
                    if (sortedQuery && sortedQuery === sortedName) {{
                        score = Math.max(score, 99);
                    }}
                }}

                if (score > bestScore) {{
                    bestScore = score;
                    bestMatch = item;
                }}
            }}

            return bestScore >= 60 ? bestMatch : null;
        }}

        async function fetchInventoryMatches(query) {{
            const response = await fetch(`/items/search?q=${{encodeURIComponent(query)}}`, {{
                headers: {{ 'Accept': 'application/json' }},
            }});
            if (!response.ok) {{
                throw new Error('فشل تحميل نتائج البحث');
            }}
            return response.json();
        }}

        function resetInventoryResults() {{
            const cards = Array.from(document.querySelectorAll('#inventory-items-grid .item-card'));
            const grid = document.getElementById('inventory-items-grid');
            for (const card of cards) {{
                card.style.display = '';
                grid.appendChild(card);
            }}
            const status = document.getElementById('inventory-search-status');
            if (status) {{
                status.textContent = 'اكتب اسم المنتج وسيتم ترتيب النتائج تلقائيًا حسب أعلى نسبة تشابه.';
            }}
        }}

        function renderInventoryResults(query, results) {{
            const grid = document.getElementById('inventory-items-grid');
            const status = document.getElementById('inventory-search-status');
            const cards = Array.from(grid.querySelectorAll('.item-card'));
            const cardsById = new Map(cards.map(card => [Number(card.dataset.itemId), card]));
            const matchedIds = new Set(results.map(item => Number(item.id)));

            for (const card of cards) {{
                card.style.display = matchedIds.has(Number(card.dataset.itemId)) ? '' : 'none';
            }}

            for (const item of results) {{
                const card = cardsById.get(Number(item.id));
                if (card) {{
                    grid.appendChild(card);
                }}
            }}

            if (!results.length) {{
                status.textContent = `لا توجد نتيجة قريبة لعبارة "${{query}}".`;
                return;
            }}

            status.textContent = `تم العثور على ${{results.length}} نتيجة مرتبة من الأقرب إلى الأبعد لعبارة "${{query}}".`;
        }}

        function scheduleInventorySearch(rawQuery) {{
            clearTimeout(inventorySearchTimer);
            inventorySearchTimer = setTimeout(() => performInventorySearch(rawQuery), 140);
        }}

        async function performInventorySearch(rawQuery) {{
            const query = (rawQuery || '').trim();
            if (!query) {{
                resetInventoryResults();
                return;
            }}

            const requestId = ++inventorySearchRequestId;
            try {{
                const results = await fetchInventoryMatches(query);
                if (requestId !== inventorySearchRequestId) {{
                    return;
                }}
                renderInventoryResults(query, results);
            }} catch (error) {{
                const status = document.getElementById('inventory-search-status');
                if (status) {{
                    status.textContent = 'تعذر تنفيذ البحث الذكي الآن. أعد المحاولة.';
                }}
            }}
        }}

        function syncProductSecurityFields(form) {{
            const actorName = document.getElementById('product-actor-name');
            const reason = document.getElementById('product-reason');
            const adminPin = document.getElementById('product-admin-pin');
            const actorInput = form.querySelector('input[name="actor_name"]');
            const reasonInput = form.querySelector('input[name="reason"]');
            const pinInput = form.querySelector('input[name="admin_pin"]');

            if (!actorName || !reason || !adminPin || !actorInput || !reasonInput || !pinInput) {{
                return false;
            }}

            actorInput.value = actorName.value.trim();
            reasonInput.value = reason.value.trim();
            pinInput.value = adminPin.value.trim();

            if (!actorInput.value || !reasonInput.value || !pinInput.value) {{
                return false;
            }}

            return true;
        }}

        async function handleProductEnter(event, searchInput) {{
            if (event.key !== 'Enter') {{
                return;
            }}
            event.preventDefault();

            const value = searchInput.value.trim();
            if (!value) {{
                return;
            }}

            const row = searchInput.closest('.sale-line');
            const unitPriceInput = row.querySelector('input[name="unit_price"]');
            const matchedItem = getMatchedItemByName(value);
            if (matchedItem) {{
                searchInput.value = matchedItem.name;
                if (!unitPriceInput.value) {{
                    unitPriceInput.value = matchedItem.sell_price.toFixed(2);
                }}
            }}

            unitPriceInput.focus();
            unitPriceInput.select();
        }}

        function handleUnitPriceEnter(event, unitPriceInput) {{
            if (event.key !== 'Enter') {{
                return;
            }}
            event.preventDefault();

            const row = unitPriceInput.closest('.sale-line');
            const productInput = row.querySelector('input[name="item_name"]');
            const qtyInput = row.querySelector('input[name="quantity"]');
            const matchedItem = getMatchedItemByName(productInput.value);

            if (!unitPriceInput.value && matchedItem) {{
                unitPriceInput.value = matchedItem.sell_price.toFixed(2);
            }}

            qtyInput.focus();
            qtyInput.select();
        }}

        function handleQuantityEnter(event, qtyInput) {{
            if (event.key !== 'Enter') {{
                return;
            }}
            event.preventDefault();

            if (!qtyInput.value) {{
                qtyInput.value = '1';
            }}

            const currentRow = qtyInput.closest('.sale-line');
            let nextRow = currentRow.nextElementSibling;
            while (nextRow && !nextRow.classList.contains('sale-line')) {{
                nextRow = nextRow.nextElementSibling;
            }}

            if (!nextRow) {{
                nextRow = addSaleLine();
            }}

            const nextProductInput = nextRow.querySelector('input[name="item_name"]');
            nextProductInput.focus();
            nextProductInput.select();
        }}

        function addSaleLine() {{
            const container = document.getElementById('sale-lines');
            const line = document.createElement('div');
            line.className = 'sale-line';
            line.innerHTML = `
                <input type="search" name="item_name" list="item-name-options" placeholder="اكتب اسم المنتج واضغط Enter لإدراجه" onkeydown="handleProductEnter(event, this)" required>
                <input type="number" name="unit_price" step="0.01" min="0.01" placeholder="سعر الوحدة (اختياري)" onkeydown="handleUnitPriceEnter(event, this)">
                <input type="number" name="quantity" value="1" min="1" required onkeydown="handleQuantityEnter(event, this)">
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

        function handlePurchaseItemEnter(event, searchInput) {{
            if (event.key !== 'Enter') {{
                return;
            }}
            event.preventDefault();

            const value = searchInput.value.trim();
            if (!value) {{
                return;
            }}

            const row = searchInput.closest('.purchase-line');
            const unitCostInput = row.querySelector('input[name="unit_cost"]');
            const matchedItem = getMatchedItemByName(value);
            if (matchedItem) {{
                searchInput.value = matchedItem.name;
                if (!unitCostInput.value && matchedItem.buy_price) {{
                    unitCostInput.value = Number(matchedItem.buy_price).toFixed(2);
                }}
            }}

            unitCostInput.focus();
            unitCostInput.select();
        }}

        function handlePurchaseCostEnter(event, unitCostInput) {{
            if (event.key !== 'Enter') {{
                return;
            }}
            event.preventDefault();

            const row = unitCostInput.closest('.purchase-line');
            const productInput = row.querySelector('input[name="item_name"]');
            const qtyInput = row.querySelector('input[name="quantity"]');
            const matchedItem = getMatchedItemByName(productInput.value);

            if (!unitCostInput.value && matchedItem && matchedItem.buy_price) {{
                unitCostInput.value = Number(matchedItem.buy_price).toFixed(2);
            }}

            qtyInput.focus();
            qtyInput.select();
        }}

        function handlePurchaseQuantityEnter(event, qtyInput) {{
            if (event.key !== 'Enter') {{
                return;
            }}
            event.preventDefault();

            if (!qtyInput.value) {{
                qtyInput.value = '1';
            }}

            const currentRow = qtyInput.closest('.purchase-line');
            let nextRow = currentRow.nextElementSibling;
            while (nextRow && !nextRow.classList.contains('purchase-line')) {{
                nextRow = nextRow.nextElementSibling;
            }}

            if (!nextRow) {{
                nextRow = addPurchaseLine();
            }}

            const nextProductInput = nextRow.querySelector('input[name="item_name"]');
            nextProductInput.focus();
            nextProductInput.select();
        }}

        function addPurchaseLine() {{
            const container = document.getElementById('purchase-lines');
            const line = document.createElement('div');
            line.className = 'purchase-line';
            line.innerHTML = `
                <input type="search" name="item_name" list="item-name-options" placeholder="اكتب اسم المنتج" onkeydown="handlePurchaseItemEnter(event, this)" required>
                <input type="number" name="unit_cost" step="0.01" min="0.01" placeholder="سعر الشراء" onkeydown="handlePurchaseCostEnter(event, this)">
                <input type="number" name="quantity" value="1" min="1" required onkeydown="handlePurchaseQuantityEnter(event, this)">
                <button type="button" class="secondary" onclick="removePurchaseLine(this)">حذف السطر</button>
            `;
            container.appendChild(line);
            return line;
        }}

        function removePurchaseLine(button) {{
            const container = document.getElementById('purchase-lines');
            if (container.children.length === 1) {{
                const row = button.closest('.purchase-line');
                row.querySelector('input[name="item_name"]').value = '';
                row.querySelector('input[name="unit_cost"]').value = '';
                row.querySelector('input[name="quantity"]').value = 1;
                return;
            }}
            button.closest('.purchase-line').remove();
        }}

        function setupProductAddEnterFlow() {{
            const form = document.getElementById('product-add-form');
            if (!form) {{
                return;
            }}

            const stockInput = form.querySelector('input[name="stock"]');
            const supplierInput = form.querySelector('input[name="supplier_name"]');
            const paidAmountInput = form.querySelector('input[name="paid_amount"]');
            const submitButton = form.querySelector('button[type="submit"]');

            if (!stockInput || !supplierInput || !paidAmountInput || !submitButton) {{
                return;
            }}

            stockInput.addEventListener('keydown', function (event) {{
                if (event.key !== 'Enter') {{
                    return;
                }}
                event.preventDefault();
                supplierInput.focus();
                supplierInput.select();
            }});

            supplierInput.addEventListener('keydown', function (event) {{
                if (event.key !== 'Enter') {{
                    return;
                }}
                event.preventDefault();
                paidAmountInput.focus();
                paidAmountInput.select();
            }});

            paidAmountInput.addEventListener('keydown', function (event) {{
                if (event.key !== 'Enter') {{
                    return;
                }}
                event.preventDefault();
                submitButton.click();
            }});
        }}

        function escapeAuditHtml(value) {{
            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }}

        function getAuditFieldLabel(fieldName) {{
            const labels = {{
                name: 'الاسم',
                display_name: 'اسم العرض',
                username: 'اسم المستخدم',
                phone: 'الهاتف',
                email: 'البريد الإلكتروني',
                address: 'العنوان',
                buy_price: 'سعر الشراء',
                sell_price: 'سعر البيع',
                price: 'السعر',
                stock: 'الكمية',
                visible_tabs: 'الواجهات',
                customer_name: 'اسم الزبون',
                company_name: 'الجهة',
                payment_method: 'طريقة الدفع',
                total_amount: 'إجمالي الوصل',
                paid_amount: 'المستلم',
                debt_amount: 'المتبقي',
                total_profit: 'الربح',
                quantity: 'الكمية',
                unit_price: 'سعر الوحدة',
                sell_total: 'إجمالي البيع',
            }};
            return labels[fieldName] || fieldName;
        }}

        function formatAuditValue(fieldName, value) {{
            if (value === null || value === undefined || value === '') {{
                return 'فارغ';
            }}
            if (fieldName === 'visible_tabs') {{
                if (!Array.isArray(value) || value.length === 0) {{
                    return 'بدون واجهات';
                }}
                const tabMap = {{
                    products: 'المنتجات',
                    customers: 'الزبائن',
                    suppliers: 'الشركات',
                    sales: 'المبيعات',
                    audit: 'الرقابة',
                    users: 'المستخدمون',
                }};
                return value.map(tabId => tabMap[tabId] || tabId).join('، ');
            }}
            if (['buy_price', 'sell_price', 'price', 'total_amount', 'paid_amount', 'debt_amount', 'total_profit', 'unit_price', 'sell_total'].includes(fieldName)) {{
                const num = Number(value);
                if (!Number.isNaN(num)) {{
                    return `${{num.toLocaleString('ar-IQ')}} د.ع`;
                }}
            }}
            return String(value);
        }}

        function buildAuditBeforeAfterLines(details) {{
            const before = details && typeof details.before === 'object' && details.before ? details.before : null;
            const after = details && typeof details.after === 'object' && details.after ? details.after : null;
            if (!before || !after) {{
                return [];
            }}

            const ignored = new Set(['id', 'created_at', 'password_hash']);
            const keys = [];
            Object.keys(before).forEach(key => {{
                if (!keys.includes(key)) {{
                    keys.push(key);
                }}
            }});
            Object.keys(after).forEach(key => {{
                if (!keys.includes(key)) {{
                    keys.push(key);
                }}
            }});

            const lines = [];
            keys.forEach(key => {{
                if (ignored.has(key)) {{
                    return;
                }}
                const beforeValue = formatAuditValue(key, before[key]);
                const afterValue = formatAuditValue(key, after[key]);
                if (String(beforeValue) === String(afterValue)) {{
                    return;
                }}
                lines.push({{
                    label: getAuditFieldLabel(key),
                    value: `${{beforeValue}} ← ${{afterValue}}`,
                }});
            }});
            return lines;
        }}

        function buildReceiptLineDiffLines(details) {{
            const beforeLines = Array.isArray(details?.before_lines) ? details.before_lines : [];
            const afterLines = Array.isArray(details?.after_lines) ? details.after_lines : [];
            if (!beforeLines.length && !afterLines.length) {{
                return [];
            }}

            const beforeMap = new Map();
            beforeLines.forEach(line => {{
                const key = String(line.item_id);
                if (!beforeMap.has(key)) {{
                    beforeMap.set(key, line);
                }}
            }});

            const afterMap = new Map();
            afterLines.forEach(line => {{
                const key = String(line.item_id);
                if (!afterMap.has(key)) {{
                    afterMap.set(key, line);
                }}
            }});

            const allKeys = Array.from(new Set([...beforeMap.keys(), ...afterMap.keys()]));
            const lines = [];
            allKeys.forEach(key => {{
                const before = beforeMap.get(key) || null;
                const after = afterMap.get(key) || null;
                const itemName = (after && after.item_name) || (before && before.item_name) || `صنف #${{key}}`;

                if (!before && after) {{
                    lines.push({{
                        label: `سطر الوصل (${{itemName}})`,
                        value: `تمت إضافته: كمية ${{formatAuditValue('quantity', after.quantity)}} | سعر وحدة ${{formatAuditValue('unit_price', after.unit_price)}}`,
                    }});
                    return;
                }}
                if (before && !after) {{
                    lines.push({{
                        label: `سطر الوصل (${{itemName}})`,
                        value: `تم حذفه: كمية ${{formatAuditValue('quantity', before.quantity)}} | سعر وحدة ${{formatAuditValue('unit_price', before.unit_price)}}`,
                    }});
                    return;
                }}

                const qtyBefore = formatAuditValue('quantity', before.quantity);
                const qtyAfter = formatAuditValue('quantity', after.quantity);
                if (String(qtyBefore) !== String(qtyAfter)) {{
                    lines.push({{
                        label: `كمية (${{itemName}})`,
                        value: `${{qtyBefore}} ← ${{qtyAfter}}`,
                    }});
                }}

                const priceBefore = formatAuditValue('unit_price', before.unit_price);
                const priceAfter = formatAuditValue('unit_price', after.unit_price);
                if (String(priceBefore) !== String(priceAfter)) {{
                    lines.push({{
                        label: `سعر الوحدة (${{itemName}})`,
                        value: `${{priceBefore}} ← ${{priceAfter}}`,
                    }});
                }}

                const totalBefore = formatAuditValue('sell_total', before.sell_total);
                const totalAfter = formatAuditValue('sell_total', after.sell_total);
                if (String(totalBefore) !== String(totalAfter)) {{
                    lines.push({{
                        label: `إجمالي السطر (${{itemName}})`,
                        value: `${{totalBefore}} ← ${{totalAfter}}`,
                    }});
                }}
            }});

            return lines;
        }}

        function openAuditDetailsFromButton(button) {{
            if (!button) {{
                return;
            }}
            const raw = button.getAttribute('data-audit-details') || '{{}}';
            let details = {{}};
            try {{
                details = JSON.parse(raw);
            }} catch (error) {{
                details = {{}};
            }}

            const modal = document.getElementById('audit-details-modal');
            const content = document.getElementById('audit-details-content');
            if (!modal || !content) {{
                return;
            }}

            const summaryLines = [];
            if (details.receipt_number) {{
                summaryLines.push({{ label: 'رقم الوصل', value: details.receipt_number }});
            }}
            if (details.customer_name) {{
                summaryLines.push({{ label: 'الزبون', value: details.customer_name }});
            }}
            if (details.supplier_name) {{
                summaryLines.push({{ label: 'الشركة', value: details.supplier_name }});
            }}

            const changeLines = buildAuditBeforeAfterLines(details);
            const lineDiffs = buildReceiptLineDiffLines(details);
            const allLines = [...summaryLines, ...changeLines, ...lineDiffs];

            if (!allLines.length) {{
                content.innerHTML = '<div class="audit-detail-empty">لا توجد تفاصيل إضافية لهذه الحركة.</div>';
            }} else {{
                content.innerHTML = allLines.map(line => (
                    `<div class="audit-detail-line"><div class="audit-detail-label">${{escapeAuditHtml(line.label)}}</div><div class="audit-detail-value">${{escapeAuditHtml(line.value)}}</div></div>`
                )).join('');
            }}

            modal.hidden = false;
        }}

        function closeAuditDetailsModal() {{
            const modal = document.getElementById('audit-details-modal');
            if (modal) {{
                modal.hidden = true;
            }}
        }}

        function applyTabPermissions() {{
            const allowed = new Set(allowedTabIds || []);
            knownTabIds.forEach(function (tabId) {{
                if (allowed.has(tabId)) {{
                    return;
                }}
                const button = document.querySelector(`.tab-btn[data-tab-target="${{tabId}}"]`);
                const content = document.getElementById(tabId);
                if (button) {{
                    button.remove();
                }}
                if (content) {{
                    content.remove();
                }}
            }});

            const remainingButtons = Array.from(document.querySelectorAll('.tab-btn'));
            const remainingContents = Array.from(document.querySelectorAll('.tab-content'));
            remainingButtons.forEach(button => button.classList.remove('active'));
            remainingContents.forEach(content => content.classList.remove('active'));

            const targetButton = requestedTabId
                ? remainingButtons.find(button => button.dataset.tabTarget === requestedTabId)
                : null;
            const firstButton = targetButton || remainingButtons[0];
            if (!firstButton) {{
                return;
            }}

            const targetId = firstButton.dataset.tabTarget;
            const targetContent = document.getElementById(targetId);
            firstButton.classList.add('active');
            if (targetContent) {{
                targetContent.classList.add('active');
            }}

            if (targetId === 'audit') {{
                refreshAuditFeed(true);
            }}
        }}

        setupProductAddEnterFlow();

        async function refreshAuditFeed(forceRefresh = false) {{
            const auditBody = document.getElementById('audit-table-body');
            if (!auditBody) {{
                return;
            }}

            const auditTab = document.getElementById('audit');
            if (!auditTab || !auditTab.classList.contains('active')) {{
                return;
            }}

            try {{
                const response = await fetch(`/audit/rows?latest_id=${{encodeURIComponent(String(latestAuditId || 0))}}`, {{
                    headers: {{ 'Accept': 'application/json' }},
                }});
                if (!response.ok) {{
                    throw new Error('فشل تحميل الرقابة');
                }}
                const payload = await response.json();
                if (forceRefresh || Number(payload.latest_id || 0) !== Number(latestAuditId || 0)) {{
                    auditBody.innerHTML = payload.rows_html || '';
                    latestAuditId = Number(payload.latest_id || 0);
                }}
                const status = document.getElementById('audit-live-status');
                if (status) {{
                    status.textContent = 'الرقابة محدثة مباشرة الآن.';
                }}
            }} catch (error) {{
                const status = document.getElementById('audit-live-status');
                if (status) {{
                    status.textContent = 'تعذر تحديث الرقابة الآن. سيستمر النظام بالمحاولة.';
                }}
            }}
        }}

        function startAuditPolling() {{
            if (auditPollTimer) {{
                clearInterval(auditPollTimer);
            }}
            auditPollTimer = setInterval(() => refreshAuditFeed(false), 1500);
        }}

        function switchTab(event, tabName) {{
            const contents = document.querySelectorAll('.tab-content');
            contents.forEach(tab => tab.classList.remove('active'));

            const buttons = document.querySelectorAll('.tab-btn');
            buttons.forEach(btn => btn.classList.remove('active'));

            document.getElementById(tabName).classList.add('active');
            event.currentTarget.classList.add('active');

            if (tabName === 'audit') {{
                refreshAuditFeed(true);
            }}
        }}

        applyTabPermissions();
        startAuditPolling();
    </script>
</body>
</html>'''


class PosHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/static/style.css':
            self.serve_static_css()
            return

        if parsed.path == '/logout':
            self.send_response(303)
            self.send_header('Set-Cookie', f'{AUTH_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax')
            self.send_header('Location', '/login')
            self.end_headers()
            return

        if parsed.path == '/login':
            query = parse_qs(parsed.query)
            message = query.get('msg', [None])[0]
            message_type = query.get('type', ['error'])[0]
            self.send_html(render_login_page(message=message, message_type=message_type))
            return

        current_user = get_current_user(self.headers, db_path=DB_PATH)
        if not current_user:
            self.send_response(303)
            self.send_header('Location', '/login')
            self.end_headers()
            return

        if parsed.path == '/items/search':
            query = parse_qs(parsed.query).get('q', [''])[0]
            results = search_items(query, db_path=DB_PATH)
            self.send_json(results)
            return

        if parsed.path == '/audit/rows':
            if not user_can_access_tab(current_user, 'audit'):
                self.send_json({'error': 'غير مصرح'}, status=403)
                return
            self.send_json(build_audit_payload(db_path=DB_PATH, limit=25))
            return

        query = parse_qs(parsed.query)
        message = query.get('msg', [None])[0]
        message_type = query.get('type', ['success'])[0]
        initial_tab = query.get('tab', [''])[0]

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

        if parsed.path.startswith('/suppliers/') and parsed.path.endswith('/details'):
            supplier_id = int(parsed.path.split('/')[2])
            supplier = get_supplier(supplier_id, db_path=DB_PATH)
            if supplier:
                purchases = get_supplier_purchases(supplier_id, db_path=DB_PATH)
                view_mode = (query.get('view', ['purchases'])[0] or 'purchases').strip().lower()
                if view_mode not in {'purchases', 'debts', 'all'}:
                    view_mode = 'purchases'
                debt_transactions = get_supplier_debt_transactions(supplier_id, db_path=DB_PATH)
                body = self.render_supplier_details(
                    supplier,
                    purchases,
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

        if parsed.path.startswith('/receipts/') and parsed.path.endswith('/edit'):
            receipt_id = int(parsed.path.split('/')[2])
            receipt = get_receipt(receipt_id, db_path=DB_PATH)
            if receipt:
                body = render_receipt_edit_page(
                    receipt,
                    get_receipt_lines(receipt_id, db_path=DB_PATH),
                    list_items(db_path=DB_PATH),
                    message=message,
                    message_type=message_type,
                )
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
            list_suppliers(db_path=DB_PATH),
            get_summary(db_path=DB_PATH),
            list_audit_logs(db_path=DB_PATH),
            get_security_status(db_path=DB_PATH),
            receipt_options=_build_receipt_options(list_receipts(db_path=DB_PATH, limit=500), 'اختر الوصل المراد حذفه'),
            message=message,
            message_type=message_type,
            current_user=current_user,
            app_users=list_app_users(db_path=DB_PATH) if current_user.get('is_admin') else [],
            initial_tab=initial_tab,
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
        redirect_tab = ''

        try:
            if parsed.path == '/login':
                username = first('username').strip()
                password = first('password').strip()
                user = authenticate_app_user(username, password, db_path=DB_PATH)
                if user:
                    secret_value = LOGIN_PASSWORD if user.get('is_admin') else user['password_hash']
                    token = build_auth_cookie_value(user['username'], secret_value)
                    self.send_response(303)
                    self.send_header('Set-Cookie', f'{AUTH_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax')
                    self.send_header('Location', '/')
                    self.end_headers()
                    return

                self.send_response(303)
                self.send_header('Location', f'/login?msg={quote("بيانات الدخول غير صحيحة")}&type=error')
                self.end_headers()
                return

            current_user = get_current_user(self.headers, db_path=DB_PATH)
            if not current_user:
                self.send_response(303)
                self.send_header('Location', '/login')
                self.end_headers()
                return

            if parsed.path == '/users':
                if not current_user.get('is_admin'):
                    raise PermissionError('هذه الصفحة متاحة للأدمن فقط')
                create_app_user(
                    first('username'),
                    first('password'),
                    display_name=first('display_name'),
                    visible_tabs=data.get('visible_tabs', []),
                    actor_name=current_user.get('display_name') or current_user.get('username'),
                    db_path=DB_PATH,
                )
                message = 'تم حفظ المستخدم بنجاح'
                redirect_tab = 'audit'
            elif re.search(r'/users/(\d+)/update/?$', parsed.path):
                if not current_user.get('is_admin'):
                    raise PermissionError('هذه الصفحة متاحة للأدمن فقط')
                match = re.search(r'/users/(\d+)/update/?$', parsed.path)
                update_app_user(
                    int(match.group(1)),
                    first('username'),
                    display_name=first('display_name'),
                    password=first('password'),
                    visible_tabs=data.get('visible_tabs', []),
                    actor_name=current_user.get('display_name') or current_user.get('username'),
                    db_path=DB_PATH,
                )
                message = 'تم تحديث المستخدم بنجاح'
                redirect_tab = 'audit'
            elif re.search(r'/users/(\d+)/delete/?$', parsed.path):
                if not current_user.get('is_admin'):
                    raise PermissionError('هذه الصفحة متاحة للأدمن فقط')
                match = re.search(r'/users/(\d+)/delete/?$', parsed.path)
                delete_app_user(
                    int(match.group(1)),
                    actor_name=current_user.get('display_name') or current_user.get('username'),
                    db_path=DB_PATH,
                )
                message = 'تم حذف المستخدم بنجاح'
                redirect_tab = 'audit'

            elif parsed.path == '/items':
                item_name = first('name')
                buy_price = first('buy_price')
                sell_price = first('sell_price')
                stock_value = first('stock')
                supplier_name = first('supplier_name')
                paid_amount = first('paid_amount')
                actor_label = current_user.get('display_name') or current_user.get('username') or 'النظام'

                item_id = add_item(
                    item_name,
                    buy_price,
                    sell_price,
                    stock_value,
                    actor_name=actor_label,
                    reason='إضافة منتج من الواجهة',
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )

                clean_supplier_name = supplier_name.strip()
                clean_paid_amount = paid_amount.strip()
                if clean_supplier_name or clean_paid_amount:
                    if not clean_supplier_name:
                        raise ValueError('يرجى إدخال اسم الشركة إذا أردت تسجيل مبلغ مسدد')

                    unit_cost = float(buy_price or 0)
                    quantity = int(float(stock_value or 0))
                    total_cost = unit_cost * quantity
                    if clean_paid_amount:
                        paid_value = float(clean_paid_amount)
                        if paid_value > total_cost:
                            raise ValueError('المبلغ المسدد أكبر من إجمالي تكلفة المنتج')
                        payment_method = 'دين' if paid_value < total_cost else 'نقدي'
                    else:
                        paid_value = total_cost
                        payment_method = 'نقدي'

                    create_supplier_purchase(
                        [{'item_id': item_id, 'quantity': quantity, 'unit_cost': unit_cost}],
                        supplier_name=clean_supplier_name,
                        payment_method=payment_method,
                        paid_amount=str(paid_value),
                        note='تسجيل شراء تلقائي من شاشة إضافة المنتج',
                        apply_stock=False,
                        actor_name=actor_label,
                        reason='تسجيل شراء تلقائي عند إضافة منتج',
                        client_ip=self.client_address[0],
                        db_path=DB_PATH,
                    )

                message = 'تمت إضافة المنتج بنجاح'
                redirect_tab = 'audit'
            elif re.search(r'/receipts/(\d+)/update/?$', parsed.path):
                match = re.search(r'/receipts/(\d+)/update/?$', parsed.path)
                if not match:
                    raise ValueError('مسار تعديل الوصل غير صحيح')

                receipt_id = int(match.group(1))
                item_ids = data.get('item_id', [])
                unit_prices = data.get('unit_price', [])
                quantities = data.get('quantity', [])
                receipt_lines = []
                max_lines = max(len(item_ids), len(unit_prices), len(quantities))
                for idx in range(max_lines):
                    item_id_value = item_ids[idx] if idx < len(item_ids) else ''
                    unit_price_value = unit_prices[idx] if idx < len(unit_prices) else ''
                    quantity_value = quantities[idx] if idx < len(quantities) else ''

                    if (item_id_value or '').strip() and (quantity_value or '').strip():
                        receipt_lines.append(
                            {
                                'item_id': item_id_value,
                                'unit_price': unit_price_value,
                                'quantity': quantity_value,
                            }
                        )

                update_sale_receipt(
                    receipt_id,
                    receipt_lines,
                    customer_name=first('customer_name'),
                    company_name=first('company_name'),
                    payment_method=first('payment_method') or 'نقدي',
                    received_amount=first('received_amount'),
                    actor_name=current_user.get('display_name') or current_user.get('username') or 'النظام',
                    reason='تعديل الوصل من الواجهة',
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم تعديل الوصل بنجاح'
                self.send_response(303)
                self.send_header('Location', f'/?msg={quote(message)}&type=success&tab=audit')
                self.end_headers()
                return
            elif parsed.path.rstrip('/').endswith('/update'):
                parts = [part for part in parsed.path.strip('/').split('/') if part]
                if len(parts) >= 3 and parts[0] == 'items':
                    update_item(
                        int(parts[1]),
                        first('name'),
                        first('buy_price'),
                        first('sell_price'),
                        first('stock'),
                        actor_name=current_user.get('display_name') or current_user.get('username') or first('actor_name'),
                        reason=first('reason'),
                        admin_pin=first('admin_pin'),
                        client_ip=self.client_address[0],
                        db_path=DB_PATH,
                    )
                    message = 'تم تعديل المنتج بنجاح'
                    redirect_tab = 'audit'
                elif len(parts) >= 3 and parts[0] == 'customers':
                    update_customer(
                        int(parts[1]),
                        first('name'),
                        first('phone'),
                        first('email'),
                        first('address'),
                        db_path=DB_PATH,
                    )
                    message = 'تم تحديث بيانات الزبون بنجاح'
                elif len(parts) >= 3 and parts[0] == 'suppliers':
                    update_supplier(
                        int(parts[1]),
                        first('name'),
                        first('phone'),
                        first('email'),
                        first('address'),
                        actor_name=current_user.get('display_name') or current_user.get('username') or 'النظام',
                        db_path=DB_PATH,
                    )
                    message = 'تم تحديث بيانات الشركة بنجاح'
                    redirect_tab = 'audit'
                elif 'receipts' in parts and 'update' in parts:
                    receipt_id = None
                    for idx, part in enumerate(parts):
                        if part == 'receipts' and idx + 1 < len(parts):
                            try:
                                receipt_id = int(parts[idx + 1])
                                break
                            except (TypeError, ValueError):
                                continue

                    if not receipt_id:
                        raise ValueError('مسار تعديل الوصل غير صحيح')

                    item_ids = data.get('item_id', [])
                    unit_prices = data.get('unit_price', [])
                    quantities = data.get('quantity', [])
                    receipt_lines = []
                    max_lines = max(len(item_ids), len(unit_prices), len(quantities))
                    for idx in range(max_lines):
                        item_id_value = item_ids[idx] if idx < len(item_ids) else ''
                        unit_price_value = unit_prices[idx] if idx < len(unit_prices) else ''
                        quantity_value = quantities[idx] if idx < len(quantities) else ''

                        if (item_id_value or '').strip() and (quantity_value or '').strip():
                            receipt_lines.append(
                                {
                                    'item_id': item_id_value,
                                    'unit_price': unit_price_value,
                                    'quantity': quantity_value,
                                }
                            )

                    update_sale_receipt(
                        receipt_id,
                        receipt_lines,
                        customer_name=first('customer_name'),
                        company_name=first('company_name'),
                        payment_method=first('payment_method') or 'نقدي',
                        received_amount=first('received_amount'),
                        actor_name=current_user.get('display_name') or current_user.get('username') or 'النظام',
                        reason='تعديل الوصل من الواجهة',
                        client_ip=self.client_address[0],
                        db_path=DB_PATH,
                    )
                    message = 'تم تعديل الوصل بنجاح'
                    self.send_response(303)
                    self.send_header('Location', f'/?msg={quote(message)}&type=success&tab=audit')
                    self.end_headers()
                    return
            elif parsed.path == '/items/delete':
                delete_item(
                    int(first('item_id') or '0'),
                    actor_name=current_user.get('display_name') or current_user.get('username') or first('actor_name'),
                    reason=first('reason'),
                    admin_pin=first('admin_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم حذف المنتج بنجاح'
                redirect_tab = 'audit'
            elif parsed.path == '/receipts/delete':
                delete_receipt(
                    int(first('receipt_id') or '0'),
                    actor_name=current_user.get('display_name') or current_user.get('username') or first('actor_name'),
                    reason=first('reason'),
                    admin_pin=first('admin_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم حذف الوصل بنجاح'
                redirect_tab = 'audit'
            elif parsed.path == '/receipts/delete-all':
                delete_all_receipts(
                    actor_name=current_user.get('display_name') or current_user.get('username') or first('actor_name'),
                    reason=first('reason'),
                    admin_pin=first('admin_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم حذف كل الوصولات بنجاح'
                redirect_tab = 'audit'
            elif parsed.path == '/summary/reset':
                reset_sales_profit_totals(
                    actor_name=current_user.get('display_name') or current_user.get('username') or first('actor_name'),
                    reason=first('reason'),
                    admin_pin=first('admin_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم تصفير إجمالي المبيعات والربح بنجاح'
                redirect_tab = 'audit'
            elif parsed.path == '/security/pin':
                update_admin_pin(
                    current_user.get('display_name') or current_user.get('username') or first('actor_name'),
                    first('current_pin'),
                    first('new_pin'),
                    first('confirm_pin'),
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                message = 'تم تحديث رمز الأمان بنجاح'
                redirect_tab = 'audit'
            elif parsed.path.endswith('/delete'):
                parts = parsed.path.split('/')
                if parts[1] == 'customers':
                    delete_customer(int(parts[2]), db_path=DB_PATH)
                    message = 'تم حذف الزبون بنجاح'
                elif parts[1] == 'suppliers':
                    delete_supplier(
                        int(parts[2]),
                        db_path=DB_PATH,
                    )
                    message = 'تم حذف الشركة بنجاح'
                    redirect_tab = 'audit'
            elif parsed.path == '/customers':
                add_customer(first('name'), first('phone'), first('email'), first('address'), db_path=DB_PATH)
                message = 'تم إضافة الزبون بنجاح'
            elif parsed.path == '/suppliers':
                raise ValueError('إضافة الشركة المباشرة متوقفة. أضف المنتج أولًا من تبويب المنتجات مع اسم الشركة ليتم إنشاء السجل تلقائيًا.')
            elif parsed.path.startswith('/customers/') and parsed.path.endswith('/debt/pay'):
                parts = parsed.path.split('/')
                if parts[1] == 'customers':
                    open_debt = pay_customer_debt(
                        int(parts[2]),
                        first('payment_amount'),
                        note=first('payment_note'),
                        actor_name=current_user.get('display_name') or current_user.get('username') or 'النظام',
                        reason='تسديد ذمة من الواجهة',
                        client_ip=self.client_address[0],
                        db_path=DB_PATH,
                    )
                    message = f'تم تسجيل التسديد بنجاح. المتبقي: {format_iqd_with_words(open_debt)}'
                    self.send_response(303)
                    self.send_header('Location', f'/?msg={quote(message)}&type=success&tab=audit')
                    self.end_headers()
                    return
            elif parsed.path == '/supplier-purchases':
                item_names = data.get('item_name', [])
                unit_costs = data.get('unit_cost', [])
                quantities = data.get('quantity', [])
                purchase_lines = []
                max_lines = max(len(item_names), len(unit_costs), len(quantities))
                for idx in range(max_lines):
                    item_name_value = item_names[idx] if idx < len(item_names) else ''
                    unit_cost_value = unit_costs[idx] if idx < len(unit_costs) else ''
                    quantity_value = quantities[idx] if idx < len(quantities) else ''

                    if (item_name_value or '').strip() and (unit_cost_value or '').strip() and (quantity_value or '').strip():
                        purchase_lines.append(
                            {
                                'item_name': item_name_value,
                                'unit_cost': unit_cost_value,
                                'quantity': quantity_value,
                            }
                        )

                purchase_id, _, supplier_id = create_supplier_purchase(
                    purchase_lines,
                    supplier_name=first('supplier_name'),
                    payment_method=first('payment_method') or 'نقدي',
                    paid_amount=first('paid_amount'),
                    note=first('purchase_note'),
                    actor_name=current_user.get('display_name') or current_user.get('username') or 'النظام',
                    reason='تسجيل شراء من الواجهة',
                    client_ip=self.client_address[0],
                    db_path=DB_PATH,
                )
                self.send_response(303)
                self.send_header('Location', '/?msg=%D8%AA%D9%85%20%D8%AD%D9%81%D8%B8%20%D8%A7%D9%84%D8%B4%D8%B1%D8%A7%D8%A1%20%D8%A8%D9%86%D8%AC%D8%A7%D8%AD&type=success&tab=audit')
                self.end_headers()
                return
            elif parsed.path.startswith('/suppliers/') and parsed.path.endswith('/debt/pay'):
                parts = parsed.path.split('/')
                if parts[1] == 'suppliers':
                    open_debt = pay_supplier_debt(
                        int(parts[2]),
                        first('payment_amount'),
                        note=first('payment_note'),
                        actor_name=current_user.get('display_name') or current_user.get('username') or 'النظام',
                        reason='تسديد شركة من الواجهة',
                        client_ip=self.client_address[0],
                        db_path=DB_PATH,
                    )
                    message = f'تم تسجيل التسديد بنجاح. المتبقي: {format_iqd_with_words(open_debt)}'
                    self.send_response(303)
                    self.send_header('Location', f'/?msg={quote(message)}&type=success&tab=audit')
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
                    actor_name=current_user.get('display_name') or current_user.get('username') or 'النظام',
                    client_ip=self.client_address[0],
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
        tab_suffix = f'&tab={quote(redirect_tab)}' if redirect_tab else ''
        self.send_header('Location', f'/?msg={quote(message)}&type={message_type}{tab_suffix}')
        self.end_headers()

    def serve_static_css(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/css; charset=utf-8')
        self.end_headers()
        with open(CSS_PATH, 'rb') as handle:
            self.wfile.write(handle.read())

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
                        <a class="print-link edit-link" href="/receipts/{receipt['id']}/edit">تعديل الوصل</a>
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

    def render_supplier_details(self, supplier, purchases, debt_transactions, message=None, message_type='success', view_mode='purchases'):
        message_html = ''
        if message:
            message_html = f'<div class="alert {message_type}">{html.escape(message)}</div>'

        total_purchased = sum(purchase['total_amount'] for purchase in purchases)
        total_paid = sum(purchase['paid_amount'] for purchase in purchases)
        total_open_debt = sum(purchase['debt_amount'] for purchase in purchases)

        purchase_rows = []
        for purchase in purchases:
            purchase_no = f"{int(purchase['id']):04d}"
            purchase_rows.append(
                f'''
                <li class="sale-row receipt-row">
                    <div>
                        <strong>فاتورة شراء رقم {purchase_no}</strong>
                        <p>الشركة: {html.escape(purchase['supplier_name'])} | طريقة الدفع: {html.escape(purchase['payment_method'])}</p>
                        <p>المشتريات: {html.escape(purchase['items_summary'] or '-')}</p>
                        <p>المسدد: {format_iqd(purchase['paid_amount'])} | المتبقي: {format_iqd_with_words(purchase['debt_amount'])}</p>
                        {f'<p>ملاحظة: {html.escape(purchase["note"] or "-")}</p>' if purchase['note'] else ''}
                    </div>
                    <div class="receipt-actions">
                        <span>{purchase['created_at']}</span>
                    </div>
                </li>
                '''
            )

        ledger_rows = []
        running_balance = 0.0
        for tx in debt_transactions:
            tx_amount = float(tx['amount'] or 0)
            tx_type = tx['transaction_type']
            if tx_type == 'charge':
                running_balance += tx_amount
            else:
                running_balance -= tx_amount

            tx_label = 'فاتورة شراء' if tx_type == 'charge' else 'تسديد'
            purchase_label = f"{int(tx['purchase_id']):04d}" if tx['purchase_id'] else '-'
            ledger_rows.append(
                f'''
                <tr>
                    <td>{tx['created_at']}</td>
                    <td>{tx_label}</td>
                    <td>{purchase_label}</td>
                    <td>{format_iqd(tx_amount)}</td>
                    <td>{format_iqd(running_balance)}</td>
                    <td>{html.escape(tx['note'] or '-')}</td>
                </tr>
                '''
            )

        purchases_html = ''.join(purchase_rows) or '<li class="sale-row">لا توجد مشتريات مسجلة لهذه الشركة</li>'
        ledger_html = ''.join(reversed(ledger_rows)) or '<tr><td colspan="6">لا توجد حركات ذمم بعد</td></tr>'
        show_purchases = view_mode in {'purchases', 'all'}
        show_debts = view_mode in {'debts', 'all'}

        return f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تفاصيل الشركة - نظام الكاشير</title>
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
                <h1>تفاصيل الشركة</h1>
                <p>{html.escape(supplier['name'])}</p>
            </div>
            <a href="/" style="padding: 0.5rem 1rem; background: #0066cc; color: white; text-decoration: none; border-radius: 4px;">العودة للرئيسية</a>
        </header>

        {message_html}

        <section class="stats-grid">
            <div class="stat-card">
                <h3>إجمالي المشتريات</h3>
                <p>{format_iqd(total_purchased)}</p>
            </div>
            <div class="stat-card">
                <h3>إجمالي المسدد</h3>
                <p>{format_iqd(total_paid)}</p>
            </div>
            <div class="stat-card">
                <h3>المتبقي علينا</h3>
                <p>{format_iqd_with_words(total_open_debt)}</p>
            </div>
            <div class="stat-card">
                <h3>عدد الفواتير</h3>
                <p>{len(purchases)}</p>
            </div>
        </section>

        <section class="panel">
            <h2>عرض السجل</h2>
            <div class="card-actions">
                <form action="/suppliers/{supplier['id']}/details" method="get">
                    <input type="hidden" name="view" value="purchases">
                    <button type="submit" class="secondary">المشتريات</button>
                </form>
                <form action="/suppliers/{supplier['id']}/details" method="get">
                    <input type="hidden" name="view" value="debts">
                    <button type="submit" class="secondary">سجل الذمم</button>
                </form>
                <form action="/suppliers/{supplier['id']}/details" method="get">
                    <input type="hidden" name="view" value="all">
                    <button type="submit" class="secondary">عرض الكل</button>
                </form>
            </div>
            <div class="debt-payment-wrap">
                <strong>تسجيل تسديد للشركة</strong>
                <p class="mini-note">المتبقي الحالي: {format_iqd_with_words(total_open_debt)}</p>
                <form action="/suppliers/{supplier['id']}/debt/pay" method="post" class="form-grid add-form">
                    <input type="number" step="0.01" min="0.01" name="payment_amount" placeholder="مبلغ التسديد" required>
                    <input type="text" name="payment_note" placeholder="ملاحظة (اختياري)">
                    <button type="submit">تسجيل التسديد</button>
                </form>
            </div>
        </section>

        <section class="panel">
            <h2>معلومات الشركة</h2>
            <p>الاسم: {html.escape(supplier['name'])}</p>
            <p>الهاتف: {html.escape(supplier['phone'] or '-')}</p>
            <p>البريد: {html.escape(supplier['email'] or '-')}</p>
            <p>العنوان: {html.escape(supplier['address'] or '-')}</p>
            <p>تاريخ التسجيل: {supplier['created_at']}</p>
        </section>

        {f'''<section class="panel"><h2>حركات الشراء</h2><ul class="sales-list">{purchases_html}</ul></section>''' if show_purchases else ''}
        {f'''<section class="panel"><h2>دفتر الذمم (شراء/سداد)</h2><div class="debt-table-wrapper"><table class="debt-table"><thead><tr><th>الوقت</th><th>نوع الحركة</th><th>المرجع</th><th>المبلغ</th><th>الرصيد بعد الحركة</th><th>ملاحظة</th></tr></thead><tbody>{ledger_html}</tbody></table></div></section>''' if show_debts else ''}
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
