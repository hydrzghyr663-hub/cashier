import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from datetime import datetime

import app
from app import PosHandler, add_customer, add_item, add_supplier, build_auth_token, create_app_user, create_sale_receipt, create_supplier_purchase, delete_item, delete_supplier, find_best_item_match, get_customer_debt_transactions, get_customer_receipts, get_daily_receipts, get_receipt, get_receipt_lines, get_summary, get_time_greeting, get_supplier, get_supplier_debt_transactions, init_db, is_authenticated, list_audit_logs, list_customers, list_items, list_sales, list_suppliers, normalize_search_text, pay_customer_debt, pay_supplier_debt, reset_sales_profit_totals, search_items, sell_item, update_admin_pin, update_app_user, update_item, update_sale_receipt, update_supplier, verify_admin_pin


class PosSystemTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, 'test_pos.db')
        init_db(self.db_path)

    def _login_cookie(self, port, username='حيدر', password='1'):
        body = urllib.parse.urlencode({'username': username, 'password': password})
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        connection.request('POST', '/login', body=body, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        response = connection.getresponse()
        self.assertEqual(response.status, 303)
        cookie = response.getheader('Set-Cookie')
        self.assertIsNotNone(cookie)
        return cookie.split(';', 1)[0]

    def test_add_edit_delete_and_sell_item(self):
        add_item('شاي', 12.5, 10, db_path=self.db_path)

        items = list_items(self.db_path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], 'شاي')
        self.assertEqual(items[0]['price'], 12.5)
        self.assertEqual(items[0]['stock'], 10)

        with self.assertRaises(ValueError):
            update_item(1, 'شاي كبير', 15, 8, db_path=self.db_path)

        denied_log = list_audit_logs(self.db_path, limit=1)[0]
        self.assertEqual(denied_log['action'], 'update')
        self.assertEqual(denied_log['status'], 'denied')

        update_item(
            1,
            'شاي كبير',
            15,
            8,
            actor_name='المدير',
            reason='تصحيح السعر والمخزون',
            admin_pin='1234',
            db_path=self.db_path,
        )
        updated = list_items(self.db_path)[0]
        self.assertEqual(updated['name'], 'شاي كبير')
        self.assertEqual(updated['price'], 15.0)
        self.assertEqual(updated['stock'], 8)

        total = sell_item(1, 2, db_path=self.db_path)
        self.assertEqual(total, 30.0)

        after_sale = list_items(self.db_path)[0]
        self.assertEqual(after_sale['stock'], 6)

        with self.assertRaises(PermissionError):
            delete_item(
                1,
                actor_name='موظف',
                reason='محاولة غير مصرح بها',
                admin_pin='9999',
                db_path=self.db_path,
            )

        delete_item(
            1,
            actor_name='المدير',
            reason='إزالة منتج متوقف',
            admin_pin='1234',
            db_path=self.db_path,
        )
        self.assertEqual(list_items(self.db_path), [])

        logs = list_audit_logs(self.db_path, limit=4)
        success_delete_log = next(log for log in logs if log['action'] == 'delete' and log['status'] == 'success')
        self.assertIn('المنتج: "شاي كبير"', success_delete_log['reason'])
        denied_delete_log = next(log for log in list_audit_logs(self.db_path, limit=10) if log['action'] == 'delete' and log['status'] == 'denied')
        self.assertIn('محاولة غير مصرح بها', denied_delete_log['reason'])

    def test_admin_pin_is_saved_and_only_admin_can_change_it(self):
        self.assertTrue(verify_admin_pin('1234', db_path=self.db_path))

        with self.assertRaises(PermissionError):
            update_admin_pin(
                'موظف',
                '9999',
                '5678',
                '5678',
                db_path=self.db_path,
            )

        update_admin_pin(
            'الأدمن',
            '1234',
            '5678',
            '5678',
            db_path=self.db_path,
        )

        self.assertFalse(verify_admin_pin('1234', db_path=self.db_path))
        self.assertTrue(verify_admin_pin('5678', db_path=self.db_path))

        add_item('عصير', 4, 6, stock=3, db_path=self.db_path)
        with self.assertRaises(PermissionError):
            delete_item(
                1,
                actor_name='الأدمن',
                reason='اختبار الرمز القديم',
                admin_pin='1234',
                db_path=self.db_path,
            )

        delete_item(
            1,
            actor_name='الأدمن',
            reason='اختبار الرمز الجديد',
            admin_pin='5678',
            db_path=self.db_path,
        )

        pin_logs = [log for log in list_audit_logs(self.db_path, limit=20) if log['action'] == 'update_pin']
        pin_statuses = {log['status'] for log in pin_logs}
        self.assertIn('success', pin_statuses)
        self.assertIn('denied', pin_statuses)

    def test_http_home_page_shows_audit_tab(self):
        add_item('سكر', 3, 4, stock=5, db_path=self.db_path)
        add_supplier('شركة الندى', '0771111111', db_path=self.db_path)

        original_db_path = app.DB_PATH
        try:
            app.DB_PATH = self.db_path
            server = ThreadingHTTPServer(('127.0.0.1', 0), PosHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                cookie = self._login_cookie(port)
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/', headers={'Cookie': cookie})
                response = connection.getresponse()
                body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('الرقابة على التعديل والحذف', body)
                self.assertIn('هذا القسم مخصص للإضافة، مراجعة المخزون، وتعديل بيانات المنتجات فقط.', body)
                self.assertIn('إدارة رمز الأمان', body)
                self.assertIn('إدارة الشركات والموردين', body)
                self.assertIn('الشركات المدينة علينا', body)
                self.assertIn('إدارة المستخدمين', body)
                self.assertIn('القوائم المدينة', body)
                self.assertIn('حذف وصل محدد', body)
                self.assertIn('حذف كل الوصولات', body)
                self.assertIn('تصفير إجمالي المبيعات والربح', body)
                self.assertIn('تصفير العدادات', body)
                self.assertIn('اسم الزبون', body)
                self.assertIn('المبلغ المستلم من القائمة المدينة', body)
                self.assertIn('اكتب اسم المنتج واضغط Enter لإدراجه', body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.DB_PATH = original_db_path

    def test_http_item_add_links_supplier_purchase_and_audit(self):
        original_db_path = app.DB_PATH
        try:
            app.DB_PATH = self.db_path
            server = ThreadingHTTPServer(('127.0.0.1', 0), PosHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                cookie = self._login_cookie(port)
                body = urllib.parse.urlencode({
                    'name': 'منتج مورّد',
                    'buy_price': '6',
                    'sell_price': '9',
                    'stock': '4',
                    'supplier_name': 'شركة المستقبل',
                    'paid_amount': '10',
                }).encode('utf-8')
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('POST', '/items', body=body, headers={'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                self.assertIn('/', response.getheader('Location', ''))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.DB_PATH = original_db_path

        items = list_items(self.db_path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], 'منتج مورّد')
        self.assertEqual(items[0]['stock'], 4)

        suppliers = list_suppliers(self.db_path)
        self.assertEqual(len(suppliers), 1)
        self.assertEqual(suppliers[0]['name'], 'شركة المستقبل')
        self.assertEqual(float(suppliers[0]['open_debt_amount'] or 0), 14.0)

        logs = list_audit_logs(self.db_path, limit=10)
        self.assertTrue(any(log['action'] == 'supplier_purchase' and log['entity_type'] == 'supplier' for log in logs))

    def test_http_user_management_limits_tabs_for_created_user(self):
        create_app_user(
            'm1',
            'pass1',
            display_name='موظف 1',
            visible_tabs=['products', 'sales'],
            db_path=self.db_path,
        )

        original_db_path = app.DB_PATH
        try:
            app.DB_PATH = self.db_path
            server = ThreadingHTTPServer(('127.0.0.1', 0), PosHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                cookie = self._login_cookie(port, username='m1', password='pass1')
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/', headers={'Cookie': cookie})
                response = connection.getresponse()
                body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('const allowedTabIds = ["products", "sales"]', body)
                self.assertIn('موظف 1', body)
                self.assertNotIn('إدارة المستخدمين', body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.DB_PATH = original_db_path

    def test_login_page_allows_switching_from_admin_to_limited_user(self):
        create_app_user(
            'm2',
            'pass2',
            display_name='موظف 2',
            visible_tabs=['products'],
            db_path=self.db_path,
        )

        original_db_path = app.DB_PATH
        try:
            app.DB_PATH = self.db_path
            server = ThreadingHTTPServer(('127.0.0.1', 0), PosHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                admin_cookie = self._login_cookie(port, username='حيدر', password='1')

                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/login', headers={'Cookie': admin_cookie})
                response = connection.getresponse()
                body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('تسجيل الدخول', body)

                form = urllib.parse.urlencode({'username': 'm2', 'password': 'pass2'})
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('POST', '/login', body=form, headers={'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': admin_cookie})
                response = connection.getresponse()
                switched_cookie = response.getheader('Set-Cookie').split(';', 1)[0]
                self.assertEqual(response.status, 303)

                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/', headers={'Cookie': switched_cookie})
                response = connection.getresponse()
                body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('const allowedTabIds = ["products"]', body)
                self.assertNotIn('إدارة المستخدمين', body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.DB_PATH = original_db_path

    def test_update_app_user_writes_audit_log(self):
        create_app_user(
            'audit-user',
            '123',
            display_name='مستخدم رقابة',
            visible_tabs=['sales'],
            actor_name='حيدر',
            db_path=self.db_path,
        )
        user = next(u for u in app.list_app_users(self.db_path) if u['username'] == 'audit-user')

        update_app_user(
            user['id'],
            'audit-user',
            display_name='مستخدم رقابة معدل',
            visible_tabs=['sales', 'products'],
            actor_name='حيدر',
            db_path=self.db_path,
        )

        logs = list_audit_logs(self.db_path, limit=10)
        update_log = next(log for log in logs if log['action'] == 'user_update')
        self.assertEqual(update_log['entity_type'], 'user')
        self.assertEqual(update_log['actor_name'], 'حيدر')
        self.assertIn('تم تعديل المستخدم "مستخدم رقابة معدل"', update_log['reason'])

        audit_html = app._render_audit_rows([update_log])
        self.assertIn('اسم العرض:', audit_html)
        self.assertIn('مستخدم رقابة', audit_html)
        self.assertIn('مستخدم رقابة معدل', audit_html)

    def test_login_gate_and_greeting_are_active(self):
        original_db_path = app.DB_PATH
        original_get_time_greeting = app.get_time_greeting
        try:
            app.DB_PATH = self.db_path
            app.get_time_greeting = lambda now=None: 'صباح الخير حيدر'
            server = ThreadingHTTPServer(('127.0.0.1', 0), PosHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]

                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/')
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader('Location'), '/login')

                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/login')
                response = connection.getresponse()
                login_body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('تسجيل الدخول', login_body)
                self.assertIn('حيدر', login_body)

                form = urllib.parse.urlencode({'username': 'حيدر', 'password': '1'})
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('POST', '/login', body=form, headers={'Content-Type': 'application/x-www-form-urlencoded'})
                response = connection.getresponse()
                cookie_header = response.getheader('Set-Cookie')
                self.assertEqual(response.status, 303)
                self.assertIn('pos_auth=', cookie_header)

                token = cookie_header.split('pos_auth=')[1].split(';', 1)[0]
                self.assertTrue(token.endswith(build_auth_token('حيدر', '1')))

                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/', headers={'Cookie': f'pos_auth={token}'})
                response = connection.getresponse()
                body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('صباح الخير حيدر', body)
                self.assertIn('نظام نقطة البيع', body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.get_time_greeting = original_get_time_greeting
            app.DB_PATH = original_db_path

    def test_sell_supports_saved_or_manual_customer_name(self):
        add_customer('سارة', '0770000000', db_path=self.db_path)
        add_item('حليب', 2, 3, stock=5, db_path=self.db_path)
        add_item('خبز', 1, 2, stock=5, db_path=self.db_path)

        sell_item(1, 1, customer_name='سارة', db_path=self.db_path)
        sell_item(2, 1, customer_name='زبون سريع', db_path=self.db_path)

        sales = list_sales(self.db_path, limit=5)
        self.assertEqual(sales[0]['customer_name'], 'زبون سريع')
        self.assertIsNone(sales[0]['customer_id'])
        self.assertEqual(sales[1]['saved_customer_name'], 'سارة')
        self.assertEqual(sales[1]['customer_name'], 'سارة')
        self.assertEqual(sales[1]['customer_id'], 1)

    def test_create_sale_receipt_supports_multiple_products_and_company_name(self):
        add_customer('سارة', '0770000000', db_path=self.db_path)
        add_item('حليب', 2, 3, stock=5, db_path=self.db_path)
        add_item('خبز', 1, 2, stock=5, db_path=self.db_path)

        receipt_id, total_amount = create_sale_receipt(
            [
                {'item_id': 1, 'quantity': 2},
                {'item_id': 2, 'quantity': 3},
            ],
            customer_name='سارة',
            company_name='مكتب لارا للتجارة العامة',
            payment_method='دين',
            db_path=self.db_path,
        )

        self.assertEqual(total_amount, 12.0)
        receipt = get_receipt(receipt_id, db_path=self.db_path)
        lines = get_receipt_lines(receipt_id, db_path=self.db_path)
        self.assertEqual(receipt['customer_name'], 'سارة')
        self.assertEqual(receipt['company_name'], 'مكتب لارا للتجارة العامة')
        self.assertEqual(receipt['payment_method'], 'دين')
        self.assertEqual(receipt['paid_amount'], 0.0)
        self.assertEqual(receipt['debt_amount'], 12.0)
        self.assertEqual(len(lines), 2)
        self.assertEqual(list_items(self.db_path)[0]['stock'], 2)
        self.assertEqual(list_items(self.db_path)[1]['stock'], 3)

        receipts, summary, _ = get_daily_receipts(db_path=self.db_path)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(summary['receipt_count'], 1)
        self.assertEqual(summary['total_amount'], 12.0)

    def test_debt_receipt_accepts_received_amount(self):
        add_item('ماء', 1, 2, stock=10, db_path=self.db_path)
        receipt_id, total_amount = create_sale_receipt(
            [{'item_id': 1, 'quantity': 3}],
            customer_name='زبون ذمم',
            payment_method='دين',
            received_amount='2',
            db_path=self.db_path,
        )

        self.assertEqual(total_amount, 6.0)
        receipt = get_receipt(receipt_id, db_path=self.db_path)
        self.assertEqual(receipt['paid_amount'], 2.0)
        self.assertEqual(receipt['debt_amount'], 4.0)

    def test_normalize_search_text_handles_arabic_variants(self):
        self.assertEqual(normalize_search_text('  أةى ــحِفَاظَة  '), 'اهي حفاظه')
        self.assertEqual(normalize_search_text('حفاضة،،، صفا'), 'حفاضه صفا')
        self.assertEqual(normalize_search_text('SOFY   Plus'), 'sofy plus')

    def test_search_items_returns_best_fuzzy_match_for_arabic_typos(self):
        add_item('حفاظة صفا', 10, 12, stock=8, db_path=self.db_path)
        add_item('حفاظة نونا', 9, 11, stock=7, db_path=self.db_path)
        add_item('مناديل مبللة', 2, 3, stock=15, db_path=self.db_path)

        for query in ['حفاضة صفا', 'حفاظه صفا', 'حفاضهصفا', 'صفا حفاظة', 'حفضة صفا']:
            results = search_items(query, db_path=self.db_path)
            self.assertTrue(results)
            self.assertEqual(results[0]['name'], 'حفاظة صفا')

        best_match = find_best_item_match('صفا حفاضة', db_path=self.db_path)
        self.assertIsNotNone(best_match)
        self.assertEqual(best_match['name'], 'حفاظة صفا')

    def test_create_sale_receipt_accepts_fuzzy_item_name(self):
        add_item('حفاظة صفا', 10, 12, stock=9, db_path=self.db_path)

        receipt_id, total_amount = create_sale_receipt(
            [{'item_name': 'حفاضهصفا', 'quantity': 2}],
            customer_name='زبون بحث ذكي',
            payment_method='نقدي',
            db_path=self.db_path,
        )

        self.assertEqual(total_amount, 24.0)
        lines = get_receipt_lines(receipt_id, db_path=self.db_path)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['item_name'], 'حفاظة صفا')
        self.assertEqual(list_items(self.db_path)[0]['stock'], 7)

    def test_debt_customer_is_auto_added_to_customer_management(self):
        add_item('دفتر', 2, 5, stock=20, db_path=self.db_path)

        receipt_id, total_amount = create_sale_receipt(
            [{'item_id': 1, 'quantity': 2}],
            customer_name='زبون دين تلقائي',
            payment_method='دين',
            db_path=self.db_path,
        )

        self.assertEqual(total_amount, 10.0)
        receipt = get_receipt(receipt_id, db_path=self.db_path)
        self.assertIsNotNone(receipt['customer_id'])
        self.assertEqual(receipt['debt_amount'], 10.0)

        customers = list_customers(self.db_path)
        names = [row['name'] for row in customers]
        self.assertIn('زبون دين تلقائي', names)

        tx = get_customer_debt_transactions(receipt['customer_id'], db_path=self.db_path)
        self.assertEqual(len(tx), 1)
        self.assertEqual(tx[0]['transaction_type'], 'charge')
        self.assertEqual(tx[0]['amount'], 10.0)

    def test_edit_receipt_debt_links_to_edited_customer_account(self):
        add_customer('الزبون القديم', '0770000010', db_path=self.db_path)
        add_customer('الزبون الجديد', '0770000011', db_path=self.db_path)
        add_item('منتج تعديل', 5, 10, stock=40, db_path=self.db_path)

        receipt_id, _ = create_sale_receipt(
            [{'item_id': 1, 'quantity': 2}],
            customer_name='الزبون القديم',
            payment_method='دين',
            db_path=self.db_path,
        )

        update_sale_receipt(
            receipt_id,
            [{'item_id': 1, 'quantity': 2, 'unit_price': '10'}],
            customer_name='الزبون الجديد',
            payment_method='دين',
            received_amount='5',
            db_path=self.db_path,
        )

        customers = list_customers(self.db_path)
        old_customer = next(row for row in customers if row['name'] == 'الزبون القديم')
        new_customer = next(row for row in customers if row['name'] == 'الزبون الجديد')

        receipt = get_receipt(receipt_id, db_path=self.db_path)
        self.assertEqual(receipt['customer_id'], new_customer['id'])
        self.assertEqual(receipt['customer_name'], 'الزبون الجديد')
        self.assertEqual(receipt['debt_amount'], 15.0)

        old_receipts = get_customer_receipts(old_customer['id'], debt_only=True, db_path=self.db_path)
        new_receipts = get_customer_receipts(new_customer['id'], debt_only=True, db_path=self.db_path)
        self.assertEqual(len(old_receipts), 0)
        self.assertEqual(len(new_receipts), 1)
        self.assertEqual(new_receipts[0]['id'], receipt_id)

        old_tx = get_customer_debt_transactions(old_customer['id'], db_path=self.db_path)
        new_tx = get_customer_debt_transactions(new_customer['id'], db_path=self.db_path)
        self.assertEqual(len(old_tx), 0)
        self.assertEqual(len(new_tx), 1)
        self.assertEqual(new_tx[0]['transaction_type'], 'charge')
        self.assertEqual(new_tx[0]['amount'], 15.0)

        logs = list_audit_logs(self.db_path, limit=5)
        receipt_update_log = next(log for log in logs if log['action'] == 'update_receipt')
        self.assertEqual(receipt_update_log['entity_type'], 'receipt')
        self.assertIn('تم تعديل الوصل رقم', receipt_update_log['reason'])
        self.assertIn('الزبون الجديد', receipt_update_log['reason'])

    def test_edit_receipt_from_cash_to_debt_auto_creates_customer_account(self):
        add_item('منتج تحويل', 5, 10, stock=50, db_path=self.db_path)

        receipt_id, _ = create_sale_receipt(
            [{'item_id': 1, 'quantity': 3}],
            customer_name='عميل تحويل',
            payment_method='نقدي',
            db_path=self.db_path,
        )

        receipt_before = get_receipt(receipt_id, db_path=self.db_path)
        self.assertEqual(receipt_before['debt_amount'], 0.0)

        update_sale_receipt(
            receipt_id,
            [{'item_id': 1, 'quantity': 3, 'unit_price': '10'}],
            customer_name='عميل تحويل دين',
            payment_method='دين',
            received_amount='0',
            db_path=self.db_path,
        )

        receipt_after = get_receipt(receipt_id, db_path=self.db_path)
        self.assertEqual(receipt_after['payment_method'], 'دين')
        self.assertGreater(float(receipt_after['debt_amount']), 0.0)
        self.assertIsNotNone(receipt_after['customer_id'])
        self.assertEqual(receipt_after['customer_name'], 'عميل تحويل دين')

        customers = list_customers(self.db_path)
        names = [row['name'] for row in customers]
        self.assertIn('عميل تحويل دين', names)

        tx = get_customer_debt_transactions(receipt_after['customer_id'], db_path=self.db_path)
        self.assertEqual(len(tx), 1)
        self.assertEqual(tx[0]['transaction_type'], 'charge')
        self.assertGreater(float(tx[0]['amount']), 0.0)

    def test_receipt_update_audit_contains_before_after_line_details(self):
        add_item('رز فاخر', 1000, 1500, stock=40, db_path=self.db_path)

        receipt_id, _ = create_sale_receipt(
            [{'item_id': 1, 'quantity': 2}],
            customer_name='زبون تفاصيل',
            payment_method='نقدي',
            db_path=self.db_path,
        )

        update_sale_receipt(
            receipt_id,
            [{'item_id': 1, 'quantity': 3, 'unit_price': '1700'}],
            customer_name='زبون تفاصيل معدل',
            payment_method='نقدي',
            actor_name='حيدر',
            reason='تدقيق تفاصيل الوصل',
            db_path=self.db_path,
        )

        logs = list_audit_logs(self.db_path, limit=10)
        receipt_update_log = next(log for log in logs if log['action'] == 'update_receipt')
        details = json.loads(receipt_update_log['details'])

        self.assertIn('before_lines', details)
        self.assertIn('after_lines', details)
        self.assertEqual(details['before_lines'][0]['quantity'], 2)
        self.assertEqual(details['after_lines'][0]['quantity'], 3)
        self.assertEqual(details['before_lines'][0]['unit_price'], 1500.0)
        self.assertEqual(details['after_lines'][0]['unit_price'], 1700.0)

        audit_html = app._render_audit_rows([receipt_update_log])
        self.assertIn('class="audit-details-btn"', audit_html)

    def test_pay_customer_debt_reduces_remaining_and_logs_payment(self):
        add_item('سكر', 3, 6, stock=30, db_path=self.db_path)
        receipt_id, _ = create_sale_receipt(
            [{'item_id': 1, 'quantity': 3}],
            customer_name='زبون سداد',
            payment_method='دين',
            db_path=self.db_path,
        )

        receipt = get_receipt(receipt_id, db_path=self.db_path)
        customer_id = receipt['customer_id']

        remaining = pay_customer_debt(customer_id, 8, note='دفعة أولى', db_path=self.db_path)
        self.assertEqual(remaining, 10.0)

        updated_receipt = get_receipt(receipt_id, db_path=self.db_path)
        self.assertEqual(updated_receipt['paid_amount'], 8.0)
        self.assertEqual(updated_receipt['debt_amount'], 10.0)

        tx = get_customer_debt_transactions(customer_id, db_path=self.db_path)
        self.assertEqual(len(tx), 2)
        self.assertEqual(tx[1]['transaction_type'], 'payment')
        self.assertEqual(tx[1]['amount'], 8.0)

    def test_supplier_purchase_tracks_debt_and_payments(self):
        add_item('دفتر مورد', 3, 5, stock=10, db_path=self.db_path)

        purchase_id, total_amount, supplier_id = create_supplier_purchase(
            [{'item_name': 'دفتر مورد', 'quantity': 4, 'unit_cost': '6'}],
            supplier_name='شركة المستقبل',
            payment_method='دين',
            paid_amount='8',
            note='فاتورة أولى',
            db_path=self.db_path,
        )

        self.assertGreater(purchase_id, 0)
        self.assertEqual(total_amount, 24.0)
        supplier = get_supplier(supplier_id, db_path=self.db_path)
        self.assertEqual(supplier['name'], 'شركة المستقبل')

        suppliers = list_suppliers(self.db_path)
        self.assertEqual(len(suppliers), 1)
        self.assertEqual(float(suppliers[0]['open_debt_amount'] or 0), 16.0)

        updated_item = list_items(self.db_path)[0]
        self.assertEqual(updated_item['stock'], 14)
        self.assertEqual(updated_item['buy_price'], 6.0)

        tx = get_supplier_debt_transactions(supplier_id, db_path=self.db_path)
        self.assertEqual(len(tx), 1)
        self.assertEqual(tx[0]['transaction_type'], 'charge')
        self.assertEqual(tx[0]['amount'], 16.0)

        remaining = pay_supplier_debt(supplier_id, 6, note='دفعة أولى', db_path=self.db_path)
        self.assertEqual(remaining, 10.0)

        updated_supplier = get_supplier(supplier_id, db_path=self.db_path)
        self.assertEqual(updated_supplier['name'], 'شركة المستقبل')

        tx = get_supplier_debt_transactions(supplier_id, db_path=self.db_path)
        self.assertEqual(len(tx), 2)
        self.assertEqual(tx[1]['transaction_type'], 'payment')
        self.assertEqual(tx[1]['amount'], 6.0)

    def test_supplier_audit_includes_name_change_and_actor_role(self):
        supplier_id = add_supplier('شركة البداية', db_path=self.db_path)

        update_supplier(
            supplier_id,
            'شركة النهاية',
            actor_name='حيدر',
            actor_role='أدمن',
            db_path=self.db_path,
        )

        delete_supplier(
            supplier_id,
            actor_name='موظف 1',
            actor_role='مستخدم',
            db_path=self.db_path,
        )

        logs = list_audit_logs(self.db_path, limit=10)
        update_log = next(log for log in logs if log['action'] == 'supplier_update')
        delete_log = next(log for log in logs if log['action'] == 'supplier_delete')

        self.assertEqual(update_log['actor_name'], 'حيدر')
        self.assertIn('اسم الشركة: "شركة البداية" إلى "شركة النهاية"', update_log['reason'])

        self.assertEqual(delete_log['actor_name'], 'موظف 1')
        self.assertEqual(delete_log['reason'], 'تم حذف الشركة "شركة النهاية"')

    def test_sell_with_existing_total_column_without_default(self):
        conn = app.connect(self.db_path)
        conn.execute('DROP TABLE IF EXISTS sales')
        conn.execute('''
            CREATE TABLE sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                total REAL NOT NULL,
                sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                buy_total REAL NOT NULL DEFAULT 0,
                sell_total REAL NOT NULL DEFAULT 0,
                profit REAL NOT NULL DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()

        add_item('قهوة', 5, 7, stock=4, db_path=self.db_path)

        total = sell_item(1, 2, db_path=self.db_path)
        self.assertEqual(total, 14.0)
        updated = list_items(self.db_path)[0]
        self.assertEqual(updated['stock'], 2)

    def test_sell_via_http_updates_stock(self):
        add_item('قهوة', 5, 7, stock=4, db_path=self.db_path)

        original_db_path = app.DB_PATH
        original_sell_defaults = app.sell_item.__defaults__
        original_add_defaults = app.add_item.__defaults__
        try:
            app.DB_PATH = self.db_path
            app.sell_item.__defaults__ = (self.db_path,)
            app.add_item.__defaults__ = (self.db_path,)

            server = ThreadingHTTPServer(('127.0.0.1', 0), PosHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                cookie = self._login_cookie(port)
                body = urllib.parse.urlencode({'item_id': '1', 'quantity': '2', 'customer_name': 'زبون اختبار'}).encode('utf-8')
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('POST', '/sell', body=body, headers={'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': cookie})
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                self.assertIn('/receipts/', response.getheader('Location', ''))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.DB_PATH = original_db_path
            app.sell_item.__defaults__ = original_sell_defaults
            app.add_item.__defaults__ = original_add_defaults

        updated = list_items(self.db_path)[0]
        self.assertEqual(updated['stock'], 2)

    def test_printable_receipt_and_daily_report_routes(self):
        add_item('قهوة', 5, 7, stock=4, db_path=self.db_path)
        receipt_id, _ = create_sale_receipt([{'item_id': 1, 'quantity': 2}], customer_name='زبون مباشر', company_name='مكتب لارا للتجارة العامة', db_path=self.db_path)

        original_db_path = app.DB_PATH
        try:
            app.DB_PATH = self.db_path
            server = ThreadingHTTPServer(('127.0.0.1', 0), PosHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                cookie = self._login_cookie(port)
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', f'/receipts/{receipt_id}/print', headers={'Cookie': cookie})
                response = connection.getresponse()
                receipt_body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('وصل بيع', receipt_body)
                self.assertIn('مكتب لارا للتجارة العامة', receipt_body)
                self.assertIn('المستلم', receipt_body)

                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/reports/daily', headers={'Cookie': cookie})
                response = connection.getresponse()
                report_body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('التقرير اليومي', report_body)
                self.assertIn('زبون مباشر', report_body)
                self.assertIn('إجمالي المتبقي', report_body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.DB_PATH = original_db_path

    def test_customer_debt_details_and_payment_route(self):
        add_item('شيبس', 2, 4, stock=20, db_path=self.db_path)
        receipt_id, _ = create_sale_receipt(
            [{'item_id': 1, 'quantity': 3}],
            customer_name='زبون ديون ويب',
            payment_method='دين',
            db_path=self.db_path,
        )
        receipt = get_receipt(receipt_id, db_path=self.db_path)

        original_db_path = app.DB_PATH
        try:
            app.DB_PATH = self.db_path
            server = ThreadingHTTPServer(('127.0.0.1', 0), PosHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                cookie = self._login_cookie(port)
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', f"/customers/{receipt['customer_id']}/details?view=debts", headers={'Cookie': cookie})
                response = connection.getresponse()
                details_body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('دفتر الذمم (دين/سداد)', details_body)
                self.assertIn('name="payment_amount"', details_body)

                pay_body = urllib.parse.urlencode({'payment_amount': '5', 'payment_note': 'تسديد اختبار'}).encode('utf-8')
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request(
                    'POST',
                    f"/customers/{receipt['customer_id']}/debt/pay",
                    body=pay_body,
                    headers={'Content-Type': 'application/x-www-form-urlencoded', 'Cookie': cookie},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                redirect_location = response.getheader('Location', '')
                self.assertIn('tab=audit', redirect_location)
                self.assertIn('msg=', redirect_location)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        finally:
            app.DB_PATH = original_db_path

        updated_receipt = get_receipt(receipt_id, db_path=self.db_path)
        self.assertEqual(updated_receipt['paid_amount'], 5.0)
        self.assertEqual(updated_receipt['debt_amount'], 7.0)

    def test_reset_sales_and_profit_totals_uses_secure_baseline(self):
        add_item('ماء صحة', 1000, 1500, stock=20, db_path=self.db_path)
        add_item('كلينكس', 500, 800, stock=20, db_path=self.db_path)

        create_sale_receipt(
            [
                {'item_id': 1, 'quantity': 2},
                {'item_id': 2, 'quantity': 1},
            ],
            customer_name='زبون تصفير',
            payment_method='نقدي',
            db_path=self.db_path,
        )

        before_reset = get_summary(self.db_path)
        self.assertEqual(before_reset['total_sales'], 3800.0)
        self.assertEqual(before_reset['total_profit'], 1300.0)

        with self.assertRaises(PermissionError):
            reset_sales_profit_totals(
                actor_name='موظف',
                reason='محاولة بدون صلاحية',
                admin_pin='9999',
                db_path=self.db_path,
            )

        reset_sales_profit_totals(
            actor_name='مدير النظام',
            reason='فتح صفحة يوم جديد',
            admin_pin='1234',
            db_path=self.db_path,
        )

        after_reset = get_summary(self.db_path)
        self.assertEqual(after_reset['total_sales'], 0.0)
        self.assertEqual(after_reset['total_profit'], 0.0)

        create_sale_receipt(
            [{'item_id': 1, 'quantity': 1}],
            customer_name='بيع بعد التصفير',
            payment_method='نقدي',
            db_path=self.db_path,
        )
        after_new_sale = get_summary(self.db_path)
        self.assertEqual(after_new_sale['total_sales'], 1500.0)
        self.assertEqual(after_new_sale['total_profit'], 500.0)

        logs = list_audit_logs(self.db_path, limit=5)
        reset_logs = [log for log in logs if log['action'] == 'reset_totals']
        self.assertEqual([log['status'] for log in reset_logs], ['success', 'denied'])


if __name__ == '__main__':
    unittest.main()
