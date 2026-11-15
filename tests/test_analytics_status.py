import ast
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from flask import Flask
from backend_test_support import load_backend

ROOT = Path(__file__).parents[1]
ORDERS = [
    {'id': 'print', 'status': 'Approved', 'service': 'Document Printing', 'total': 100, 'copies': 2, 'pages': 3},
    {'id': 'store', 'status': 'Order Submitted', 'service': 'School Supplies', 'total': 200},
    {'id': 'pos', 'status': 'Completed', 'source': 'POS', 'total': 300, 'productTotal': 125, 'serviceTotal': 175},
    {'id': 'cancel-pos', 'status': 'Cancelled', 'source': 'POS', 'total': 4e12, 'productTotal': 3e12, 'serviceTotal': 1e12},
    {'id': 'PRNT-8069', 'status': 'Rejected: hahhahaag', 'service': 'Document Printing', 'total': 4.5e15},
    {'id': 'legacy-cancel', 'status': '  CANCELED by customer ', 'service': 'In-Store Supplies', 'total': 4e12},
    {'id': 'free', 'status': 'Completed', 'total': 0, 'price': 999},
]


class AnalyticsStatusTests(unittest.TestCase):
    def test_api_excludes_stopped_amounts_from_totals_components_and_average(self):
        backend = load_backend({'get_orders': lambda: ORDERS,
            'identity': lambda **kwargs: {'uid': 'admin', 'role': 'admin'},
            'order_history_revision': lambda actor: 'existing-revision'})
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'get_analytics')
        node.decorator_list = []
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), backend.__dict__)
        app = Flask(__name__)
        app.add_url_rule('/api/analytics', view_func=backend.get_analytics)
        response = app.test_client().get('/api/analytics', headers={'If-None-Match': '"existing-revision"'})
        self.assertEqual(response.status_code, 200)
        data = response.json
        for key, expected in {'totalSales': 600, 'storeRevenue': 325, 'printRevenue': 275,
                              'totalOrders': 7, 'avgOrderValue': 150, 'totalPagesPrinted': 8}.items():
            self.assertEqual(data[key], expected, key)
        self.assertEqual(data['statusCounts']['Rejected'], 1)
        self.assertEqual(data['statusCounts']['Cancelled'], 2)
        self.assertEqual(backend.get_analytics_summary()['activeOrders'], 2)

    def test_legacy_currency_zero_and_non_finite_amounts(self):
        backend = load_backend()
        for value in ['NaN', 'Infinity', -10, True, 'bad']:
            self.assertEqual(backend.analytics_order_amount({'status': 'Approved', 'total': value}), 0)
        self.assertEqual(backend.analytics_order_amount({'total': '₱1,200.50'}), 1200.5)
        self.assertEqual(backend.analytics_order_amount({'total': 0, 'price': 99}), 0)
        self.assertEqual(backend.analytics_order_amount({'total': None, 'price': None, 'amount': 50}), 50)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_web_uses_fresh_orders_and_excludes_rejected_and_cancelled_pos_revenue(self):
        base = (ROOT / 'templates/base.html').read_text(encoding='utf-8')
        admin = (ROOT / 'templates/admin.html').read_text(encoding='utf-8')
        helpers = base[base.index('    function orderStatusLabel'):base.index('    let actionNoticeTimer;')]
        analytics = admin[admin.index('function fetchAdminAnalytics()'):admin.index('function renderAnalyticsCharts(')]
        fetch_orders = admin[admin.index("let adminOrdersEtag = '';"):admin.index('function updateBadgeCounters()')]
        script = r'''
const vm=require('vm'), assert=require('node:assert/strict');
const nodes={};let chart;let fetched=0;
const env={allAdminOrdersCache:[],currentAdminTab:'analytics',formatCurrency:Number,formatInteger:Number,
 document:{getElementById:id=>nodes[id]??={textContent:''}},
 renderAnalyticsCharts:data=>chart=data, updateBadgeCounters(){},renderAdminOrders(){},
 fetch:async()=>{fetched++;return {ok:true,status:200,headers:{get:()=> 'revision'},json:async()=>ORDERS}}};
vm.createContext(env);vm.runInContext(SOURCE,env);
(async()=>{
 await env.fetchAdminAnalytics();
 assert.equal(fetched,1,'Refresh must fetch once without recursion');
 assert.equal(chart.totalSales,600);assert.equal(chart.storeRevenue,325);assert.equal(chart.printRevenue,275);
 assert.equal(chart.avgOrderValue,150);assert.equal(chart.totalOrders,7);assert.equal(chart.totalPagesPrinted,8);
 assert.equal(chart.statusCounts.Rejected,1);assert.equal(chart.statusCounts.Cancelled,2);
 assert.equal(nodes.statTotalSales.textContent,600);
 env.allAdminOrdersCache=ORDERS.map(order=>({...order,status:'Cancelled'}));env.renderAdminAnalytics();
 assert.equal(chart.totalSales,0);assert.equal(chart.storeRevenue,0);assert.equal(chart.printRevenue,0);
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE', json.dumps(helpers + analytics + fetch_orders)).replace('ORDERS', json.dumps(ORDERS))
        result = subprocess.run(['node'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
