"""Responsive rendering must preserve existing record controls and dynamic updates."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ResponsiveTests(unittest.TestCase):
    def test_logout_dialog_retains_compact_width_and_green_theme(self):
        base = (ROOT / 'templates/base.html').read_text(encoding='utf-8')
        css = (ROOT / 'static/css/responsive.css').read_text(encoding='utf-8')
        modal = base.split('id="logoutModal"', 1)[1].split('<!-- ================= TERMS', 1)[0]
        self.assertIn('max-w-sm w-full', modal)
        self.assertIn('bg-emerald-700 hover:bg-emerald-800', modal)
        self.assertNotIn('bg-gradient', modal)
        self.assertIn('beginLogout(this)', modal)
        self.assertIn(':where(body .fixed.inset-0 > .bg-white', css)
        panel_styles = css.split('body .fixed.inset-0 > .order-action-panel {', 1)[1].split('}', 1)[0]
        self.assertNotIn('max-width:', panel_styles)

    def test_shared_assets_and_navigation_breakpoint(self):
        base = (ROOT / 'templates/base.html').read_text(encoding='utf-8')
        self.assertIn("filename='css/responsive.css'", base)
        self.assertIn("filename='js/responsive.js'", base)
        self.assertIn('lg:pl-56', base)
        self.assertNotIn('md:pl-56', base)
        self.assertIn('hidden lg:flex flex-col w-56', base)
        self.assertIn('inert aria-label="Main navigation"', base)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_records_resize_and_menu_position_without_cloning_actions(self):
        source = (ROOT / 'static/js/responsive.js').read_text(encoding='utf-8')
        script = r'''
const vm = require('vm');
const assert = require('assert/strict');
function element(classes=[]) {
  const set = new Set(classes);
  return {nodeType:1, dataset:{}, style:{setProperty(){}}, classList:{
    add(...xs){xs.forEach(x=>set.add(x))}, remove(...xs){xs.forEach(x=>set.delete(x))}, contains:x=>set.has(x)},
    setAttribute(k,v){this[k]=v}, querySelectorAll(){return []}, closest(){return null},
    matches(){return false}, addEventListener(){}, querySelector(){return null}};
}
const headers=['Reference','Customer','Actions'].map(text=>({...element(),textContent:text}));
const action=element();
const row={cells:[element(),element(),action]};
row.cells.forEach(cell=>cell.colSpan=1);
const parent=element();
const table={...element(),parentElement:parent,matches:s=>s==='table',
  querySelectorAll:s=>s==='thead th'?headers:s==='tbody tr'?[row]:[]};
const drawer=element();
const pane=element(['hidden']);
const inbox=element();
const anchor={getBoundingClientRect:()=>({left:280,right:318,top:650,bottom:694})};
const menu={...element(['hidden']),parentElement:{querySelector:()=>anchor},
  getBoundingClientRect:()=>({width:176,height:180}),
  matches(s){return s===':popover-open'&&this.open===true},
  showPopover(){this.open=true},hidePopover(){this.open=false}};
const events={}; let observer; let closed=0;
const document={body:element(),documentElement:element(),activeElement:null,
  createComment:()=>({}),querySelector:()=>({querySelectorAll:()=>[table]}),
  querySelectorAll:()=>[menu],addEventListener:(name,fn)=>events[name]=fn,
  getElementById:id=>({'mobileSidebarDrawer':drawer,'adminChatSidebar':inbox,'adminChatMainPane':pane}[id]||null)};
const window={innerWidth:320,innerHeight:700,addEventListener:(name,fn)=>events[name]=fn};
class Observer {constructor(fn){this.notify=fn;observer=this}observe(){}}
const env={document,window,MutationObserver:Observer,Node:{ELEMENT_NODE:1},toggleMobileSidebar(){closed++;drawer.classList.add('-translate-x-full')}};
vm.runInNewContext(SOURCE,env);
assert.equal(row.cells[0].dataset.label,'Reference');
assert.equal(row.cells[2],action);
assert.equal(action.dataset.label,'Actions');
assert.equal(headers[0].scope,'col');
assert(table.classList.contains('responsive-records'));
const added=element();added.colSpan=1;row.cells[1]=added;
observer.notify([{type:'childList',addedNodes:[table]}]);
assert.equal(added.dataset.label,'Customer');
menu.classList.remove('hidden');observer.notify([{type:'attributes',target:menu}]);
assert.equal(menu.open,true);
assert(parseFloat(menu.style.left)>=8);
assert(parseFloat(menu.style.left)+176<=312);
assert(parseFloat(menu.style.top)+180<=692);
menu.classList.add('hidden');observer.notify([{type:'attributes',target:menu}]);
assert.equal(menu.open,false);
window.innerWidth=1024;events.resize();
assert.equal(closed,1);
assert.equal(pane.classList.contains('hidden'),false);
window.innerWidth=320;events.resize();
assert.equal(pane.classList.contains('hidden'),true);
'''.replace('SOURCE', json.dumps(source))
        result = subprocess.run(['node'], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixture_uses_real_templates_and_synthetic_data(self):
        from responsive_fixture import app
        with app.test_client() as client:
            for page in ['admin', 'customer', 'store', 'order', 'login', 'index']:
                response = client.get('/' + page)
                self.assertEqual(response.status_code, 200, page)
                self.assertIn(b'responsive.js', response.data)
            self.assertEqual(client.post('/api/orders').status_code, 405)
