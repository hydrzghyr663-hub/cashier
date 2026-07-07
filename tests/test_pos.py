import http.client
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer

import app
from app import PosHandler, add_customer, add_item, create_sale_receipt, delete_item, get_customer_debt_transactions, get_daily_receipts, get_receipt, get_receipt_lines, get_summary, init_db, list_audit_logs, list_customers, list_items, list_sales, pay_customer_debt, reset_sales_profit_totals, sell_item, update_admin_pin, update_item, verify_admin_pin


class PosSystemTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, 'test_pos.db')
        init_db(self.db_path)

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
        self.assertEqual([log['status'] for log in logs], ['success', 'denied', 'success', 'denied'])

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

        pin_logs = [log for log in list_audit_logs(self.db_path, limit=4) if log['action'] == 'update_pin']
        self.assertEqual([log['status'] for log in pin_logs], ['success', 'denied'])

    def test_http_home_page_shows_audit_tab(self):
        add_item('سكر', 3, 4, stock=5, db_path=self.db_path)

        original_db_path = app.DB_PATH
        try:
            app.DB_PATH = self.db_path
            server = ThreadingHTTPServer(('127.0.0.1', 0), PosHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_address[1]
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/')
                response = connection.getresponse()
                body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('الرقابة على التعديل والحذف', body)
                self.assertIn('هذا القسم مخصص للإضافة، مراجعة المخزون، وتعديل بيانات المنتجات فقط.', body)
                self.assertIn('إدارة رمز الأمان', body)
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
                body = urllib.parse.urlencode({'item_id': '1', 'quantity': '2', 'customer_name': 'زبون اختبار'}).encode('utf-8')
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('POST', '/sell', body=body, headers={'Content-Type': 'application/x-www-form-urlencoded'})
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
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', f'/receipts/{receipt_id}/print')
                response = connection.getresponse()
                receipt_body = response.read().decode('utf-8')
                self.assertEqual(response.status, 200)
                self.assertIn('وصل بيع', receipt_body)
                self.assertIn('مكتب لارا للتجارة العامة', receipt_body)
                self.assertIn('المستلم', receipt_body)

                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', '/reports/daily')
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
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                connection.request('GET', f"/customers/{receipt['customer_id']}/details?view=debts")
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
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 303)
                self.assertIn(f"/customers/{receipt['customer_id']}/details?view=debts", response.getheader('Location', ''))
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
