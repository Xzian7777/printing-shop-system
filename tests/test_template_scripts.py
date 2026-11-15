"""Check browser script syntax without starting Flask or accessing Firebase."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path


class TemplateSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser feedback checks')
    def test_admin_feedback_and_logout_loading_handle_success_failure_and_retries(self):
        root = Path(__file__).parents[1] / 'templates'
        base = (root / 'base.html').read_text(encoding='utf-8')
        admin = (root / 'admin.html').read_text(encoding='utf-8')
        notice = base[base.index('    let actionNoticeTimer;'):base.index('    function openLogoutModal()')]
        update = admin[admin.index('const pendingOrderStatusUpdates'):admin.index('async function openReviewSpecsModal')]
        import json
        script = '''
const vm = require('vm');
const assert = require('assert/strict');
const source = SOURCE;
function setup() {
  const notice = {style:{}, classList:{remove(){}, add(){}}};
  const button = {disabled:false};
  const cancel = {disabled:false};
  const state = {fetches:0, navigated:null};
  const env = {
    document:{getElementById:()=>notice, querySelector:()=>cancel,
      querySelectorAll:()=>[{dataset:{orderId:'PRNT-123456'}, querySelectorAll:()=>[button]}]},
    setTimeout:()=>1, clearTimeout(){}, confirm:()=>true,
    fetchAdminOrders:async()=>{}, fetchAdminAnalytics(){},
    window:{location:{assign:url=>state.navigated=url}},
  };
  vm.createContext(env); vm.runInContext(source,env);
  return {env,notice,button,cancel,state};
}
(async()=>{
  const {env,notice,button,state}=setup();
  let finish;
  env.fetch=()=>{state.fetches++;return new Promise(resolve=>finish=resolve)};
  const first=env.updateOrderStatusFromBackend('PRNT-123456','Approved');
  assert.equal(button.disabled,true);
  assert.match(notice.textContent,/Updating/);
  await env.updateOrderStatusFromBackend('PRNT-123456','Approved');
  assert.equal(state.fetches,1);
  finish({ok:true,json:async()=>({success:true})});await first;
  assert.equal(notice.textContent,'You approved order PRNT-123456.');
  assert.equal(button.disabled,false);
  env.fetch=async()=>({ok:false,json:async()=>({message:'Approval was rejected.'})});
  await env.updateOrderStatusFromBackend('PRNT-123456','Approved');
  assert.equal(notice.textContent,'Approval was rejected.');
  assert.equal(button.disabled,false);
  const logout=setup();
  const link={setAttribute(){},removeAttribute(){},textContent:'Confirm'};
  let end;
  logout.env.fetch=()=>{logout.state.fetches++;return new Promise(resolve=>end=resolve)};
  const pending=logout.env.beginLogout(link);
  assert.match(link.innerHTML,/Logging out/);
  assert.equal(logout.cancel.disabled,true);
  await logout.env.beginLogout(link);
  assert.equal(logout.state.fetches,1);
  end({ok:true,url:'http://localhost/'});await pending;
  assert.equal(logout.state.navigated,'http://localhost/');
  const failed=setup();failed.env.fetch=async()=>{throw Error('offline')};
  await failed.env.beginLogout(link);
  assert.equal(link.textContent,'Confirm');
  assert.equal(failed.cancel.disabled,false);
  assert.match(failed.notice.textContent,/Unable to log out/);
})().catch(error=>{console.error(error);process.exitCode=1});
'''.replace('SOURCE', json.dumps(notice + '\n' + update))
        result = subprocess.run(['node'], input=script, text=True, capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_modified_pages_render(self):
        from flask import Flask, render_template, session
        root = Path(__file__).parents[1]
        app = Flask(__name__, template_folder=str(root / 'templates'), static_folder=str(root / 'static'))
        app.secret_key = 'template-test-only'
        with app.test_request_context('/'):
            session.update(username='Admin', email='admin@example.com', role='admin')
            for filename in ['admin.html', 'customer.html', 'store.html']:
                with self.subTest(template=filename):
                    self.assertIn('<html', render_template(filename))

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for browser script syntax checks')
    def test_modified_browser_scripts_parse(self):
        root = Path(__file__).parents[1] / 'templates'
        for name in ['rfid_reader.js', 'uploads.js']:
            external = root.parent / 'static' / 'js' / name
            result = subprocess.run(['node', '--check', str(external)], text=True, capture_output=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
        for filename in ['base.html', 'admin.html', 'store.html', 'order.html', 'customer.html']:
            html = (root / filename).read_text(encoding='utf-8')
            for index, script in enumerate(re.findall(r'<script\b[^>]*>(.*?)</script>', html, re.S | re.I)):
                if not script.strip(): continue
                script = re.sub(r'\{\{.*?\}\}', 'null', script, flags=re.S)
                script = re.sub(r'\{%.*?%\}', '', script, flags=re.S)
                with self.subTest(template=filename, script=index):
                    result = subprocess.run(['node', '--check'], input=script, text=True, capture_output=True, encoding='utf-8')
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__': unittest.main()
