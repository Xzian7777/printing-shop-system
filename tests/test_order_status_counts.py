import json
import shutil
import subprocess
import unittest
from pathlib import Path
from backend_test_support import order_status_label, load_backend
from test_admin_sync import Database


class OrderStatusCountsTests(unittest.TestCase):
    def test_admin_history_includes_legacy_orders_missing_created_at(self):
        database = Database()
        collection = database.collection('artifacts/printcraft-pro/public/data/orders')
        collection.document('old').set({'status': 'Approved', 'timestamp': '2026-11-01T12:00:00'})
        collection.document('new').set({'status': 'Printing', 'createdAt': '2026-11-13T12:00:00'})
        backend = load_backend({'db': database, 'get_orders_ref': lambda: collection})
        rows = backend.customer_order_records({'uid': 'admin', 'role': 'admin'})
        self.assertEqual([row['id'] for row in rows], ['new', 'old'])

    def test_status_labels_preserve_legacy_compatibility(self):
        for status in ['Printing', 'Preparing', 'Processing']:
            self.assertEqual(order_status_label(status), 'Processing')
        self.assertEqual(order_status_label('Approved'), 'Approved')

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_each_counter_matches_the_orders_in_its_filter(self):
        root = Path(__file__).parents[1] / 'templates'
        base = (root / 'base.html').read_text(encoding='utf-8')
        admin = (root / 'admin.html').read_text(encoding='utf-8')
        helpers = base[base.index('    function orderStatusLabel'):base.index('    let actionNoticeTimer;')]
        counters = admin[admin.index('function updateBadgeCounters()'):admin.index('function setAdminStatusFilter(')]
        renderer = admin[admin.index('function renderAdminOrders()'):admin.index('function renderAdminRowHTML(')]
        script = '''
const vm=require('vm'), assert=require('assert/strict');
const statuses=['Order Submitted','For Approval','Approved','Printing','Preparing','Processing','Ready for Pickup','Completed','Cancelled','Rejected'];
const nodes={};
let rendered=[];
const context={
 allAdminOrdersCache:statuses.map((status,id)=>({id:String(id),customer:'Test',status})),
 currentAdminStatusFilter:'all',currentAdminPage:1,adminPageSize:100,currentAdminViewMode:'list',
 formatInteger:String,
 renderAdminRowHTML:o=>{rendered.push(o.id);return '<tr></tr>'},
 document:{getElementById:id=>nodes[id]??=(['adminSearchInput','adminServiceDropdown','adminSortDropdown'].includes(id)
   ? {value:id==='adminServiceDropdown'?'all':id==='adminSortDropdown'?'latest':''}:{textContent:'',innerHTML:''})},
};
vm.createContext(context);vm.runInContext(SOURCE,context);
context.updateBadgeCounters();
const expected={all:7,approval:2,printing:4,pickup:1,completed:1,cancelled:1,rejected:1};
const fields={all:'All',approval:'Approval',printing:'Printing',pickup:'Pickup',completed:'Completed',cancelled:'Cancelled',rejected:'Rejected'};
for(const [filter,count] of Object.entries(expected)){
 assert.equal(nodes['pillCount'+fields[filter]].textContent,String(count));
 context.currentAdminStatusFilter=filter;rendered=[];context.renderAdminOrders();
 assert.equal(rendered.length,count,'Filter mismatch '+filter);
}
assert.equal(context.orderStatusLabel('Printing'),'Processing');
assert.equal(context.orderStatusLabel('Preparing'),'Processing');
'''.replace('SOURCE', json.dumps(helpers + counters + renderer))
        result = subprocess.run(['node'], input=script, text=True, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
